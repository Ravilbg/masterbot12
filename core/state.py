from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from aiogram.types import InlineKeyboardMarkup, Message

class _State:
    """Runtime‑данные, живут пока работает процесс."""

    # ——— config / tokens ——————————————————————————
    config: Dict[str, Any] = {}          # JSON из config.json
    tokens: Dict[str, Any] = {}          # tokens.json

    # ——— Chat / Spreadsheet ——————————————————————
    admin_chat_id: Optional[int] = None
    svetofor_spreadsheet_id: Optional[str] = None

    # ——— Poll / Distribution ————————————————————
    current_poll_deals: List[Dict] = []
    responses: Dict[str, Any] = {}
    distribution_cache: Dict[str, Dict[str, str]] = {}
    distribution_keyboard: Optional[InlineKeyboardMarkup] = None
    current_poll_leader: Optional[int] = None
    coordination_cycle_active: bool = False
    personal_report_message_id: Optional[int] = None

    # ——— Periods ——————————————————————————————
    current_event_period: Optional[List[datetime]] = None
    last_event_period: Optional[List[datetime]] = None

    # ——— Caches / msg housekeeping ————————————
    pipeline_mapping: Dict = {}
    deals_cache: Dict[str, Dict] = {}
    messages_to_delete: Dict[int, List[int]] = {}
    last_user_messages: Dict[int, List[Message]] = {}
    detail_blocks: Dict[Tuple[int, int], List[Message]] = {}
    games_by_user: Dict[int, List[Dict]] = {}

    # ——— cache helpers ————————————————————————
    def cache_ok(self, key: str, ttl: timedelta) -> bool:
        entry = self.deals_cache.get(key)
        if not entry:
            return False
        return datetime.now() - entry["timestamp"] < ttl

    # ——— async per‑user lock ——————————————————————
    _user_locks: Dict[int, asyncio.Lock] = {}

    def lock_for(self, uid: int) -> asyncio.Lock:
        return self._user_locks.setdefault(uid, asyncio.Lock())

state = _State()
