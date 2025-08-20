# handlers/my_games.py — дашборд «Мои игры»
# ─────────────────────────────────────────────────────────────────────────────
"""
Версия 5.4 · 2025-08-18 (merge)

Что внутри (против 5.3 от 2025-08-13):
• Сохранён весь рабочий функционал старой версии (видимость, кнопки, детали).
• Пылесос сделан более совместимым: поддерживает разные сигнатуры ядра
  delete_previous_private_messages(...) и дополнительно подметает state.detail_blocks.
• Обработчик «Замены» оставлен shim'ом (как в рабочей версии), чтобы не дублировать
  логику из polls_lifecycle и не ловить гонки.

Логика, формат, публичные API и тексты — без изменений.
"""

from __future__ import annotations

# ███ [1] IMPORTS
# --------------------------------------------------------------------
import re
import contextlib
import logging
import unicodedata
import inspect
import asyncio
from datetime import datetime
from typing import Dict, List, Optional, Set, Any

from aiogram import Bot, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pytz import timezone

from core.config import settings
from core.state import state
from core.utils import truncate, delete_previous_private_messages, resolve_notify_chat_id  # SSOT-резолвер чата
from services.amocrm import (
    get_amocrm_deals,
    update_amocrm_tags,   # для удаления подтверждающих тегов при замене (используется в чужом пайплайне)
    update_deal_status,   # перевод сделки обратно в «Бронь» (используется в чужом пайплайне)
)

# (детали из отчёта могут понадобиться в будущем; импорт оставляем совместимым)
try:
    from handlers.poll_details import refresh_deal_details  # noqa: F401
except Exception:
    refresh_deal_details = None  # type: ignore

# Префикс подтверждения должен совпадать с handlers.confirmations.py
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
RESPOND_PREFIX = "swap_accept_"   # кнопка в общем чате «Откликнуться»

# «Предварительная заявка» — допускается к назначению и подтверждениям
PRELIM_STATUS_ID: str = str(
    getattr(settings, "PRELIM_STATUS_ID", getattr(settings, "PRELIMINARY_STATUS_ID", "")) or ""
)

# История изменений [1]:
# 2025-08-19 — импортирован SSOT-резолвер resolve_notify_chat_id из core.utils; остальное без изменений.

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


# ███ [2.1] SHOW MY-GAME DETAILS — CRM + утверждённый состав + «✅ Подтвердить»/«Попросить замену»
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
      • кнопка «✅ Подтвердить участие» — при статусе «Бронь» ИЛИ «Предварительная заявка», если назначен;
      • кнопка «🔄 Попросить замену» — при статусе «Завершение сделки» и если назначен.
      • повторно кнопку подтверждения не показываем, если подтверждение уже учтено локально/по тегам.

    ФИКС «пылесоса»: все отправленные сообщения аккумулируются и сохраняются
    в state.last_user_messages[uid] — следующий vacuum их удалит гарантированно.
    """
    # — фильтр против автоперехода из дашборда — только details
    if not str(callback.data).startswith("mygame_details_"):
        with contextlib.suppress(Exception):
            await callback.answer()
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

    # 2) Фолбэк: игры, уже отрисованные пользователю
    if not deal:
        user_list = (getattr(state, "games_by_user", {}) or {}).get(uid) or []
        deal = next((d for d in user_list if int(d.get("id", 0)) == deal_id), None)

    # 3) Жёсткий фолбэк из локального кэша/слотов
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
    ok_id = str(OK_STATUS_ID)

    # определяем «предварительную заявку» по ID или названию статуса
    status_name_raw = str(deal.get("status_name") or deal.get("status") or "").strip().lower()
    prelim = (PRELIM_STATUS_ID and status_id == str(PRELIM_STATUS_ID)) or (status_name_raw in {"предварительная заявка", "предварительно", "предварит."})

    status = str(
        deal.get("status_name")
        or ("Предварительная заявка" if prelim else ("Бронь" if status_id == bron_id else "Завершение сделки" if status_id == ok_id else "—"))
    )

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

    # ⬇️ собираем строки деталей с «активными» полями AmoCRM, кроме финансов
    lines = [
        f"🎮 *{name}*",
        f"📅 {date_s} · 🕒 {time_s}",
        f"📍 {place}",
        f"📦 Пакет: {pkg_raw}",
        f"👥 Игроки: {players}",
    ]
    age = str(deal.get("age") or "").strip()
    if age:
        lines.append(f"🎂 Возраст: {age}")
    extra_services = str(deal.get("extra_services") or "").strip()
    if extra_services:
        lines.append(f"➕ Услуги: {extra_services}")
    photographer = str(deal.get("photographer") or "").strip()
    if photographer:
        lines.append(f"📸 Фотограф: {photographer}")
    comment = str(deal.get("comment") or "").strip()
    if comment:
        lines.append(f"💬 Комментарий: {truncate(comment, 700)}")

    lines.append(f"📌 Статус CRM: {status}")
    lines.append("— — —")
    lines.append(f"🧭 Ведущие: {mains_text}")
    lines.append(f"🛟 Помощники: {assists_text}")
    lines.append(f"🛡️ Админ: {admin_text}")
    if trainee_text:
        lines.append(f"🎓 Стажёр: {trainee_text}")

    msgs: List[types.Message] = []
    msgs.append(await bot.send_message(uid, "\n".join(lines), parse_mode="Markdown"))

    # Роль пользователя (для кнопок)
    role = _assigned_role_from_state(uid, deal_id)  # main/assist/admin/None

    # confirmed по факту: теги ИЛИ локальный state.pending_confirmations
    confirmed_by_tags = _has_confirmation_tag(deal, uid)
    confirmed_local = _is_locally_confirmed(deal_id, uid)
    confirmed = confirmed_by_tags or confirmed_local

    can_confirm = (role in {"main", "assist", "admin"}) and ((status_id == bron_id) or prelim) and (not confirmed)
    can_swap    = (role in {"main", "assist", "admin"}) and (status_id == ok_id)

    # Второе сообщение с одной кнопкой: либо «Подтвердить», либо «Попросить замену»
    second_kb: Optional[InlineKeyboardMarkup] = None
    if can_confirm:
        second_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Подтвердить участие", callback_data=f"{CONFIRM_PREFIX}{deal_id}_{role}")]
            ]
        )
    elif can_swap:
        second_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Попросить замену", callback_data=f"{SWAP_PREFIX}{deal_id}")]
            ]
        )

    if second_kb:
        msgs.append(await bot.send_message(uid, "\u2060", reply_markup=second_kb))

    # Сохраняем всю пачку для будущего vacuum
    try:
        (getattr(state, "last_user_messages", {}) or {}).setdefault(uid, [])
        state.last_user_messages[uid] = msgs
    except Exception:
        logger.debug("[my_games] failed to record last_user_messages for uid=%s", uid)

    with contextlib.suppress(Exception):
        await callback.answer()

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


def _label_belongs_to_uid(slot_value: Optional[str], uid: int) -> bool:
    """
    Надёжная проверка «этот слот про данного пользователя», поддерживает:
      • новый формат: 'Имя Ф..1|123' — по |uid,
      • фолбэк:       'Имя Ф..1'     — по ярлыку (сопоставляется с коротким именем).
    """
    if not slot_value or not isinstance(slot_value, str):
        return False

    # 1) если в слоте есть |uid — проверяем его
    tuid = _tag_uid(slot_value)
    if tuid is not None:
        return int(tuid) == int(uid)

    # 2) фолбэк по ярлыку слота и короткому имени
    label = _tag_label(slot_value)
    base = _short_name(uid)
    if not label or not base:
        return False

    # нормализуем пробелы/невидимые символы
    def _norm(s: str) -> str:
        s = s.replace(NBSP, " ").replace(FE0F, "").replace(ZWNBSP, "")
        s = " ".join(s.split()).strip()
        return s

    return _norm(label).lower().startswith(_norm(base).lower())


def _assigned_role_from_state(uid: int, deal_id: int) -> Optional[str]:
    """
    Определяет роль пользователя в зафиксированном составе (после «Утвердить»):
    • новый формат: слоты lead*/assistant*/admin → строки «Имя Ф.<суффикс>|uid» или «Имя Ф.<суффикс>» (фолбэк);
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
            if _label_belongs_to_uid(roles.get(k), uid):
                return "main"
        for k in _sorted_slots(roles, "assistant"):
            if _label_belongs_to_uid(roles.get(k), uid):
                return "assist"
        if _label_belongs_to_uid(roles.get("admin"), uid):
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


def _bron_status_id() -> str:
    """Безопасный резолвер ID статуса «Бронь»."""
    try:
        return str(BRON_STATUS_ID)  # noqa: F821
    except Exception:
        return str(getattr(settings, "BRON_STATUS_ID", ""))


def _details_kb(uid: int, deal: Dict, confirmed: bool) -> InlineKeyboardMarkup:
    """
    Фолбэк-клавиатура (когда недоступна расширенная разметка).
    Кнопка «✅ Подтвердить участие» активна только для статуса «Бронь»,
    когда пользователь назначен по зафиксированному распределению и ещё не подтвердил.
    """
    kb = InlineKeyboardBuilder()

    role = _assigned_role_from_state(uid, int(deal.get("id") or 0))
    can_confirm = (
        _safe_status_id(deal) == _bron_status_id()
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

# История изменений [3]:
# • 2025-08-19 — добавлен _label_belongs_to_uid и использован в _assigned_role_from_state;
#                теперь «Мои игры» видят слоты без суффикса |uid (фолбэк по ярлыку).



# ════════════════════════════════════════════════════════════════════
# [3.1] SWAP HELPERS (используются внешними пайплайнами; оставляем)
# ════════════════════════════════════════════════════════════════════
def _find_deal_snapshot(deal_id: int) -> Dict[str, Any]:
    """
    Возвращает «снимок» сделки из возможных источников: games_by_user, current_poll_deals, CRM.
    Нужен для формирования текста и работы с тегами.
    """
    # 1) локальный кэш у пользователей
    for deals in (getattr(state, "games_by_user", {}) or {}).values():
        d = next((x for x in deals if int(x.get("id") or 0) == deal_id), None)
        if d:
            return d
    # 2) текущая выборка опроса
    d = next((x for x in (getattr(state, "current_poll_deals", []) or []) if int(x.get("id") or 0) == deal_id), None)
    if d:
        return d
    # 3) CRM полный список
    try:
        all_deals = state._last_deals_cache  # возможен быстрый кэш, если уже тянули ранее
    except Exception:
        all_deals = None
    try:
        if not all_deals:
            all_deals = asyncio.run(get_amocrm_deals())  # fallback sync-context (редкий путь)
    except Exception:
        all_deals = []
    return next((x for x in (all_deals or []) if int(x.get("id") or 0) == deal_id), {})  # может быть пустой


def _clear_assigned_slot(deal_id: int, role: str, uid: int) -> None:
    """
    Очищает соответствующий слот в locked_distribution и удаляет deal_id из assigned_index[uid].
    """
    locked = (getattr(state, "locked_distribution", {}) or {})
    dist: Dict[str, Any] = locked.get(deal_id) or locked.get(str(deal_id)) or {}
    if not isinstance(dist, dict):
        dist = {}

    # слоты нового формата
    if role == "main":
        for k in list(dist.keys()):
            if isinstance(k, str) and k.startswith("lead") and _tag_uid(dist.get(k)) == uid:
                dist[k] = ""
    elif role == "assist":
        for k in list(dist.keys()):
            if isinstance(k, str) and k.startswith("assistant") and _tag_uid(dist.get(k)) == uid:
                dist[k] = ""
    elif role == "admin":
        if _tag_uid(dist.get("admin")) == uid:
            dist["admin"] = ""

    # сохранить обратно (по тому же ключу типа)
    if deal_id in locked:
        locked[deal_id] = dist
    elif str(deal_id) in locked:
        locked[str(deal_id)] = dist

    # assigned_index
    try:
        aidx: Dict[int, Set[int]] = getattr(state, "assigned_index", {}) or {}
        cur = set(aidx.get(uid) or set())
        if deal_id in cur:
            cur.discard(deal_id)
            aidx[uid] = cur
    except Exception:
        pass


def _filter_tags_without_user(tags: List[Dict[str, Any]] | List[Any], uid: int) -> List[str]:
    """
    Возвращает новый список имён тегов БЕЗ подтверждающих тегов указанного пользователя.
    На вход подаём любые структуры тегов из CRM (list[dict{name}] или list[str]).
    """
    names: Set[str] = set()
    for t in tags or []:
        if isinstance(t, dict) and t.get("name"):
            names.add(str(t.get("name")))
        elif isinstance(t, str):
            names.add(t)

    kill = _expected_tags_for(uid)
    return [n for n in names if n not in kill]

# ════════════════════════════════════════════════════════════════════
# [3.2] CONFIRMATIONS: MERGE + "ALL CONFIRMED" ANNOUNCER
# ════════════════════════════════════════════════════════════════════
def _assigned_uids_from_dist(dist: Dict[str, Any]) -> Set[int]:
    uids: Set[int] = set()
    if not isinstance(dist, dict):
        return uids
    for k, v in dist.items():
        if not isinstance(k, str):
            continue
        if k.startswith("lead") or k.startswith("assistant") or k in {"admin", "trainee"}:
            if isinstance(v, str):
                u = _tag_uid(v)
                if u:
                    uids.add(int(u))
    return uids


def merge_committed_slots(deal_id: int, new_slots: Dict[str, str]) -> Dict[str, str]:
    """
    Безопасный мёрдж "утверждённых" слотов:
    - НЕ затираем уже записанные слоты пустыми значениями;
    - Проставляем только те ключи, что реально пришли непустыми;
    - Возвращаем актуальную копию словаря.
    Используй при "утвердить все", чтобы не оставался один админ.
    """
    locked = getattr(state, "locked_distribution", {}) or {}
    cur: Dict[str, str] = {}
    node = locked.get(deal_id) or locked.get(str(deal_id)) or {}
    if isinstance(node, dict):
        cur = dict(node)

    # пишем только непустые
    for k, v in (new_slots or {}).items():
        if not isinstance(k, str):
            continue
        if isinstance(v, str) and v.strip():
            cur[k] = v

    # сохраняем обратно тем же типом ключа
    if deal_id in locked:
        locked[deal_id] = cur
    elif str(deal_id) in locked:
        locked[str(deal_id)] = cur
    else:
        locked[deal_id] = cur
    state.locked_distribution = locked
    return cur


def _notify_chat_id() -> Optional[int]:
    """
    Куда слать "все подтвердили".
    Теперь — через SSOT-резолвер из core.utils.
    """
    try:
        cid = resolve_notify_chat_id()  # SSOT (sync)
        return int(cid) if cid is not None else None
    except Exception:
        return None


def _format_all_confirmed_text(deal: Dict[str, Any]) -> str:
    """
    Формирует уведомление:
    — Для Бронь (обычное закрытие): стандартный текст «⏰ Приходим за 30 минут…».
    — Для «Предварительной заявки»: отдельный текст с пометкой и эмодзи (ожидание предоплаты).
    """
    title = _safe_title(deal)
    dt = _safe_event_dt(deal)

    date_s = dt.strftime("%d.%m.%Y") if dt else str(deal.get("event_date") or "—")
    time_s = str(deal.get("event_time") or "—")
    package = str(deal.get("package") or "—")
    bonuses = str(deal.get("bonuses") or deal.get("bonus") or deal.get("extra_services") or "—")

    # Определяем «предварительную заявку» по ID или названию статуса
    sid = _safe_status_id(deal)
    name = str(deal.get("status_name") or deal.get("status") or "").strip().lower()
    prelim = (
        (PRELIM_STATUS_ID and sid == str(PRELIM_STATUS_ID))
        or (name in {"предварительная заявка", "предварительно", "предварит."})
    )

    # Единый блок внутри кавычек
    info_block = f"{title}. {date_s}. {time_s}. {package}. {bonuses}"

    if prelim:
        # Спец-уведомление для «Предварительной заявки»
        return (
            f"✅ Вся команда подтвердила выход на игру \"{info_block}\" "
            f"⚠️ Внимание: это предварительная заявка! 🤔 Гости ещё думают. 💳 Ждём предоплату."
        )

    # Стандартное уведомление (Бронь / обычное закрытие)
    return (
        f"✅ Вся команда подтвердила выход на игру \"{info_block}\" "
        f"⏰ Приходим за 30 минут ✨! Не опаздываем 😉"
    )



async def announce_if_all_confirmed(deal_id: int) -> None:
    """
    Если по deal_id все назначенные (lead*/assistant*/admin) подтвердили —
    один раз шлём анонс в чат и помечаем в state, чтобы не дублировать.
    """
    try:
        # уже анонсировали?
        done: Set[int] = getattr(state, "confirm_announce_done", set())
        if int(deal_id) in done:
            return
    except Exception:
        done = set()

    # состав из зафиксированного распределения
    locked = (getattr(state, "locked_distribution", {}) or {})
    dist = locked.get(deal_id) or locked.get(str(deal_id)) or {}
    if not isinstance(dist, dict) or not dist:
        return

    assigned = _assigned_uids_from_dist(dist)
    if not assigned:
        return

    # снимок сделки и проверка подтверждений
    deal = _find_deal_snapshot(int(deal_id)) or {"id": int(deal_id)}
    all_ok = True
    for uid in assigned:
        # подтверждение тегом или локально (state.pending_confirmations)
        by_tag = _has_confirmation_tag(deal, uid)
        local = _is_locally_confirmed(int(deal_id), uid)
        if not (by_tag or local):
            all_ok = False
            break

    if not all_ok:
        return

    chat_id = _notify_chat_id()
    if not chat_id:
        logger.warning("[all_confirmed] no chat_id to notify; skip (deal_id=%s)", deal_id)
        state.confirm_announce_done = done | {int(deal_id)}
        return

    text = _format_all_confirmed_text(deal)
    try:
        await Bot.get_current().send_message(chat_id, text)
        logger.info("[all_confirmed] announced for deal_id=%s → chat_id=%s", deal_id, chat_id)
        state.confirm_announce_done = done | {int(deal_id)}
    except Exception as e:
        logger.warning("[all_confirmed] send failed for deal_id=%s: %s", deal_id, e)

# История изменений [3]:
# • 2025-08-19 — добавлен _label_belongs_to_uid и использован в _assigned_role_from_state;
#                теперь «Мои игры» видят слоты без суффикса |uid (фолбэк по ярлыку).
# • 2025-08-19 — альтернативное уведомление для «Предварительной заявки» (⚠️🤔💳), без изменения логики распределения.


# ════════════════════════════════════════════════════════════════════
# [4] ВЫБОРКА ИГР
# ════════════════════════════════════════════════════════════════════
def _wanted_status(deal: Dict) -> bool:
    """
    Игра попадает в «Мои игры», если её статус один из допустимых:
    «Бронь» (BRON_STATUS_ID), «Предварительная заявка» (PRELIM_STATUS_ID/по названию)
    или «Завершение сделки» (OK_STATUS_ID).
    Если статус не указан/не распознан — считаем «Бронь» как безопасный фолбэк.
    """
    try:
        sid = _safe_status_id(deal)
    except Exception:
        sid = None
    sid = sid or str(BRON_STATUS_ID)

    # поддержка «Предварительной заявки»: по ID (если определён глобально) и по названию
    prelim_id = str(globals().get("PRELIM_STATUS_ID", "") or "")
    name = str(deal.get("status_name") or deal.get("status") or "").strip().lower()

    # допустимые статусы
    if sid in {str(BRON_STATUS_ID), str(OK_STATUS_ID)}:
        return True
    if prelim_id and sid == prelim_id:
        return True
    if name in {"предварительная заявка", "предварительно", "предварит."}:
        return True

    # фолбэк: «неизвестный» считаем как «Бронь»
    return sid == str(BRON_STATUS_ID)


def _assigned_deal_ids_from_locked(uid: int) -> Set[int]:
    """
    Собираем id всех сделок, где пользователь записан в зафиксированном составе
    (слоты lead*/assistant*/admin/trainee). SSOT — state.locked_distribution.
    Учитываем как «Имя Ф..1|uid», так и «Имя Ф..1» (фолбэк по ярлыку).
    """
    out: Set[int] = set()
    locked = (getattr(state, "locked_distribution", {}) or {})
    for did_key, dist in locked.items():
        if not isinstance(dist, dict):
            continue
        try:
            did = int(did_key)
        except Exception:
            continue
        for k, v in dist.items():
            if not isinstance(k, str):
                continue
            if k.startswith("lead") or k.startswith("assistant") or k in {"admin", "trainee"}:
                if isinstance(v, str) and _label_belongs_to_uid(v, uid):
                    out.add(did)
                    break
    return out


def _assigned_role_via_locked(uid: int, deal_id: int) -> Optional[str]:
    """
    Роль пользователя в сделке по зафиксированным слотам:
    1) сначала стандартный способ (_assigned_role_from_state),
    2) затем фолбэк по ярлыку слота (когда в значении нет «|uid»).
    Возвращает: 'main' | 'assist' | 'admin' | 'trainee' | None
    """
    try:
        role = _assigned_role_from_state(uid, deal_id)
        if role:
            return role
    except Exception:
        pass

    locked_all = (getattr(state, "locked_distribution", {}) or {})
    dist = None
    if isinstance(locked_all.get(deal_id), dict):
        dist = locked_all.get(deal_id)
    elif isinstance(locked_all.get(str(deal_id)), dict):
        dist = locked_all.get(str(deal_id))

    if not isinstance(dist, dict):
        return None

    for k, v in dist.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if k.startswith("lead") and _label_belongs_to_uid(v, uid):
            return "main"
        if k.startswith("assistant") and _label_belongs_to_uid(v, uid):
            return "assist"
        if k == "admin" and _label_belongs_to_uid(v, uid):
            return "admin"
        if k == "trainee" and _label_belongs_to_uid(v, uid):
            return "trainee"
    return None


def _augment_with_locked(uid: int, all_deals: List[Dict]) -> List[Dict]:
    """
    Дополняем CRM-список сделками из зафиксированного распределения,
    чтобы утверждённые игры были видны даже при пустом ответе CRM.

    Источники карточки (по приоритету):
      1) уже в all_deals (CRM),
      2) state.current_poll_deals,
      3) state.games_by_user,
      4) state.poll_details / state.deal_titles (минимальный фолбэк).
    """
    # id из слотов (SSOT) + из assigned_index (на случай старых индексов)
    want_ids: Set[int] = _assigned_deal_ids_from_locked(uid)
    try:
        aidx: Dict[int, Set[int]] = getattr(state, "assigned_index", {}) or {}
        want_ids |= set(aidx.get(uid) or set())
    except Exception:
        pass

    logger.debug("[my_games] augment_with_locked: uid=%s want_ids=%s", uid, sorted(want_ids))

    if not want_ids:
        return list(all_deals or [])

    # Индекс уже имеющихся карточек (CRM)
    by_id: Dict[int, Dict] = {}
    out: List[Dict] = []
    for d in all_deals or []:
        try:
            did = int(d.get("id") or 0)
        except Exception:
            continue
        if did:
            by_id[did] = d
            out.append(d)

    for did in sorted(want_ids):
        if did in by_id:
            continue

        snap: Optional[Dict] = None

        # 2) текущая выборка опроса
        try:
            snap = next(
                (x for x in (getattr(state, "current_poll_deals", []) or [])
                 if int(x.get("id") or 0) == did),
                None,
            )
        except Exception:
            snap = None

        # 3) прошлые показы пользователям
        if not snap:
            try:
                for deals in (getattr(state, "games_by_user", {}) or {}).values():
                    x = next((t for t in (deals or []) if int(t.get("id") or 0) == did), None)
                    if x:
                        snap = x
                        break
            except Exception:
                pass

        # 4) минимальный фолбэк по poll_details / deal_titles
        if not snap:
            details = (getattr(state, "poll_details", {}) or {}).get(did) or {}
            title = (
                details.get("title")
                or (getattr(state, "deal_titles", {}) or {}).get(did)
                or f"Сделка #{did}"
            )
            snap = {
                "id": did,
                "name": title,
                "event_datetime": details.get("event_datetime"),
                "event_time": details.get("event_time") or "—",
                "address": details.get("address") or details.get("location") or "—",
                "package": details.get("package") or "—",
                "players": details.get("players") or "—",
                "tags": details.get("tags") or [],
                "comment": details.get("comment") or "",
                # статус обязателен — по умолчанию считаем «Бронь», чтобы пройти фильтр
                "status_id": str(BRON_STATUS_ID),
            }

        # Нормализуем критичные поля и добавляем
        out.append(dict(snap))

    return out


def _visible_deals_for_user(uid: int, all_deals: List[Dict]) -> List[Dict]:
    """
    Итоговая выборка для «Мои игры»:
      • статус «Бронь» / «Предварительная заявка» / «Завершение сделки» (неизвестный → «Бронь»);
      • пользователь назначен (в первую очередь — по locked_distribution);
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

        # Главный критерий — назначение через слоты (с поддержкой слотов без |uid)
        assigned_role = _assigned_role_via_locked(uid, did)
        assigned_by_locked = assigned_role is not None

        # Дополнительные источники (совместимость)
        assigned_legacy = _is_user_assigned_legacy(uid, d) or _has_confirmation_tag(d, uid)
        try:
            aidx: Dict[int, Set[int]] = getattr(state, "assigned_index", {}) or {}
            assigned_by_index = did in (aidx.get(uid) or set())
        except Exception:
            assigned_by_index = False

        if not (assigned_by_locked or assigned_by_index or assigned_legacy):
            continue

        out.append(d)
        seen.add(did)

    return out

# История изменений [4]:
# • 2025-08-19 — видимость «Моих игр» выровнена под слоты без |uid:
#                _assigned_deal_ids_from_locked использует _label_belongs_to_uid;
#                добавлен отладочный лог augment_with_locked;
#                + новый фолбэк _assigned_role_via_locked (учёт ярлыка без |uid)
#                  и использование его в _visible_deals_for_user.
# • 2025-08-19 — добавлена поддержка статуса «Предварительная заявка»:
#                распознаётся по PRELIM_STATUS_ID (если определён) и по названию.


# ════════════════════════════════════════════════════════════════════
# [4.1] Backward compatibility (for handlers.profile import)
# ════════════════════════════════════════════════════════════════════
def _my_games(uid: int, deals: List[Dict]) -> List[Dict]:
    """Совместимость со старыми модулями (handlers.profile)."""
    return _visible_deals_for_user(uid, deals)


# ███ [5] DASHBOARD / DETAILS
# ────────────────────────────────────────────────────────────────────
async def _vacuum_safe(uid: int, keep: Optional[List[Any]] = None) -> None:
    """
    Совместимый «пылесос»:
      1) пробуем новую сигнатуру (bot, uid, keep=...),
      2) затем старую позиционную (bot, uid, keep),
      3) затем (uid, keep=...),
      4) затем (uid).
    Дополнительно чистим state.detail_blocks[(uid, ...)].
    """
    bot = Bot.get_current()

    # Нормализуем keep → список message_id (поддерживаем types.Message и int)
    keep_ids: List[int] = []
    for k in keep or []:
        try:
            if isinstance(k, types.Message):
                keep_ids.append(int(k.message_id))
            elif isinstance(k, int):
                keep_ids.append(int(k))
        except Exception:
            continue

    try:
        await delete_previous_private_messages(bot=bot, uid=uid, keep=keep_ids)  # type: ignore[call-arg]
        pass_done = True  # noqa
        return
    except TypeError:
        pass
    except Exception:
        logger.debug("[my_games] vacuum (new kw) failed for uid=%s", uid, exc_info=True)

    try:
        await delete_previous_private_messages(bot, uid, keep_ids)  # type: ignore[misc]
        return
    except TypeError:
        pass
    except Exception:
        logger.debug("[my_games] vacuum (positional) failed for uid=%s", uid, exc_info=True)

    try:
        await delete_previous_private_messages(uid, keep=keep_ids)  # type: ignore[misc]
        return
    except TypeError:
        pass
    except Exception:
        logger.debug("[my_games] vacuum (uid, keep=) failed for uid=%s", uid, exc_info=True)

    try:
        await delete_previous_private_messages(uid)  # type: ignore[misc]
    except Exception:
        logger.debug("[my_games] vacuum (uid) failed for uid=%s", uid, exc_info=True)

    # Доп. подметание detail_blocks для пользователя
    try:
        db = getattr(state, "detail_blocks", {}) or {}
        to_del = [key for key in db.keys() if isinstance(key, tuple) and key and key[0] == uid]
        for k in to_del:
            db.pop(k, None)
    except Exception:
        logger.debug("[my_games] detail_blocks cleanup skipped for uid=%s", uid)


def make_my_games_confirm_btn_for_row(deal: Dict[str, Any], uid: int) -> Optional[InlineKeyboardButton]:
    """
    Возвращает кнопку «✅ Подтвердить» для строки списка «Мои игры» ИЛИ «✅ Подтверждено».
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

    # допускаем подтверждение и для «Предварительной заявки»
    name = str(deal.get("status_name") or deal.get("status") or "").strip().lower()
    prelim = (PRELIM_STATUS_ID and status_id == str(PRELIM_STATUS_ID)) or (name in {"предварительная заявка", "предварительно", "предварит."})

    if not (role in {"main", "assist", "admin"} and (status_id == bron_id or prelim)):
        return None

    if confirmed:
        return InlineKeyboardButton(text="✅ Подтверждено", callback_data="noop")

    return InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"{CONFIRM_PREFIX}{deal_id}_{role}")


def make_my_games_swap_btn_for_row(deal: Dict[str, Any], uid: int) -> Optional[InlineKeyboardButton]:
    """Возвращает кнопку «Замена» для списка."""
    try:
        deal_id = int(deal.get("id") or 0)
    except Exception:
        return None
    status_id = str(deal.get("status_id") or "")
    if status_id != str(OK_STATUS_ID):
        return None
    if not _is_user_assigned_current(uid, deal):
        return None
    return InlineKeyboardButton(text="🔄 Замена", callback_data=f"{SWAP_PREFIX}{deal_id}")


async def _send_dashboard(uid: int, deals: List[Dict]) -> None:
    """
    ВАЖНО: защищаем «вакуум→отправку» per-user локом.
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

            # статус в строке списка: Бронь / Предвар. / Заверш.
            sid = _safe_status_id(d)
            name = str(d.get("status_name") or d.get("status") or "").strip().lower()
            if (PRELIM_STATUS_ID and sid == str(PRELIM_STATUS_ID)) or (name in {"предварительная заявка", "предварительно", "предварит."}):
                status = "Предвар."
            else:
                status = "Бронь" if sid == BRON_STATUS_ID else "Заверш."

            kb.button(
                text=f"ℹ️ {title} · {date} · {status}",
                callback_data=f"{DETAILS_PREFIX}{d['id']}",
            )
            row_btn = make_my_games_confirm_btn_for_row(d, uid) or make_my_games_swap_btn_for_row(d, uid)
            if row_btn:
                kb.row(row_btn)

        kb.adjust(1)

        await _vacuum_safe(uid)
        msg = await bot.send_message(uid, "🎲 *Мои игры:*", parse_mode="Markdown", reply_markup=kb.as_markup())
        (getattr(state, "games_by_user", {}) or {}).setdefault(uid, [])
        state.games_by_user[uid] = deals_sorted
        (getattr(state, "last_user_messages", {}) or {}).setdefault(uid, [])
        state.last_user_messages[uid] = [msg]


async def _send_details(uid: int, deal: Dict) -> None:
    """
    Открывает карточку деталей «Мои игры».
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
                data=f"{DETAILS_PREFIX}{deal_id}",
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

        text = f"ℹ️ {truncate(_safe_title(deal), 40)}\n📅 {date_s} · 🕒 {time_s}\n📌 Статус: {status_name}"
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад к списку", callback_data="mygames_back")
        msg = await bot.send_message(uid, text, reply_markup=kb.as_markup())
        (getattr(state, "last_user_messages", {}) or {}).setdefault(uid, [])
        state.last_user_messages[uid] = [msg]

# ════════════════════════════════════════════════════════════════════
# [6] PUBLIC API
# ════════════════════════════════════════════════════════════════════
async def redraw_my_games(uid: int) -> None:
    """
    Перерисовывает дашборд «Мои игры» пользователю.
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
    """
    uid = message.from_user.id

    # Закрепим контекст
    try:
        ctx = getattr(state, "ui_context", None)
        if ctx is None:
            state.ui_context = {}
        state.ui_context[uid] = "my_games"
    except Exception:
        pass

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
        await _send_dashboard(uid, deals)
    else:
        await Bot.get_current().send_message(uid, "😔 Назначенных игр пока нет.")

    with contextlib.suppress(Exception):
        await message.delete()


@router.callback_query(lambda c: (c.data or "").startswith(DETAILS_PREFIX))
async def _open_my_game_details_cb(callback: types.CallbackQuery) -> None:
    """Открывает детали по кнопке «mygame_details_{deal_id}»."""
    uid = callback.from_user.id

    # Закрепим контекст
    try:
        ctx = getattr(state, "ui_context", None)
        if ctx is None:
            state.ui_context = {}
        state.ui_context[uid] = "my_games"
    except Exception:
        pass

    # Попробуем вытащить саму сделку (если не найдём — перейдём на прямой рендер через show_my_game_details)
    try:
        deal_id = int((callback.data or "").split("_")[-1])
    except Exception:
        with contextlib.suppress(Exception):
            await callback.answer("Не удалось открыть детали.", show_alert=True)
        logger.warning("[my_games] open details failed: bad deal_id in %r", callback.data)
        return

    deal: Optional[Dict] = None
    try:
        # сначала смотрим, что уже рисовали пользователю
        deal = next(
            (d for d in (getattr(state, "games_by_user", {}) or {}).get(uid, [])
             if int(d.get("id") or 0) == deal_id),
            None,
        )
        # если нет — делаем свежую выборку видимых игр и берём оттуда
        if not deal:
            deal = next(
                (d for d in _visible_deals_for_user(uid, await get_amocrm_deals())
                 if int(d.get("id") or 0) == deal_id),
                None,
            )
    except Exception as e:
        logger.debug("[my_games] details fetch via visible_deals failed: %s", e)
        deal = None

    if deal:
        await _send_details(uid, deal)
        with contextlib.suppress(Exception):
            await callback.answer()
        return

    # Фолбэк: отдаём управление стандартному рендеру деталей (он сам соберёт снапшот)
    try:
        await show_my_game_details(callback)
    except Exception as e:
        with contextlib.suppress(Exception):
            await callback.answer("Не удалось открыть детали.", show_alert=True)
        logger.warning("[my_games] open details failed (fallback): %s", e)


@router.callback_query(lambda c: c.data == "mygames_back")
async def cb_back(callback: types.CallbackQuery) -> None:
    """Вернуться к списку «Мои игры»."""
    uid = callback.from_user.id
    try:
        state.ui_context.setdefault(uid, "my_games")
    except Exception:
        pass

    if (getattr(state, "games_by_user", {}) or {}).get(uid):
        await _send_dashboard(uid, state.games_by_user[uid])
    else:
        await redraw_my_games(uid)
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith(REPORT_PREFIX))
async def cb_report_placeholder(callback: types.CallbackQuery) -> None:
    await callback.answer("📝 Отчёт — в разработке.", show_alert=True)


@router.callback_query(
    lambda c: c.data
    and c.data.startswith("mygame_")
    and c.data not in {"mygames_back"}
    and not c.data.startswith(REPORT_PREFIX)
    and not c.data.startswith(SWAP_PREFIX)
    and not c.data.startswith(DETAILS_PREFIX)
)
async def cb_ack_misc(callback: types.CallbackQuery) -> None:
    """Прочие mygame_* коллбэки — просто ACK (совместимость)."""
    try:
        state.ui_context[callback.from_user.id] = "my_games"
    except Exception:
        pass
    await callback.answer()


# [7.2] HANDLER: SWAP («Замена»/«Попросить замену») — shim
# ════════════════════════════════════════════════════════════════════
"""
DEPRECATION-SHIM.
Истинная реализация «Замены» перенесена в handlers/polls_lifecycle.py:[4.8] и [4.9],
где происходит публикация объявления, ожидание «Откликнуться», пересборка состава
и синхронизация отчётов. Здесь намеренно нет обработчика стандартного префикса.
"""

@router.callback_query(lambda c: c.data and c.data.startswith(f"{SWAP_PREFIX}legacy_"))
async def _cb_swap_request_legacy_noop(callback: types.CallbackQuery) -> None:
    try:
        await callback.answer("Функция замены обновлена. Повторите действие.", show_alert=False)
    except Exception:
        pass
    logger.debug("[my_games.swap] legacy noop called for data=%s", getattr(callback, "data", ""))


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

    assert _is_user_assigned_current(123, dummy_bron) is True
    assert _assigned_role_from_state(123, 1) == "main"
    assert _assigned_role_from_state(456, 1) == "assist"
    assert _assigned_role_from_state(789, 1) == "admin"

    assert _wanted_status(dummy_bron) and _wanted_status(dummy_done)

    assert _is_my_games_btn("🎲 Мои игры")
    assert _is_my_games_btn("🎲\u00A0Мои игры")
    assert _is_my_games_btn("🎲\uFE0F\u00A0Мои игры")
    assert _is_my_games_btn("\uFEFF🎲 Мои игры")

    print("handlers.my_games ✅ tests passed")


if __name__ == "__main__":
    import asyncio, logging as _log
    _log.basicConfig(level=_log.DEBUG)
    asyncio.run(_test())
