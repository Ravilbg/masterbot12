"""core/utils.py — универсальные вспомогательные функции MasterBot
────────────────────────────────────────────────────────────────────────────
Версия 2025-08-11 · совместима с MasterBot ≥ 12.92

Изменения 2025-08-11
────────────────────
• delete_previous_private_messages: улучшена устойчивость и совместимость,
  поддержан параметр keep (Message | int), безопасная работа при отсутствии полей state.
• «Пылесос»: корректно удаляет всё, кроме переданных в keep, чистит detail-блоки
  в новом формате dict[(uid, deal_id)] → List[Message] и терпимо обрабатывает старый set-формат.
• Мелкие правки логирования, защита от редких крашей.
"""

from __future__ import annotations

# ███ [0] IMPORTS
# --------------------------------------------------------------------
import asyncio
import functools
import logging
import re
from typing import Any, List, Sequence, Tuple, Union

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
    hard_cut = max(1, limit - 1)
    boundaries: Sequence[int] = [m.start() for m in _WORD_BOUNDARY_RE.finditer(text[:hard_cut])]
    cut_pos = boundaries[-1] if boundaries else hard_cut
    return f"{text[:cut_pos].rstrip()}…"

# История изменений: добавлено кэширование, безопасный hard_cut (2025-08-11)


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
        logger.debug("parse_players_count: non-castable value %r", val, exc_info=True)
        return 0
    numbers = _DIGIT_RE.findall(s)
    return int(numbers[-1]) if numbers else 0

# История изменений: без изменений логики (2025-08-11)


# ════════════════════════════════════════════════════════════════════
# [3] helpers – flood-safe delete
# ════════════════════════════════════════════════════════════════════
async def _safe_delete(bot: Bot, chat_id: int, message_id: int) -> bool:
    """Удаляет сообщение. Возвращает *True* при успехе."""
    for attempt in range(3):
        try:
            await bot.delete_message(chat_id, message_id)
            return True
        except TelegramRetryAfter as e:
            wait = int(getattr(e, "retry_after", 1)) + 1
            logger.warning(
                "[vacuum] FloodWait %ds for %d/%d (attempt %d)",
                wait,
                chat_id,
                message_id,
                attempt + 1,
            )
            await asyncio.sleep(wait)
        except Exception as exc:
            # Любая иная ошибка — считаем, что удалить не удалось, не падаем.
            logger.debug(
                "[vacuum] delete %d/%d failed on attempt %d: %s",
                chat_id,
                message_id,
                attempt + 1,
                exc,
            )
            return False
    return False


async def _safe_delete_message_obj(msg: Message) -> bool:
    """Безопасно удаляет объект Message. True при успехе."""
    try:
        await msg.delete()
        return True
    except TelegramRetryAfter as e:
        await asyncio.sleep(int(getattr(e, "retry_after", 1)) + 1)
        try:
            return await _safe_delete(Bot.get_current(), msg.chat.id, msg.message_id)
        except Exception:
            return False
    except Exception as exc:
        logger.debug("[vacuum] delete obj %d/%d failed: %s", getattr(msg.chat, "id", 0), msg.message_id, exc)
        return False

# История изменений: унифицированный лог, защита от редких Nones (2025-08-11)


# ════════════════════════════════════════════════════════════════════
# [4] delete_previous_private_messages (backward-compatible)
# ════════════════════════════════════════════════════════════════════
async def delete_previous_private_messages(
    *args: Union[int, Bot],
    keep: Sequence[Union[Message, int]] | None = None,
) -> None:
    """
    Удаляет все сохранённые ботом личные сообщения пользователя.

    Совместимые сигнатуры:
      • delete_previous_private_messages(user_id: int)
      • delete_previous_private_messages(bot: Bot, user_id: int, state_obj: Any = None)

    Дополнительно:
      • keep=[Message|int, ...] — список сообщений/ID, которые надо СОХРАНИТЬ.
    """
    # ── разбор аргументов (совместимость со старым вызовом) ─────────
    if len(args) == 1:
        user_id = int(args[0])  # type: ignore[arg-type]
        try:
            bot = Bot.get_current()
        except Exception as e:
            logger.error("delete_previous_private_messages: Bot.get_current() failed: %s", e)
            raise
    elif len(args) >= 2 and isinstance(args[0], Bot):
        bot = args[0]  # type: ignore[assignment]
        user_id = int(args[1])  # type: ignore[arg-type]
    else:
        raise TypeError("delete_previous_private_messages: expected (user_id) or (bot, user_id, state)")

    # ── нормализация keep → множество ID ────────────────────────────
    keep_ids: set[int] = set()
    if keep:
        for item in keep:
            if isinstance(item, Message):
                keep_ids.add(item.message_id)
            else:
                try:
                    keep_ids.add(int(item))
                except Exception:
                    pass

    removed: List[int] = []
    failed: List[int] = []

    # ── гарантируем структуры state (без крашей при отсутствии) ─────
    mtd: dict[int, List[int]] = getattr(state, "messages_to_delete", None) or {}
    setattr(state, "messages_to_delete", mtd)

    last_user_messages: dict[int, List[Message]] = getattr(state, "last_user_messages", None) or {}
    setattr(state, "last_user_messages", last_user_messages)

    detail_blocks = getattr(state, "detail_blocks", None)

    # 1) объекты Message из last_user_messages
    msg_objs: List[Message] = []
    try:
        for m in last_user_messages.pop(user_id, []):
            if isinstance(m, Message) and m.message_id not in keep_ids:
                msg_objs.append(m)
    except Exception as e:
        logger.debug("[vacuum] last_user_messages cleanup failed for uid=%d: %s", user_id, e)

    # 2) detail-view карточки
    #    Новый формат: dict[(uid, deal_id)] -> List[Message]
    #    Старый формат: set[(uid, deal_id)]
    if isinstance(detail_blocks, dict):
        try:
            to_pop = [k for k in detail_blocks.keys() if isinstance(k, tuple) and len(k) == 2 and k[0] == user_id]
            for key in to_pop:
                for m in detail_blocks.get(key, []) or []:
                    if isinstance(m, Message) and m.message_id not in keep_ids:
                        msg_objs.append(m)
                detail_blocks.pop(key, None)
        except Exception as e:
            logger.debug("[vacuum] detail_blocks(dict) cleanup failed for uid=%d: %s", user_id, e)
    elif isinstance(detail_blocks, set):
        # чистка старого формата — здесь нет объектов Message, только ключи
        try:
            to_remove = [k for k in detail_blocks if isinstance(k, tuple) and len(k) == 2 and k[0] == user_id]
            for key in to_remove:
                try:
                    detail_blocks.remove(key)
                except Exception:
                    pass
        except Exception as e:
            logger.debug("[vacuum] detail_blocks(set) cleanup failed for uid=%d: %s", user_id, e)

    # 3) orphan-ids (сообщения, сохранённые как ID)
    orphan_ids: List[int] = []
    try:
        orphan_ids = [mid for mid in mtd.pop(user_id, []) if mid not in keep_ids]
    except Exception as e:
        logger.debug("[vacuum] messages_to_delete cleanup failed for uid=%d: %s", user_id, e)

    # 4) персональный отчёт лидера — сохраняем, если он в keep
    try:
        current_leader = getattr(state, "current_poll_leader", None)
        personal_msg_id = getattr(state, "personal_report_message_id", None)
        if user_id == current_leader and personal_msg_id:
            if personal_msg_id not in keep_ids:
                orphan_ids.append(personal_msg_id)
                setattr(state, "personal_report_message_id", None)
    except Exception:
        pass

    # ── УДАЛЕНИЕ: объекты Message ───────────────────────────────────
    if msg_objs:
        results = await asyncio.gather(
            *(_safe_delete_message_obj(m) for m in msg_objs),
            return_exceptions=False,
        )
        for ok, m in zip(results, msg_objs):
            (removed if ok else failed).append(m.message_id)

    # ── УДАЛЕНИЕ: по ID ─────────────────────────────────────────────
    if orphan_ids:
        results = await asyncio.gather(
            *(_safe_delete(bot, user_id, mid) for mid in orphan_ids),
            return_exceptions=False,
        )
        for ok, mid in zip(results, orphan_ids):
            (removed if ok else failed).append(mid)

    logger.debug(
        "[vacuum] uid=%d removed=%s failed=%s keep=%s",
        user_id,
        removed,
        failed,
        sorted(keep_ids),
    )

# История изменений: добавлено keep=(Message|int), защита getattr(state,*), чистка detail-блоков (2025-08-11)


# ════════════════════════════════════════════════════════════════════
# [99] SELF-TEST
# ════════════════════════════════════════════════════════════════════
async def _test() -> None:
    assert truncate("abcd efgh ijkl", 8) == "abcd…"
    assert parse_players_count("до 8 чел.") == 8
    print("core.utils ✅ tests passed")


if __name__ == "__main__":
    import asyncio as _a
    import logging as _l

    _l.basicConfig(level=_l.DEBUG)
    _a.run(_test())
