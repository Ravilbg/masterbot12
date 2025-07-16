# handlers/poll_details.py
# ─────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from aiogram import Bot, Router, types
from aiogram.types import InlineKeyboardButton, User, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.config import settings
from core.db import get_user_info
from core.state import state
from core.utils import delete_previous_private_messages, truncate
from services.gsheets import get_user_status_from_svetofor
from services.cache import redis_cache

logger = logging.getLogger(__name__)
router = Router()


def _tag_uid(tag: str) -> Optional[int]:
    """Извлекает user_id из строки 'Имя|user_id', иначе None."""
    if "|" in tag:
        try:
            return int(tag.split("|")[-1])
        except ValueError:
            return None
    return None


def _role_cfg(game_name: str) -> Dict[str, int]:
    """
    Возвращает настройки ролей для игры без учёта регистра.
    """
    nm = game_name.strip().lower()
    for key, cfg in settings.GAME_ROLE_MAPPING.items():
        if key.strip().lower() == nm:
            return cfg
    # fallback, если не найдётся
    return {"main_leaders": 1, "assistants": 0}


async def _get_cached_status(user_id: int, game: str) -> str:
    """
    Возвращает статус из "Светофора", кешируя на CACHE_TTL_SECONDS.
    """
    key = f"svetofor:{user_id}:{game}"
    return await redis_cache.remember(
        key,
        ex=settings.CACHE_TTL_SECONDS,
        fetcher=lambda: get_user_status_from_svetofor(user_id, game)
    )


@router.callback_query(lambda c: c.data.startswith("show_deal_"))
async def show_deal_callback_handler(callback: CallbackQuery) -> None:
    """
    Показывает детальную карточку игры:
      • требования из GAME_ROLE_MAPPING (через локальный _role_cfg);
      • greedy-расклад по ролям;
      • альтернатива, стажёры, кнопка «Назад».
    """
    uid = callback.from_user.id
    deal_id = int(callback.data.split("_")[-1])
    is_refresh = callback.id == "redraw"

    deal = next((d for d in state.current_poll_deals if d["id"] == deal_id), None)
    if not deal:
        if not is_refresh:
            await callback.answer("⚠️ Игра не найдена.", show_alert=True)
        return

    if not is_refresh:
        await delete_previous_private_messages(uid)
        try:
            await callback.message.delete()
        except Exception:
            logger.exception("Не удалось удалить старое сообщение детализации")

    # Шапка
    game = deal["game_name"]
    date_s = deal["event_datetime"].strftime("%d.%m.%Y")
    time_s = deal.get("event_time", "—")
    pkg_raw = (deal.get("package") or "—").strip().lower()
    players = truncate(deal.get("players") or "—", 40)
    pkg_icon = {
        "компакт": "🎒",
        "стандарт": "📦",
        "стандарт+": "📦➕",
        "премиум": "💎",
        "vip": "👑",
        "вип": "👑",
    }.get(pkg_raw, "🎁")

    bot = Bot.get_current()
    msgs: List[types.Message] = [
        await bot.send_message(
            uid,
            (
                f"🎮 *{game}*\n"
                f"📅 {date_s} · 🕒 {time_s}\n"
                f"📦 *Пакет:* {pkg_icon} {pkg_raw.capitalize()}\n"
                f"👥 *Игроки:* {players}"
            ),
            parse_mode="Markdown",
        )
    ]

    # Роли
    cfg = _role_cfg(game)
    need = {
        "main": cfg["main_leaders"],
        "assist": cfg["assistants"],
        "admin": 1 if pkg_raw in {"стандарт", "стандарт+", "премиум", "vip", "вип"} else 0,
    }
    dist = state.distribution_cache.setdefault(str(deal_id), {})

    # Сбор респондентов
    respondents: Dict[int, Dict] = {}
    for pdata in state.responses.values():
        for u in pdata["deals"].get(deal_id, []):
            respondents[u["user_id"]] = u
        for adm in pdata["admin_available"]:
            respondents[adm["user_id"]] = {**respondents.get(adm["user_id"], {}), **adm}

    async def fits(u: Dict, role: str) -> bool:
        if role == "admin":
            return u.get("is_admin_eligible", False)
        status = await _get_cached_status(u["user_id"], game)
        return (status == "green") if role == "main" else (status in {"green", "yellow"})

    chosen_global: set[int] = set()

    async def render_section(title: str, role: str, icon: str) -> None:
        chosen: List[Tuple[Dict, str]] = []

        # Закреплённые из dist
        if role == "admin":
            tag = dist.get("admin", "")
            uid_tag = _tag_uid(tag) or 0
            if uid_tag in respondents:
                chosen.append((respondents[uid_tag], "🛡️"))
                chosen_global.add(uid_tag)
        else:
            pref = "lead" if role == "main" else "assistant"
            for i in range(1, need[role] + 1):
                tag = dist.get(f"{pref}{i}", "")
                uid_tag = _tag_uid(tag) or 0
                if uid_tag in respondents:
                    stat = await _get_cached_status(uid_tag, game)
                    mark = "🟢" if stat == "green" else "🟡"
                    chosen.append((respondents[uid_tag], mark))
                    chosen_global.add(uid_tag)

        # Greedy-назначение
        for u in respondents.values():
            if len(chosen) >= need[role]:
                break
            if u["user_id"] in chosen_global:
                continue
            if await fits(u, role):
                stat = await _get_cached_status(u["user_id"], game)
                mark = "🛡️" if role == "admin" else ("🟢" if stat == "green" else "🟡")
                chosen.append((u, mark))
                chosen_global.add(u["user_id"])

        # Вывод секции
        ready = len(chosen) >= need[role]
        header = f"───── {icon} *{title.upper()}* ─────\n{'✅' if ready else '❌'} {len(chosen)}/{need[role]}"
        lines = [header] + [f"– {u['first_name']} {u.get('last_name_initial','')} {m}" for u, m in chosen]
        msgs.append(await bot.send_message(uid, "\n".join(lines), parse_mode="Markdown"))

        # Альтернативы
        alts = [u for u in respondents.values() if u["user_id"] not in chosen_global and await fits(u, role)]
        if alts:
            kb_alt = InlineKeyboardBuilder()
            for u in alts:
                stat = "" if role == "admin" else await _get_cached_status(u["user_id"], game)
                mark = "🛡️" if role == "admin" else ("🟢" if stat == "green" else "🟡")
                kb_alt.button(
                    text=f"{u['first_name']} {u.get('last_name_initial','')} {mark}",
                    callback_data=f"swap_{deal_id}_{role}_{u['user_id']}"
                )
            kb_alt.adjust(1)
            msgs.append(await bot.send_message(uid, "🔁 Альтернатива:", reply_markup=kb_alt.as_markup()))

    await render_section("Ведущие", "main", "🧭")
    await render_section("Помощники", "assist", "🛟")
    await render_section("Админ", "admin", "🛡️")

    # Стажёры
    interns = [u for u in respondents.values() if await _get_cached_status(u["user_id"], game) == "red"]
    if interns:
        block = ["───── 👷 *СТАЖЁРЫ* ─────"] + [
            f"– {u['first_name']} {u.get('last_name_initial','')} 🔴" for u in interns
        ]
        msgs.append(await bot.send_message(uid, "\n".join(block), parse_mode="Markdown"))

    # Назад
    kb_back = InlineKeyboardBuilder()
    kb_back.button(text="⬅️ Назад к списку", callback_data="back_to_games_list")
    kb_back.adjust(1)
    msgs.append(await bot.send_message(uid, "⬅️", reply_markup=kb_back.as_markup()))

    state.last_user_messages[uid] = msgs
    state.detail_blocks[(uid, deal_id)] = msgs

    try:
        await callback.answer()
    except Exception:
        bot = Bot.get_current()
        await callback.answer.as_(bot)()


async def refresh_deal_details(user_id: int, deal_id: int) -> None:
    """
    Удаляет старые сообщения детализации и заново вызывает show_deal_callback_handler.
    """
    old = state.detail_blocks.pop((user_id, deal_id), [])
    for m in old:
        try:
            await m.delete()
        except Exception:
            logger.exception("Не удалось удалить сообщение при refresh_deal_details")

    state.last_user_messages.pop(user_id, None)

    bot = Bot.get_current()
    dummy = CallbackQuery(
        id="redraw",
        from_user=User(id=user_id, is_bot=False, first_name=""),
        chat_instance="",
        message=None,
        data=f"show_deal_{deal_id}"
    )
    dummy.bot = bot
    await show_deal_callback_handler(dummy)


@router.callback_query(lambda c: c.data.startswith("swap_"))
async def assign_swap_handler(callback: types.CallbackQuery) -> None:
    """
    Swap-обмен: меняет выбранного пользователя на указанную роль.
    """
    _, deal_id_s, role_type, new_uid_s = callback.data.split("_", 3)
    deal_id, new_uid = int(deal_id_s), int(new_uid_s)

    deal = next((d for d in state.current_poll_deals if d["id"] == deal_id), None)
    if not deal:
        await callback.answer("⚠️ Игра не найдена.", show_alert=True)
        return

    dist = state.distribution_cache.setdefault(str(deal_id), {})
    cfg = _role_cfg(deal["game_name"])
    main_cnt, assist_cnt = cfg["main_leaders"], cfg["assistants"]

    # Снимаем старые
    for slot, tag in list(dist.items()):
        if _tag_uid(tag) == new_uid:
            dist[slot] = ""

    # Слоты
    if role_type == "admin":
        slots = ["admin"]
    elif role_type == "main":
        slots = [f"lead{i}" for i in range(1, main_cnt + 1)]
    else:
        slots = [f"assistant{i}" for i in range(1, assist_cnt + 1)]

    target = next((s for s in slots if not dist.get(s)), slots[0])
    info = await get_user_info(new_uid) or {}
    name = f"{info.get('first_name','')} {info.get('last_name_initial','')}".strip()
    suffix = ".Ад" if role_type == "admin" else ".1" if role_type == "main" else ".Пом"
    dist[target] = f"{name}{suffix}|{new_uid}"

    await callback.answer("✅ Обмен выполнен.")
    await refresh_deal_details(callback.from_user.id, deal_id)
