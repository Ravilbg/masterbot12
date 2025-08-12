# handlers/poll_details.py — detail-view игр + manual-switches
# ─────────────────────────────────────────────────────────────────────────────
"""
Реактивные карточки игр (detail-view) для цикла распределения.

Версия 13.0 · 2025-08-09
──────────────────────────────────────────────────────────────────────────────
• Полностью локальный кэш распределения (без Redis).
• Пылесос удаляет ВСЕ старые сообщения (detail + меню) для пользователя.
• Формат и логика распределения полностью сохранены.
• Автоподбор ведущих/помощников по статусам Светофора.
• Возможность ручной замены.
• Поддержка "Утвердить" и "Стоп набор".
"""

# ███ [0] IMPORTS & SETUP
# --------------------------------------------------------------------
from __future__ import annotations

import logging
import re
from datetime import datetime
from contextlib import asynccontextmanager
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

from aiogram import Bot, Router, types
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config import settings
from core.db import get_user_info
from core.state import state
from core.utils import truncate, delete_previous_private_messages
from services.gsheets import get_user_status_from_svetofor

logger = logging.getLogger(__name__)
router = Router()

POLL_BACK = "poll_back_to_games_list"
_ADMIN_PKGS = {"стандарт", "стандарт+", "премиум", "vip", "вип", "биглион"}
_OK_STATUSES = {"green", "yellow"}  # для помощников
_STATUS_RE = re.compile(r"[^\w\d]+", re.UNICODE)

# Локальный кэш для статусов «Светофора»: key -> (status, ts)
_local_status_cache: Dict[str, Tuple[str, float]] = {}
STATUS_CACHE_TTL = 60 * 60 * 4  # 4 часа


@asynccontextmanager
async def user_lock(uid: int):
    """Персональная блокировка, чтобы не было гонок при рендере деталей."""
    lock = state.lock_for(uid)
    await lock.acquire()
    try:
        yield
    finally:
        lock.release()

# История изменений: пересобран импорт, единая точка пылесоса, локальный статус-кэш (2025-08-11)

# ════════════════════════════════════════════════════════════════════
# 0. Per-user async-lock
# ════════════════════════════════════════════════════════════════════
@asynccontextmanager
async def user_lock(uid: int):
    lock = state.lock_for(uid)
    await lock.acquire()
    try:
        yield
    finally:
        lock.release()

# ███ [1] HELPERS (normalize, role cfg, svetofor cache, tags/ids, invariants)
# --------------------------------------------------------------------
"""
Хелперы для detail-view:
• нормализация имени игры и толерантный поиск конфигурации ролей;
• локальный кэш "Светофора";
• форматирование тегов «Имя.Суффикс|uid» и извлечение uid;
• проверка прав лидера;
• инварианта «1 пользователь → 1 роль» внутри одной игры.
"""

from difflib import SequenceMatcher
from typing import Dict, List, Optional, Set, Tuple
import re
import time

_STATUS_RE = re.compile(r"[^\w\d]+", re.UNICODE)
_OK_STATUSES = {"green", "yellow"}  # для помощников
_ADMIN_PKGS = {"стандарт", "стандарт+", "премиум", "vip", "вип", "биглион"}
_local_status_cache: Dict[str, Tuple[str, float]] = {}
STATUS_CACHE_TTL = 60 * 60 * 4  # 4 часа


def _clean(s: str) -> str:
    return _STATUS_RE.sub(" ", (s or "")).lower().strip()


def _role_cfg(game_name: str) -> Dict[str, int]:
    """
    Возвращает требуемые количества ролей для игры (толерантный поиск).
    Формат: {"main_leaders": int, "assistants": int}
    """
    norm = _clean(game_name)
    best_ratio = 0.0
    best_cfg: Optional[Dict[str, int]] = None
    for key, cfg in settings.GAME_ROLE_MAPPING.items():
        k_norm = _clean(key)
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


async def _fmt(uid_: int, role_key: str) -> str:
    """
    Формирует тег для кэша распределения/подтверждений:
      main    -> «Имя Ф.1|uid»
      assist  -> «Имя Ф.2|uid»
      admin   -> «Имя Ф.Адм|uid»
      trainee -> «Имя Ф.Стаж|uid»
    """
    info = await get_user_info(uid_) or {}
    name = f"{info.get('first_name','')} {info.get('last_name_initial','')}".strip()
    suffix = {
        "main": ".1",
        "assist": ".2",
        "admin": ".Адм",
        "trainee": ".Стаж",
    }.get(role_key, "")
    return f"{name}{suffix}|{uid_}".strip()


def _tag_uid(tag: Optional[str]) -> Optional[int]:
    if not tag or "|" not in str(tag):
        return None
    try:
        return int(str(tag).rsplit("|", 1)[-1])
    except Exception:
        return None


def _is_leader(uid: int) -> bool:
    return uid == state.current_poll_leader


def _role_slots(need_main: int, need_assist: int) -> Tuple[List[str], List[str]]:
    """Возвращает списки ключей слотов для main/assist в distribution_cache."""
    leads = [f"lead{i}" for i in range(1, max(1, need_main) + 1)]
    assis = [f"assistant{i}" for i in range(1, max(0, need_assist) + 1)]
    return leads, assis


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

# История изменений:
# • Добавлен _role_slots и _ensure_single_role (строгая инварианта «1 uid → 1 роль»), 2025-08-12
# • Перенесены _fmt/_tag_uid/_status_cached в единый блок, 2025-08-12


# ███ [2] DETAIL-VIEW (автоподбор + стажёр + инварианта) — show_deal_callback_handler
# --------------------------------------------------------------------
@router.callback_query(lambda c: c.data.startswith("show_deal_"))
async def show_deal_callback_handler(callback: types.CallbackQuery) -> None:
    """
    Показывает карточку игры:
      • автоподбор в пустые слоты с учётом «Светофора»;
      • жёсткая инварианта «1 пользователь → 1 роль» (main/assist/admin/trainee);
      • стажёр: выбирается КРАСНЫЙ, не занятый в других ролях;
      • запись результата в state.distribution_cache[deal_id] — источник для «Утвердить», «Мои игры» и тегов CRM;
      • перерисовка кнопок управления («Утвердить» / «Утверждено», «Стоп набор»), без автопереходов.
      • «Альтернативы» могут содержать уже занятых в других ролях — при выборе произойдёт ПЕРЕНОС (см. [3.3]).
    """
    uid = callback.from_user.id
    deal_id = int(callback.data.rsplit("_", 1)[-1])
    bot = Bot.get_current()

    # Пылесос: в деталях оставляем только текущий блок
    await delete_previous_private_messages(uid)

    deal = next((d for d in state.current_poll_deals or [] if int(d.get("id", 0)) == deal_id), None)
    if not deal:
        await bot.send_message(uid, "⚠️ Игра не найдена или уже закрыта.")
        return

    g_name = str(deal.get("game_name") or "Игра")
    date_s = deal.get("event_datetime")
    date_s = date_s.strftime("%d.%m.%Y") if hasattr(date_s, "strftime") else str(date_s or "—")
    time_s = str(deal.get("event_time") or "—")
    pkg_raw = str(deal.get("package") or "—").strip().lower()
    pkg_icon = {"компакт": "🎒", "стандарт": "📦", "стандарт+": "📦➕", "премиум": "💎", "vip": "👑", "вип": "👑"}.get(pkg_raw, "🎁")
    players = truncate(str(deal.get("players") or "—"), 40)

    header = (
        f"🎮 *{g_name}*\n"
        f"📅 {date_s} · 🕒 {time_s}\n"
        f"📦 *Пакет:* {pkg_icon} {pkg_raw.capitalize()}\n"
        f"👥 *Игроки:* {players}"
    )
    msgs: List[types.Message] = [await bot.send_message(uid, header, parse_mode="Markdown")]

    # Требуемые роли
    cfg = _role_cfg(g_name)
    need_main = int(cfg.get("main_leaders", 1))
    need_assist = int(cfg.get("assistants", 0))
    need_admin = 1 if pkg_raw in _ADMIN_PKGS else 0

    # Кандидаты по откликам
    respondents: Dict[int, Dict] = {}
    for pdata in (state.responses or {}).values():
        for u in (pdata.get("deals", {}).get(deal_id, []) or []):
            respondents[u["user_id"]] = {**respondents.get(u["user_id"], {}), **u}
        for adm in (pdata.get("admin_available", []) or []):
            respondents[adm["user_id"]] = {**respondents.get(adm["user_id"], {}), **adm}

    # Текущее распределение и первичная чистка дублей
    dist: Dict[str, str] = state.distribution_cache.setdefault(str(deal_id), {})
    await _ensure_single_role(dist, need_main, need_assist)

    chosen_global: Set[int] = set()  # пользователи, занятые В ЛЮБОЙ роли (кроме стажёра — он тоже учитывается)

    async def _fits(user: Dict, role: str) -> bool:
        if role == "admin":
            return bool(user.get("is_admin_eligible"))
        st = await _status_cached(user["user_id"], g_name)
        if role == "main":
            return st == "green"
        if role == "assist":
            return st in _OK_STATUSES
        return False

    async def _render_role(role: str, title: str, icon: str, need: int) -> None:
        nonlocal dist, chosen_global, msgs
        chosen: List[Tuple[Dict, str]] = []
        prefix = "lead" if role == "main" else ("assistant" if role == "assist" else "admin")

        # Уже назначенные (из кэша) — в рамках лимита и только валидные
        if role == "admin":
            tag_uid = _tag_uid(dist.get("admin"))
            if need == 1 and tag_uid and tag_uid in respondents:
                chosen.append((respondents[tag_uid], "🛡️"))
                chosen_global.add(tag_uid)
            else:
                dist.pop("admin", None)
        else:
            for i in range(1, need + 1):
                slot = f"{prefix}{i}"
                tag_uid = _tag_uid(dist.get(slot))
                if tag_uid and tag_uid in respondents:
                    st = await _status_cached(tag_uid, g_name)
                    chosen.append((respondents[tag_uid], "🟢" if st == "green" else "🟡"))
                    chosen_global.add(tag_uid)
                else:
                    dist.pop(slot, None)

        # Автодобор до лимита — НЕ ставим одного и того же в разные роли
        for u in respondents.values():
            if len(chosen) >= need:
                break
            if u["user_id"] in chosen_global:
                continue
            if await _fits(u, role):
                st = await _status_cached(u["user_id"], g_name)
                mark = "🛡️" if role == "admin" else ("🟢" if st == "green" else "🟡")
                chosen.append((u, mark))
                chosen_global.add(u["user_id"])
                # запись в кэш
                if role == "admin":
                    dist["admin"] = await _fmt(u["user_id"], "admin")
                else:
                    for i in range(1, need + 1):
                        slot = f"{prefix}{i}"
                        if not dist.get(slot):
                            dist[slot] = await _fmt(u["user_id"], role)
                            break

        # Синхронизация кэша по факту
        if role == "admin" and need == 1 and chosen and not dist.get("admin"):
            dist["admin"] = await _fmt(chosen[0][0]["user_id"], "admin")
        elif role != "admin":
            idx = 1
            for u, _m in chosen:
                dist[f"{prefix}{idx}"] = await _fmt(u["user_id"], role)
                idx += 1
            for i in range(idx, need + 1):
                dist.pop(f"{prefix}{i}", None)

        # Вывод блока роли
        ready = len(chosen) >= need
        lines = [
            f"───── {icon} *{title.upper()}* ─────",
            f"{'✅' if ready else '❌'} {len(chosen)}/{need}",
            *[f"– {u['first_name']} {u.get('last_name_initial','')} {mark}" for u, mark in chosen],
        ]
        msgs.append(await bot.send_message(uid, "\n".join(lines), parse_mode="Markdown"))

        # Альтернативы: показываем всех подходящих, кто НЕ в этой роли (может быть в другой — для переноса)
        chosen_role_uids = {u["user_id"] for u, _ in chosen}
        alts = [
            u for u in respondents.values()
            if u["user_id"] not in chosen_role_uids and await _fits(u, role)
        ]
        if alts:
            kb = InlineKeyboardBuilder()
            for u in alts:
                st = await _status_cached(u["user_id"], g_name) if role != "admin" else ""
                mark = "🛡️" if role == "admin" else ("🟢" if st == "green" else "🟡")
                kb.button(
                    text=f"{u['first_name']} {u.get('last_name_initial','')} {mark}",
                    callback_data=f"swap_{deal_id}_{role}_{u['user_id']}",
                )
            kb.adjust(1)
            msgs.append(await bot.send_message(uid, "🔁 Альтернатива:", reply_markup=kb.as_markup()))

    # Роли
    await _render_role("main", "Ведущие", "🧭", need_main)
    await _render_role("assist", "Помощники", "🛟", need_assist)

    # Админ по пакетам
    if need_admin:
        await _render_role("admin", "Админ", "🛡️", 1)
    else:
        dist.pop("admin", None)

    # Повторная гарантия инварианты после автодобора
    await _ensure_single_role(dist, need_main, need_assist)

    # Стажёр: выбираем КРАСНОГО, который НЕ занят в других ролях
    dist = state.distribution_cache[str(deal_id)]
    trainee_uid: Optional[int] = _tag_uid(dist.get("trainee"))
    red_pool = []
    for u in respondents.values():
        st = await _status_cached(u["user_id"], g_name)
        if st == "red" and u["user_id"] not in { _tag_uid(dist.get(k)) for k in (*[f"lead{i}" for i in range(1, need_main+1)], *[f"assistant{i}" for i in range(1, need_assist+1)], "admin") }:
            red_pool.append(u)

    if red_pool:
        if trainee_uid not in {u["user_id"] for u in red_pool}:
            trainee_uid = red_pool[0]["user_id"]
            dist["trainee"] = await _fmt(trainee_uid, "trainee")
        t_info = next((u for u in red_pool if u["user_id"] == trainee_uid), red_pool[0])
        block = [
            "───── 👷 *СТАЖЁР* ─────",
            f"– {t_info['first_name']} {t_info.get('last_name_initial','')} 🔴",
            "_Стажёр не влияет на индикатор набора._",
        ]
        msgs.append(await bot.send_message(uid, "\n".join(block), parse_mode="Markdown"))
    else:
        dist.pop("trainee", None)

    # Кнопки управления (без автопереходов)
    if _is_leader(uid) and deal_id not in getattr(state, "deal_force_closed", set()):
        kb_mgr = InlineKeyboardBuilder()
        if str(deal_id) in (state.locked_distribution or {}):
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
    state.detail_blocks[(uid, deal_id)] = msgs

# История изменений:
# • Жёсткая инварианта «1 uid → 1 роль» до/после автодобора; стажёр не конфликтует с ролями (2025-08-12)
# • Управление без автопереходов; «Утверждено» берётся из locked_distribution (2025-08-12)


# ███ [3.0] REFRESH — перерисовка деталей игры по deal_id
# --------------------------------------------------------------------
async def refresh_deal_details(uid: int, deal_id: int) -> None:
    """
    Перерисовывает карточку игры для пользователя *uid*.
    Под капотом вызывает show_deal_callback_handler с фейковым callback.
    """
    fake_cb = types.CallbackQuery(
        id="0",
        from_user=types.User(id=uid, is_bot=False, first_name=""),
        chat_instance="",
        message=types.Message(
            message_id=0,
            date=datetime.now(),
            chat=types.Chat(id=uid, type="private"),
        ),
        data=f"show_deal_{deal_id}",
    )
    await show_deal_callback_handler(fake_cb)

# История изменений: совместимость с существующими вызовами (2025-08-11)
# ███ [3.1] APPROVE — делегирование в handlers.polls_distribution
# --------------------------------------------------------------------
@router.callback_query(lambda c: c.data.startswith("poll_approve_"))
async def poll_approve_game_handler(callback: CallbackQuery) -> None:
    """
    Делегирует утверждение состава в единый обработчик распределения:
    handlers.polls_distribution.poll_approve_game_handler
    (там: фиксация locked_distribution, уведомления в чат, подготовка цикла подтверждений).
    """
    try:
        from handlers.polls_distribution import poll_approve_game_handler as _impl
    except Exception as e:
        await callback.answer("Ошибка модуля утверждения.", show_alert=True)
        logger.exception("poll_approve import failed: %s", e)
        return
    await _impl(callback)

# История изменений: устранён дублирующийся код утверждения (2025-08-11)
# ███ [3.2] STOP — принудительная остановка набора
# --------------------------------------------------------------------
@router.callback_query(lambda c: c.data.startswith("poll_stop_"))
async def poll_stop_game_handler(callback: CallbackQuery) -> None:
    try:
        deal_id = int(callback.data.rsplit("_", 1)[-1])
    except Exception:
        await callback.answer("Некорректный идентификатор игры.", show_alert=True)
        return
    state.deal_force_closed.add(deal_id)
    await callback.answer("Набор остановлен.")
    logger.info("[details.stop] deal_id=%s by uid=%s", deal_id, callback.from_user.id)

# История изменений: без изменений логики (2025-08-11)
# ███ [3.3] SWAP — перенос кандидата между ролями с инвариантой
# --------------------------------------------------------------------
@router.callback_query(lambda c: c.data.startswith("swap_"))
async def poll_swap_handler(callback: CallbackQuery) -> None:
    """
    swap_{deal_id}_{role}_{new_uid}
    role ∈ {main, assist, admin}

    Правила:
    • Выбор из «Альтернативы» переносит пользователя: снимаем из ВСЕХ ролей этой игры и ставим в целевую.
    • Инварианта «1 пользователь → 1 роль» соблюдается жёстко (включая trainee/admin).
    """
    try:
        _, deal_id_s, role, uid_new_s = callback.data.split("_")
        deal_id = int(deal_id_s)
        uid_new = int(uid_new_s)
    except Exception:
        await callback.answer("Ошибка формата swap.", show_alert=True)
        return

    deal = next((d for d in (state.current_poll_deals or []) if int(d.get("id", 0)) == deal_id), None)
    if not deal:
        await callback.answer("Игра не найдена.", show_alert=True)
        return

    g_name = str(deal.get("game_name") or "Игра")
    cfg = _role_cfg(g_name)
    need_main = int(cfg.get("main_leaders", 1))
    need_assist = int(cfg.get("assistants", 0))

    dist: Dict[str, str] | None = (state.distribution_cache or {}).get(str(deal_id))
    if not dist:
        await callback.answer("Нет текущего распределения.", show_alert=True)
        return

    # 1) Удаляем пользователя из всех ролей (main/assist/admin/trainee)
    leads = [f"lead{i}" for i in range(1, max(1, need_main) + 1)]
    assis = [f"assistant{i}" for i in range(1, max(0, need_assist) + 1)]
    for k in [*leads, *assis, "admin", "trainee"]:
        if _tag_uid(dist.get(k)) == uid_new:
            dist.pop(k, None)

    # 2) Ставим в целевую роль
    if role == "admin":
        dist["admin"] = await _fmt(uid_new, "admin")
    elif role == "main":
        placed = False
        for k in leads:
            if not dist.get(k):
                dist[k] = await _fmt(uid_new, "main")
                placed = True
                break
        if not placed and leads:
            dist[leads[-1]] = await _fmt(uid_new, "main")
    elif role == "assist":
        placed = False
        for k in assis:
            if not dist.get(k):
                dist[k] = await _fmt(uid_new, "assist")
                placed = True
                break
        if not placed and assis:
            dist[assis[-1]] = await _fmt(uid_new, "assist")
    else:
        await callback.answer("Неизвестная роль.", show_alert=True)
        return

    # 3) Гарантируем инварианту (включая мусорные ключи)
    await _ensure_single_role(dist, need_main, need_assist)

    # 4) Перерисуем детали без автопереходов
    try:
        await refresh_deal_details(callback.from_user.id, deal_id)
    except Exception as e:
        logger.warning("[swap] refresh failed for deal=%s: %s", deal_id, e)

    await callback.answer("Состав обновлён.")

# История изменений:
# • Снятие со всех ролей (включая trainee/admin) перед установкой, строгая инварианта (2025-08-12)

# ███ [3.4] BACK — назад к списку игр текущего опроса
# --------------------------------------------------------------------
@router.callback_query(lambda c: c.data == POLL_BACK)
async def poll_back_handler(callback: CallbackQuery) -> None:
    from handlers.polls_lifecycle import _sync_leader_report
    try:
        await _sync_leader_report()
    except Exception as e:
        logger.warning("[details.back] _sync_leader_report failed: %s", e)
    await callback.answer()

# История изменений: без изменений логики (2025-08-11)

# ███ [4] УТИЛИТЫ (пылесос-сумматор и self-test)
# --------------------------------------------------------------------
"""
Служебные функции и локальные тесты.
Внимание: функции форматирования и кэша статусов определены в блоке [1].
"""

async def _purge_msgs(uid: int, coll) -> None:
    """
    Удаляет все сообщения пользователя из коллекции coll.

    Поддерживаем оба исторических формата:
      • dict: {uid|(uid, deal_id) -> List[aiogram.types.Message]}
      • set:  {(uid, deal_id), ...}  — старый формат, где сообщений нет,
               просто очищаем элементы пользователя.
    Любые ошибки удаления сообщений не критичны — message_id кладём в
    state.messages_to_delete[uid] для отложенной уборки.
    """
    if coll is None:
        return

    # Новый/актуальный формат — словарь
    if isinstance(coll, dict):
        for key in list(coll.keys()):
            is_user_key = (key == uid) or (isinstance(key, tuple) and key and key[0] == uid)
            if not is_user_key:
                continue

            msgs = coll.get(key) or []
            for msg in list(msgs):
                try:
                    await msg.delete()
                except Exception:
                    mid = getattr(msg, "message_id", None)
                    if mid:
                        state.messages_to_delete.setdefault(uid, []).append(mid)
            coll.pop(key, None)
        return

    # Старый формат — множество (set) с ключами без сообщений
    if isinstance(coll, set):
        to_remove = []
        for item in list(coll):
            if item == uid or (isinstance(item, tuple) and item and item[0] == uid):
                to_remove.append(item)
        for item in to_remove:
            try:
                coll.discard(item)
            except Exception:
                pass
        return

    logger.debug("[purge] skip unknown collection type: %s", type(coll).__name__)


# ─────────────────────────────────────────────────────────────────────
async def _test() -> None:
    """
    Локальный тест переносов и инварианты «1 uid → 1 роль».
    """
    did = 101
    state.current_poll_deals = [{"id": did, "game_name": "Время приключений", "package": "стандарт"}]
    state.distribution_cache.clear()

    # Допустим, один и тот же человек случайно оказался в main и admin
    state.distribution_cache[str(did)] = {
        "lead1": "Иван И.|111",
        "admin": "Иван И.|111",
        "assistant1": "Пётр П.|222",
    }

    # Конфигурация ролей
    cfg = _role_cfg("Время приключений")
    need_main = int(cfg.get("main_leaders", 1))
    need_assist = int(cfg.get("assistants", 2))

    # Подчистка дублей
    await _ensure_single_role(state.distribution_cache[str(did)], need_main, need_assist)
    dist = state.distribution_cache[str(did)]
    # Проверка: максимум одно вхождение 111
    all_uids = [
        _tag_uid(dist.get(k))
        for k in (["lead1", "lead2"] + ["assistant1", "assistant2"] + ["admin", "trainee"])
        if isinstance(k, str)
    ]
    assert all_uids.count(111) <= 1, "Дубли не устранены"

    # Перенос 111 из текущей роли в assist
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
        if isinstance(k, str)
    ]
    assert all_uids_after.count(111) == 1, "Инварианта нарушена после swap"
    print("handlers.poll_details ✅ tests passed")

# История изменений:
# • Убран дублирующийся код форматирования/кэша (они в [1]); добавлен self-test переносов, 2025-08-12

# ─────────────────────────────────────────────────────────────────────
async def _test() -> None:
    """
    Локальный тест функций кэша и форматирования.
    """
    print(await _fmt(1, "main"))
    print(await _status_cached(1, "Цветочная башня"))
