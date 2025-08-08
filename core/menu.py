"""core/menu.py — генерация reply-меню MasterBot
────────────────────────────────────────────────────────────────────────
Версия 15.6 · 2025-08-07

• Ведущие/админы: 5 кнопок (игры, новые, статистика, обучение, бонусы).
• Кнопки цикла опроса видит только роль «руководитель».
• _add() исключает дубли; раскладка — 2 кнопки в строке.
"""

from __future__ import annotations

import logging
from typing import Set

from aiogram.types import ReplyKeyboardMarkup
from aiogram.utils.keyboard import ReplyKeyboardBuilder

from core.config import settings
from core.db import get_user_info
from core.state import state

logger = logging.getLogger(__name__)
__all__ = ["get_main_menu"]

# ── роли, которым доступен цикл опроса ──────────────────────────────
_POLL_MASTER_ROLES: Set[str] = {"руководитель"}


async def get_main_menu(user_id: int) -> ReplyKeyboardMarkup | None:
    """Строит главное меню в ЛС с учётом роли и статуса цикла."""
    ui = await get_user_info(user_id) or {}
    role: str = ui.get("role", "")
    kb = ReplyKeyboardBuilder()
    seen: Set[str] = set()

    def _add(text: str) -> None:
        if text not in seen:
            kb.button(text=text)
            seen.add(text)

    # ── Ведущие / администраторы  (ACCESS["games"]) ────────────────
    if role in settings.ACCESS.get("games", []):
        for txt in (
            "🎲 Мои игры",
            "📅 Новые игры",
            "📈 Статистика",
            "📚 Обучение",
            "🎁 Бонусы",
        ):
            _add(txt)

    # ── Руководитель (цикл опроса) ─────────────────────────────────
    if role in _POLL_MASTER_ROLES:
        # Игры, распределение
        for txt in ("🎲 Мои игры", "📅 Новые игры", "✅ Распределённые игры"):
            _add(txt)

        # Кнопки цикла
        if state.coordination_cycle_active:
            for txt in (
                "📊 Отчёт по опросу",
                "✉️ Рассылка уведомлений",
                "📈 Статистика игр",
                "⏹️ Завершить цикл",
            ):
                _add(txt)
        else:
            _add("📋 Создать опрос")

        # Дополнительные
        _add("📈 Статистика по команде")
        _add("📚 База знаний")

    if not seen:
        return None

    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)


# ════════════════════════════════════════════════════════════════════
# TESTS
# ════════════════════════════════════════════════════════════════════
async def _test() -> None:
    """Проверяем набор кнопок и отсутствие дублей."""
    orig = get_user_info

    async def fake(role_name: str):
        return {"role": role_name}

    import core.db as _db
    # games-роль (админ)
    _db.get_user_info = lambda _uid: fake("администратор")  # type: ignore
    g = await get_main_menu(1)
    assert g, "games-меню не создано"
    assert all(
        t not in {"📊 Отчёт по опросу", "📋 Создать опрос"}  # цикл
        for row in g.keyboard for t in [btn.text for btn in row]
    )
    # poll-роль (руководитель)
    _db.get_user_info = lambda _uid: fake("руководитель")  # type: ignore
    state.coordination_cycle_active = True
    p = await get_main_menu(2)
    texts = [btn.text for row in p.keyboard for btn in row]
    assert "📊 Отчёт по опросу" in texts, "нет кнопки цикла"
    assert len(texts) == len(set(texts)), "дубли кнопок"

    print("core/menu.py tests passed ✓")
    _db.get_user_info = orig  # restore


if __name__ == "__main__":
    import asyncio
    asyncio.run(_test())
