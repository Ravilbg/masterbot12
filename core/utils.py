"""core/utils.py — универсальные вспомогательные функции MasterBot
────────────────────────────────────────────────────────────────────────────
Версия 2025‑08‑07 · совместима с MasterBot ≥ 15.0

Main changes vs 2025‑08‑03
──────────────────────────
• delete_previous_private_messages():
    – теперь отслеживает **неудавшиеся** удаления и пишет их обратно
      в state.messages_to_delete[user] c предупреждением в логе;
    – _safe_delete() / _safe_delete_message_obj() возвращают bool success.
"""

from __future__ import annotations

# ███ [0] IMPORTS
# --------------------------------------------------------------------
import asyncio
import functools
import logging
import re
from typing import Any, List, Sequence, Union

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import Message

from core.state import state

logger = logging.getLogger(__name__)

__all__ = [
    "truncate",
    "parse_players_count",
    "delete_previous_private_messages",
]

# ════════════════════════════════════════════════════════════════════
# [1] truncate
# ════════════════════════════════════════════════════════════════════
_WORD_BOUNDARY_RE = re.compile(r"\s+")


@functools.lru_cache(maxsize=4096)
def truncate(text: str, limit: int = 100) -> str:  # noqa: D401
    """Обрезает *text* до *limit* символов, добавляя «…» при обрезке."""
    if len(text) <= limit:
        return text
    hard_cut = limit - 1
    boundaries: Sequence[int] = [m.start() for m in _WORD_BOUNDARY_RE.finditer(text[:hard_cut])]
    cut_pos = boundaries[-1] if boundaries else hard_cut
    return f"{text[:cut_pos].rstrip()}…"

# ════════════════════════════════════════════════════════════════════
# [2] parse_players_count
# ════════════════════════════════════════════════════════════════════
_DIGIT_RE = re.compile(r"\d+")


def parse_players_count(val: Any) -> int:
    """Возвращает последнее целое число из произвольного *val*."""
    if not val:
        return 0
    try:
        s = str(val)
    except Exception:
        logger.debug("parse_players_count: non‑castable value %r", val, exc_info=True)
        return 0
    numbers = _DIGIT_RE.findall(s)
    return int(numbers[-1]) if numbers else 0

# ════════════════════════════════════════════════════════════════════
# [3] helpers – flood‑safe delete
# ════════════════════════════════════════════════════════════════════
async def _safe_delete(bot: Bot, chat_id: int, message_id: int) -> bool:
    """Удаляет сообщение. Возвращает *True*, если успешно."""
    for attempt in range(3):
        try:
            await bot.delete_message(chat_id, message_id)
            return True
        except TelegramRetryAfter as e:
            wait = int(getattr(e, "retry_after", 1)) + 1
            logger.warning("[vacuum] FloodWait %ds for %d/%d (attempt %d)", wait, chat_id, message_id, attempt + 1)
            await asyncio.sleep(wait)
        except Exception as exc:
            logger.debug("[vacuum] delete %d/%d failed on attempt %d: %s", chat_id, message_id, attempt + 1, exc)
            return False
    return False


async def _safe_delete_message_obj(msg: Message) -> bool:
    """Безопасно удаляет объект Message. True при успехе."""
    try:
        await msg.delete()
        return True
    except TelegramRetryAfter as e:
        await asyncio.sleep(int(getattr(e, "retry_after", 1)) + 1)
        return await _safe_delete(Bot.get_current(), msg.chat.id, msg.message_id)
    except Exception as exc:
        logger.debug("[vacuum] delete obj %d/%d failed: %s", msg.chat.id, msg.message_id, exc)
        return False

# ════════════════════════════════════════════════════════════════════
# [4] delete_previous_private_messages
# ════════════════════════════════════════════════════════════════════
async def delete_previous_private_messages(user_id: int) -> None:
    """Удаляет все сохранённые ботом личные сообщения пользователя."""
    bot = Bot.get_current()
    removed: List[int] = []
    failed: List[int] = []

    # 1) объекты Message
    msg_objs: List[Message] = state.last_user_messages.pop(user_id, [])

    # 2) detail‑view карточки
    for key in [k for k in state.detail_blocks if k[0] == user_id]:
        msg_objs.extend(state.detail_blocks.pop(key, []))

    # 3) orphan ids, накопленные ранее
    orphan_ids: List[int] = state.messages_to_delete.pop(user_id, [])

    # 4) персональный отчёт лидера
    if user_id == state.current_poll_leader and state.personal_report_message_id:
        orphan_ids.append(state.personal_report_message_id)
        state.personal_report_message_id = None

    # ── удаляем объекты Message ────────────────────────────────────
    if msg_objs:
        results = await asyncio.gather(*(_safe_delete_message_obj(m) for m in msg_objs), return_exceptions=False)
        for ok, m in zip(results, msg_objs):
            (removed if ok else failed).append(m.message_id)

    # ── удаляем по ID ──────────────────────────────────────────────
    if orphan_ids:
        results = await asyncio.gather(*(_safe_delete(bot, user_id, mid) for mid in orphan_ids), return_exceptions=False)
        for ok, mid in zip(results, orphan_ids):
            (removed if ok else failed).append(mid)

    # если что‑то не удалилось — сохраним на потом и залогируем
    if failed:
        state.messages_to_delete.setdefault(user_id, []).extend(failed)
        logger.warning("[vacuum] uid=%d NOT removed %d msg(s): %s", user_id, len(failed), failed)
    if removed:
        logger.debug("[vacuum] uid=%d removed %d msg(s): %s", user_id, len(removed), removed)

# ════════════════════════════════════════════════════════════════════
# [99] SELF‑TEST
# ════════════════════════════════════════════════════════════════════
async def _test() -> None:
    assert truncate("abcd efgh ijkl", 8) == "abcd…"
    assert parse_players_count("до 8 чел.") == 8
    print("core.utils ✅ tests passed")


if __name__ == "__main__":
    import asyncio, logging as _l
    _l.basicConfig(level=_l.DEBUG)
    asyncio.run(_test())
