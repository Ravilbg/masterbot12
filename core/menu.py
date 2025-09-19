"""Main menu helpers for MasterBot (reconstructed).
This module rebuilds the lost menu logic with essential functionality.
"""
from __future__ import annotations

import contextlib
import logging
from typing import Any, Dict, List, Optional, Set

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

# Ensure state keeps map of pinned menu messages
try:
    if not isinstance(getattr(state, "menu_message_id", None), dict):
        state.menu_message_id = {}  # type: ignore[assignment]
except Exception:  # pragma: no cover - fallback for tests
    state.menu_message_id = {}  # type: ignore[assignment]

_STAR_BUTTON = "⭐ Мой рейтинг"
_STAR_ADMIN_BUTTON = "⭐ Рейтинг команды"
_ZERO_WIDTH = "\u2060"

# Rough defaults for known reply buttons used across handlers
_GAMES_BUTTONS = [
    "🎲 Мои игры",
    "📋 Создать опрос",
    "📚 База знаний",
]

_POLL_MASTER_EXTRA: List[str] = []


def _add_unique(builder: ReplyKeyboardBuilder, seen: Set[str], text: str) -> None:
    if text not in seen:
        builder.button(text=text)
        seen.add(text)


async def get_main_menu(user_id: int) -> Optional[ReplyKeyboardMarkup]:
    """Build reply keyboard for the user based on their role."""
    ui = await get_user_info(user_id) or {}
    role: str = str(ui.get("role") or "")

    builder = ReplyKeyboardBuilder()
    seen: Set[str] = set()

    try:
        games_access = set(getattr(settings, "ACCESS", {}).get("games", []) or [])
    except Exception:  # pragma: no cover
        games_access = set()

    try:
        poll_roles = set(getattr(settings, "ACCESS", {}).get("poll", []) or [])
    except Exception:  # pragma: no cover
        poll_roles = set()

    buttons = list(_GAMES_BUTTONS)
    if (role in poll_roles or role in {"менеджер", "администратор"}) and getattr(state, "coordination_cycle_active", False):
        buttons = [("📊 Отчёт по опросу" if caption == "📋 Создать опрос" else caption) for caption in buttons]

    if role in games_access or not games_access:
        for caption in buttons:
            _add_unique(builder, seen, caption)

    # Generic rating button for everyone
    _add_unique(builder, seen, _STAR_BUTTON)

    if role in poll_roles or role in {"менеджер", "администратор"}:
        for caption in _POLL_MASTER_EXTRA:
            _add_unique(builder, seen, caption)
        _add_unique(builder, seen, _STAR_ADMIN_BUTTON)

    if not seen:
        return None

    builder.adjust(2)
    return builder.as_markup(resize_keyboard=True)


async def send_root_menu_singleton(uid: int, kb: ReplyKeyboardMarkup, *, pin: bool = True) -> int:
    """Send (or replace) root menu message using DM singleton helper."""
    message = await dm_singleton_send(int(uid), _ZERO_WIDTH, reply_markup=kb)

    prev = get_menu_message_id(uid)
    if isinstance(prev, int) and prev > 0 and prev != message.message_id:
        with contextlib.suppress(Exception):
            bot = Bot.get_current()
            await bot.delete_message(int(uid), int(prev))

    remember_menu_message(uid, message.message_id)

    if pin:
        with contextlib.suppress(Exception):
            bot = Bot.get_current()
            await bot.pin_chat_message(int(uid), int(message.message_id), disable_notification=True)
    return int(message.message_id)


def remember_menu_message(uid: int, message_or_id: Any) -> None:
    """Store menu message id in state."""
    try:
        message_id = int(getattr(message_or_id, "message_id", message_or_id))
    except Exception:
        return
    if message_id <= 0:
        return
    try:
        menu_map: Dict[int, int] = getattr(state, "menu_message_id")  # type: ignore[assignment]
    except Exception:
        menu_map = {}
        state.menu_message_id = menu_map  # type: ignore[assignment]
    menu_map[int(uid)] = message_id


def get_menu_message_id(uid: int) -> Optional[int]:
    try:
        menu_map = getattr(state, "menu_message_id", {})
        value = menu_map.get(int(uid)) if isinstance(menu_map, dict) else None
        return int(value) if value else None
    except Exception:
        return None


def forget_menu_message(uid: int) -> None:
    try:
        menu_map = getattr(state, "menu_message_id", {})
        if isinstance(menu_map, dict):
            menu_map.pop(int(uid), None)
    except Exception:
        pass


# 2025-09-17 · динамическая замена «📋 Создать опрос» → «📊 Отчёт по опросу» при активном цикле;
#              жёсткий синглтон меню: удаляем прежнее, пин нового.
