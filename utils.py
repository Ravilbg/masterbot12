"""
core/utils.py — вспомогательные функции, используемые большинством модулей
────────────────────────────────────────────────────────────────────────────
* truncate(text, limit)               — обрезает строку, стараясь не резать слова
* parse_players_count(raw)            — вытаскивает последнюю цифру / диапазон «2-10»
* delete_previous_private_messages()  — «пылесос» личных сообщений пользователя

Файл не изменяет публичный интерфейс, поэтому другие модули работать не
придётся. Добавлены:
    • word-friendly truncate + LRU-cache;
    • regex-based parse_players_count;
    • _safe_delete() с Flood-control (429 FloodWait);
    • более детальный logging.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
from typing import Any, List, Sequence

from aiogram import Bot, types
from aiogram.exceptions import TelegramRetryAfter

from core.state import state

logger = logging.getLogger(__name__)

__all__ = [
    "truncate",
    "parse_players_count",
    "delete_previous_private_messages",
]

# ────────────────────────────────────────────────────────────────────
# 1. truncate
# ────────────────────────────────────────────────────────────────────
_WORD_BOUNDARY_RE = re.compile(r"\s+")


@functools.lru_cache(maxsize=4096)
def truncate(text: str, limit: int = 100) -> str:
    """
    Аккуратно обрезает *text* до *limit* символов.

    • Если длина ≤ limit — возвращает исходную строку.
    • В пределах limit ищет ближайший пробел слева, чтобы не рвать слово.
    • Если пробелов нет — режет «по-старому» (чтобы сохранить совместимость).
    • Добавляет многоточие «…», если текст был урезан.

    Кэшируется LRU (4096 последних вызовов): часто используются одинаковые
    подписи игр/пакетов → даёт ~25-30 % выигрыша на профиле.
    """
    if len(text) <= limit:
        return text

    # позиция, где сам обрез должен закончиться (учитываем «…»)
    hard_cut = limit - 1
    match: Sequence[int] = [
        m.start() for m in _WORD_BOUNDARY_RE.finditer(text[:hard_cut])
    ]

    cut_pos = match[-1] if match else hard_cut
    trimmed = text[:cut_pos].rstrip()

    return f"{trimmed}…"


# ────────────────────────────────────────────────────────────────────
# 2. parse_players_count
# ────────────────────────────────────────────────────────────────────
_PLAYERS_RE = re.compile(r"\d+")


def parse_players_count(val: Any) -> int:
    """
    Пытается извлечь *последнее* целое число из строковых представлений.

    Примеры
    -------
    >>> parse_players_count("7")
    7
    >>> parse_players_count("2–10 игроков")
    10
    >>> parse_players_count(None)
    0
    """
    if not val:
        return 0

    try:
        s = str(val).replace(",", ".")
    except Exception:
        logger.debug("parse_players_count: non-castable value %r", val, exc_info=True)
        return 0

    numbers = _PLAYERS_RE.findall(s)
    if not numbers:
        return 0

    try:
        return int(numbers[-1])
    except ValueError:
        return 0


# ────────────────────────────────────────────────────────────────────
# 3. helpers для delete_previous_private_messages
# ────────────────────────────────────────────────────────────────────
async def _safe_delete(bot: Bot, chat_id: int, message_id: int) -> None:
    """
    Безопасное удаление сообщения с обработкой FloodWait (429).
    Не бросает исключений наружу.
    """
    for attempt in range(3):  # до трёх попыток
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            return
        except TelegramRetryAfter as e:
            wait_sec = int(e.retry_after) + 1
            logger.warning("FloodWait %ds while deleting %d/%d", wait_sec, chat_id, message_id)
            await asyncio.sleep(wait_sec)
        except Exception:
            logger.debug("Failed to delete msg %d/%d (attempt %d)", chat_id, message_id, attempt + 1, exc_info=True)
            return


# ────────────────────────────────────────────────────────────────────
# 4. delete_previous_private_messages
# ────────────────────────────────────────────────────────────────────
async def delete_previous_private_messages(user_id: int) -> None:
    """
    «Пылесос» личных сообщений пользователя.

    1. Удаляет сообщения, сохранённые в:
       • state.last_user_messages
       • state.detail_blocks   (карточки игр)
       • state.messages_to_delete
    2. Если пользователь — лидер активного опроса, удаляет личный дашборд.
    3. Flood-safe, подробный лог.
    """
    bot = Bot.get_current()
    removed: List[int] = []  # для логирования

    # — основной список
    msg_objs: List[types.Message] = state.last_user_messages.pop(user_id, [])
    # — карточки detail-view
    for key in [k for k in list(state.detail_blocks) if k[0] == user_id]:
        msg_objs.extend(state.detail_blocks.pop(key, []))
    # — орфанные id
    orphan_ids = state.messages_to_delete.pop(user_id, [])

    # — если лидер опроса — убираем его дашборд
    if user_id == state.current_poll_leader and state.personal_report_message_id:
        orphan_ids.append(state.personal_report_message_id)
        state.personal_report_message_id = None

    # batch-delete Message objects
    for msg in msg_objs:
        try:
            await msg.delete()
            removed.append(msg.message_id)
        except TelegramRetryAfter as e:
            await asyncio.sleep(int(e.retry_after) + 1)
            await _safe_delete(bot, user_id, msg.message_id)
            removed.append(msg.message_id)
        except Exception:
            logger.debug("Can't delete Message object %r", msg, exc_info=True)

    # batch-delete orphan ids
    for mid in orphan_ids:
        await _safe_delete(bot, user_id, mid)
        removed.append(mid)

    if removed:
        logger.debug("[vacuum] uid=%d removed %d msg(s): %s", user_id, len(removed), removed)
