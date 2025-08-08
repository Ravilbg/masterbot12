"""handlers/profile.py — личный кабинет пользователя
─────────────────────────────────────────────────────────────────────────────
Версия 15.1 · 2025-08-07

Fixes vs 15.0
─────────────
• Добавлены импорты `F` и `ChatType` (NameError).
• Завершён тело `back_to_profile_handler` и добавлен callback `noop`.
"""

from __future__ import annotations

# ███ [1] IMPORTS
# --------------------------------------------------------------------
import logging
from typing import Dict, List, Optional

from aiogram import Bot, Router, F, types
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config import settings
from core.db import get_user_info
from core.state import state
from core.utils import delete_previous_private_messages
from handlers.confirmations import CONFIRM_PREFIX
from handlers.guide import PROFILE_BUTTON_TEXT
from core.menu import get_main_menu
from handlers.my_games import _my_games, _send_dashboard
from services.amocrm import get_amocrm_deals

logger = logging.getLogger(__name__)
router = Router()

# ███ [2] CONSTANTS / CALLBACK-DATA
# --------------------------------------------------------------------
BACK_TO_MENU    = "profile_back"
BACK_TO_PROFILE = "profile_main"
DETAILS_PREFIX  = "profile_deal_"
SWAP_PREFIX     = "profile_swap_"
MY_GAMES_CB     = "profile_mygames"
NOOP_CB         = "noop"

KB_LINK = getattr(settings, "KB_LINK", "https://example.com/knowledge_base")

# ════════════════════════════════════════════════════════════════════
# [3] HELPERS
# ════════════════════════════════════════════════════════════════════

def _extract_uid(tag: str) -> Optional[int]:
    if "|" not in tag:
        return None
    try:
        return int(tag.rsplit("|", 1)[-1])
    except ValueError:
        return None


def _my_assigned_deals(uid: int) -> List[Dict]:
    if not state.locked_distribution or not state.current_poll_deals:
        return []
    mine = [int(did) for did, roles in state.locked_distribution.items() if any(_extract_uid(tag) == uid for tag in roles.values())]
    return [d for d in state.current_poll_deals if d["id"] in mine]


def _stats_stub() -> str:
    return "📈 *Статистика*: _в разработке_"


def _profile_text(ui: Dict, deals: List[Dict]) -> str:
    name = f"{ui.get('first_name', '')} {ui.get('last_name_initial', '')}".strip()
    header = f"👤 *{name or 'Пользователь'}*"
    if ui.get("role"):
        header += f" · _{ui['role']}_"  # type: ignore[index]

    if deals:
        lines = [f"• {d['game_name']} — {d['event_datetime']:%d.%m}" for d in sorted(deals, key=lambda x: x["event_datetime"])]
        games_block = "🎯 *Мои назначенные игры:*\n" + "\n".join(lines)
    else:
        games_block = "😔 Назначенных игр пока нет."

    return "\n\n".join([header, _stats_stub(), games_block])


async def _profile_keyboard(uid: int, deals: List[Dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    reply_kb = await get_main_menu(uid)
    if reply_kb:
        for row in reply_kb.keyboard:  # type: ignore[attr-defined]
            for btn in row:
                txt = btn.text
                if txt == "🎲 Мои игры":
                    kb.button(text=txt, callback_data=MY_GAMES_CB)
                elif txt == PROFILE_BUTTON_TEXT:
                    continue
                else:
                    kb.button(text=txt, callback_data=NOOP_CB)
    kb.button(text="📚 База знаний", url=KB_LINK)
    for d in sorted(deals, key=lambda x: x["event_datetime"]):
        kb.button(text=f"{d['game_name']} · {d['event_datetime']:%d.%m}", callback_data=f"{DETAILS_PREFIX}{d['id']}")
    kb.button(text="← Назад", callback_data=BACK_TO_MENU)
    kb.adjust(1)
    return kb.as_markup()


def _confirmed(uid: int, deal_id: int) -> bool:
    return uid in state.__dict__.get("confirmed", {}).get(deal_id, set())


def _details_text(deal: Dict, confirmed: bool) -> str:
    status = "✅ Подтверждено" if confirmed else "⏳ Ожидает подтверждения"
    return "\n".join([
        f"🎮 *{deal['game_name']}*",
        f"📅 *Дата*: {deal['event_datetime']:%d.%m.%Y}",
        f"🕒 *Время*: {deal.get('event_time', '—')}",
        f"📦 *Пакет*: {deal.get('package', '—')}",
        f"👥 *Игроки*: {deal.get('players', '—')}",
        f"🔖 *Статус*: {status}",
        f"💬 *Комментарий*: {deal.get('comment', '—')}",
    ])


def _details_keyboard(deal_id: int, confirmed: bool) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if not confirmed:
        rows.append([InlineKeyboardButton(text="✅ Подтвердить участие", callback_data=f"{CONFIRM_PREFIX}{deal_id}")])
    else:
        rows.append([InlineKeyboardButton(text="🔄 Попросить замену", callback_data=f"{SWAP_PREFIX}{deal_id}")])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=BACK_TO_PROFILE)])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ════════════════════════════════════════════════════════════════════
# [4] HANDLERS
# ════════════════════════════════════════════════════════════════════

@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
@router.message(CommandStart(deep_link="profile"))
@router.message(Command(commands=["profile"]))
@router.message(lambda m: (m.text or "").strip() == PROFILE_BUTTON_TEXT)
async def profile_handler(message: types.Message) -> None:
    uid = message.from_user.id
    ui = await get_user_info(uid) or {}
    await delete_previous_private_messages(uid)
    text = _profile_text(ui, _my_assigned_deals(uid))
    kb = await _profile_keyboard(uid, _my_assigned_deals(uid))
    sent = await Bot.get_current().send_message(uid, text, parse_mode="Markdown", reply_markup=kb)
    state.last_user_messages[uid] = [sent]
    try:
        await message.delete()
    except Exception:
        pass


@router.callback_query(lambda c: c.data == MY_GAMES_CB)
async def open_my_games_from_profile(callback: types.CallbackQuery) -> None:
    uid = callback.from_user.id
    await delete_previous_private_messages(uid)
    my_deals = _my_games(uid, await get_amocrm_deals(settings.SVETOFOR_SPREAD_ID))
    if not my_deals:
        await callback.message.answer("😔 Назначенных игр нет.")
    else:
        await _send_dashboard(uid, my_deals)
    await callback.answer()


@router.callback_query(lambda c: c.data == BACK_TO_MENU)
async def profile_back_handler(callback: types.CallbackQuery) -> None:
    uid = callback.from_user.id
    await delete_previous_private_messages(uid)
    kb = await get_main_menu(uid)
    if kb:
        await callback.message.answer("\u2060", reply_markup=kb)
    await callback.answer()


@router.callback_query(lambda c: c.data.startswith(DETAILS_PREFIX))
async def profile_deal_details_handler(callback: types.CallbackQuery) -> None:
    uid = callback.from_user.id
    deal_id = int(callback.data.split("_")[-1])
    deal = next((d for d in state.current_poll_deals if d["id"] == deal_id), None)
    if not deal:
        await callback.answer("⚠️ Игра не найдена.", show_alert=True)
        return
    await delete_previous_private_messages(uid)
    sent = await Bot.get_current().send_message(uid, _details_text(deal, _confirmed(uid, deal_id)), parse_mode="Markdown", reply_markup=_details_keyboard(deal_id, _confirmed(uid, deal_id)))
    state.last
