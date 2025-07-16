# handlers/polls.py
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Set

from aiogram import Bot, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from pytz import timezone

from core.config import settings
from core.db import get_user_info
from core.state import state
from core.utils import delete_previous_private_messages, truncate
from handlers.poll_details import refresh_deal_details
from services.amocrm import get_amocrm_deals, update_amocrm_tags
from services.gsheets import get_user_status_from_svetofor
from handlers.games import _refresh_menu, _delete_trigger  # для пересборки меню и удаления триггеров

logger = logging.getLogger(__name__)
router = Router()
MSK_TZ = timezone("Europe/Moscow")


async def get_main_menu(user_id: int) -> ReplyKeyboardMarkup | None:
    ui = await get_user_info(user_id)
    role = (ui or {}).get("role", "")
    btns: List[types.KeyboardButton] = []

    if role in settings.ACCESS["games"]:
        btns += [
            types.KeyboardButton(text="📅 Новые игры"),
            types.KeyboardButton(text="✅ Распределённые игры"),
        ]
    if role in settings.ACCESS["poll"]:
        if state.coordination_cycle_active:
            btns.append(types.KeyboardButton(text="📊 Отчёт по опросу"))
        else:
            btns.append(types.KeyboardButton(text="📋 Создать опрос"))

    if not btns:
        return None

    builder = ReplyKeyboardBuilder()
    builder.add(*btns).adjust(2)
    return builder.as_markup(resize_keyboard=True)


def _distribution_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="👌 Утвердить распределение", callback_data="distribute_leaders")
    kb.button(text="💾 Сохранить в AmoCRM",     callback_data="save_distribution")
    kb.button(text="🔧 Изменить состав вручную", callback_data="manual_edit")
    kb.adjust(1)
    return kb.as_markup()


def _role_cfg(game_name: str) -> Dict[str, int]:
    """
    Возвращает настройки ролей для игры без учёта регистра.
    """
    nm = game_name.strip().lower()
    for key, cfg in settings.GAME_ROLE_MAPPING.items():
        if key.strip().lower() == nm:
            return cfg
    # fallback, если не найдётся в маппинге
    return {"main_leaders": 1, "assistants": 0}


async def _is_admin_role(uid: int) -> bool:
    ui = await get_user_info(uid)
    role = (ui or {}).get("role", "").lower().strip()
    return role in {"администратор", "админ", "руководитель", "administrator"}


@router.message(Command("create_poll"))
@router.message(lambda m: m.text and m.text.strip() == "📋 Создать опрос")
async def create_poll_handler(message: types.Message) -> None:
    bot = Bot.get_current()
    uid = message.from_user.id

    ui = await get_user_info(uid)
    if not ui or ui["role"] not in settings.ACCESS["poll"]:
        await message.answer("⛔ Нет доступа.", reply_markup=await get_main_menu(uid))
        await _delete_trigger(message)
        return

    if state.coordination_cycle_active:
        await message.answer("⚠️ Уже есть активный опрос.")
        await _delete_trigger(message)
        return

    if not state.admin_chat_id:
        await message.answer("⚠️ Чат ведущих не настроен.")
        await _delete_trigger(message)
        return

    spread_id = settings.SVETOFOR_SPREAD_ID
    deals    = await get_amocrm_deals(spread_id)
    now      = datetime.now(tz=MSK_TZ)
    window   = now + timedelta(days=14)
    poll_deals = [
        d for d in deals
        if d["status_id"] in settings.NEW_GAMES_STATUS_IDS
        and now <= d["event_datetime"] <= window
        and not d["team_leads"]
    ]
    if not poll_deals:
        await message.answer("😔 Нет подходящих игр для опроса.",
                             reply_markup=await get_main_menu(uid))
        await _delete_trigger(message)
        return

    # фиксация состояния
    state.current_poll_deals        = poll_deals
    state.current_poll_leader       = uid
    state.responses.clear()
    state.distribution_cache.clear()
    state.coordination_cycle_active = True

    urgent      = any(d["event_datetime"] <= now + timedelta(days=3) for d in poll_deals)
    header_base = "🚨 Срочные игры!" if urgent else "📊 Новые игры"
    chunks      = [poll_deals[i:i+8] for i in range(0, len(poll_deals), 8)]

    for idx, chunk in enumerate(chunks, 1):
        header = f"{header_base} (Часть {idx})" if len(chunks) > 1 else header_base
        opts, idx_map = [], {}
        for i, d in enumerate(chunk):
            base = f"🎉 {d['name']} – {d['event_datetime'].strftime('%d.%m')}"
            if d["package"]:
                base += f" – {d['package']}"
            if d["extra_services"]:
                base += f" {d['extra_services']}"
            opts.append(truncate(base))
            idx_map[i] = d["id"]
        opts += ["🚫 Не смогу работать", "🛡️ Могу администратором"]

        poll_msg = await bot.send_poll(
            state.admin_chat_id,
            header,
            opts,
            is_anonymous=False,
            allows_multiple_answers=True,
        )
        state.responses[poll_msg.poll.id] = {
            "deals":          {d["id"]: [] for d in chunk},
            "admin_available": [],
            "not_available":   [],
            "deal_indices":    idx_map,
        }

    # отправили опросы
    ok = await message.answer("✅ Опросы отправлены.")
    state.messages_to_delete.setdefault(uid, []).extend(
        [message.message_id, ok.message_id]
    )

    # смена кнопки «📋» → «📊»
    await _refresh_menu(uid)

    # отчёт руководителю
    await _send_leader_report(uid)

    # планируем очистку
    asyncio.get_event_loop().call_later(
        settings.POLL_DURATION_HOURS * 3600,
        lambda: asyncio.create_task(clear_poll_data(uid))
    )


async def _send_leader_report(leader_id: int) -> None:
    bot  = Bot.get_current()
    text = await generate_poll_report()
    sent = await bot.send_message(
        leader_id,
        text,
        parse_mode="Markdown",
        reply_markup=state.distribution_keyboard,
    )
    state.personal_report_message_id   = sent.message_id
    state.last_user_messages[leader_id] = [sent]
    state.messages_to_delete.setdefault(leader_id, []).append(sent.message_id)


@router.message(lambda m: m.text and m.text.strip() in {"📊 Отчёт по опросу", "📈 Отчёт по опросу"})
async def poll_report_handler(message: types.Message) -> None:
    uid = message.from_user.id
    ui  = await get_user_info(uid) or {}
    if ui.get("role") not in settings.ACCESS["poll"]:
        await message.answer("⛔ Нет доступа.", reply_markup=await get_main_menu(uid))
        await _delete_trigger(message)
        return
    if not state.coordination_cycle_active:
        await message.answer("⚠️ Активных опросов нет.", reply_markup=await get_main_menu(uid))
        await _delete_trigger(message)
        return

    await delete_previous_private_messages(uid)
    bot = Bot.get_current()
    dash = await bot.send_message(
        uid,
        await generate_poll_report(),
        parse_mode="Markdown",
        reply_markup=state.distribution_keyboard,
    )
    state.last_user_messages[uid]     = [dash]
    state.personal_report_message_id = dash.message_id
    state.messages_to_delete.setdefault(uid, []).append(dash.message_id)

    await _refresh_menu(uid)
    await _delete_trigger(message)


async def generate_poll_report() -> str:
    if not state.current_poll_deals or not state.responses:
        return "⚠️ Нет активных опросов."

    keyboard: List[List[InlineKeyboardButton]] = []
    poll_admins = [
        u
        for pdata in state.responses.values()
        for u in pdata["admin_available"]
        if await _is_admin_role(u["user_id"])
    ]

    for deal in state.current_poll_deals:
        did      = deal["id"]
        game     = deal["game_name"]
        date_str = deal["event_datetime"].strftime("%d.%m")
        cfg      = _role_cfg(game)
        need_main   = cfg["main_leaders"]
        need_assist = cfg["assistants"]
        need_admin  = 1 if (deal.get("package") or "").strip().lower() in {
            "стандарт", "стандарт+", "премиум", "vip", "вип"
        } else 0

        respondents: Dict[int, Dict] = {}
        for pdata in state.responses.values():
            for u in pdata["deals"].get(did, []):
                respondents[u["user_id"]] = u
        for u in poll_admins:
            respondents[u["user_id"]] = {**respondents.get(u["user_id"], {}), **u}

        have_main = have_assist = have_admin = 0
        for u in respondents.values():
            if u.get("is_admin_eligible") and have_admin < need_admin:
                have_admin += 1
                continue
            status = await get_user_status_from_svetofor(u["user_id"], game)
            if status == "green":
                if have_main < need_main:
                    have_main += 1
                elif have_assist < need_assist:
                    have_assist += 1
            elif status == "yellow" and have_assist < need_assist:
                have_assist += 1

        ready = (
            have_main >= need_main
            and have_assist >= need_assist
            and have_admin >= need_admin
        )
        icon = "✅" if ready else "❌"
        keyboard.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} {game} — {date_str}",
                    callback_data=f"show_deal_{did}",
                )
            ]
        )

    state.distribution_keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard)
    return "📊 *Опрос создан. Выберите игру:*"


@router.poll_answer()
async def handle_poll_answer(event: types.PollAnswer) -> None:
    uid, poll_id, chosen = event.user.id, event.poll_id, event.option_ids
    if poll_id not in state.responses:
        return
    data = state.responses[poll_id]

    # убрать старые
    for lst in data["deals"].values():
        lst[:] = [u for u in lst if u["user_id"] != uid]
    data["not_available"][:]   = [
        u for u in data["not_available"] if u["user_id"] != uid
    ]
    data["admin_available"][:] = [
        u for u in data["admin_available"] if u["user_id"] != uid
    ]

    ui = await get_user_info(uid) or {}
    base = {
        "user_id": uid,
        "first_name": ui.get("first_name", ""),
        "last_name_initial": ui.get("last_name_initial", ""),
        "is_admin_eligible": False,
    }

    num_games   = len(data["deal_indices"])
    refresh_all = False
    impacted: Set[int] = set()

    for idx in chosen:
        if idx < num_games:
            did = data["deal_indices"][idx]
            data["deals"][did].append(base.copy())
            impacted.add(did)
        elif idx == num_games:
            data["not_available"].append(base.copy())
        elif idx == num_games + 1:
            adm = base.copy()
            adm["is_admin_eligible"] = True
            data["admin_available"].append(adm)
            refresh_all = True

    if refresh_all:
        for lst in data["deals"].values():
            lst[:] = [u for u in lst if not u.get("is_admin_eligible")]

    await _sync_leader_report()
    await _refresh_detail_views(impacted, refresh_all)


async def _sync_leader_report() -> None:
    bot      = Bot.get_current()
    new_text = await generate_poll_report()
    leader   = state.current_poll_leader
    msg_id   = state.personal_report_message_id
    if not leader:
        return
    try:
        await bot.edit_message_text(
            new_text,
            chat_id=leader,
            message_id=msg_id,
            parse_mode="Markdown",
            reply_markup=state.distribution_keyboard,
        )
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        try:
            await bot.delete_message(chat_id=leader, message_id=msg_id)
        except Exception:
            pass
        sent = await bot.send_message(
            leader,
            new_text,
            parse_mode="Markdown",
            reply_markup=state.distribution_keyboard,
        )
        state.personal_report_message_id   = sent.message_id
        state.last_user_messages[leader]    = [sent]


async def _refresh_detail_views(impacted: Set[int], refresh_all: bool) -> None:
    if refresh_all:
        impacted = {d["id"] for d in state.current_poll_deals}
    tasks = [
        refresh_deal_details(u_id, d_id)
        for (u_id, d_id) in list(state.detail_blocks)
        if d_id in impacted
    ]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


@router.callback_query(lambda c: c.data == "distribute_leaders")
async def distribute_leaders_handler(callback: types.CallbackQuery) -> None:
    uid = callback.from_user.id
    if not state.distribution_cache:
        await callback.answer("⚠️ Распределение ещё не сформировано.", show_alert=True)
        return

    report = ["📋 *Предварительное распределение:*"]
    for deal in state.current_poll_deals:
        did  = str(deal["id"])
        dist = state.distribution_cache.get(did, {})
        if not dist:
            continue
        report.append(f"\n🎯 *{deal['name']}* — {deal['event_datetime'].strftime('%d.%m')}")
        for role, tag in dist.items():
            emoji = {"lead": "🧭", "assistant": "🛟", "admin": "🛡️"}.get(
                role.split("1")[0], "👤"
            )
            txt   = tag or "_не назначен_"
            report.append(f"{emoji} {role.capitalize()}: {txt}")

    await callback.message.answer("\n".join(report), parse_mode="Markdown")
    await callback.answer()


@router.callback_query(lambda c: c.data == "save_distribution")
async def save_distribution_to_amocrm_handler(callback: types.CallbackQuery) -> None:
    if not state.distribution_cache:
        await callback.answer("⚠️ Сначала утвердите распределение.", show_alert=True)
        return

    updates: List[str] = []
    for deal in state.current_poll_deals:
        did_str = str(deal["id"])
        dist    = state.distribution_cache.get(did_str, {})
        tags    = [t for t in dist.values() if t]
        if not tags:
            updates.append(f"⚠️ {deal['name']} — нет тегов")
            continue
        success = await update_amocrm_tags(deal_id=deal["id"], tags=tags)
        updates.append(f"{'✅' if success else '❌'} {deal['name']}")

    await callback.message.answer(
        "💾 *Сохранение в AmoCRM:*\n" + "\n".join(updates),
        parse_mode="Markdown",
    )
    await callback.answer()


async def clear_poll_data(user_id: int) -> None:
    state.responses.clear()
    state.current_poll_deals.clear()
    state.distribution_cache.clear()
    state.personal_report_message_id   = None
    state.current_poll_leader           = None
    state.coordination_cycle_active     = False
    state.distribution_keyboard         = None
    state.current_event_period          = None
    state.messages_to_delete.pop(user_id, None)
    logger.info("[polls] Data cleared for %d", user_id)
