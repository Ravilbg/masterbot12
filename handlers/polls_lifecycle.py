# handlers/polls_lifecycle.py — цикл опроса/распределения
# ─────────────────────────────────────────────────────────────────────
"""
Создание опросов, приём ответов, отчёт лидеру и служебные утилиты.

Версия 6.1 · 2025-08-11
──────────────────────────────────────────────────────────────────────
• Жёсткое правило «один активный блок» у лидера: edit → send → vacuum(keep).
• Исправлен «пылесос»: больше нет вызовов корутины без await.
• Генерация отчёта не дублируется, совместимость с polls_distribution/poll_details.
• Уведомления и таймеры не тронуты; логика автораспределения/готовности сохранена.
"""

from __future__ import annotations

# ███ [0] IMPORTS
# --------------------------------------------------------------------
import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Dict, List, Set

from aiogram import Bot, Router, types, F
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
#   • 2025-08-09 — импорт F; унификация approve → poll_approve_{id}
#   • 2025-08-11 — фикс «пылесоса», единый активный блок, удалены дубликаты

# ════════════════════════════════════════════════════════════════════
# [1] ГЛАВНОЕ МЕНЮ (ре-экспорт)
# ════════════════════════════════════════════════════════════════════
from core.menu import get_main_menu  # noqa: F401


# ════════════════════════════════════════════════════════════════════
# [2] ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ, НАПОМИНАНИЯ, ДЕТАЛИ
# ════════════════════════════════════════════════════════════════════

# ── 2.0  Конфиг ролей для игры (tolerant-match) ────────────────────
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

def _deal_title(deal: dict) -> str:
    """Безопасно возвращает заголовок игры для UI/уведомлений."""
    return str(deal.get("game_name") or deal.get("name") or f"Сделка #{deal.get('id')}")

# ────────────────────────────────────────────────────────────────────
# 2.1  Напоминания «Отметьтесь в опросе»
# ────────────────────────────────────────────────────────────────────
async def _send_reminders() -> None:
    """
    Отправляет ЛС тем, кто ещё не заполнил опрос. Планируется через _schedule_reminders().
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
    """Планирует два вызова `_send_reminders()` через 6 ч и 18 ч."""
    _cancel_reminders()
    loop = asyncio.get_event_loop()
    for hours in (6, 18):
        handle = loop.call_later(
            hours * 3600,
            lambda: asyncio.create_task(_send_reminders()),
        )
        state.reminder_tasks.append(handle)
    logger.debug("[reminders] scheduled (6 h, 18 h)")

def _cancel_reminders() -> None:
    """Отменяет все запланированные напоминания и очищает список."""
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
    """Создаёт пост «Подтвердите “+”» и помечает, что он уже отправлен."""
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
    """Перерисовывает открытые detail-view’ы пользователей."""
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

# ────────────────────────────────────────────────────────────────────
# 2.4  Светофор-адаптер (совместимость sync/async, безопасные ошибки)
# ────────────────────────────────────────────────────────────────────
async def _sv_status(user_id: int, game_name: str) -> str:
    """
    Универсальный вызов статуса «Светофора».

    • Поддерживает как синхронную, так и асинхронную реализацию
      services.gsheets.get_user_status_from_svetofor.
    • На любых исключениях возвращает '' и пишет предельно информативный лог.
    """
    try:
        res = get_user_status_from_svetofor(user_id, game_name)
        if asyncio.iscoroutine(res):
            status = await res  # type: ignore[func-returns-value]
        else:
            status = res
        status = (status or "").strip().lower()
        if status not in {"green", "yellow", "red", ""}:
            logger.debug("[svetofor] normalize unknown status '%s' → '' (uid=%s, game=%s)",
                         status, user_id, game_name)
            return ""
        return status
    except Exception as exc:
        logger.warning("[svetofor] status fetch failed (uid=%s, game=%s): %s",
                       user_id, game_name, exc)
        return ""


# ════════════════════════════════════════════════════════════════════
# [3] СОЗДАНИЕ ОПРОСА
# ════════════════════════════════════════════════════════════════════
@router.message(Command("create_poll"))
@router.message(lambda m: m.text == "📋 Создать опрос")
async def create_poll_handler(message: types.Message) -> None:
    """
    Хендлер кнопки/команды «Создать опрос».

    • Проверка доступа, активного цикла и admin-чата.
    • Выбор игр (14 дней вперёд, статусы из settings.NEW_GAMES_STATUS_IDS, без назначенных ведущих).
    • Инициализация state, рассылка Poll’ов по частям до 8 опций.
    • Таймер авто-завершения, напоминания, отчёт лидеру.
    • ВАЖНО: при старте цикла немедленно меняем пункт меню «📋 Создать опрос» → «📊 Отчёт по опросу».
    """
    uid = message.from_user.id
    logger.info("[create_poll] invoked by %d, text=%s", uid, (message.text or "").strip())

    # ── 1. проверки доступа ─────────────────────────────────────────
    ui = await get_user_info(uid) or {}
    role = ui.get("role")
    if role not in settings.ACCESS["poll"]:
        logger.warning("[create_poll] no access: uid=%d role=%s", uid, role)
        await message.answer("⛔ Нет доступа.", reply_markup=await get_main_menu(uid))
        await _delete_trigger(message)
        return

    # если цикл уже активен — сразу показываем «Отчёт по опросу» и обновляем меню
    if state.coordination_cycle_active:
        logger.warning("[create_poll] cycle already active, uid=%d", uid)
        await message.answer("⚠️ Уже есть активный опрос.", reply_markup=await get_main_menu(uid))
        # гарантируем замену пункта меню на «📊 Отчёт по опросу»
        try:
            await _refresh_menu(uid)
        except Exception as e:
            logger.debug("[create_poll] _refresh_menu on active cycle failed: %s", e)
        await _delete_trigger(message)
        return

    if not state.admin_chat_id:
        logger.warning("[create_poll] admin_chat_id not set, uid=%d", uid)
        await message.answer("⚠️ Чат не настроен.", reply_markup=await get_main_menu(uid))
        await _delete_trigger(message)
        return

    # ── 2. получаем сделки из AmoCRM ────────────────────────────────
    try:
        deals = await get_amocrm_deals()               # ← без аргумента
    except Exception as exc:
        logger.exception("[create_poll] get_amocrm_deals failed: %s", exc)
        await message.answer("⚠️ Не удалось получить игры из AmoCRM.")
        await _delete_trigger(message)
        return

    now     = datetime.now(tz=MSK_TZ)
    window  = now + timedelta(days=14)
    poll_deals = [
        d for d in deals
        if d["status_id"] in settings.NEW_GAMES_STATUS_IDS
        and now <= d["event_datetime"] <= window
        and not d.get("team_leads")
    ]
    logger.debug("[create_poll] %d deals fetched, %d suitable for poll",
                 len(deals), len(poll_deals))

    if not poll_deals:
        logger.info("[create_poll] no new games, uid=%d", uid)
        await message.answer("😔 Нет новых игр.", reply_markup=await get_main_menu(uid))
        await _delete_trigger(message)
        return

    # ── 3. инициализация state ─────────────────────────────────────
    state.current_poll_deals         = poll_deals
    state.current_poll_leader        = uid
    state.responses.clear()
    state.distribution_cache.clear()
    state.coordination_cycle_active  = True
    state.force_closed               = False
    state.deal_force_closed.clear()
    state.manual_confirm_requested   = False
    state.confirmed_users.clear()
    state.current_deal_ready.clear()
    state.all_ready_notified         = False
    state.personal_report_message_id = None

    # немедленная замена пункта меню на «📊 Отчёт по опросу» (не ждём конца процедуры)
    try:
        await _refresh_menu(uid)
    except Exception as e:
        logger.debug("[create_poll] _refresh_menu after init failed: %s", e)

    # ── 4. отправляем опрос(ы) ──────────────────────────────────────
    urgent = any(d["event_datetime"] <= now + timedelta(days=3) for d in poll_deals)
    header_base = "🚨 Срочные!" if urgent else "📊 Новые игры"
    chunks = [poll_deals[i : i + 8] for i in range(0, len(poll_deals), 8)]
    logger.debug("[create_poll] split into %d chunk(s)", len(chunks))

    for idx, chunk in enumerate(chunks, 1):
        header = f"{header_base} (Часть {idx})" if len(chunks) > 1 else header_base
        opts: List[str] = []
        idx_map: Dict[int, int] = {}

        for i, d in enumerate(chunk):
            s = f"🎉 {_deal_title(d)} — {d['event_datetime']:%d.%m}"
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

    # ── 5. завершаем ────────────────────────────────────────────────
    await message.answer("✅ Опросы отправлены.")
    # меню ещё раз — на случай, если пользователь успел уйти в другое окно
    try:
        await _refresh_menu(uid)
    except Exception as e:
        logger.debug("[create_poll] _refresh_menu at finalize failed: %s", e)

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
# История изменений [3]:
# 2025-08-12 — немедленная замена пункта меню «📋 Создать опрос» → «📊 Отчёт по опросу»
#              при старте цикла и при повторном вызове в активном цикле; все await сохранены.

# ════════════════════════════════════════════════════════════════════
# [4] ОТЧЁТ / КЛАВИАТУРА / ПРИЁМ ОТВЕТОВ
# ════════════════════════════════════════════════════════════════════
def _merge_keyboards(
    k1: InlineKeyboardMarkup,
    k2: InlineKeyboardMarkup,
) -> InlineKeyboardMarkup:
    """Склейка двух Inline-клавиатур, сохраняя порядок строк."""
    return InlineKeyboardMarkup(
        inline_keyboard=[*k1.inline_keyboard, *k2.inline_keyboard]
    )

async def generate_poll_report() -> str:
    """
    Строит текст отчёта и заполняет `state.distribution_keyboard`.

    • Для каждой игры показывает «✅/❌», название и дату.
    • Если минимум набран — добавляет кнопку «👍 Утвердить».
    • Если игра уже зафиксирована — показывает «✅ Утверждено».
    • «Утвердить все» появляется, только если есть готовые и не зафиксированные игры.
    • Для ready-игр синхронизирует distribution из poll_details → state.poll_distribution.
    """
    if not state.current_poll_deals or not state.responses:
        return "⚠️ Нет активных опросов."

    keyboard: List[List[InlineKeyboardButton]] = []
    any_ready_unlocked = False

    locked_map = getattr(state, "locked_distribution", {}) or {}

    for deal in state.current_poll_deals:
        did = deal["id"]
        if did in state.deal_force_closed:
            continue

        ready = await _is_deal_ready(did)
        locked = did in locked_map
        title = _deal_title(deal)
        icon = "✅" if (ready or locked) else "❌"

        # Синхронизация распределения из деталей (только для ready)
        if ready and not locked:
            details = getattr(state, "poll_details", {}).get(did)
            if details and "distribution" in details:
                state.poll_distribution[did] = details["distribution"]
                logger.debug("[report] deal_id=%d distribution synced from poll_details", did)
            else:
                logger.warning("[report] deal_id=%d ready, но нет distribution в poll_details", did)

        row: List[InlineKeyboardButton] = [
            InlineKeyboardButton(
                text=f"{icon} {title} — {deal['event_datetime']:%d.%m}",
                callback_data=f"show_deal_{did}",
            )
        ]
        if locked:
            row.append(
                InlineKeyboardButton(
                    text="✅ Утверждено",
                    callback_data="noop",
                )
            )
        elif ready:
            row.append(
                InlineKeyboardButton(
                    text="👍 Утвердить",
                    callback_data=f"poll_approve_{did}",
                )
            )
            any_ready_unlocked = True

        keyboard.append(row)
        logger.debug("[report] deal_id=%d ready=%s locked=%s (%s)", did, ready, locked, title)

    if any_ready_unlocked:
        keyboard.append([InlineKeyboardButton(text="Утвердить все", callback_data="approve_all_ready")])

    state.distribution_keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard)
    return "📊 *Опрос создан. Выберите игру:*"

def _build_report_keyboard() -> InlineKeyboardMarkup:
    """
    Собирает клавиатуру для личного отчёта лидеру:
      • верх — кнопки игр (state.distribution_keyboard);
      • низ — action-панель из polls_distribution.
    """
    from handlers.polls_distribution import distribution_actions_markup  # локальный import
    games_kb = state.distribution_keyboard or InlineKeyboardMarkup(inline_keyboard=[])
    actions_kb = distribution_actions_markup()
    return InlineKeyboardMarkup(inline_keyboard=games_kb.inline_keyboard + actions_kb.inline_keyboard)

@router.poll_answer()
async def handle_poll_answer(event: types.PollAnswer) -> None:
    """Фиксация выбора пользователя + пересчёт готовности (ЕДИНАЯ версия)."""
    uid, poll_id, chosen = event.user.id, event.poll_id, event.option_ids
    data = state.responses.get(poll_id)
    if not data:
        return

    logger.debug("[answer] uid=%d poll=%s choices=%s", uid, poll_id, chosen)

    # очистка старых меток пользователя
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
            # «🚫 Не смогу»
            data["not_available"].append(base.copy())
        else:
            # «🛡️ Админом» — ДОПОЛНИТЕЛЬНО к выбору игр, не вычищаем из deals
            adm = base.copy()
            adm["is_admin_eligible"] = True
            data["admin_available"].append(adm)
            refresh_all = True  # обновим админ-блоки в деталях

    # ВНИМАНИЕ: преднамеренно НЕ удаляем пользователя из выбранных игр,
    # даже если он отметил «🛡️ Админом». Это фикс текущей ошибки распределения.

    await _sync_leader_report()
    await _check_ready_state(impacted)
    asyncio.create_task(_refresh_detail_views(impacted, refresh_all))

# ════════════════════════════════════════════════════════════════════
# [5] ОТЧЁТ ЛИДЕРУ И ГОТОВНОСТЬ ИГР
# ════════════════════════════════════════════════════════════════════
async def _send_leader_report(leader_id: int) -> None:
    """Отправляет или обновляет отчёт лидеру (первичная выдача)."""
    bot = Bot.get_current()
    text = await generate_poll_report()
    kb = _build_report_keyboard()

    # перед отправкой подчистим старые служебные сообщения
    try:
        await delete_previous_private_messages(bot, leader_id, keep=[])
    except TypeError:
        # совместимость со старой сигнатурой
        try:
            await delete_previous_private_messages(leader_id)  # type: ignore
        except Exception:
            pass

    sent = await bot.send_message(leader_id, text, parse_mode="Markdown", reply_markup=kb)
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

    bot = Bot.get_current()
    # подчистим старые перед отрисовкой нового
    try:
        await delete_previous_private_messages(bot, uid, keep=[])
    except TypeError:
        try:
            await delete_previous_private_messages(uid)  # type: ignore
        except Exception:
            pass

    text = await generate_poll_report()
    kb = _build_report_keyboard()
    dash = await bot.send_message(uid, text, parse_mode="Markdown", reply_markup=kb)
    state.personal_report_message_id = dash.message_id
    state.last_user_messages[uid] = [dash]

    await _refresh_menu(uid)
    await _delete_trigger(message)

# ────────────────────────────────────────────────────────────────────
# 5.1 Готовность одной игры
# ────────────────────────────────────────────────────────────────────
async def _is_deal_ready(did: int) -> bool:
    """
    True, если для игры did набран минимум по маппингу и цветам «Светофора».
    Логика:
      green  → main (если есть слот), иначе assist (если есть слот), иначе trainee
      yellow → assist (если есть слот), иначе trainee
      red    → trainee
      ''     → трактуем как 'yellow' (мягкий фолбэк, если таблица ответила пусто)
    Админ: обязателен для некоторых пакетов — берём из ручного назначения
    (state.distribution_cache) или из "админ доступен" при отсутствии пересечений.
    """
    # 0) мета по сделке и требования по ролям
    deal = next(d for d in state.current_poll_deals if d["id"] == did)
    game_name = deal.get("game_name") or deal.get("name") or ""
    cfg = _role_cfg(game_name)
    need_main, need_assist = int(cfg["main_leaders"]), int(cfg["assistants"])

    admin_pkgs = {"стандарт", "стандарт+", "премиум", "vip", "вип", "биглион"}
    need_admin = int((deal.get("package") or "").lower().strip() in admin_pkgs)

    # 1) проходим по откликнувшимся и раскладываем по ролям
    team: Dict[str, List[int]] = {"main": [], "assist": [], "trainee": []}
    seen: Set[int] = set()

    for pdata in state.responses.values():
        users = pdata["deals"].get(did, [])
        if not users:
            continue

        for u in users:
            uid = int(u["user_id"])
            if uid in seen:
                continue  # один пользователь — одна роль

            status = await _sv_status(uid, game_name)  # 'green'|'yellow'|'red'|''
            if not status:
                status = "yellow"  # мягкий фолбэк (ключ валиден, но ячейка могла быть белой)

            # маппинг цвета → роль (с учётом занятых слотов)
            if status == "green":
                if len(team["main"]) < need_main:
                    team["main"].append(uid); seen.add(uid); continue
                if len(team["assist"]) < need_assist:
                    team["assist"].append(uid); seen.add(uid); continue
                team["trainee"].append(uid); seen.add(uid); continue

            if status == "yellow":
                if len(team["assist"]) < need_assist:
                    team["assist"].append(uid); seen.add(uid); continue
                team["trainee"].append(uid); seen.add(uid); continue

            # status == 'red'
            team["trainee"].append(uid); seen.add(uid); continue

    # ── промо ассистента в основного, если не хватило main
    have_main = len(team["main"])
    have_assist = len(team["assist"])
    if have_main < need_main and have_assist > 0:
        promote_cnt = min(need_main - have_main, have_assist)
        promoted = team["assist"][:promote_cnt]
        team["assist"] = team["assist"][promote_cnt:]
        team["main"].extend(promoted)
        have_main = len(team["main"])
        have_assist = len(team["assist"])
        logger.debug("[deal_ready] promote %d assist → main (fallback)", promote_cnt)

    # 2) админ: вручную назначенный (distribution_cache) либо из "админ доступен"
    dist = state.distribution_cache.get(str(did), {}) or {}
    have_admin = 1 if dist.get("admin") else 0

    if need_admin and not have_admin:
        assigned = set(team["main"]) | set(team["assist"])
        for pdata in state.responses.values():
            if did not in pdata["deal_indices"].values():
                continue
            for adm in pdata["admin_available"]:
                uid = int(adm["user_id"])
                if uid not in assigned:
                    have_admin = 1
                    dist.setdefault("admin", uid)
                    state.distribution_cache[str(did)] = dist
                    break
            if have_admin:
                break

    logger.debug(
        "[deal_ready] id=%d main:%d/%d assist:%d/%d admin:%d/%d",
        did, have_main, need_main, have_assist, need_assist, have_admin, need_admin
    )

    return (
        have_main >= need_main
        and have_assist >= need_assist
        and have_admin >= need_admin
    )

# ────────────────────────────────────────────────────────────────────
# 5.2 Уведомления о готовности
# ────────────────────────────────────────────────────────────────────
async def _check_ready_state(impacted: Set[int]) -> None:
    """Уведомления: «предварительный состав набран / все готовы»."""
    bot = Bot.get_current()
    lead = state.current_poll_leader
    chat_id = state.admin_chat_id or lead
    newly_ready: List[str] = []

    for did in impacted:
        ready = await _is_deal_ready(did)
        if ready and not state.current_deal_ready.get(did):
            state.current_deal_ready[did] = True
            deal = next(d for d in state.current_poll_deals if d["id"] == did)
            newly_ready.append(f"{_deal_title(deal)} — {deal['event_datetime']:%d.%m}")

    if newly_ready:
        txt = (
            "✅ *Предварительный состав команды на игру набран!*\n"
            "Успейте отметиться в опросе, чтобы участвовать в распределении:\n"
            + "\n".join(f"• {n}" for n in newly_ready)
        )
        await bot.send_message(chat_id, txt, parse_mode="Markdown")

    if (
        state.current_poll_deals
        and all(state.current_deal_ready.get(d["id"]) for d in state.current_poll_deals)
        and not state.all_ready_notified
    ):
        await bot.send_message(
            chat_id,
            "✅ Для всех игр определён предварительный состав команды. "
            "Успейте отметиться в опросе, чтобы участвовать в распределении.",
            parse_mode="Markdown"
        )
        state.all_ready_notified = True

# Экспорт «приватных» функций, используемых снаружи
__all__ = [
    "_cancel_reminders",
    "_request_confirmations",
    "_is_deal_ready",
    "_sync_leader_report",
    "clear_poll_data",
    "_vacuum_old_messages",
]


# ════════════════════════════════════════════════════════════════════
# [6] СИНХРОНИЗАЦИЯ, ОЧИСТКА, ФОНОВЫЕ УТИЛИТЫ, TESTS
# ════════════════════════════════════════════════════════════════════
async def _sync_leader_report() -> None:
    """
    Поддерживает у лидера **один** актуальный дашборд.

    Алгоритм:
      1) Пытаемся отредактировать прежний пост (если id известен).
      2) Если редактирование не удалось — отправляем новый.
      3) «Пылесос»: удаляем все старые служебные сообщения и фиксируем новый как единственный активный.
    """
    bot    = Bot.get_current()
    leader = state.current_poll_leader
    if not leader:
        logger.debug("[sync_report] skip: no leader")
        return

    text = await generate_poll_report()
    kb   = _build_report_keyboard()
    old_id = state.personal_report_message_id

    # ── 1. Попытка редактирования существующего сообщения
    if old_id:
        try:
            await bot.edit_message_text(
                text,
                chat_id=leader,
                message_id=old_id,
                parse_mode="Markdown",
                reply_markup=kb,
            )
            # Зафиксируем, что у нас один активный пост (старые из state.last_user_messages не нужны)
            try:
                prev = (state.last_user_messages or {}).get(leader, [])
                keep = [m for m in prev if getattr(m, "message_id", None) == old_id]
                try:
                    await delete_previous_private_messages(bot, leader, keep=keep)
                except TypeError:
                    await delete_previous_private_messages(leader)  # type: ignore
            except Exception as e_vac:
                logger.debug("[sync_report] vacuum after edit failed: %s", e_vac)
            logger.debug("[sync_report] edited msg %d", old_id)
            return
        except Exception as exc:
            logger.debug("[sync_report] edit failed (%s) → send new", exc)

    # ── 2. Отправляем новый
    try:
        sent = await bot.send_message(leader, text, parse_mode="Markdown", reply_markup=kb)
    except Exception as send_exc:
        logger.exception("[sync_report] FAILED to send new report: %s", send_exc)
        return

    # ── 3. «Пылесос»: удаляем всё, кроме только что отправленного сообщения
    try:
        try:
            await delete_previous_private_messages(bot, leader, keep=[sent])
        except TypeError:
            await delete_previous_private_messages(leader)  # type: ignore
    except Exception as del_exc:
        logger.debug("[sync_report] vacuum keep=[%s] failed: %s", sent.message_id, del_exc)

    state.personal_report_message_id = sent.message_id
    state.last_user_messages[leader] = [sent]
    logger.info("[sync_report] rendered msg_id=%s", sent.message_id)

# ────────────────────────────────────────────────────────────────────
# 6.1 Завершение цикла
# ────────────────────────────────────────────────────────────────────
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
    state.poll_details.clear()
    state.deal_titles.clear()
    state.locked_distribution.clear()
    state.pending_confirmations.clear()
    state.poll_distribution.clear()
    state.detail_blocks.clear()
    # чистим только очередь текущего пользователя (если была)
    state.messages_to_delete.pop(user_id, None)

    bot = Bot.get_current()
    try:
        await bot.send_message(user_id, "♻️ Цикл распределения завершён.")
    except Exception:
        pass

    logger.info("[polls] cleared by %d", user_id)
    await _refresh_menu(user_id)

# ────────────────────────────────────────────────────────────────────
# 6.2 Автопроверка «все игры закрыты?» и завершение цикла
# ────────────────────────────────────────────────────────────────────
async def _check_cycle_finished(trigger_user: int | None = None) -> None:
    """Если активных игр не осталось — закрываем цикл."""
    if not state.coordination_cycle_active:
        return
    if state.current_poll_deals:
        return
    leader = state.current_poll_leader or trigger_user or 0
    await clear_poll_data(leader)

async def finish_if_all_deals_completed(bot: Bot | None = None) -> None:
    """
    Завершает цикл, когда все игры из текущего опроса переведены в «Завершение сделки»
    (или отсутствуют среди активных сделок AmoCRM) либо вручную выведены руководителем.

    Вызывается после подтверждений ролей.
    """
    if not state.coordination_cycle_active or not state.current_poll_deals:
        return

    try:
        from services.amocrm import get_amocrm_deals  # type: ignore
        deals = await get_amocrm_deals()
    except Exception as e:
        logger.debug("[finish_cycle] get_amocrm_deals failed: %s", e)
        return

    target_ids = {int(d["id"]) for d in state.current_poll_deals}
    status_ok = {"завершение сделки", "закрыта", "реализация завершена"}
    alive = 0

    for d in deals:
        did = int(d.get("id", 0) or 0)
        if did not in target_ids:
            continue
        st = (d.get("status_name") or "").strip().lower()
        if st not in status_ok:
            alive += 1

    # учтём ручной вывод игр из цикла
    for d in list(state.current_poll_deals):
        if d.get("id") in state.deal_force_closed:
            target_ids.discard(d.get("id"))

    if alive == 0 or not target_ids:
        leader = state.current_poll_leader or 0
        await clear_poll_data(leader)
        logger.info("[finish_cycle] completed: all deals closed or removed")

# ────────────────────────────────────────────────────────────────────
# 6.3 Фоновый «пылесос» для устаревших сообщений (ручной и планировщик)
# ────────────────────────────────────────────────────────────────────
async def _vacuum_old_messages() -> None:
    """
    Раз в 15 минут пытаемся удалить сообщения, помеченные к очистке.

    • Работает только через create_task/планировщик (НЕ вызывать синхронно).
    • Логирует ошибки удаления, чтобы видеть причину.
    • Если Bot недоступен — просто выходим, задача запустится в следующий раз.
    """
    try:
        bot = Bot.get_current()
    except Exception as e:
        logger.warning("[vacuum] Bot.get_current() недоступен: %s", e)
        return

    if not state.messages_to_delete:
        logger.debug("[vacuum] Очередь очистки пуста")
        return

    logger.info("[vacuum] Запущен, всего пользователей в очереди: %d", len(state.messages_to_delete))

    for uid, msg_ids in list(state.messages_to_delete.items()):
        updated: list[int] = []
        for mid in msg_ids:
            try:
                await bot.delete_message(chat_id=uid, message_id=mid)
                logger.debug("[vacuum] Удалено сообщение %s для uid=%s", mid, uid)
            except Exception as e:
                logger.debug("[vacuum] Не удалось удалить сообщение %s для uid=%s: %s", mid, uid, e)
                updated.append(mid)  # оставить, если не удалось
        if updated:
            state.messages_to_delete[uid] = updated
        else:
            state.messages_to_delete.pop(uid, None)

    logger.info("[vacuum] Завершён, очередь после очистки: %d", len(state.messages_to_delete))


# ███ [99] _TEST
# --------------------------------------------------------------------
async def _test() -> None:
    """
    Smoke-тест _sync_leader_report: имитируем отсутствие старого сообщения —
    должен отправиться новый, а затем пылесос оставить только его.
    """
    class _Msg:
        def __init__(self, mid): self.message_id = mid

    # подготовка фейкового состояния
    uid = 123
    state.current_poll_leader = uid
    state.coordination_cycle_active = True
    state.current_poll_deals = []
    state.responses = {}
    state.last_user_messages[uid] = [_Msg(10), _Msg(11)]
    state.personal_report_message_id = None

    # подмена generate_poll_report/_build_report_keyboard
    async def _fake_report(): return "⚠️ Нет активных опросов."
    def _fake_kb(): return InlineKeyboardMarkup(inline_keyboard=[])
    globals()["generate_poll_report"], globals()["_build_report_keyboard"] = _fake_report, _fake_kb

    # заглушки bot методов
    class _Bot:
        async def edit_message_text(self, *a, **kw): raise RuntimeError("no old message")
        async def send_message(self, chat_id, text, parse_mode=None, reply_markup=None):
            class _S: message_id = 99
            return _S()
        async def delete_message(self, chat_id, message_id): return
    Bot.set_current(_Bot())  # type: ignore

    # заглушка delete_previous_private_messages с новой и старой сигнатурой
    async def _fake_delete(bot_or_uid, uid=None, keep=None): return
    globals()["delete_previous_private_messages"] = _fake_delete

    await _sync_leader_report()
    assert state.personal_report_message_id == 99
    assert [m.message_id for m in state.last_user_messages[uid]] == [99]
    print("handlers/polls_lifecycle [sync+vacuum] OK")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_test())
