# handlers/confirmations.py
# ─────────────────────────────────────────────────────────────────────────────
"""
Подтверждения участия ведущими.

Версия v15.2 · 2025‑08‑13
──────────────────────────────────────────────────────────────────────────────
• «✅ Подтвердить» ставит персональный тег в AmoCRM (add‑only, без перезаписи).
• Надёжное определение роли и состава из locked_distribution (списки и слоты).
• Локальная отметка кнопки на «✅ Подтверждено» без переходов и «пылесоса».
• Уведомление в общий чат: «Имя Ф. подтвердил выход на игру „Название“ ДД.ММ ЧЧ:ММ ✅».
• Проверка полноты подтверждений по locked_distribution + pending_confirmations.
• Автоперевод в статус «Завершение сделки», если все требуемые роли подтвердили.
• Совместимость с sync/async core.db.get_user_info.
• Экспорт CONFIRM_PREFIX — для «🎲 Мои игры».
"""

from __future__ import annotations

# ███ [0] IMPORTS & CONSTANTS
# --------------------------------------------------------------------
import inspect
import logging
from contextlib import suppress
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from aiogram import Router, types
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from core.config import settings
from core.state import state

# AmoCRM (универсальные обёртки)
from services import amocrm as amo  # type: ignore

# Детали сделки — на случай принудительных перерисовок
try:
    from handlers.poll_details import refresh_deal_details  # type: ignore
except Exception:  # pragma: no cover
    refresh_deal_details = None  # type: ignore

# Профиль: get_user_info может быть sync или async — обработаем оба случая
try:
    from core.db import get_user_info  # type: ignore
except Exception:  # pragma: no cover
    get_user_info = None  # type: ignore

logger = logging.getLogger(__name__)
router = Router(name="confirmations")

# Префикс для inline‑кнопок подтверждения
CONFIRM_PREFIX = "confirm_role_"

# Доступные стадии «успеха» (любой из вариантов настроек)
OK_STATUS_ID = (
    getattr(settings, "FINISH_STAGE_ID", None)
    or getattr(settings, "SUCCESSFUL_STATUS_ID", None)
    or getattr(state, "OK_STATUS_ID", None)
)

# История изменений [0]: 2025‑08‑13 — единый Router name, безопасные импорты, OK_STATUS_ID fallback


# ███ [1] NAME/ROLE/TAG HELPERS
# --------------------------------------------------------------------
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
            last = (ui.get("last_name") or "").strip()
            last_ini = (ui.get("last_name_initial") or (last[:1].upper() + "." if last else "")).strip()
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
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(k, str) and (k.startswith(("lead", "assistant")) or "admin" in k or "trainee" in k):
                roles[_role_alias(k)].update(_to_uid_list(v))

    return roles


def _deal_when(deal_id: int) -> Tuple[str, str]:
    """
    Возвращает (date, time) строки для уведомлений.
    Источники: state.current_poll_deals → state.deals_index → пусто.
    """
    # 1) Текущие сделки опроса
    with suppress(Exception):
        for d in (state.current_poll_deals or []):
            if int(d.get("id") or 0) == int(deal_id):
                date_s = ""
                if d.get("event_datetime") and hasattr(d["event_datetime"], "strftime"):
                    date_s = d["event_datetime"].strftime("%d.%m.%Y")
                else:
                    date_s = str(d.get("event_date") or "")
                time_s = str(d.get("event_time") or "")
                return (date_s or "", time_s or "")
    # 2) Индекс сделок
    with suppress(Exception):
        meta = (getattr(state, "deals_index", {}) or {}).get(deal_id) \
             or (getattr(state, "deals_index", {}) or {}).get(str(deal_id)) \
             or {}
        return (str(meta.get("date") or ""), str(meta.get("time") or ""))
    # 3) Пусто
    return ("", "")


def _deal_title_from_state(deal_id: int) -> str:
    """
    Возвращает короткий заголовок игры без обращения к UI/деталям.
    Источники: state.deal_titles → current_poll_deals → «Сделка #id».
    """
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
    Локально меняем кнопку «Подтвердить» на «✅ Подтверждено» в текущем сообщении.
    Никаких открытий деталей/редравов.
    """
    try:
        kb = callback.message.reply_markup if callback.message else None
        if not isinstance(kb, InlineKeyboardMarkup):
            return None
        new_rows: List[List[InlineKeyboardButton]] = []
        target_cd = f"{CONFIRM_PREFIX}{deal_id}_{role}"
        for row in (kb.inline_keyboard or []):
            new_row: List[InlineKeyboardButton] = []
            for btn in row:
                cd = getattr(btn, "callback_data", "") or ""
                if cd == target_cd:
                    new_row.append(InlineKeyboardButton(text="✅ Подтверждено", callback_data="noop"))
                else:
                    new_row.append(btn)
            new_rows.append(new_row)
        return InlineKeyboardMarkup(inline_keyboard=new_rows)
    except Exception:
        logger.exception("[confirm] failed to rebuild keyboard")
    return None


# ███ [2] AMOCRM HELPERS (универсальные фолбэки)
# --------------------------------------------------------------------
_TEAM_SUFFIXES: Set[str] = {".1", ".2", ".Адм", ".Стаж"}


def _is_team_tag(name: str) -> bool:
    """Командный тег — любой, что оканчивается на один из служебных суффиксов."""
    return bool(name) and any(str(name).endswith(suf) for suf in _TEAM_SUFFIXES)


async def _amo_add_tag(lead_id: int, tag: str) -> bool:
    """
    Добавляет командный тег в сделку, НЕ затирая ранее поставленные.
    ВНИМАНИЕ: здесь ИСКЛЮЧИТЕЛЬНО add‑only сценарии, без массовой перезаписи списка.
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

    return set()


async def _amo_set_status_success(lead_id: int) -> bool:
    """Переводит сделку в «Завершение сделки» по ID стадии из настроек."""
    try:
        stage_id = getattr(settings, "FINISH_STAGE_ID", None) or getattr(settings, "SUCCESSFUL_STATUS_ID", None) or OK_STATUS_ID
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


async def _resolve_notify_chat_id(bot) -> Optional[int]:
    """
    Возвращает первый доступный чат для уведомлений:
    POLLS_CHAT_ID → LEADERS_CHAT_ID → state.admin_chat_id → ADMIN_CHAT_ID.
    Валидирует доступ через get_chat (без падений).
    """
    candidates = [
        getattr(settings, "POLLS_CHAT_ID", None),
        getattr(settings, "LEADERS_CHAT_ID", None),
        getattr(state, "admin_chat_id", None),
        getattr(settings, "ADMIN_CHAT_ID", None),
    ]
    for cid in candidates:
        if not cid:
            continue
        try:
            cid_int = int(str(cid).strip())
            await bot.get_chat(cid_int)
            return cid_int
        except Exception:
            logger.warning("[confirm] notify chat %s not accessible", cid)
    return None


# ███ [3] DETAILS & CONFIRMATION CHECK
# --------------------------------------------------------------------
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
    """
    locked = _assigned_uids_from_locked(deal_id)
    confirmed = _confirmed_from_state(deal_id)
    return all((not locked[k]) or locked[k].issubset(confirmed[k]) for k in ("main", "assist", "admin"))


# ███ [4] CALLBACK: CONFIRM ROLE
# --------------------------------------------------------------------
@router.callback_query(lambda c: c.data and c.data.startswith(CONFIRM_PREFIX))
async def confirm_role_handler(callback: CallbackQuery) -> None:
    """
    Кнопки: confirm_role_{deal_id}_{role}
    • Ставит командный тег в AmoCRM (add-only), НЕ перетирая существующие.
    • Локально меняет кнопку на «✅ Подтверждено» (edit_reply_markup).
    • НИКАКИХ переходов в детали и «пылесоса».
    • Отправляет уведомление в общий чат (гендерно-нейтрально):
      «✅ Участие подтверждено: Имя Ф. — 🎭 Ведущий на «Название» ДД.ММ ЧЧ:ММ.»
    • При полном комплекте подтверждений — переводит сделку в «Завершение сделки».
    """
    data = str(callback.data or "")
    try:
        _, _, tail = data.partition(CONFIRM_PREFIX)
        lead_s, role_raw = tail.rsplit("_", 1)
        deal_id = int(lead_s)
    except Exception:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    role = role_raw.strip().lower()
    if role not in {"main", "assist", "admin"}:
        await callback.answer("Неизвестная роль.", show_alert=True)
        return

    uid = int(callback.from_user.id)
    short = await _short_name(uid)

    # Проверяем, что роль действительно назначена этому пользователю
    assigned = _assigned_uids_from_locked(deal_id)
    if uid not in assigned.get(role, set()):
        await callback.answer("Эта роль не назначена на вас.", show_alert=True)
        return

    # Ставим тег в AmoCRM (add-only), формат «Имя Ф.1|2|Ад»
    tag = f"{short}{_role_suffix(role)}"
    ok = await _amo_add_tag(deal_id, tag)
    if not ok:
        await callback.answer("Не удалось проставить тег. Попробуйте позже.", show_alert=True)
        return

    # Локально: меняем кнопку на «✅ Подтверждено», без переходов/пылесоса
    with suppress(Exception):
        new_kb = _mark_confirmed_on_message_kb(callback, deal_id, role)
        if new_kb and callback.message:
            await callback.message.edit_reply_markup(reply_markup=new_kb)

    # Тихий тост пользователю
    with suppress(Exception):
        await callback.answer("Вы подтвердили выход на игру ✅", show_alert=False)

    # Обновим локальное состояние подтверждений (без UI)
    with suppress(Exception):
        pc = getattr(state, "pending_confirmations", None)
        if isinstance(pc, dict):
            rec = pc.setdefault(deal_id, {})
            conf = rec.get("confirmed")
            if isinstance(conf, dict):
                conf.setdefault(role, set()).add(uid)
            else:
                rec["confirmed"] = {role: {uid}}

    # ── Уведомление в общий чат (гендерно-нейтрально, с эмодзи роли) ─────────
    try:
        bot = callback.message.bot if callback.message else None
        if bot:
            chat_id = await _resolve_notify_chat_id(bot)
            if chat_id is not None:
                title = _deal_title_from_state(deal_id)
                d_s, t_s = _deal_when(deal_id)
                when = f"{d_s} {t_s}".strip()

                role_human_map = {"main": "Ведущий", "assist": "Помощник", "admin": "Админ"}
                role_emoji_map = {"main": "🎭", "assist": "🤝", "admin": "🛡️"}
                role_human = role_human_map.get(role, "Участник")
                r_emoji = role_emoji_map.get(role, "🎯")

                text = f"✅ Участие подтверждено: {short} — {r_emoji} {role_human} на «{title}» {when}."
                await bot.send_message(chat_id, text.strip())
            else:
                logger.error("[confirm] no available chat for notify; skipped")
    except Exception as e:
        logger.warning("[confirm] notify failed: %s", e)

    # Финализация стадии: если комплект подтверждений достигнут — переводим в «Завершение сделки»
    with suppress(Exception):
        all_ok = await _all_required_confirmed(deal_id)
        if all_ok and OK_STATUS_ID:
            await _amo_set_status_success(deal_id)
            # мягкая очистка из активного цикла (если реализовано)
            with suppress(Exception):
                import handlers.polls_lifecycle as plc
                if callable(getattr(plc, "remove_deal_from_poll_cycle", None)):
                    plc.remove_deal_from_poll_cycle(deal_id)  # type: ignore
                if callable(getattr(plc, "maybe_finish_poll_cycle", None)):
                    await plc.maybe_finish_poll_cycle()  # type: ignore

    # (опционально) Обновим детали, если где-то открыты
    with suppress(Exception):
        if callable(refresh_deal_details):
            await refresh_deal_details(bot=callback.message.bot, deal_id=deal_id, force_approved=True)  # type: ignore



# ███ [99] SELF‑TEST (минимальный)
# --------------------------------------------------------------------
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

    # confirmed_from_state старый/новый формат
    state.pending_confirmations = {
        1: {"confirmed": {"main": {101}, "assist": {202}, "admin": {303}}}
    }
    c = _confirmed_from_state(1)
    assert c["main"] == {101} and c["assist"] == {202} and c["admin"] == {303}

    print("handlers.confirmations ✅ tests passed")


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(_test())

# История изменений [99]: 2025‑08‑13 — расширен self‑test на смешанные форматы
