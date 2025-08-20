# core/utils.py — единый слой утилит и SSOT-хелперов
# ─────────────────────────────────────────────────────────────────────────────
"""
Единый набор утилит для MasterBot.

Версия 7.3 · 2025-08-18
──────────────────────────────────────────────────────────────────────────────
• Добавлены truncate() и parse_players_count() — требуются my_games/amocrm.
• NEW: assigned_role_from_state(uid, deal_id) — единый SSOT-хелпер для ролей.
• SSOT-хелперы: short_name/role_suffix/team_bulleted_lines.
• Нормализация ролей и разбор слотов: normalize_roles/parse_uid/to_uid_list.
• Резолвер общего чата: resolve_notify_chat_id() — понимает строковые ID.
• Канонический «пылесос» ЛС: vacuum_private() + совместимый delete_previous_private_messages().
  Поддержка хранения detail_blocks по ключам uid И (uid, deal_id).

Правила:
— async def используем только если внутри есть await.
— Хелперы, работающие в памяти, — обычные def.
— Никаких дублирующих реализаций в модулях-вызывателях: импортировать отсюда.
"""

from __future__ import annotations

# ███ [1] IMPORTS & TYPES
# --------------------------------------------------------------------
import asyncio
import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union, NamedTuple

try:
    # aiogram 3.x
    from aiogram import Bot
except Exception:  # pragma: no cover
    Bot = Any  # type: ignore

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
    # парсинг домена
    "parse_players_count",
    # роли из state
    "assigned_role_from_state",
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


# ███ [2] ИМЕНА И ФОРМАТЫ — SSOT
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

    # Слот "Имя Ф.|12345"
    if isinstance(subject, str):
        if "|" in subject:
            left, right = subject.split("|", 1)
            left = (left or "").strip()
            _ = parse_uid(right)  # валидация правой части
            return left or "Без имени"
        if subject.isdigit():  # Просто "12345"
            subject = int(subject)
        else:
            return (subject or "").strip() or "Без имени"

    # Словарь с first_name/last_name
    if isinstance(subject, dict):
        return format_short_name(subject.get("first_name"), subject.get("last_name"))

    # uid: запросим профиль (поддержка sync/async get_user_info)
    if isinstance(subject, int):
        try:
            ui = get_user_info(subject)
            if asyncio.iscoroutine(ui):
                ui = await ui  # type: ignore[func-returns-value]
            if isinstance(ui, dict) and (ui.get("first_name") or ui.get("last_name")):
                return format_short_name(ui.get("first_name"), ui.get("last_name"))
        except Exception as e:  # pragma: no cover
            logger.debug("[short_name] get_user_info failed for %s: %s", subject, e)
        return f"uid:{subject}"

    return "Без имени"


def role_suffix(role: str, index: Optional[int] = None) -> str:
    """
    Возвращает суффикс роли для уведомлений:
    main -> .1/.2 (по index),
    assist -> .1/.2,
    admin -> .Адм,
    trainee -> .Стаж
    """
    r = (role or "").lower()
    if r in ("main", "assist"):
        if index is None:
            return ""
        return f".{index}"
    if r == "admin":
        return ".Адм"
    if r == "trainee":
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

    # Основные ведущие — поддержим до 4 на будущее
    for i in range(1, 5):
        k = f"lead{i}"
        u = parse_uid(slots.get(k))
        if isinstance(u, int):
            main.append(u)
    # Помощники — поддержим до 4
    for i in range(1, 5):
        k = f"assistant{i}"
        u = parse_uid(slots.get(k))
        if isinstance(u, int):
            assist.append(u)
    # Админ
    u = parse_uid(slots.get("admin"))
    if isinstance(u, int):
        admin.append(u)
    # Стажёры (может быть список слотов/uid)
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

    # Уже нормальная форма?
    if all(k in raw for k in ("main", "assist", "admin")):
        out = {
            "main": to_uid_list(raw.get("main")),
            "assist": to_uid_list(raw.get("assist")),
            "admin": to_uid_list(raw.get("admin")),
            "trainee": to_uid_list(raw.get("trainee")),
        }
        return out

    # Иначе считаем «слотами»
    return _uids_from_slots(raw)


# ███ [4] СПИСКИ КОМАНДЫ ДЛЯ УВЕДОМЛЕНИЙ
# --------------------------------------------------------------------
async def team_bulleted_lines(
    roles_or_slots: Dict[str, Any],
    *,
    prefer_slot_names: bool = True,
) -> List[str]:
    """
    Строит строки команды для уведомлений.
    При prefer_slot_names=True сначала пытается взять имя из слота "Имя|uid",
    чтобы не делать лишних запросов к профилю. Если имени нет — обращается к БД.

    Поддерживает оба формата входа: роли и слоты.
    """
    lines: List[str] = []

    # Если это не нормализованные роли — считаем, что пришли слоты (для имён)
    slots: Dict[str, Any] = {}
    if not all(k in roles_or_slots for k in ("main", "assist", "admin")):
        slots = dict(roles_or_slots)

    # Нормализуем uid-списки
    norm = normalize_roles(roles_or_slots)

    async def _name_from_uid_or_slot(uid: int, slot_key: Optional[str], index_in_role: Optional[int], role: str) -> str:
        # 1) если есть слот и prefer_slot_names — используем имя слева
        if prefer_slot_names and slot_key and slot_key in slots:
            val = slots.get(slot_key)
            if isinstance(val, str) and "|" in val:
                left, _ = val.split("|", 1)
                n = (left or "").strip()
                if n:
                    return f"{n}{role_suffix(role, index_in_role)}"
        # 2) иначе — short_name(uid)
        n = await short_name(uid)
        return f"{n}{role_suffix(role, index_in_role)}"

    # MAIN
    for i, uid in enumerate(norm["main"], start=1):
        skey = f"lead{i}"
        lines.append(f"• {await _name_from_uid_or_slot(uid, skey, i, 'main')}")
    # ASSIST
    for i, uid in enumerate(norm["assist"], start=1):
        skey = f"assistant{i}"
        lines.append(f"• {await _name_from_uid_or_slot(uid, skey, i, 'assist')}")
    # ADMIN
    if norm["admin"]:
        uid = norm["admin"][0]
        lines.append(f"• {await _name_from_uid_or_slot(uid, 'admin', None, 'admin')}")
    # TRAINEE
    for uid in norm.get("trainee", []):
        lines.append(f"• {await _name_from_uid_or_slot(uid, None, None, 'trainee')}")

    return lines


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

    Приоритеты (первое валидное значение побеждает):
      1) settings.NOTIFY_CHAT_ID
      2) settings.NOTIFY_CHAT_IDS[0]
      3) settings.POLLS_CHAT_ID
      4) settings.PRIMARY_CHAT_ID
      5) settings.LEADERS_CHAT_ID
      6) settings.TEAM_CHAT_ID
      7) settings.ADMIN_CHAT_ID
      8) state.admin_chat_id   ← важный фолбэк (куда уже приходят опросы)

    Возвращает int или None, если подходящий чат не найден.
    Игнорирует плейсхолдеры и невалидные значения.
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
            getattr(state, "admin_chat_id", None),  # ← supergroup/id из chat_id.json
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

# ███ [6] «ПЫЛЕСОС» ЛС — КАНОНИЧЕСКАЯ РЕАЛИЗАЦИЯ (бережём главное меню)
# --------------------------------------------------------------------
from typing import Optional, Sequence, Tuple, Any, List
import logging
import contextlib

logger = logging.getLogger(__name__)

async def vacuum_private(uid: int, keep: Optional[Sequence[int]] = None) -> None:
    """
    Удаляет все устаревшие личные сообщения пользователя, оставляя только `keep`.
    ВАЖНО: Сообщение главного меню теперь защищено автоматически:
           если state.menu_message_id содержит id меню для uid, мы всегда
           добавляем его в keep, чтобы меню не исчезало.

    Поддерживаются форматы state.*:
      • state.last_user_messages[uid] -> List[int] | List[Message] | List[Tuple[int, int]]
      • state.detail_blocks[uid]      -> List[int]
      • state.detail_blocks[(uid, deal_id)] -> int | List[int]
      • state.menu_message_id         -> int | Dict[uid->int]     ← БЕРЕЖЁМ
      • state.personal_report_message_id -> int                   (может удаляться)
    """
    # локальный импорт, чтобы избежать циклов
    from aiogram import Bot
    from core.state import state  # гарантируем актуальный объект

    keep_list = list(keep or [])
    keep_set = set(keep_list)

    # ── ДОБАВИМ ГЛАВНОЕ МЕНЮ В KEEP АВТОМАТИЧЕСКИ
    mm = getattr(state, "menu_message_id", None)
    if isinstance(mm, dict):
        mid = mm.get(uid)
        if isinstance(mid, int) and mid > 0:
            keep_set.add(mid)
    elif isinstance(mm, int) and mm > 0:
        keep_set.add(mm)

    try:
        bot = Bot.get_current()

        async def _safe_delete(chat_id: int, message_id: int) -> bool:
            try:
                await bot.delete_message(chat_id, message_id)
                return True
            except Exception:
                return False

        # 1) last_user_messages
        lum = getattr(state, "last_user_messages", {})
        if isinstance(lum, dict):
            msgs = lum.get(uid) or []
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
                    await _safe_delete(uid, mid)
            lum[uid] = new_list

        # 2) detail_blocks
        db = getattr(state, "detail_blocks", {})
        if isinstance(db, dict):
            if uid in db and isinstance(db[uid], list):
                new_list = []
                for mid in list(db[uid]):
                    if isinstance(mid, int) and mid in keep_set:
                        new_list.append(mid)
                    elif isinstance(mid, int):
                        await _safe_delete(uid, mid)
                db[uid] = new_list

            tuple_keys: List[Tuple[int, Any]] = [
                k for k in db.keys() if isinstance(k, tuple) and len(k) >= 2 and k[0] == uid
            ]
            for tkey in tuple_keys:
                val = db.get(tkey)
                if isinstance(val, list):
                    new_list = []
                    for mid in val:
                        if isinstance(mid, int) and mid in keep_set:
                            new_list.append(mid)
                        elif isinstance(mid, int):
                            await _safe_delete(uid, mid)
                    db[tkey] = new_list
                elif isinstance(val, int):
                    mid = val
                    if mid not in keep_set:
                        await _safe_delete(uid, mid)
                        with contextlib.suppress(Exception):
                            del db[tkey]

        # 3) personal_report_message_id — можно удалять (если не в keep)
        prm = getattr(state, "personal_report_message_id", None)
        if isinstance(prm, int) and (prm not in keep_set):
            ok = await _safe_delete(uid, prm)
            if ok:
                setattr(state, "personal_report_message_id", None)

        # 4) menu_message_id — НЕ УДАЛЯЕМ здесь, он уже в keep_set

    except Exception as e:  # pragma: no cover
        logger.debug("[vacuum_private] failed: %s", e)


# ███ [7] LEGACY-ВРАППЕР ПЫЛЕСОСА (не менялся)
# --------------------------------------------------------------------
async def delete_previous_private_messages(*args, **kwargs) -> None:
    """
    Совместимый враппер:
      delete_previous_private_messages(uid, keep=[...]) | delete_previous_private_messages(uid)
    """
    from core.state import state  # noqa
    if args and isinstance(args[0], int) and not kwargs.get("uid"):
        kwargs["uid"] = args[0]
    if "bot" in kwargs:
        kwargs.pop("bot", None)
    uid = kwargs.get("uid") or (args[1] if len(args) >= 2 and isinstance(args[1], int) else None)
    if not isinstance(uid, int):
        return
    keep = kwargs.get("keep") or (args[2] if len(args) >= 3 else None)
    await vacuum_private(uid, keep=keep)

# История изменений:
# 2025-08-18 • v7.5 — вакуум автоматически сохраняет сообщение главного меню (menu_message_id).




# ███ [8] ПАРСИНГ ПОЛЯ «ИГРОКИ» (2–6, 6+, до 10, от 3 до 7 и т.п.)
# --------------------------------------------------------------------
class PlayersRange(NamedTuple):
    """
    Унифицированный результат парсинга количества игроков.
    • min/max — могут быть None (если указана только верхняя/нижняя граница).
    • text    — нормализованный человекочитаемый вид: '2-6', '6+', 'до 10'.
    • avg     — усреднение, если обе границы известны; иначе min/max.
    Поведение совместимо с использованием как tuple (min, max, text, avg).
    """
    min: Optional[int]
    max: Optional[int]
    text: str
    avg: Optional[float]


_RANGE_SEP = r"[\-\–—]"  # дефис / en-dash / em-dash

def parse_players_count(raw: Any) -> PlayersRange:
    """
    Разбирает произвольное значение «кол-во игроков»:
      • '2-6', '2–6', '2 — 6'
      • '6+', '10+' → min=6/10, max=None
      • 'до 10', '≤10' → min=None, max=10
      • 'от 3 до 7', '>=3' → min=3, max=None (если верх не указан)
      • '5' → min=max=5
      • любое другое → (None, None, '—', None)

    Возвращает PlayersRange(min, max, text, avg).
    """
    s = "" if raw is None else str(raw)
    s_norm = s.strip().lower()
    s_norm = re.sub(r"\s+", " ", s_norm)

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

    # 1) 'a-b'
    m = re.search(rf"(\d+)\s*{_RANGE_SEP}\s*(\d+)", s_norm)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a > b:
            a, b = b, a
        return _mk(a, b)

    # 2) 'N+'
    m = re.search(r"(\d+)\s*\+", s_norm)
    if m:
        return _mk(int(m.group(1)), None)

    # 3) 'до N' / '<=N'
    m = re.search(r"(до|<=|≤)\s*(\d+)", s_norm)
    if m:
        return _mk(None, int(m.group(2)))

    # 4) 'от N (до M)?'
    m = re.search(r"от\s*(\d+)(?:\s*(?:до|–|-|—)\s*(\d+))?", s_norm)
    if m:
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else None
        return _mk(lo, hi)

    # 5) одиночное число
    m = re.fullmatch(r"\s*(\d+)\s*", s_norm)
    if m:
        n = int(m.group(1))
        return PlayersRange(n, n, str(n), float(n))

    # 6) fallback
    return PlayersRange(None, None, "—", None)


# ███ [9] ХЕЛПЕР РОЛЕЙ ИЗ STATE (SSOT)
# --------------------------------------------------------------------
def assigned_role_from_state(uid: int, deal_id: int) -> Optional[str]:
    """
    Возвращает роль ('main'|'assist'|'admin') пользователя по сделке из:
      1) state.locked_distribution[deal_id]  (в приоритете)
      2) state.distribution_cache[str(deal_id)]  (предварительный состав)

    Поддерживает оба формата:
      • новый «слотовый»: {'lead1': 'Имя|uid', 'assistant1': 'Имя|uid', 'admin': 'Имя|uid'}
      • legacy-ролевой:  {'main': [uids], 'assist': [uids], 'admin': [uids]}
    """
    uid = int(uid)
    did_i = int(deal_id)
    locked = getattr(state, "locked_distribution", {}) or {}
    cache  = getattr(state, "distribution_cache", {}) or {}

    # 1) утвердённый состав
    dist: Any = locked.get(did_i) or locked.get(str(did_i))  # типо-безопасно
    if not isinstance(dist, dict):
        # 2) предварительный (чаще строковый ключ)
        dist = cache.get(str(did_i)) or cache.get(did_i)
    if not isinstance(dist, dict):
        return None

    def _match_slot(val: Any) -> bool:
        return parse_uid(val) == uid

    # Слоты?
    if any(isinstance(k, str) and (k.startswith("lead") or k.startswith("assistant") or k in {"admin", "trainee"})
           for k in dist.keys()):
        for k, v in dist.items():
            if not isinstance(k, str):
                continue
            if k.startswith("lead") and _match_slot(v):
                return "main"
            if k.startswith("assistant") and _match_slot(v):
                return "assist"
        if _match_slot(dist.get("admin")):
            return "admin"
        return None

    # Legacy-роли
    if parse_uid(dist.get("admin")) == uid or uid in to_uid_list(dist.get("admin")):
        return "admin"
    if uid in to_uid_list(dist.get("main")):
        return "main"
    if uid in to_uid_list(dist.get("assist")):
        return "assist"
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

    # role_suffix
    assert role_suffix("main", 1) == ".1"
    assert role_suffix("assist", 2) == ".2"
    assert role_suffix("admin") == ".Адм"
    assert role_suffix("trainee") == ".Стаж"
    assert role_suffix("unknown") == ""

    # team_bulleted_lines (используем имена из слотов, чтобы не дёргать БД)
    lines = await team_bulleted_lines({
        "lead1": "Анна М.|10",
        "assistant1": "Равиль Ш.|12",
        "admin": "Дарья В.|14",
        "trainee": ["Стажёр X|16", "17"],
    })
    assert lines[0].startswith("• Анна М..1")
    assert lines[1].startswith("• Равиль Ш..1")
    assert lines[2].startswith("• Дарья В..Адм")
    assert lines[3].endswith(".Стаж")
    assert lines[4].endswith(".Стаж")

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

    print("core/utils.py ✅ tests passed")

if __name__ == "__main__":  # локальный прогон
    import asyncio as _a
    _a.run(_test())

# История изменений:
#   2025-08-17 — v7.1: SSOT-утилиты, резолвер чата, пылесос, совместимость legacy.
#   2025-08-18 — v7.2: добавлены truncate/parse_players_count; расширен парсинг слотов/ролей.
#   2025-08-18 — v7.3: добавлен assigned_role_from_state (SSOT) + тесты.
