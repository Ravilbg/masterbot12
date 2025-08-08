"""handlers/my_games.py — дашборд «Мои игры»
─────────────────────────────────────────────────────────────────────────────
Версия 2.0 · 2025-08-07

Изменения против 1.0
────────────────────
• Добавлен «📝 Написать отчёт» (если пользователь — главный ведущий).
• В деталях всегда есть «🔄 Попросить замену».
• Кнопка «✅ Подтвердить» сохраняется (для статуса «Бронь»).
• Все переходы очищают прошлые сообщения через delete_previous_private_messages.
"""

from __future__ import annotations

# ███ [1] IMPORTS
# --------------------------------------------------------------------
import logging
from typing import Dict, List, Optional, Set

from aiogram import Bot, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pytz import timezone

from core.config import settings
from core.db import get_user_info
from core.state import state
from core.utils import truncate, delete_previous_private_messages
from handlers.confirmations import CONFIRM_PREFIX  # «confirm_participation_…»

from services.amocrm import get_amocrm_deals

logger = logging.getLogger(__name__)
router = Router()
MSK_TZ = timezone("Europe/Moscow")

# ── статус-ID, где нужен «Подтвердить» ────────────────────────────
BRON_STATUS_ID = "18913933"  # «Бронь»
OK_STATUS_ID   = settings.SUCCESSFUL_STATUS_ID  # «Закрытие сделки»

# ── callback-префиксы ─────────────────────────────────────────────
DETAILS_PREFIX = "mygame_details_"
REPORT_PREFIX  = "mygame_report_"
SWAP_PREFIX    = "mygame_swap_"

# ════════════════════════════════════════════════════════════════════
# [2] HELPERS
# ════════════════════════════════════════════════════════════════════

def _confirmed(uid: int, deal_id: int) -> bool:
    confirmed_map: Dict[int, Set[int]] = state.__dict__.setdefault("confirmed", {})
    return uid in confirmed_map.get(deal_id, set())


def _is_main_leader(uid: int, deal: Dict) -> bool:
    """Определяем, является ли uid главным ведущим для deal."""
    # Правило: первый в списке team_leads — главный.
    leads = deal.get("team_leads", [])
    return bool(leads and str(leads[0].get("id")) == str(uid))


def _details_text(deal: Dict, confirmed: bool) -> str:
    date_part = deal["event_datetime"].strftime("%d.%m.%Y")
    status = "✅ Подтверждено" if confirmed else "⏳ Ожидает подтверждения"
    return "\n".join([
        f"🎮 *{deal['game_name']}*",
        f"📅 *Дата*: {date_part}",
        f"🕒 *Время*: {deal.get('event_time', '—')}",
        f"📦 *Пакет*: {deal.get('package', '—')}",
        f"👥 *Игроки*: {deal.get('players', '—')}",
        f"🔖 *Статус*: {status}",
        f"💬 *Комментарий*: {deal.get('comment', '—')}",
    ])


def _details_keyboard(uid: int, deal: Dict, confirmed: bool) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []

    # Подтверждение (если Бронь и не подтверждён)
    if deal["status_id"] == BRON_STATUS_ID and not confirmed:
        rows.append([
            InlineKeyboardButton(text="✅ Подтвердить участие", callback_data=f"{CONFIRM_PREFIX}{deal['id']}")
        ])

    # Кнопка «Написать отчёт» (если главный ведущий)
    if _is_main_leader(uid, deal):
        rows.append([
            InlineKeyboardButton(text="📝 Написать отчёт", callback_data=f"{REPORT_PREFIX}{deal['id']}")
        ])

    # Попросить замену (всегда доступно)
    rows.append([
        InlineKeyboardButton(text="🔄 Попросить замену", callback_data=f"{SWAP_PREFIX}{deal['id']}")
    ])

    rows.append([
        InlineKeyboardButton(text="← Назад", callback_data="mygames_back")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _my_games(uid: int, deals: List[Dict]) -> List[Dict]:
    """Выбираем сделки по статусу и присутствию uid в team_leads."""
    wanted = {BRON_STATUS_ID, OK_STATUS_ID}
    return [
        d for d in deals
        if d["status_id"] in wanted and any(str(uid) == str(t.get("id")) for t in d.get("team_leads", []))
    ]

# ════════════════════════════════════════════════════════════════════
# [3] DASHBOARD
# ════════════════════════════════════════════════════════════════════

async def _send_dashboard(uid: int, deals: List[Dict]) -> None:
    bot = Bot.get_current()
    kb = InlineKeyboardBuilder()

    for d in sorted(deals, key=lambda x: x["event_datetime"]):
        date = d["event_datetime"].strftime("%d.%m")
        title = truncate(f"{d['game_name']} · {date}", 40)

        kb.button(text=f"ℹ️ {title}", callback_data=f"{DETAILS_PREFIX}{d['id']}")

    kb.adjust(1)
    await delete_previous_private_messages(uid)
    sent = await bot.send_message(uid, "🎲 *Мои игры:*", parse_mode="Markdown", reply_markup=kb.as_markup())

    state.my_games_by_user[uid] = deals
    state.last_user_messages[uid] = [sent]

# ════════════════════════════════════════════════════════════════════
# [4] HANDLERS
# ════════════════════════════════════════════════════════════════════

@router.message(Command("my_games"))
@router.message(lambda m: (m.text or "").strip() == "🎲 Мои игры")
async def my_games_handler(message: types.Message) -> None:
    uid = message.from_user.id
    all_deals = await get_amocrm_deals(settings.SVETOFOR_SPREAD_ID)
    deals = _my_games(uid, all_deals)

    if not deals:
        await message.answer("😔 Назначенных игр пока нет.")
    else:
        await _send_dashboard(uid, deals)

    try:
        await message.delete()
    except Exception:
        pass


@router.callback_query(lambda c: c.data.startswith(DETAILS_PREFIX))
async def mygame_details_handler(callback: types.CallbackQuery) -> None:
    uid = callback.from_user.id
    deal_id = int(callback.data.split("_")[-1])
    deal = next((d for d in state.my_games_by_user.get(uid, []) if d["id"] == deal_id), None)
    if not deal:
        await callback.answer("⚠️ Игра не найдена.", show_alert=True)
        return

    await delete_previous_private_messages(uid)
    conf = _confirmed(uid, deal_id)
    text = _details_text(deal, conf)
    kb = _details_keyboard(uid, deal, conf)
    msg = await Bot.get_current().send_message(uid, text, parse_mode="Markdown", reply_markup=kb)
    state.last_user_messages[uid] = [msg]
    await callback.answer()


@router.callback_query(lambda c: c.data == "mygames_back")
async def back_to_dashboard_handler(callback: types.CallbackQuery) -> None:
    uid = callback.from_user.id
    deals = state.my_games_by_user.get(uid, [])
    if deals:
        await _send_dashboard(uid, deals)
    await callback.answer()


@router.callback_query(lambda c: c.data.startswith(REPORT_PREFIX))
async def report_placeholder(callback: types.CallbackQuery) -> None:
    await callback.answer("📝 Отчёт — в разработке.", show_alert=True)


@router.callback_query(lambda c: c.data.startswith(SWAP_PREFIX))
async def swap_placeholder(callback: types.CallbackQuery) -> None:
    await callback.answer("🔄 Замена — в разработке.", show_alert=True)

# ════════════════════════════════════════════════════════════════════
# [5] SELF-TEST
# ════════════════════════════════════════════════════════════════════

async def _test() -> None:
    from datetime import datetime

    dummy = {
        "id": 1,
        "game_name": "Quest Room",
        "event_datetime": MSK_TZ.localize(datetime.now()),
        "status_id": BRON_STATUS_ID,
        "team_leads": [{"id": "123"}],
        "players": "2-6",
    }
    assert _my_games(123, [dummy]) == [dummy]
    assert _is_main_leader(123, dummy)
    assert not _confirmed(123, 1)
    print("handlers.my_games ✅ tests passed")


if __name__ == "__main__":
    import asyncio, logging as _l
    _l.basicConfig(level=_l.DEBUG)
    asyncio.run(_test())