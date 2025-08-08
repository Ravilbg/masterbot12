"""handlers/stats.py — Статистика по команде / по играм
─────────────────────────────────────────────────────────────────────────────
Версия 2.0 · 2025-08-07

Новая функциональность «📈 Статистика по команде» для роли *poll*:
1. Кнопка или команда выводит **inline‑список пользователей**.
2. Выбор пользователя — подробная карточка:
   • количество игр за 30 дней;
   • текущий приоритет назначения;
   • кнопки «🔼 Приоритет» / «🔽 Приоритет».
3. Везде работают пылесос и «← Назад».
Старая «📈 Статистика игр» (глобальный список) сохранена командой /stats.
"""

from __future__ import annotations

# ███ [1] IMPORTS
# --------------------------------------------------------------------
import logging
from typing import Dict, List

from aiogram import Bot, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config import settings
from core.db import get_user_info, get_all_leader_uids
from core.state import state
from core.utils import delete_previous_private_messages
from services.stats import games_per_leader

logger = logging.getLogger(__name__)
router = Router()

# ── callback‑префиксы ───────────────────────────────────────────────
USER_PREFIX   = "teamstat_user_"
PRIO_UP       = "teamstat_up_"
PRIO_DOWN     = "teamstat_down_"
BACK_TO_LIST  = "teamstat_back"

# ════════════════════════════════════════════════════════════════════
# [2] HELPERS
# ════════════════════════════════════════════════════════════════════

async def _human_name(uid: int) -> str:
    info = await get_user_info(uid) or {}
    first = info.get("first_name") or "БезИмени"
    last_i = info.get("last_name_initial", "")
    return f"{first} {last_i}."


def _get_priority(uid: int) -> int:
    """Возвращает текущий приоритет (0..5). Placeholder → state.priorities."""
    return state.priorities.get(uid, 0)  # type: ignore[attr-defined]


def _set_priority(uid: int, delta: int) -> None:
    state.priorities[uid] = max(0, min(5, _get_priority(uid) + delta))  # type: ignore[attr-defined]


async def _team_list_keyboard(uids: List[int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for uid in sorted(uids):
        name = await _human_name(uid)
        kb.button(text=name, callback_data=f"{USER_PREFIX}{uid}")
    kb.adjust(1)
    return kb.as_markup()


async def _user_detail(uid: int) -> str:
    games_cnt = (await games_per_leader(30)).get(uid, 0)
    prio = _get_priority(uid)
    name = await _human_name(uid)
    return (
        f"👤 *{name}*\n"
        f"🎮 *Игр за 30 дней*: {games_cnt}\n"
        f"⭐️ *Приоритет*: {prio}"
    )


def _user_detail_keyboard(uid: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔼 Повысить", callback_data=f"{PRIO_UP}{uid}")
    kb.button(text="🔽 Понизить",  callback_data=f"{PRIO_DOWN}{uid}")
    kb.button(text="← Назад", callback_data=BACK_TO_LIST)
    kb.adjust(1)
    return kb.as_markup()

# ════════════════════════════════════════════════════════════════════
# [3] HANDLERS: СТАТИСТИКА ПО КОМАНДЕ
# ════════════════════════════════════════════════════════════════════

@router.message(lambda m: m.text and m.text.strip() == "📈 Статистика по команде")
async def team_stats_handler(message: types.Message) -> None:
    uid = message.from_user.id
    ui = await get_user_info(uid) or {}
    if ui.get("role") not in settings.ACCESS["poll"]:
        await message.answer("⛔ Нет доступа.")
        return

    uids = await get_all_leader_uids()
    if not uids:
        await message.answer("ℹ️ Нет данных по команде.")
        return

    await delete_previous_private_messages(uid)
    kb = await _team_list_keyboard(uids)
    sent = await message.answer("📈 *Команда:*", parse_mode="Markdown", reply_markup=kb)
    state.last_user_messages[uid] = [sent]
    try:
        await message.delete()
    except Exception:
        pass


@router.callback_query(lambda c: c.data.startswith(USER_PREFIX))
async def user_detail_handler(callback: types.CallbackQuery) -> None:
    uid = callback.from_user.id
    target = int(callback.data.split("_")[-1])

    await delete_previous_private_messages(uid)
    txt = await _user_detail(target)
    kb = _user_detail_keyboard(target)
    sent = await Bot.get_current().send_message(uid, txt, parse_mode="Markdown", reply_markup=kb)
    state.last_user_messages[uid] = [sent]
    await callback.answer()


@router.callback_query(lambda c: c.data in {BACK_TO_LIST})
async def back_to_list(callback: types.CallbackQuery) -> None:
    uid = callback.from_user.id
    uids = await get_all_leader_uids()
    kb = await _team_list_keyboard(uids)
    await delete_previous_private_messages(uid)
    sent = await Bot.get_current().send_message(uid, "📈 *Команда:*", parse_mode="Markdown", reply_markup=kb)
    state.last_user_messages[uid] = [sent]
    await callback.answer()


@router.callback_query(lambda c: c.data.startswith(PRIO_UP) or c.data.startswith(PRIO_DOWN))
async def priority_change(callback: types.CallbackQuery) -> None:
    uid = callback.from_user.id
    data = callback.data
    target = int(data.split("_")[-1])
    delta = 1 if data.startswith(PRIO_UP) else -1
    _set_priority(target, delta)

    # Обновляем карточку
    txt = await _user_detail(target)
    kb = _user_detail_keyboard(target)
    await callback.message.edit_text(txt, parse_mode="Markdown", reply_markup=kb)
    await callback.answer("✅ Приоритет обновлён")

# ════════════════════════════════════════════════════════════════════
# [4] СТАРАЯ «СТАТИСТИКА ИГР» (ГЛОБАЛЬНЫЙ СПИСОК)
# ════════════════════════════════════════════════════════════════════

@router.message(Command("stats"))
@router.message(lambda m: m.text and m.text.strip() == "📈 Статистика игр")
async def show_stats_handler(message: types.Message) -> None:
    uid = message.from_user.id
    ui = await get_user_info(uid) or {}
    if ui.get("role") not in settings.ACCESS["poll"]:
        await message.answer("⛔ Нет доступа к статистике.")
        return

    stats: Dict[int, int] = await games_per_leader(30)
    if not stats:
        await message.answer("ℹ️ За последние 30 дней игр не найдено.")
        return

    lines = ["📈 *Статистика за 30 дней:*"]
    for user_id, cnt in sorted(stats.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"• {(await _human_name(user_id))} — *{cnt}*")
    await message.answer("\n".join(lines), parse_mode="Markdown")

# ════════════════════════════════════════════════════════════════════
# [5] SELF‑TEST (smoke)
# ════════════════════════════════════════════════════════════════════

async def _test():
    assert isinstance(await games_per_leader(1), dict)
    print("handlers.stats ✅ tests passed")


if __name__ == "__main__":
    import asyncio, logging as _l
    _l.basicConfig(level=_l.DEBUG)
    asyncio.run(_test())
