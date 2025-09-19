# handlers/poll_details.py — detail-view игр + manual-switches
# ─────────────────────────────────────────────────────────────────────────────
"""
Реактивные карточки игр (detail-view) для цикла распределения.

Версия 13.4 · 2025-08-24
──────────────────────────────────────────────────────────────────────────────
• Единый источник правды по распределению — state.distribution_cache[str(deal_id)].
• Жёсткая инварианта «1 пользователь = 1 роль» (приоритет main > assist > admin > trainee).
• Автоподбор в пустые слоты по «Светофору» (green→main, green|yellow→assist).
• Стажёр: только «красный», не занятый в других ролях; стажёр не влияет на укомплектованность.
• SWAP переносит кандидата между ролями, автоматически убирая его из прежних ролей.
• Кнопка «Утвердить» делегируется в handlers.polls_distribution, без автопереходов.
• Пылесос в ЛС: перед рендером деталей удаляются старые сообщения; после действий — перерисовка.
• Совместимость сигнатур refresh_deal_details:
  – новый стиль: refresh_deal_details(bot=Bot, deal_id=int, force_approved:bool=False, uid:Optional[int]=None)
  – старый стиль: refresh_deal_details(uid:int, deal_id:int)
"""

# ███ [0] IMPORTS & SETUP
# --------------------------------------------------------------------
from __future__ import annotations

import contextlib
import logging
import re
import time
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Set, Tuple

from aiogram import Bot, Router, types
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config import settings
from core.state import state
from core.utils import truncate, delete_previous_private_messages
from core.utils import public_game_title
try:
    # если есть современное ядро с вакуумом — используем
    from core.utils import vacuum_private as _vacuum_private  # type: ignore
except Exception:  # pragma: no cover
    _vacuum_private = None  # type: ignore

from services.gsheets import get_user_status_from_svetofor

logger = logging.getLogger(__name__)
router = Router(name="poll_details")

# Общие константы/регексы
POLL_BACK = "poll_back_to_games_list"
_OK_STATUSES = {"green", "yellow"}            # для помощников
_STATUS_RE = re.compile(r"[^\w\d]+", re.UNICODE)
# Требование администратора по пакетам (нормализованные названия)
_ADMIN_PKGS = {"стандарт", "стандарт+", "премиум", "vip", "вип", "биглион"}

# Локальный кэш статусов «Светофора»: key -> (status, ts)
_local_status_cache: Dict[str, Tuple[str, float]] = {}
STATUS_CACHE_TTL = 60 * 60 * 4  # 4 часа

# История изменений:
# • 2025-08-13 — пересобран импорт, единая точка пылесоса, локальный статус-кэш.
# • 2025-08-15 — добавлен import contextlib для suppress().
# • 2025-08-24 — починен SWAP (ручная установка), стабилизирован рендер и пылесос.


# ███ [1] HELPERS (normalize, role cfg, svetofor cache, tags/ids, invariants)
# --------------------------------------------------------------------
def _clean(s: str) -> str:
    return _STATUS_RE.sub(" ", (s or "")).lower().strip()


def _role_cfg(game_name: str) -> Dict[str, int]:
    """Возвращает требуемые количества ролей для игры (толерантный поиск)."""
    norm = _clean(game_name)
    best_ratio = 0.0
    best_cfg: Optional[Dict[str, int]] = None
    for key, cfg in (getattr(settings, "GAME_ROLE_MAPPING", {}) or {}).items():
        k_norm = _clean(str(key))
        if norm == k_norm or norm in k_norm or k_norm in norm:
            return cfg
        ratio = SequenceMatcher(None, norm, k_norm).ratio()
        if ratio > best_ratio:
            best_ratio, best_cfg = ratio, cfg
    return best_cfg or {"main_leaders": 1, "assistants": 0}


async def _status_cached(uid: int, game: str) -> str:
    """Локальный кэш статуса пользователя по игре из таблицы «Светофор»."""
    key = f"sv:{uid}:{_clean(game)}"
    now = time.time()
    if key in _local_status_cache:
        status, ts = _local_status_cache[key]
        if now - ts < STATUS_CACHE_TTL:
            return status
    try:
        status = await get_user_status_from_svetofor(uid, game)
    except Exception as e:  # pragma: no cover
        logger.warning("[svetofor] fail uid=%s game=%s: %s", uid, game, e)
        status = ""
    _local_status_cache[key] = (status, now)
    return status


# — регэксп валидного «Имя Ф.» (есть пробел и инициал с точкой)
_NAME_WITH_INITIAL_RE = re.compile(r"^\S+\s[А-ЯA-Z]\.$")


async def _short_name(uid: int) -> str:
    """
    Имя в формате «Имя Ф.».
    1) если в state.user_short лежит валидное «Имя Ф.» — используем его;
    2) иначе берём ФИО из БД и форматируем через core.utils.short_name();
    3) фолбэк «uid:{uid}».
    """
    try:
        cached = (getattr(state, "user_short", {}) or {}).get(uid)
        if isinstance(cached, str) and _NAME_WITH_INITIAL_RE.match(cached.strip()):
            return cached.strip()
    except Exception:
        pass

    # получаем ФИО из БД (совместимо с sync/async)
    try:
        from core.db import get_user_info
        ui = get_user_info(uid)
        if hasattr(ui, "__await__"):  # async-совместимость
            ui = await ui  # type: ignore[func-returns-value]
        ui = ui or {}
    except Exception:
        ui = {}

    first = str(ui.get("first_name") or "").strip()
    last = str(ui.get("last_name") or "").strip()
    if not first and not last:
        return f"uid:{uid}"

    # используем SSOT функцию short_name на словаре (чтобы получить «Имя Ф.»)
    from core.utils import short_name as _ssot_short_name
    subj = {"first_name": first, "last_name": last}
    sn = _ssot_short_name(subj)
    if hasattr(sn, "__await__"):
        sn = await sn  # type: ignore[func-returns-value]
    return str(sn or f"uid:{uid}").strip()


def _role_slots(need_main: int, need_assist: int) -> Tuple[List[str], List[str]]:
    """Возвращает списки ключей слотов для main/assist в distribution_cache."""
    leads = [f"lead{i}" for i in range(1, max(1, need_main) + 1)]
    assis = [f"assistant{i}" for i in range(1, max(0, need_assist) + 1)]
    return leads, assis


def _need_admin_by_package(pkg_raw: str) -> int:
    return 1 if _clean(pkg_raw) in _ADMIN_PKGS else 0


def _parse_uid(tag: Optional[str]) -> Optional[int]:
    """SSOT-парсер uid из строки слота/тега «…|uid»."""
    if not tag:
        return None
    s = str(tag).strip()
    if "|" in s:
        with contextlib.suppress(Exception):
            return int(s.rsplit("|", 1)[-1])
    with contextlib.suppress(Exception):
        return int(s)
    return None


async def _fmt(uid_: int, role_key: str) -> str:
    """
    Формирует тег для кэша распределения/подтверждений:
      main    -> «Имя Ф.1|uid»
      assist  -> «Имя Ф.2|uid»
      admin   -> «Имя Ф.Адм|uid»
      trainee -> «Имя Ф.Стаж|uid»
    """
    from core.utils import role_suffix as _role_suffix
    human = await _short_name(uid_)
    idx = 1 if role_key == "main" else (2 if role_key == "assist" else None)
    suffix = _role_suffix(role_key, idx)
    return f"{human}{suffix}|{uid_}".strip()


async def _ensure_single_role(dist: Dict[str, str], need_main: int, need_assist: int) -> None:
    """Инварианта «один UID — одна роль»; приоритет: main > assist > admin > trainee."""
    leads, assis = _role_slots(need_main, need_assist)
    priority_keys: List[str] = [*leads, *assis, "admin", "trainee"]

    seen: Set[int] = set()
    for key in priority_keys:
        uid = _parse_uid(dist.get(key))
        if uid is None:
            continue
        if uid in seen:
            dist.pop(key, None)
        else:
            seen.add(uid)

    for key in list(dist.keys()):
        if key.startswith("lead"):
            idx = int("".join(ch for ch in key if ch.isdigit()) or "0")
            if idx < 1 or idx > need_main:
                dist.pop(key, None)
        elif key.startswith("assistant"):
            idx = int("".join(ch for ch in key if ch.isdigit()) or "0")
            if idx < 1 or idx > need_assist:
                dist.pop(key, None)


async def _normalize_tag_texts(dist: Dict[str, str], need_main: int, need_assist: int) -> None:
    """Приводит тексты слотов к «Имя Ф.+суффикс|uid», пересобирая по uid."""
    leads, assis = _role_slots(need_main, need_assist)
    for role, keys in (("main", leads), ("assist", assis)):
        for k in keys:
            uid = _parse_uid(dist.get(k))
            if uid:
                dist[k] = await _fmt(uid, role)
    for role, k in (("admin", "admin"), ("trainee", "trainee")):
        uid = _parse_uid(dist.get(k))
        if uid:
            dist[k] = await _fmt(uid, role)


async def _vacuum(uid: int, *, keep: Optional[List[int]] = None, bot: Optional[Bot] = None) -> None:
    """
    Унифицированный вызов «пылесоса».
    Если есть core.utils.vacuum_private — используем его. Иначе fallback на delete_previous_private_messages.
    """
    keep = keep or []
    if _vacuum_private:
        with contextlib.suppress(Exception):
            await _vacuum_private(uid, keep=keep)  # type: ignore[misc]
        return
    # fallback на старую функцию
    try:
        if bot is not None:
            await delete_previous_private_messages(bot, uid, keep=keep)  # новая сигнатура
        else:
            await delete_previous_private_messages(uid)  # старая сигнатура
    except TypeError:
        with contextlib.suppress(Exception):
            await delete_previous_private_messages(uid)  # type: ignore[misc]


async def _render_detail(uid: int, deal_id: int, bot: Bot, *, force_approved: bool = False) -> None:
    """
    Рисует карточку игры user→deal_id через единый билдер (_build_blocks):
      • стабильная последовательность секций (header → role → role_alt → … → trainee → mgr → back);
      • инварианта «1 uid → 1 роль» и автоподбор/стажёр — внутри билдера;
      • кнопки «Утвердить/Стоп/Назад».

    ВАЖНО:
    • Перед показом — жёсткий пылесос (оставляем только новый блок).
      (на время пылесоса подавляем сохранение дашборда отчёта лидера)
    • После отправки — фиксируем реестр сообщений через _set_block(...),
      а также ЛИНКУЕМ блок в last_user_messages, чтобы глобальные вакуумы могли корректно его убирать.
    """
    # Диагностика: снимок реестра до рендера
    try:
        from core.state import state as _st
        snapshot_before = {
            did: len(v or [])
            for (u, did), v in (getattr(_st, "detail_blocks", {}) or {}).items()
            if int(u) == int(uid)
        }
        logger.info("poll_details[render] pre-vacuum uid=%s deal=%s blocks=%s", uid, deal_id, snapshot_before)
    except Exception:
        pass

    # 0) Пылесос перед рендером (временно разрешаем снести дашборд отчёта)
    from core.state import state as _state
    _prev_flag = bool(getattr(_state, "suppress_report_keep", False))
    setattr(_state, "suppress_report_keep", True)
    logger.debug("[details:vacuum] uid=%s deal=%s suppress_report_keep=True", uid, deal_id)
    try:
        await _vacuum(uid, bot=bot, keep=[])
    finally:
        setattr(_state, "suppress_report_keep", _prev_flag)
        logger.debug("[details:vacuum] uid=%s deal=%s suppress_report_keep=%s(restored)", uid, deal_id, _prev_flag)

    # 1) Проверяем, что сделка есть в текущем опросе
    deal = next((d for d in (state.current_poll_deals or []) if int(d.get("id") or 0) == int(deal_id)), None)
    if not deal:
        # Сообщение об ошибке тоже фиксируем как блок, чтобы не потерять контекст
        msg = await bot.send_message(uid, "⚠️ Игра не найдена или уже закрыта.")
        _set_block(uid, deal_id, [msg])
        _link_to_last_user_messages(uid, [msg])
        logger.info("poll_details[render] created-ERROR uid=%s deal=%s mids=[%s]", uid, deal_id, getattr(msg, "message_id", None))
        return

    # 2) Строим тексты/кнопки единственным источником (совпадает с refresh)
    built = await _build_blocks(bot=bot, uid=uid, deal_id=deal_id, force_approved=force_approved)  # type: ignore[name-defined]
    seq = built.get("sequence") or []

    # 3) Нормализуем и отправляем сообщения в точном порядке
    msgs: List[types.Message] = []
    for text, kb in seq:
        t = (text if text is not None else "\u2060") or "\u2060"
        m = await bot.send_message(
            uid,
            t,
            parse_mode="Markdown",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
        msgs.append(m)

    # 4) Сохраняем реестр и линкуем в last_user_messages — для корректной уборки внешним пылесосом
    _set_block(uid, deal_id, msgs)
    _link_to_last_user_messages(uid, msgs)

    # Диагностика: снимок реестра после рендера
    try:
        from core.state import state as _st
        snapshot_after = {
            did: len(v or [])
            for (u, did), v in (getattr(_st, "detail_blocks", {}) or {}).items()
            if int(u) == int(uid)
        }
        logger.info(
            "poll_details[render] created uid=%s deal=%s msgs=%s blocks=%s",
            uid, deal_id, [getattr(m, "message_id", None) for m in msgs], snapshot_after
        )
    except Exception:
        logger.info(
            "poll_details[render] created uid=%s deal=%s msgs=%s",
            uid, deal_id, [getattr(m, "message_id", None) for m in msgs]
        )

# История изменений:
# 2025-09-05 — FIX: временно подавляем сохранение отчёта лидера при рендере деталей (suppress_report_keep)


# ███ [2.1] RENDER & REFRESH
# --------------------------------------------------------------------

# ────────────────────────────────────────────────────────────────────
# [2.1.1] Импорты и базовые константы
# ────────────────────────────────────────────────────────────────────

import contextlib
import logging
from typing import Any, Dict, List, Optional, Tuple, Set

from aiogram import Bot, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

logger = logging.getLogger(__name__)

# Если константа объявлена выше в файле — используем её. Иначе — мягкий дефолт.
try:  # noqa: SIM105
    POLL_BACK  # type: ignore[name-defined]
except NameError:  # pragma: no cover
    POLL_BACK = "poll_back_to_games_list"  # type: ignore[assignment]

# Пояснение:
# _render_detail (из блока [2]) — полный первичный рендер.
# render_detail  — обёртка: если уже есть сообщения → тихий refresh, иначе полный рендер.
# refresh_deal_details — «тихий» апдейт; точечно правит текст/markup по позициям, без массового удаления.


# ────────────────────────────────────────────────────────────────────
# [2.1.2] Утилиты последовательностей и «тихих» правок в Telegram
# ────────────────────────────────────────────────────────────────────
def _normalize_seq(
    sequence: List[Tuple[Optional[str], Optional[InlineKeyboardMarkup]]]
) -> List[Tuple[str, Optional[InlineKeyboardMarkup]]]:
    """Гарантируем текст для каждого элемента (None → U+2060)."""
    out: List[Tuple[str, Optional[InlineKeyboardMarkup]]] = []
    for text, kb in sequence:
        t = (text if text is not None else "\u2060") or "\u2060"
        out.append((t, kb))
    return out


async def _safe_send(bot: Bot, chat_id: int, text: str, kb: Optional[InlineKeyboardMarkup]) -> types.Message:
    """Единообразная отправка (Markdown, без превью)."""
    return await bot.send_message(
        chat_id, text, parse_mode="Markdown",
        reply_markup=kb, disable_web_page_preview=True
    )


async def _safe_edit_text_and_markup(
    bot: Bot,
    chat_id: int,
    msg: types.Message,
    *,
    text: Optional[str],
    kb: Optional[InlineKeyboardMarkup],
) -> Tuple[str, Optional[types.Message]]:
    """
    Пробуем тихо обновить сообщение:
      • если text is not None — edit_message_text(+reply_markup),
      • иначе — edit_message_reply_markup.
    Возврат: ("ok" | "not_modified" | "not_found" | "error", new_msg_if_replaced)
    """
    if text is not None:
        st = await _edit_text_status(bot, chat_id, msg.message_id, text)
        if st == "not_found":
            # Сообщение исчезло — шлём новое
            new_msg = await _safe_send(bot, chat_id, text or (msg.text or "\u2060") or "\u2060", kb)
            return "ok", new_msg
        if st in {"ok", "not_modified"}:
            st_m = await _edit_markup_status(bot, chat_id, msg.message_id, kb)
            if st_m == "not_found":
                new_msg = await _safe_send(bot, chat_id, text or (msg.text or "\u2060") or "\u2060", kb)
                return "ok", new_msg
            return st, None
        return st, None
    else:
        st = await _edit_markup_status(bot, chat_id, msg.message_id, kb)
        if st == "not_found":
            new_msg = await _safe_send(bot, chat_id, (msg.text or "\u2060") or "\u2060", kb)
            return "ok", new_msg
        return st, None


# ────────────────────────────────────────────────────────────────────
# [2.1.3] Реестр сообщений деталей: хранение (List[int]) + миграция ключей
# ────────────────────────────────────────────────────────────────────
def _who_called(max_up: int = 3) -> str:
    """Короткая цепочка вызовов — для быстрой диагностики, кто изменил реестр."""
    import inspect
    frm = inspect.stack()[2:max_up+2]
    chain = [f"{f.function}:{f.lineno}" for f in frm]
    return " <= ".join(chain)


def _ensure_registry() -> None:
    """
    Инициализирует контейнеры в state:
      • detail_blocks: Dict[Tuple[int,int], List[int]] — список message_id карточки деталей (SSOT)
      • detail_index : Dict[Tuple[int,int], Dict[str,int]] — карта key->message_id (header/main/…)
      • last_user_messages: Dict[int, List[int|Message]] — общий список сообщений в ЛС
    """
    from core.state import state as _st
    if not isinstance(getattr(_st, "detail_blocks", None), dict):
        _st.detail_blocks = {}
    if not isinstance(getattr(_st, "detail_index", None), dict):
        _st.detail_index = {}
    if not isinstance(getattr(_st, "last_user_messages", None), dict):
        _st.last_user_messages = {}


def _reg_key(uid: int, deal_id: int) -> Tuple[int, int]:
    return (int(uid), int(deal_id))


class _MsgStub:
    """Лёгкая обёртка вокруг message_id, чтобы обращаться как к Message."""
    __slots__ = ("message_id", "text")
    def __init__(self, mid: int, text: Optional[str] = None) -> None:
        self.message_id = int(mid)
        self.text = (text or "").strip()


def _to_msg_list(raw: Any) -> List[types.Message]:
    """
    Унифицирует разные форматы хранения блока:
      • List[Message]         — старый формат
      • List[int] / int       — SSOT-формат
      • None/прочее           — пусто
    Возвращает List[Message]-совместимый список (stub'ы на id).
    """
    out: List[types.Message] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, types.Message):
                out.append(item)
            else:
                with contextlib.suppress(Exception):
                    out.append(_MsgStub(int(item)))  # type: ignore[return-value]
    elif isinstance(raw, int) and raw > 0:
        out.append(_MsgStub(raw))  # type: ignore[return-value]
    return out


def _extract_ids(msgs: List[types.Message]) -> List[int]:
    ids: List[int] = []
    for m in msgs:
        mid = getattr(m, "message_id", None)
        if isinstance(mid, int):
            ids.append(mid)
    return ids


def _cleanup_registry_keys() -> None:
    """
    Нормализует ключи detail_blocks/detail_index к виду (uid:int, deal_id:int).
    Чистит строки вида "uid:deal", "uid|deal", ("uid","deal") и т.п., переводя в tuple[int,int].
    Удаляет мусорные ключи, чтобы фоновые итераторы `for (uid, deal_id), ...` не падали.
    """
    from core.state import state as _st

    def _normalize_dict_keys(d: Any) -> None:
        if not isinstance(d, dict):
            return
        add: Dict[Tuple[int, int], Any] = {}
        remove: List[Any] = []
        for k, v in list(d.items()):
            # уже корректный ключ-двойка
            if isinstance(k, tuple) and len(k) == 2:
                try:
                    nk = (int(k[0]), int(k[1]))
                except Exception:
                    remove.append(k)
                    continue
                if nk != k:
                    add[nk] = v
                    remove.append(k)
                continue
            # строковые ключи "uid:deal" / "uid|deal" / "uid,deal" / "uid/deal"
            if isinstance(k, str):
                for sep in (":", "|", ",", "/"):
                    if sep in k:
                        left, right = k.split(sep, 1)
                        with contextlib.suppress(Exception):
                            nk = (int(left.strip()), int(right.strip()))
                            add[nk] = v
                            remove.append(k)
                        break
                else:
                    remove.append(k)
                continue
            # что-то ещё — удаляем
            remove.append(k)
        for k in remove:
            d.pop(k, None)
        d.update(add)

    _normalize_dict_keys(getattr(_st, "detail_blocks", None))
    _normalize_dict_keys(getattr(_st, "detail_index", None))


def _get_block(uid: int, deal_id: int) -> List[types.Message]:
    from core.state import state as _st
    _ensure_registry()
    _cleanup_registry_keys()
    raw = (_st.detail_blocks or {}).get(_reg_key(uid, deal_id))
    msgs = _to_msg_list(raw)
    logger.debug(
        "poll_details[registry] get_block uid=%s deal=%s mids=%s len=%s",
        uid, deal_id, [getattr(m, "message_id", None) for m in msgs], len(msgs)
    )
    return msgs


def _save_detail_index(uid: int, deal_id: int, msgs: List[types.Message]) -> None:
    """
    Построить и сохранить карту key->message_id для деталей (header/main/main_alt/assist/...).
    Добавлены страховки: если эвристика не нашла header/back — берём первый/последний сообщения.
    """
    from core.state import state as _st
    _ensure_registry()
    _cleanup_registry_keys()

    idx_msgs = _index_current_blocks(msgs)
    mapping: Dict[str, int] = {}
    for k, m in idx_msgs.items():
        mid = getattr(m, "message_id", None)
        if isinstance(mid, int):
            mapping[k] = mid

    if "header" not in mapping and msgs:
        hmid = getattr(msgs[0], "message_id", None)
        if isinstance(hmid, int):
            mapping["header"] = hmid
    if "back" not in mapping and msgs:
        bmid = getattr(msgs[-1], "message_id", None)
        if isinstance(bmid, int):
            mapping["back"] = bmid

    _st.detail_index[_reg_key(uid, deal_id)] = mapping
    logger.debug("poll_details[registry] index-saved uid=%s deal=%s map=%s", uid, deal_id, mapping)


def _get_index_by_mapping(uid: int, deal_id: int) -> Dict[str, types.Message]:
    """
    Вернуть карту key->MessageStub по сохранённой detail_index.
    Это позволяет refresh-у адресно редактировать сообщения по ID, даже если у нас только List[int].
    """
    from core.state import state as _st
    _ensure_registry()
    _cleanup_registry_keys()

    mapping: Dict[str, int] = {}
    try:
        mapping = (getattr(_st, "detail_index", {}) or {}).get(_reg_key(uid, deal_id), {}) or {}
    except Exception:
        mapping = {}

    out: Dict[str, types.Message] = {}
    for k, mid in mapping.items():
        with contextlib.suppress(Exception):
            out[k] = _MsgStub(int(mid))  # type: ignore[return-value]

    logger.debug("poll_details[registry] get_index uid=%s deal=%s keys=%s", uid, deal_id, sorted(mapping.keys()))
    return out


def _update_detail_index(uid: int, deal_id: int, key: str, message_id: Optional[int]) -> None:
    """
    Обновить карту key->message_id: записать новый id для ключа (или удалить ключ, если message_id=None).
    """
    from core.state import state as _st
    _ensure_registry()
    _cleanup_registry_keys()

    reg_key = _reg_key(uid, deal_id)
    entry = (getattr(_st, "detail_index", {}) or {}).get(reg_key)
    if not isinstance(entry, dict):
        entry = {}
        _st.detail_index[reg_key] = entry

    prev = entry.get(key) if isinstance(entry.get(key), int) else None
    if message_id is None:
        entry.pop(key, None)
    else:
        entry[key] = int(message_id)

    logger.debug(
        "poll_details[registry] index-updated uid=%s deal=%s key=%s %s->%s caller=%s",
        uid, deal_id, key, prev, message_id, _who_called()
    )


def _set_block(uid: int, deal_id: int, msgs: List[types.Message]) -> None:
    """
    Сохраняем реестр сообщений деталей как List[int] — SSOT-совместимо.
    Дополнительно сохраняем карту key->message_id (detail_index), чтобы refresh мог работать с ID.
    """
    from core.state import state as _st
    _ensure_registry()
    _cleanup_registry_keys()

    key = _reg_key(uid, deal_id)
    new_ids = _extract_ids(msgs)

    # прежнее состояние — для диагностики
    prev_ids = []
    if isinstance(getattr(_st, "detail_blocks", None), dict):
        prev_raw = (getattr(_st, "detail_blocks", {}) or {}).get(key)
        prev_ids = prev_raw if isinstance(prev_raw, list) else ([prev_raw] if isinstance(prev_raw, int) else [])
        prev_ids = [int(x) for x in prev_ids if isinstance(x, int)]

    if not isinstance(getattr(_st, "detail_blocks", None), dict):
        _st.detail_blocks = {}
    _st.detail_blocks[key] = new_ids

    try:
        _save_detail_index(uid, deal_id, msgs)
    finally:
        logger.debug(
            "poll_details[registry] set_block uid=%s deal=%s was=%s -> new=%s caller=%s",
            uid, deal_id, prev_ids, new_ids, _who_called()
        )
        if not new_ids:
            logger.info("poll_details[registry] set_block EMPTY uid=%s deal=%s", uid, deal_id)


def _detach_from_last_user_messages(uid: int, keep: List[types.Message]) -> None:
    """
    Удаляет detail-сообщения из state.last_user_messages[uid], чтобы внешние вакуумы
    не сносили карточку, и «тихие» правки были возможны.
    Поддерживает как Message-объекты, так и int message_id в текущем списке пользователя.
    """
    from core.state import state as _st
    lst = getattr(_st, "last_user_messages", None)
    if not isinstance(lst, dict):
        logger.debug("poll_details[registry] detach skipped: no last_user_messages dict")
        return
    cur = lst.get(int(uid))
    if not isinstance(cur, list):
        logger.debug("poll_details[registry] detach skipped: last_user_messages[%s] is not list", uid)
        return

    keep_ids = set(_extract_ids(keep))

    def _id_of(x: Any) -> Optional[int]:
        mid = getattr(x, "message_id", None)
        if isinstance(mid, int):
            return mid
        with contextlib.suppress(Exception):
            return int(x)
        return None

    before_len = len(cur)
    filtered: List[Any] = []
    removed: List[int] = []
    for item in cur:
        mid = _id_of(item)
        if isinstance(mid, int) and mid in keep_ids:
            removed.append(mid)
            continue
        filtered.append(item)
    lst[int(uid)] = filtered
    logger.debug(
        "poll_details[registry] detach uid=%s removed=%s before=%s after=%s keep=%s",
        uid, removed, before_len, len(filtered), sorted(list(keep_ids))
    )


def _link_to_last_user_messages(uid: int, keep: List[types.Message]) -> None:
    """
    Регистрирует detail-сообщения в state.last_user_messages[uid], чтобы
    глобальные вакуумы (меню/дашборд) могли их корректно удалить.
    Не добавляет дубликаты; поддерживает и int, и Message.
    """
    from core.state import state as _st
    lst = getattr(_st, "last_user_messages", None)
    if not isinstance(lst, dict):
        _st.last_user_messages = {}
        lst = _st.last_user_messages

    cur = lst.get(int(uid))
    if not isinstance(cur, list):
        cur = []
        lst[int(uid)] = cur

    def _id_of(x: Any) -> Optional[int]:
        mid = getattr(x, "message_id", None)
        if isinstance(mid, int):
            return mid
        with contextlib.suppress(Exception):
            return int(x)
        return None

    existing_ids: Set[int] = {m for m in (_id_of(x) for x in cur) if isinstance(m, int)}
    added: List[int] = []
    for mid in _extract_ids(keep):
        if mid not in existing_ids:
            cur.append(mid)  # храним как int — SSOT-совместимо
            existing_ids.add(mid)
            added.append(mid)

    logger.debug("poll_details[registry] link uid=%s added=%s total=%s", uid, added, len(cur))




# ────────────────────────────────────────────────────────────────────
# [2.1.4] Публичные функции рендера (обёртка и «тихий» апдейт)
# ────────────────────────────────────────────────────────────────────
async def render_detail(*, bot: Bot, uid: int, deal_id: int, force_approved: bool = False) -> None:
    """
    Если блок уже открыт — делаем «тихий» refresh.
    Если блока нет — выполняем первичный рендер через _render_detail().
    ВАЖНО: _render_detail сам записывает detail_blocks/detail_index и
    отвязывает их от last_user_messages — здесь повторно ничего не трогаем.
    """
    prev_msgs = _get_block(uid, deal_id)
    logger.info("poll_details[render] start uid=%s deal=%s has_prev=%s", uid, deal_id, bool(prev_msgs))
    if prev_msgs:
        await refresh_deal_details(bot=bot, uid=uid, deal_id=deal_id, force_approved=force_approved)
        return

    await _render_detail(uid=uid, deal_id=deal_id, bot=bot, force_approved=force_approved)
    created = _get_block(uid, deal_id)
    logger.info(
        "poll_details[render] created uid=%s deal=%s msgs=%s",
        uid, deal_id, [getattr(m, "message_id", None) for m in created]
    )
    # _render_detail уже сделал _set_block и _detach_from_last_user_messages


async def refresh_deal_details(*, bot: Bot, uid: int, deal_id: int, force_approved: bool = False) -> None:
    """
    «Тихий» апдейт карточки деталей. Ничего массово не удаляет:
    правит текст/markup по адресным message_id из detail_index,
    при рассинхроне дописывает «хвост» и обновляет реестр.
    """
    # 0) Текущее состояние блока
    current_msgs: List[types.Message] = _get_block(uid, deal_id)
    index = _get_index_by_mapping(uid, deal_id)

    cur_ids = [getattr(m, "message_id", None) for m in current_msgs]
    logger.debug(
        "poll_details[refresh] precheck uid=%s deal=%s cur_ids=%s index_keys=%s",
        uid, deal_id, cur_ids, sorted(index.keys())
    )

    if not current_msgs and not index:
        logger.info("poll_details[refresh] skip: block-not-open uid=%s deal=%s", uid, deal_id)
        return

    if not current_msgs and index:
        logger.warning(
            "poll_details[refresh] anomaly: index-exists-but-cur_ids-empty uid=%s deal=%s. "
            "Вероятно, внешний вакуум. Будет выполнена хвостовая перезапись.",
            uid, deal_id
        )

    # 1) Собираем свежие тексты/кнопки
    built = await _build_blocks(bot=bot, uid=uid, deal_id=deal_id, force_approved=force_approved)
    header_text = built.get("header_text")
    main_text, main_alt = built.get("main_text"), built.get("main_alt")
    assist_text, assist_alt = built.get("assist_text"), built.get("assist_alt")
    admin_text, admin_alt = built.get("admin_text"), built.get("admin_alt")
    trainee_text = built.get("trainee_text")
    mgr_kb = built.get("mgr_kb")
    back_kb = built.get("back_kb")

    # 2) Эталонная последовательность (alt сразу после своей роли)
    expected_keys: List[str] = ["header"]
    if main_text:
        expected_keys += ["main", "main_alt"]
    if assist_text:
        expected_keys += ["assist", "assist_alt"]
    if admin_text:
        expected_keys += ["admin", "admin_alt"]
    if trainee_text:
        expected_keys.append("trainee")
    if mgr_kb is not None:
        expected_keys.append("mgr")
    expected_keys.append("back")

    logger.debug("poll_details[refresh] expected_keys uid=%s deal=%s -> %s", uid, deal_id, expected_keys)

    # 3) Ключи текущих сообщений (по сохранённой карте key->id)
    mid_to_key: Dict[int, str] = {}
    for k, stub in index.items():
        mid = getattr(stub, "message_id", None)
        if isinstance(mid, int):
            mid_to_key[mid] = k

    current_keys_in_order: List[str] = []
    for m in current_msgs:
        mid = getattr(m, "message_id", None)
        if isinstance(mid, int):
            k = mid_to_key.get(mid)
            if k in expected_keys:
                current_keys_in_order.append(k)

    logger.info(
        "poll_details[refresh] begin uid=%s deal=%s cur_ids=%s index_keys=%s",
        uid, deal_id, cur_ids, sorted(index.keys())
    )
    logger.debug("poll_details[refresh] current_keys_in_order uid=%s deal=%s -> %s",
                 uid, deal_id, current_keys_in_order)

    # 4) Проверка порядка/целостности
    pos_by_key: Dict[str, int] = {k: i for i, k in enumerate(current_keys_in_order)}
    cut_from_ix = 0 if not current_msgs else len(expected_keys)

    if current_msgs:
        last_pos = -1
        for i, k in enumerate(expected_keys):
            msg_stub = index.get(k)
            if not msg_stub or not isinstance(getattr(msg_stub, "message_id", None), int):
                cut_from_ix = min(cut_from_ix, i); break
            pos = pos_by_key.get(k)
            if pos is None:
                cut_from_ix = min(cut_from_ix, i); break
            if k.endswith("_alt") and pos != last_pos + 1:
                cut_from_ix = min(cut_from_ix, i - 1 if i > 0 else 0); break
            last_pos = pos
        else:
            cut_from_ix = len(expected_keys)

    # 5) Правки «на месте»
    if cut_from_ix >= len(expected_keys):
        logger.info("poll_details[refresh] order-ok uid=%s deal=%s → edit-in-place", uid, deal_id)

        async def _edit_text(key: str, text: Optional[str]) -> None:
            if not text:
                return
            m = index.get(key)
            if not m:
                return
            st = await _edit_text_status(bot, uid, m.message_id, text)
            logger.debug("poll_details[refresh] edit-text key=%s mid=%s status=%s", key, m.message_id, st)

        async def _edit_kb(key: str, kb: Optional[InlineKeyboardMarkup]) -> None:
            m = index.get(key)
            if not m:
                return
            st = await _edit_markup_status(bot, uid, m.message_id, kb)
            logger.debug("poll_details[refresh] edit-kb key=%s mid=%s status=%s", key, m.message_id, st)

        await _edit_text("header", header_text)
        await _edit_text("main", main_text)
        await _edit_text("assist", assist_text)
        await _edit_text("admin", admin_text)
        await _edit_text("trainee", trainee_text)

        await _edit_kb("main_alt", main_alt)
        await _edit_kb("assist_alt", assist_alt)
        await _edit_kb("admin_alt", admin_alt)
        await _edit_kb("mgr", mgr_kb)
        await _edit_kb("back", back_kb)
        return

    # 6) Хвостовая перезапись
    logger.warning(
        "poll_details[refresh] order-fix uid=%s deal=%s cut_from_ix=%s exp=%s cur=%s",
        uid, deal_id, cut_from_ix, expected_keys, current_keys_in_order
    )

    # 6.1 Удаляем хвост и чистим индекс
    for k in expected_keys[cut_from_ix:]:
        stub = index.get(k)
        if not stub:
            continue
        mid = getattr(stub, "message_id", None)
        if isinstance(mid, int):
            with contextlib.suppress(Exception):
                await bot.delete_message(uid, mid)
                logger.debug("poll_details[refresh] deleted key=%s mid=%s", k, mid)
        _update_detail_index(uid, deal_id, k, None)

    # 6.2 Живой префикс
    prefix_msgs: List[types.Message] = []
    if current_msgs:
        prefix_keys = set(expected_keys[:cut_from_ix])
        for m in current_msgs:
            mid = getattr(m, "message_id", None)
            if isinstance(mid, int) and mid_to_key.get(mid) in prefix_keys:
                prefix_msgs.append(m)

    # 6.3 Досылаем хвост по эталону
    fresh: List[types.Message] = []

    async def _send_text(key: str, text: Optional[str]) -> None:
        if text is None:
            return
        msg = await bot.send_message(uid, text, parse_mode="Markdown", disable_web_page_preview=True)
        _update_detail_index(uid, deal_id, key, getattr(msg, "message_id", None))
        fresh.append(msg)

    async def _send_caption(key: str, kb: Optional[InlineKeyboardMarkup], caption: str) -> None:
        if kb is None:
            return
        msg = await bot.send_message(uid, caption, reply_markup=kb)
        _update_detail_index(uid, deal_id, key, getattr(msg, "message_id", None))
        fresh.append(msg)

    for key in expected_keys[cut_from_ix:]:
        if key == "header":
            await _send_text("header", header_text)
        elif key == "main":
            await _send_text("main", main_text)
        elif key == "main_alt":
            await _send_caption("main_alt", main_alt, "🔁 Альтернатива:")
        elif key == "assist":
            await _send_text("assist", assist_text)
        elif key == "assist_alt":
            await _send_caption("assist_alt", assist_alt, "🔁 Альтернатива:")
        elif key == "admin":
            await _send_text("admin", admin_text)
        elif key == "admin_alt":
            await _send_caption("admin_alt", admin_alt, "🔁 Альтернатива:")
        elif key == "trainee":
            await _send_text("trainee", trainee_text)
        elif key == "mgr":
            await _send_caption("mgr", mgr_kb, "🛠 Управление:")
        elif key == "back":
            await _send_caption("back", back_kb, "\u2060")

    # 6.4 Обновляем реестр
    alive = prefix_msgs + fresh
    _set_block(uid, deal_id, alive)
    _detach_from_last_user_messages(uid, alive)

    logger.info(
        "poll_details[refresh] order-fixed uid=%s deal=%s kept=%s appended=%s",
        uid, deal_id, [getattr(m, "message_id", None) for m in prefix_msgs],
        [getattr(m, "message_id", None) for m in fresh],
    )
    if not alive:
        logger.error(
            "poll_details[refresh] FAILED-EMPTY uid=%s deal=%s — после перезаписи нет сообщений в блоке!"
        )

# ────────────────────────────────────────────────────────────────────
# [2.1.5] Вспомогательные функции детекции статусов правок
# ────────────────────────────────────────────────────────────────────
async def _edit_text_status(bot: Bot, uid: int, message_id: int, new_text: str) -> str:
    """
    Возвращает: 'ok' | 'not_modified' | 'not_found' | 'error'
    """
    try:
        await bot.edit_message_text(
            chat_id=uid, message_id=message_id,
            text=new_text, parse_mode="Markdown", disable_web_page_preview=True
        )
        return "ok"
    except TelegramBadRequest as e:
        msg = str(e).lower()
        if "message is not modified" in msg:
            return "not_modified"
        if "message to edit not found" in msg:
            return "not_found"
        return "error"
    except Exception:
        return "error"


async def _edit_markup_status(bot: Bot, uid: int, message_id: int, kb: Optional[InlineKeyboardMarkup]) -> str:
    try:
        await bot.edit_message_reply_markup(chat_id=uid, message_id=message_id, reply_markup=kb)
        return "ok"
    except TelegramBadRequest as e:
        msg = str(e).lower()
        if "message to edit not found" in msg:
            return "not_found"
        if "message is not modified" in msg:
            return "not_modified"
        return "error"
    except Exception:
        return "error"


async def _patch_role_block(
    bot: Bot,
    uid: int,
    index: Dict[str, types.Message],
    role_key: str,
    new_text: Optional[str],
    new_alt_kb: Optional[InlineKeyboardMarkup],
) -> str:
    """
    Правит блок роли (текст + клавиатуру 'Альтернатива') по ключу role_key ∈ {main, assist, admin}.
    Возвращает: 'ok' | 'not_found' | 'error'
    """
    status = "ok"
    if new_text:
        msg = index.get(role_key)
        if msg:
            st = await _edit_text_status(bot, uid, msg.message_id, new_text)
            if st == "not_found":
                status = "not_found"
    alt_msg_key = f"{role_key}_alt"
    alt_msg = index.get(alt_msg_key)
    if alt_msg:
        st = await _edit_markup_status(bot, uid, alt_msg.message_id, new_alt_kb)
        if st == "not_found":
            status = "not_found"
    return status


# ────────────────────────────────────────────────────────────────────
# [2.1.6] Построение данных/текстов деталей (SSOT-совместимо)
# ────────────────────────────────────────────────────────────────────
import contextlib
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Set

from aiogram import Bot, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Внешние зависимости (SSOT)
from core.utils import short_name as _short_name  # async
from core.state import state

# Константы и небольшие утилиты локально (без дубликатов SSOT)
_OK_STATUSES = {"green", "yellow"}  # кого можно ставить в core-ролях


def _parse_uid(val: Any) -> Optional[int]:
    """int | 'uid' | 'Имя Ф.|uid' → uid|None."""
    # Пытаемся использовать SSOT-парсер, если доступен
    try:
        from core.utils import parse_uid as _parse_uid_ssot  # type: ignore
        with contextlib.suppress(Exception):
            u = _parse_uid_ssot(val)
            return int(u) if u is not None else None
    except Exception:
        pass

    # Локальный фолбэк
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        s = val.strip()
        if "|" in s:
            s = s.rsplit("|", 1)[-1]
        with contextlib.suppress(Exception):
            return int(s)
    return None


async def _fmt(uid: int, _role: str = "") -> str:
    """
    Формат слота 'Имя Ф.<суффикс>|uid' (имя — строго через SSOT short_name).
    Суффиксы ролей (.1/.2/.Адм/.Стаж) — через core.utils.role_suffix.
    ВНИМАНИЕ: индекс для main/assist здесь символический (1/2), как и в остальном проекте.
    """
    human = (getattr(state, "user_short", {}) or {}).get(uid) or (await _short_name(uid))
    base = (human or f"user{uid}").strip()
    try:
        from core.utils import role_suffix as _role_suffix  # SSOT
        idx = 1 if _role == "main" else (2 if _role == "assist" else None)
        suf = _role_suffix(_role, idx)
    except Exception:
        suf = ""  # мягкий фолбэк: без суффикса
    return f"{base}{suf}|{uid}"


def _role_cfg(game_name: str) -> Dict[str, int]:
    """Минимальный адаптер; основная логика выбора ролей — в handlers/polls_lifecycle.py."""
    try:
        from core.config import settings
        from difflib import SequenceMatcher
        mapping = getattr(settings, "GAME_ROLE_MAPPING", {}) or {}
        norm = str(game_name or "").strip().lower()

        best_ratio = 0.0
        best_cfg: Optional[Dict[str, int]] = None
        for key, cfg in mapping.items():
            k_norm = str(key or "").strip().lower()
            # точные и частичные совпадения — приоритетно
            if norm == k_norm or (norm and (norm in k_norm or k_norm in norm)):
                return {
                    "main_leaders": int(cfg.get("main_leaders", 1)),
                    "assistants": int(cfg.get("assistants", 0)),
                }
            # иначе — фуззи
            ratio = SequenceMatcher(None, norm, k_norm).ratio()
            if ratio > best_ratio:
                best_ratio, best_cfg = ratio, cfg

        if best_cfg:
            return {
                "main_leaders": int(best_cfg.get("main_leaders", 1)),
                "assistants": int(best_cfg.get("assistants", 0)),
            }
    except Exception:
        pass
    return {"main_leaders": 1, "assistants": 0}


def _need_admin_by_package(pkg_raw: str) -> int:
    return 1 if (pkg_raw or "").strip().lower() in {"стандарт", "стандарт+", "премиум", "vip", "вип", "биглион"} else 0


async def _status_cached(uid: int, game_name: str) -> str:
    """
    Кеш светофора на уровне state: user_svetofor_cache[game_name][uid] = 'green|yellow|red|'
    Дополнительно используем локальный TTL-кэш (если объявлен выше в модуле).
    """
    g = (game_name or "").strip().lower()
    if not g:
        return ""

    # 0) локальный TTL-кэш (если доступен)
    try:
        _local_status_cache = globals().get("_local_status_cache", None)
        _ttl = int(globals().get("STATUS_CACHE_TTL", 60 * 60 * 4))
        if isinstance(_local_status_cache, dict):
            key = f"sv:{uid}:{g}"
            now = time.time()
            item = _local_status_cache.get(key)
            if isinstance(item, tuple) and len(item) == 2:
                status, ts = item
                if (now - float(ts)) < _ttl:
                    return (status or "").strip().lower()
    except Exception:
        pass

    # 1) state-кэш
    cache = getattr(state, "user_svetofor_cache", None)
    if not isinstance(cache, dict):
        cache = {}
        setattr(state, "user_svetofor_cache", cache)
    per_game = cache.setdefault(g, {})
    if uid in per_game:
        status = (per_game[uid] or "").strip().lower()
        # обновим локальный TTL-кэш
        try:
            if isinstance(_local_status_cache, dict):
                _local_status_cache[f"sv:{uid}:{g}"] = (status, time.time())
        except Exception:
            pass
        return status

    # 2) загрузка из services.gsheets
    try:
        from services.gsheets import get_user_status_from_svetofor
        res = get_user_status_from_svetofor(uid, game_name)
        status = await res if hasattr(res, "__await__") else res
        status = (status or "").strip().lower()
        if status not in {"green", "yellow", "red"}:
            status = ""
    except Exception:
        status = ""

    # запись в оба кэша
    per_game[uid] = status
    try:
        if isinstance(_local_status_cache, dict):
            _local_status_cache[f"sv:{uid}:{g}"] = (status, time.time())
    except Exception:
        pass
    return status


async def _ensure_single_role(dist: Dict[str, Any], need_main: int, need_assist: int) -> None:
    """
    Инвариант: 1 пользователь = 1 роль.
    Убираем дубли uid из lead*/assistant*/admin, оставляя первое вхождение.
    """
    seen: Set[int] = set()
    # lead
    for i in range(1, max(1, need_main) + 1):
        k = f"lead{i}"
        u = _parse_uid(dist.get(k))
        if u and u in seen:
            dist[k] = None
        elif u:
            seen.add(u)
    # assistant
    for i in range(1, max(0, need_assist) + 1):
        k = f"assistant{i}"
        u = _parse_uid(dist.get(k))
        if u and u in seen:
            dist[k] = None
        elif u:
            seen.add(u)
    # admin
    u = _parse_uid(dist.get("admin"))
    if u and u in seen:
        dist["admin"] = None


async def _normalize_tag_texts(dist: Dict[str, Any], need_main: int, need_assist: int) -> None:
    """
    Создаём отсутствующие ключи слотов под фактическую конфигурацию ролей
    И НОРМАЛИЗУЕМ тексты тегов к виду 'Имя Ф.<суффикс>|uid' для всех присутствующих uid.
    """
    # Создание ключей
    for i in range(1, max(1, need_main) + 1):
        dist.setdefault(f"lead{i}", None)
    for i in range(1, max(0, need_assist) + 1):
        dist.setdefault(f"assistant{i}", None)
    dist.setdefault("admin", dist.get("admin", None))
    # trainee показывается отдельно — не влияет на готовность

    # Нормализация содержимого (если в слоте есть uid)
    for i in range(1, max(1, need_main) + 1):
        k = f"lead{i}"
        u = _parse_uid(dist.get(k))
        if u:
            dist[k] = await _fmt(u, "main")
    for i in range(1, max(0, need_assist) + 1):
        k = f"assistant{i}"
        u = _parse_uid(dist.get(k))
        if u:
            dist[k] = await _fmt(u, "assist")
    u_admin = _parse_uid(dist.get("admin"))
    if u_admin:
        dist["admin"] = await _fmt(u_admin, "admin")
    u_tr = _parse_uid(dist.get("trainee"))
    if u_tr:
        dist["trainee"] = await _fmt(u_tr, "trainee")


# Текстовые блоки и последовательность
async def _detail_header_lines(deal: Dict[str, Any]) -> List[str]:
    """
    Строки заголовка.
    • Время берём как на кнопках: event_time (если задано) иначе из event_datetime;
    • Добавлены «🎁 Бонусы» из CRM (bonuses/bonus/extra_bonuses/extra_services).
    """
    base_title = (deal.get("game_name") or deal.get("name") or f"Сделка #{deal.get('id')}").strip()
    title = public_game_title(base_title)
    dt = deal.get("event_datetime")

    # дата
    if isinstance(dt, datetime):
        date_s = dt.strftime("%d.%m")
    else:
        date_s = str(deal.get("event_date") or "—")

    # время: сначала берём поле event_time, затем — из dt
    et_raw = str(deal.get("event_time") or "").strip()
    if et_raw:
        time_s = et_raw
    elif isinstance(dt, datetime):
        time_s = dt.strftime("%H:%M")
    else:
        time_s = str(deal.get("event_time") or "—")

    pkg = (deal.get("package") or "—").strip()
    ppl = str(deal.get("players") or deal.get("guests") or "").strip()
    bonuses = deal.get("bonuses", deal.get("bonus", deal.get("extra_bonuses", deal.get("extra_services"))))
    bonuses_s = str(bonuses or "").strip()

    head = [
        f"🎮 *{title}*",
        f"🗓 {date_s} {time_s} • 📦 {pkg}"
        + (f" • 👥 {ppl}" if ppl else "")
        + (f" • 🎁 {bonuses_s}" if bonuses_s else ""),
    ]
    return head


def _index_current_blocks(msgs: List[types.Message]) -> Dict[str, types.Message]:
    """
    Разметка текущих сообщений по типам: header/main/main_alt/assist/assist_alt/admin/admin_alt/trainee/mgr/back.
    Опирается на уникальные заголовки блоков.
    """
    out: Dict[str, types.Message] = {}
    for i, m in enumerate(msgs):
        t = (m.text or "")
        tu = t.upper()
        if tu.startswith("🎮 *") or tu.startswith("🧩 ") or "• 📦" in t:
            out.setdefault("header", m)
        elif tu.startswith("─────") and "ВЕДУЩ" in tu:
            out["main"] = m
            if i + 1 < len(msgs) and (msgs[i + 1].text or "").startswith("🔁"):
                out["main_alt"] = msgs[i + 1]
        elif tu.startswith("─────") and "ПОМОЩНИК" in tu:
            out["assist"] = m
            if i + 1 < len(msgs) and (msgs[i + 1].text or "").startswith("🔁"):
                out["assist_alt"] = msgs[i + 1]
        elif tu.startswith("─────") and "АДМИНИСТРАТОР" in tu:
            out["admin"] = m
            if i + 1 < len(msgs) and (msgs[i + 1].text or "").startswith("🔁"):
                out["admin_alt"] = msgs[i + 1]
        elif tu.startswith("─────") and "СТАЖЁР" in tu:
            out["trainee"] = m
        elif t == "🛠 Управление:":
            out["mgr"] = m
        elif t == "\u2060":  # zero-width spacer — нужен пылесосу/кнопке «Назад»
            out["back"] = m
    return out


# SSOT-shim: получить откликнувшихся по сделке из state с нормализацией
async def _get_respondents(deal_id: int) -> Dict[int, Dict[str, Any]]:
    """
    Возвращает uid → { 'status': str, 'is_admin_eligible': bool }.
    Источники (по приоритету):
      1) state.poll_respondents / state.respondents_cache
      2) fallback: сборка из state.responses (ответы на опрос)
    """
    def _coerce_map(raw: Any) -> Dict[int, Dict[str, Any]]:
        out: Dict[int, Dict[str, Any]] = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                uid: Optional[int] = None
                with contextlib.suppress(Exception):
                    uid = int(k)
                if uid is None and isinstance(v, dict) and "uid" in v:
                    with contextlib.suppress(Exception):
                        uid = int(v["uid"])
                if not uid:
                    continue
                if isinstance(v, dict):
                    st = str(v.get("status") or v.get("svetofor") or v.get("color") or "").strip().lower()
                    admin_ok = bool(v.get("is_admin_eligible") or v.get("can_admin") or v.get("admin"))
                    out[uid] = {"status": st, "is_admin_eligible": admin_ok}
                else:
                    st = str(v or "").strip().lower()
                    out[uid] = {"status": st, "is_admin_eligible": False}
        return out

    # 1) старые источники
    src = (getattr(state, "poll_respondents", {}) or {})
    raw = src.get(str(deal_id)) if isinstance(src, dict) else None
    if not raw and isinstance(src, dict):
        raw = src.get(deal_id)

    if not raw:
        cache = (getattr(state, "respondents_cache", {}) or {})
        raw = cache.get(str(deal_id)) if isinstance(cache, dict) else None
        if not raw and isinstance(cache, dict):
            raw = cache.get(deal_id)

    mapped = _coerce_map(raw)
    if mapped:
        return mapped

    # 2) fallback: собрать из state.responses
    resp_out: Dict[int, Dict[str, Any]] = {}
    try:
        for pdata in (getattr(state, "responses", {}) or {}).values():
            deals_map = (pdata.get("deals") or {})
            for u in (deals_map.get(int(deal_id), []) or []):
                uid = int(u.get("user_id", 0) or 0)
                if not uid:
                    continue
                resp_out.setdefault(uid, {"status": "", "is_admin_eligible": False})
            for adm in (pdata.get("admin_available") or []):
                uid = int(adm.get("user_id", 0) or 0)
                if not uid:
                    continue
                info = resp_out.setdefault(uid, {"status": "", "is_admin_eligible": False})
                info["is_admin_eligible"] = True
    except Exception:
        resp_out = {}

    # кэшируем
    if resp_out:
        with contextlib.suppress(Exception):
            cache = getattr(state, "respondents_cache", None)
            if not isinstance(cache, dict):
                cache = {}
                setattr(state, "respondents_cache", cache)
            cache[str(int(deal_id))] = {
                uid: {
                    "status": v.get("status", ""),
                    "is_admin_eligible": bool(v.get("is_admin_eligible")),
                }
                for uid, v in resp_out.items()
            }

    return resp_out


async def _build_blocks(*, bot: Bot, uid: int, deal_id: int, force_approved: bool) -> Dict[str, Any]:
    """
    Строит все тексты/клавиатуры для деталей без отправки.
    Возвращает dict с ключами:
      header_text, main_text, main_alt, assist_text, assist_alt, admin_text, admin_alt,
      trainee_text, mgr_kb, back_kb, sequence=[(text, kb), ...].

    ВАЖНО для «тихой замены»:
    • выбранные роли берём строго из dist (видны ручные назначения);
    • альтернативы считаем по откликнувшимся и «светофору»;
    • формат слотов поддерживает SSOT-суффиксы — стабильные теги для последующих стадий.
    """
    deal = next((d for d in (getattr(state, "current_poll_deals", []) or [])
                 if int(d.get("id", 0)) == int(deal_id)), None)
    if not deal:
        return {"sequence": [("⚠️ Игра не найдена.", None)]}

    g_name = (deal.get("game_name") or deal.get("name") or "").strip()
    cfg = _role_cfg(g_name)
    need_main = int(cfg.get("main_leaders", 1))
    need_assist = int(cfg.get("assistants", 0))
    need_admin = _need_admin_by_package(str(deal.get("package") or ""))

    respondents = await _get_respondents(deal_id)  # uid → {status, is_admin_eligible}
    dist: Dict[str, Any] = (getattr(state, "distribution_cache", {}) or {}).setdefault(str(deal_id), {})
    await _normalize_tag_texts(dist, need_main, need_assist)
    await _ensure_single_role(dist, need_main, need_assist)

    async def _compose_role(role: str, title: str, icon: str, need: int) -> Tuple[Optional[str], Optional[InlineKeyboardMarkup]]:
        """
        Печать выбранных и альтернатив.
        ВАЖНО: выбранные (назначенные) берём ТОЛЬКО из dist — без фильтра по respondents,
        чтобы не скрывать вручную назначенных, кто не отвечал в опросе.
        Альтернативы формируем по respondents с фильтрацией «светофора».
        """
        if need <= 0:
            if role == "admin":
                dist.pop("admin", None)
            else:
                pref = "lead" if role == "main" else "assistant"
                for i in range(1, 5):
                    dist.pop(f"{pref}{i}", None)
            return None, None

        prefix = "lead" if role == "main" else ("assistant" if role == "assist" else "admin")
        chosen: List[Tuple[int, str]] = []
        if role == "admin":
            u = _parse_uid(dist.get("admin"))
            if u:
                chosen.append((u, "🛡️"))
        else:
            for i in range(1, need + 1):
                u = _parse_uid(dist.get(f"{prefix}{i}"))
                if u:
                    chosen.append((u, ""))

        # пометки по «светофору» для отображения
        if role != "admin":
            tmp: List[Tuple[int, str]] = []
            for u, _m in chosen:
                st = await _status_cached(u, g_name)
                mark = "🟢" if st == "green" else ("🟡" if st == "yellow" else "")
                tmp.append((u, mark))
            chosen = tmp

        ready = len([u for u, _ in chosen]) >= need
        lines = [f"─────{icon} *{title.upper()}* ────", f"{'✅' if ready else '❌'} {min(len(chosen), need)}/{need}"]
        for u, mark in chosen[:max(need, 0)]:
            human = (getattr(state, "user_short", {}) or {}).get(u) or (await _short_name(u))
            lines.append(f"– {human} {mark}")
        text = "\n".join(lines)

        # альтернативы — только из откликнувшихся, не занятых в выбранных
        chosen_uids = {u for u, _ in chosen}

        async def _fits_for_block(user_id: int) -> bool:
            if role == "admin":
                return bool(respondents.get(user_id, {}).get("is_admin_eligible"))
            st = await _status_cached(user_id, g_name)
            if role == "main":
                return st == "green"
            if role == "assist":
                return st in _OK_STATUSES
            return False

        kb = None
        alts = [u for u in respondents.keys() if (u not in chosen_uids) and (await _fits_for_block(u))]
        if alts:
            kbld = InlineKeyboardBuilder()
            for u in alts:
                if role == "admin":
                    mark = "🛡️"
                else:
                    st = await _status_cached(u, g_name)
                    mark = "🟢" if st == "green" else ("🟡" if st == "yellow" else "")
                human = (getattr(state, "user_short", {}) or {}).get(u) or (await _short_name(u))
                kbld.button(text=f"{human} {mark}", callback_data=f"swap_{deal_id}_{role}_{u}")
            kbld.adjust(1)
            kb = kbld.as_markup()

        return text, kb

    # Секции ролей
    main_text, main_alt = await _compose_role("main", "Ведущие", "🎤", need_main)
    assist_text, assist_alt = await _compose_role("assist", "Помощники", "🧑‍🤝‍🧑", need_assist)
    admin_text, admin_alt = (None, None)
    if need_admin:
        admin_text, admin_alt = await _compose_role("admin", "Администратор", "🛡️", 1)
    else:
        dist.pop("admin", None)

    # Стажёр (всегда отображаем; не влияет на индикатор комплектности)
    trainee_text: Optional[str] = None
    trainee_uid: Optional[int] = _parse_uid(dist.get("trainee"))
    occupied: Set[int] = {
        *[(_parse_uid(dist.get(f"lead{i}")) or -1) for i in range(1, need_main + 1)],
        *[(_parse_uid(dist.get(f"assistant{i}")) or -1) for i in range(1, need_assist + 1)],
        (_parse_uid(dist.get("admin")) or -1),
    }
    reds: List[int] = []
    for u in respondents.keys():
        if u in occupied:
            continue
        st = await _status_cached(u, g_name)
        if st == "red":
            reds.append(u)
    if reds:
        if trainee_uid not in reds:
            trainee_uid = reds[0]
            dist["trainee"] = await _fmt(trainee_uid, "trainee")
        human = (getattr(state, "user_short", {}) or {}).get(trainee_uid) or (await _short_name(int(trainee_uid)))  # type: ignore[arg-type]
        trainee_text = "\n".join(["───── 👷 *СТАЖЁР* ─────", f"– {human} 🔴", "_Стажёр не влияет на индикатор набора._"])
    else:
        dist.pop("trainee", None)
        trainee_text = "───── 👷 *СТАЖЁР* ─────\n_Пока никого._\n_Стажёр не влияет на индикатор набора._"

    # Кнопки управления
    is_locked = (deal_id in (getattr(state, "locked_distribution", {}) or {})) or (str(deal_id) in (getattr(state, "locked_distribution", {}) or {}))
    is_force_closed = deal_id in (getattr(state, "deal_force_closed", set()) or set())
    mgr_kb = None
    if (getattr(state, "current_poll_leader", None) == uid) and not is_force_closed:
        kb_mgr = InlineKeyboardBuilder()
        if is_locked or force_approved:
            kb_mgr.button(text="✅ Утверждено", callback_data="noop")
        else:
            kb_mgr.button(text=f"✅ Утвердить игру", callback_data=f"poll_approve_{deal_id}")
        kb_mgr.button(text="⏹️ Стоп набор", callback_data=f"poll_stop_{deal_id}")
        kb_mgr.adjust(1)
        mgr_kb = kb_mgr.as_markup()

    # Назад
    back_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к списку", callback_data=POLL_BACK)]]
    )

      # Последовательность для первого показа (alt — всегда рядом с ролью)
    header_text = "\n".join(await _detail_header_lines(deal))
    seq: List[Tuple[Optional[str], Optional[InlineKeyboardMarkup]]] = []
    seq.append((header_text, None))
    if main_text:
        seq.append((main_text, None))
        seq.append(("🔁 Альтернатива:", main_alt))      # ← безусловно
    if assist_text:
        seq.append((assist_text, None))
        seq.append(("🔁 Альтернатива:", assist_alt))    # ← безусловно
    if admin_text:
        seq.append((admin_text, None))
        seq.append(("🔁 Альтернатива:", admin_alt))     # ← безусловно
    if trainee_text:
        seq.append((trainee_text, None))
    if mgr_kb:
        seq.append(("🛠 Управление:", mgr_kb))
    # «невидимая» заглушка для корректного удаления хвоста
    seq.append(("\u2060", back_kb))


    return {
        "header_text": header_text,
        "main_text": main_text,
        "main_alt": main_alt,
        "assist_text": assist_text,
        "assist_alt": assist_alt,
        "admin_text": admin_text,
        "admin_alt": admin_alt,
        "trainee_text": trainee_text,
        "mgr_kb": mgr_kb,
        "back_kb": back_kb,
        "sequence": seq,
    }

# История изменений [2.1.6]:
# • 2025-08-25 — выровнено под SSOT: _fmt с role_suffix, стабильная «тихая замена» (chosen из dist, alts из respondents),
#                 стажёр всегда виден, совместимость с int/str ключами locked_distribution.



# ────────────────────────────────────────────────────────────────────
# [2.1.7] Legacy-совместимость (старые вызовы без bot=)
# ────────────────────────────────────────────────────────────────────
async def refresh_deal_details_legacy(uid: int, deal_id: int, *_, **__) -> None:
    """Совместимость со старым вызовом refresh_deal_details(uid, deal_id)."""
    bot = Bot.get_current()
    await refresh_deal_details(bot=bot, uid=uid, deal_id=deal_id, force_approved=False)


async def render_detail_legacy(uid: int, deal_id: int, *_, **__) -> None:
    """Совместимость со старым вызовом render_detail(uid, deal_id)."""
    bot = Bot.get_current()
    await render_detail(bot=bot, uid=uid, deal_id=deal_id, force_approved=False)


# История изменений [2.1]:
# • 2025-08-25 — выровнено под SSOT: detail_blocks храним как List[int]; поддержан старый формат при чтении;
#                render_detail после первичного рендера конвертирует реестр и отвязывает от last_user_messages;
#                refresh_deal_details — патч по индексам, точечные пересоздания, без «массового» delete;
#                разбито на подпункты 2.1.1–2.1.7; фиксы Pylance/тайпинги; «тихая замена» стабилизирована.

# ███ [2.6] SWAP (альтернативы) — ручное назначение кандидата на роль (с УМНОЙ РОКИРОВКОЙ)
# --------------------------------------------------------------------
@router.callback_query(lambda c: c.data and c.data.startswith("swap_"))
async def poll_swap_handler(callback: types.CallbackQuery) -> None:
    """
    Переназначение из альтернатив:
    • одноразовый ответ на callback (alert на отказ, короткое уведомление на успех);
    • все прежние проверки/инварианты сохранены.
    """
    # ── 0) Разбор callback-data
    try:
        _, deal_s, role_target, uid_s = (callback.data or "").split("_", 3)
        deal_id = int(deal_s)
        uid_alt = int(uid_s)
        role_target = (role_target or "").strip().lower()  # main|assist|admin|trainee
    except Exception:
        # сразу объясняем и выходим
        with contextlib.suppress(Exception):
            await callback.answer("Некорректные данные для замены.", show_alert=True)
        return

    bot = Bot.get_current()
    answered = False  # следим, чтобы ответить РОВНО один раз

    async def _fail(msg: str, code: str) -> None:
        nonlocal answered
        logger.info("[swap] %s: %s", code, msg)
        if not answered:
            with contextlib.suppress(Exception):
                await callback.answer(msg, show_alert=True)
            answered = True
        # безопасный «тихий» refresh уже после ответа пользователю
        with contextlib.suppress(Exception):
            await refresh_deal_details(bot=bot, uid=callback.from_user.id, deal_id=deal_id, force_approved=False)

    # ── 1) Защита от правок зафиксированного состава
    locked = (getattr(state, "locked_distribution", {}) or {})
    if str(deal_id) in locked or deal_id in locked:
        return await _fail("🔒 Состав уже утверждён — правки недоступны.", "locked")

    # ── 2) Сделка и конфиг ролей
    deal = next((d for d in (state.current_poll_deals or []) if int(d.get("id") or 0) == deal_id), None)
    if not deal:
        return await _fail("Игра не найдена или уже закрыта.", "deal_not_found")

    game_name = str(deal.get("game_name") or deal.get("name") or "Игра")
    cfg = _role_cfg(game_name)
    need_main = int(cfg.get("main_leaders", 1))
    need_assist = int(cfg.get("assistants", 0))
    if role_target == "assist" and need_assist <= 0:
        return await _fail("⛔ Для этой игры помощники не требуются.", "assist_not_required")

    # ── 3) Копия дистрибуции и утилиты (как было)
    dist_all = getattr(state, "distribution_cache", {}) or {}
    dist: Dict[str, Any] = dict(dist_all.get(str(deal_id)) or {})

    def uid_of(val: Any) -> Optional[int]:
        try:
            u = _parse_uid(val)
            return int(u) if u is not None else None
        except Exception:
            return None

    def _find_user_slot(uid: int) -> Tuple[Optional[str], Optional[str]]:
        for k in ("admin", "trainee"):
            if uid_of(dist.get(k)) == uid:
                return ("admin" if k == "admin" else "trainee"), k
        for i in range(1, max(need_main, 1) + 1):
            k = f"lead{i}"
            if uid_of(dist.get(k)) == uid:
                return "main", k
        for i in range(1, max(need_assist, 0) + 1):
            k = f"assistant{i}"
            if uid_of(dist.get(k)) == uid:
                return "assist", k
        return None, None

    def _first_free(prefix: str, max_needed: int) -> Optional[str]:
        if max_needed <= 0:
            return None
        for i in range(1, max_needed + 1):
            if not str(dist.get(f"{prefix}{i}") or "").strip():
                return f"{prefix}{i}"
        return None

    def _existing_last(prefix: str, max_needed: int) -> Optional[str]:
        for i in range(max_needed, 0, -1):
            k = f"{prefix}{i}"
            if uid_of(dist.get(k)) is not None:
                return k
        return None

    respondents = await _get_respondents(deal_id)

    def _status_emoji(st: str) -> str:
        st = (st or "").lower()
        return "🟢" if st == "green" else ("🟡" if st == "yellow" else ("🔴" if st == "red" else "⬜️"))

    async def _fits(uid: int, role: str) -> bool:
        if role == "admin":
            return bool(respondents.get(uid, {}).get("is_admin_eligible"))
        st = await _status_cached(uid, game_name)
        if role == "main":
            return st == "green"
        if role == "assist":
            return st in _OK_STATUSES
        if role == "trainee":
            return st == "red"
        return False

    # ── 4) Положение выбранного
    role_old, slot_old = _find_user_slot(uid_alt)
    if role_old == role_target:
        # не раздражаем алёртом, просто тихо ответим
        if not answered:
            with contextlib.suppress(Exception):
                await callback.answer("Уже назначен в этой роли.", show_alert=False)
            answered = True
        return

    # ── 5) Проверка соответствия целевой роли
    if not await _fits(uid_alt, role_target):
        st = await _status_cached(uid_alt, game_name)
        note = "не отмечен как админ" if role_target == "admin" else f"статус { _status_emoji(st) or 'неизвестен' }"
        return await _fail(f"⛔ Кандидат не подходит для роли: {note}.", "not_qualified")

    # ── 6) Целевой слот и вытесняемый
    if role_target == "admin":
        slot_target = "admin"
        displaced_uid = uid_of(dist.get("admin"))
    elif role_target == "main":
        free = _first_free("lead", need_main)
        if free:
            slot_target = free
            displaced_uid = None
        else:
            slot_target = _existing_last("lead", need_main) or "lead1"
            displaced_uid = uid_of(dist.get(slot_target))
    elif role_target == "assist":
        free = _first_free("assistant", need_assist)
        if free:
            slot_target = free
            displaced_uid = None
        else:
            slot_target = _existing_last("assistant", need_assist) or "assistant1"
            displaced_uid = uid_of(dist.get(slot_target))
    else:
        return await _fail("Некорректная целевая роль.", "bad_role")

    fallback_slot_for_displaced: Optional[str] = None

    # ── 7) Нужна замена в прежней роли (умная рокировка)
    sticky_roles = {"main", "assist", "admin"}
    replacement_uid: Optional[int] = None

    async def _find_replacement(for_role: str, *, exclude: set[int]) -> Optional[int]:
        assigned: set[int] = {u for u in (uid_of(v) for v in dist.values()) if u}
        for u in respondents.keys():
            if u in exclude or u in assigned:
                continue
            if await _fits(u, for_role):
                return u
        return None

    if role_old in sticky_roles and role_old != role_target:
        if displaced_uid and await _fits(displaced_uid, role_old):
            replacement_uid = displaced_uid
        else:
            replacement_uid = await _find_replacement(role_old, exclude={x for x in [uid_alt, displaced_uid] if x})
        if not replacement_uid:
            human = (getattr(state, "user_short", {}) or {}).get(uid_alt) or (await _short_name(uid_alt))
            return await _fail(f"⚠️ Нельзя снять {human}: нет подходящей замены.", "no_replacement_prev_role")

    # ── 8) Если целевой слот занят — проверим, куда посадить вытеснённого
    if displaced_uid and replacement_uid != displaced_uid:
        if role_target == "main":
            free_for_displaced = _first_free("lead", need_main)
        elif role_target == "assist":
            free_for_displaced = _first_free("assistant", need_assist)
        elif role_target == "admin":
            free_for_displaced = _first_free("assistant", need_assist) if await _fits(displaced_uid, "assist") else None
        else:
            free_for_displaced = None

        if free_for_displaced:
            fallback_slot_for_displaced = free_for_displaced
        else:
            who = (getattr(state, "user_short", {}) or {}).get(displaced_uid) or (await _short_name(displaced_uid))
            logger.info(f"[swap] releasing displaced uid={displaced_uid} ({who}): no fallback slot for role={role_target}")
            displaced_uid = None
            fallback_slot_for_displaced = None

    # ── 9) Применение изменений
    for k, v in list(dist.items()):
        if isinstance(k, str) and (k.startswith("lead") or k.startswith("assistant") or k in {"admin", "trainee"}):
            if uid_of(v) == uid_alt:
                dist[k] = ""
    dist[slot_target] = await _fmt(uid_alt, role_target)
    if replacement_uid and role_old and slot_old:
        dist[slot_old] = await _fmt(replacement_uid, role_old)
    if displaced_uid and replacement_uid != displaced_uid:
        if role_target == "main":
            free = fallback_slot_for_displaced or _first_free("lead", need_main)
            if free:
                dist[free] = await _fmt(displaced_uid, "main")
        elif role_target == "assist":
            free = fallback_slot_for_displaced or _first_free("assistant", need_assist)
            if free:
                dist[free] = await _fmt(displaced_uid, "assist")
        elif role_target == "admin" and await _fits(displaced_uid, "assist"):
            free = fallback_slot_for_displaced or _first_free("assistant", need_assist)
            if free:
                dist[free] = await _fmt(displaced_uid, "assist")

    # ── 10) Инварианты, сохранение, единичный ответ и только потом refresh
    await _ensure_single_role(dist, need_main, need_assist)
    await _normalize_tag_texts(dist, need_main, need_assist)
    state.distribution_cache[str(deal_id)] = dist

    # ответ пользователю — до долгих операций
    with contextlib.suppress(Exception):
        if not answered:
            await callback.answer("✅ Переназначено", show_alert=False)
            answered = True

    await refresh_deal_details(bot=bot, deal_id=deal_id, uid=callback.from_user.id)

# История изменений [2.6]:
# • 2025-09-04 — исправлено отображение причин отказа: убран предварительный ACK; теперь ровно один callback.answer.


# ███ [2.7] NAVIGATION: «Назад к списку» — vacuum как в дашборде + очистка реестров
# --------------------------------------------------------------------

import contextlib
import logging
from typing import Any, Dict, List, Tuple, Optional

from aiogram import types, Bot
from aiogram.exceptions import TelegramBadRequest

from core.state import state

logger = logging.getLogger(__name__)


def _reg_key(uid: int, deal_id: int) -> Tuple[int, int]:
    return (int(uid), int(deal_id))


def _collect_keep_ids(uid: int) -> List[int]:
    """Собираем те же keep-id, что при входе в дашборд: главное меню + «Мои игры»."""
    keep: List[int] = []

    # главное меню
    try:
        from core.menu import get_menu_message_id  # lazy import
    except Exception:  # pragma: no cover
        get_menu_message_id = lambda _uid: None  # type: ignore[assignment]
    with contextlib.suppress(Exception):
        mid = get_menu_message_id(uid)
        if isinstance(mid, int):
            keep.append(mid)

    # «Мои игры» (если проект хранит их id в state.games_by_user)
    try:
        games_bucket: Any = getattr(state, "games_by_user", {})
        if isinstance(games_bucket, dict) and uid in games_bucket:
            val = games_bucket.get(uid)
            if isinstance(val, list):
                for m in val:
                    with contextlib.suppress(Exception):
                        keep.append(int(m))
            elif isinstance(val, int):
                keep.append(int(val))
    except Exception:
        pass

    return keep


# ────────────────────────────────────────────────────────────────────
# ПУБЛИЧНЫЙ API: забыть все детали у пользователя (без общего vacuum)
# ────────────────────────────────────────────────────────────────────
async def forget_all_details_for_user(uid: int, bot: Optional[Bot] = None) -> None:
    """
    Публичный API для модулей извне (например, handlers.my_games):
      • удаляет ВСЕ сообщения detail-view у пользователя,
      • очищает наши реестры state.detail_blocks/state.detail_index по этому uid,
      • вычищает соответствующие id из state.last_user_messages[uid],
      • НЕ запускает общий пылесос (меню/дашборды не трогает).

    Совместимость:
      forget_all_details_for_user(uid, bot=Bot) — предпочитаемый вызов;
      forget_all_details_for_user(uid)          — bot берётся через Bot.get_current().
    """
    _bot = bot or Bot.get_current()

    # 1) собрать и удалить все detail-сообщения текущего пользователя
    to_delete: List[int] = []

    try:
        from core.menu import get_menu_message_id  # lazy import
    except Exception:
        get_menu_message_id = lambda _uid: None  # type: ignore

    try:
        db: Dict[Tuple[int, int], List[int]] = getattr(state, "detail_blocks", {}) or {}
        keys_to_pop: List[Tuple[int, int]] = []
        for (k_uid, _deal), mids in list(db.items()):
            if int(k_uid) != int(uid):
                continue
            keys_to_pop.append((k_uid, _deal))
            if isinstance(mids, list):
                for m in mids:
                    with contextlib.suppress(Exception):
                        to_delete.append(int(m))
            elif isinstance(mids, int):
                to_delete.append(int(m))
        # удаляем сообщения «деталей»
        menu_mid = get_menu_message_id(uid)
        if isinstance(menu_mid, int):
            to_delete = [m for m in to_delete if m != menu_mid]
        for mid in to_delete:
            with contextlib.suppress(TelegramBadRequest, Exception):
                await _bot.delete_message(chat_id=int(uid), message_id=int(mid))
        # чистим detail_blocks по ключам пользователя
        for k in keys_to_pop:
            getattr(state, "detail_blocks", {}).pop(k, None)
    except Exception:
        # мягкая деградация: продолжаем чистить индексы/last_user_messages
        pass

    # 2) чистим индексы деталей для uid
    try:
        idx: Dict[Tuple[int, int], Dict[str, int]] = getattr(state, "detail_index", {}) or {}
        for (k_uid, _deal) in list(idx.keys()):
            if int(k_uid) == int(uid):
                idx.pop((k_uid, _deal), None)
    except Exception:
        pass

    # 3) вычистить id деталей из last_user_messages[uid]
    try:
        lst = getattr(state, "last_user_messages", None)
        if isinstance(lst, dict):
            cur = lst.get(int(uid))
            if isinstance(cur, list) and cur:
                to_delete_set = set(to_delete)
                filtered: List[Any] = []
                for item in cur:
                    mid: Optional[int] = None
                    mobj = getattr(item, "message_id", None)
                    if isinstance(mobj, int):
                        mid = int(mobj)
                    else:
                        with contextlib.suppress(Exception):
                            mid = int(item)  # иногда id хранятся как int
                    if isinstance(mid, int) and mid in to_delete_set:
                        continue
                    filtered.append(item)
                lst[int(uid)] = filtered
    except Exception:
        pass

    logger.debug("[details:forget] uid=%s deleted=%s", uid, to_delete)


async def _vacuum_details_like_dashboard(uid: int) -> None:
    """
    Делает РОВНО тот же пылесос, что и при переходе в дашборд:
    • сначала «забывает» детали (через публичный API выше),
    • затем вызывает вакуум с keep (меню/«Мои игры»), ignore_sticky=True при наличии поддержки.
    """
    # 1) удалить детали и очистить реестры
    await forget_all_details_for_user(int(uid))

    # 2) общий «дашбордный» пылесос
    keep = _collect_keep_ids(int(uid))
    # Используем безопасный вызов: если есть современный vacuum_private — применим его с ignore_sticky,
    # иначеfallback на локальный _vacuum (объявлен выше в модуле).
    try:
        _vp = globals().get("_vacuum_private", None)
        if _vp:
            with contextlib.suppress(TypeError):
                await _vp(int(uid), keep=keep, ignore_sticky=True)  # новый API
                logger.debug("[details:back] dashboard-like vacuum (new) keep=%s uid=%s", keep, uid)
                return
            with contextlib.suppress(Exception):
                await _vp(int(uid), keep=keep)  # старый API без ignore_sticky
                logger.debug("[details:back] dashboard-like vacuum (old) keep=%s uid=%s", keep, uid)
                return
    except Exception:
        pass

    # фолбэк: локальная обёртка _vacuum без ignore_sticky
    try:
        await globals()["__dict__"]["_vacuum"](int(uid), keep=keep)  # type: ignore[func-returns-value]
        logger.debug("[details:back] dashboard-like vacuum (fallback) keep=%s uid=%s", keep, uid)
    except Exception:
        logger.debug("[details:back] vacuum fallback failed uid=%s", uid)


# поддерживаем твой текущий callback-ключ «Назад»
@router.callback_query(lambda c: (c.data or "") == POLL_BACK)  # type: ignore[name-defined]
async def _on_back_to_list(callback: types.CallbackQuery) -> None:
    uid = callback.from_user.id
    with contextlib.suppress(Exception):
        await callback.answer()

    # 1) запускаем ровно тот же пылесос, что при входе в дашборд
    await _vacuum_details_like_dashboard(uid)

    # 2) показываем единый отчёт (он отредактируется «на месте», без дублей)
    try:
        from handlers.polls_lifecycle import _send_leader_report  # lazy import
        await _send_leader_report(int(uid))
    except Exception:
        logger.debug("[details:back] report not available for uid=%s", uid)

# История изменений [2.7]:
# • 2025-08-28 — «Назад» вызывает тот же SSOT-вакуум, что и кнопки дашборда; дополнительно чистим detail_* реестры.
# • 2025-08-31 — добавлен публичный API forget_all_details_for_user(uid, bot=None);
#                _vacuum_details_like_dashboard теперь использует этот API перед общим vacuum.
# • 2025-09-05 — FIX: убран прямой import vacuum_private и добавлен безопасный вызов с поддержкой старого/нового API
#                и фолбэком на локальную обёртку _vacuum — чтобы не падать там, где vacuum_private отсутствует.




# ███ [3.a] HANDLER: «Утвердить» (LEGACY GUARD — делегирование в polls_distribution)
# --------------------------------------------------------------------
@router.callback_query(lambda c: c.data and c.data.startswith("poll_approve_"))
async def poll_approve_game_handler(callback: CallbackQuery) -> None:
    """
    Защита от двойной обработки «Утвердить».
    По умолчанию владельцем обработчика является handlers.polls_distribution.
    Если в settings не указано APPROVE_HANDLER_OWNER="poll_details",
    здесь аккуратно выходим; основная логика выполнится в polls_distribution.
    При включении флага — делегируем выполнение в polls_distribution, чтобы не дублировать код.
    """
    owner = getattr(settings, "APPROVE_HANDLER_OWNER", "polls_distribution")
    if owner != "poll_details":
        with contextlib.suppress(Exception):
            await callback.answer()
        logger.info("[poll_details] approve skipped (owner=%s)", owner)
        return

    try:
        from handlers.polls_distribution import poll_approve_game_handler as _delegate
        await _delegate(callback)
    except Exception as e:
        logger.exception("[poll_details] approve delegate failed: %s", e)
        with contextlib.suppress(Exception):
            await callback.answer("Ошибка обработки утверждения.", show_alert=True)


# ███ [99] SELF-TESTS
# --------------------------------------------------------------------
async def _test_invariant() -> None:
    """Локальный тест переносов и инварианты «1 uid → 1 роль»."""
    did = 101
    state.current_poll_deals = [{"id": did, "game_name": "Время приключений", "package": "стандарт"}]
    state.distribution_cache = {str(did): {"lead1": "Иван И.|111", "admin": "Иван И.|111", "assistant1": "Пётр П.|222"}}

    cfg = _role_cfg("Время приключений")
    need_main = int(cfg.get("main_leaders", 1))
    need_assist = int(cfg.get("assistants", 2))

    await _ensure_single_role(state.distribution_cache[str(did)], need_main, need_assist)
    dist = state.distribution_cache[str(did)]
    all_uids = [
        _parse_uid(dist.get(k))
        for k in (["lead1", "lead2"] + ["assistant1", "assistant2"] + ["admin", "trainee"])
    ]
    assert all_uids.count(111) <= 1, "Дубли не устранены"

    # имитация swap → перенос 111 в assist
    fake_cb = types.CallbackQuery(
        id="x",
        from_user=types.User(id=999, is_bot=False, first_name=""),
        chat_instance="",
        message=types.Message(message_id=0, date=datetime.now(), chat=types.Chat(id=999, type="private")),
        data=f"swap_{did}_assist_111",
    )
    await poll_swap_handler(fake_cb)  # type: ignore

    dist = state.distribution_cache[str(did)]
    all_uids_after = [
        _parse_uid(dist.get(k))
        for k in (["lead1", "lead2"] + ["assistant1", "assistant2"] + ["admin", "trainee"])
    ]
    assert all_uids_after.count(111) == 1, "Инварианта нарушена после swap"
    print("handlers.poll_details — invariant tests passed")


async def _test_fmt_status() -> None:
    """Локальный тест форматирования и кэша статуса."""
    print(await _fmt(1, "main"))
    print(await _status_cached(1, "Цветочная башня"))


async def _test():
    await _test_invariant()
    await _test_fmt_status()


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(_test())

# История изменений:
# • 2025-08-24 — v13.4: починен ручной SWAP, выровнено хранение «Имя Ф.|uid», стабилен пылесос в деталях,

#                       совместимость с aiogram 3.x и SSOT, добавлены мягкие guard'ы и self-tests.
