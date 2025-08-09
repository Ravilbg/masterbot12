"""core/state.py — глобальное runtime-состояние MasterBot
──────────────────────────────────────────────────────────────────────────────
Хранит все временные данные процесса: конфиг, кеши, состояние опроса,
таймеры напоминаний и прочие «живые» переменные.

Дополнения v15.2 · 2025-08-09
• **pending_confirmations** — deal_id → {"distribution": {...}, "confirmed": set()}.
• **confirmed**             — deal_id → {uid…} (подтверждение «✅»).
• **my_games_by_user**       — uid → [List[deal]] для нового дашборда.
• **_vacuum_task**           — ссылка на фоновый «пылесос» сообщений.
• **last_user_messages**     — теперь List[Message], а не single id.

Остальной интерфейс совместим с версиями < 15.0.
"""

from __future__ import annotations

# ███ [1.0] IMPORTS
# --------------------------------------------------------------------
import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from aiogram.types import InlineKeyboardMarkup, Message
else:
    InlineKeyboardMarkup = Any  # type: ignore
    Message = Any  # type: ignore

# ███ [2.0] STATE CLASS
# --------------------------------------------------------------------
class _State:
    """Runtime-данные, живут пока работает процесс."""

    # ——— Config / tokens ——————————————————————————
    config: Dict[str, Any] = {}
    tokens: Dict[str, Any] = {}

    # ——— Chat / Spreadsheet ——————————————————————
    admin_chat_id: Optional[int] = None
    svetofor_spreadsheet_id: Optional[str] = None

    # ——— Poll / Distribution ————————————————————
    current_poll_deals: List[Dict] = []                      # игры в текущем опросе
    poll_message_ids: List[int] = []                         # ID опубликованных опросов
    locked_distribution: Dict[int, Dict[str, str]] = {}      # deal_id → {role: tag}
    confirmed: Dict[int, Set[int]] = {}                      # deal_id → {uid…}
    pending_confirmations: Dict[int, Dict[str, Any]] = {}    # deal_id → {"distribution": {...}, "confirmed": set()}
    responses: Dict[str, Any] = {}                           # poll_id → ответы
    distribution_cache: Dict[str, Dict[str, str]] = {}       # deal_id → {role: tag}
    distribution_keyboard: Optional[InlineKeyboardMarkup] = None
    current_poll_leader: Optional[int] = None
    coordination_cycle_active: bool = False
    personal_report_message_id: Optional[int] = None

    # ——— Manual control flags ————————————————
    force_closed: bool = False
    deal_force_closed: Set[int] = set()
    manual_confirm_requested: bool = False
    confirmed_users: Set[int] = set()                        # устарело (исп. для «+»)
    reminder_tasks: List[asyncio.TimerHandle] = []

    # ——— Ready / Approvals ——————————————————————
    current_deal_ready: Dict[int, bool] = {}
    all_ready_notified: bool = False
    pending_plus: Dict[int, int] = {}

    # ——— Periods ——————————————————————————————
    current_event_period: Optional[List[datetime]] = None
    last_event_period: Optional[List[datetime]] = None

    # ——— Caches / msg housekeeping —————————————
    pipeline_mapping: Dict = {}
    deals_cache: Dict[str, Dict] = {}
    messages_to_delete: Dict[int, List[int]] = {}
    last_user_messages: Dict[int, List[Message]] = {}
    detail_blocks: Dict[Tuple[int, int], List[Message]] = {}
    games_by_user: Dict[int, List[Dict]] = {}               # legacy cache
    my_games_by_user: Dict[int, List[Dict]] = {}            # новый дашборд

    # ——— Background tasks ——————————————————————
    _vacuum_task: Optional[asyncio.Task] = None

    # ——— Pinned menus ——————————————————————————
    group_menu_message_id: Dict[int, int] = {}

    # ——— cache helpers —————————————————————————
    def cache_ok(self, key: str, ttl: timedelta) -> bool:
        entry = self.deals_cache.get(key)
        if not entry:
            return False
        return datetime.now() - entry.get("timestamp", datetime.now()) < ttl

    # ——— async per-user lock —————————————————————
    _user_locks: Dict[int, asyncio.Lock] = {}

    def lock_for(self, uid: int) -> asyncio.Lock:
        """Возвращает новый или существующий asyncio.Lock для пользователя."""
        return self._user_locks.setdefault(uid, asyncio.Lock())

    # ——— message housekeeping ————————————————
    async def register_message(self, user_id: int, message_id: int) -> Optional[int]:
        prev = None
        msgs = self.last_user_messages.setdefault(user_id, [])
        if msgs:
            prev = msgs[-1].message_id if hasattr(msgs[-1], "message_id") else None
        return prev

    # ——— poll housekeeping ——————————————————————
    async def register_poll(self, message_id: int) -> None:
        self.poll_message_ids.append(message_id)

# ███ [3.0] SINGLETON
# --------------------------------------------------------------------
state = _State()

# История изменений:
#   • 2025-07-22 — initial manual-cycle fields
#   • 2025-08-04 — locked_distribution, poll_message_ids
#   • 2025-08-08 — confirmed, my_games_by_user, _vacuum_task, list last_user_messages
#   • 2025-08-09 — pending_confirmations для цикла подтверждений
