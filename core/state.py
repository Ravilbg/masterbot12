# core/state.py
# ────────────────────────────────────────────────────────────────────
"""core/state.py — глобальное состояние MasterBot.

Держим только данные/кэши. Никакой await здесь быть не должно.
Версия: 2025-08-12 · совместимо с handlers/*.py v12.92+
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Set, Tuple
from aiogram.types import Message


class _State:
    """Единый контейнер состояния. Хранит кэши, флаги и рабочие структуры."""

    # per-user lock registry (внутреннее хранилище)
    _locks: Dict[int, asyncio.Lock]

    def __init__(self) -> None:
        # ═════════════════════════════════════════════════════════════
        # ОПРОСЫ / РАСПРЕДЕЛЕНИЕ
        # ═════════════════════════════════════════════════════════════
        self.coordination_cycle_active: bool = False
        self.force_closed: bool = False

        # список игр активного опроса (элементы — словари сделок из AmoCRM)
        self.current_poll_deals: List[dict] = []
        # кто запустил текущий цикл
        self.current_poll_leader: Optional[int] = None

        # ответы пользователей по poll_id:
        # {poll_id: {"deals": {deal_id: [users...]}, "not_available": [...],
        #            "admin_available": [...], "deal_indices": {idx: deal_id}}}
        self.responses: Dict[str, dict] = {}

        # готовность по конкретным играм (минимальный состав набран)
        self.current_deal_ready: Dict[int, bool] = {}
        self.all_ready_notified: bool = False

        # ручная остановка набора по сделкам
        self.deal_force_closed: Set[int] = set()

        # сообщение-дашборд лидера
        self.personal_report_message_id: Optional[int] = None
        # последние личные сообщения по пользователю: {uid: [Message,...]}
        self.last_user_messages: Dict[int, List[Message]] = {}
# ��� ������������ ��������� �������� ���� � ��: {uid: message_id}
        self.menu_message_id: Dict[int, int] = {}

        # клавиатура отчёта лидеру (верхняя часть — игры)
        self.distribution_keyboard: Any = None

        # период «подтвердите +» (список message_id в чате лидеров)
        self.current_event_period: Optional[List[int]] = None
        self.manual_confirm_requested: bool = False

        # кто уже подтвердился «плюсом» (устар., оставлен для совместимости)
        self.pending_plus: Set[int] = set()

        # 👇 НУЖНО ДЛЯ create_poll_handler (очищается при старте цикла)
        self.confirmed_users: Set[int] = set()

        # сообщения, которые нужно попытаться удалить позже
        self.messages_to_delete: Dict[int, List[int]] = {}

        # таймеры напоминаний (loop.call_later handles)
        self.reminder_tasks: List[object] = []

        # ═════════════════════════════════════════════════════════════
        # ДЕТАЛИ / РАСПРЕДЕЛЕНИЕ / МОИ ИГРЫ
        # ═════════════════════════════════════════════════════════════
        # кэш раскладки ролей в карточках деталей:
        #   {str(deal_id): {"main":[uid|tag,...], "assist":[...], "admin":[...]}}
        self.distribution_cache: Dict[str, Dict[str, List[Any]]] = {}

        # единый источник деталей, используемый разными модулями:
        #   {deal_id: {"distribution": {...}, ...}}
        self.poll_details: Dict[int, dict] = {}

        # заголовки сделок (для удобных уведомлений)
        self.deal_titles: Dict[int, str] = {}

        # индекс сделок (быстрый доступ по id)
        #   {deal_id: {...метаданные...}}
        self.deals_index: Dict[int, Dict[str, Any]] = {}

        # раскладка, «зафиксированная» руководителем (после «Утвердить»)
        #   {deal_id: {"main":[uid], "assist":[uid], "admin":[uid]}}
        self.locked_distribution: Dict[int, Dict[str, List[int]]] = {}

        # ожидание подтверждений после утверждения:
        #   {deal_id: {"distribution": {...}, "confirmed": set(uid,...)}}
        self.pending_confirmations: Dict[int, dict] = {}
        self.swap_replacements: Dict[int, dict] = {}
        self.urgent_swap_award: Dict[int, int] = {}

        # вспомогательный кэш для отчётов лидеру (не критично)
        self.poll_distribution: Dict[int, Dict[str, List[Any]]] = {}

        # открытые detail-блоки у пользователей:
        #   dict ключом (uid, deal_id) → List[Message]
        self.detail_blocks: Dict[Tuple[int, int], List[Message]] = {}

        # список игр, последний показанный пользователю (для списков/деталей)
        #   {uid: [deal_dict,...]}
        self.games_by_user: Dict[int, List[dict]] = {}

        # индекс назначенных по пользователю (для «🎲 Мои игры»):
        #   {uid: set(deal_id,...)} — наполняется при «Утвердить»
        self.assigned_index: Dict[int, Set[int]] = {}

        # ═════════════════════════════════════════════════════════════
        # ОБЩИЕ ПАРАМЕТРЫ ОКРУЖЕНИЯ
        # ═════════════════════════════════════════════════════════════
        # id админ/лидер чата, может выставляться на старте
        self.admin_chat_id: Optional[int] = None

        # ═════════════════════════════════════════════════════════════
        # AMOCRM AUTH/STATE
        # ═════════════════════════════════════════════════════════════
        # Словарь с актуальными токенами AmoCRM.
        # Ожидается структура вроде:
        # {"access_token": str, "refresh_token": str, "expires_at": int, ...}
        # Важно: handlers не должны очищать это поле при сбросе цикла!
        self.tokens: Optional[Dict[str, Any]] = None

        # Доп. кэши (опционально используются в services/amocrm)
        self.amocrm_meta: Dict[str, Any] = {}          # напр.: аккаунт/пользователи/воронки
        self.amocrm_last_refresh_ts: int = 0           # unix-ts последнего успешного refresh

        # инициализация локов
        self._locks = {}

    # ────────────────────────────────────────────────────────────────
    # Per-user async-lock: синхронный геттер без await (логики ожидания нет)
    # ────────────────────────────────────────────────────────────────
    def lock_for(self, uid: int) -> asyncio.Lock:
        """Возвращает (или создаёт) asyncio.Lock для конкретного пользователя."""
        lock = self._locks.get(uid)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[uid] = lock
        return lock


state = _State()

__all__ = ["state"]

# История изменений:
# • 2025-08-12 — добавлены: lock_for (per-user lock), games_by_user, assigned_index.
# • 2025-08-10 — detail_blocks переведён в dict[(uid, deal_id)] → List[Message];
#                добавлен deals_index для совместимости.
# • 2025-08-09 — добавлены poll_details, deal_titles, pending_confirmations, detail_blocks.
# • 2025-08-09 — добавлены tokens/amocrm_meta/amocrm_last_refresh_ts.
# • 2025-08-09 — добавлено confirmed_users.
# � 2025-09-17 � �������� menu_message_id ��� ��������� �������� ���� (SSOT).
# 2025-09-17 � ������ ��������: ��������� ��� SSOT.
