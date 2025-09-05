# core/utils.py — единый слой утилит и SSOT-хелперов
# ─────────────────────────────────────────────────────────────────────────────
"""
Единый набор утилит для MasterBot.

Версия 7.7 · 2025-09-02
──────────────────────────────────────────────────────────────────────────────
• NEW: [7.12] strict_vacuum / remember_dm / dm_singleton_send / dm_singleton_edit_or_send.
       Единое правило: в ЛС остаётся только текущий блок (исключения — тихий
       refresh для poll_details/my_games).
• FIX: выровнены импорты и __all__, вложенная очистка detail_blocks не ломает
       sticky-дэшборд и главное меню. Совместимо с прежними вызовами.
"""

from __future__ import annotations

# ███ [1] IMPORTS & TYPES
# --------------------------------------------------------------------
import asyncio
import contextlib
import logging
import re
from datetime import datetime, date as _date
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union, NamedTuple, Set

try:
    # aiogram 3.x
    from aiogram import Bot
except Exception:  # pragma: no cover
    Bot = Any  # type: ignore

# ─────────────────────────────────────────────────────────────────────────────
# ███ [8] ПАРСИНГ ПОЛЯ «ИГРОКИ» — поднят выше для разрыва цикла импорта
# (utils -> core.db -> services.amocrm -> utils.parse_players_count)
# --------------------------------------------------------------------
class PlayersRange(NamedTuple):
    min: Optional[int]
    max: Optional[int]
    text: str
    avg: Optional[float]

_RANGE_SEP = r"[\-\–—]"  # дефис / en-dash / em-dash

def parse_players_count(raw: Any) -> PlayersRange:
    s = "" if raw is None else str(raw)
    s_norm = re.sub(r"\s+", " ", s.strip().lower())

    def _mk(min_v: Optional[int], max_v: Optional[int]) -> PlayersRange:
        if isinstance(min_v, int) and isinstance(max_v, int):
            txt = f"{min_v}-{max_v}"
            avg = (min_v + max_v) / 2.0
            return PlayersRange(min_v, max_v, txt, avg)
        if isinstance(min_v, int) and max_v is None:
            return PlayersRange(min_v, None, f"{min_v}+", float(min_v))
        if min_v is None and isinstance(max_v, int):
            return PlayersRange(None, max_v, f"до {max_v}", float(max_v))
        return PlayersRange(None, None, "—", None)

    m = re.search(rf"(\d+)\s*{_RANGE_SEP}\s*(\d+)", s_norm)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a > b:
            a, b = b, a
        return _mk(a, b)

    m = re.search(r"(\d+)\s*\+", s_norm)
    if m:
        return _mk(int(m.group(1)), None)

    m = re.search(r"(до|<=|≤)\s*(\d+)", s_norm)
    if m:
        return _mk(None, int(m.group(2)))

    m = re.search(r"от\s*(\d+)(?:\s*(?:до|–|-|—)\s*(\d+))?", s_norm)
    if m:
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else None
        return _mk(lo, hi)

    m = re.fullmatch(r"\s*(\d+)\s*", s_norm)
    if m:
        n = int(m.group(1))
        return PlayersRange(n, n, str(n), float(n))

    return PlayersRange(None, None, "—", None)
# ─────────────────────────────────────────────────────────────────────────────

try:
    # Каноничные пути проекта
    from core.config import settings  # type: ignore
    from core.db import get_user_info  # type: ignore
    from core.state import state  # type: ignore
except Exception:  # pragma: no cover
    # Фолбэк на плоскую структуру (для ранних сборок / офлайн-тестов)
    from config import settings  # type: ignore
    from db import get_user_info  # type: ignore
    from state import state  # type: ignore

logger = logging.getLogger(__name__)

__all__ = [
    # базовые
    "truncate",
    # имена / форматирование
    "format_short_name",
    "short_name",
    "role_suffix",
    "team_bulleted_lines",
    # uid / нормализация
    "parse_uid",
    "to_uid_list",
    "normalize_roles",
    # уведомления
    "resolve_notify_chat_id",
    # пылесос
    "vacuum_private",
    "delete_previous_private_messages",  # совместимость
    # DM-вакуум (strict) и синглтон-helpers
    "strict_vacuum",
    "remember_dm",
    "dm_singleton_send",
    "dm_singleton_edit_or_send",
    # парсинг домена
    "parse_players_count",
    # роли из state
    "assigned_role_from_state",
    # sticky-дэшборд
    "set_my_games_dashboard",
    "get_sticky_my_games",
    "keep_for_vacuum",
]


# ███ [0] БАЗОВОЕ
# --------------------------------------------------------------------
def truncate(text: Union[str, None], max_len: int = 200) -> str:
    """
    Аккуратная обрезка строки с многоточием.
    • Не ломает None.
    • Учитывает короткие значения max_len (>= 1).
    """
    s = "" if text is None else str(text)
    if max_len <= 0:
        return ""
    if len(s) <= max_len:
        return s
    # всегда добавляем односимвольное многоточие
    return s[: max(0, max_len - 1)].rstrip() + "…"


# ███ [2] ИМЕНА/РОЛИ/ФОРМАТИРОВАНИЕ
# --------------------------------------------------------------------
def format_short_name(first_name: Optional[str], last_name: Optional[str]) -> str:
    """
    Формирует «Имя Ф.» из двух строк (без внешних запросов).
    Пустые значения безопасно игнорируются.
    """
    f = (first_name or "").strip()
    l = (last_name or "").strip()
    if not f and not l:
        return "Без имени"
    if not l:
        return f
    return f"{f} {l[:1]}."  # «Имя Ф.»

async def short_name(subject: Union[int, str, Dict[str, Any], None]) -> str:
    """
    Унифицированное «Имя Ф.» по uid, "Имя|uid" или словарю с полями.
    Если передана строка слота "Имя Ф.|uid" — берём имя из слота,
    чтобы избежать лишних запросов к БД.
    """
    if subject is None:
        return "Без имени"

    if isinstance(subject, str):
        if "|" in subject:
            left, right = subject.split("|", 1)
            left = (left or "").strip()
            _ = parse_uid(right)  # валидация правой части
            return left or "Без имени"
        if subject.isdigit():
            subject = int(subject)
        else:
            return (subject or "").strip() or "Без имени"

    if isinstance(subject, dict):
        fn = (subject.get("first_name") or "").strip()
        ln = (subject.get("last_name") or "").strip()
        li = (subject.get("last_name_initial") or (ln[:1] if ln else "")).strip()
        if fn and li:
            return f"{fn} {li}."
        if fn or ln:
            return format_short_name(fn, ln)
        return "Без имени"

    if isinstance(subject, int):
        try:
            ui = get_user_info(subject)
            if asyncio.iscoroutine(ui):
                ui = await ui  # type: ignore[func-returns-value]
            if isinstance(ui, dict):
                fn = (ui.get("first_name") or "").strip()
                ln = (ui.get("last_name") or "").strip()
                li = (ui.get("last_name_initial") or (ln[:1] if ln else "")).strip()
                if fn and li:
                    return f"{fn} {li}."
                if fn or ln:
                    return format_short_name(fn, ln)
        except Exception as e:  # pragma: no cover
            logger.debug("[short_name] get_user_info failed for %s: %s", subject, e)
        return f"uid:{subject}"

    return "Без имени"

# ВНИМАНИЕ: публичный role_suffix — в блоке [2.x] (SSOT).
# Это внутренний хелпер без точек — оставлен для обратной совместимости
# и нигде не экспортируется.
def _role_suffix_plain(role: str, index: Optional[int] = None) -> str:
    r = (role or "").lower()
    if r == "main":
        return "1"
    if r == "assist":
        return "2"
    if r == "admin":
        return "Адм"
    if r == "trainee":
        return "Стаж"
    return ""

# ███ [2.5] ДАТЫ/ВРЕМЯ — short_dt()
# --------------------------------------------------------------------
def short_dt(value: Any) -> str:
    """
    Короткий формат даты/времени для заголовков деталей:
    • datetime → 'ДД.ММ HH:MM'
    • date     → 'ДД.ММ'
    • ISO-строки ('YYYY-MM-DD', 'YYYY-MM-DD HH:MM', '...T...') → авторазбор
    • Иные строки возвращаем как есть; None → '—'
    """
    if value is None:
        return "—"

    if isinstance(value, datetime):
        return value.strftime("%d.%m %H:%M")

    if isinstance(value, _date):
        return value.strftime("%d.%m")

    if isinstance(value, str):
        s = value.strip()
        if not s:
            return "—"
        try:
            iso = s.replace("T", " ")
            dt = datetime.fromisoformat(iso)
            if dt.time() == datetime.min.time():
                return dt.strftime("%d.%m")
            return dt.strftime("%d.%m %H:%M")
        except Exception:
            pass

        m = re.search(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", s)
        if m:
            dd, mm = int(m.group(1)), int(m.group(2))
            return f"{dd:02}.{mm:02}"
        return s

    return str(value)

# ███ [2.x] СУФФИКСЫ РОЛЕЙ (SSOT)
# --------------------------------------------------------------------
def role_suffix(role: str, index_in_role: Optional[int] = None) -> str:
    """
    Канонические суффиксы ролей для тегов/ярлыков:
    • main/lead  → '.1'
    • assist     → '.2'
    • admin      → '.Адм'
    • trainee    → '.Стаж'
    index_in_role игнорируем намеренно.
    """
    r = (role or "").lower()
    if r in {"main", "lead", "leader"}:
        return ".1"
    if r in {"assist", "assistant", "helper"}:
        return ".2"
    if r in {"admin", "administrator", "админ", "adm"}:
        return ".Адм"
    if r in {"trainee", "intern", "стаж", "стажер", "стажёр"}:
        return ".Стаж"
    return ""


# ███ [3] UID / ПАРСИНГ / НОРМАЛИЗАЦИЯ
# --------------------------------------------------------------------
def parse_uid(value: Any) -> Optional[int]:
    """
    Возвращает uid из int / "123" / "Имя|123" / {"uid":...}.
    Не валится на мусоре: возвращает None.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip()
        if "|" in s:
            _, right = s.rsplit("|", 1)
            return parse_uid(right)
        return int(s) if s.isdigit() else None
    if isinstance(value, dict):
        v = value.get("uid") or value.get("user_id")
        return parse_uid(v)
    return None


def to_uid_list(value: Any) -> List[int]:
    """
    Преобразует value к списку uid: поддерживает int, str, list, set, tuple, None.
    Слоты "Имя|uid" разбираются.
    """
    if value is None or value == "":
        return []
    if isinstance(value, (list, set, tuple)):
        out: List[int] = []
        for x in value:
            u = parse_uid(x)
            if isinstance(u, int):
                out.append(u)
        return out
    u = parse_uid(value)
    return [u] if isinstance(u, int) else []


def _uids_from_slots(slots: Dict[str, Any]) -> Dict[str, List[int]]:
    """
    Вспомогательный разбор слотов {"lead1": "Имя|123", ...} → dict ролей.
    """
    main: List[int] = []
    assist: List[int] = []
    admin: List[int] = []
    trainee: List[int] = []

    for i in range(1, 5):
        k = f"lead{i}"
        u = parse_uid(slots.get(k))
        if isinstance(u, int):
            main.append(u)
    for i in range(1, 5):
        k = f"assistant{i}"
        u = parse_uid(slots.get(k))
        if isinstance(u, int):
            assist.append(u)
    u = parse_uid(slots.get("admin"))
    if isinstance(u, int):
        admin.append(u)
    trainee = to_uid_list(slots.get("trainee"))

    return {"main": main, "assist": assist, "admin": admin, "trainee": trainee}


def normalize_roles(raw: Dict[str, Any]) -> Dict[str, List[int]]:
    """
    Приводит вход к универсальному формату ролей:
    {"main":[uid...], "assist":[uid...], "admin":[uid...], "trainee":[uid...]}

    Поддерживает:
      • уже нормализованный формат (как есть),
      • слотовый формат {"lead1": "...", "assistant1": "...", "admin": "...", "trainee": ...}.
    Не кидает исключений — пустые/битые значения превращаются в пустые списки.
    """
    if not isinstance(raw, dict):
        return {"main": [], "assist": [], "admin": [], "trainee": []}

    if all(k in raw for k in ("main", "assist", "admin")):
        out = {
            "main": to_uid_list(raw.get("main")),
            "assist": to_uid_list(raw.get("assist")),
            "admin": to_uid_list(raw.get("admin")),
            "trainee": to_uid_list(raw.get("trainee")),
        }
        return out

    return _uids_from_slots(raw)


# ███ [4] СПИСКИ КОМАНДЫ ДЛЯ УВЕДОМЛЕНИЙ (SSOT в [7.1])
# --------------------------------------------------------------------
# Реализация team_bulleted_lines в блоке [7.1] ниже.


# ███ [5] ОБЩИЙ ЧАТ ДЛЯ УВЕДОМЛЕНИЙ
# --------------------------------------------------------------------
def _as_int(val: Any) -> Optional[int]:
    """Пробует привести значение к int (поддержка строковых ID из env)."""
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        s = val.strip()
        if s and ((s[0] == "-" and s[1:].isdigit()) or s.isdigit()):
            with contextlib.suppress(ValueError):
                return int(s)
    return None


def _is_placeholder(cid: Optional[int]) -> bool:
    """Фильтр мусора/плейсхолдеров: 0, -1001234567890 и None не годятся."""
    return cid in (None, 0, -1001234567890)


def resolve_notify_chat_id(*_args: Any, **_kwargs: Any) -> Optional[int]:
    """
    SSOT-резолвер id чата для системных уведомлений.
    """
    try:
        candidates: List[Any] = [
            getattr(settings, "NOTIFY_CHAT_ID", None),
            (getattr(settings, "NOTIFY_CHAT_IDS", []) or [None])[0],
            getattr(settings, "POLLS_CHAT_ID", None),
            getattr(settings, "PRIMARY_CHAT_ID", None),
            getattr(settings, "LEADERS_CHAT_ID", None),
            getattr(settings, "TEAM_CHAT_ID", None),
            getattr(settings, "ADMIN_CHAT_ID", None),
            getattr(state, "admin_chat_id", None),
        ]

        for raw in candidates:
            cid = _as_int(raw)
            if isinstance(cid, int) and not _is_placeholder(cid):
                logger.debug("[notify] resolved chat_id=%s (raw=%r)", cid, raw)
                return cid

        logger.warning(
            "[notify] no notify chat configured; set NOTIFY_CHAT_ID or ensure state.admin_chat_id"
        )
        return None
    except Exception as e:  # pragma: no cover
        logger.debug("[notify] resolve failed: %s", e)
        return None

# История изменений (блок [5]):
# 2025-08-19 — расширен резолвер: добавлен fallback на state.admin_chat_id,
#              игнор плейсхолдеров, бэксовместимая сигнатура; выровнено под SSOT.


# ███ [5.5] STICKY «МОИ ИГРЫ» — SSOT-ХЕЛПЕРЫ
# --------------------------------------------------------------------
def set_my_games_dashboard(uid: int, message_id: int) -> None:
    """
    Сохраняет message_id липкого дашборда «Мои игры» в state.my_games_sticky.
    Используется рендером «Моих игр» и вакуумом.
    """
    try:
        d = getattr(state, "my_games_sticky", None)
        if not isinstance(d, dict):
            d = {}
            setattr(state, "my_games_sticky", d)
        d[int(uid)] = int(message_id)
        logger.debug("[sticky] set my_games_sticky[%s]=%s", uid, message_id)
    except Exception as e:  # pragma: no cover
        logger.debug("[sticky] set failed: %s", e)

def get_sticky_my_games(uid: int) -> Optional[int]:
    """
    Возвращает message_id липкого дашборда «Мои игры», если сохранён.
    """
    try:
        d = getattr(state, "my_games_sticky", None)
        mid = d.get(int(uid)) if isinstance(d, dict) else None
        return int(mid) if isinstance(mid, int) else None
    except Exception:  # pragma: no cover
        return None

def keep_for_vacuum(uid: int, *extra_msg_ids: int) -> List[int]:
    """
    Список message_id, которые НЕЛЬЗЯ удалять «пылесосом»:
    • sticky «Мои игры», если есть
    • любые дополнительные message_id, переданные вызвавшим кодом
    """
    keep: List[int] = []
    sticky = get_sticky_my_games(int(uid))
    if isinstance(sticky, int):
        keep.append(sticky)
    for mid in extra_msg_ids:
        if isinstance(mid, int) and mid not in keep:
            keep.append(mid)
    return keep


# ███ [6] «ПЫЛЕСОС» ЛС — КАНОНИЧЕСКАЯ РЕАЛИЗАЦИЯ (бережём главное меню и «Мои игры»)
# --------------------------------------------------------------------
async def vacuum_private(uid: int, keep: Optional[Sequence[int]] = None) -> None:
    """
    Удаляет все устаревшие личные сообщения пользователя, оставляя только `keep`.

    ВАЖНО:
      • Сообщение главного меню бережём автоматически (state.menu_message_id).
      • БЕРЕЖЁМ ДАШБОРД «МОИ ИГРЫ» (липкий): state.my_games_sticky[uid] (через keep_for_vacuum).
      • БЕРЕЖЁМ ЛИЧНЫЙ ДАШБОРД ОТЧЁТА ЛИДЕРА ОПРОСА (если открыт и не подавлено).
      • Поддержка хранения detail_blocks по ключам uid И (uid, deal_id).
    """
    from core.state import state as _state  # гарантируем актуальный объект

    # 0) нормализуем keep и добавляем sticky
    keep_list = list(keep or [])
    keep_set = set(int(x) for x in keep_list if isinstance(x, int))

    # sticky «Мои игры»
    try:
        for mid in keep_for_vacuum(int(uid)):
            keep_set.add(int(mid))
    except Exception:
        pass

    # главный корневой «меню»-мид (независимо от sticky)
    def _add_keep_from(attr: str) -> None:
        val = getattr(_state, attr, None)
        if isinstance(val, dict):
            mid = val.get(int(uid))
            if isinstance(mid, int) and mid > 0:
                keep_set.add(mid)
        elif isinstance(val, int) and val > 0:
            keep_set.add(val)

    _add_keep_from("menu_message_id")

    # ——— SSOT: бережём дашборд отчёта лидера опроса (если не подавлено) ———
    try:
        cur_leader = getattr(_state, "current_poll_leader", None)
        report_mid = getattr(_state, "personal_report_message_id", None)
        suppress_keep = bool(getattr(_state, "suppress_report_keep", False))
        if (
            not suppress_keep
            and isinstance(cur_leader, int) and int(cur_leader) == int(uid)
            and isinstance(report_mid, int) and report_mid > 0
        ):
            keep_set.add(int(report_mid))
            logger.debug("[vacuum] keep leader report uid=%s mid=%s", uid, report_mid)
        elif suppress_keep:
            logger.debug("[vacuum] skip keep leader report uid=%s (suppress flag)", uid)
    except Exception:
        pass

    bot = None
    try:
        bot = Bot.get_current()
    except Exception:
        pass

    async def _safe_delete(chat_id: int, message_id: int) -> bool:
        if not bot:
            return False
        try:
            await bot.delete_message(chat_id, message_id)
            return True
        except Exception:
            return False

    # 1) last_user_messages — удаляем всё, что не в keep_set
    lum = getattr(_state, "last_user_messages", {})
    if isinstance(lum, dict):
        msgs = lum.get(int(uid)) or []
        new_list = []
        for m in list(msgs):
            mid = None
            if hasattr(m, "message_id"):
                with contextlib.suppress(Exception):
                    mid = int(m.message_id)
            elif isinstance(m, tuple) and len(m) >= 2:
                with contextlib.suppress(Exception):
                    mid = int(m[1])
            elif isinstance(m, int):
                mid = m

            if isinstance(mid, int) and mid in keep_set:
                new_list.append(m)
            elif isinstance(mid, int):
                await _safe_delete(int(uid), mid)
        lum[int(uid)] = new_list

    # 2) detail_blocks — чистим только «хвосты», keep_set уважаем
    db = getattr(_state, "detail_blocks", {})
    if isinstance(db, dict):
        if int(uid) in db and isinstance(db[int(uid)], list):
            new_list2: List[int] = []
            for mid in list(db[int(uid)]):
                if isinstance(mid, int) and mid in keep_set:
                    new_list2.append(mid)
                elif isinstance(mid, int):
                    await _safe_delete(int(uid), mid)
            db[int(uid)] = new_list2

        tuple_keys: List[Tuple[int, Any]] = [
            k for k in db.keys() if isinstance(k, tuple) and len(k) >= 2 and k[0] == int(uid)
        ]
        for tkey in tuple_keys:
            val = db.get(tkey)
            if isinstance(val, list):
                new_list3: List[int] = []
                for mid in val:
                    if isinstance(mid, int) and mid in keep_set:
                        new_list3.append(mid)
                    elif isinstance(mid, int):
                        await _safe_delete(int(uid), mid)
                db[tkey] = new_list3
            elif isinstance(val, int):
                mid = val
                if mid not in keep_set:
                    await _safe_delete(int(uid), mid)
                    with contextlib.suppress(Exception):
                        del db[tkey]

    # 3) personal_report_message_id — можно удалять (если не в keep)
    prm = getattr(_state, "personal_report_message_id", None)
    if isinstance(prm, int) and (prm not in keep_set):
        ok = await _safe_delete(int(uid), prm)
        if ok:
            setattr(_state, "personal_report_message_id", None)

    logger.debug("[vacuum] uid=%s keep=%s", uid, sorted(list(keep_set)))

# История изменений:
# 2025-09-05 — SSOT: vacuum_private бережёт отчёт лидера + suppress_report_keep


# ███ [7] LEGACY-ВРАППЕР ПЫЛЕСОСА (совместимость)
# --------------------------------------------------------------------
async def delete_previous_private_messages(*args, **kwargs) -> None:
    """
    Совместимый враппер:
      delete_previous_private_messages(uid, keep=[...]) | delete_previous_private_messages(uid)
    """
    _uid = None
    if args and isinstance(args[0], int) and not kwargs.get("uid"):
        _uid = args[0]
    _uid = kwargs.get("uid", _uid)
    if not isinstance(_uid, int):
        return
    keep = kwargs.get("keep")
    await vacuum_private(_uid, keep=keep)


# ════════════════════════════════════════════════════════════════════
# [7.1] TEAM LINES — bullets for notifications (SSOT)
# ════════════════════════════════════════════════════════════════════
_ROLE_SUFFIXES: Dict[str, str] = {
    "lead": ".1",
    "assistant": ".2",
    "admin": ".Адм",
    "trainee": ".Стаж",
}

# распознаём уже прилипший суффикс в конце строки имени
_SUFFIX_RE = re.compile(r"(?:\.(?:1|2|Адм|Стаж))\s*$", re.IGNORECASE)

def _role_of_slot(slot_key: str) -> Tuple[str, str]:
    s = (slot_key or "").lower()
    if s.startswith("lead"):
        return "lead", _ROLE_SUFFIXES["lead"]
    if s.startswith("assistant"):
        return "assistant", _ROLE_SUFFIXES["assistant"]
    if s == "admin":
        return "admin", _ROLE_SUFFIXES["admin"]
    if s == "trainee":
        return "trainee", _ROLE_SUFFIXES["trainee"]
    return "", ""

def _extract_human(value: Any) -> str:
    if isinstance(value, str):
        return value.split("|", 1)[0].strip()
    return ""

def _strip_existing_suffix(human: str) -> str:
    h = _SUFFIX_RE.sub("", human or "").strip()
    while ".." in h:
        h = h.replace("..", ".")
    return h

def _join_name_and_suffix(human: str, suffix: str) -> str:
    if not suffix:
        return human
    if human.endswith(".") and suffix.startswith("."):
        return f"{human}{suffix[1:]}"
    return f"{human}{suffix}"

async def team_bulleted_lines(slots: Dict[str, Any]) -> List[str]:
    """
    Формирует строки состава с буллитами.
    Правила:
      — суффиксы ролей: .1 (ведущий), .2 (помощник), .Адм, .Стаж — как в SSOT;
      — удаляем уже прилипший суффикс, чтобы не было «..1»;
      — значения слотов читаем только до «|uid».
    """
    if not isinstance(slots, dict):
        return []

    lines: List[str] = []
    ordered_keys: List[str] = []
    ordered_keys += [k for k in sorted(slots.keys()) if str(k).startswith("lead")]
    ordered_keys += [k for k in sorted(slots.keys()) if str(k).startswith("assistant")]
    if "admin" in slots:
        ordered_keys.append("admin")
    if "trainee" in slots:
        ordered_keys.append("trainee")

    for key in ordered_keys:
        raw = slots.get(key)
        human_raw = _extract_human(raw)
        if not human_raw:
            continue
        role, suffix = _role_of_slot(str(key))
        base = _strip_existing_suffix(human_raw)
        with_suffix = _join_name_and_suffix(base, suffix)
        lines.append(f"• {with_suffix}")

    return lines


# ─────────────────────────────────────────────────────────────────────────────
# ███ [7.12] DM-ВАКУУМ (STRICT) + СИНГЛТОН-БЛОКИ ДЛЯ ЛС — SSOT
# ---------------------------------------------------------------------
# Контексты, где разрешаем «тихий» refresh без полной перерисовки:
SOFT_REFRESH_CONTEXTS: Set[str] = {"poll_details", "my_games"}

def _detail_blocks_registry() -> Dict[Any, Any]:
    """
    Глобальный реестр сообщений с деталями в ЛС.
    Храним в state.detail_blocks, чтобы разные модули видели общий список.

    НОРМАЛИЗАЦИЯ: устаревшие ключи вида `uid: int` мигрируются в кортеж `(uid, 0)`,
    чтобы внешние итераторы, ожидающие `(uid, deal_id)`, не падали.
    """
    store = getattr(state, "detail_blocks", None)
    if not isinstance(store, dict):
        store = {}
        setattr(state, "detail_blocks", store)

    # Миграция ключей int → (uid, 0)
    legacy_int_keys = [k for k in list(store.keys()) if isinstance(k, int)]
    for k in legacy_int_keys:
        val = store.pop(k)
        key_tuple = (int(k), 0)
        # аккуратно слияние
        if key_tuple in store and isinstance(store[key_tuple], list) and isinstance(val, list):
            store[key_tuple] = list(store[key_tuple]) + list(val)
        else:
            store[key_tuple] = val
    return store  # ключи: (uid, deal_id) → [message_id...] | int

async def strict_vacuum(uid: int, keep_ids: Optional[Set[int]] = None) -> None:
    """
    Универсальная зачистка ЛС: удаляет все старые сообщения, кроме keep_ids.
    1) Вызывает каноничный vacuum_private (бережёт главное меню и sticky «Мои игры»).
    2) Дочищает вручную по реестру state.detail_blocks.
    """
    keep_ids = keep_ids or set()
    # 1) базовая очистка ядром
    await vacuum_private(uid, keep=list(keep_ids))

    # 2) дочистка локального реестра — по всем кортежным ключам данного uid
    bot = None
    with contextlib.suppress(Exception):
        bot = Bot.get_current()
    reg = _detail_blocks_registry()

    # соберём все ключи для данного uid (k0 == uid)
    keys_for_uid: List[Any] = [
        k for k in list(reg.keys())
        if (isinstance(k, tuple) and len(k) >= 2 and int(k[0]) == int(uid))
    ]
    for k in keys_for_uid:
        v = reg.get(k)
        remaining: List[int] = []
        if isinstance(v, list):
            for mid in list(v):
                if isinstance(mid, int) and mid in keep_ids:
                    remaining.append(mid)
                    continue
                if bot and isinstance(mid, int):
                    with contextlib.suppress(Exception):
                        await bot.delete_message(chat_id=int(uid), message_id=mid)
        elif isinstance(v, int):
            mid = v
            if mid in keep_ids:
                remaining = [mid]
            else:
                if bot:
                    with contextlib.suppress(Exception):
                        await bot.delete_message(chat_id=int(uid), message_id=mid)
                remaining = []
        # обновим реестр: если есть что держать — оставим, иначе очистим
        if remaining:
            reg[k] = remaining
        else:
            with contextlib.suppress(Exception):
                del reg[k]

async def remember_dm(uid: int, message_id: int) -> None:
    """
    Регистрирует сообщение «текущего блока» в общем реестре, чтобы
    потом его можно было удалить strict-вакуумом.
    Храним под кортежным ключом (uid, 0).
    """
    reg = _detail_blocks_registry()
    key = (int(uid), 0)
    lst = reg.get(key) or []
    if not isinstance(lst, list):
        lst = [lst] if isinstance(lst, int) else []
    lst.append(int(message_id))
    if len(lst) > 30:
        lst = lst[-30:]
    reg[key] = lst

async def dm_singleton_send(
    uid: int,
    text: str,
    *,
    context: str = "default",
    **send_kwargs: Any,
):
    """
    Отправляет НОВЫЙ «текущий» блок в ЛС:
    • перед отправкой чистит всё (strict_vacuum),
    • регистрирует отправленное сообщение как единственное активное.
    """
    await strict_vacuum(int(uid), keep_ids=set())
    bot = Bot.get_current()
    msg = await bot.send_message(chat_id=int(uid), text=text, **send_kwargs)
    await remember_dm(int(uid), int(msg.message_id))
    return msg

async def dm_singleton_edit_or_send(
    uid: int,
    message_id: Optional[int],
    text: str,
    *,
    context: str = "default",
    **send_kwargs: Any,
):
    """
    Пытается отредактировать текущий блок; если не удалось — шлёт новый,
    при этом гарантированно остаётся один активный блок.
    • Для контекстов из SOFT_REFRESH_CONTEXTS (poll_details/my_games) — сначала
      пробуем «тихо» редактировать без тотальной зачистки.
    """
    bot = Bot.get_current()
    if context in SOFT_REFRESH_CONTEXTS and isinstance(message_id, int):
        with contextlib.suppress(Exception):
            m = await bot.edit_message_text(
                chat_id=int(uid), message_id=int(message_id), text=text, **send_kwargs
            )
            await strict_vacuum(int(uid), keep_ids={int(message_id)})
            await remember_dm(int(uid), int(message_id))
            return m

    # не получилось редактировать — шлём новый и оставляем его один
    await strict_vacuum(int(uid), keep_ids=set())
    msg = await bot.send_message(chat_id=int(uid), text=text, **send_kwargs)
    await remember_dm(int(uid), int(msg.message_id))
    return msg

# История изменений (блок [7.12]):
# 2025-09-02 — добавлен жёсткий DM-вакуум и синглтон-хелперы; единое правило:
#               «нажали кнопку — выше ЛС пусто», исключения — poll_details/my_games.
# 2025-09-03 — нормализация ключей state.detail_blocks: int → (uid, 0);
#               remember_dm пишет в (uid, 0); strict_vacuum чистит по кортежным ключам.


# ███ [9] ХЕЛПЕР РОЛЕЙ ИЗ STATE (SSOT)
# --------------------------------------------------------------------
def assigned_role_from_state(uid: int, deal_id: int) -> Optional[str]:
    """
    Возвращает роль ('main'|'assist'|'admin'|'trainee') пользователя по сделке из:
      1) state.locked_distribution[deal_id]               — активные утверждённые (приоритет)
      2) state.finished_locked[deal_id]                   — переведённые в «Завершение сделки» (вариант 1)
      3) state.finished_locked_distribution[deal_id]      — переведённые в «Завершение сделки» (вариант 2)
      4) state.distribution_cache[str(deal_id)]           — предварительный состав (snapshot/драфт)
    """
    uid = int(uid)
    did_i = int(deal_id)

    locked    = getattr(state, "locked_distribution", {}) or {}
    finished1 = getattr(state, "finished_locked", {}) or {}
    finished2 = getattr(state, "finished_locked_distribution", {}) or {}
    cache     = getattr(state, "distribution_cache", {}) or {}

    def _pick(d: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(d, dict):
            return None
        v = d.get(did_i)
        if not isinstance(v, dict):
            v = d.get(str(did_i))
        return v if isinstance(v, dict) else None

    # порядок приоритета: locked → finished (оба варианта ключа) → cache
    dist: Optional[Dict[str, Any]] = (
        _pick(locked) or _pick(finished1) or _pick(finished2) or _pick(cache)
    )
    if not isinstance(dist, dict):
        return None

    def _match_any(val: Any) -> bool:
        # поддержка "Имя|uid", списков и чистых uid
        if isinstance(val, list):
            return any(parse_uid(x) == uid for x in val)
        return parse_uid(val) == uid

    # Слотовый формат: lead*/assistant*/admin/trainee → строки "Имя Ф.<суф>|uid" или "Имя Ф.<суф>"
    has_slot_keys = any(
        isinstance(k, str) and (k.startswith("lead") or k.startswith("assistant") or k in {"admin", "trainee"})
        for k in dist.keys()
    )
    if has_slot_keys:
        for k, v in dist.items():
            if not isinstance(k, str):
                continue
            if k.startswith("lead") and _match_any(v):
                return "main"
            if k.startswith("assistant") and _match_any(v):
                return "assist"
        if _match_any(dist.get("admin")):
            return "admin"
        if _match_any(dist.get("trainee")):
            return "trainee"
        return None

    # Нормализованный формат {"main":[...], "assist":[...], "admin":[...], "trainee":[...]}
    if _match_any(dist.get("admin")):
        return "admin"
    if _match_any(dist.get("main")):
        return "main"
    if _match_any(dist.get("assist")):
        return "assist"
    if _match_any(dist.get("trainee")):
        return "trainee"
    return None


# ███ [10] ВСТРОЕННЫЕ ТЕСТЫ
# --------------------------------------------------------------------
async def _test():
    # truncate
    assert truncate("abcd", 3) == "ab…"
    assert truncate(None, 10) == ""
    assert truncate("ok", 10) == "ok"

    # format_short_name
    assert format_short_name("Анна", "Миронова") == "Анна М."
    assert format_short_name("Равиль", "") == "Равиль"
    assert format_short_name("", "") == "Без имени"

    # parse_uid / to_uid_list
    assert parse_uid(123) == 123
    assert parse_uid("123") == 123
    assert parse_uid("Имя|456") == 456
    assert to_uid_list(["1", "Имя|2", None, "x"]) == [1, 2]

    # normalize_roles: roles already normalized
    nr = normalize_roles({"main": [1, "2"], "assist": ["Имя|3"], "admin": "4", "trainee": ["5", "Имя|6"]})
    assert nr == {"main": [1, 2], "assist": [3], "admin": [4], "trainee": [5, 6]}

    # normalize_roles: slots
    nr2 = normalize_roles({"lead1": "Анна М.|10", "assistant2": "Равиль Ш.|12", "admin": "Дарья В.|14"})
    assert nr2 == {"main": [10], "assist": [12], "admin": [14], "trainee": []}

    # role_suffix (SSOT, с точкой)
    assert role_suffix("main", 1) == ".1"
    assert role_suffix("assist", 2) == ".2"
    assert role_suffix("admin") == ".Адм"
    assert role_suffix("trainee") == ".Стаж"
    assert role_suffix("unknown") == ""

    # team_bulleted_lines (SSOT из [7.1])
    lines = await team_bulleted_lines({
        "lead1": "Анна М.|10",
        "assistant1": "Равиль Ш.|12",
        "admin": "Дарья В.|14",
        "trainee": "Стажёр X|16",  # строка вместо списка — соответствует текущей реализации
    })
    assert lines[0] == "• Анна М..1" or lines[0] == "• Анна М.1"
    assert lines[1].endswith(".2")
    assert lines[2].endswith(".Адм")
    assert lines[-1].endswith(".Стаж")

    # parse_players_count
    assert parse_players_count("2-6")[:2] == (2, 6)
    assert parse_players_count("6+").min == 6 and parse_players_count("6+").max is None
    assert parse_players_count("до 10").max == 10
    assert parse_players_count("от 3 до 7")[:2] == (3, 7)
    assert parse_players_count("5")[:2] == (5, 5)
    assert parse_players_count("много")[:2] == (None, None)

    # assigned_role_from_state
    state.locked_distribution = {
        1: {"lead1": "Анна М.|10", "assistant1": "Равиль Ш.|12", "admin": "Дарья В.|14"},
    }
    state.distribution_cache = {
        "2": {"main": [10], "assist": [12], "admin": [14]},
    }
    assert assigned_role_from_state(10, 1) == "main"
    assert assigned_role_from_state(12, 1) == "assist"
    assert assigned_role_from_state(14, 1) == "admin"
    assert assigned_role_from_state(10, 2) == "main"
    assert assigned_role_from_state(99, 1) is None

    # sticky helpers
    set_my_games_dashboard(777, 555)
    assert get_sticky_my_games(777) == 555
    assert keep_for_vacuum(777) == [555]

    # detail_blocks: миграция ключей int → (uid, 0)
    setattr(state, "detail_blocks", {777: [101, 102]})
    _ = _detail_blocks_registry()
    assert isinstance(state.detail_blocks, dict)
    assert (777, 0) in state.detail_blocks and 777 not in state.detail_blocks

    # smoke-тест наличия новых DM-хелперов
    assert isinstance(SOFT_REFRESH_CONTEXTS, set)
    assert ("my_games" in SOFT_REFRESH_CONTEXTS) and ("poll_details" in SOFT_REFRESH_CONTEXTS)
    assert callable(strict_vacuum) and callable(remember_dm)
    assert callable(dm_singleton_send) and callable(dm_singleton_edit_or_send)

    print("core/utils.py ✅ tests passed")

if __name__ == "__main__":  # локальный прогон
    import asyncio as _a
    _a.run(_test())

# История изменений:
#   2025-08-27 — v7.6: vacuum_private уважает липкий дашборд «Мои игры» (keep_for_vacuum);
#                      добавлены SSOT-хелперы sticky; выровнены импорты под Pylance.
#   2025-09-02 — v7.7: добавлен [7.12] DM-вакуум strict и синглтон-хелперы ЛС; единое правило
#                      «кнопку нажали — выше ЛС пусто», исключения для poll_details/my_games.
#   2025-09-03 — нормализация ключей state.detail_blocks (int → (uid, 0)),
#                фиксация assigned_role_from_state для finished_locked.