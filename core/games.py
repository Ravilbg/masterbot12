# handlers/games.py
# ─────────────────────────────────────────────────────────────────────────────
"""handlers/games.py — список и детализация игр (v12.93-cycle · 2025-07-22)

• «📅 Новые игры» / «✅ Распределённые игры»
• inline-список до 20 игр, детализация, «Назад»
• per-user async-lock, vacuum старых сообщений
• helper _delete_trigger (используется в polls_lifecycle.py)
"""

from __future__ import annotations

# ███ [1.0] IMPORTS
# --------------------------------------------------------------------
import contextlib
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import List

from aiogram import Bot, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pytz import timezone

from core.config import settings
from core.db import get_user_info
from core.state import state
from core.utils import delete_previous_private_messages
from services.amocrm import get_amocrm_deals
from services.gsheets import get_user_status_from_svetofor  # noqa: F401

logger = logging.getLogger(__name__)
router = Router()
MSK_TZ = timezone("Europe/Moscow")

# ███ [2.0] ASYNC-LOCK helper
# --------------------------------------------------------------------
@asynccontextmanager
async def user_lock(uid: int):
    """Per-user глобальная блокировка: защищает от гонок."""
    lock = state.lock_for(uid)
    await lock.acquire()
    try:
        yield
    finally:
        lock.release()

# ███ [3.0] UTILS
# --------------------------------------------------------------------
async def _delete_trigger(msg: types.Message) -> None:
    try:
        await msg.delete()
    except Exception:
        state.messages_to_delete.setdefault(msg.from_user.id, []).append(msg.message_id)


def _truncate(text: str, limit: int = 100) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _refresh_menu(user_id: int) -> None:
    from core.menu import get_main_menu

    try:
        kb = await get_main_menu(user_id)
        if not kb:
            return

        bot = Bot.get_current()
        old_id = getattr(state, "menu_message_id", None)

        if old_id:
            with contextlib.suppress(Exception):
                await bot.delete_message(chat_id=user_id, message_id=old_id)
            state.menu_message_id = None

        for txt in ("\u2060", "."):
            try:
                sent = await bot.send_message(user_id, txt, reply_markup=kb)
                state.menu_message_id = sent.message_id
                return
            except Exception:
                continue
    except Exception as e:
        logger.error("[games] _refresh_menu: %s", e, exc_info=True)

# ███ [4.0] CORE LIST/DETAILS LOGIC
# --------------------------------------------------------------------
async def show_games(
    message: types.Message,
    user_id: int,
    status_ids: List[str],
    title: str,
    only_unassigned: bool | None = None,
) -> None:
    """Формирует inline-список игр и сохраняет его в state.games_by_user."""
    bot = Bot.get_current()

    async with user_lock(user_id):
        await delete_previous_private_messages(user_id)

        deals = await get_amocrm_deals()          # вызов БЕЗ аргументов
        if not deals:
            logger.error("[games] AmoCRM deals fetch failed for %d", user_id)
            await message.answer(f"{title}\n⚠️ Не удалось загрузить данные игр.")
            return

        now = datetime.now(tz=MSK_TZ)
        if only_unassigned is None:
            only_unassigned = status_ids == settings.NEW_GAMES_STATUS_IDS

        filtered = [
            d for d in deals
            if d["status_id"] in status_ids
            and d["event_datetime"] >= now
            and (not only_unassigned or not d["team_leads"])
        ]

        if not filtered:
            await message.answer(f"{title}\n😔 Подходящих игр нет.")
            return

        filtered.sort(key=lambda d: d["event_datetime"])
        filtered = filtered[:20]

        kb = InlineKeyboardBuilder()
        for d in filtered:
            date = d["event_datetime"].strftime("%d.%m")
            extra = d.get("package") or d.get("extra_services") or ""
            extra = f" · {extra}" if extra and extra != "Не указано" else ""
            kb.button(
                text=f"🎉 {_truncate(d['name'])} — {date}{extra}",
                callback_data=f"game_details_{d['id']}",
            )
        kb.adjust(1)

        sent = await bot.send_message(
            user_id, f"{title}\nВыберите игру:", reply_markup=kb.as_markup()
        )
        state.last_user_messages[user_id] = [sent]
        state.games_by_user[user_id] = filtered

# ███ [5.0] HANDLERS
# --------------------------------------------------------------------
@router.message(Command("new_games"))
@router.message(lambda m: m.text and m.text.strip() == "📅 Новые игры")
async def new_games_handler(message: types.Message) -> None:
    uid = message.from_user.id
    ui = await get_user_info(uid)
    if ui and ui["role"] in settings.ACCESS["games"]:
        await show_games(
            message,
            uid,
            settings.NEW_GAMES_STATUS_IDS,
            "📅 Новые игры:",
            True,
        )
    else:
        await message.answer("⛔ Нет доступа.")
    await _delete_trigger(message)



@router.message(Command("assigned_games"))
@router.message(lambda m: m.text and m.text.strip() == "✅ Распределённые игры")
async def assigned_games_handler(message: types.Message) -> None:
    uid = message.from_user.id
    ui = await get_user_info(uid)
    if ui and ui["role"] in settings.ACCESS["games"]:
        await show_games(
            message,
            uid,
            [settings.SUCCESSFUL_STATUS_ID],
            "✅ Распределённые игры:",
        )
    else:
        await message.answer("⛔ Нет доступа.")
    await _delete_trigger(message)


@router.callback_query(lambda c: c.data.startswith("game_details_"))
async def game_details_handler(callback: types.CallbackQuery) -> None:
    user_id = callback.from_user.id
    deal_id = int(callback.data.rsplit("_", 1)[-1])
    bot = Bot.get_current()

    try:
        await delete_previous_private_messages(user_id)

        deal = next(
            (d for d in state.games_by_user.get(user_id, []) if d["id"] == deal_id),
            None,
        )
        if not deal:
            await callback.answer("⚠️ Игра не найдена.", show_alert=True)
            return

        date_part = deal["event_datetime"].strftime("%d.%m.%Y")
        time_part = deal.get("event_time", "—")
        text = "\n".join(
            [
                f"🎮 *Игра*: {deal.get('game_name', '—')}",
                f"📦 *Пакет*: {deal.get('package', '—')}",
                f"🔖 *Статус*: {deal.get('status', '—')}",
                f"📅 *Дата*: {date_part}",
                f"🕒 *Время*: {time_part}",
                f"👥 *Игроки*: {deal.get('players', '—')}",
                f"⚠️ *Возраст*: {deal.get('age', '—')}",
                f"➕ *Доп. услуги*: {deal.get('extra_services', '—')}",
                f"💬 *Комментарий*: {deal.get('comment', '—')}",
                f"💸 *Предоплата*: {deal.get('prepayment', 0)} ₽",
                f"🧑‍🤝‍🧑 *Ведущие*: "
                f"{', '.join(l['name'] for l in deal.get('team_leads', [])) or '—'}",
                f"📷 *Фотограф*: {deal.get('photographer', '—')}",
                f"💰 *Бюджет*: {deal.get('total_budget', 0)} ₽",
                f"🧮 *К расчёту*: {deal.get('to_calculate', 0)} ₽",
            ]
        )

        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад к списку", callback_data="back_to_games_list")
        kb.adjust(1)

        sent = await bot.send_message(
            user_id, text, parse_mode="Markdown", reply_markup=kb.as_markup()
        )
        state.last_user_messages[user_id] = [sent]
        await callback.answer()
    except Exception as e:
        logger.error("[games] details: %s", e, exc_info=True)
        await callback.answer("⚠️ Ошибка при отображении деталей.", show_alert=True)


@router.callback_query(lambda c: c.data == "back_to_games_list")
async def back_to_games_list_handler(callback: types.CallbackQuery) -> None:
    user_id = callback.from_user.id
    try:
        await delete_previous_private_messages(user_id)

        games = state.games_by_user.get(user_id)
        if not games:
            await callback.answer("😔 Список игр недоступен.", show_alert=True)
            return

        kb = InlineKeyboardBuilder()
        for d in games:
            btn_text = f"🎉 {_truncate(d['name'])} — {d.get('event_time', '—')}"
            kb.button(text=btn_text, callback_data=f"game_details_{d['id']}")
        kb.adjust(1)

        sent = await callback.message.answer(
            "📋 Список игр:\nВыберите игру:", reply_markup=kb.as_markup()
        )
        state.last_user_messages[user_id] = [sent]
        await callback.answer()
    except Exception as e:
        logger.error("[games] back_to_list: %s", e, exc_info=True)
        await callback.answer("⚠️ Не удалось вернуться к списку.", show_alert=True)
