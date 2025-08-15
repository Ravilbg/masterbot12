# handlers/poll_details.py — detail-view игр + manual-switches
# ─────────────────────────────────────────────────────────────────────────────
"""
Реактивные карточки игр (detail-view) для цикла распределения.

Версия 13.2 · 2025-08-13
──────────────────────────────────────────────────────────────────────────────
• Единый источник правды по распределению — state.distribution_cache[str(deal_id)].
• Жёсткая инварианта «1 пользователь = 1 роль» (приоритет main > assist > admin > trainee).
• Автоподбор в пустые слоты по «Светофору» (green→main, yellow→assist).
• Стажёр: только «красный», не занятый в других ролях; стажёр не влияет на укомплектованность.
• SWAP переносит кандидата между ролями, автоматически убирая его из прежних ролей.
• Кнопка «Утвердить» делегируется в handlers.polls_distribution, без автопереходов.
• Пылесос в ЛС: остаётся только текущая группа сообщений с деталями.
• Совместимость сигнатур refresh_deal_details:
  – новый стиль: refresh_deal_details(bot=Bot, deal_id=int, force_approved:bool=False, uid:Optional[int]=None)
  – старый стиль: refresh_deal_details(uid:int, deal_id:int)
"""
# ███ [0] IMPORTS & SETUP
# --------------------------------------------------------------------
from __future__ import annotations

import logging
import re
import time
import contextlib  # ← добавлено для suppress(...) в [2]
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from aiogram import Bot, Router, types
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config import settings
from core.state import state
from core.utils import truncate, delete_previous_private_messages
from services.gsheets import get_user_status_from_svetofor

logger = logging.getLogger(__name__)
router = Router(name="poll_details")

# Общие константы/регексы
POLL_BACK = "poll_back_to_games_list"
_OK_STATUSES = {"green", "yellow"}            # для помощников
_STATUS_RE = re.compile(r"[^\w\d]+", re.UNICODE)
# Требование администратора по пакетам (нормализованные названия)
_ADMIN_PKGS = {"стандарт", "стандарт+", "премиум", "vip", "вип", "биглион"}

# Локальный кэш статусов «Светофора»: key -> (status, ts)
_local_status_cache: Dict[str, Tuple[str, float]] = {}
STATUS_CACHE_TTL = 60 * 60 * 4  # 4 часа

# История изменений:
# • 2025-08-13 — пересобран импорт, единая точка пылесоса, локальный статус-кэш.
# • 2025-08-15 — добавлен import contextlib для suppress() в блоке [2].


# ███ [1] HELPERS (normalize, role cfg, svetofor cache, tags/ids, invariants)
# --------------------------------------------------------------------
"""
Хелперы для detail-view:
• нормализация имени игры и толерантный поиск конфигурации ролей;
• локальный кэш «Светофора»;
• форматирование тегов «Имя.Суффикс|uid» и извлечение uid;
• инварианта «1 пользователь → 1 роль» внутри одной игры.
"""

def _clean(s: str) -> str:
    return _STATUS_RE.sub(" ", (s or "")).lower().strip()


def _role_cfg(game_name: str) -> Dict[str, int]:
    """
    Возвращает требуемые количества ролей для игры (толерантный поиск).
    Формат: {"main_leaders": int, "assistants": int}
    """
    from difflib import SequenceMatcher
    norm = _clean(game_name)
    best_ratio = 0.0
    best_cfg: Optional[Dict[str, int]] = None
    for key, cfg in (getattr(settings, "GAME_ROLE_MAPPING", {}) or {}).items():
        k_norm = _clean(str(key))
        if norm == k_norm or norm in k_norm or k_norm in norm:
            return cfg
        ratio = SequenceMatcher(None, norm, k_norm).ratio()
        if ratio > best_ratio:
            best_ratio, best_cfg = ratio, cfg
    return best_cfg or {"main_leaders": 1, "assistants": 0}


async def _status_cached(uid: int, game: str) -> str:
    """Локальный кэш статуса пользователя по игре из таблицы «Светофор»."""
    key = f"sv:{uid}:{_clean(game)}"
    now = time.time()
    if key in _local_status_cache:
        status, ts = _local_status_cache[key]
        if now - ts < STATUS_CACHE_TTL:
            return status
    try:
        status = await get_user_status_from_svetofor(uid, game)
    except Exception as e:
        logger.warning("[svetofor] fail uid=%s game=%s: %s", uid, game, e)
        status = ""
    _local_status_cache[key] = (status, now)
    return status


async def _short_name(uid: int) -> str:
    """Имя в формате «Имя Ф.»; надёжный фолбэк по uid."""
    try:
        from core.db import get_user_info
        ui = get_user_info(uid)
        if hasattr(ui, "__await__"):
            ui = await ui  # поддержка async реализации
        ui = ui or {}
    except Exception:
        ui = {}
    fn = str(ui.get("first_name") or "").strip()
    ln = str(ui.get("last_name") or "").strip()
    li = (ln[:1].upper() + ".") if ln else str(ui.get("last_name_initial") or "").strip()
    res = (f"{fn} {li}".strip() or f"uid:{uid}").strip()
    return res


async def _fmt(uid_: int, role_key: str) -> str:
    """
    Формирует тег для кэша распределения/подтверждений:
      main    -> «Имя Ф.1|uid»
      assist  -> «Имя Ф.2|uid»
      admin   -> «Имя Ф.Адм|uid»
      trainee -> «Имя Ф.Стаж|uid»
    Источник имени — сначала state.user_short (там уже «Имя Ф.»), затем fallback на БД.
    """
    # 1) предпочитаем кэш коротких имён (формат «Имя Ф.»)
    human = None
    try:
        human = getattr(state, "user_short", {}).get(uid_)
    except Exception:
        human = None
    # 2) фолбэк на базу
    if not human:
        human = await _short_name(uid_)

    suffix = {
        "main": ".1",
        "assist": ".2",
        "admin": ".Адм",
        "trainee": ".Стаж",
    }.get(role_key, "")
    return f"{human}{suffix}|{uid_}".strip()


def _tag_uid(tag: Optional[str]) -> Optional[int]:
    if not tag or "|" not in str(tag):
        return None
    try:
        return int(str(tag).rsplit("|", 1)[-1])
    except Exception:
        return None


def _role_slots(need_main: int, need_assist: int) -> Tuple[List[str], List[str]]:
    """Возвращает списки ключей слотов для main/assist в distribution_cache."""
    leads = [f"lead{i}" for i in range(1, max(1, need_main) + 1)]
    assis = [f"assistant{i}" for i in range(1, max(0, need_assist) + 1)]
    return leads, assis


def _need_admin_by_package(pkg_raw: str) -> int:
    return 1 if _clean(pkg_raw) in _ADMIN_PKGS else 0


async def _ensure_single_role(dist: Dict[str, str], need_main: int, need_assist: int) -> None:
    """
    Инварианта «один UID — одна роль» для одной игры.
    Если UID встречается в нескольких местах, сохраняем первое в порядке приоритета:
      main > assist > admin > trainee; остальные вхождения вычищаем.
    Также подчищаем слоты вне допустимых диапазонов.
    """
    leads, assis = _role_slots(need_main, need_assist)
    priority_keys: List[str] = [*leads, *assis, "admin", "trainee"]

    seen: Set[int] = set()
    for key in priority_keys:
        val = dist.get(key)
        uid = _tag_uid(val)
        if uid is None:
            continue
        if uid in seen:
            dist.pop(key, None)
        else:
            seen.add(uid)

    # убрать мусорные ключи вне диапазона
    for key in list(dist.keys()):
        if key.startswith("lead"):
            try:
                idx = int("".join(ch for ch in key if ch.isdigit()) or "0")
            except Exception:
                idx = 0
            if idx < 1 or idx > need_main:
                dist.pop(key, None)
        elif key.startswith("assistant"):
            try:
                idx = int("".join(ch for ch in key if ch.isdigit()) or "0")
            except Exception:
                idx = 0
            if idx < 1 or idx > need_assist:
                dist.pop(key, None)


async def _normalize_tag_texts(dist: Dict[str, str], need_main: int, need_assist: int) -> None:
    """
    Приводит текст тегов к каноническому виду «Имя Ф.+суффикс|uid», пересобирая по uid.
    Чинит старые записи вида «Антон.1|uid», где отсутствовал инициал.
    """
    leads, assis = _role_slots(need_main, need_assist)

    # main / assist
    for role, keys in (("main", leads), ("assist", assis)):
        for k in keys:
            uid = _tag_uid(dist.get(k))
            if uid:
                dist[k] = await _fmt(uid, role)

    # admin / trainee
    for role, k in (("admin", "admin"), ("trainee", "trainee")):
        uid = _tag_uid(dist.get(k))
        if uid:
            dist[k] = await _fmt(uid, role)

# История изменений: добавлены _need_admin_by_package, жёсткая инварианта и нормализация текстов тегов (2025-08-13)

# ███ [1.5] UTILS: enforce_single_role (совместимо с polls_distribution)
# --------------------------------------------------------------------
from typing import Any, Dict, List, Optional, Set

def _enforce_single_role(data: Dict[str, Any], *, need_main: Optional[int] = None, need_assist: Optional[int] = None) -> Dict[str, Any]:
    """
    Универсальная защита «1 uid → 1 роль» с приоритетом: main > assist > admin > trainee.

    Принимает два типа структур:
    1) РОЛЕВАЯ:
       {"main":[int], "assist":[int], "admin":[int]} → вернёт НОВЫЙ dict без дублей.
    2) СЛОТОВАЯ (distribution_cache/poll_details):
       {"lead1":"Имя.1|uid", "assistant1":"Имя.2|uid", "admin":"Имя.Адм|uid", ...}
       → чистит ДУБЛИ НА МЕСТЕ и подрезает лишние слоты за пределами need_main/need_assist.
         Возвращает исходный dict (для удобства чейнинга).

    Параметры need_main/need_assist нужны только для слотовой схемы.
    Если не заданы — определяются по максимальному индексу lead*/assistant* в data.
    """
    # Ветка 1: ролевая структура
    if any(k in data for k in ("main", "assist", "admin")) and not any(
        k for k in data.keys() if isinstance(k, str) and (k.startswith("lead") or k.startswith("assistant") or k in {"admin", "trainee"})
    ):
        roles_in = {
            "main":  [int(u) for u in (data.get("main")   or []) if isinstance(u, int)],
            "assist":[int(u) for u in (data.get("assist") or []) if isinstance(u, int)],
            "admin": [int(u) for u in (data.get("admin")  or []) if isinstance(u, int)],
        }
        seen: Set[int] = set()
        out: Dict[str, List[int]] = {"main": [], "assist": [], "admin": []}
        for u in roles_in["main"]:
            if u not in seen:
                out["main"].append(u); seen.add(u)
        for u in roles_in["assist"]:
            if u not in seen:
                out["assist"].append(u); seen.add(u)
        for u in roles_in["admin"]:
            if u not in seen:
                out["admin"].append(u); seen.add(u)
        return out  # новый объект

    # Ветка 2: слотовая структура
    dist: Dict[str, Any] = data

    # Определяем потребности, если не заданы
    if need_main is None:
        max_lead = 0
        for k in dist.keys():
            if isinstance(k, str) and k.startswith("lead"):
                with contextlib.suppress(Exception):
                    max_lead = max(max_lead, int("".join(ch for ch in k if ch.isdigit()) or "0"))
        need_main = max(1, max_lead)
    if need_assist is None:
        max_asst = 0
        for k in dist.keys():
            if isinstance(k, str) and k.startswith("assistant"):
                with contextlib.suppress(Exception):
                    max_asst = max(max_asst, int("".join(ch for ch in k if ch.isdigit()) or "0"))
        need_assist = max(0, max_asst)

    leads = [f"lead{i}" for i in range(1, max(1, int(need_main)) + 1)]
    assis = [f"assistant{i}" for i in range(1, max(0, int(need_assist)) + 1)]
    priority_keys: List[str] = [*leads, *assis, "admin", "trainee"]

    seen_uids: Set[int] = set()
    # проход по приоритету: первое вхождение uid остаётся, последующие вычищаем
    for key in priority_keys:
        val = dist.get(key)
        uid = _tag_uid(val) if isinstance(val, str) else None
        if uid is None:
            continue
        if uid in seen_uids:
            dist.pop(key, None)
        else:
            seen_uids.add(uid)

    # подрезаем мусорные ключи вне диапазонов
    for key in list(dist.keys()):
        if isinstance(key, str) and key.startswith("lead"):
            with contextlib.suppress(Exception):
                idx = int("".join(ch for ch in key if ch.isdigit()) or "0")
                if idx < 1 or idx > int(need_main):
                    dist.pop(key, None)
        elif isinstance(key, str) and key.startswith("assistant"):
            with contextlib.suppress(Exception):
                idx = int("".join(ch for ch in key if ch.isdigit()) or "0")
                if idx < 1 or idx > int(need_assist):
                    dist.pop(key, None)

    return dist  # тот же объект (in-place), для удобства использования

# ███ [2] CORE RENDER — общий рендер карточки (используется show/refresh)
# --------------------------------------------------------------------
async def _render_detail(uid: int, deal_id: int, bot: Bot, *, force_approved: bool = False) -> None:
    """
    Рисует карточку игры user→deal_id:
      • автоподбор в пустые слоты с учётом «Светофора»;
      • инварианта «1 uid → 1 роль»;
      • стажёр (красный);
      • кнопки «Утвердить/Стоп/Назад».
    Аргумент force_approved зарезервирован для совместимости разметки.
    """
    # Пылесос: в деталях оставляем только текущий блок
    try:
        await delete_previous_private_messages(bot, uid, keep=[])
    except TypeError:
        # совместимость со старой сигнатурой
        with contextlib.suppress(Exception):
            await delete_previous_private_messages(uid)  # type: ignore

    # Поиск сделки в текущем опросе
    deal = next((d for d in (state.current_poll_deals or []) if int(d.get("id") or 0) == deal_id), None)
    if not deal:
        await bot.send_message(uid, "⚠️ Игра не найдена или уже закрыта.")
        return

    g_name = str(deal.get("game_name") or deal.get("name") or "Игра")
    # дата/время
    date_s = deal.get("event_datetime")
    date_s = date_s.strftime("%d.%m.%Y") if hasattr(date_s, "strftime") else str(deal.get("event_date") or "—")
    time_s = str(deal.get("event_time") or "—")
    # пакет/игроки
    pkg_raw = str(deal.get("package") or "—").strip()
    pkg_icon = {"компакт": "🎒", "стандарт": "📦", "стандарт+": "📦➕", "премиум": "💎", "vip": "👑", "вип": "👑"}.get(_clean(pkg_raw), "🎁")
    players = truncate(str(deal.get("players") or deal.get("players_count") or "—"), 40)

    header = (
        f"🎮 *{g_name}*\n"
        f"📅 {date_s} · 🕒 {time_s}\n"
        f"📦 *Пакет:* {pkg_icon} {pkg_raw}\n"
        f"👥 *Игроки:* {players}"
    )
    msgs: List[types.Message] = [await bot.send_message(uid, header, parse_mode="Markdown")]

    # Требуемые роли
    cfg = _role_cfg(g_name)
    need_main = int(cfg.get("main_leaders", 1))
    need_assist = int(cfg.get("assistants", 0))
    need_admin = _need_admin_by_package(pkg_raw)

    # Кандидаты по откликам
    respondents: Dict[int, Dict[str, Any]] = {}
    for pdata in (state.responses or {}).values():
        # Отклики по сделкам
        for u in (pdata.get("deals", {}).get(deal_id, []) or []):
            respondents[int(u["user_id"])] = {**respondents.get(int(u["user_id"]), {}), **u}
        # Админ-доступность
        for adm in (pdata.get("admin_available", []) or []):
            respondents[int(adm["user_id"])] = {**respondents.get(int(adm["user_id"]), {}), **adm}

    # Текущее распределение из кэша
    dist: Dict[str, str] = getattr(state, "distribution_cache", {}).setdefault(str(deal_id), {})
    await _ensure_single_role(dist, need_main, need_assist)
    await _normalize_tag_texts(dist, need_main, need_assist)  # нормализация текста тегов

    chosen_global: Set[int] = set()  # занятые в любой роли (включая стажёра)

    async def _fits(user_id: int, role: str) -> bool:
        if role == "admin":
            return bool(respondents.get(user_id, {}).get("is_admin_eligible"))
        st = await _status_cached(user_id, g_name)
        if role == "main":
            return st == "green"
        if role == "assist":
            return st in _OK_STATUSES
        return False

    async def _render_role(role: str, title: str, icon: str, need: int) -> None:
        nonlocal dist, chosen_global, msgs
        if need <= 0:
            # подчистим мусор для ролей, которых не требуется
            if role == "admin":
                dist.pop("admin", None)
            else:
                prefix = "lead" if role == "main" else "assistant"
                for i in range(1, 5):  # безопасная граница
                    dist.pop(f"{prefix}{i}", None)
            return

        # Фикс дублей/формата: инварианта и нормализация ДО чтения слотов
        await _ensure_single_role(dist, need_main, need_assist)
        await _normalize_tag_texts(dist, need_main, need_assist)

        prefix = "lead" if role == "main" else ("assistant" if role == "assist" else "admin")

        def _chosen_from_dist() -> List[Tuple[int, str]]:
            out: List[Tuple[int, str]] = []
            if role == "admin":
                uid0 = _tag_uid(dist.get("admin"))
                if uid0 and (not respondents or uid0 in respondents):
                    out.append((uid0, "🛡️"))
            else:
                for i in range(1, need + 1):
                    uid0 = _tag_uid(dist.get(f"{prefix}{i}"))
                    if uid0 and (not respondents or uid0 in respondents):
                        out.append((uid0, ""))
            return out

        chosen: List[Tuple[int, str]] = _chosen_from_dist()
        if role != "admin":
            tmp = []
            for u, _ in chosen:
                st = await _status_cached(u, g_name)
                tmp.append((u, "🟢" if (role == "main") else ("🟢" if st == "green" else "🟡")))
            chosen = tmp

        for u, _ in chosen:
            chosen_global.add(u)

        # Автодобор
        for u in respondents.keys():
            if len(chosen) >= need:
                break
            if u in chosen_global:
                continue
            if await _fits(u, role):
                if role == "admin":
                    chosen.append((u, "🛡️")); chosen_global.add(u)
                    dist["admin"] = await _fmt(u, "admin")
                else:
                    st = await _status_cached(u, g_name)
                    mark = "🟢" if (role == "main") else ("🟢" if st == "green" else "🟡")
                    chosen.append((u, mark)); chosen_global.add(u)
                    # записываем в первый пустой слот
                    for i in range(1, need + 1):
                        slot = f"{prefix}{i}"
                        if not dist.get(slot):
                            dist[slot] = await _fmt(u, role)
                            break

        # Синхронизация кэша и выравнивание слотов
        if role == "admin":
            if need == 1 and chosen and not dist.get("admin"):
                dist["admin"] = await _fmt(chosen[0][0], "admin")
        else:
            idx = 1
            for uid_ch, _m in chosen[:need]:
                dist[f"{prefix}{idx}"] = await _fmt(uid_ch, role)
                idx += 1
            # удалить лишние
            for i in range(idx, need + 3):  # небольшой верхний зазор
                dist.pop(f"{prefix}{i}", None)

        # Повторная гарантия + нормализация ПОСЛЕ автодобора
        await _ensure_single_role(dist, need_main, need_assist)
        await _normalize_tag_texts(dist, need_main, need_assist)

        # Пересчитываем chosen после чистки/нормализации
        chosen = _chosen_from_dist()
        if role != "admin":
            tmp = []
            for u, _ in chosen:
                st = await _status_cached(u, g_name)
                tmp.append((u, "🟢" if (role == "main") else ("🟢" if st == "green" else "🟡")))
            chosen = tmp

        # Вывод блока роли
        ready = len(chosen) >= need

        def _nm(u: int) -> Optional[str]:
            try:
                return getattr(state, "user_short", {}).get(u)
            except Exception:
                return None

        lines = [
            f"─────{icon} *{title.upper()}* ────",
            f"{'✅' if ready else '❌'} {min(len(chosen), need)}/{need}",
        ]
        for u, mark in chosen[:max(need, 0)]:
            human = _nm(u) or (await _short_name(u))
            lines.append(f"– {human} {mark}")

        msgs.append(await bot.send_message(uid, "\n".join(lines), parse_mode="Markdown"))

        # Альтернативы
        chosen_role_uids = {u for u, _ in chosen}
        alts = [u for u in respondents.keys() if (u not in chosen_role_uids) and await _fits(u, role)]
        if alts:
            kb = InlineKeyboardBuilder()
            for u in alts:
                if role == "admin":
                    mark = "🛡️"
                else:
                    st = await _status_cached(u, g_name)
                    mark = "🟢" if (role == "main") else ("🟢" if st == "green" else "🟡")
                human = (getattr(state, "user_short", {}) or {}).get(u) or (await _short_name(u))
                kb.button(
                    text=f"{human} {mark}",
                    callback_data=f"swap_{deal_id}_{role}_{u}",
                )
            kb.adjust(1)
            msgs.append(await bot.send_message(uid, "🔁 Альтернатива:", reply_markup=kb.as_markup()))

    # Роли
    await _render_role("main", "Ведущие", "🎤", need_main)
    await _render_role("assist", "Помощники", "🧑‍🤝‍🧑", need_assist)

    # Админ по пакетам
    if need_admin:
        await _render_role("admin", "Администратор", "🛡️", 1)
    else:
        dist.pop("admin", None)

    # Финальная гарантия + нормализация (на всякий)
    await _ensure_single_role(dist, need_main, need_assist)
    await _normalize_tag_texts(dist, need_main, need_assist)

    # Стажёр: выбираем КРАСНОГО, который НЕ занят в других ролях
    trainee_uid: Optional[int] = _tag_uid(dist.get("trainee"))
    red_pool: List[int] = []
    occupied: Set[int] = {
        *[(_tag_uid(dist.get(f"lead{i}")) or -1) for i in range(1, need_main + 1)],
        *[(_tag_uid(dist.get(f"assistant{i}")) or -1) for i in range(1, need_assist + 1)],
        (_tag_uid(dist.get("admin")) or -1),
    }
    for u in respondents.keys():
        if u in occupied:
            continue
        st = await _status_cached(u, g_name)
        if st == "red":
            red_pool.append(u)
    if red_pool:
        if trainee_uid not in red_pool:
            trainee_uid = red_pool[0]
            dist["trainee"] = await _fmt(trainee_uid, "trainee")
        human = (getattr(state, "user_short", {}) or {}).get(trainee_uid) or (await _short_name(trainee_uid))  # type: ignore
        block = [
            "───── 👷 *СТАЖЁР* ─────",
            f"– {human} 🔴",
            "_Стажёр не влияет на индикатор набора._",
        ]
        msgs.append(await bot.send_message(uid, "\n".join(block), parse_mode="Markdown"))
    else:
        dist.pop("trainee", None)

    # Кнопки управления (без автопереходов)
    is_locked = (deal_id in (getattr(state, "locked_distribution", {}) or {})) or (str(deal_id) in (getattr(state, "locked_distribution", {}) or {}))
    is_force_closed = deal_id in (getattr(state, "deal_force_closed", set()) or set())
    if (getattr(state, "current_poll_leader", None) == uid) and not is_force_closed:
        kb_mgr = InlineKeyboardBuilder()
        if is_locked or force_approved:
            kb_mgr.button(text="✅ Утверждено", callback_data="noop")
        else:
            kb_mgr.button(text="✅ Утвердить игру", callback_data=f"poll_approve_{deal_id}")
        kb_mgr.button(text="⏹️ Стоп набор", callback_data=f"poll_stop_{deal_id}")
        kb_mgr.adjust(1)
        msgs.append(await bot.send_message(uid, "🛠 Управление:", reply_markup=kb_mgr.as_markup()))

    # Назад к списку
    kb_back = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к списку", callback_data=POLL_BACK)]]
    )
    msgs.append(await bot.send_message(uid, "\u2060", reply_markup=kb_back))

    # Активный блок деталей — для пылесоса
    getattr(state, "detail_blocks", {}).setdefault((uid, deal_id), msgs)
# История изменений:
# • 2025-08-15 — добавлен параметр force_approved; выровнены пылесос/фолбэки; нормализация тегов до/после автодобора;
#                аккуратная работа с отсутствующими респондентами; совместимость уведомлений и кнопок.



# ███ [3] HANDLERS — show / refresh / swap / back
# ─────────────────────────────────────────────────────────────────────
from contextlib import suppress
from typing import Optional, Any, Dict, List, Tuple
import handlers.polls_lifecycle as plc  # для _check_ready_state/_sync_leader_report/_refresh_detail_views

@router.callback_query(lambda c: c.data and c.data.startswith("show_deal_"))
async def show_deal_callback_handler(callback: types.CallbackQuery) -> None:
    """Открыть detail-view конкретной игры из отчёта."""
    try:
        did = int(str(callback.data).rsplit("_", 1)[-1])
    except Exception:
        with suppress(Exception):
            await callback.answer("⚠️ Неверный формат кнопки.", show_alert=True)
        return
    await _render_detail(uid=callback.from_user.id, deal_id=did, bot=callback.message.bot, force_approved=False)
    with suppress(Exception):
        await callback.answer()

async def refresh_deal_details(
    bot: Optional[Bot] = None,
    deal_id: Optional[int] = None,
    *,
    force_approved: bool = False,
    uid: Optional[int] = None,
    # совместимость с legacy: refresh_deal_details(uid, deal_id)
    **kwargs: Any,
) -> None:
    """
    Перерисовка detail-view для конкретной сделки.

    Правила:
    • Не «автооткрывает» карточку, если она не была открыта.
    • Если uid не передан, ищем владельца по state.detail_blocks[(uid, deal_id)].
      Если владельца нет — ТИХО ВЫХОДИМ.
    • Поддерживает старую позиционную сигнатуру refresh_deal_details(uid, deal_id).
    """
    # legacy позиционная сигнатура: (uid, deal_id)
    if uid is None and deal_id is None:
        args = kwargs.pop("args", ()) or ()
        if args:
            with suppress(Exception):
                if len(args) >= 1 and isinstance(args[0], int):
                    uid = args[0]
                if len(args) >= 2 and isinstance(args[1], int):
                    deal_id = args[1]

    # бот
    bot = bot or Bot.get_current()

    # если uid не передан — попробуем найти владельца уже открытого detail-блока
    if uid is None and deal_id is not None:
        try:
            for (u, d), msgs in (getattr(state, "detail_blocks", {}) or {}).items():
                if int(d) == int(deal_id) and msgs:
                    uid = int(u)
                    break
        except Exception:
            uid = None

    if not bot or uid is None or deal_id is None:
        logger.debug(
            "[poll_details.refresh] skip (no open view): bot=%s uid=%s deal_id=%s",
            bool(bot), uid, deal_id
        )
        return

    await _render_detail(uid=uid, deal_id=deal_id, bot=bot, force_approved=force_approved)

@router.callback_query(lambda c: c.data and c.data.startswith("swap_"))
async def poll_swap_handler(callback: types.CallbackQuery) -> None:
    """
    SWAP-кнопка из блока «Альтернативы»:
    • переносит выбранного кандидата в целевую роль;
    • удаляет его из всех прочих ролей;
    • пересчитывает «готовность», синхронизирует отчёт и перерисовывает detail-view.
    Формат data: swap_{deal_id}_{targetRole}_{uid}
        targetRole ∈ {main, assist, admin}
    """
    data_s = str(callback.data or "")
    try:
        _, deal_id_s, role, uid_s = data_s.split("_", 3)
        deal_id = int(deal_id_s)
        uid_new = int(uid_s)
    except Exception:
        with suppress(Exception):
            await callback.answer("⚠️ Ошибка формата кнопки.", show_alert=True)
        return

    # Текущее распределение
    dist_map = getattr(state, "distribution_cache", {}) or {}
    dist: Dict[str, str] = dist_map.setdefault(str(deal_id), {})
    # Мета по игре
    details = (getattr(state, "poll_details", {}) or {}).get(deal_id) or {}
    game_name = details.get("game_name") or details.get("title") or ""
    cfg = _role_cfg(str(game_name))
    need_main   = int(cfg.get("main_leaders", 1) or 1)
    need_assist = int(cfg.get("assistants", 1) or 1)

    # 1) убрать uid_new из ВСЕХ слотов (инварианта 1 uid = 1 роль)
    for k, v in list(dist.items()):
        if isinstance(k, str) and (k.startswith("lead") or k.startswith("assistant") or k in {"admin", "trainee"}):
            if _tag_uid(v) == uid_new:
                dist[k] = ""

    # 2) записать в нужную роль
    if role == "admin":
        dist["admin"] = await _fmt(uid_new, "admin")
    else:
        prefix = "lead" if role == "main" else "assistant"
        # найдём первый пустой целевой слот, иначе — первый по порядку
        target_key = None
        for idx in range(1, (need_main if prefix == "lead" else need_assist) + 1):
            k = f"{prefix}{idx}"
            if not _tag_uid(dist.get(k)):
                target_key = k
                break
        if target_key is None:
            target_key = f"{prefix}1"
        dist[target_key] = await _fmt(uid_new, "main" if role == "main" else "assist")

    # 3) нормализация + гарантия инварианты
    await _ensure_single_role(dist, need_main, need_assist)
    await _normalize_tag_texts(dist, need_main, need_assist)

    # 4) синхронизация UI: сводный отчёт и реактивные detail-view
    try:
        await plc._check_ready_state({deal_id})
    except Exception as e:
        logger.warning("[swap] ready-state failed: %s", e)
    try:
        await plc._sync_leader_report()
    except Exception as e:
        logger.warning("[swap] report-sync failed: %s", e)
    try:
        await plc._refresh_detail_views({deal_id}, refresh_all=False)
    except Exception as e:
        logger.warning("[swap] details-refresh failed: %s", e)

    # 5) локальная перерисовка для инициатора
    try:
        await refresh_deal_details(bot=callback.message.bot, deal_id=deal_id, uid=callback.from_user.id)
    except TypeError:
        with suppress(Exception):
            await refresh_deal_details(callback.from_user.id, deal_id)  # type: ignore

    with suppress(Exception):
        await callback.answer("✅ Состав обновлён")
    logger.info("[swap] deal %d → %s uid=%d", deal_id, role, uid_new)

@router.callback_query(lambda c: c.data == POLL_BACK)
async def poll_back_handler(callback: types.CallbackQuery) -> None:
    """
    «Назад к списку»:
    • чистим private-ленточку пользователя;
    • обновляем сводный отчёт лидеру (если он открыт).
    """
    bot = callback.message.bot if callback.message else None
    uid = callback.from_user.id
    if bot:
        try:
            await delete_previous_private_messages(bot, uid, keep=[])
        except TypeError:
            with suppress(Exception):
                await delete_previous_private_messages(uid)  # type: ignore
    with suppress(Exception):
        await plc._sync_leader_report()
    with suppress(Exception):
        await callback.answer("↩️ Возврат к списку")
# История изменений:
# • 2025-08-15 — добавлен SWAP-обработчик с инвариантой «1 uid = 1 роль» + синхронизацией отчёта и detail-view;
#                refresh_deal_details выровнен с новой сигнатурой _render_detail; надёжный «Назад к списку».



# ███ [99] SELF-TESTS
# --------------------------------------------------------------------
async def _test_invariant() -> None:
    """Локальный тест переносов и инварианты «1 uid → 1 роль»."""
    did = 101
    state.current_poll_deals = [{"id": did, "game_name": "Время приключений", "package": "стандарт"}]
    state.distribution_cache = {str(did): {"lead1": "Иван И.|111", "admin": "Иван И.|111", "assistant1": "Пётр П.|222"}}

    cfg = _role_cfg("Время приключений")
    need_main = int(cfg.get("main_leaders", 1))
    need_assist = int(cfg.get("assistants", 2))

    await _ensure_single_role(state.distribution_cache[str(did)], need_main, need_assist)
    dist = state.distribution_cache[str(did)]
    all_uids = [
        _tag_uid(dist.get(k))
        for k in (["lead1", "lead2"] + ["assistant1", "assistant2"] + ["admin", "trainee"])
    ]
    assert all_uids.count(111) <= 1, "Дубли не устранены"

    # имитация swap → перенос 111 в assist
    fake_cb = types.CallbackQuery(
        id="x",
        from_user=types.User(id=999, is_bot=False, first_name=""),
        chat_instance="",
        message=types.Message(message_id=0, date=datetime.now(), chat=types.Chat(id=999, type="private")),
        data=f"swap_{did}_assist_111",
    )
    await poll_swap_handler(fake_cb)  # type: ignore

    dist = state.distribution_cache[str(did)]
    all_uids_after = [
        _tag_uid(dist.get(k))
        for k in (["lead1", "lead2"] + ["assistant1", "assistant2"] + ["admin", "trainee"])
    ]
    assert all_uids_after.count(111) == 1, "Инварианта нарушена после swap"
    print("handlers.poll_details — invariant tests passed")


async def _test_fmt_status() -> None:
    """Локальный тест форматирования и кэша статуса."""
    print(await _fmt(1, "main"))
    print(await _status_cached(1, "Цветочная башня"))

async def _test():
    await _test_invariant()
    await _test_fmt_status()

if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(_test())

# История изменений:
# • 2025-08-13 — Полная синхронизация с утверждённой логикой циклов; исправлена дублирующая сигнатура refresh;
#                 жёсткая инварианта ролей; стабильный пылесос; корректная работа «Назад» с обоими API отчёта.
