# handlers/confirmations.py
# ─────────────────────────────────────────────────────────────────────────────
"""Подтверждения «+» от ведущих.

Функциональность
• plus_confirmation_handler — ловит сообщения с «+»:
    ─ если «+» дан в ответ на пост-утверждение игры → пишет теги в AmoCRM;
    ─ если «+» дан под общим постом подтверждения → засчитывает подтверждение
      в рамках всего опроса.
• _get_all_assigned_uids()  — UID всех, кто назначен на текущие игры.

Соответствует MasterBot Style Guide 12.93 (2025-07-22).
"""

from __future__ import annotations

# ███ [0] IMPORTS
# --------------------------------------------------------------------
import logging
from typing import Optional, Set

from aiogram import Bot, Router, types

from core.config import settings
from core.state import state
from services.amocrm import update_amocrm_tags

logger = logging.getLogger(__name__)
router = Router()

# ════════════════════════════════════════════════════════════════════
# [1] HELPERS
# ════════════════════════════════════════════════════════════════════
def _extract_user_id_from_tag(tag: str) -> Optional[int]:
    """Из строки 'Имя|123' или 'Имя Ф.|123' вытаскивает int-user_id."""
    if "|" not in tag:
        return None
    try:
        return int(tag.rsplit("|", 1)[-1])
    except ValueError:
        return None


async def _get_all_assigned_uids() -> Set[int]:
    """UID всех, кто назначен хотя бы на одну игру."""
    assigned: Set[int] = set()
    for deal_tags in state.distribution_cache.values():
        for tag in deal_tags.values():
            uid = _extract_user_id_from_tag(tag)
            if uid is not None:
                assigned.add(uid)
    return assigned


def _uids_for_deal(deal_id: int) -> Set[int]:
    """UID назначенных на конкретную игру."""
    tags = state.distribution_cache.get(str(deal_id), {})
    return {
        uid
        for uid in (_extract_user_id_from_tag(t) for t in tags.values())
        if uid is not None
    }

# ════════════════════════════════════════════════════════════════════
# [2] HANDLER
# ════════════════════════════════════════════════════════════════════
@router.message(lambda m: m.text and m.text.strip() == "+")
async def plus_confirmation_handler(message: types.Message) -> None:
    """
    Ловит «+» в admin-чате.

    ① Ответ на пост утверждения игры
       → проверяет, что отправитель входит в команду,
       → записывает теги в AmoCRM,
       → удаляет запись из pending_plus.
    ② Любое «+» под общим постом подтверждений
       → добавляет UID в state.confirmed_users.
    """
    if not state.coordination_cycle_active or message.chat.id != state.admin_chat_id:
        return

    user_id = message.from_user.id

    # ── ① reply-to пост (утверждение игры) ──────────────────────────
    if (
        message.reply_to_message
        and message.reply_to_message.message_id in state.pending_plus
    ):
        deal_id = state.pending_plus.pop(message.reply_to_message.message_id)
        if user_id not in _uids_for_deal(deal_id):
            try:
                await message.reply("ℹ️ Вы не назначены на эту игру.", reply=False)
            except Exception:
                logger.exception("Не удалось уведомить не-назначенного %d", user_id)
            return

        ok = await update_amocrm_tags(
            {str(deal_id): state.distribution_cache.get(str(deal_id), {})}
        )
        if ok:
            txt = "💾 Теги сохранены, спасибо!"
            logger.info("[confirmations] deal %d: tags saved by %d", deal_id, user_id)
        else:
            txt = "❌ Ошибка при сохранении тегов."
            logger.error("[confirmations] deal %d: tag save FAILED", deal_id)

        try:
            await message.reply(txt, reply=False)
        except Exception:
            logger.exception("Не удалось ответить на + (%d)", user_id)
        return  # reply-case обработан полностью

    # ── ② общее подтверждение «+» ───────────────────────────────────
    assigned = await _get_all_assigned_uids()
    if user_id not in assigned:
        try:
            await message.reply("ℹ️ Вы не назначены на текущие игры.", reply=False)
        except Exception:
            logger.exception("Не уведомили лишнего пользователя %d", user_id)
        return

    state.confirmed_users.add(user_id)
    logger.info(
        "[confirmations] '+' от %d — %d/%d",
        user_id,
        len(state.confirmed_users),
        len(assigned),
    )
    try:
        await message.reply("✅ Принято, спасибо!", reply=False)
    except Exception:
        logger.exception("Не ответили на + пользователя %d", user_id)

    if state.confirmed_users >= assigned:
        bot = Bot.get_current()
        try:
            await bot.send_message(
                state.admin_chat_id,
                "🎉 Все ведущие подтвердили участие. Цикл распределения завершён.",
            )
        except Exception:
            logger.exception("Не удалось сообщить о завершении цикла")

        from handlers.polls_lifecycle import clear_poll_data  # локальный import
        initiator = state.current_poll_leader or settings.LEADER_ID
        await clear_poll_data(initiator)

# ════════════════════════════════════════════════════════════════════
# [3] TESTS
# ════════════════════════════════════════════════════════════════════
async def _test() -> None:
    """Мини-тесты: парсинг тегов и assigned UID."""
    assert _extract_user_id_from_tag("Иван|123") == 123
    assert _extract_user_id_from_tag("WrongTag") is None

    state.distribution_cache = {
        "1": {"lead1": "A|1", "assistant1": "B|2", "admin": "C|3"}
    }
    assert await _get_all_assigned_uids() == {1, 2, 3}
    print("handlers/confirmations tests passed")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_test())
