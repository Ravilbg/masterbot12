"""core/state.py — глобальное runtime-состояние MasterBot
──────────────────────────────────────────────────────────────────────────────
Хранит все временные данные процесса: конфиг, кеши, состояние опроса,
таймеры напоминаний и прочие «живые» переменные.

Дополнения v12.93-cycle (2025-07-22)
• confirmed_users, reminder_tasks, force_closed, deal_force_closed,
  manual_confirm_requested — для цикла распределения.
• current_deal_ready, all_ready_notified, pending_plus — логика «готово / +».
"""

from __future__ import annotations

# ███ [1.0] IMPORTS
# --------------------------------------------------------------------
import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from aiogram.types import InlineKeyboardMarkup, Message


# ███ [2.0] STATE CLASS
# --------------------------------------------------------------------
class _State:
    """Runtime-данные, живут пока работает процесс."""

    # ——— Config / tokens ——————————————————————————
    config: Dict[str, Any] = {}          # JSON из config.json
    tokens: Dict[str, Any] = {}          # tokens.json

    # ——— Chat / Spreadsheet ——————————————————————
    admin_chat_id: Optional[int] = None
    svetofor_spreadsheet_id: Optional[str] = None

    # ——— Poll / Distribution ————————————————————
    current_poll_deals: List[Dict] = []                 # сделки в текущем опросе
    responses: Dict[str, Any] = {}                      # poll_id → ответы
    distribution_cache: Dict[str, Dict[str, str]] = {}  # deal_id → {role: tag}
    distribution_keyboard: Optional[InlineKeyboardMarkup] = None
    current_poll_leader: Optional[int] = None
    coordination_cycle_active: bool = False
    personal_report_message_id: Optional[int] = None

    # ——— Manual control flags (v12.93) ——————————
    force_closed: bool = False                       # ручной стоп всего цикла
    deal_force_closed: Set[int] = set()              # ID игр, закрытых «Стоп набор»
    manual_confirm_requested: bool = False           # «+» запрошены вручную
    confirmed_users: Set[int] = set()                # UID, приславшие «+»
    reminder_tasks: List[asyncio.TimerHandle] = []   # ссылки на call_later-таймеры

    # ——— Ready / Approvals (v12.93) ————————————
    current_deal_ready: Dict[int, bool] = {}         # deal_id → True, если набран минимум
    all_ready_notified: bool = False                 # уже сообщали «все готовы»
    pending_plus: Dict[int, int] = {}                # msg_id (ожид. «+») → deal_id

    # ——— Periods ——————————————————————————————
    current_event_period: Optional[List[datetime]] = None
    last_event_period: Optional[List[datetime]] = None

    # ——— Caches / msg housekeeping ————————————
    pipeline_mapping: Dict = {}
    deals_cache: Dict[str, Dict] = {}                # произвольный кеш сделок
    messages_to_delete: Dict[int, List[int]] = {}    # uid → [msg_id…]
    last_user_messages: Dict[int, List[Message]] = {}  # для автo-удаления
    detail_blocks: Dict[Tuple[int, int], List[Message]] = {}  # (uid, deal_id) → msgs
    games_by_user: Dict[int, List[Dict]] = {}        # uid → [{deal}, …]

    # ——— cache helpers ————————————————————————
    def cache_ok(self, key: str, ttl: timedelta) -> bool:
        """True, если deals_cache[key] свежее, чем ttl."""
        entry = self.deals_cache.get(key)
        if not entry:
            return False
        return datetime.now() - entry["timestamp"] < ttl

    # ——— async per-user lock ——————————————————————
    _user_locks: Dict[int, asyncio.Lock] = {}

    def lock_for(self, uid: int) -> asyncio.Lock:
        """Возвращает (и создаёт при необходимости) asyncio-Lock для пользователя."""
        return self._user_locks.setdefault(uid, asyncio.Lock())


# ███ [3.0] SINGLETON
# --------------------------------------------------------------------
state = _State()

# История изменений:
#   • 2025-07-22 — добавлены поля для manual-cycle, reminder_tasks, ready/approvals
