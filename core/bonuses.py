"""handlers/bonuses.py — меню «Бонусы»
─────────────────────────────────────────────────────────────────────────────
Версия 1.0 · 2025‑08‑07

Функциональность (заглушка)
───────────────────────────
Кнопка «🎁 Бонусы» или команда /bonuses выводит inline‑меню:
    • 🎉 Праздник со скидкой
    • 🖨 Принтер
    • 🎭 Реквизит
    • 🕓 Свободное время в квест‑кафе
Каждый пункт пока отвечает заглушкой «В разработке».
Все экраны используют пылесос delete_previous_private_messages и пишут
последние сообщения в state.last_user_messages.

Доступ: роли из settings.ACCESS["games"].
"""

from __future__ import annotations

# ███ [1] IMPORTS
# --------------------------------------------------------------------
import logging
from aiogram import Bot, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config import settings
from core.db import get_user_info
from core.state import state
from core.utils import delete_previous_private_messages

logger = logging.getLogger(__name__)
router = Router()

# ── callback‑константы ──────────────────────────────────────────────
BONUS_PREFIX  = "bonus_"
CB_DISCOUNT   = f"{BONUS_PREFIX}discount"
CB_PRINTER    = f"{BONUS_PREFIX}printer"
CB_PROPS      = f"{BONUS_PREFIX}props"
CB_SCHEDULE   = f"{BONUS_PREFIX}schedule"
CB_BACK       = f"{BONUS_PREFIX}back"

# ════════════════════════════════════════════════════════════════════
# [2] HELPERS
# ════════════════════════════════════════════════════════════════════

def _bonus_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🎉 Праздник со скидкой", callback_data=CB_DISCOUNT)
    kb.button(text="🖨 Принтер",            callback_data=CB_PRINTER)
    kb.button(text="🎭 Реквизит",           callback_data=CB_PROPS)
    kb.button(text="🕓 Свободное время",    callback_data=CB_SCHEDULE)
    kb.adjust(1)
    return kb.as_markup()

# ════════════════════════════════════════════════════════════════════
# [3] HANDLERS
# ════════════════════════════════════════════════════════════════════

@router.message(Command("bonuses"))
@router.message(lambda m: (m.text or "").strip() == "🎁 Бонусы")
async def bonuses_handler(message: types.Message) -> None:
    """Главная кнопка «Бонусы» — показывает меню вариантов."""
    uid = message.from_user.id
    ui = await get_user_info(uid) or {}
    if ui.get("role") not in settings.ACCESS["games"]:
        await message.answer("⛔ Нет доступа.")
        return

    await delete_previous_private_messages(uid)
    kb = _bonus_keyboard()
    sent = await message.answer("🎁 *Доступные бонусы:*", parse_mode="Markdown", reply_markup=kb)
    state.last_user_messages[uid] = [sent]
    try:
        await message.delete()
    except Exception:
        pass


@router.callback_query(lambda c: c.data in {CB_DISCOUNT, CB_PRINTER, CB_PROPS, CB_SCHEDULE})
async def bonus_placeholder(callback: types.CallbackQuery) -> None:
    """Заглушка для каждого бонуса."""
    mapping = {
        CB_DISCOUNT:  "🎉 Праздник со скидкой",
        CB_PRINTER:   "🖨 Принтер",
        CB_PROPS:     "🎭 Реквизит",
        CB_SCHEDULE:  "🕓 Свободное время",
    }
    await callback.answer(f"{mapping[callback.data]} — в разработке.", show_alert=True)

# ════════════════════════════════════════════════════════════════════
# [4] SELF‑TEST (smoke)
# ════════════════════════════════════════════════════════════════════

async def _test():
    assert CB_DISCOUNT.startswith(BONUS_PREFIX)
    print("handlers.bonuses ✅ tests passed")


if __name__ == "__main__":
    import asyncio, logging as _l
    _l.basicConfig(level=_l.DEBUG)
    asyncio.run(_test())
