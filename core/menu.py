"""core/menu.py — генерация reply-меню MasterBot
────────────────────────────────────────────────────────────────────────
Версия 15.9 · 2025-09-02

• NEW: send_root_menu_singleton — показать корневое меню как единственный блок
       (жёсткий пылесос внутри), записать state.menu_message_id и опционально закрепить.
• SSOT: хранение сообщения главного меню как мапы {uid -> message_id}.
• Хелперы: remember_menu_message / get_menu_message_id / forget_menu_message.
• Фиксы Pylance: убраны дублирующиеся импорты, аккуратные типы и безопасные обращения к state.
"""

from __future__ import annotations

import logging
import contextlib
from typing import Any, Optional, Set, Dict

from aiogram import Bot
from aiogram.types import ReplyKeyboardMarkup
from aiogram.utils.keyboard import ReplyKeyboardBuilder

from core.config import settings
from core.db import get_user_info
from core.state import state
from core.utils import dm_singleton_send

logger = logging.getLogger(__name__)
__all__ = [
    "get_main_menu",
    "send_root_menu_singleton",
    "remember_menu_message",
    "get_menu_message_id",
    "forget_menu_message",
]

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


# ────────────────────────────────────────────────────────────────────
# ПОКАЗ КОРНЕВОГО МЕНЮ КАК ЕДИНСТВЕННОГО БЛОКА (жёсткий пылесос внутри)
# ────────────────────────────────────────────────────────────────────
async def send_root_menu_singleton(uid: int, kb: ReplyKeyboardMarkup, *, pin: bool = True) -> int:
    """
    Рендерит главное меню в ЛС:
    • удаляет всё выше (жёсткий пылесос внутри dm_singleton_send),
    • отправляет один «корневой» блок с невидимым текстом,
    • сохраняет message_id в state.menu_message_id[uid],
    • (опц.) закрепляет сообщение в ЛС.
    Возвращает message_id меню.
    """
    # Невидимый символ, чтобы сообщение было «пустым», а кнопки — видимыми
    msg = await dm_singleton_send(int(uid), "\u2060", reply_markup=kb)

    # запоминаем ID меню (это нужно вакууму, чтобы не сносить актуальный корень)
    d = getattr(state, "menu_message_id", None)
    if not isinstance(d, dict):
        d = {}
        setattr(state, "menu_message_id", d)
    d[int(uid)] = int(msg.message_id)

    if pin:
        with contextlib.suppress(Exception):
            bot = Bot.get_current()
            await bot.pin_chat_message(int(uid), int(msg.message_id), disable_notification=True)
            logger.info("[guide] pinned new menu %s in chat %s", msg.message_id, uid)

    return int(msg.message_id)


# ════════════════════════════════════════════════════════════════════
# ХЕЛПЕРЫ ДЛЯ ХРАНЕНИЯ СООБЩЕНИЯ МЕНЮ (совместимость)
# ════════════════════════════════════════════════════════════════════
def remember_menu_message(uid: int, message_or_id: Any) -> None:
    """
    Запоминает message_id главного меню для пользователя.
    Совместимость с местами, где меню отправлялось напрямую.
    """
    try:
        mid = int(message_or_id)  # если уже int
    except Exception:
        mid = int(getattr(message_or_id, "message_id", 0) or 0)
    if not mid:
        return
    try:
        if not isinstance(getattr(state, "menu_message_id", None), dict):
            state.menu_message_id = {}  # type: ignore[assignment]
        cast_map: Dict[int, int] = getattr(state, "menu_message_id")  # type: ignore[assignment]
        cast_map[int(uid)] = int(mid)
    except Exception:
        state.menu_message_id = {int(uid): int(mid)}  # type: ignore[assignment]


def get_menu_message_id(uid: int) -> Optional[int]:
    """Возвращает сохранённый message_id меню для пользователя (или None)."""
    try:
        val = (getattr(state, "menu_message_id", {}) or {}).get(int(uid))
        return int(val) if val is not None else None
    except Exception:
        return None


def forget_menu_message(uid: int) -> None:
    """Удаляет запись о сообщении меню пользователя (для пылесоса/перерисовки)."""
    try:
        (getattr(state, "menu_message_id", {}) or {}).pop(int(uid), None)
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

# История изменений:
#   2025-08-18 — v15.8: SSOT-хранилище сообщения главного меню; хелперы remember/get/forget.
#   2025-09-02 — v15.9: добавлен send_root_menu_singleton (жёсткий пылесос, запись menu_message_id, pin);
#                       Pylance-фиксы, убраны дубли импорта.
