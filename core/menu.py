"""core/menu.py — генерация reply-меню MasterBot
────────────────────────────────────────────────────────────────────────
Версия 15.8 · 2025-08-18

• Меню без изменений.
• SSOT: хранение сообщения главного меню как мапы {uid -> message_id}.
• Хелперы: remember_menu_message / get_menu_message_id / forget_menu_message.
• Фиксы Pylance: аккуратные типы и безопасные обращения к state.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Set, Dict

from aiogram.types import ReplyKeyboardMarkup
from aiogram.utils.keyboard import ReplyKeyboardBuilder

from core.config import settings
from core.db import get_user_info
from core.state import state

logger = logging.getLogger(__name__)
__all__ = ["get_main_menu", "remember_menu_message", "get_menu_message_id", "forget_menu_message"]

# ── безопасная инициализация мапы сообщений меню ────────────────────
try:
    # если где-то остался старый int — просто затираем на пустую мапу
    if not isinstance(getattr(state, "menu_message_id", None), dict):
        state.menu_message_id = {}  # type: ignore[assignment]
except Exception:
    state.menu_message_id = {}  # type: ignore[assignment]

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

    # ── Ведущие / администраторы (ACCESS["games"]) ──────────────────
    try:
        games_access = set(getattr(settings, "ACCESS", {}).get("games", []) or [])
    except Exception:
        games_access = set()
    if role in games_access:
        for txt in ("🎲 Мои игры", "📅 Новые игры", "📈 Статистика", "📚 Обучение", "🎁 Бонусы"):
            _add(txt)

    # ── Руководитель (цикл опроса) ───────────────────────────────────
    if role in _POLL_MASTER_ROLES:
        # Игры, распределение
        for txt in ("🎲 Мои игры", "📅 Новые игры", "✅ Распределённые игры"):
            _add(txt)

        # Кнопки цикла
        if bool(getattr(state, "coordination_cycle_active", False)):
            for txt in ("📊 Отчёт по опросу", "✉️ Рассылка уведомлений", "📈 Статистика игр", "⏹️ Завершить цикл"):
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
# ХЕЛПЕРЫ ДЛЯ ХРАНЕНИЯ СООБЩЕНИЯ МЕНЮ (используйте в местах, где отправляете меню)
# ════════════════════════════════════════════════════════════════════
def remember_menu_message(uid: int, message_or_id: Any) -> None:
    """
    Запоминает message_id главного меню для пользователя.
    Вызывайте после отправки меню:
        sent = await bot.send_message(uid, "...", reply_markup=await get_main_menu(uid))
        remember_menu_message(uid, sent)
    """
    try:
        mid = int(message_or_id)  # если уже int
    except Exception:
        mid = int(getattr(message_or_id, "message_id", 0) or 0)
    if not mid:
        return
    try:
        # гарантируем, что хранилище — мапа
        if not isinstance(getattr(state, "menu_message_id", None), dict):
            state.menu_message_id = {}  # type: ignore[assignment]
        cast_map: Dict[int, int] = getattr(state, "menu_message_id")  # type: ignore[assignment]
        cast_map[uid] = mid
    except Exception:
        # на всякий случай восстановим структуру
        state.menu_message_id = {uid: mid}  # type: ignore[assignment]


def get_menu_message_id(uid: int) -> Optional[int]:
    """Возвращает сохранённый message_id меню для пользователя (или None)."""
    try:
        val = (getattr(state, "menu_message_id", {}) or {}).get(uid)
        return int(val) if val is not None else None
    except Exception:
        return None


def forget_menu_message(uid: int) -> None:
    """Удаляет запись о сообщении меню пользователя (для пылесоса/перерисовки)."""
    try:
        (getattr(state, "menu_message_id", {}) or {}).pop(uid, None)
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════
# TESTS
# ════════════════════════════════════════════════════════════════════
async def _test() -> None:
    """Проверяем набор кнопок и отсутствие дублей + мапу меню."""
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

    # мапа меню
    forget_menu_message(2)  # чистый старт
    remember_menu_message(2, 555)
    assert get_menu_message_id(2) == 555
    forget_menu_message(2)
    assert get_menu_message_id(2) is None

    print("core/menu.py tests passed ✓")
    _db.get_user_info = orig  # restore


if __name__ == "__main__":
    import asyncio
    asyncio.run(_test())
