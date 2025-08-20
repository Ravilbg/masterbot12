"""handlers/profile.py — личный кабинет пользователя
────────────────────────────────────────────────────────────────────────────
Версия 15.3 · 2025-08-17

Fix 15.3
• «🎲 Мои игры» — не рисуем дашборд в этом модуле; делегируем в handlers.my_games.redraw_my_games(uid).
• Перед показом профиля — жёсткий пылесос delete_previous_private_messages(bot, uid).
• Сообщение профиля складывается в state.last_user_messages[uid], чтобы потом корректно сносилось.
• Безопасные вызовы: get_user_info поддерживает sync/async; сортировки и форматирование дат не падают на None.
• Удалены неиспользуемые импорты, устранены причины ворнингов Pylance.
"""

from __future__ import annotations

# ███ [1] IMPORTS
# --------------------------------------------------------------------
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

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

logger = logging.getLogger(__name__)
router = Router()

# ███ [2] CALLBACK-CONSTS
# --------------------------------------------------------------------
BACK_TO_MENU = "profile_back"
BACK_TO_PROFILE = "profile_main"
DETAILS_PREFIX = "profile_deal_"
SWAP_PREFIX = "profile_swap_"
MY_GAMES_CB = "profile_mygames"

KB_LINK = getattr(settings, "KB_LINK", "https://example.com/knowledge_base")


# ███ [3] HELPERS
# --------------------------------------------------------------------
def _extract_uid(tag: str) -> Optional[int]:
    if "|" not in tag:
        return None
    try:
        return int(tag.rsplit("|", 1)[-1])
    except (ValueError, TypeError):
        return None


def _my_assigned_deals(uid: int) -> List[Dict[str, Any]]:
    """Назначенные пользователю сделки из зафиксированного распределения + текущей выборки."""
    locked = getattr(state, "locked_distribution", {}) or {}
    current = getattr(state, "current_poll_deals", []) or []
    if not locked or not current:
        return []

    mine_ids: List[int] = []
    for did, roles in locked.items():
        try:
            deal_id = int(did)
        except Exception:
            continue
        if not isinstance(roles, dict):
            continue
        if any(_extract_uid(str(tag)) == uid for tag in roles.values() if tag):
            mine_ids.append(deal_id)

    by_id = {int(d.get("id", 0)): d for d in current if isinstance(d, dict)}
    return [by_id[i] for i in mine_ids if i in by_id]


def _stats_stub() -> str:
    return "📈 *Статистика*: _в разработке_"


def _safe_dt_fmt(dt: Any, fmt: str = "%d.%m.%Y") -> str:
    if isinstance(dt, datetime):
        return dt.strftime(fmt)
    return "—"


def _profile_text(ui: Dict[str, Any], deals: List[Dict[str, Any]]) -> str:
    name = f"{ui.get('first_name', '')} {ui.get('last_name_initial', '')}".strip()
    header = f"👤 *{name or 'Пользователь'}*"
    role = ui.get("role")
    if role:
        header += f" · _{role}_"

    if deals:
        def _key(d: Dict[str, Any]):
            dt = d.get("event_datetime")
            return (dt is None, dt or datetime.max)

        lines = [
            f"• {d.get('game_name') or d.get('name', 'Игра')} — {_safe_dt_fmt(d.get('event_datetime'), '%d.%m')}"
            for d in sorted(deals, key=_key)
        ]
        games_block = "\n".join(lines)
    else:
        games_block = "Нет назначенных игр."

    return "\n".join([header, "", games_block, "", _stats_stub()])


async def _profile_keyboard(uid: int, deals: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    # глобальные кнопки
    kb.button(text="🎲 Мои игры", callback_data=MY_GAMES_CB)
    kb.button(text="📚 База знаний", url=KB_LINK)

    # список игр — просто ссылки на детали (локальный просмотр в профиле)
    def _key(d: Dict[str, Any]):
        dt = d.get("event_datetime")
        return (dt is None, dt or datetime.max)

    for d in sorted(deals, key=_key):
        date = _safe_dt_fmt(d.get("event_datetime"), "%d.%m")
        title = d.get("game_name") or d.get("name", "Игра")
        kb.button(text=f"{title} · {date}", callback_data=f"{DETAILS_PREFIX}{d.get('id')}")

    kb.button(text="← Назад", callback_data=BACK_TO_MENU)
    kb.adjust(1)
    return kb.as_markup()


def _confirmed(uid: int, deal_id: int) -> bool:
    """Старый локальный маркер подтверждений, оставляем для совместимости."""
    try:
        return uid in state.__dict__.get("confirmed", {}).get(deal_id, set())
    except Exception:
        return False


def _details_text(deal: Dict[str, Any], confirmed: bool) -> str:
    status = "✅ Подтверждено" if confirmed else "⏳ Ожидает подтверждения"
    return "\n".join(
        [
            f"🎮 *{deal.get('game_name') or deal.get('name', 'Игра')}*",
            f"📅 *Дата*: {_safe_dt_fmt(deal.get('event_datetime'), '%d.%m.%Y')}",
            f"🕒 *Время*: {deal.get('event_time', '—')}",
            f"📦 *Пакет*: {deal.get('package', '—')}",
            f"👥 *Игроки*: {deal.get('players', '—')}",
            f"🔖 *Статус*: {status}",
            f"💬 *Комментарий*: {deal.get('comment', '—')}",
        ]
    )


def _details_keyboard(deal_id: int, confirmed: bool) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if not confirmed:
        # В проекте CONFIRM_PREFIX используется общим обработчиком подтверждений
        rows.append([InlineKeyboardButton(text="✅ Подтвердить участие", callback_data=f"{CONFIRM_PREFIX}{deal_id}")])
    else:
        rows.append([InlineKeyboardButton(text="🔄 Попросить замену", callback_data=f"{SWAP_PREFIX}{deal_id}")])
    rows.append([InlineKeyboardButton(text="← Назад", callback_data=BACK_TO_PROFILE)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _get_user_info(uid: int) -> Dict[str, Any]:
    """Поддержка sync/async варианта core.db.get_user_info."""
    try:
        if callable(get_user_info):
            if hasattr(get_user_info, "__await__"):  # pragma: no cover (редко)
                data = await get_user_info(uid)  # type: ignore[misc]
            else:
                data = get_user_info(uid)  # type: ignore[misc]
            return data or {}
    except Exception:
        logger.debug("[profile] get_user_info failed", exc_info=True)
    return {}


# ███ [4] HANDLERS
# --------------------------------------------------------------------
@router.message(CommandStart(), F.chat.type == ChatType.PRIVATE)
@router.message(CommandStart(deep_link="profile"))
@router.message(Command(commands=["profile"]))
@router.message(lambda m: (m.text or "").strip() == PROFILE_BUTTON_TEXT)
async def profile_handler(message: types.Message) -> None:
    logger.debug("[profile_handler] from uid=%d text=%r", message.from_user.id, message.text)
    uid = message.from_user.id
    bot = Bot.get_current()

    # Жёсткий пылесос перед показом профиля
    await delete_previous_private_messages(bot, uid)

    ui = await _get_user_info(uid)
    deals = _my_assigned_deals(uid)
    text = _profile_text(ui, deals)
    kb = await _profile_keyboard(uid, deals)

    sent = await bot.send_message(uid, text, parse_mode="Markdown", reply_markup=kb)
    # Записываем профильное сообщение, чтобы «пылесос» затем сносил и его
    (getattr(state, "last_user_messages", {}) or {}).setdefault(uid, [])
    state.last_user_messages[uid] = [sent]

    # Уберём команду/кнопку пользователя
    try:
        await message.delete()
    except Exception:
        pass


@router.callback_query(lambda c: c.data == MY_GAMES_CB)
async def open_my_games_from_profile(callback: types.CallbackQuery) -> None:
    logger.debug("[open_my_games] uid=%d", callback.from_user.id)
    uid = callback.from_user.id

    # Делегируем в дашборд из handlers.my_games (не рисуем второй дашборд здесь!)
    try:
        from handlers.my_games import redraw_my_games  # локальный импорт — исключаем циклический импорт
        await redraw_my_games(uid)
    except Exception as exc:
        logger.exception("[profile] redraw_my_games failed: %s", exc)
        await callback.answer("⚠️ Не удалось получить список игр.", show_alert=True)
        return

    await callback.answer()


@router.callback_query(lambda c: c.data == BACK_TO_MENU)
async def profile_back_handler(callback: types.CallbackQuery) -> None:
    uid = callback.from_user.id
    logger.debug("[profile_back] uid=%d", uid)
    bot = Bot.get_current()
    await delete_previous_private_messages(bot, uid)
    kb = await get_main_menu(uid)
    if kb:
        await callback.message.answer("\u2060", reply_markup=kb)
    await callback.answer()


@router.callback_query(lambda c: c.data.startswith(DETAILS_PREFIX))
async def profile_deal_details_handler(callback: types.CallbackQuery) -> None:
    uid = callback.from_user.id
    bot = Bot.get_current()
    try:
        deal_id = int((callback.data or "").split("_")[-1])
    except Exception:
        await callback.answer("⚠️ Игра не найдена.", show_alert=True)
        return

    current = getattr(state, "current_poll_deals", []) or []
    deal = next((d for d in current if int(d.get("id", 0)) == deal_id), None)
    if not deal:
        await callback.answer("⚠️ Игра не найдена.", show_alert=True)
        return

    # Жёстко очищаем ЛС перед показом деталей профиля
    await delete_previous_private_messages(bot, uid)

    confirmed = _confirmed(uid, deal_id)
    sent = await bot.send_message(
        uid,
        _details_text(deal, confirmed),
        parse_mode="Markdown",
        reply_markup=_details_keyboard(deal_id, confirmed),
    )
    (getattr(state, "last_user_messages", {}) or {}).setdefault(uid, [])
    state.last_user_messages[uid] = [sent]
    await callback.answer()


# ███ [5] SELF-TEST
# --------------------------------------------------------------------
async def _test() -> None:
    """Smoke-тест: проверяем вызовы helper-ов без Telegram API."""
    dummy = {"id": 1, "game_name": "Quest", "event_datetime": None}
    assert _extract_uid("Alex|123") == 123
    assert _extract_uid("nope") is None
    assert isinstance(_profile_text({}, []), str)
    assert isinstance(_details_keyboard(1, False), InlineKeyboardMarkup)
    print("handlers.profile ✅ tests passed")


if __name__ == "__main__":
    import asyncio, logging as _l
    _l.basicConfig(level=_l.DEBUG)
    asyncio.run(_test())
