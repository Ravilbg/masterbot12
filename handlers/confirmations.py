# handlers/confirmations.py
# ─────────────────────────────────────────────────────────────────────────────
"""
Подтверждения участия ведущими.

Версия v15.9 · 2025-08-24
──────────────────────────────────────────────────────────────────────────────
• SSOT: short_name/to_uid_list/normalize_roles/resolve_notify_chat_id из core.utils.
• «✅ Подтвердить» ставит персональный тег в AmoCRM (add-only, без перезаписи).
• Надёжное определение роли и состава из locked_distribution (списки и слоты).
• ВАЖНО: Формат тегов строго берётся из locked_distribution → «Имя Ф.1/2/Адм/Стаж».
  (Берём подпись до «|uid», при отсутствии суффикса — аккуратно добавляем.)
• Локальная отметка кнопки на «✅ Подтверждено» без «прыжков» UI.
• Уведомление в общий чат: «✅ Участие подтверждено: Имя Ф. — 🎭 Роль на „Название“ ДД.ММ ЧЧ:ММ».
• Проверка полноты подтверждений: CRM-теги → локальный кэш (fallback).
• Автоперевод в статус «Завершение сделки» — ТОЛЬКО из «Бронь», если все роли подтверждены.
• Совместимость с aiogram 3.x и Pylance (строгие типы).
• ФИКС: после подтверждения из «Мои игры» НЕ открываем детали отчёта; редрав только у зрителей деталей.
"""

from __future__ import annotations

# ███ [0] IMPORTS & CONSTANTS
# --------------------------------------------------------------------
import contextlib
import logging
from contextlib import suppress
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

from core.config import settings
from core.state import state
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
CONFIRM_PREFIX = "confirm_role_"

# Статусы из настроек
BRON_STATUS_ID        = str(getattr(settings, "BRON_STATUS_ID", "") or "")
SUCCESSFUL_STATUS_ID  = str(getattr(settings, "SUCCESSFUL_STATUS_ID", "") or "")
PRELIM_STATUS_ID      = str(getattr(settings, "PRELIM_STATUS_ID", getattr(settings, "PRELIMINARY_STATUS_ID", "") or "") or "")

# История изменений [0]:
# 2025-08-18 — выровнено под SSOT, убраны локальные дубли, фиксы Pylance
# 2025-08-19 — фикс формата тегов: берём из locked_distribution (подпись до |uid)
# 2025-08-19 — перевод в SUCCESS только из «Бронь» (жёсткая проверка статуса)
# 2025-08-24 — ФИКС: убран автопереход в детали из «Мои игры», редрав только у зрителей деталей


# ███ [0.1] SAFE HELPERS
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


# ███ [1] ROLE/ASSIGNMENT HELPERS
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
    """Локально меняем кнопку «Подтвердить» на «✅ Подтверждено» в текущем сообщении."""
    try:
        msg = getattr(callback, "message", None)
        kb = getattr(msg, "reply_markup", None)
        if not isinstance(kb, InlineKeyboardMarkup):
            return None
        new_rows: List[List[InlineKeyboardButton]] = []
        target_cd = f"{CONFIRM_PREFIX}{deal_id}_{role}"
        for row in (kb.inline_keyboard or []):
            new_row: List[InlineKeyboardButton] = []
            for btn in row:
                cd = (getattr(btn, "callback_data", "") or "")
                if cd == target_cd:
                    new_row.append(InlineKeyboardButton(text="✅ Подтверждено", callback_data="noop"))
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


# ███ [2] AMOCRM HELPERS (add-only теги и перевод статуса)
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



# ███ [2.1] HUMAN LABELS (ИМЯ ДЛЯ ТЕГА) — ИЗ locked_distribution
# --------------------------------------------------------------------
def _ensure_suffix(label: str, role: str) -> str:
    """
    Если подпись уже вида «Имя Ф.1/2/Адм/Стаж» — оставляем.
    Иначе аккуратно добавляем суффикс: если label оканчивается на «.», то «+SUF»,
    иначе «.+SUF». Пример: «Анна М.» + 1 → «Анна М.1».
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


# ███ [3] CONFIRMATION CHECK — CRM→LOCAL fallback
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

# История изменений [3]: 2025-08-18 — CRM→LOCAL фолбэк (SSOT)
#                       2025-08-19 — expected теги составляются из locked_distribution


# ███ [3.1] STATUS READERS — определение статуса сделки (BRON/PRELIM/другое)
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


# ════════════════════════════════════════════════════════════════════
# ███ [4] CALLBACK: CONFIRM ROLE — идемпотентность + единый источник правды
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
      • все требуемые роли подтвердили участие (CRM→LOCAL);
      • текущий статус сделки — «Бронь».
    Для «Предварительной заявки» и любого другого статуса — НЕ переводим.
    Всегда отправляем групповое уведомление «вся команда подтвердила...» отдельно.
    """
    try:
        # Антидубли на процесс: один перевод за сессию
        moved_set: Set[int] = state.__dict__.setdefault("_moved_success", set())  # type: ignore[assignment]
        if int(deal_id) in moved_set:
            return

        # 0) Проверка комплектности ожидаемых тегов
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
            missing = (expected_required - crm_tags) if crm_tags else expected_required
            logger.debug("[confirm] deal %s tags missing: %s", deal_id, ", ".join(sorted(missing)))
            return

        # best-effort: если каких-то тегов нет в CRM — добавим (не критично)
        if not crm_tags:
            to_add = expected_required - crm_tags
            for tag in sorted(to_add):
                with contextlib.suppress(Exception):
                    await _amo_add_tag(deal_id, tag)

        # 1) Читаем статус сделки (CRM→state), решение строгое
        sid, name = await _read_status_info(deal_id)
        if _is_prelim(sid, name):
            can_move = False
        elif _is_bron(sid, name):
            can_move = True
        else:
            can_move = False

        # уведомление «вся команда подтвердила»
        try:
            from handlers.my_games import announce_if_all_confirmed  # type: ignore
        except Exception:
            announce_if_all_confirmed = None  # type: ignore
        with contextlib.suppress(Exception):
            if callable(announce_if_all_confirmed):
                await announce_if_all_confirmed(int(deal_id))

        if not can_move or not SUCCESSFUL_STATUS_ID:
            if not SUCCESSFUL_STATUS_ID:
                logger.warning("[confirm] SUCCESS status id not configured; lead=%s", deal_id)
            return

        # 2) Перевод в «Завершение сделки»
        with contextlib.suppress(Exception):
            await update_deal_status(int(deal_id), str(SUCCESSFUL_STATUS_ID))
            # Локальный кэш: актуализируем статусы в открытках
            try:
                for d in (getattr(state, "current_poll_deals", []) or []):
                    if int(d.get("id") or 0) == int(deal_id):
                        d["status_id"] = int(SUCCESSFUL_STATUS_ID)
                        d["status_name"] = "Завершение сделки"
            except Exception:
                pass
            try:
                for _uid, arr in (getattr(state, "games_by_user", {}) or {}).items():
                    for d in (arr or []):
                        if int(d.get("id") or 0) == int(deal_id):
                            d["status_id"] = int(SUCCESSFUL_STATUS_ID)
                            d["status_name"] = "Завершение сделки"
            except Exception:
                pass
            logger.info("[confirm] deal %d moved to SUCCESS (%s)", deal_id, SUCCESSFUL_STATUS_ID)
            moved_set.add(int(deal_id))

    except Exception as e:
        logger.warning("[confirm] _maybe_move_to_success failed: %s", e)


async def _perform_confirm(callback: CallbackQuery, deal_id: int, role: str) -> None:
    uid = int(callback.from_user.id)
    user_role = await _role_of_user_in_locked(uid, deal_id)
    if user_role != role:
        await _safe_answer(callback, "⛔ Вы не назначены на эту роль.", show_alert=True)
        return

    # init locals
    state.__dict__.setdefault("pending_confirmations", {})
    state.__dict__.setdefault("confirmed", {})

    expected = _expected_tag_map(deal_id)
    crm_tags = await _crm_confirmation_tags(deal_id)
    local = _confirmed_from_state(deal_id)
    human_expected = expected.get(role, set())
    already = (crm_tags and any(tag in crm_tags for tag in human_expected)) or (uid in local.get(role, set()))
    if already:
        # UI фолбэк (если «Мои игры» — ниже мы всё равно вызовем общий UI-патч)
        kb = _mark_confirmed_on_message_kb(callback, deal_id, role)
        with contextlib.suppress(Exception):
            if kb:
                await callback.message.edit_reply_markup(reply_markup=kb)
        await _safe_answer(callback, "Уже подтверждено ✅")
        return

    # тег: строго из locked_distribution (подпись до |uid)
    human = await short_name(uid)
    tag_text = _label_for_user_from_locked(deal_id, uid, role)
    if not tag_text:
        suffix = _suffix_for_role(role)
        tag_text = f"{human}{suffix}" if human.endswith(".") else f"{human}.{suffix}"

    # NEW: ключ точного слота для update_amocrm_tags
    slot_key = await _slot_key_for_user(deal_id, uid)
    with contextlib.suppress(Exception):
        await _amo_add_tag(deal_id, tag_text, slot_key=slot_key)

    # локальная отметка
    pc = state.pending_confirmations.setdefault(deal_id, {})
    if not isinstance(pc.get("confirmed"), dict):
        pc["confirmed"] = {}
    (pc["confirmed"].setdefault(role, set())).add(uid)  # type: ignore[index]
    state.confirmed.setdefault(deal_id, set()).add(uid)

    # NEW: UI после подтверждения — если пользователь сейчас в «Моих играх», отдаём патч туда
    applied_ui = False
    try:
        ui_ctx = (getattr(state, "ui_context", {}) or {}).get(uid)
        if ui_ctx == "my_games":
            try:
                from handlers.my_games import mygames_after_confirm_ui_patch as _mg_after  # type: ignore
                if callable(_mg_after):
                    await _mg_after(uid, deal_id, role, message=getattr(callback, "message", None))
                    applied_ui = True
            except Exception:
                pass
    except Exception:
        pass

    # Фолбэк — просто перекрасить кнопку «✅ Подтверждено», если «Моих игр» рядом нет
    if not applied_ui:
        kb = _mark_confirmed_on_message_kb(callback, deal_id, role)
        with contextlib.suppress(Exception):
            if kb:
                await callback.message.edit_reply_markup(reply_markup=kb)

    # Уведомление о личном подтверждении
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
                role_emoji_map = {"main": "🎭", "assist": "🤝", "admin": "🛡️", "trainee": "🧪"}
                role_human = role_human_map.get(role, "Участник")
                r = role_emoji_map.get(role, "🎯")

                base = f"✅ Участие подтверждено: {human} — {r} {role_human} на «{title}»"
                text = f"{base} {when}." if when else f"{base}."
                notified: Set[Tuple[int, int, str]] = state.__dict__.setdefault("_confirm_notified", set())  # type: ignore[assignment]
                key = (int(deal_id), int(uid), str(role))
                if key not in notified:
                    await bot.send_message(chat_id, text)
                    notified.add(key)
    except Exception:
        logger.exception("[confirm] notify failed")

    # Проверка комплектности + возможный перевод статуса (строго из «Бронь»)
    with contextlib.suppress(Exception):
        await _maybe_move_to_success(deal_id)

    with contextlib.suppress(Exception):
        await _safe_answer(callback, "Готово ✅")



# История изменений [4]:
# 2025-08-18 — добавлен _maybe_move_to_success; resolve_notify_chat_id — sync (SSOT).
# 2025-08-19 — при проставлении тега берём подпись из locked_distribution (строго «Имя Ф.Суф»).
# 2025-08-19 — _maybe_move_to_success: переводить только из «Бронь»; антидубли перевода.
# 2025-08-24 — отключён редрав деталей при подтверждении (во избежание «прыжка» из «Мои игры»).



# ███ [4.0] CALLBACK: CONFIRM BUTTON «confirm_role_{deal}_{role}»
@router.callback_query(lambda c: c.data and c.data.startswith(CONFIRM_PREFIX))
async def confirm_role_handler(callback: CallbackQuery) -> None:
    """
    Коллбэк из карточек/деталей: confirm_role_{deal_id}_{role}
    • Не делаем навигаций — только локальная перекраска кнопки и пайплайн подтверждения.
    """
    data = str(callback.data or "")
    try:
        parts = data.split("_")
        deal_part = parts[-2]
        role = parts[-1]
        deal_id = int(deal_part)
    except Exception:
        await _safe_answer(callback, "⚠️ Ошибочная кнопка.", show_alert=True)
        return

    role = _role_alias(role)
    if role not in {"main","assist","admin","trainee"}:
        await _safe_answer(callback, "⚠️ Роль не распознана.", show_alert=True)
        return

    await _perform_confirm(callback, deal_id, role)  # стажёр поддерживается (ставим тег «.Стаж»)


# ███ [4.1] CALLBACK: CONFIRM FROM «Мои игры»
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

    # Приоритет: admin → main → assist → trainee.
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

    # Помечаем UI-контекст пользователя — это поможет и другим модулям, если нужно.
    try:
        ui = getattr(state, "ui_context", None)
        if not isinstance(ui, dict):
            ui = {}
        ui[int(uid)] = "my_games"
        state.ui_context = ui  # type: ignore[assignment]
    except Exception:
        pass

    await _perform_confirm(callback, deal_id, role)

# История изменений [4.1]: 2025-08-18 — базовая логика; 2025-08-24 — пометка ui_context для «Мои игры».


# ███ [99] SELF-TEST (минимальный)
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
        1: {"main": [ "Иван И.1|101" ], "assistant1": "Петр П.2|202", "admin": "Света С.Адм|303"},
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

    print("handlers.confirmations ✅ tests passed")


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(_test())

# История изменений [99]:
# 2025-08-18 — self-test под SSOT.
# 2025-08-19 — проверки формата тегов из locked_distribution.
# 2025-08-19 — _read_status_info и строгая логика перевода в SUCCESS (только из «Бронь»).
# 2025-08-24 — предотвращён автопереход в детали при подтверждении из «Мои игры».