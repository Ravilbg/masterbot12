"""handlers/stats.py — вывод 30-дневной статистики игр
─────────────────────────────────────────────────────────────────────────────
Кнопка «📈 Статистика игр» (или команда /stats) показывает,
сколько игр провёл каждый ведущий за последние 30 суток.

• show_stats_handler()  — основной роутер.
• _human_name()         — превращает user_id → «Имя Ф.».
• ACCESS: settings.ACCESS["poll"]  (руководитель / администратор).
"""

from __future__ import annotations

# ███ [1.0] IMPORTS
# --------------------------------------------------------------------
import logging
from typing import Dict

from aiogram import Bot, Router, types
from aiogram.filters import Command

from core.config import settings
from core.db import get_user_info
from services.stats import games_per_leader

logger = logging.getLogger(__name__)
router = Router()


# ███ [1.1] HELPERS
# --------------------------------------------------------------------
async def _human_name(user_id: int) -> str:
    """
    Формирует «Имя Ф.» (или raw uid, если данных нет).
    """
    info = await get_user_info(user_id)
    if not info:
        return str(user_id)
    first = info["first_name"] or "БезИмени"
    last_i = info["last_name_initial"]
    return f"{first} {last_i}."


# ███ [2.0] HANDLER
# --------------------------------------------------------------------
@router.message(
    Command("stats"),
)
@router.message(lambda m: m.text and m.text.strip() == "📈 Статистика игр")
async def show_stats_handler(message: types.Message) -> None:
    """
    Отправляет список «Ведущий — кол-во игр» за 30 дней.
    Доступ — роли из settings.ACCESS["poll"].
    """
    uid = message.from_user.id
    ui = await get_user_info(uid) or {}

    # —–– проверка доступа
    if ui.get("role") not in settings.ACCESS["poll"]:
        await message.answer("⛔ Нет доступа к статистике.")
        return

    stats: Dict[int, int] = await games_per_leader(30)
    if not stats:
        await message.answer("ℹ️ За последние 30 дней игр не найдено.")
        return

    # сортировка по убыванию количества
    rows = sorted(stats.items(), key=lambda kv: (-kv[1], kv[0]))
    lines = ["📈 *Статистика за 30 дней:*"]
    for user_id, cnt in rows:
        name = await _human_name(user_id)
        lines.append(f"• {name} — *{cnt}*")

    txt = "\n".join(lines)
    kb = await message.bot.get_current().get_my_commands(scope=None)  # placeholder: no keyboard
    try:
        await message.answer(txt, parse_mode="Markdown", reply_markup=None)
    except Exception as exc:
        logger.error("[stats] Failed to send stats: %s", exc)
        await message.answer("⚠️ Не удалось отправить статистику, попробуйте позже.")


# ███ [3.0] TESTS
# --------------------------------------------------------------------
async def _test():
    """
    Псевдо-тест: games_per_leader() возвращает dict; _human_name наполняет строку.
    """
    s = await games_per_leader(1)
    assert isinstance(s, dict)
    name = await _human_name(0)
    assert isinstance(name, str)
    print("handlers.stats tests passed")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_test())
