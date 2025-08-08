# handlers/confirmations.py
# ─────────────────────────────────────────────────────────────────────────────
"""
Подтверждение участия кнопкой «✅ Подтвердить участие».

Изменения v13.0 · 2025-08-03
──────────────────────────────────────────────────────────────────────────────
• Старый *plus_confirmation_handler* удалён — «+» больше не используется.
• Новый *confirm_participation_handler* работает по callback-data  
  ``confirm_participation_<deal_id>`` и выполняет:

  1. Проверяет, назначен ли пользователь в *state.locked_distribution*  
     на указанную игру. Если нет — отвечает предупреждением.
  2. Добавляет UID в `state.confirmed[deal_id]`.
  3. Когда подтвердили **все** назначенные на игру —  
     • пишет теги в AmoCRM из *locked_distribution*;  
     • шлёт сообщение в чат ведущих о полном подтверждении.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Set

from aiogram import Bot, Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup

from core.config import settings
from core.state import state
from services.amocrm import update_amocrm_tags

logger = logging.getLogger(__name__)
router = Router()

# ════════════════════════════════════════════════════════════════════
# [1] HELPERS
# ════════════════════════════════════════════════════════════════════
CONFIRM_PREFIX = "confirm_participation_"


def _extract_uid(tag: str) -> Optional[int]:
    """'Имя|123' → 123 | None."""
    if "|" not in tag:
        return None
    try:
        return int(tag.rsplit("|", 1)[-1])
    except ValueError:
        return None


def _assigned_uids(deal_id: int) -> Set[int]:
    """UID-ы всех, кто зафиксирован на игру *deal_id*."""
    tags = state.locked_distribution.get(deal_id, {})
    return {uid for uid in (_extract_uid(t) for t in tags.values()) if uid is not None}


def _get_all_assigned_uids() -> Set[int]:  # ← для handlers.polls_distribution
    """UID всех, кто назначен хотя бы на одну игру текущего цикла."""
    assigned: Set[int] = set()
    for deal_roles in state.locked_distribution.values():
        for tag in deal_roles.values():
            uid = _extract_uid(tag)
            if uid is not None:
                assigned.add(uid)
    return assigned


def _confirm_map() -> Dict[int, Set[int]]:
    """
    state.confirmed: deal_id → {uid…}
    Создаётся динамически при первом обращении.
    """
    return state.__dict__.setdefault("confirmed", {})


# ════════════════════════════════════════════════════════════════════
# [2] CALLBACK-HANDLER
# ════════════════════════════════════════════════════════════════════
@router.callback_query(lambda c: c.data.startswith(CONFIRM_PREFIX))
async def confirm_participation_handler(callback: CallbackQuery) -> None:
    """
    Пользователь нажал «✅ Подтвердить участие».
    """
    await callback.answer()  # instant ACK

    try:
        deal_id = int(callback.data[len(CONFIRM_PREFIX):])
    except ValueError:
        await callback.answer("⚠️ Неверные данные.", show_alert=True)
        return

    uid = callback.from_user.id
    assigned = _assigned_uids(deal_id)

    # 1️⃣ назначен ли пользователь?
    if uid not in assigned:
        await callback.answer("Вы не назначены на эту игру.", show_alert=True)
        return

    # 2️⃣ фиксируем подтверждение
    confirmed = _confirm_map().setdefault(deal_id, set())
    if uid in confirmed:
        await callback.answer("Участие уже подтверждено ✅", show_alert=True)
        return

    confirmed.add(uid)
    await callback.answer("Спасибо! Участие подтверждено ✅", show_alert=True)
    logger.info("[confirm] uid=%d confirmed deal=%d (%d/%d)",
                uid, deal_id, len(confirmed), len(assigned))

    # 3️⃣ все подтвердили?
    if confirmed >= assigned and assigned:
        ok = await update_amocrm_tags(
            {str(deal_id): state.locked_distribution[deal_id]}
        )
        if not ok:
            logger.error("[confirm] AmoCRM tags write FAILED for deal %d", deal_id)

        # сообщение в чат ведущих
        bot = Bot.get_current()
        try:
            game_name = next(
                d["game_name"] for d in state.current_poll_deals if d["id"] == deal_id
            )
        except StopIteration:
            game_name = f"ID {deal_id}"

        await bot.send_message(
            state.admin_chat_id,
            f"🎉 Все участники подтвердили игру *{game_name}*.",
            parse_mode="Markdown",
        )
        logger.info("[confirm] deal %d FULLY confirmed", deal_id)

        # убираем кнопку у подтвердившего последним
        try:
            await callback.message.edit_reply_markup(
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[])
            )
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════
# [3] TESTS
# ════════════════════════════════════════════════════════════════════
async def _test() -> None:
    state.locked_distribution = {
        1: {"lead1": "Иван|1", "assistant1": "Пётр|2"},
        2: {"lead1": "Ольга|3"},
    }
    assert _assigned_uids(1) == {1, 2}
    assert _assigned_uids(2) == {3}
    assert _get_all_assigned_uids() == {1, 2, 3}

    cm = _confirm_map()
    cm.clear()
    cm[1] = {1}
    assert 1 in cm[1]
    print("handlers.confirmations ✅ tests passed")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_test())
