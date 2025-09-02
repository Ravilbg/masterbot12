# handlers/stats.py — Статистика по команде / по играм 
# ─────────────────────────────────────────────────────────────────────────────
"""
Версия 2.1 · 2025-08-16

Что нового
• Пылесос ЛС перед показом любых экранов статистики (команда/игры).
• Любой экранный месседж в ЛС сохраняется в state.last_user_messages[uid].
• Безопасная инициализация state.priorities = {} (не падаем, если нет атрибута).
"""

from __future__ import annotations

# ███ [1] IMPORTS
# --------------------------------------------------------------------
import logging
import contextlib
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

# ── init safe fields ────────────────────────────────────────────────
# state.priorities может отсутствовать в ранних сборках → создаём
try:
    if not isinstance(getattr(state, "priorities", None), dict):
        state.priorities = {}
except Exception:
    state.priorities = {}

# ── callback-префиксы ───────────────────────────────────────────────
USER_PREFIX   = "teamstat_user_"
PRIO_UP       = "teamstat_up_"
PRIO_DOWN     = "teamstat_down_"
BACK_TO_LIST  = "teamstat_back"

# ════════════════════════════════════════════════════════════════════
# [1.5] VACUUM helper (совместим с legacy/new сигнатурами)
# ════════════════════════════════════════════════════════════════════
async def _vacuum(uid: int) -> None:
    """
    Удаляет старые личные сообщения пользователя. Пробует новую и legacy сигнатуры.
    """
    bot = Bot.get_current()
    # Новая сигнатура: delete_previous_private_messages(uid, keep=[])
    try:
        await delete_previous_private_messages(uid, keep=[])
        return
    except TypeError:
        pass
    except Exception:
        pass
    # Legacy сигнатура: delete_previous_private_messages(bot, uid, keep=[])
    with contextlib.suppress(Exception):
        await delete_previous_private_messages(bot, uid, keep=[])

# ════════════════════════════════════════════════════════════════════
# [2] HELPERS
# ════════════════════════════════════════════════════════════════════
async def _human_name(uid: int) -> str:
    info = await get_user_info(uid) or {}
    first = (info.get("first_name") or "БезИмени").strip()
    last_i = (info.get("last_name_initial") or "").strip()
    dot = "." if last_i and not last_i.endswith(".") else ""
    return f"{first} {last_i}{dot}".strip()

def _get_priority(uid: int) -> int:
    """Возвращает текущий приоритет (0..5)."""
    try:
        return int((getattr(state, "priorities", {}) or {}).get(uid, 0))
    except Exception:
        return 0

def _set_priority(uid: int, delta: int) -> None:
    state.__dict__.setdefault("priorities", {})
    cur = _get_priority(uid)
    state.priorities[uid] = max(0, min(5, cur + delta))

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
@router.message(lambda m: (m.text or "").strip() == "📈 Статистика по команде")
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

    await _vacuum(uid)
    kb = await _team_list_keyboard(uids)
    sent = await message.answer("📈 *Команда:*", parse_mode="Markdown", reply_markup=kb)
    state.last_user_messages[uid] = [sent]

    with contextlib.suppress(Exception):
        await message.delete()

@router.callback_query(lambda c: (c.data or "").startswith(USER_PREFIX))
async def user_detail_handler(callback: types.CallbackQuery) -> None:
    uid = callback.from_user.id
    target = int((callback.data or "").split("_")[-1])

    await _vacuum(uid)
    txt = await _user_detail(target)
    kb = _user_detail_keyboard(target)
    sent = await Bot.get_current().send_message(uid, txt, parse_mode="Markdown", reply_markup=kb)
    state.last_user_messages[uid] = [sent]
    await callback.answer()

@router.callback_query(lambda c: (c.data or "") == BACK_TO_LIST)
async def back_to_list(callback: types.CallbackQuery) -> None:
    uid = callback.from_user.id
    uids = await get_all_leader_uids()

    await _vacuum(uid)
    kb = await _team_list_keyboard(uids or [])
    sent = await Bot.get_current().send_message(uid, "📈 *Команда:*", parse_mode="Markdown", reply_markup=kb)
    state.last_user_messages[uid] = [sent]
    await callback.answer()

@router.callback_query(lambda c: (c.data or "").startswith(PRIO_UP) or (c.data or "").startswith(PRIO_DOWN))
async def priority_change(callback: types.CallbackQuery) -> None:
    data = callback.data or ""
    target = int(data.split("_")[-1])
    delta = 1 if data.startswith(PRIO_UP) else -1
    _set_priority(target, delta)

    # Обновляем карточку на месте (ЛС не «захламляем» дополнительными сообщениями)
    txt = await _user_detail(target)
    kb = _user_detail_keyboard(target)
    await callback.message.edit_text(txt, parse_mode="Markdown", reply_markup=kb)
    await callback.answer("✅ Приоритет обновлён")

# ════════════════════════════════════════════════════════════════════
# [4] СТАРАЯ «СТАТИСТИКА ИГР» (ГЛОБАЛЬНЫЙ СПИСОК)
# ════════════════════════════════════════════════════════════════════
@router.message(Command("stats"))
@router.message(lambda m: (m.text or "").strip() == "📈 Статистика игр")
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

    # ЛС-экран: пылесос + сохранение сообщения
    await _vacuum(uid)
    sent = await message.answer("\n".join(lines), parse_mode="Markdown")
    state.last_user_messages[uid] = [sent]

    with contextlib.suppress(Exception):
        await message.delete()

# ════════════════════════════════════════════════════════════════════
# [5] SELF-TEST (smoke)
# ════════════════════════════════════════════════════════════════════
async def _test():
    assert isinstance(await games_per_leader(1), dict)
    print("handlers.stats ✅ tests passed")

if __name__ == "__main__":
    import asyncio, logging as _l
    _l.basicConfig(level=_l.DEBUG)
    asyncio.run(_test())
