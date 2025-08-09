# handlers/polls_distribution.py
# ─────────────────────────────────────────────────────────────────────────────
"""
Ручное управление распределением (этап лидера):
• Руководитель нажимает «✅ Утвердить игру» или «👌 Утвердить все готовые».
• Распределение фиксируется → `state.locked_distribution`.
• В чат ведущих уходит уведомление «Состав … утверждён…».
• Каждому назначенному UID дашборд «Мои игры» перерисовывается.
• Игры переводятся в режим ожидания подтверждений (state.pending_confirmations).

Версия v14.2-cycle · 2025-08-09
"""

from __future__ import annotations

import logging
from typing import Dict, List, Set

from aiogram import Bot, Router, types
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from core.config import settings
from core.state import state
from handlers.my_games import redraw_my_games
from handlers.poll_details import refresh_deal_details

import handlers.polls_lifecycle as plc  # noqa: E402

logger = logging.getLogger(__name__)
router = Router()

# алиасы
_is_deal_ready = plc._is_deal_ready


# ════════════════════════════════════════════════════════════════════
# [1] УТИЛИТЫ
# ════════════════════════════════════════════════════════════════════

def _leaders_chat_id() -> int:
    return (
        getattr(settings, "LEADERS_CHAT_ID", None)
        or getattr(settings, "leaders_chat_id", None)
        or getattr(settings, "ADMIN_CHAT_ID", None)
        or getattr(settings, "admin_chat_id", None)
    )

def _uids_from_roles(roles: Dict[str, List[int]]) -> Set[int]:
    return set(roles.get("main", []) + roles.get("assist", []) + roles.get("admin", []))

def _extract_current_team(deal_id: int) -> Dict[str, List[int]]:
    roles = getattr(state, "detail_roles", {})
    if isinstance(roles, dict) and deal_id in roles:
        return {
            "main": list(roles[deal_id].get("main", [])),
            "assist": list(roles[deal_id].get("assist", [])),
            "admin": list(roles[deal_id].get("admin", [])),
        }
    return {"main": [], "assist": [], "admin": []}

def _lock_distribution(deal_id: int, roles: Dict[str, List[int]]) -> Set[int]:
    state.locked_distribution[deal_id] = roles
    state.pending_confirmations[deal_id] = {
        "distribution": roles,
        "confirmed": set()
    }
    logger.debug("[polls_dist] deal %d locked, pending_confirmations=%s", deal_id, roles)
    return _uids_from_roles(roles)


# ════════════════════════════════════════════════════════════════════
# [2] INLINE-КЛАВИАТУРА (заглушка)
# ════════════════════════════════════════════════════════════════════

def distribution_actions_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[])


# ════════════════════════════════════════════════════════════════════
# [3] HANDLER approve_deal
# ════════════════════════════════════════════════════════════════════

@router.callback_query(lambda c: c.data.startswith("approve_deal_"))
async def poll_approve_game_handler(callback: CallbackQuery) -> None:
    try:
        deal_id = int(callback.data.rsplit("_", 1)[-1])
    except Exception:
        await callback.answer("Ошибка: неизвестный формат callback.", show_alert=True)
        return

    from handlers.polls_lifecycle import _sync_leader_report
    ready = await _is_deal_ready(deal_id)
    if not ready:
        await callback.answer("Минимальный состав ещё не набран.", show_alert=True)
        return

    roles = _extract_current_team(deal_id)
    if not _uids_from_roles(roles):
        logger.warning("[approve] deal %d has no roles in detail_roles", deal_id)
        await callback.answer("Нет текущего распределения (откройте детали и расставьте роли).", show_alert=True)
        return

    _lock_distribution(deal_id, roles)
    state.approved_deals.add(deal_id)

    await callback.answer("Игра утверждена ✅")

    # уведомление в чат ведущих
    chat_id = _leaders_chat_id()
    try:
        title = state.deal_titles.get(deal_id, f"Сделка #{deal_id}")
        text = f"🚦 Состав команды на игру «{title}» утверждён.\n" \
               f"Подтвердите своё участие в личном кабинете: «🎲 Мои игры» → «✅ Подтвердить»."
        await callback.message.bot.send_message(chat_id, text)
    except Exception as e:
        logger.error("[approve] notify leaders chat failed: %s", e)

    # обновляем дашборды «Мои игры»
    for uid in _uids_from_roles(roles):
        try:
            await redraw_my_games(uid)
        except Exception as exc:
            logger.warning("[polls_dist] redraw_my_games uid %d failed: %s", uid, exc)

    try:
        await _sync_leader_report()
    except Exception as e:
        logger.warning("[approve] _sync_leader_report failed: %s", e)

    try:
        await refresh_deal_details(callback.from_user.id, deal_id)
    except Exception as e:
        logger.warning("[approve] refresh_deal_details failed: %s", e)

    logger.info("[approve] deal %d approved by %d; roles=%s", deal_id, callback.from_user.id, roles)


# ════════════════════════════════════════════════════════════════════
# [4] HANDLERS stop/back
# ════════════════════════════════════════════════════════════════════

@router.callback_query(lambda c: c.data.startswith("poll_stop_"))
async def poll_stop_game_handler(callback: CallbackQuery) -> None:
    deal_id = int(callback.data.rsplit("_", 1)[-1])
    state.deal_force_closed.add(deal_id)
    await callback.answer("Набор остановлен.")
    logger.info("[details] deal %d force-stopped by %d", deal_id, callback.from_user.id)

@router.callback_query(lambda c: c.data.startswith("poll_back_"))
async def poll_back_handler(callback: CallbackQuery) -> None:
    await callback.answer()
    from handlers.polls_lifecycle import _sync_leader_report
    try:
        await _sync_leader_report()
    except Exception as e:
        logger.warning("[back] _sync_leader_report failed: %s", e)


# ════════════════════════════════════════════════════════════════════
# [99] SELF-TEST
# ════════════════════════════════════════════════════════════════════

async def _test() -> None:
    roles = {"main": [1], "assist": [2], "admin": [3]}
    uids = _uids_from_roles(roles)
    assert uids == {1, 2, 3}
    print("handlers.polls_distribution ✅ tests passed")

if __name__ == "__main__":
    import asyncio, logging as _l
    _l.basicConfig(level=_l.DEBUG)
    asyncio.run(_test())
