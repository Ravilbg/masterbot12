# handlers/my_games.py — дашборд «Мои игры»
# ─────────────────────────────────────────────────────────────────────────────
"""
Версия 5.3 · 2025-08-13

Что исправлено/усилено по сравнению с 5.2
──────────────────────────────────────────
• ФИКС «пылесоса»: сообщения деталей теперь жёстко учитываются и удаляются.
  show_my_game_details накапливает отправленные сообщения в state.last_user_messages[uid],
  _vacuum_safe дополнительно чистит state.detail_blocks для uid.
• Роль для кнопки «✅ Подтвердить участие» определяется из зафиксированного состава
  _assigned_role_from_state(...) (поддерживает и новый формат слотов, и legacy-списки),
  чтобы не терять кнопку при legacy-раскладках.
• Мягкие хардены: дефолтные значения, безопасные обращения к state.*, единый lock.

Логика, формат и публичные API полностью сохранены.
"""

from __future__ import annotations

# ███ [1] IMPORTS
# --------------------------------------------------------------------
import re
import contextlib
import logging
import unicodedata
import inspect
from datetime import datetime
from typing import Dict, List, Optional, Set, Any, Tuple

from aiogram import Bot, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pytz import timezone

from core.config import settings
from core.state import state
from core.utils import truncate, delete_previous_private_messages
from services.amocrm import get_amocrm_deals

# (детали из отчёта могут понадобиться в будущем; импорт оставляем совместимым)
try:
    from handlers.poll_details import refresh_deal_details  # noqa: F401
except Exception:
    refresh_deal_details = None  # type: ignore

# Префикс подтверждения должен совпадать с handlers/confirmations.py
try:
    from handlers.confirmations import CONFIRM_PREFIX  # ожидается "confirm_role_"
except Exception:  # fallback на ранних сборках
    CONFIRM_PREFIX = "confirm_role_"

# (необязательно) получим ФИО из базы, если доступно — для "Имя Ф."
try:
    from core.db import get_user_info  # type: ignore
except Exception:  # pragma: no cover
    get_user_info = None  # type: ignore

logger = logging.getLogger(__name__)
router = Router()
MSK_TZ = timezone("Europe/Moscow")

# ── спецсимволы ──────────────────────────────────────────────────────
NBSP = "\u00A0"     # non-breaking space
FE0F = "\uFE0F"     # emoji variation selector
ZWNBSP = "\uFEFF"   # zero-width no-break space (BOM)

# ── статус-ID ────────────────────────────────────────────────────────
# «Бронь» — требуется подтверждение состава
BRON_STATUS_ID: str = str(getattr(settings, "BRON_STATUS_ID", "18913933") or "18913933")
# «Завершение сделки»
OK_STATUS_ID: str = str(getattr(settings, "SUCCESSFUL_STATUS_ID", "0") or "0")

# ── callback-префиксы ────────────────────────────────────────────────
DETAILS_PREFIX = "mygame_details_"
REPORT_PREFIX  = "mygame_report_"
SWAP_PREFIX    = "mygame_swap_"


# ════════════════════════════════════════════════════════════════════
# [2] TEXT HELPERS
# ════════════════════════════════════════════════════════════════════
def _log_text(prefix: str, text: str | None) -> None:
    if logger.isEnabledFor(logging.INFO):
        cps = " ".join(f"U+{ord(c):04X}" for c in (text or ""))
        logger.info("[my_games:%s] raw=%r cps=%s", prefix, text, cps)


def _normalize_btn(text: str | None) -> str:
    """Удаляем NBSP/FE0F/ZWNBSP и любые символы категории Cf, приводим к lower()."""
    if not text:
        return ""
    trimmed = (
        text.replace(NBSP, " ")
            .replace(FE0F, "")
            .replace(ZWNBSP, "")
            .strip()
            .lower()
    )
    return "".join(ch for ch in trimmed if unicodedata.category(ch) != "Cf")


def _is_my_games_btn(text: str | None) -> bool:
    _log_text("received", text)
    norm = _normalize_btn(text)
    ok = "мои" in norm and "игры" in norm
    logger.info("[my_games:match] %s => %s", norm, ok)
    return ok


# ███ [2.1] SHOW MY-GAME DETAILS — CRM + утверждённый состав + «✅ Подтвердить»
# --------------------------------------------------------------------
def _tag_uid(tag: Optional[str]) -> Optional[int]:
    """user_id из тега «Имя.Суффикс|123»."""
    if not tag or "|" not in str(tag):
        return None
    try:
        return int(str(tag).rsplit("|", 1)[-1])
    except Exception:
        return None


def _tag_label(tag: Optional[str]) -> str:
    """Человекочитаемая часть тега до «|uid»."""
    if not tag:
        return ""
    s = str(tag)
    return s.split("|", 1)[0].strip()


def _sorted_slots(dist: Dict[str, str], prefix: str) -> List[str]:
    """Возвращает список ключей слотов (lead*/assistant*) в порядке по индексу."""
    slots = [k for k in dist.keys() if isinstance(k, str) and k.startswith(prefix)]

    def keyf(k: str) -> int:
        m = re.search(r"(\d+)$", k)
        return int(m.group(1)) if m else 0

    return sorted(slots, key=keyf)


def _is_locally_confirmed(deal_id: int, uid: int) -> bool:
    """
    Проверка локального подтверждения из state.pending_confirmations:
    • новая схема: dict по ролям {'main': {uid}, ...}
    • старая схема: set {uid, ...}
    """
    pc = getattr(state, "pending_confirmations", {}) or {}
    node = pc.get(deal_id) or {}
    conf = node.get("confirmed")
    if isinstance(conf, dict):
        return any(int(uid) in set(map(int, conf.get(k, set()))) for k in ("main", "assist", "admin"))
    if isinstance(conf, set):
        return int(uid) in set(map(int, conf))
    return False


@router.callback_query(lambda c: c.data and c.data.startswith("mygame_"))
async def show_my_game_details(callback: types.CallbackQuery) -> None:
    """
    Детали игры в «🎲 Мои игры»:
      • подробности из CRM (дата, время, место, пакет, игроки, статус);
      • утверждённый состав из state.locked_distribution (слоты lead*/assistant*/admin/trainee);
      • кнопка «✅ Подтвердить участие» — только для назначенного и только при статусе «Бронь».
      • повторно кнопку не показываем, если подтверждение уже учтено локально.
    ФИКС «пылесоса»: все отправленные сообщения аккумулируются и сохраняются
    в state.last_user_messages[uid] — следующий vacuum их удалит гарантированно.

    ВАЖНО: обработчик открывает детали ТОЛЬКО по колбэку «mygame_details_{deal_id}».
    Любые другие «mygame_*» (например, кнопка подтверждения в списке) лишь ACK’аются без действий,
    чтобы интерфейс не «прыгал».
    """
    # — фильтр против автоперехода из дашборда —
    if not str(callback.data).startswith("mygame_details_"):
        with contextlib.suppress(Exception):
            await callback.answer()  # просто ACK, ничего не открываем
        return

    bot = Bot.get_current()
    uid = callback.from_user.id
    try:
        deal_id = int(str(callback.data).rsplit("_", 1)[-1])
    except Exception:
        with contextlib.suppress(Exception):
            await callback.answer("Некорректные данные.", show_alert=True)
        return

    # 1) CRM-данные из текущей выборки опроса
    deal = next((d for d in (state.current_poll_deals or []) if int(d.get("id", 0)) == deal_id), None)

    # 2) Фолбэк: игры, которые уже отрисованы этому пользователю в «Мои игры»
    if not deal:
        user_list = (getattr(state, "games_by_user", {}) or {}).get(uid) or []
        deal = next((d for d in user_list if int(d.get("id", 0)) == deal_id), None)

    # 3) Жёсткий фолбэк из локального кэша/слотов (когда CRM вернула 204/пусто)
    if not deal:
        details = (getattr(state, "poll_details", {}) or {}).get(deal_id) or {}
        title = details.get("title") or (getattr(state, "deal_titles", {}) or {}).get(deal_id) or f"Сделка #{deal_id}"
        deal = {
            "id": deal_id,
            "name": title,
            "event_datetime": details.get("event_datetime"),
            "event_time": details.get("event_time") or "—",
            "status_id": BRON_STATUS_ID,
            "address": details.get("address") or details.get("location") or "—",
            "package": details.get("package") or "—",
            "players": details.get("players") or "—",
            "tags": details.get("tags") or [],
            "comment": details.get("comment") or "",
        }

    name = str(deal.get("game_name") or deal.get("name") or f"Сделка #{deal_id}")
    event_dt = deal.get("event_datetime")
    date_s = event_dt.strftime("%d.%m.%Y") if hasattr(event_dt, "strftime") else str(deal.get("event_date") or "—")
    time_s = str(deal.get("event_time") or "—")
    place = str(deal.get("address") or deal.get("location") or "—")
    pkg_raw = str(deal.get("package") or "—")
    players = truncate(str(deal.get("players") or "—"), 60)
    status_id = str(deal.get("status_id") or "")
    bron_id = str(BRON_STATUS_ID)
    status = str(deal.get("status_name") or ("Бронь" if status_id == bron_id else "—"))

    # Состав из locked_distribution (источник правды — новые "слоты")
    locked_all = (getattr(state, "locked_distribution", {}) or {})
    dist: Dict[str, str] = {}
    if isinstance(locked_all.get(deal_id), dict):
        dist = locked_all[deal_id]
    elif isinstance(locked_all.get(str(deal_id)), dict):
        dist = locked_all[str(deal_id)]  # pragma: no cover

    lead_keys = _sorted_slots(dist, "lead")
    asst_keys = _sorted_slots(dist, "assistant")

    mains_text = ", ".join(filter(None, [_tag_label(dist.get(k)) for k in lead_keys])) or "—"
    assists_text = ", ".join(filter(None, [_tag_label(dist.get(k)) for k in asst_keys])) or "—"
    admin_text = _tag_label(dist.get("admin")) or "—"
    trainee_text = _tag_label(dist.get("trainee"))  # может быть пустым

    msgs: List[types.Message] = []
    msgs.append(await bot.send_message(
        uid,
        "\n".join([
            f"🎮 *{name}*",
            f"📅 {date_s} · 🕒 {time_s}",
            f"📍 {place}",
            f"📦 Пакет: {pkg_raw}",
            f"👥 Игроки: {players}",
            f"📌 Статус CRM: {status}",
            "— — —",
            f"🧭 Ведущие: {mains_text}",
            f"🛟 Помощники: {assists_text}",
            f"🛡️ Админ: {admin_text}",
        ] + ([f"🎓 Стажёр: {trainee_text}"] if trainee_text else [])),
        parse_mode="Markdown"
    ))

    # Роль для кнопки «✅ Подтвердить участие» — из зафиксированного состава (новый и legacy)
    role = _assigned_role_from_state(uid, deal_id)  # main/assist/admin/None

    # confirmed по факту: теги ИЛИ локальный state.pending_confirmations
    confirmed_by_tags = _has_confirmation_tag(deal, uid)
    confirmed_local = _is_locally_confirmed(deal_id, uid)
    confirmed = confirmed_by_tags or confirmed_local

    can_confirm = (role in {"main", "assist", "admin"}) and (status_id == bron_id) and (not confirmed)

    if can_confirm:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Подтвердить участие", callback_data=f"{CONFIRM_PREFIX}{deal_id}_{role}")]
            ]
        )
        # отдельным сообщением, чтобы confirmations.py мог безопасно edit_reply_markup
        msgs.append(await bot.send_message(uid, "\u2060", reply_markup=kb))

    # Сохраняем всю пачку для будущего vacuum
    try:
        (getattr(state, "last_user_messages", {}) or {}).setdefault(uid, [])
        state.last_user_messages[uid] = msgs
    except Exception:
        logger.debug("[my_games] failed to record last_user_messages for uid=%s", uid)

    # безопасный ответ на колбэк (если это реальный апдейт; для фейкового — проглотим)
    with contextlib.suppress(Exception):
        await callback.answer()


# ── ХЕЛПЕР для дашборда списка «Мои игры»: инлайн-кнопка подтверждения ──────
def make_my_games_confirm_btn_for_row(deal: Dict[str, Any], uid: int) -> Optional[InlineKeyboardButton]:
    """
    Возвращает кнопку «✅ Подтвердить» для строки списка «Мои игры» ИЛИ «✅ Подтверждено»,
    либо None — если подтверждать нельзя (не назначен/не тот статус/уже подтверждено).
    Логику берём ту же, что и в деталях.
    """
    try:
        deal_id = int(deal.get("id") or 0)
    except Exception:
        return None

    role = _assigned_role_from_state(uid, deal_id)
    status_id = str(deal.get("status_id") or "")
    bron_id = str(BRON_STATUS_ID)

    confirmed_by_tags = _has_confirmation_tag(deal, uid)
    confirmed_local = _is_locally_confirmed(deal_id, uid)
    confirmed = confirmed_by_tags or confirmed_local

    if not (role in {"main", "assist", "admin"} and status_id == bron_id):
        return None

    if confirmed:
        return InlineKeyboardButton(text="✅ Подтверждено", callback_data="noop")

    # ВАЖНО: используем отдельный префикс для списка, чтобы show_my_game_details не срабатывал
    return InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"mygames_confirm_{deal_id}")


# ════════════════════════════════════════════════════════════════════
# [3] DOMAIN HELPERS
# ════════════════════════════════════════════════════════════════════
def _safe_event_dt(deal: Dict) -> Optional[datetime]:
    dt = deal.get("event_datetime")
    return dt if isinstance(dt, datetime) else None


def _safe_title(deal: Dict) -> str:
    return (deal.get("game_name") or deal.get("name") or f"Сделка #{deal.get('id')}").strip()


def _safe_status_id(deal: Dict) -> str:
    return str(deal.get("status_id") or "")


def _is_user_assigned_legacy(uid: int, deal: Dict) -> bool:
    """Legacy-назначение: пользователь есть в team_leads у сделки из CRM (доп. источник)."""
    leads = deal.get("team_leads") or []
    uid_s = str(uid)
    for t in leads:
        try:
            if str(t.get("id")) == uid_s:
                return True
        except Exception:
            continue
    return False


def _is_main_leader_legacy(uid: int, deal: Dict) -> bool:
    """Legacy: первый из team_leads считается основным."""
    leads = deal.get("team_leads") or []
    try:
        return bool(leads and str(leads[0].get("id")) == str(uid))
    except Exception:
        return False


def _short_name(uid: int) -> str:
    """
    Короткое имя "Имя Ф." (с точкой).
    Берём из core.db.get_user_info (если СИНХРОННАЯ функция) или state.users.
    """
    if callable(get_user_info) and not inspect.iscoroutinefunction(get_user_info):
        try:
            u = get_user_info(uid)  # sync путь
            if isinstance(u, dict):
                fn = (u.get("first_name") or "").strip()
                li = (u.get("last_name_initial") or "").strip()
                if fn or li:
                    return f"{fn} {li}".strip()
        except Exception:
            pass
    users_map: Dict[int, Dict[str, Any]] = getattr(state, "users", {}) or {}
    u = users_map.get(uid) or {}
    fn = (u.get("first_name") or "").strip()
    li = (u.get("last_name_initial") or "").strip()
    if fn or li:
        return f"{fn} {li}".strip()
    return ""


def _expected_tags_for(uid: int) -> Set[str]:
    """
    Набор тегов подтверждения для пользователя:
    { 'Имя Ф..1', 'Имя Ф..2', 'Имя Ф..Адм' }
    Учитываем исторические вариации: с двойной точкой/пробелом перед «Адм».
    Если короткое имя неизвестно — возвращаем пустой набор.
    """
    base = _short_name(uid)
    if not base:
        return set()
    # базовые формы
    tags = {f"{base}.1", f"{base}.2", f"{base}.Адм"}
    # исторические вариации
    tags |= {f"{base} .Адм", f"{base}. Адм", f"{base}.Ад", f"{base}. Ад"}
    # двойная точка встречается в части генераторов («Имя Ф.» + «.1»)
    tags |= {f"{base}..1", f"{base}..2", f"{base}..Адм"}
    return tags


def _has_confirmation_tag(deal: Dict, uid: int) -> bool:
    tags = {str(t.get("name")) for t in (deal.get("tags") or []) if isinstance(t, dict) and t.get("name")}
    need = _expected_tags_for(uid)
    return bool(tags & need)


def _assigned_role_from_state(uid: int, deal_id: int) -> Optional[str]:
    """
    Определяет роль пользователя в зафиксированном составе (после «Утвердить»):
    • новый формат: слоты lead*/assistant*/admin → строки «Имя Ф.<суффикс>|uid»;
    • legacy формат: списки int по ключам main/assist/admin.
    Возвращает 'main' | 'assist' | 'admin' | None.
    """
    roles = (getattr(state, "locked_distribution", {}) or {}).get(deal_id) \
            or (getattr(state, "locked_distribution", {}) or {}).get(str(deal_id)) \
            or {}

    # Новый формат (слоты)
    if any(isinstance(k, str) and k.startswith("lead") for k in roles.keys()) or \
       (isinstance(roles.get("admin"), str)):
        for k in _sorted_slots(roles, "lead"):
            if _tag_uid(roles.get(k)) == uid:
                return "main"
        for k in _sorted_slots(roles, "assistant"):
            if _tag_uid(roles.get(k)) == uid:
                return "assist"
        if _tag_uid(roles.get("admin")) == uid:
            return "admin"
        return None

    # Legacy формат (списки int)
    main = set(map(int, roles.get("main", []) or []))
    assist = set(map(int, roles.get("assist", []) or []))
    admin = set(map(int, roles.get("admin", []) or []))
    if uid in main:
        return "main"
    if uid in assist:
        return "assist"
    if uid in admin:
        return "admin"
    return None


def _is_user_assigned_current(uid: int, deal: Dict) -> bool:
    """
    Пользователь назначен на сделку:
    • утверждён руководителем (state.assigned_index[uid] содержит deal_id), ИЛИ
    • есть роль в locked_distribution (новый/legacy), ИЛИ
    • legacy — указан в team_leads, ИЛИ
    • есть тег подтверждения (факт).
    """
    did = int(deal.get("id") or 0)
    assigned_index: Dict[int, Set[int]] = getattr(state, "assigned_index", {}) or {}
    if did and uid in assigned_index and did in (assigned_index.get(uid) or set()):
        return True
    if _assigned_role_from_state(uid, did):
        return True
    if _is_user_assigned_legacy(uid, deal):
        return True
    if _has_confirmation_tag(deal, uid):
        return True
    return False


def _details_text(deal: Dict, confirmed: bool) -> str:
    dt = _safe_event_dt(deal)
    date_part = dt.strftime("%d.%m.%Y") if dt else "—"
    status_txt = "✅ Подтверждено" if confirmed else "⏳ Ожидает подтверждения"
    time_txt = deal.get("event_time", "—")
    package = deal.get("package", "—")
    players = deal.get("players", "—")
    comment = deal.get("comment", "—")
    title = _safe_title(deal)
    return (
        f"🎮 *{title}*\n"
        f"📅 *Дата*: {date_part}\n"
        f"🕒 *Время*: {time_txt}\n"
        f"📦 *Пакет*: {package}\n"
        f"👥 *Игроки*: {players}\n"
        f"🔖 *Статус*: {status_txt}\n"
        f"💬 *Комментарий*: {comment}"
    )


def _details_kb(uid: int, deal: Dict, confirmed: bool) -> InlineKeyboardMarkup:
    """
    Фолбэк-клавиатура (когда недоступна расширенная разметка).
    Кнопка «✅ Подтвердить участие» активна только для статуса «Бронь»,
    когда пользователь назначен по зафиксированному распределению и ещё не подтвердил.
    """
    kb = InlineKeyboardBuilder()

    role = _assigned_role_from_state(uid, int(deal.get("id") or 0))
    can_confirm = (
        _safe_status_id(deal) == BRON_STATUS_ID
        and (role in {"main", "assist", "admin"})
        and not confirmed
    )

    if can_confirm:
        kb.button(
            text="✅ Подтвердить участие",
            callback_data=f"{CONFIRM_PREFIX}{deal['id']}_{role}",
        )

    if _is_main_leader_legacy(uid, deal):
        kb.button(text="📝 Написать отчёт", callback_data=f"{REPORT_PREFIX}{deal['id']}")

    if _is_user_assigned_current(uid, deal):
        kb.button(text="🔄 Попросить замену", callback_data=f"{SWAP_PREFIX}{deal['id']}")

    kb.button(text="← Назад", callback_data="mygames_back")
    kb.adjust(1)
    return kb.as_markup()


# ════════════════════════════════════════════════════════════════════
# [4] ВЫБОРКА ИГР
# ════════════════════════════════════════════════════════════════════
def _wanted_status(deal: Dict) -> bool:
    """
    Игра попадает в «Мои игры», если её статус один из допустимых:
    «Бронь» (BRON_STATUS_ID) или «Завершение сделки» (OK_STATUS_ID).
    """
    try:
        sid = _safe_status_id(deal)
    except Exception:
        sid = None
    return sid in {BRON_STATUS_ID, OK_STATUS_ID}


def _augment_with_locked(uid: int, all_deals: List[Dict]) -> List[Dict]:
    """
    Дополняем CRM-список сделками из зафиксированного распределения,
    если CRM вернул пусто/неполный набор.

    Раньше опирались только на state.assigned_index → игры могли не попасть
    пользователю, если index ещё не успели собрать. Теперь:
    • берём id'шники и из assigned_index[uid],
    • ДОПОЛНИТЕЛЬНО сканируем state.locked_distribution по слотам lead*/assistant*/admin/trainee,
      чтобы гарантированно включить игру всем назначенным.
    """
    # 1) ID из assigned_index (если есть)
    try:
        assigned_index: Dict[int, Set[int]] = getattr(state, "assigned_index", {}) or {}
        want_ids: Set[int] = set(assigned_index.get(uid) or set())
    except Exception:
        want_ids = set()

    # 2) ID из locked_distribution (по слотам) — новый, обязательный источник
    try:
        locked = (getattr(state, "locked_distribution", {}) or {})
        for did_key, dist in locked.items():
            try:
                did = int(did_key)
            except Exception:
                continue
            if not isinstance(dist, dict):
                continue

            # проверяем все слоты нового формата
            found = False
            for k, v in dist.items():
                if not isinstance(k, str):
                    continue
                if k.startswith("lead") or k.startswith("assistant") or k in {"admin", "trainee"}:
                    u = _tag_uid(v) if isinstance(v, str) else None
                    if u == uid:
                        found = True
                        break
            if found:
                want_ids.add(did)
    except Exception:
        # не блокируем сборку, просто остаёмся с тем, что есть
        pass

    if not want_ids:
        return list(all_deals or [])

    # индексация уже пришедших из CRM
    by_id = {int(d.get("id") or 0): d for d in (all_deals or []) if isinstance(d, dict)}
    out: List[Dict] = list(all_deals or [])

    for did in sorted(want_ids):
        if did in by_id:
            continue

        # достраиваем карточку из локальных деталей/кэша
        details = (getattr(state, "poll_details", {}) or {}).get(did) or {}
        title = (
            details.get("title")
            or (getattr(state, "deal_titles", {}) or {}).get(did)
            or f"Сделка #{did}"
        )
        event_dt = details.get("event_datetime")

        out.append(
            {
                "id": did,
                "name": title,
                "event_datetime": event_dt,
                "status_id": BRON_STATUS_ID,  # по умолчанию считаем «Бронь», пока не подтянем CRM
                "team_leads": details.get("team_leads") or [],
                "players": details.get("players") or "—",
                "package": details.get("package") or "—",
                "tags": details.get("tags") or [],
                "comment": details.get("comment") or "",
            }
        )

    return out


def _visible_deals_for_user(uid: int, all_deals: List[Dict]) -> List[Dict]:
    """
    Итоговая выборка для «Мои игры»:
      • статус «Бронь» или «Завершение сделки»;
      • пользователь назначен (assigned_index/locked_distribution-слоты/legacy team_leads/факт по тегам);
      • + дополнение из локального кэша, если CRM вернул пусто.
    """
    all_deals = _augment_with_locked(uid, list(all_deals or []))

    out: List[Dict] = []
    seen: Set[int] = set()

    def _key(d: Dict):
        dt = _safe_event_dt(d)
        from datetime import datetime as _dt
        return (dt is None, dt or _dt.max)

    for d in sorted(all_deals, key=_key):
        try:
            did = int(d.get("id") or 0)
        except Exception:
            continue
        if not did or did in seen:
            continue
        if not _wanted_status(d):
            continue
        if not _is_user_assigned_current(uid, d):
            continue

        out.append(d)
        seen.add(did)

    return out


# ════════════════════════════════════════════════════════════════════
# [4.1] Backward compatibility (for handlers.profile import)
# ════════════════════════════════════════════════════════════════════
def _my_games(uid: int, deals: List[Dict]) -> List[Dict]:
    """
    Совместимость со старыми модулями (handlers.profile).
    Ранее _my_games фильтровал сделки только по team_leads.
    Теперь делегируем новой логике (_visible_deals_for_user), учитывающей:
      • утверждённый состав (assigned_index/locked_distribution слоты и legacy),
      • legacy-назначение team_leads,
      • фактические теги подтверждения в CRM.
    """
    return _visible_deals_for_user(uid, deals)


# ███ [5] DASHBOARD / DETAILS
# ────────────────────────────────────────────────────────────────────
async def _vacuum_safe(uid: int, keep: Optional[List[Any]] = None) -> None:
    """
    Совместимость с разными версиями core.utils.delete_previous_private_messages:
    сначала пробуем новую сигнатуру (bot, uid, keep), затем старую (uid).
    Плюс: чистим state.detail_blocks[(uid, *)], чтобы точно убрать прежние блоки.
    """
    bot = Bot.get_current()
    try:
        await delete_previous_private_messages(bot, uid, keep=keep or [])
    except TypeError:
        try:
            await delete_previous_private_messages(uid)  # type: ignore
        except Exception:
            pass

    # Доп. подметание detail_blocks для пользователя
    try:
        db = getattr(state, "detail_blocks", {}) or {}
        to_del = [key for key in db.keys() if isinstance(key, tuple) and key and key[0] == uid]
        for k in to_del:
            db.pop(k, None)
    except Exception:
        logger.debug("[my_games] detail_blocks cleanup skipped for uid=%s", uid)


async def _send_dashboard(uid: int, deals: List[Dict]) -> None:
    """
    ВАЖНО: защищаем «вакуум→отправку» per-user локом, чтобы избежать гонок,
    когда несколько мест одновременно хотят перерисовать дашборд.
    """
    bot = Bot.get_current()
    lock = state.lock_for(uid)
    async with lock:
        kb = InlineKeyboardBuilder()

        def _key(d: Dict):
            dt = _safe_event_dt(d)
            return (dt is None, dt or datetime.max)

        deals_sorted = sorted(deals, key=_key)

        for d in deals_sorted:
            title = truncate(_safe_title(d), 28)
            dt = _safe_event_dt(d)
            date = dt.strftime("%d.%m") if dt else "??.??"
            status = "Бронь" if _safe_status_id(d) == BRON_STATUS_ID else "Заверш."
            kb.button(
                text=f"ℹ️ {title} · {date} · {status}",
                callback_data=f"{DETAILS_PREFIX}{d['id']}",
            )

        kb.adjust(1)

        await _vacuum_safe(uid)
        msg = await bot.send_message(
            uid, "🎲 *Мои игры:*", parse_mode="Markdown", reply_markup=kb.as_markup()
        )
        (getattr(state, "games_by_user", {}) or {}).setdefault(uid, [])
        state.games_by_user[uid] = deals_sorted
        (getattr(state, "last_user_messages", {}) or {}).setdefault(uid, [])
        state.last_user_messages[uid] = [msg]


async def _send_details(uid: int, deal: Dict) -> None:
    """
    Открывает карточку деталей «Мои игры» (см. [2.1]).
    Без вызова refresh_deal_details — это другой UI из отчёта опроса.
    При ошибке — фолбэк: краткий текст + «Назад».
    """
    bot = Bot.get_current()
    deal_id = int(deal.get("id") or 0)

    lock = state.lock_for(uid)
    async with lock:
        await _vacuum_safe(uid)
        try:
            fake_cb = types.CallbackQuery(
                id="0",
                from_user=types.User(id=uid, is_bot=False, first_name=""),
                chat_instance="",
                message=types.Message(
                    message_id=0,
                    date=datetime.now(),
                    chat=types.Chat(id=uid, type="private"),
                ),
                data=f"mygame_{deal_id}",
            )
            await show_my_game_details(fake_cb)
            return
        except Exception as e:
            logger.exception("[my_games:details] show_my_game_details failed: %s", e)

        # Фолбэк: минимум информации
        dt = _safe_event_dt(deal)
        date_s = dt.strftime("%d.%m.%Y") if dt else "—"
        time_s = str(deal.get("event_time") or "—")
        status_name = str(deal.get("status_name") or "—")

        text = (
            f"ℹ️ {truncate(_safe_title(deal), 40)}\n"
            f"📅 {date_s} · 🕒 {time_s}\n"
            f"📌 Статус: {status_name}"
        )
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад к списку", callback_data="mygames_back")
        msg = await bot.send_message(uid, text, reply_markup=kb.as_markup())
        (getattr(state, "last_user_messages", {}) or {}).setdefault(uid, [])
        state.last_user_messages[uid] = [msg]

# История изменений:
# • 2025-08-12 — _send_details вызывает [2.1] напрямую; фолбэк без зависимостей от отчёта.
# • 2025-08-13 — безопасные callback.answer() и фолбэк источников данных для деталей.
# • 2025-08-13 — v5.3: show_my_game_details сохраняет отправленные сообщения → vacuum удаляет гарантированно.


# ════════════════════════════════════════════════════════════════════
# [6] PUBLIC API
# ════════════════════════════════════════════════════════════════════
async def redraw_my_games(uid: int) -> None:
    """
    Перерисовывает дашборд «Мои игры» пользователю:
    • если есть назначенные (state/locked_distribution/legacy/tags) — рисуем список,
    • если нет — очищаем и показываем «Назначенных игр нет».
    """
    try:
        all_deals = await get_amocrm_deals()
    except Exception as e:
        logger.error("[my_games:redraw] get_amocrm_deals failed: %s", e)
        await _vacuum_safe(uid)
        await Bot.get_current().send_message(uid, "⚠️ Не удалось получить список игр.")
        return

    deals = _visible_deals_for_user(uid, all_deals)
    if deals:
        await _send_dashboard(uid, deals)
    else:
        await _vacuum_safe(uid)
        await Bot.get_current().send_message(uid, "😔 Назначенных игр пока нет.")


# ════════════════════════════════════════════════════════════════════
# [7] HANDLERS
# ════════════════════════════════════════════════════════════════════
@router.message(Command("my_games"))
@router.message(lambda m: _is_my_games_btn(getattr(m, "text", None)))
async def my_games_handler(message: types.Message) -> None:
    """
    Точка входа в «🎲 Мои игры».
    Фикс: перед любой отрисовкой жёстко вызываем «пылесос», чтобы в ЛС всегда оставался
    один актуальный блок (даже если CRM вернула пусто).
    Плюс: устанавливаем UI-контекст 'my_games', чтобы внешние апдейты не «вклинивались».
    """
    uid = message.from_user.id

    # 🔒 зафиксировать контекст «мы сейчас в Мои игры»
    try:
        ctx = getattr(state, "ui_context", None)
        if ctx is None:
            state.ui_context = {}
        state.ui_context[uid] = "my_games"
    except Exception:
        pass

    # всегда чистим ЛС перед новой отрисовкой
    await _vacuum_safe(uid)

    try:
        deals_all = await get_amocrm_deals()
        deals = _visible_deals_for_user(uid, deals_all)
    except Exception as e:
        logger.error("[my_games:handler] get_amocrm_deals failed: %s", e)
        await Bot.get_current().send_message(uid, "⚠️ Не удалось получить список игр.")
        with contextlib.suppress(Exception):
            await message.delete()
        return

    if deals:
        await _send_dashboard(uid, deals)  # _send_dashboard сам вызывает vacuum внутри лока
    else:
        # даже при пустом списке у нас уже был vacuum в начале — отправляем единичное сообщение
        await Bot.get_current().send_message(uid, "😔 Назначенных игр пока нет.")

    with contextlib.suppress(Exception):
        await message.delete()


@router.callback_query(lambda c: c.data and c.data.startswith(DETAILS_PREFIX))
async def cb_details(callback: types.CallbackQuery) -> None:
    """
    Открыть карточку деталей «Мои игры».
    Фикс: при любом заходе в детали переустанавливаем UI-контекст 'my_games',
    чтобы фоновые апдейты чужого UI (отчёт опроса) игнорировали этого пользователя.
    """
    uid = callback.from_user.id

    # 🔒 закрепить контекст
    try:
        ctx = getattr(state, "ui_context", None)
        if ctx is None:
            state.ui_context = {}
        state.ui_context[uid] = "my_games"
    except Exception:
        pass

    try:
        deal_id = int((callback.data or "").split("_")[-1])
    except Exception:
        await callback.answer("⚠️ Ошибочная кнопка.", show_alert=True)
        return

    deal: Optional[Dict] = next(
        (d for d in (getattr(state, "games_by_user", {}) or {}).get(uid, []) if int(d.get("id") or 0) == deal_id),
        None,
    )
    if not deal:
        try:
            deal = next(
                (d for d in _visible_deals_for_user(uid, await get_amocrm_deals())
                 if int(d.get("id") or 0) == deal_id),
                None,
            )
        except Exception as e:
            logger.error("[my_games:details] get_amocrm_deals failed: %s", e)
            deal = None

    if not deal:
        await callback.answer("⚠️ Игра не найдена.", show_alert=True)
        return

    await _send_details(uid, deal)  # _send_details внутри делает vacuum и рисует подробности + кнопку confirm_role
    await callback.answer()


@router.callback_query(lambda c: c.data == "mygames_back")
async def cb_back(callback: types.CallbackQuery) -> None:
    """
    Вернуться к списку «Мои игры».
    Фикс: при возврате также закрепляем контекст 'my_games'.
    """
    uid = callback.from_user.id

    # 🔒 закрепить контекст
    try:
        ctx = getattr(state, "ui_context", None)
        if ctx is None:
            state.ui_context = {}
        state.ui_context[uid] = "my_games"
    except Exception:
        pass

    if (getattr(state, "games_by_user", {}) or {}).get(uid):
        await _send_dashboard(uid, state.games_by_user[uid])  # внутри есть vacuum
    else:
        await redraw_my_games(uid)  # внутри есть vacuum
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith(REPORT_PREFIX))
async def cb_report_placeholder(callback: types.CallbackQuery) -> None:
    # Контекст остаётся 'my_games' (не меняем), просто ответ.
    await callback.answer("📝 Отчёт — в разработке.", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith(SWAP_PREFIX))
async def cb_swap_placeholder(callback: types.CallbackQuery) -> None:
    # Контекст остаётся 'my_games' (не меняем), просто ответ.
    await callback.answer("🔄 Замена — в разработке.", show_alert=True)


@router.callback_query(
    lambda c: c.data
    and c.data.startswith("mygame_")
    and not c.data.startswith(DETAILS_PREFIX)
    and c.data not in {"mygames_back"}
    and not c.data.startswith(REPORT_PREFIX)
    and not c.data.startswith(SWAP_PREFIX)
)
async def cb_ack_misc(callback: types.CallbackQuery) -> None:
    """
    Любые прочие mygame_* коллбэки (служебные/устаревшие) — просто ACK.
    Ничего не открываем, чтобы не было «прыжков» UI.
    """
    uid = callback.from_user.id
    # 🔒 на всякий случай поддержим контекст
    try:
        ctx = getattr(state, "ui_context", None)
        if ctx is None:
            state.ui_context = {}
        state.ui_context[uid] = "my_games"
    except Exception:
        pass

    await callback.answer()

# История изменений:
# • 2025-08-14 — ФИКС автопереходов: при входе/деталях/назад выставляется state.ui_context[uid]='my_games';
#   добавлен ACK-хэндлер для прочих mygame_* чтобы не триггерить чужие переходы.



# ════════════════════════════════════════════════════════════════════
# [8] SELF-TEST
# ════════════════════════════════════════════════════════════════════
async def _test() -> None:
    now = MSK_TZ.localize(datetime.now())
    dummy_bron = {
        "id": 1,
        "game_name": "Quest Room",
        "event_datetime": now,
        "status_id": BRON_STATUS_ID,
        "team_leads": [{"id": "123"}],
        "players": "2-6",
        "tags": [],
    }
    dummy_done = {
        "id": 2,
        "name": "Another Game",
        "event_datetime": now,
        "status_id": OK_STATUS_ID,
        "team_leads": [],
        "players": "5-8",
        "tags": [{"name": "Иван И.2"}],
    }

    # эмулируем слоты, как пишет polls_distribution
    state.locked_distribution = {
        1: {"lead1": "Иван И..1|123", "assistant1": "Пётр П..2|456", "admin": "Анна А..Адм|789"},
        2: {"lead1": "Иван И..1|123"},
    }
    state.assigned_index = {123: {1, 2}, 456: {1}, 789: {1}}
    state.games_by_user = {}

    # назначение/видимость
    assert _is_user_assigned_current(123, dummy_bron) is True
    assert _assigned_role_from_state(123, 1) == "main"
    assert _assigned_role_from_state(456, 1) == "assist"
    assert _assigned_role_from_state(789, 1) == "admin"

    # статусы
    assert _wanted_status(dummy_bron) and _wanted_status(dummy_done)

    # нормализация кнопки
    assert _is_my_games_btn("🎲 Мои игры")
    assert _is_my_games_btn("🎲\u00A0Мои игры")
    assert _is_my_games_btn("🎲\uFE0F\u00A0Мои игры")
    assert _is_my_games_btn("\uFEFF🎲 Мои игры")

    print("handlers.my_games ✅ tests passed")


if __name__ == "__main__":
    import asyncio, logging as _log
    _log.basicConfig(level=_log.DEBUG)
    asyncio.run(_test())
