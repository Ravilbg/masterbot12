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

from __future__ import annotations

from datetime import datetime
import logging
import re
from contextlib import asynccontextmanager
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

from aiogram import Bot, Router, types
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    User
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config import settings
from core.db import get_user_info
from core.state import state
from core.utils import truncate
from services.gsheets import get_user_status_from_svetofor

logger = logging.getLogger(__name__)
router = Router()

POLL_BACK = "poll_back_to_games_list"
_ADMIN_PKGS = {"стандарт", "стандарт+", "премиум", "vip", "вип", "биглион"}
_OK_STATUSES = {"green", "yellow"}

# Локальный кэш для статусов Светофора
_local_status_cache: Dict[str, Tuple[str, float]] = {}  # key -> (status, timestamp)
STATUS_CACHE_TTL = 60 * 60 * 4  # 4 часа

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

# ════════════════════════════════════════════════════════════════════
# 1. Вспомогательные функции
# ════════════════════════════════════════════════════════════════════
def _tag_uid(tag: str) -> Optional[int]:
    try:
        return int(tag.rsplit("|", 1)[-1])
    except Exception:
        return None

def _role_cfg(game_name: str) -> Dict[str, int]:
    """Возвращает конфиг ролей для игры с tolerant-поиском."""
    _re = re.compile(r"[^\w\d]+", re.UNICODE)
    def _clean(s: str) -> str:
        return _re.sub(" ", s).lower().strip()
    norm = _clean(game_name)
    best_ratio = 0.0
    best_cfg = None
    for key, cfg in settings.GAME_ROLE_MAPPING.items():
        k_norm = _clean(key)
        if norm == k_norm or norm in k_norm or k_norm in norm:
            return cfg
        ratio = SequenceMatcher(None, norm, k_norm).ratio()
        if ratio > best_ratio:
            best_ratio, best_cfg = ratio, cfg
    if best_ratio > 0.80 and best_cfg:
        return best_cfg
    return {"main_leaders": 1, "assistants": 0}

async def _status_cached(uid: int, game: str) -> str:
    """Локальный кэш статуса из Светофора."""
    import time
    key = f"sv:{uid}:{game}".lower()
    now = time.time()
    if key in _local_status_cache:
        status, ts = _local_status_cache[key]
        if now - ts < STATUS_CACHE_TTL:
            return status
    try:
        status = await get_user_status_from_svetofor(uid, game)
        _local_status_cache[key] = (status, now)
        return status
    except Exception as exc:
        logger.warning("[details] Svetofor lookup failed %d/%s: %s", uid, game, exc)
        return ""

def _build_games_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for d in state.current_poll_deals:
        if d["id"] in state.deal_force_closed:
            continue
        kb.button(
            text=f"{d['game_name']} · {d['event_datetime']:%d.%m.%Y} · {d.get('event_time','—')}",
            callback_data=f"show_deal_{d['id']}",
        )
    kb.adjust(1)
    return kb.as_markup()

def _is_leader(uid: int) -> bool:
    return uid == state.current_poll_leader

async def _purge_msgs(uid: int, coll: Dict) -> None:
    """Удаляет все сообщения пользователя из коллекции."""
    for key, lst in list(coll.items()):
        if isinstance(key, tuple) and key[0] != uid:
            continue
        if key == uid or (isinstance(key, tuple) and key[0] == uid):
            for m in lst:
                try:
                    await m.delete()
                except Exception:
                    state.messages_to_delete.setdefault(uid, []).append(m.message_id)
            coll.pop(key, None)
# ███ [2] Detail-view: основной callback-handler
# --------------------------------------------------------------------
@router.callback_query(lambda c: c.data.startswith("show_deal_"))
async def show_deal_callback_handler(callback: types.CallbackQuery) -> None:
    """
    Детализация распределения по конкретной игре.
    Делает автоподбор в пустые слоты, сохраняет в distribution_cache,
    рендерит блоки с ролями, стажёрами и кнопками управления.
    """
    uid = callback.from_user.id
    deal_id = int(callback.data.rsplit("_", 1)[-1])
    bot = Bot.get_current()

    if state.force_closed:
        await bot.send_message(uid, "⚠️ Цикл распределения завершён.")
        return

    deal = next((d for d in state.current_poll_deals if d["id"] == deal_id), None)
    if not deal:
        await bot.send_message(uid, "⚠️ Игра не найдена.")
        return

    # Пылесос — удаляем старые сообщения
    await _purge_msgs(uid, state.last_user_messages)
    await _purge_msgs(uid, state.detail_blocks)
    msgs: list[types.Message] = []

    g_name = deal["game_name"]
    date_s = deal["event_datetime"].strftime("%d.%m.%Y")
    time_s = deal.get("event_time", "—")
    players = truncate(deal.get("players") or "—", 40)
    pkg_raw = (deal.get("package") or "—").strip().lower()
    pkg_icon = {
        "компакт": "🎒", "стандарт": "📦", "стандарт+": "📦➕",
        "премиум": "💎", "vip": "👑", "вип": "👑",
    }.get(pkg_raw, "🎁")

    header = (
        f"🎮 *{g_name}*\n"
        f"📅 {date_s} · 🕒 {time_s}\n"
        f"📦 *Пакет:* {pkg_icon} {pkg_raw.capitalize()}\n"
        f"👥 *Игроки:* {players}"
    )
    msgs.append(await bot.send_message(uid, header, parse_mode="Markdown"))

    cfg = _role_cfg(g_name)
    need = {
        "main": cfg["main_leaders"],
        "assist": cfg["assistants"],
        "admin": int(pkg_raw in _ADMIN_PKGS),
    }

    # Кэш распределения для этого мероприятия
    dist: dict = state.distribution_cache.setdefault(str(deal_id), {})

    respondents: dict[int, dict] = {}
    for pdata in state.responses.values():
        for u in pdata["deals"].get(deal_id, []):
            respondents[u["user_id"]] = u
        for adm in pdata.get("admin_available", []):
            respondents[adm["user_id"]] = {**respondents.get(adm["user_id"], {}), **adm}

    chosen_global: set[int] = set()

    async def _fits(user: dict, role: str) -> bool:
        if role == "admin":
            return user.get("is_admin_eligible", False)
        st = await _status_cached(user["user_id"], g_name)
        return st == "green" if role == "main" else st in _OK_STATUSES

    async def _fmt(uid_: int, role_key: str) -> str:
        info = await get_user_info(uid_) or {}
        name = f"{info.get('first_name','')} {info.get('last_name_initial','')}".strip()
        suffix = ".Адм" if role_key == "admin" else (".1" if role_key == "main" else ".2")
        return f"{name}{suffix}|{uid_}"

    async def _render(role: str, title: str, icon: str) -> None:
        chosen: list[tuple[dict, str]] = []

        # 1) Уже назначенные
        if role == "admin":
            tag_uid = _tag_uid(dist.get("admin", ""))
            if tag_uid and tag_uid in respondents:
                chosen.append((respondents[tag_uid], "🛡️"))
                chosen_global.add(tag_uid)
        else:
            prefix = "lead" if role == "main" else "assistant"
            for i in range(1, need[role] + 1):
                slot = f"{prefix}{i}"
                tag_uid = _tag_uid(dist.get(slot, ""))
                if tag_uid and tag_uid in respondents:
                    st = await _status_cached(tag_uid, g_name)
                    chosen.append((respondents[tag_uid], "🟢" if st == "green" else "🟡"))
                    chosen_global.add(tag_uid)

        # 2) Автоподбор
        for u in respondents.values():
            if len(chosen) >= need[role] or u["user_id"] in chosen_global:
                continue
            if await _fits(u, role):
                st = await _status_cached(u["user_id"], g_name)
                mark = "🛡️" if role == "admin" else ("🟢" if st == "green" else "🟡")
                chosen.append((u, mark))
                chosen_global.add(u["user_id"])
                if role == "admin":
                    dist["admin"] = await _fmt(u["user_id"], "admin")
                else:
                    prefix = "lead" if role == "main" else "assistant"
                    for i in range(1, need[role] + 1):
                        slot = f"{prefix}{i}"
                        if not dist.get(slot):
                            dist[slot] = await _fmt(u["user_id"], role)
                            break

        # 3) Синхронизация кэша
        if role == "admin" and not dist.get("admin") and chosen:
            dist["admin"] = await _fmt(chosen[0][0]["user_id"], "admin")
        elif role != "admin":
            prefix = "lead" if role == "main" else "assistant"
            for idx, (u, _) in enumerate(chosen, 1):
                dist[f"{prefix}{idx}"] = await _fmt(u["user_id"], role)

        # 4) Вывод блока
        ready = len(chosen) >= need[role]
        block = [
            f"───── {icon} *{title.upper()}* ─────",
            f"{'✅' if ready else '❌'} {len(chosen)}/{need[role]}",
            *[f"– {u['first_name']} {u.get('last_name_initial','')} {m}" for u, m in chosen],
        ]
        msgs.append(await bot.send_message(uid, "\n".join(block), parse_mode="Markdown"))

        # 5) Альтернативы
        alts = [
            u for u in respondents.values()
            if u["user_id"] not in chosen_global and await _fits(u, role)
        ]
        if alts:
            kb_alt = InlineKeyboardBuilder()
            for u in alts:
                st = "" if role == "admin" else await _status_cached(u["user_id"], g_name)
                mark = "🛡️" if role == "admin" else ("🟢" if st == "green" else "🟡")
                kb_alt.button(
                    text=f"{u['first_name']} {u.get('last_name_initial','')} {mark}",
                    callback_data=f"swap_{deal_id}_{role}_{u['user_id']}",
                )
            kb_alt.adjust(1)
            msgs.append(await bot.send_message(uid, "🔁 Альтернатива:", reply_markup=kb_alt.as_markup()))

    await _render("main", "Ведущие", "🧭")
    await _render("assist", "Помощники", "🛟")
    await _render("admin", "Админ", "🛡️")

    # Стажёры
    red_users = [
        u for u in respondents.values()
        if u["user_id"] not in chosen_global
        and await _status_cached(u["user_id"], g_name) == "red"
    ]
    if red_users:
        block = ["───── 👷 *СТАЖЁРЫ* ─────"] + [
            f"– {u['first_name']} {u.get('last_name_initial','')} 🔴" for u in red_users
        ]
        msgs.append(await bot.send_message(uid, "\n".join(block), parse_mode="Markdown"))

    # Кнопки управления
    if _is_leader(uid) and deal_id not in state.deal_force_closed:
        kb_mgr = InlineKeyboardBuilder()
        kb_mgr.button(text="✅ Утвердить игру", callback_data=f"poll_approve_{deal_id}")
        kb_mgr.button(text="⏹️ Стоп набор", callback_data=f"poll_stop_{deal_id}")
        kb_mgr.adjust(1)
        msgs.append(await bot.send_message(uid, "🛠 Управление:", reply_markup=kb_mgr.as_markup()))

    # Кнопка Назад
    kb_back = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к списку", callback_data=POLL_BACK)]]
    )
    msgs.append(await bot.send_message(uid, "\u2060", reply_markup=kb_back))

    state.detail_blocks[(uid, deal_id)] = msgs
# ███ [3] Обновление деталей и обработчики управления
# --------------------------------------------------------------------

async def refresh_deal_details(uid: int, deal_id: int) -> None:
    """
    Перерисовывает карточку игры у пользователя, сохраняя чистоту экрана.
    """
    await _purge_msgs(uid, state.detail_blocks)
    fake_cb = types.CallbackQuery(
        id="0",
        from_user=types.User(id=uid, is_bot=False, first_name=""),
        chat_instance="",
        message=types.Message(
            message_id=0,
            date=datetime.now(),
            chat=types.Chat(id=uid, type="private")
        ),
        data=f"show_deal_{deal_id}"
    )
    await show_deal_callback_handler(fake_cb)

# ─────────────────────────────────────────────────────────────────────
@router.callback_query(lambda c: c.data.startswith("poll_approve_"))
async def poll_approve_game_handler(callback: types.CallbackQuery) -> None:
    """
    Утверждение состава: фиксирует текущее распределение, уведомляет чат лидеров,
    переводит игру в ожидание подтверждений в личном кабинете у участников.
    """
    deal_id = int(callback.data.rsplit("_", 1)[-1])

    if deal_id in state.deal_force_closed:
        await callback.answer("Набор уже остановлен.", show_alert=True)
        return

    dist = state.distribution_cache.get(str(deal_id)) or {}
    if not dist:
        await callback.answer("⚠️ Нет распределения.", show_alert=True)
        return

    # Фиксируем распределение
    state.locked_distribution[deal_id] = dist
    state.pending_confirmations[deal_id] = {"distribution": dist, "confirmed": set()}

    # Уведомляем чат
    title = state.deal_titles.get(deal_id, f"Сделка #{deal_id}")
    chat_id = getattr(settings, "LEADERS_CHAT_ID", getattr(settings, "admin_chat_id", None))
    try:
        await callback.message.bot.send_message(
            chat_id,
            f"🚦 Состав команды на игру «{title}» утверждён.\nПодтвердите своё участие в личном кабинете."
        )
    except Exception as e:
        logger.error("[approve] notify leaders chat failed: %s", e)

    # Обновляем "Мои игры" у всех участников
    uids = {_tag_uid(v) for v in dist.values() if _tag_uid(v)}
    for uid in uids:
        try:
            await redraw_my_games(uid)
        except Exception as e:
            logger.warning("[approve] redraw_my_games failed for %d: %s", uid, e)

    await callback.answer("Игра утверждена ✅")
    logger.info("[approve] deal %d locked with %d users", deal_id, len(uids))

# ─────────────────────────────────────────────────────────────────────
@router.callback_query(lambda c: c.data.startswith("poll_stop_"))
async def poll_stop_game_handler(callback: types.CallbackQuery) -> None:
    """
    Принудительная остановка набора на игру.
    """
    deal_id = int(callback.data.rsplit("_", 1)[-1])
    state.deal_force_closed.add(deal_id)
    await callback.answer("Набор остановлен.")
    logger.info("[stop] deal %d stopped by %d", deal_id, callback.from_user.id)

# ─────────────────────────────────────────────────────────────────────
@router.callback_query(lambda c: c.data.startswith("swap_"))
async def poll_swap_handler(callback: types.CallbackQuery) -> None:
    """
    Замена ведущего/помощника/админа на другого кандидата.
    """
    try:
        _, deal_id, role, uid_new = callback.data.split("_")
        deal_id, uid_new = int(deal_id), int(uid_new)
    except Exception:
        await callback.answer("Ошибка формата swap.", show_alert=True)
        return

    dist = state.distribution_cache.get(str(deal_id))
    if not dist:
        await callback.answer("Нет текущего распределения.", show_alert=True)
        return

    if role == "admin":
        dist["admin"] = await _fmt(uid_new, "admin")
    else:
        prefix = "lead" if role == "main" else "assistant"
        for k in list(dist.keys()):
            if k.startswith(prefix):
                dist[k] = await _fmt(uid_new, role)
                break

    await refresh_deal_details(callback.from_user.id, deal_id)
    await callback.answer("Состав обновлён.")

# ─────────────────────────────────────────────────────────────────────
@router.callback_query(lambda c: c.data == POLL_BACK)
async def poll_back_handler(callback: types.CallbackQuery) -> None:
    """
    Возврат к списку игр текущего опроса.
    """
    from handlers.polls_lifecycle import _sync_leader_report
    try:
        await _sync_leader_report()
    except Exception as e:
        logger.warning("[back] _sync_leader_report failed: %s", e)
    await callback.answer()

# ███ [4] Утилиты, кэш и пылесос
# --------------------------------------------------------------------

async def _fmt(uid_: int, role_key: str) -> str:
    """
    Форматирует имя пользователя в тег для кэша распределения.
    Пример: 'Иван И.1|12345'
    """
    info = await get_user_info(uid_) or {}
    name = f"{info.get('first_name','')} {info.get('last_name_initial','')}".strip()
    suffix = ".Адм" if role_key == "admin" else ".1" if role_key == "main" else ".2"
    return f"{name}{suffix}|{uid_}"


def _tag_uid(tag: str) -> Optional[int]:
    """
    Извлекает user_id из тега «Имя.1|123».
    """
    if "|" not in tag:
        return None
    try:
        return int(tag.rsplit("|", 1)[-1])
    except Exception:
        return None


async def _status_cached(uid: int, game: str) -> str:
    """
    Локальный кэш статуса из Светофора.
    Кэш хранится в _local_status_cache на уровне модуля.
    """
    import time
    key = f"sv:{uid}:{game}".lower()
    now = time.time()
    if key in _local_status_cache:
        status, ts = _local_status_cache[key]
        if now - ts < STATUS_CACHE_TTL:
            return status
    try:
        status = await get_user_status_from_svetofor(uid, game)
    except Exception as e:
        logger.warning("[status_cached] fail for %d/%s: %s", uid, game, e)
        status = ""
    _local_status_cache[key] = (status, now)
    return status


async def _purge_msgs(uid: int, coll: Dict) -> None:
    """
    Удаляет все сообщения пользователя из словаря coll.
    Поддерживает ключи как uid, так и (uid, deal_id).
    """
    for key in list(coll.keys()):
        # Проверка, что ключ относится к пользователю
        if key == uid or (isinstance(key, tuple) and key[0] == uid):
            msgs = coll.get(key, [])
            for msg in msgs:
                try:
                    await msg.delete()
                except Exception:
                    state.messages_to_delete.setdefault(uid, []).append(
                        getattr(msg, "message_id", None)
                    )
            coll.pop(key, None)


# ─────────────────────────────────────────────────────────────────────
async def _fmt(uid_: int, role_key: str) -> str:
    """
    Форматирует имя пользователя в тег для кэша распределения.
    """
    info = await get_user_info(uid_) or {}
    name = f"{info.get('first_name','')} {info.get('last_name_initial','')}".strip()
    suffix = ".Адм" if role_key == "admin" else ".1" if role_key == "main" else ".2"
    return f"{name}{suffix}|{uid_}"

# ─────────────────────────────────────────────────────────────────────
async def _test() -> None:
    """
    Локальный тест функций кэша и форматирования.
    """
    print(await _fmt(1, "main"))
    print(await _status_cached(1, "Цветочная башня"))

if __name__ == "__main__":
    import asyncio
    asyncio.run(_test())
