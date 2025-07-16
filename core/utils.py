"""Разные мелкие хелперы, используемые сразу в нескольких модулях."""

from __future__ import annotations

import logging
from typing import Any, Dict

from aiogram import Bot, types

from core.state import state

logger = logging.getLogger(__name__)


def truncate(text: str, limit: int = 100) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_players_count(val: Any) -> int:
    try:
        if not val:
            return 0
        s = str(val).strip()
        if "-" in s:
            return int(s.split("-")[-1].strip())
        return int(s)
    except Exception:
        return 0


async def delete_previous_private_messages(user_id: int) -> None:
    msg_objects = state.last_user_messages.pop(user_id, [])
    for key in [k for k in list(state.detail_blocks) if k[0] == user_id]:
        msg_objects += state.detail_blocks.pop(key, [])
    orphan_ids = state.messages_to_delete.pop(user_id, [])
    if (
        user_id == state.current_poll_leader
        and state.personal_report_message_id
    ):
        orphan_ids.append(state.personal_report_message_id)
        state.personal_report_message_id = None

    bot = Bot.get_current()
    for m in msg_objects:
        try:
            await m.delete()
        except Exception:
            pass
    for mid in orphan_ids:
        try:
            await bot.delete_message(chat_id=user_id, message_id=mid)
        except Exception:
            pass
