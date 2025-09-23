# handlers/confirmations.py
# -----------------------------------------------------------------------------
"""
Подтверждения участия ведущими.

Версия v15.9 · 2025-08-24
------------------------------------------------------------------------------
• SSOT: short_name/to_uid_list/normalize_roles/resolve_notify_chat_id из core.utils.
• «✅ Подтвердить» ставит персональный тег в AmoCRM (add-only, без перезаписи).
• Надёжное определение роли и состава из locked_distribution (списки и слоты).
• ВАЖНО: Формат тегов строго берётся из locked_distribution > «Имя Ф.1/2/Адм/Стаж».
  (Берём подпись до «|uid», при отсутствии суффикса — аккуратно добавляем.)
• Локальная отметка кнопки на «✅ Подтверждено» без «прыжков» UI.
• Уведомление в общий чат: «? Участие подтверждено: Имя Ф. — 🎭 Роль на „Название“ ДД.ММ ЧЧ:ММ».
• Проверка полноты подтверждений: CRM-теги > локальный кэш (fallback).
• Автоперевод в статус «Завершение сделки» — ТОЛЬКО из «Бронь», если все роли подтверждены.
• Совместимость с aiogram 3.x и Pylance (строгие типы).
• ФИКС: после подтверждения из «Мои игры» НЕ открываем детали отчёта; редрав только у зрителей деталей.
"""

from __future__ import annotations

# --- [0] IMPORTS & CONSTANTS
# --------------------------------------------------------------------
import contextlib
import logging
import time
from contextlib import suppress
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

from core.config import settings
from core.state import state
try:
    from services.ratings import record_event  # type: ignore
except Exception:
    async def record_event(*_: Any, **__: Any) -> None:
        return None

from core.utils import (
    short_name,              # SSOT: «Имя Ф.»
    to_uid_list,             # SSOT: парсинг uid
    normalize_roles,         # SSOT: нормализация ролей/слотов
    resolve_notify_chat_id,  # SSOT: резолвер общего чата (sync/async-совм.)
)

# AmoCRM (универсальные обёртки)
from services import amocrm as amo  # type: ignore
from services.amocrm import update_deal_status  # сетевые действия с Amo — через services.amocrm

# Детали сделки — мягкая перерисовка после подтверждения (только у зрителей)
try:
    from handlers.poll_details import refresh_deal_details  # type: ignore
except Exception:  # pragma: no cover
    refresh_deal_details = None  # type: ignore

logger = logging.getLogger(__name__)
router = Router(name="confirmations")

# Префикс для inline-кнопок подтверждения (используется в my_games)
CONFIRM_PREFIX = "confirm_participation_"
LEGACY_CONFIRM_PREFIX = "confirm_role_"

# Статусы из настроек
BRON_STATUS_ID        = str(getattr(settings, "BRON_STATUS_ID", "") or "")
SUCCESSFUL_STATUS_ID  = str(getattr(settings, "SUCCESSFUL_STATUS_ID", "") or "")
PRELIM_STATUS_ID      = str(getattr(settings, "PRELIM_STATUS_ID", getattr(settings, "PRELIMINARY_STATUS_ID", "") or "") or "")

# История изменений [0]:
# 2025-08-18 — выровнено под SSOT, убраны локальные дубли, фиксы Pylance
# 2025-08-19 — фикс формата тегов: берём из locked_distribution (подпись до |uid)
# 2025-08-19 — перевод в SUCCESS только из «Бронь» (жёсткая проверка статуса)
# 2025-08-24 — ФИКС: убран автопереход в детали из «Мои игры», редрав только у зрителей деталей


# --- [0.1] SAFE HELPERS
# --------------------------------------------------------------------
async def _safe_answer(callback: CallbackQuery, text: str, *, show_alert: bool = False) -> None:
    """Безопасный ответ на callback.answer: игнорируем «query is too old / invalid»."""
    try:
        await callback.answer(text, show_alert=show_alert)
    except TelegramBadRequest as e:
        msg = str(e)
        if "query is too old" in msg or "query ID is invalid" in msg:
            logger.debug("[confirm] skip old/invalid query answer: %s", msg)
        else:
            raise


# --- [1] ROLE/ASSIGNMENT HELPERS
# --------------------------------------------------------------------
def _role_alias(key: str) -> str:
    """Нормализует ключ в одно из {'main','assist','admin','trainee'}."""
    k = (key or "").lower()
    if k.startswith("lead") or k == "main":
        return "main"
    if k.startswith("assist"):
        return "assist"
    if "admin" in k:
        return "admin"
    if "trainee" in k or "intern" in k or "стаж" in k:
        return "trainee"
    return k


def _assigned_uids_from_locked(deal_id: int) -> Dict[str, Set[int]]:
    """Читает state.locked_distribution и возвращает назначенных uid по ролям."""
    raw = (getattr(state, "locked_distribution", {}) or {}).get(deal_id) \
          or (getattr(state, "locked_distribution", {}) or {}).get(str(deal_id)) \
          or {}
    roles = normalize_roles(raw)  # {'main':[...], 'assist':[...], 'admin':[...], 'trainee':[...]}
    return {k: set(map(int, to_uid_list(v))) for k, v in roles.items()}


def _deal_when(deal_id: int) -> Tuple[str, str]:
    """Возвращает (date, time) строки для уведомлений из локального кэша."""
    with suppress(Exception):
        for d in (state.current_poll_deals or []):
            if int(d.get("id") or 0) == int(deal_id):
                if d.get("event_datetime") and hasattr(d["event_datetime"], "strftime"):
                    date_s = d["event_datetime"].strftime("%d.%m.%Y")
                else:
                    date_s = str(d.get("event_date") or "")
                time_s = str(d.get("event_time") or "")
                return (date_s or "", time_s or "")
    with suppress(Exception):
        meta = (getattr(state, "deals_index", {}) or {}).get(deal_id) \
             or (getattr(state, "deals_index", {}) or {}).get(str(deal_id)) \
             or {}
        return (str(meta.get("date") or ""), str(meta.get("time") or ""))
    return ("", "")


def _deal_title_from_state(deal_id: int) -> str:
    """Возвращает короткий заголовок игры без обращения к CRM."""
    with suppress(Exception):
        t = (getattr(state, "deal_titles", {}) or {}).get(deal_id) \
            or (getattr(state, "deal_titles", {}) or {}).get(str(deal_id))
        if t:
            return str(t)
    with suppress(Exception):
        for d in (state.current_poll_deals or []):
            if int(d.get("id") or 0) == int(deal_id):
                return str(d.get("game_name") or d.get("name") or f"Сделка #{deal_id}")
    return f"Сделка #{deal_id}"


def _mark_confirmed_on_message_kb(callback: CallbackQuery, deal_id: int, role: str) -> InlineKeyboardMarkup | None:
    """
    ТИХАЯ замена кнопки «Подтвердить» > «🔁 Замена» в текущем сообщении.
    Поддерживает два формата исходных кнопок:
      • confirm_participation_{deal_id}_{role}
      • mygames_confirm_{deal_id}
    Новый callback: mygames_swap_{deal_id}
    """
    try:
        msg = getattr(callback, "message", None)
        kb = getattr(msg, "reply_markup", None)
        if not isinstance(kb, InlineKeyboardMarkup):
            return None
        new_rows: List[List[InlineKeyboardButton]] = []
        target_cd_role = f"{CONFIRM_PREFIX}{deal_id}_{role}"
        target_cd_mg   = f"mygames_confirm_{deal_id}"
        replace_cd     = f"mygames_swap_{deal_id}"  # FIX: plural to match my_games handlers

        for row in (kb.inline_keyboard or []):
            new_row: List[InlineKeyboardButton] = []
            for btn in row:
                cd = (getattr(btn, "callback_data", "") or "")
                if cd == target_cd_role or cd == target_cd_mg or (cd.startswith("confirm_") and str(deal_id) in cd):
                    new_row.append(InlineKeyboardButton(text="🔁 Замена", callback_data=replace_cd))
                else:
                    new_row.append(btn)
            new_rows.append(new_row)
        return InlineKeyboardMarkup(inline_keyboard=new_rows)
    except Exception:
        logger.exception("[confirm] failed to rebuild keyboard")
    return None



# [1.5] SLOT MEMBERSHIP & SLOT KEY HELPERS — поддержка слотов БЕЗ «|uid»
NBSP = "\u00A0"; FE0F = "\uFE0F"; ZWNBSP = "\uFEFF"

def _norm_label(s: str) -> str:
    s = (s or "").replace(NBSP, " ").replace(FE0F, "").replace(ZWNBSP, "")
    return " ".join(s.split()).strip().lower()

async def _label_belongs_to_uid(slot_value: Any, uid: int) -> bool:
    """
    True, если значение слота относится к пользователю:
      • сначала пытаемся по uid (to_uid_list),
      • фолбэк — по ярлыку до «|uid», сравнивая с short_name(uid).
    """
    if slot_value is None:
        return False

    # 1) по uid (новый формат «...|123» и коллекции)
    try:
        uids = set(map(int, to_uid_list(slot_value)))
        if int(uid) in uids:
            return True
    except Exception:
        pass

    # 2) по ярлыку — слоты без «|uid»
    try:
        base = await short_name(uid)
    except Exception:
        base = ""
    if not base:
        return False
    label = str(slot_value).split("|", 1)[0].strip()
    return _norm_label(label).startswith(_norm_label(base))

async def _slot_key_for_user(deal_id: int, uid: int) -> Optional[str]:
    """
    Возвращает имя КОНКРЕТНОГО слота (например, 'lead1', 'assistant2', 'admin'),
    в котором находится пользователь.
    """
    raw = (getattr(state, "locked_distribution", {}) or {}).get(deal_id) \
       or (getattr(state, "locked_distribution", {}) or {}).get(str(deal_id)) \
       or {}
    if not isinstance(raw, dict):
        return None

    for slot, val in raw.items():
        seq: Iterable[Any] = val if isinstance(val, (list, tuple, set)) else [val]
        for item in seq:
            if await _label_belongs_to_uid(item, uid):
                return str(slot)
    return None


# --- [2] AMOCRM HELPERS (add-only теги и перевод статуса)
# --------------------------------------------------------
_TEAM_SUFFIXES: Set[str] = {".1", ".2", ".Адм", ".Стаж"}

def _is_team_tag(name: str) -> bool:
    return bool(name) and any(str(name).endswith(suf) for suf in _TEAM_SUFFIXES)

def _suffix_for_role(role: str) -> str:
    if role == "main": return "1"
    if role == "assist": return "2"
    if role == "admin": return "Адм"
    return "Стаж"

async def _amo_add_tag(lead_id: int, tag: str, *, slot_key: Optional[str] = None) -> bool:
    """
    Добавляет один командный тег в сделку НЕ перезаписывая остальные.
    Формат для services.amocrm.update_amocrm_tags:
        { "<lead_id>": { "<slot_key>": "<Имя Ф.Суф>" } }
    Например:
        { "29946795": { "lead1": "Дарья И.1" } }
    Если slot_key неизвестен — используем нейтральный "tag" (совместимость).
    """
    try:
        from services.amocrm import update_amocrm_tags  # lazy import
        key = str(slot_key or "tag")
        payload: Dict[str, Dict[str, str]] = {str(int(lead_id)): {key: str(tag)}}
        ok = await update_amocrm_tags(payload)
        return bool(ok)
    except Exception:
        logger.exception("[confirm] add tag failed lead=%s tag=%s slot=%s", lead_id, tag, slot_key)
        return False


# --- [2.1] HUMAN LABELS (ИМЯ ДЛЯ ТЕГА) — ИЗ locked_distribution
# --------------------------------------------------------------------
def _ensure_suffix(label: str, role: str) -> str:
    """
    Если подпись уже вида «Имя Ф.1/2/Адм/Стаж» — оставляем.
    Иначе аккуратно добавляем суффикс: если label оканчивается на «.», то «+SUF»,
    иначе «.+SUF». Пример: «Анна М.» + 1 > «Анна М.1».
    """
    label = (label or "").strip()
    if not label:
        return label
    if _is_team_tag(label):
        return label
    suf = _suffix_for_role(role)
    if label.endswith("."):
        return f"{label}{suf}"
    return f"{label}.{suf}"


def _labels_from_value(val: Any, role: str) -> List[str]:
    """Извлекаем подписи людей из значения слота, аккуратно добавляя суффиксы роли."""
    out: List[str] = []
    seq: Iterable[Any] = val if isinstance(val, (list, tuple, set)) else [val]
    for item in seq:
        s = str(item or "").strip()
        if not s:
            continue
        label = s.split("|", 1)[0].strip() or s
        out.append(_ensure_suffix(label, role))
    return out


def _expected_tag_map(deal_id: int) -> Dict[str, Set[str]]:
    """
    Ожидаемые текстовые теги формируем ИЗ ЗАФИКСИРОВАННОГО РАСПРЕДЕЛЕНИЯ.
    Возвращает {'main': {...}, 'assist': {...}, 'admin': {...}, 'trainee': {...}}.
    """
    raw = (getattr(state, "locked_distribution", {}) or {}).get(deal_id) \
       or (getattr(state, "locked_distribution", {}) or {}).get(str(deal_id)) \
       or {}
    out: Dict[str, Set[str]] = {"main": set(), "assist": set(), "admin": set(), "trainee": set()}
    if isinstance(raw, dict):
        for slot, val in raw.items():
            role = _role_alias(slot)
            if role not in out:
                continue
            for label in _labels_from_value(val, role):
                out[role].add(label)
    else:
        roles = normalize_roles(raw)
        for role, vals in roles.items():
            for label in _labels_from_value(vals, role):
                out.setdefault(role, set()).add(label)
    return out


def _label_for_user_from_locked(deal_id: int, uid: int, role: str) -> Optional[str]:
    """Ищем в locked_distribution подпись для конкретного uid и роли — «Имя Ф.Суф»."""
    raw = (getattr(state, "locked_distribution", {}) or {}).get(deal_id) \
       or (getattr(state, "locked_distribution", {}) or {}).get(str(deal_id)) \
       or {}
    if not isinstance(raw, dict):
        return None
    for slot, val in raw.items():
        if _role_alias(slot) != role:
            continue
        seq: Iterable[Any] = val if isinstance(val, (list, tuple, set)) else [val]
        for item in seq:
            s = str(item or "").strip()
            if not s:
                continue
            try:
                uids = set(map(int, to_uid_list(s)))
            except Exception:
                uids = set()
            if int(uid) in uids:
                label = s.split("|", 1)[0].strip()
                return _ensure_suffix(label, role)
    return None


# --- [3] CONFIRMATION CHECK — CRM>LOCAL fallback
# --------------------------------------------------------------------
async def _crm_confirmation_tags(deal_id: int) -> Set[str]:
    """Теги CRM по сделке (Set[str]). На ошибки/204 возвращаем пустое множество."""
    try:
        if hasattr(amo, "get_deal_by_id"):
            deal = await amo.get_deal_by_id(int(deal_id))  # type: ignore[arg-type]
            tags = (deal or {}).get("tags")
            if isinstance(tags, list):
                return {str(t.get("name") or "").strip() for t in tags if isinstance(t, dict) and t.get("name")}
    except Exception:
        logger.warning("[confirm] CRM tags unavailable for lead=%s (treating as empty)", deal_id)
    return set()


def _confirmed_from_state(deal_id: int) -> Dict[str, Set[int]]:
    """Локальные подтверждения (когда CRM недоступна)."""
    out: Dict[str, Set[int]] = {"main": set(), "assist": set(), "admin": set()}
    pc = (getattr(state, "pending_confirmations", {}) or {}).get(deal_id) or {}
    conf = pc.get("confirmed")
    if isinstance(conf, dict):
        for k in ("main", "assist", "admin"):
            vals = conf.get(k)
            if isinstance(vals, set):
                out[k] |= {int(x) for x in vals}
            elif vals:
                out[k] |= set(int(x) for x in to_uid_list(vals))
    elif isinstance(conf, set):
        assigned = _assigned_uids_from_locked(deal_id)
        for k in ("main", "assist", "admin"):
            out[k] |= (assigned.get(k, set()) & conf)
    return out


async def _all_required_confirmed(deal_id: int) -> bool:
    """True, если все назначенные роли подтвердили участие."""
    assigned = _assigned_uids_from_locked(deal_id)
    if not any(assigned.values()):
        return False

    expected = _expected_tag_map(deal_id)
    crm_tags = await _crm_confirmation_tags(deal_id)
    if crm_tags:
        for role in ("main", "assist", "admin"):
            need = len(assigned.get(role, set()))
            have = len([x for x in (expected.get(role) or set()) if x in crm_tags])
            if have < max(need, 0):
                return False
        return True

    local = _confirmed_from_state(deal_id)
    for role, uids in assigned.items():
        if uids and not (uids <= local.get(role, set())):
            return False
    return True

# История изменений [3]: 2025-08-18 — CRM>LOCAL фолбэк (SSOT)
#                       2025-08-19 — expected теги составляются из locked_distribution


# --- [3.1] STATUS READERS — определение статуса сделки (BRON/PRELIM/другое)
# --------------------------------------------------------------------
async def _read_status_info(deal_id: int) -> Tuple[Optional[str], str]:
    """
    Возвращает (status_id:str|None, status_name_lower:str).
    Источник истины — AmoCRM (get_deal_by_id). Фолбэки: current_poll_deals, deals_index.
    """
    # 1) CRM
    try:
        if hasattr(amo, "get_deal_by_id"):
            deal = await amo.get_deal_by_id(int(deal_id))  # type: ignore[arg-type]
            if isinstance(deal, dict):
                sid = deal.get("status_id") or deal.get("pipeline_status_id")
                name = str(deal.get("status_name") or deal.get("status") or "").strip().lower()
                return (str(sid) if sid is not None else None, name)
    except Exception:
        pass

    # 2) current_poll_deals
    with suppress(Exception):
        for d in (getattr(state, "current_poll_deals", []) or []):
            if int(d.get("id") or 0) == int(deal_id):
                sid = d.get("status_id") or d.get("pipeline_status_id")
                name = str(d.get("status_name") or d.get("status") or "").strip().lower()
                return (str(sid) if sid is not None else None, name)

    # 3) deals_index
    with suppress(Exception):
        meta = (getattr(state, "deals_index", {}) or {}).get(deal_id) \
            or (getattr(state, "deals_index", {}) or {}).get(str(deal_id)) \
            or {}
        sid = meta.get("status_id")
        name = str(meta.get("status_name") or meta.get("status") or "").strip().lower()
        return (str(sid) if sid is not None else None, name)

    return (None, "")


def _is_prelim(sid: Optional[str], name_lower: str) -> bool:
    if PRELIM_STATUS_ID and sid and str(sid) == PRELIM_STATUS_ID:
        return True
    return name_lower in {"предварительная заявка", "предварительно", "предварит."}


def _is_bron(sid: Optional[str], name_lower: str) -> bool:
    if BRON_STATUS_ID and sid and str(sid) == BRON_STATUS_ID:
        return True
    return name_lower == "бронь"

# --- [3.2] ALL-CONFIRMED ANNOUNCE — SSOT title/date/time/team
# --------------------------------------------------------------------
from typing import Any, Dict, List, Optional, Tuple
from contextlib import suppress

from core.state import state  # глобальный state нужен хелперам

def _deal_title(d: Dict[str, Any]) -> str:
    """
    SSOT для заголовка игры:
      game_name > name > f"Сделка #{id}"
    Идентично логике из handlers/polls_lifecycle.py.
    """
    try:
        return str(d.get("game_name") or d.get("name") or f"Сделка #{int(d.get('id') or 0)}").strip()
    except Exception:
        return f"Сделка #{d.get('id')}" if d and d.get("id") else "Сделка"

def _normalize_time_str(raw: Optional[str]) -> str:
    """
    Нормализует строку времени к 'HH:MM'. Примеры: '18.00'>'18:00', '930'>'09:30', '9'>'09:00'.
    Пустое/мусор > ''.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    s = s.replace(" ", "").replace(".", ":")
    if ":" not in s:
        if not s.isdigit():
            return ""
        if len(s) == 4:
            hh, mm = s[:2], s[2:]
        elif len(s) == 3:
            hh, mm = s[:1], s[1:]
        elif len(s) == 2:
            hh, mm = s, "00"
        else:
            hh, mm = s, "00"
        return f"{int(hh):02d}:{int(mm):02d}"
    try:
        h, m = s.split(":", 1)
        hh = int(h) if h else 0
        mm = int(m) if m else 0
        return f"{hh:02d}:{mm:02d}"
    except Exception:
        return ""

def _team_slots_for_announce(deal_id: int) -> Dict[str, Any]:
    """
    Источник состава для анонса (НЕ модифицирует state):
      1) state.locked_distribution[deal_id]
      2) state.finished_locked_distribution[deal_id] или state.finished_locked[deal_id]
      3) snapshot из state.distribution_cache[str(deal_id)]
    """
    raw = (getattr(state, "locked_distribution", {}) or {}).get(deal_id) \
       or (getattr(state, "locked_distribution", {}) or {}).get(str(deal_id))
    if isinstance(raw, dict) and raw:
        return raw

    fin = (getattr(state, "finished_locked_distribution", {}) or {}).get(deal_id) \
       or (getattr(state, "finished_locked_distribution", {}) or {}).get(str(deal_id)) \
       or (getattr(state, "finished_locked", {}) or {}).get(deal_id) \
       or (getattr(state, "finished_locked", {}) or {}).get(str(deal_id))
    if isinstance(fin, dict) and fin:
        return fin

    snap = (getattr(state, "distribution_cache", {}) or {}).get(str(deal_id)) or {}
    return snap if isinstance(snap, dict) else {}

def _date_time_for_announce(deal_id: int) -> Tuple[str, str]:
    """
    Возвращает (date_s, time_s) строго из кастомных полей CRM:
      • event_date, event_time (time > HH:MM, '.'>':');
      • фолбэк — время из event_datetime, если оно не '00:00'.
    Источники: state.current_poll_deals > state.deals_index.
    """
    date_s, time_s = "", ""

    # 1) текущий снапшот опроса
    with suppress(Exception):
        for d in (getattr(state, "current_poll_deals", []) or []):
            if int(d.get("id") or 0) == int(deal_id):
                date_s = str(d.get("event_date") or "").strip()
                time_s = _normalize_time_str(d.get("event_time"))
                if not time_s and d.get("event_datetime") and hasattr(d["event_datetime"], "strftime"):
                    t_dt = d["event_datetime"].strftime("%H:%M")
                    time_s = "" if t_dt == "00:00" else t_dt
                return (date_s or "", time_s or "")

    # 2) локальный индекс (например, собранный при опросе)
    with suppress(Exception):
        meta = (getattr(state, "deals_index", {}) or {}).get(deal_id) \
            or (getattr(state, "deals_index", {}) or {}).get(str(deal_id)) \
            or {}
        if isinstance(meta, dict) and meta:
            date_s = str(meta.get("event_date") or "").strip()
            time_s = _normalize_time_str(meta.get("event_time"))
            if not time_s and meta.get("event_datetime") and hasattr(meta["event_datetime"], "strftime"):
                t_dt = meta["event_datetime"].strftime("%H:%M")
                time_s = "" if t_dt == "00:00" else t_dt

    return (date_s or "", time_s or "")

async def _resolve_title_for_announce(deal_id: int) -> str:
    """
    Возвращает корректный заголовок для анонса:
      1) state.current_poll_deals > _deal_title
      2) state.deals_index > _deal_title
      3) AmoCRM: пытаемся достать game_name из custom_fields_values; если нет — используем name.
      4) Фолбэк: «Сделка #id»
    """
    did = int(deal_id)
    # 1) current_poll_deals
    with suppress(Exception):
        for d in (getattr(state, "current_poll_deals", []) or []):
            if int(d.get("id") or 0) == did:
                t = _deal_title(d)
                if t:
                    return t

    # 2) deals_index
    with suppress(Exception):
        meta = (getattr(state, "deals_index", {}) or {}).get(did) \
            or (getattr(state, "deals_index", {}) or {}).get(str(did)) \
            or {}
        if isinstance(meta, dict) and meta:
            t = _deal_title({"id": did, **meta})
            if t:
                return t

    # 3) AmoCRM (custom_field «game_name» приоритетнее, чем обычное name)
    with suppress(Exception):
        from services import amocrm as _amo  # type: ignore
        if hasattr(_amo, "get_deal_by_id"):
            deal = await _amo.get_deal_by_id(did)
            if isinstance(deal, dict) and deal:
                # Попробуем достать game_name из custom_fields_values
                cf_name = ""
                with suppress(Exception):
                    for cf in (deal.get("custom_fields_values") or []):
                        code = (cf.get("field_code") or "").lower()
                        if code in {"game_name", "gamename", "quest_name"}:
                            vals = cf.get("values") or []
                            if vals:
                                cf_name = str(vals[0].get("value") or "").strip()
                                if cf_name:
                                    break
                if cf_name:
                    return _deal_title({"id": did, "game_name": cf_name})
                # Иначе обычное name как мягкий фолбэк
                name = str(deal.get("name") or "").strip()
                if name:
                    return _deal_title({"id": did, "name": name})

    # 4) Фолбэк
    return f"Сделка #{did}"

async def _announce_all_confirmed(deal_id: int) -> None:
    """
    Единоразово шлёт в общий чат:
      «✅ Вся команда подтвердила участие.»
      «📅 «<Заголовок>» — ДД.ММ.ГГГГ ЧЧ:ММ»
      <буллет-список состава>
    Идемпотентность на процесс: не более одного анонса на deal_id.
    """
    try:
        did = int(deal_id)
    except Exception:
        return

    # антидубли
    announced: set[int] = state.__dict__.setdefault("_all_confirmed_announced", set())  # type: ignore[assignment]
    if did in announced:
        return

    # состав
    slots = _team_slots_for_announce(did)
    if not isinstance(slots, dict) or not slots:
        return

    # строки состава
    try:
        from core.utils import team_bulleted_lines  # type: ignore
        lines: List[str] = await team_bulleted_lines(slots)
    except Exception:
        lines = []

    # заголовок/дата/время
    title = await _resolve_title_for_announce(did)
    date_s, time_s = _date_time_for_announce(did)
    head = f"📅 «{title}»"
    tail = " ".join(x for x in (date_s, time_s) if x)
    if tail:
        head = f"{head} — {tail}"

    # куда слать
    try:
        from core.utils import resolve_notify_chat_id  # type: ignore
        chat_id = resolve_notify_chat_id()
    except TypeError:
        from core.utils import resolve_notify_chat_id  # type: ignore
        chat_id = resolve_notify_chat_id()
    if chat_id is None:
        return

    # текст
    parts: List[str] = ["? Вся команда подтвердила участие."]
    if head:
        parts.append(head)
    if lines:
        parts.append("\n".join(lines))
    text = "\n".join(p for p in parts if p)

    # отправка
    from aiogram import Bot as _Bot  # локальный импорт для типов
    bot = _Bot.get_current()
    await bot.send_message(chat_id, text)
    announced.add(did)

# История изменений:
#   • 2025-09-02 — выровнено под SSOT: title=game_name>name>fallback; время из custom fields; Pylance ok.



# --- [4.0] CALLBACK: CONFIRM ROLE — универсальный вход из деталей и других мест
# --------------------------------------------------------------------
@router.callback_query(lambda c: c.data and c.data.startswith(CONFIRM_PREFIX))
async def confirm_role_handler(callback: CallbackQuery) -> None:
    """
    Поддерживает оба формата:
      • confirm_participation_{deal_id}_{role}
      • confirm_participation_{deal_id}
      • legacy: confirm_role_{deal_id}_{role}
    Во втором случае роль определяется по зафиксированному составу.
    """
    data = str(callback.data or "")
    if data.startswith(CONFIRM_PREFIX):
        tail = data[len(CONFIRM_PREFIX):]
    elif data.startswith(LEGACY_CONFIRM_PREFIX):
        tail = data[len(LEGACY_CONFIRM_PREFIX):]
    else:
        await _safe_answer(callback, "⚠️ Ошибочные данные кнопки.", show_alert=True)
        return

    parts = tail.split("_")
    try:
        deal_id = int(parts[0])
    except Exception:
        await _safe_answer(callback, "⚠️ Ошибочные данные кнопки.", show_alert=True)
        return

    role = parts[1] if len(parts) > 1 and parts[1] else None
    if role is None:
        uid = int(callback.from_user.id)
        role = await _role_of_user_in_locked(uid, deal_id)

    if role not in {"main", "assist", "admin", "trainee"}:
        await _safe_answer(callback, "⚠️ Роль не найдена или не назначена на вас.", show_alert=True)
        return

    await _perform_confirm(callback, deal_id, role)

# --- [4.1] CORE HELPERS — идемпотентность + единый источник правды 
# --------------------------------------------------------------------
async def _role_of_user_in_locked(uid: int, deal_id: int) -> Optional[str]:
    """main/assist/admin/trainee/None — по зафиксированному составу (поддержка слотов без «|uid»)."""
    raw = (getattr(state, "locked_distribution", {}) or {}).get(deal_id) \
          or (getattr(state, "locked_distribution", {}) or {}).get(str(deal_id)) \
          or {}
    if not isinstance(raw, dict):
        return None

    for slot, val in raw.items():
        seq: Iterable[Any] = val if isinstance(val, (list, tuple, set)) else [val]
        for item in seq:
            if await _label_belongs_to_uid(item, uid):
                return _role_alias(slot)
    return None


async def _maybe_move_to_success(deal_id: int) -> None:
    """
    Переводит сделку в «Завершение сделки» ТОЛЬКО если:
      • все требуемые роли подтвердили участие (CRM>LOCAL);
      • текущий статус сделки — «Бронь»;
      • нет активного запроса замены (state.swap_open).
    Для «Предварительной заявки» и любого другого статуса — статус НЕ меняем.
    Уведомление «вся команда подтвердила» всегда отправляет handlers.my_games.announce_if_all_confirmed.

    Дополнения (2025-08-31):
    • После успешного перевода — мягкая «очистка замков» локальных подтверждений по сделке:
      state.pending_confirmations[deal_id], state.confirmed[deal_id], state.swap_requests[deal_id] (если были).
    • Синхронизация: безопасный вызов _sync_leader_report() и finish_if_all_deals_completed()
      (lazy-import, с подавлением ошибок), чтобы отчёт обновился и цикл мог завершиться сам.
    • Обновление локального состояния для логики скрытия: после добавления тегов и/или перевода в SUCCESS
      подтягиваем свежие теги/статус из CRM и сбрасываем кеш снапшотов для этой сделки.
    """
    try:
        moved_set: Set[int] = state.__dict__.setdefault("_moved_success", set())  # type: ignore[assignment]
        if int(deal_id) in moved_set:
            return

        # Локальный helper для обновления снапшота из CRM и сброса кеша
        async def _refresh_local_snapshot_for_hiding() -> None:
            with contextlib.suppress(Exception):
                ts = state.__dict__.setdefault("deal_snapshots_ts", {})  # type: ignore[assignment]
                if isinstance(ts, dict):
                    ts[int(deal_id)] = 0.0
                if hasattr(amo, "get_deal_by_id"):
                    snap = await amo.get_deal_by_id(int(deal_id))  # type: ignore[arg-type]
                else:
                    snap = None
                if isinstance(snap, dict):
                    # current_poll_deals — обновляем статусы и теги (для отчёта)
                    with contextlib.suppress(Exception):
                        for d in (getattr(state, "current_poll_deals", []) or []):
                            if int(d.get("id") or 0) == int(deal_id):
                                if "tags" in snap:
                                    d["tags"] = snap["tags"]
                                sid2 = snap.get("status_id") or snap.get("pipeline_status_id")
                                if sid2 is not None:
                                    d["status_id"] = sid2
                                sname2 = snap.get("status_name") or snap.get("status")
                                if sname2:
                                    d["status_name"] = sname2
                                break
                    # ВАЖНО: games_by_user НЕ меняем статус — чтобы «Мои игры» не пропадали.
                    # Разрешено аккуратно обновить только теги (без статуса).
                    with contextlib.suppress(Exception):
                        for _uid, arr in (getattr(state, "games_by_user", {}) or {}).items():
                            for d in (arr or []):
                                if int(d.get("id") or 0) == int(deal_id):
                                    if "tags" in snap:
                                        d["tags"] = snap["tags"]
                                    break

        expected_map: Dict[str, Set[str]] = _expected_tag_map(deal_id)
        expected_required: Set[str] = set()
        for k in ("main", "assist", "admin"):
            expected_required |= set(expected_map.get(k) or set())
        if not expected_required:
            logger.debug("[confirm] no expected tags for deal %s — skip status change", deal_id)
            return

        crm_tags: Set[str] = set(await _crm_confirmation_tags(deal_id) or [])
        all_ok = False
        if crm_tags and expected_required.issubset(crm_tags):
            all_ok = True
        else:
            assigned = _assigned_uids_from_locked(deal_id)
            local = _confirmed_from_state(deal_id)
            ok = True
            for role in ("main", "assist", "admin"):
                need = assigned.get(role, set())
                have = local.get(role, set())
                if need and not (need <= have):
                    ok = False
                    break
            all_ok = ok

        if not all_ok:
            return
        
            # Проверяем активный запрос замены
        if hasattr(state, 'swap_open') and deal_id in getattr(state, 'swap_open', {}):
            logger.debug("[confirm] swap request active for deal %s — skip status change", deal_id)
            return
        # Попытка явного скрытия из отчёта, если есть хук в polls_lifecycle
        try:
            from handlers.polls_lifecycle import hide_deal_from_report  # type: ignore
            with contextlib.suppress(Exception):
                await hide_deal_from_report(int(deal_id))
        except Exception:
            pass

        # Уже все подтвердили — синхронизация снапшота и отчёта
        with contextlib.suppress(Exception):
            await _refresh_local_snapshot_for_hiding()
        try:
            from handlers.polls_lifecycle import _sync_leader_report, finish_if_all_deals_completed  # type: ignore
            with contextlib.suppress(Exception):
                await _sync_leader_report()
            with contextlib.suppress(Exception):
                await finish_if_all_deals_completed()
        except Exception:
            pass

        # Доливаем недостающие теги в CRM при локальном подтверждении
        if not crm_tags:
            to_add = expected_required - crm_tags
            for tag in sorted(to_add):
                with contextlib.suppress(Exception):
                    await _amo_add_tag(deal_id, tag)
            await _refresh_local_snapshot_for_hiding()

        # текущее состояние статуса
        sid, name = await _read_status_info(deal_id)

        # ЕДИНЫЙ анонс — всегда через my_games (без дублей здесь)
        try:
            from handlers.my_games import announce_if_all_confirmed  # type: ignore
        except Exception:
            announce_if_all_confirmed = None  # type: ignore
        with contextlib.suppress(Exception):
            if callable(announce_if_all_confirmed):
                await announce_if_all_confirmed(int(deal_id))

        # На «Предварительной заявке» статус НЕ меняем
        if _is_prelim(sid, name):
            return

        # «Бронь» > «Завершение сделки»
        if _is_bron(sid, name) and SUCCESSFUL_STATUS_ID:
            with contextlib.suppress(Exception):
                await update_deal_status(int(deal_id), str(SUCCESSFUL_STATUS_ID))
                # Обновляем только current_poll_deals (для отчёта)
                try:
                    for d in (getattr(state, "current_poll_deals", []) or []):
                        if int(d.get("id") or 0) == int(deal_id):
                            d["status_id"] = int(SUCCESSFUL_STATUS_ID)
                            d["status_name"] = "Завершение сделки"
                except Exception:
                    pass
                # ВАЖНО: games_by_user НЕ трогаем (пусть остаётся в «Моих играх»)
                logger.info("[confirm] deal %d moved to SUCCESS (%s)", deal_id, SUCCESSFUL_STATUS_ID)
                # ? NEW (2025-08-31): скрыть из отчёта и ПЕРЕНЕСТИ «замки» в finished_* вместо удаления
                try:
                    from handlers.polls_lifecycle import hide_deal_from_report, finish_if_all_deals_completed  # type: ignore
                    with contextlib.suppress(Exception):
                        await hide_deal_from_report(int(deal_id))  # 1) скрыть из отчёта

                    # 2) перенос «замков» и кэша автораспределения в резерв (int и str ключи)
                    try:
                        finished_ld = state.__dict__.setdefault("finished_locked_distribution", {})
                        finished_dc = state.__dict__.setdefault("finished_distribution_cache", {})
                        src_ld = (getattr(state, "locked_distribution", {}) or {})
                        src_dc = (getattr(state, "distribution_cache", {}) or {})
                        for k in (int(deal_id), str(deal_id)):
                            if k in src_ld:
                                finished_ld[k] = src_ld.pop(k)
                            if k in src_dc:
                                finished_dc[k] = src_dc.pop(k)
                    except Exception:
                        logger.debug("[confirm] move locks to finished_* skipped (no-op) deal=%s", deal_id)

                    logger.info("[confirm] hidden in report & moved to finished_locked: %s", int(deal_id))

                    # 3) возможное завершение цикла
                    with contextlib.suppress(Exception):
                        await finish_if_all_deals_completed()
                except Exception:
                    pass
                moved_set.add(int(deal_id))

            # NEW: очистка локальных подтверждений/обменов
            try:
                pc = getattr(state, "pending_confirmations", None)
                if isinstance(pc, dict):
                    pc.pop(int(deal_id), None)
            except Exception:
                pass
            try:
                conf = getattr(state, "confirmed", None)
                if isinstance(conf, dict):
                    conf.pop(int(deal_id), None)
            except Exception:
                pass
            try:
                swaps = getattr(state, "swap_requests", None)
                if isinstance(swaps, dict):
                    swaps.pop(int(deal_id), None)
            except Exception:
                pass

            # После перевода — ещё раз обновим снапшот отчёта (без изменения «Моих игр»)
            await _refresh_local_snapshot_for_hiding()
            try:
                from handlers.polls_lifecycle import _sync_leader_report, finish_if_all_deals_completed  # type: ignore
                with contextlib.suppress(Exception):
                    await _sync_leader_report()
                with contextlib.suppress(Exception):
                    await finish_if_all_deals_completed()
            except Exception:
                pass

        elif _is_bron(sid, name) and not SUCCESSFUL_STATUS_ID:
            logger.warning("[confirm] SUCCESS status id not configured; lead=%s", deal_id)
            # даже если не можем перевести — отчёт уже синхронизирован выше

    except Exception as e:
        logger.warning("[confirm] _maybe_move_to_success failed: %s", e)



async def _perform_confirm(callback: CallbackQuery, deal_id: int, role: str) -> None:
    uid = int(callback.from_user.id)
    now_ts = int(time.time())
    user_role = await _role_of_user_in_locked(uid, deal_id)
    if user_role != role:
        await _safe_answer(callback, "⚠️ Вы не назначены на эту роль.", show_alert=True)
        return

    state.__dict__.setdefault("pending_confirmations", {})
    state.__dict__.setdefault("confirmed", {})

    expected = _expected_tag_map(deal_id)
    crm_tags = await _crm_confirmation_tags(deal_id)
    local = _confirmed_from_state(deal_id)
    human_expected = expected.get(role, set())
    already = (crm_tags and any(tag in crm_tags for tag in human_expected)) or (uid in local.get(role, set()))
    if already:
        kb = _mark_confirmed_on_message_kb(callback, deal_id, role)
        with contextlib.suppress(Exception):
            if kb:
                await callback.message.edit_reply_markup(reply_markup=kb)
        await _safe_answer(callback, "Уже подтверждено ✅")
        return

    human = await short_name(uid)
    tag_text = _label_for_user_from_locked(deal_id, uid, role)
    if not tag_text:
        suffix = _suffix_for_role(role)
        tag_text = f"{human}{suffix}" if human.endswith(".") else f"{human}.{suffix}"
    else:
        # Берем имя+суффикс до | из SSOT формата
        tag_text = tag_text.split('|')[0].strip()

    slot_key = await _slot_key_for_user(deal_id, uid)
    # Используем services.amocrm.update_amocrm_tags с частью до | из формата
    with contextlib.suppress(Exception):
        from services.amocrm import update_amocrm_tags
        payload = {str(deal_id): {slot_key or "tag": tag_text}}
        await update_amocrm_tags(payload)

    pc = state.pending_confirmations.setdefault(deal_id, {})
    if not isinstance(pc.get("confirmed"), dict):
        pc["confirmed"] = {}
    (pc["confirmed"].setdefault(role, set())).add(uid)  # type: ignore[index]
    state.confirmed.setdefault(deal_id, set()).add(uid)

    pending_map = pc.get("pending")
    if isinstance(pending_map, dict):
        role_pending = pending_map.get(role)
        if isinstance(role_pending, set):
            role_pending.discard(uid)
            if not role_pending:
                pending_map.pop(role, None)
        if not pending_map:
            pc.pop("pending", None)
    pending_uids = pc.get("pending_uids")
    if isinstance(pending_uids, set):
        pending_uids.discard(int(uid))
        if not pending_uids:
            pc.pop("pending_uids", None)
    try:
        pc_entry = state.pending_confirmations.get(deal_id, {}) if isinstance(getattr(state, "pending_confirmations", {}), dict) else {}
        assign_map = pc_entry.get("assign_ts", {}) if isinstance(pc_entry, dict) else {}
        assigned_at = assign_map.get(int(uid)) or assign_map.get(str(uid)) or now_ts
        await record_event(
            uid,
            "confirm",
            {"deal_id": str(deal_id), "t_assign": int(assigned_at)},
            deal_id=str(deal_id),
        )
        urgent_award = getattr(state, "urgent_swap_award", {})
        if isinstance(urgent_award, dict):
            award_uid = urgent_award.get(int(deal_id)) or urgent_award.get(str(deal_id))
            if award_uid and int(award_uid) == uid:
                await record_event(
                    uid,
                    "urgent_replacement",
                    {"deal_id": str(deal_id), "reason": "urgent_swap"},
                    deal_id=str(deal_id),
                )
                urgent_award.pop(int(deal_id), None)
                urgent_award.pop(str(deal_id), None)
        # Доп. совместимость: если кандидат помечен через handlers/swap (swap_urgent_candidates)
        try:
            cand_map = getattr(state, "swap_urgent_candidates", {}) or {}
            cand_uid = cand_map.get(int(deal_id)) or cand_map.get(str(deal_id))
            if cand_uid and int(cand_uid) == int(uid):
                await record_event(
                    uid,
                    "urgent_replacement",
                    {"deal_id": str(deal_id)},
                    when=now_ts,
                    deal_id=str(deal_id),
                )
                # очистим флаг
                try:
                    cand_map.pop(int(deal_id), None)
                    cand_map.pop(str(deal_id), None)
                except Exception:
                    pass
        except Exception:
            logger.debug("[rating] swap_urgent_candidates hook failed")
    except Exception as exc:
        logger.debug("[rating] confirm hook failed: %s", exc)


    swaps = getattr(state, "swap_requests", {}) or {}
    swap_entry = swaps.get(deal_id) or swaps.get(str(deal_id))
    if isinstance(swaps, dict) and isinstance(swap_entry, dict):
        try:
            if int(swap_entry.get("accepted_by") or 0) == uid:
                swaps.pop(deal_id, None)
                swaps.pop(str(deal_id), None)
            else:
                swap_entry["awaiting_confirmation"] = False
                swap_entry["confirmed_at"] = now_ts
                swaps[int(deal_id)] = swap_entry
        except Exception:
            logger.debug("[confirm] swap cleanup skipped")
        finally:
            state.swap_requests = swaps  # type: ignore[attr-defined]

    replacements = getattr(state, "swap_replacements", {}) or {}
    repl_entry = replacements.get(deal_id) or replacements.get(str(deal_id))
    if isinstance(repl_entry, dict):
        with contextlib.suppress(Exception):
            if int(repl_entry.get("candidate") or 0) == uid:
                repl_entry["confirmed"] = True
                repl_entry["confirmed_at"] = now_ts
                replacements[int(deal_id)] = repl_entry
                state.swap_replacements = replacements  # type: ignore[attr-defined]

    try:
        if refresh_deal_details:
            opened = getattr(state, "detail_blocks", {}).get(uid, {})
            if opened and deal_id in opened:
                await refresh_deal_details(bot=callback.message.bot, uid=uid, deal_id=deal_id)  # type: ignore[arg-type]
    except Exception:
        logger.debug("[confirm] details refresh skipped")

    # Тихая перекраска дашборда сразу после подтверждения > «✅ Подтверждено»
    applied_ui = False
    with contextlib.suppress(Exception):
        from handlers.my_games import mygames_after_confirm_ui_patch as _mg_after  # type: ignore
        await _mg_after(uid, deal_id)
        applied_ui = True

    if not applied_ui:
        kb = _mark_confirmed_on_message_kb(callback, deal_id, role)
        with contextlib.suppress(Exception):
            if kb:
                await callback.message.edit_reply_markup(reply_markup=kb)

    # уведомление в общий чат
    try:
        bot = callback.message.bot if callback.message else None
        if bot:
            try:
                chat_id = resolve_notify_chat_id(bot)  # type: ignore[arg-type]
            except TypeError:
                chat_id = resolve_notify_chat_id()     # type: ignore[call-arg]
            if chat_id is not None:
                title = _deal_title_from_state(deal_id)
                d_s, t_s = _deal_when(deal_id)
                when = f"{d_s} {t_s}".strip()

                role_human_map = {"main": "Ведущий", "assist": "Помощник", "admin": "Админ", "trainee": "Стажёр"}
                # main role emoji should be theatrical mask
                role_emoji_map = {"main": "�", "assist": "🤝", "admin": "🛡️", "trainee": "👷"}
                role_human = role_human_map.get(role, "Участник")
                r = role_emoji_map.get(role, "👤")

                base = f"{human} подтвердил выход на игру «{title}»"
                if when:
                    base = f"{base} {when}"
                detail = f"{r} {role_human}".strip()
                if detail:
                    base = f"{base}. {detail}"
                text = base if base.endswith('.') else f"{base}."
                notified: Set[Tuple[int, int, str]] = state.__dict__.setdefault("_confirm_notified", set())  # type: ignore[assignment]
                key = (int(deal_id), int(uid), str(role))
                if key not in notified:
                    await bot.send_message(chat_id, text)
                    notified.add(key)
    except Exception:
        logger.exception("[confirm] notify failed")

    with contextlib.suppress(Exception):
        await _maybe_move_to_success(deal_id)

    # Повторная тихая перекраска после возможного перевода в SUCCESS > «🔁 Замена»
    with contextlib.suppress(Exception):
        from handlers.my_games import mygames_after_confirm_ui_patch as _mg_after  # type: ignore
        await _mg_after(uid, deal_id)

    with contextlib.suppress(Exception):
        await _safe_answer(callback, "Готово ✅")

    # Общий refresh/перерисовка кнопок в «Мои игры» и деталях
    try:
        from handlers.my_games import refresh_all_user_games
        with contextlib.suppress(Exception):
            await refresh_all_user_games(uid)
    except Exception:
        pass

# История изменений:
# 2025-08-31 · скрытие игры из отчёта и перенос «замков» в finished_* вместо удаления (сохранение в «Моих играх» до пост-отчёта)
# 2025-09-22 — подтверждения: блокировка при pending swap; авто-перевод статуса после закрытия




# --- [4.2] CALLBACK: CONFIRM FROM «Мои игры»
# --------------------------------------------------------------------
@router.callback_query(lambda c: c.data and c.data.startswith("mygames_confirm_"))
async def confirm_from_mygames_handler(callback: CallbackQuery) -> None:
    """
    Коллбэк из дашборда: mygames_confirm_{deal_id}
    • Определяем роль пользователя из locked_distribution;
    • Запускаем стандартный пайплайн подтверждения (_perform_confirm);
    • Без автопереходов и «прыжков» UI.
    """
    data = str(callback.data or "")
    try:
        deal_id = int(data.split("_")[-1])
    except Exception:
        await _safe_answer(callback, "⚠️ Ошибочная кнопка.", show_alert=True)
        return

    uid = int(callback.from_user.id)
    assigned = _assigned_uids_from_locked(deal_id)

    role: Optional[str] = None
    if uid in assigned.get("admin", set()):
        role = "admin"
    elif uid in assigned.get("main", set()):
        role = "main"
    elif uid in assigned.get("assist", set()):
        role = "assist"
    elif uid in assigned.get("trainee", set()):
        role = "trainee"

    if not role:
        await _safe_answer(callback, "Роль не найдена или не назначена на вас.", show_alert=True)
        return

    try:
        ui = getattr(state, "ui_context", None)
        if not isinstance(ui, dict):
            ui = {}
        ui[int(uid)] = "my_games"
        state.ui_context = ui  # type: ignore[assignment]
    except Exception:
        pass

    await _perform_confirm(callback, deal_id, role)



# --- [99] SELF-TEST (минимальный)
# --------------------------------------------------------------------
async def _test() -> None:
    # to_uid_list
    assert to_uid_list("Иван И.|101") == [101]
    assert set(to_uid_list(["101", 202, "Петр|303"])) == {101, 202, 303}

    # role alias
    assert _role_alias("lead1") == "main"
    assert _role_alias("assistant2") == "assist"
    assert _role_alias("admin") == "admin"

    # assigned_uids: смешанный формат (списки + слоты)
    state.locked_distribution = {
        1: {"main": ["Иван И.1|101"], "assistant1": "Петр П.2|202", "admin": "Света С.Адм|303"},
    }
    a = _assigned_uids_from_locked(1)
    assert a["main"] == {101} and a["assist"] == {202} and a["admin"] == {303}

    # expected_tag_map: строго из подписей слотов (до |uid)
    em = _expected_tag_map(1)
    assert "Иван И.1" in em["main"] and "Петр П.2" in em["assist"] and "Света С.Адм" in em["admin"]

    # label_for_user: точная подпись из locked
    assert _label_for_user_from_locked(1, 101, "main") == "Иван И.1"
    assert _label_for_user_from_locked(1, 202, "assist") == "Петр П.2"
    assert _label_for_user_from_locked(1, 303, "admin") == "Света С.Адм"

    # статус: имитация «Бронь»
    state.current_poll_deals = [{"id": 2, "status_id": int(BRON_STATUS_ID) if BRON_STATUS_ID else 12345}]
    state.deal_titles = {2: "Тестовая игра"}

    print("handlers.confirmations ? tests passed")


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(_test())

# История изменений [99]:
# 2025-08-18 — self-test под SSOT.
# 2025-08-19 — проверки формата тегов из locked_distribution.
# 2025-08-19 — _read_status_info и строгая логика перевода в SUCCESS (только из «Бронь»).
# 2025-08-24 — предотвращён автопереход в детали при подтверждении из «Мои игры».
# 2025-09-17 · модуль рейтинга: выровнено под SSOT.
# 2025-01-20 — проверка активного запроса замены (swap_open); теги из SSOT формата до |.
# 2025-09-22 — подтверждения: блокировка при pending swap; авто-перевод статуса после закрытия.

