# ███ [0] IMPORTS
# --------------------------------------------------------------------
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Dict, List, Set

from aiogram import Bot, Router, types
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from pytz import timezone
from difflib import SequenceMatcher

from core.config import settings
from core.db import get_user_info, get_all_leader_uids
from core.state import state
from core.utils import delete_previous_private_messages, truncate
from handlers.games import _delete_trigger, _refresh_menu
from handlers.poll_details import refresh_deal_details
from handlers.guide import PROFILE_BUTTON_TEXT
from services.amocrm import get_amocrm_deals
from services.gsheets import get_user_status_from_svetofor

# ── настройка модуля ────────────────────────────────────────────────
logger = logging.getLogger(__name__)
router = Router()
MSK_TZ = timezone("Europe/Moscow")

# История изменений:
#   • 2025-07-31 — перенёс future.annotations, убрал лишние импорты
#   • 2025-08-03 — добавлен PROFILE_BUTTON_TEXT и ReplyKeyboardBuilder


# ════════════════════════════════════════════════════════════════════
# [1] ГЛАВНОЕ МЕНЮ
# ════════════════════════════════════════════════════════════════════
# Единственный источник меню теперь в core.menu.
# Оставляем ре-экспорт, чтобы старые импорты не упали.
from core.menu import get_main_menu  # noqa: F401



# ════════════════════════════════════════════════════════════════════
# [2] ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ, НАПОМИНАНИЯ, «+»-КОНФИРМЫ
# ════════════════════════════════════════════════════════════════════

# ── 2.0  Конфиг ролей для игры ─────────────────────────────────────
# FIX 2025-08-03 — расширенный tolerant-поиск:
#   • точное совпадение → подстрока → fuzzy-ratio > 0.8.
#   • исключены дубли кода.
# --------------------------------------------------------------------
_RE_NON_ALNUM = re.compile(r"[^\w\d]+", re.UNICODE)

def _clean(txt: str) -> str:
    """«Понижает шум»: удаляет всё, кроме букв/цифр, приводит к lower()."""
    return _RE_NON_ALNUM.sub(" ", txt).lower().strip()

def _role_cfg(game_name: str) -> Dict[str, int]:
    """
    Возвращает конфиг ролей {"main_leaders": X, "assistants": Y}
    для *game_name* с tolerant-поиском.
    """
    norm = _clean(game_name)
    best_ratio = 0.0
    best_cfg: Dict[str, int] | None = None

    for key, cfg in settings.GAME_ROLE_MAPPING.items():
        k_norm = _clean(key)
        if norm == k_norm or norm in k_norm or k_norm in norm:
            return cfg
        ratio = SequenceMatcher(None, norm, k_norm).ratio()
        if ratio > best_ratio:
            best_ratio, best_cfg = ratio, cfg

    if best_ratio > 0.80 and best_cfg:
        return best_cfg  # type: ignore[return-value]
    return {"main_leaders": 1, "assistants": 0}

# ────────────────────────────────────────────────────────────────────
# 2.1  Напоминания «Отметьтесь в опросе»
# ────────────────────────────────────────────────────────────────────
async def _send_reminders() -> None:
    """
    Отправляет личное сообщение тем, кто ещё не заполнил опрос.
    Планируется через `_schedule_reminders()`.
    """
    if state.force_closed or not state.coordination_cycle_active:
        logger.debug("[reminders] skip: cycle finished/paused")
        return

    responded: Set[int] = {
        u["user_id"]
        for pdata in state.responses.values()
        for lst in (
            list(pdata["deals"].values())
            + [pdata["not_available"], pdata["admin_available"]]
        )
        for u in lst
    }
    pending = set(await get_all_leader_uids()) - responded
    if not pending:
        logger.debug("[reminders] everyone answered, nothing to ping")
        return

    bot = Bot.get_current()
    for uid in pending:
        try:
            await bot.send_message(uid, "👋 Напоминание! Отметьтесь в опросе.")
            logger.debug("[reminders] ping sent to %d", uid)
        except Exception as exc:
            logger.warning("[reminders] ping to %d FAILED: %s", uid, exc)

def _schedule_reminders() -> None:
    """
    Планирует два вызова `_send_reminders()` через 6 ч и 18 ч.
    Сохраняет таймеры в state.reminder_tasks.
    """
    _cancel_reminders()
    loop = asyncio.get_event_loop()
    for hours in (6, 18):
        handle = loop.call_later(
            hours * 3600,
            lambda: asyncio.create_task(_send_reminders()),
        )
        state.reminder_tasks.append(handle)
    logger.debug("[reminders] scheduled (%s)", ", ".join(f"{h} h" for h in (6, 18)))

def _cancel_reminders() -> None:
    """
    Отменяет все запланированные напоминания и очищает список.
    """
    for h in state.reminder_tasks:
        try:
            h.cancel()
        except Exception:
            pass
    if state.reminder_tasks:
        logger.debug("[reminders] %d timer(s) cancelled", len(state.reminder_tasks))
    state.reminder_tasks.clear()

# ────────────────────────────────────────────────────────────────────
# 2.2  Пост «Подтвердите +» в админ-чате
# ────────────────────────────────────────────────────────────────────
async def _request_confirmations() -> None:
    """
    Создаёт в admin-чате пост «Подтвердите “+”» и помечает, что он уже отправлен.
    Не дублирует публикацию, если уже был вызван.
    """
    if state.manual_confirm_requested:
        logger.debug("[confirm_post] already requested → skip")
        return
    if not state.admin_chat_id:
        logger.warning("[confirm_post] admin_chat_id not set")
        return

    bot = Bot.get_current()
    msg = await bot.send_message(
        state.admin_chat_id,
        "📌 Распределение готово! Подтвердите «+» под этим сообщением.",
    )
    state.current_event_period = [msg.message_id]
    state.manual_confirm_requested = True
    logger.info("[confirm_post] sent, msg_id=%d", msg.message_id)

# ────────────────────────────────────────────────────────────────────
# 2.3  Обновление detail-view карточек у пользователей
# ────────────────────────────────────────────────────────────────────
async def _refresh_detail_views(impacted: Set[int], refresh_all: bool) -> None:
    """
    Перерисовывает открытые detail-view’ы пользователей.
    """
    if refresh_all:
        impacted = {d["id"] for d in state.current_poll_deals}

    tasks = [
        refresh_deal_details(uid, deal_id)
        for (uid, deal_id) in list(state.detail_blocks)
        if deal_id in impacted
    ]
    if not tasks:
        logger.debug("[details] nothing to refresh")
        return

    logger.debug("[details] refreshing %d view(s) for deals: %s",
                 len(tasks), ", ".join(map(str, impacted)))
    await asyncio.gather(*tasks, return_exceptions=True)



# ════════════════════════════════════════════════════════════════════
# [3] СОЗДАНИЕ ОПРОСА
# ════════════════════════════════════════════════════════════════════
@router.message(Command("create_poll"))
@router.message(lambda m: m.text == "📋 Создать опрос")
async def create_poll_handler(message: types.Message) -> None:
    """
    Хендлер кнопки/команды «Создать опрос».

    Детализированное логирование:
    • INFO  — вызов хендлера, успешное создание опроса;
    • DEBUG — параметры, число сделок, детали по каждой части;
    • WARN  — причины раннего выхода (нет доступа, активный цикл и т.д.).
    """
    uid = message.from_user.id
    logger.info("[create_poll] invoked by %d, text=%s", uid, (message.text or "").strip())

    # --- проверка доступа ----------------------------------------------------
    ui = await get_user_info(uid) or {}
    role = ui.get("role")
    if role not in settings.ACCESS["poll"]:
        logger.warning("[create_poll] no access: uid=%d role=%s", uid, role)
        await message.answer("⛔ Нет доступа.", reply_markup=await get_main_menu(uid))
        await _delete_trigger(message)
        return

    if state.coordination_cycle_active:
        logger.warning("[create_poll] cycle already active, uid=%d", uid)
        await message.answer("⚠️ Уже есть активный опрос.")
        await _delete_trigger(message)
        return

    if not state.admin_chat_id:
        logger.warning("[create_poll] admin_chat_id not set, uid=%d", uid)
        await message.answer("⚠️ Чат не настроен.")
        await _delete_trigger(message)
        return

    # --- выбор сделок --------------------------------------------------------
    try:
        deals = await get_amocrm_deals(settings.SVETOFOR_SPREAD_ID)
    except Exception as exc:
        logger.exception("[create_poll] get_amocrm_deals failed: %s", exc)
        await message.answer("⚠️ Не удалось получить игры из AmoCRM.")
        await _delete_trigger(message)
        return

    now = datetime.now(tz=MSK_TZ)
    window = now + timedelta(days=14)

    poll_deals = [
        d
        for d in deals
        if d["status_id"] in settings.NEW_GAMES_STATUS_IDS
        and now <= d["event_datetime"] <= window
        and not d["team_leads"]
    ]
    logger.debug("[create_poll] %d deals fetched, %d suitable for poll",
                 len(deals), len(poll_deals))

    if not poll_deals:
        logger.info("[create_poll] no new games, uid=%d", uid)
        await message.answer("😔 Нет новых игр.", reply_markup=await get_main_menu(uid))
        await _delete_trigger(message)
        return

    # --- инициализация state -------------------------------------------------
    state.current_poll_deals        = poll_deals
    state.current_poll_leader       = uid
    state.responses.clear()
    state.distribution_cache.clear()
    state.coordination_cycle_active = True
    state.force_closed              = False
    state.deal_force_closed.clear()
    state.manual_confirm_requested  = False
    state.confirmed_users.clear()
    state.current_deal_ready.clear()
    state.all_ready_notified        = False

    # --- отправка опросов ----------------------------------------------------
    urgent = any(d["event_datetime"] <= now + timedelta(days=3) for d in poll_deals)
    header_base = "🚨 Срочные!" if urgent else "📊 Новые игры"
    chunks = [poll_deals[i : i + 8] for i in range(0, len(poll_deals), 8)]
    logger.debug("[create_poll] split into %d chunk(s)", len(chunks))

    for idx, chunk in enumerate(chunks, 1):
        header = f"{header_base} (Часть {idx})" if len(chunks) > 1 else header_base
        opts: List[str] = []
        idx_map: Dict[int, int] = {}

        for i, d in enumerate(chunk):
            s = f"🎉 {d['name']} — {d['event_datetime']:%d.%m}"
            if pkg := d.get("package"):
                s += f" · {pkg}"
            if extra := d.get("extra_services"):
                s += f" {extra}"
            opts.append(truncate(s))
            idx_map[i] = d["id"]

        opts += ["🚫 Не смогу", "🛡️ Админом"]

        poll = await Bot.get_current().send_poll(
            state.admin_chat_id,
            header,
            opts,
            is_anonymous=False,
            allows_multiple_answers=True,
        )
        state.responses[poll.poll.id] = {
            "deals": {d["id"]: [] for d in chunk},
            "not_available": [],
            "admin_available": [],
            "deal_indices": idx_map,
        }
        logger.debug("[create_poll] poll sent: id=%s, deals=%s",
                     poll.poll.id, list(idx_map.values()))

    # --- завершение ----------------------------------------------------------
    await message.answer("✅ Опросы отправлены.")
    await _refresh_menu(uid)
    await _send_leader_report(uid)

    # авто-завершение через N часов
    asyncio.get_event_loop().call_later(
        settings.POLL_DURATION_HOURS * 3600,
        lambda: asyncio.create_task(clear_poll_data(uid)),
    )
    _schedule_reminders()

    logger.info("[create_poll] poll created successfully, leader=%d, parts=%d",
                uid, len(chunks))
    await _delete_trigger(message)

# ════════════════════════════════════════════════════════════════════
# [4] ОТЧЁТ / КЛАВИАТУРА
# ════════════════════════════════════════════════════════════════════
def _merge_keyboards(
    k1: InlineKeyboardMarkup,
    k2: InlineKeyboardMarkup,
) -> InlineKeyboardMarkup:
    """
    Склеивает две Inline-клавиатуры, сохраняя порядок строк.
    Используется при формировании личного дашборда лидера:
      • k1 — кнопки игр,
      • k2 — action-панель (approve/refresh и т. д.).
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[*k1.inline_keyboard, *k2.inline_keyboard]
    )


async def generate_poll_report() -> str:
    """
    Строит текст отчёта и заполняет `state.distribution_keyboard`.

    • Для каждой игры показывает «✅/❌», название и дату.
    • Если минимум набран — добавляет кнопку «👍 Утвердить».
    • Когда готова хотя бы одна игра — добавляет «Утвердить все».
    """
    if not state.current_poll_deals or not state.responses:
        return "⚠️ Нет активных опросов."

    keyboard: List[List[InlineKeyboardButton]] = []
    any_ready = False

    for deal in state.current_poll_deals:
        did = deal["id"]
        if did in state.deal_force_closed:
            continue

        ready = await _is_deal_ready(did)
        any_ready |= ready
        icon = "✅" if ready else "❌"

        row: List[InlineKeyboardButton] = [
            InlineKeyboardButton(
                text=f"{icon} {deal['game_name']} — {deal['event_datetime']:%d.%m}",
                callback_data=f"show_deal_{did}",
            )
        ]
        if ready:
            row.append(
                InlineKeyboardButton(
                    text="👍 Утвердить",
                    callback_data=f"approve_deal_{did}",
                )
            )
        keyboard.append(row)

        logger.debug(
            "[report] deal_id=%d ready=%s (%s)",
            did,
            ready,
            deal["game_name"],
        )

    if any_ready:
        keyboard.append(
            [InlineKeyboardButton(text="Утвердить все", callback_data="approve_all_ready")]
        )

    state.distribution_keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard)
    return "📊 *Опрос создан. Выберите игру:*"

# ███ [4.x] Вспом. клавиатура отчёта и __all__
# --------------------------------------------------------------------
def _build_report_keyboard() -> InlineKeyboardMarkup:
    """
    Собирает клавиатуру для личного отчёта лидеру:
      • верхняя часть — кнопки игр (state.distribution_keyboard);
      • нижняя часть — action-панель из polls_distribution.
    """
    from handlers.polls_distribution import distribution_actions_markup  # локальный import

    games_kb = state.distribution_keyboard or InlineKeyboardMarkup(inline_keyboard=[])
    actions_kb = distribution_actions_markup()
    # «Склейка» двух клавиатур
    return InlineKeyboardMarkup(
        inline_keyboard=games_kb.inline_keyboard + actions_kb.inline_keyboard
    )

# Экспорт «приватных» функций, которые используют другие модули
__all__ = [
    "_cancel_reminders",
    "_request_confirmations",
    "_is_deal_ready",
    "clear_poll_data",
]
# История изменений: добавлено 2025-07-30 — фиксы Pylance (_build_report_keyboard, __all__)


# ════════════════════════════════════════════════════════════════════
# [5] ПРИЁМ ОТВЕТОВ
# ════════════════════════════════════════════════════════════════════
@router.poll_answer()
async def handle_poll_answer(event: types.PollAnswer) -> None:
    uid, poll_id, chosen = event.user.id, event.poll_id, event.option_ids
    data = state.responses.get(poll_id)
    if not data:
        return

    logger.debug("[answer] uid=%d poll=%s choices=%s", uid, poll_id, chosen)

    # очистка
    for lst in data["deals"].values():
        lst[:] = [u for u in lst if u["user_id"] != uid]
    data["not_available"][:] = [u for u in data["not_available"] if u["user_id"] != uid]
    data["admin_available"][:] = [u for u in data["admin_available"] if u["user_id"] != uid]

    ui = await get_user_info(uid) or {}
    base = {
        "user_id": uid,
        "first_name": ui.get("first_name", ""),
        "last_name_initial": ui.get("last_name_initial", ""),
        "is_admin_eligible": False,
    }

    num = len(data["deal_indices"])
    impacted: Set[int] = set()
    refresh_all = False

    for idx in chosen:
        if idx < num:
            did = data["deal_indices"][idx]
            if did not in state.deal_force_closed:
                data["deals"][did].append(base.copy())
                impacted.add(did)
        elif idx == num:
            data["not_available"].append(base.copy())
        else:
            adm = base.copy()
            adm["is_admin_eligible"] = True
            data["admin_available"].append(adm)
            refresh_all = True

    if refresh_all:
        for lst in data["deals"].values():
            lst[:] = [u for u in lst if not u.get("is_admin_eligible")]

    await _sync_leader_report()
    await _check_ready_state(impacted)
    asyncio.create_task(_refresh_detail_views(impacted, refresh_all))




# ════════════════════════════════════════════════════════════════════
# [6] ОТЧЁТ ЛИДЕРУ
# ════════════════════════════════════════════════════════════════════
async def _send_leader_report(leader_id: int) -> None:
    """Отправляет или обновляет отчёт лидеру."""
    bot = Bot.get_current()
    text = await generate_poll_report()
    kb = _build_report_keyboard()
    sent = await bot.send_message(
        leader_id, text, parse_mode="Markdown", reply_markup=kb
    )
    state.personal_report_message_id = sent.message_id
    state.last_user_messages[leader_id] = [sent]


@router.message(lambda m: m.text == "📊 Отчёт по опросу")
async def poll_report_handler(message: types.Message) -> None:
    """Кнопка «📊 Отчёт по опросу» в меню."""
    uid = message.from_user.id
    ui = await get_user_info(uid) or {}
    if ui.get("role") not in settings.ACCESS["poll"]:
        await message.answer("⛔ Нет доступа.", reply_markup=await get_main_menu(uid))
        await _delete_trigger(message)
        return
    if not state.coordination_cycle_active:
        await message.answer("⚠️ Нет активных опросов.", reply_markup=await get_main_menu(uid))
        await _delete_trigger(message)
        return

    await delete_previous_private_messages(uid)
    bot = Bot.get_current()
    text = await generate_poll_report()
    kb = _build_report_keyboard()
    dash = await bot.send_message(uid, text, parse_mode="Markdown", reply_markup=kb)
    state.personal_report_message_id = dash.message_id
    state.last_user_messages[uid] = [dash]

    await _refresh_menu(uid)
    await _delete_trigger(message)


# ███ [7] ГЕНЕРАЦИЯ И ПРОВЕРКА ГОТОВНОСТИ
# --------------------------------------------------------------------
async def _is_deal_ready(did: int) -> bool:
    """True, если для игры did набран минимум (учитывая админа)."""
    deal = next(d for d in state.current_poll_deals if d["id"] == did)
    cfg = _role_cfg(deal["game_name"])
    need_main, need_assist = cfg["main_leaders"], cfg["assistants"]

    admin_pkgs = {"стандарт", "стандарт+", "премиум", "vip", "вип", "биглион"}
    need_admin = int((deal.get("package") or "").lower().strip() in admin_pkgs)

    # ─── 1. main / assist ────────────────────────────────────────────
    have_main = have_assist = 0
    main_ids: Set[int] = set()
    assist_ids: Set[int] = set()

    for pdata in state.responses.values():
        for u in pdata["deals"].get(did, []):
            status = await get_user_status_from_svetofor(u["user_id"], deal["game_name"])
            if status == "green" and have_main < need_main:
                have_main += 1
                main_ids.add(u["user_id"])
            elif status in {"green", "yellow"} and have_assist < need_assist:
                have_assist += 1
                assist_ids.add(u["user_id"])

    # ─── 2. admin ────────────────────────────────────────────────────
    dist = state.distribution_cache.get(str(did), {})
    # 2.1 вручную назначен?
    have_admin = 1 if dist.get("admin") else 0

    # 2.2 автоподбор из admin_available, если ещё нужен
    if need_admin and not have_admin:
        for pdata in state.responses.values():
            # считаем только «часть» опроса, где есть эта игра
            if did not in pdata["deal_indices"].values():
                continue
            for adm in pdata["admin_available"]:
                uid = adm["user_id"]
                if (
                    adm.get("is_admin_eligible")
                    and uid not in main_ids
                    and uid not in assist_ids
                ):
                    have_admin = 1
                    break
            if have_admin:
                break

    logger.debug(
        "[deal_ready] id=%d main:%d/%d assist:%d/%d admin:%d/%d",
        did, have_main, need_main, have_assist, need_assist, have_admin, need_admin
    )

    return (
        have_main   >= need_main
        and have_assist >= need_assist
        and have_admin  >= need_admin
    )




async def _check_ready_state(impacted: Set[int]) -> None:
    """Уведомления: «минимум набран / все готовы»."""
    bot = Bot.get_current()
    lead = state.current_poll_leader
    newly_ready: List[str] = []

    for did in impacted:
        ready = await _is_deal_ready(did)
        if ready and not state.current_deal_ready.get(did):
            state.current_deal_ready[did] = True
            deal = next(d for d in state.current_poll_deals if d["id"] == did)
            newly_ready.append(f"{deal['game_name']} — {deal['event_datetime']:%d.%m}")

    if newly_ready:
        txt = "✅ *Минимум набран:*\n" + "\n".join(f"• {n}" for n in newly_ready)
        await bot.send_message(lead, txt, parse_mode="Markdown")

    if (
        state.current_poll_deals
        and all(state.current_deal_ready.get(d["id"]) for d in state.current_poll_deals)
        and not state.all_ready_notified
    ):
        await bot.send_message(lead, "🎉 Все игры укомплектованы минимумом!")
        state.all_ready_notified = True


async def generate_poll_report() -> str:
    """Строит текст отчёта и Inline-клавиатуру state.distribution_keyboard."""
    if not state.current_poll_deals or not state.responses:
        return "⚠️ Нет активных опросов."

    keyboard: List[List[InlineKeyboardButton]] = []
    for deal in state.current_poll_deals:
        did = deal["id"]
        if did in state.deal_force_closed:
            continue
        ready = await _is_deal_ready(did)
        icon = "✅" if ready else "❌"

        row = [
            InlineKeyboardButton(
                text=f"{icon} {deal['game_name']} — {deal['event_datetime']:%d.%m}",
                callback_data=f"show_deal_{did}",
            )
        ]
        if ready:
            row.append(
                InlineKeyboardButton(
                    text="👍 Утвердить",
                    callback_data=f"approve_deal_{did}",
                )
            )
        keyboard.append(row)

    if any(state.current_deal_ready.get(d["id"]) for d in state.current_poll_deals):
        keyboard.append([
            InlineKeyboardButton(text="Утвердить все", callback_data="approve_all_ready")
        ])

    state.distribution_keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard)
    return "📊 *Опрос создан. Выберите игру:*"


# ════════════════════════════════════════════════════════════════════
# [8] ПРИЁМ ОТВЕТОВ ПОЛЬЗОВАТЕЛЕЙ
# ════════════════════════════════════════════════════════════════════
@router.poll_answer()
async def handle_poll_answer(event: types.PollAnswer) -> None:
    """Фиксация выбора пользователя + пересчёт готовности."""
    uid, poll_id, chosen = event.user.id, event.poll_id, event.option_ids
    data = state.responses.get(poll_id)
    if not data:
        return

    # очищаем старые метки пользователя
    for lst in data["deals"].values():
        lst[:] = [u for u in lst if u["user_id"] != uid]
    data["not_available"][:] = [u for u in data["not_available"] if u["user_id"] != uid]
    data["admin_available"][:] = [u for u in data["admin_available"] if u["user_id"] != uid]

    ui = await get_user_info(uid) or {}
    base = {
        "user_id": uid,
        "first_name": ui.get("first_name", ""),
        "last_name_initial": ui.get("last_name_initial", ""),
        "is_admin_eligible": False,
    }

    num = len(data["deal_indices"])
    impacted: Set[int] = set()
    refresh_all = False

    for idx in chosen:
        if idx < num:  # конкретная игра
            did = data["deal_indices"][idx]
            if did not in state.deal_force_closed:
                data["deals"][did].append(base.copy())
                impacted.add(did)
        elif idx == num:  # «🚫 Не смогу»
            data["not_available"].append(base.copy())
        else:  # «🛡️ Админом»
            adm = base.copy()
            adm["is_admin_eligible"] = True
            data["admin_available"].append(adm)
            refresh_all = True

    if refresh_all:
        for lst in data["deals"].values():
            lst[:] = [u for u in lst if not u.get("is_admin_eligible")]

    await _sync_leader_report()
    await _check_ready_state(impacted)
    asyncio.create_task(_refresh_detail_views(impacted, refresh_all))


# ════════════════════════════════════════════════════════════════════
# [9] СИНХРОНИЗАЦИЯ ОТЧЁТА ЛИДЕРУ  (FIX: duplicate dashboards)
# ════════════════════════════════════════════════════════════════════
async def _sync_leader_report() -> None:
    """
    Поддерживает у лидера **один** актуальный дашборд.

    1. Пытается *редактировать* прежний пост (если id известен).
    2. Если не вышло — шлёт новый, перед этим удаляя/помечая все старые
       дашборды в `state.last_user_messages[leader]` **и** orphan-id.
    3. Обновляет `personal_report_message_id` + `last_user_messages`.
    """
    bot    = Bot.get_current()
    leader = state.current_poll_leader
    if not leader:
        logger.debug("[sync_report] skip: no leader")
        return

    text = await generate_poll_report()
    kb   = _build_report_keyboard()
    old_id = state.personal_report_message_id

    # ── 1. Попытка редактирования ──────────────────────────────────
    if old_id:
        try:
            await bot.edit_message_text(
                text,
                chat_id=leader,
                message_id=old_id,
                parse_mode="Markdown",
                reply_markup=kb,
            )
            logger.debug("[sync_report] edited msg %d", old_id)
            return
        except Exception as exc:
            logger.debug("[sync_report] edit failed (%s) → send new", exc)

    # ── 2. Отправляем новый, чистим старые ─────────────────────────
    try:
        sent = await bot.send_message(
            leader, text, parse_mode="Markdown", reply_markup=kb
        )
    except Exception as send_exc:
        logger.exception("[sync_report] FAILED to send new report: %s", send_exc)
        return

    leftovers = state.last_user_messages.get(leader, [])
    for msg in leftovers:
        if msg.message_id == sent.message_id:
            continue
        try:
            await bot.delete_message(chat_id=leader, message_id=msg.message_id)
            logger.debug("[sync_report] old dashboard %d deleted", msg.message_id)
        except Exception as del_exc:
            logger.debug("[sync_report] can't delete %d: %s", msg.message_id, del_exc)
            state.messages_to_delete.setdefault(leader, []).append(msg.message_id)

    # orphan-id: старый personal_report_message_id, которого нет в leftovers
    if old_id and old_id != sent.message_id and all(m.message_id != old_id for m in leftovers):
        try:
            await bot.delete_message(chat_id=leader, message_id=old_id)
            logger.debug("[sync_report] orphan old_id %d deleted", old_id)
        except Exception as del_exc:
            logger.debug("[sync_report] can't delete orphan %d: %s", old_id, del_exc)
            state.messages_to_delete.setdefault(leader, []).append(old_id)

    # ── 3. Обновляем состояние ─────────────────────────────────────
    state.personal_report_message_id = sent.message_id
    state.last_user_messages[leader] = [sent]


# ════════════════════════════════════════════════════════════════════
# [10] ЗАВЕРШЕНИЕ ЦИКЛА
# ════════════════════════════════════════════════════════════════════
async def clear_poll_data(user_id: int) -> None:
    """Полный сброс состояния цикла."""
    _cancel_reminders()
    state.responses.clear()
    state.current_poll_deals.clear()
    state.distribution_cache.clear()
    state.personal_report_message_id = None
    state.current_poll_leader = None
    state.coordination_cycle_active = False
    state.distribution_keyboard = None
    state.current_event_period = None
    state.force_closed = True
    state.deal_force_closed.clear()
    state.manual_confirm_requested = False
    state.confirmed_users.clear()
    state.current_deal_ready.clear()
    state.all_ready_notified = False
    state.pending_plus.clear()
    state.messages_to_delete.pop(user_id, None)

    bot = Bot.get_current()
    try:
        await bot.send_message(user_id, "♻️ Цикл распределения завершён.")
    except Exception:
        pass

    logger.info("[polls] cleared by %d", user_id)
    await _refresh_menu(user_id)


# ███ [99] _TEST
# --------------------------------------------------------------------
async def _test() -> None:
    """
    Smoke-тест get_main_menu и generate_poll_report (пустой),
    подменяем get_user_info на фиктивную, чтобы были кнопки.
    """
    # подменяем get_user_info, чтобы роль была в ACCESS["poll"]
    orig = get_user_info
    async def fake_get_user_info(uid: int) -> dict:
        return {"role": settings.ACCESS["poll"][0]}
    globals()["get_user_info"] = fake_get_user_info

    try:
        uid = 1
        state.coordination_cycle_active = True
        menu = await get_main_menu(uid)
        assert menu is not None, "Меню не должно быть None для роли poll"
        # проверяем, что кнопка «✉️ Рассылка уведомлений» действительно есть
        assert any(
            btn.text == "✉️ Рассылка уведомлений"
            for row in menu.keyboard
            for btn in row
        ), "Нет кнопки «✉️ Рассылка уведомлений» в меню"
        # проверяем отчёт
        state.current_poll_deals = []
        state.responses = {}
        assert await generate_poll_report() == "⚠️ Нет активных опросов."
        print("handlers/polls_lifecycle OK")
    finally:
        # восстанавливаем оригинальную функцию
        globals()["get_user_info"] = orig

if __name__ == "__main__":
    import asyncio
    asyncio.run(_test())
