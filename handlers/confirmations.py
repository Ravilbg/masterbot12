# handlers/confirmations.py
# ─────────────────────────────────────────────────────────────────────────────
"""
Подтверждения участия ведущими.

Версия v15.0 · 2025-08-12
──────────────────────────────────────────────────────────────────────────────
• Поддержка sync/async core.db.get_user_info (фикса падения AttributeError).
• Теги в AmoCRM ставятся ТОЛЬКО по «✅ Подтвердить», «Утвердить» теги не трогает.
• Проверка полноты подтверждений: по фактическим тегам CRM и/или details.confirmed.
• Совместимость форматов состава: списки uid (main/assist/admin) и слоты lead*/assistant*/admin "Имя|uid".
• Безопасные вызовы AmoCRM (несколько API-вариантов), устойчивость к 204/пустым данным.
• Мягкая интеграция с «🎲 Мои игры»: экспорт CONFIRM_PREFIX, после подтверждения делаем redraw.
"""

from __future__ import annotations
from __future__ import annotations
from __future__ import annotations
# ███ [0] IMPORTS & CONSTANTS
# --------------------------------------------------------------------
import inspect
import logging
from typing import Any, Dict, Iterable, List, Optional, Set

from aiogram import Router, types
from aiogram.types import CallbackQuery

from core.config import settings
from core.state import state

# AmoCRM (универсальные обёртки)
from services import amocrm as amo  # type: ignore

# Детали сделки — универсальный рендер/источник правды
from handlers.poll_details import refresh_deal_details  # type: ignore

# Профиль: get_user_info может быть sync или async — обработаем оба случая
try:
    from core.db import get_user_info  # type: ignore
except Exception:  # pragma: no cover
    get_user_info = None  # type: ignore

logger = logging.getLogger(__name__)
router = Router(name="confirmations")

# Префикс для inline-кнопок подтверждения
CONFIRM_PREFIX = "confirm_role_"

# Идентификаторы статусов (используем только успешный)
OK_STATUS_ID: Optional[str] = str(getattr(settings, "SUCCESSFUL_STATUS_ID", "") or "") or None

# История изменений [0]: 2025-08-13 — упрощены константы, импортированы amo + refresh_deal_details


# Чат для уведомлений (любой из доступных; порядок приоритета)
ADMIN_CHAT_ID: Optional[int] = (
    getattr(state, "admin_chat_id", None)
    or getattr(settings, "POLLS_CHAT_ID", None)
    or getattr(settings, "LEADERS_CHAT_ID", None)
    or getattr(settings, "ADMIN_CHAT_ID", None)
)


# ────────────────────────────────────────────────────────────────────
# [1] NAME/ROLE/TAG HELPERS
# ────────────────────────────────────────────────────────────────────
async def _short_name(uid: int) -> str:
    """
    Возвращает «Имя Ф.» (с точкой).
    • сначала core.db.get_user_info (если корутина — await; если sync — прямой вызов),
    • затем state.users,
    • иначе uid.
    """
    # 1) core.db.get_user_info (sync/async)
    if callable(get_user_info):
        try:
            if inspect.iscoroutinefunction(get_user_info):  # async версия
                ui = await get_user_info(uid)  # type: ignore
            else:  # sync версия
                ui = get_user_info(uid)  # type: ignore
        except Exception:
            ui = None
        if isinstance(ui, dict):
            first = (ui.get("first_name") or "").strip()
            last_ini = (ui.get("last_name_initial") or ui.get("last_name_i") or "").strip()
            base = f"{first} {last_ini}".strip()
            if base:
                return base

    # 2) fallback — state.users
    try:
        u = (getattr(state, "users", {}) or {}).get(uid) or {}
        first = (u.get("first_name") or "").strip()
        last_ini = (u.get("last_name_initial") or "").strip()
        base = f"{first} {last_ini}".strip()
        if base:
            return base
    except Exception:
        pass

    # 3) uid
    return str(uid)


def _role_suffix(role: str) -> str:
    """Суффикс для тега по роли (унифицировано с распределением)."""
    return {"main": ".1", "assist": ".2", "admin": ".Адм", "trainee": ".Стаж"}.get(role, "")


def _to_uid_list(v: Any) -> List[int]:
    """Преобразует значение в список uid: int | 'Имя|uid' | Iterable → [int]."""
    out: List[int] = []
    if v is None:
        return out
    if isinstance(v, int):
        return [v]
    if isinstance(v, str):
        s = v.strip()
        if "|" in s:
            s = s.rsplit("|", 1)[-1]
        try:
            out.append(int(s))
        except ValueError:
            pass
        return out
    if isinstance(v, Iterable):
        for x in v:
            out.extend(_to_uid_list(x))
    return out


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
    """
    Читает state.locked_distribution в обоих форматах:
    • новый: {'main':[uids], 'assist':[uids], 'admin':[uids]}
    • слоты: {'lead1':'Имя|123', 'assistant1':'Имя|456', 'admin':'Имя|789'}
    """
    raw = (getattr(state, "locked_distribution", {}) or {}).get(deal_id) \
          or (getattr(state, "locked_distribution", {}) or {}).get(str(deal_id)) \
          or {}
    roles: Dict[str, Set[int]] = {"main": set(), "assist": set(), "admin": set(), "trainee": set()}

    # списковая схема
    for k in ("main", "assist", "admin", "trainee"):
        for v in _to_uid_list(raw.get(k)):
            roles[k].add(v)

    # слотная схема
    for k, v in raw.items():
        if isinstance(k, str) and (k.startswith(("lead", "assistant")) or "admin" in k or "trainee" in k):
            roles[_role_alias(k)].update(_to_uid_list(v))

    return roles


def _mark_confirmed_on_message_kb(callback: CallbackQuery, deal_id: int, role: str) -> types.InlineKeyboardMarkup | None:
    """
    Локально меняем кнопку «Подтвердить» на «✅ Подтверждено» в текущем сообщении.
    Никаких открытий деталей/редравов.
    """
    try:
        kb = callback.message.reply_markup if callback.message else None
        if not isinstance(kb, types.InlineKeyboardMarkup):
            return None
        new_rows: List[List[types.InlineKeyboardButton]] = []
        target_cd = f"{CONFIRM_PREFIX}{deal_id}_{role}"
        for row in kb.inline_keyboard:
            new_row: List[types.InlineKeyboardButton] = []
            for btn in row:
                cd = getattr(btn, "callback_data", "") or ""
                if cd == target_cd:
                    new_row.append(types.InlineKeyboardButton(text="✅ Подтверждено", callback_data="noop"))
                else:
                    new_row.append(btn)
            new_rows.append(new_row)
        return types.InlineKeyboardMarkup(inline_keyboard=new_rows)
    except Exception:
        logger.exception("[confirm] failed to rebuild keyboard")
    return None

# ────────────────────────────────────────────────────────────────────
# [2] AMOCRM HELPERS (универсальные фолбэки)
# ────────────────────────────────────────────────────────────────────
_TEAM_SUFFIXES: Set[str] = {".1", ".2", ".Адм", ".Стаж"}


def _is_team_tag(name: str) -> bool:
    """Командный тег — любой, что оканчивается на один из служебных суффиксов."""
    if not name:
        return False
    return any(name.endswith(suf) for suf in _TEAM_SUFFIXES)


async def _amo_add_tag(lead_id: int, tag: str) -> bool:
    """
    Добавляет командный тег в сделку, НЕ затирая ранее поставленные.
    ВНИМАНИЕ: здесь ИСКЛЮЧИТЕЛЬНО add-only сценарии, без массовой перезаписи списка.
    """
    try:
        # Предпочитаем точечную обёртку, если она есть в services.amocrm
        if hasattr(amo, "add_tag_to_lead"):
            ok = await amo.add_tag_to_lead(int(lead_id), tag)  # type: ignore[arg-type]
            return bool(ok)

        # Универсальный PATCH одной сделки: _embedded.tags, который у AmoCRM добавляет тег
        if hasattr(amo, "patch_lead"):
            ok = await amo.patch_lead(int(lead_id), {"_embedded": {"tags": [{"name": str(tag)}]}})  # type: ignore[arg-type]
            return bool(ok)

        logger.warning("[confirm] amo add-tag API not found; lead=%s tag=%s", lead_id, tag)
        return False
    except Exception as e:
        logger.error("[confirm] add tag failed lead=%s tag=%s: %s", lead_id, tag, e)
        return False


async def _amo_get_tags(lead_id: int) -> Set[str]:
    """
    Возвращает множество названий тегов сделки; устойчив к 204 и пустым данным.
    Если конкретной обёртки нет — возвращает пустое множество (не считается ошибкой).
    """
    # Попытка через get_deal_by_id (если реализация добавляет 'tags' к нормализованной сделке)
    try:
        if hasattr(amo, "get_deal_by_id"):
            d = await amo.get_deal_by_id(int(lead_id))  # type: ignore[arg-type]
            if isinstance(d, dict) and d.get("tags"):
                return {
                    str(t.get("name"))
                    for t in (d.get("tags") or [])
                    if isinstance(t, dict) and t.get("name")
                }
    except Exception:
        logger.debug("[confirm] get_deal_by_id failed for %s (tags not available)", lead_id)

    # Нет безопасного пути — просто вернём пусто (логика подтверждений не будет зависеть от тегов UI)
    return set()


async def _amo_set_status_success(lead_id: int) -> bool:
    """Переводит сделку в «Завершение сделки» по ID стадии из настроек."""
    try:
        stage_id = getattr(settings, "FINISH_STAGE_ID", None) or getattr(settings, "SUCCESSFUL_STATUS_ID", None)
        if stage_id is None:
            logger.warning("[confirm] SUCCESS status id not configured; lead=%s", lead_id)
            return False

        if hasattr(amo, "update_deal_status"):
            ok = await amo.update_deal_status(int(lead_id), str(stage_id))  # type: ignore[arg-type]
            return bool(ok)

        if hasattr(amo, "patch_lead"):
            ok = await amo.patch_lead(int(lead_id), {"status_id": int(stage_id)})  # type: ignore[arg-type]
            return bool(ok)

        logger.warning("[confirm] amo status API not found; lead=%s", lead_id)
        return False
    except Exception:
        logger.exception("[confirm] set status failed lead=%s", lead_id)
        return False


# ────────────────────────────────────────────────────────────────────
# [3] DETAILS & CONFIRMATION CHECK
# ────────────────────────────────────────────────────────────────────
def _deal_title_from_state(deal_id: int) -> str:
    """
    Возвращает короткий заголовок игры без обращения к UI/деталям.
    Источники: state.deal_titles, затем «Сделка #id».
    """
    try:
        t = (getattr(state, "deal_titles", {}) or {}).get(deal_id) \
            or (getattr(state, "deal_titles", {}) or {}).get(str(deal_id))
        if t:
            return str(t)
    except Exception:
        pass
    return f"Сделка #{deal_id}"


def _confirmed_from_state(deal_id: int) -> Dict[str, Set[int]]:
    """
    Возвращает подтверждённых из state.pending_confirmations (без UI и CRM).
    Структура: {'main': set[int], 'assist': set[int], 'admin': set[int]}.
    """
    out: Dict[str, Set[int]] = {"main": set(), "assist": set(), "admin": set()}
    try:
        pc = (getattr(state, "pending_confirmations", {}) or {}).get(deal_id) or {}
        conf = pc.get("confirmed")
        if isinstance(conf, dict):
            for k in ("main", "assist", "admin"):
                if isinstance(conf.get(k), set):
                    out[k] |= {int(x) for x in conf.get(k)}  # type: ignore[arg-type]
                else:
                    out[k] |= set(_to_uid_list(conf.get(k)))
        elif isinstance(conf, set):
            # старая схема — распределим по назначенным слотам
            locked = _assigned_uids_from_locked(deal_id)
            for k in ("main", "assist", "admin"):
                out[k] |= (locked.get(k, set()) & conf)  # type: ignore[operator]
    except Exception:
        pass
    return out


async def _all_required_confirmed(deal_id: int) -> bool:
    """
    True — если все назначенные (по locked_distribution) подтвердили участие в боте.
    Логика без UI:
      1) Берём назначенных из state.locked_distribution.
      2) Берём подтверждённых из state.pending_confirmations.
      3) Смотрим полноту покрытия.
    Примечание: при необходимости можно дополнить проверкой по тегам CRM (_amo_get_tags).
    """
    locked = _assigned_uids_from_locked(deal_id)
    confirmed = _confirmed_from_state(deal_id)

    return all((not locked[k]) or locked[k].issubset(confirmed[k]) for k in ("main", "assist", "admin"))



# ────────────────────────────────────────────────────────────────────
# [4] CALLBACK: CONFIRM ROLE
# ────────────────────────────────────────────────────────────────────
@router.callback_query(lambda c: c.data and c.data.startswith(CONFIRM_PREFIX))
async def confirm_role_handler(callback: CallbackQuery) -> None:
    """
    Кнопки: confirm_role_{deal_id}_{role}
    • Ставит командный тег в AmoCRM (add-only), НЕ перетирая существующие.
    • Локально меняет кнопку на «✅ Подтверждено» (edit_reply_markup).
    • НИКАКИХ переходов в детали и «пылесоса».
    • Тихий тост пользователю и ненавязчивая нотификация в общий чат.
    • При полном комплекте подтверждений — переводит сделку в «Завершение сделки».
    """
    data = str(callback.data or "")
    try:
        _, _, tail = data.partition(CONFIRM_PREFIX)
        lead_s, role = tail.rsplit("_", 1)
        deal_id = int(lead_s)
    except Exception:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    role = role.strip().lower()
    if role not in {"main", "assist", "admin"}:
        await callback.answer("Неизвестная роль.", show_alert=True)
        return

    uid = callback.from_user.id
    short = await _short_name(uid)

    # Проверяем, что роль действительно назначена этому пользователю
    assigned = _assigned_uids_from_locked(deal_id)
    if uid not in assigned.get(role, set()):
        await callback.answer("Эта роль не назначена на вас.", show_alert=True)
        return

    # Ставим тег в AmoCRM (add-only) — без массовых апдейтов
    tag = f"{short}{_role_suffix(role)}"
    ok = await _amo_add_tag(deal_id, tag)
    if not ok:
        await callback.answer("Не удалось проставить тег. Попробуйте позже.", show_alert=True)
        return

    # Локально: меняем кнопку на «✅ Подтверждено», без каких-либо переходов
    try:
        new_kb = _mark_confirmed_on_message_kb(callback, deal_id, role)
        if new_kb and callback.message:
            await callback.message.edit_reply_markup(reply_markup=new_kb)
    except Exception:
        logger.debug("[confirm] edit_reply_markup failed")

    # Тихий тост пользователю (не блокирующий и без алерта)
    await callback.answer("Вы подтвердили выход на игру! Неопаздывайте ;)", show_alert=False)

    # Обновим локальное состояние подтверждений (без UI)
    try:
        pc = getattr(state, "pending_confirmations", None)
        if isinstance(pc, dict):
            rec = pc.setdefault(deal_id, {})
            conf = rec.get("confirmed")
            if isinstance(conf, dict):
                conf.setdefault(role, set()).add(int(uid))
            else:
                rec["confirmed"] = {role: {int(uid)}}
    except Exception:
        logger.debug("[confirm] pending_confirmations update skipped")

    # Ненавязчивая нотификация в общий чат (если доступен) — без падений и стектрейсов
    if ADMIN_CHAT_ID:
        try:
            from contextlib import suppress
            with suppress(Exception):
                bot = callback.message.bot if callback.message else None
                if bot:
                    title = _deal_title_from_state(deal_id)
                    await bot.send_message(int(ADMIN_CHAT_ID), f"✅ {short} подтвердил выход на игру: «{title}».")
        except Exception:
            # умышленно без logger.exception, чтобы не засорять логи трейсбэками
            logger.warning("[confirm] notify chat failed (ADMIN_CHAT_ID=%r)", ADMIN_CHAT_ID)

    # Финализация стадии: если комплект подтверждений достигнут — переводим в «Завершение сделки»
    try:
        all_ok = await _all_required_confirmed(deal_id)
        if all_ok and OK_STATUS_ID:
            await _amo_set_status_success(deal_id)
    except Exception:
        logger.debug("[confirm] finalize skipped")

# [99] SELF-TEST (минимальный)
# ────────────────────────────────────────────────────────────────────
async def _test() -> None:
    # to_uid_list
    assert _to_uid_list("Иван И.|101") == [101]
    assert set(_to_uid_list(["101", 202, "Петр|303"])) == {101, 202, 303}

    # role alias
    assert _role_alias("lead1") == "main"
    assert _role_alias("assistant2") == "assist"
    assert _role_alias("admin") == "admin"

    # assigned_uids: смешанный формат (списки + слоты)
    state.locked_distribution = {
        1: {"main": [101], "assistant1": "Иван|202", "admin": "Петр|303"},
    }
    a = _assigned_uids_from_locked(1)
    assert a["main"] == {101} and a["assist"] == {202} and a["admin"] == {303}

    print("handlers.confirmations ✅ tests passed")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_test())

# История изменений [99]: 2025-08-13 — расширен self-test на смешанные форматы
