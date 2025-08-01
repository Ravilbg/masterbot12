# handlers/poll_details.py — detail-view игр + manual-switches
# ─────────────────────────────────────────────────────────────────────────────
"""
Реактивные карточки игр (detail-view) для цикла распределения.

Версия 12.97 · 2025-07-31
──────────────────────────────────────────────────────────────────────────────
• Карточка игры показывает состав команды, статусы, стажёров.
• Руководитель может: ✅ утвердить игру, ⏹️ остановить набор.
• Исправлен тайм-аут callback-query (instant ACK) и добавлены per-user locks.
• «Пылесос»: старые сообщения корректно удаляются или кладутся в
  state.messages_to_delete для фоновой уборки.
• _build_games_keyboard() сразу возвращает InlineKeyboardMarkup, чтобы
  не вызывать .as_markup() в нескольких местах.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Dict, List, Optional, Set, Tuple

from aiogram import Bot, Router, types
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    User,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder   # ← правильный импорт

from core.config import settings
from core.db import get_all_leader_uids, get_user_info
from core.state import state
from core.utils import truncate
from services.cache import redis_cache
from services.gsheets import get_user_status_from_svetofor

logger = logging.getLogger(__name__)
router = Router()

POLL_BACK      = "poll_back_to_games_list"          # callback-data кнопки «Назад»
_ADMIN_PKGS    = {"стандарт", "стандарт+", "премиум", "vip", "вип", "биглион"}
_OK_STATUSES   = {"green", "yellow"}

# ════════════════════════════════════════════════════════════════════
# 0.  Per-user async-lock (общее с handlers.games)
# ════════════════════════════════════════════════════════════════════
@asynccontextmanager
async def user_lock(uid: int):
    """Async-Lock для пользователя; предотвращает гонки при множественных redraw-ах."""
    lock = state.lock_for(uid)
    await lock.acquire()
    try:
        yield
    finally:
        lock.release()


# ════════════════════════════════════════════════════════════════════
# 1.  Вспомогательные функции
# ════════════════════════════════════════════════════════════════════
def _tag_uid(tag: str) -> Optional[int]:
    """Из тега «Имя|123» извлекает 123, иначе None."""
    try:
        return int(tag.rsplit("|", 1)[-1])
    except Exception:                       # noqa: BLE001
        return None


def _role_cfg(game_name: str) -> Dict[str, int]:
    """
    Конфиг ролей для *game_name* из settings.GAME_ROLE_MAPPING
    (регистр игнорируется). Если игра не найдена — 1 ведущий, 0 ассистентов.
    """
    norm = game_name.strip().lower()
    for key, cfg in settings.GAME_ROLE_MAPPING.items():
        if key.lower() == norm:
            return cfg
    return {"main_leaders": 1, "assistants": 0}


# ███ [1.3] Svetofor-cached status
# --------------------------------------------------------------------
async def _status_cached(uid: int, game: str) -> str:
    """
    Безопасно получает 'green' | 'yellow' | 'red' | '' из «Светофора».

    • Результат кешируется на *settings.CACHE_TTL_SECONDS*.  
    • Любая ошибка внутри fetcher() или в кеше не прерывает работу —  
      пишем warning и возвращаем ''.
    """
    key = f"sv:{uid}:{game}".lower()

    async def _fetch() -> str:
        try:
            return await get_user_status_from_svetofor(uid, game)
        except Exception as exc:            # noqa: BLE001
            logger.warning(
                "[details] Svetofor lookup failed for %d/%s: %s",
                uid, game, exc
            )
            return ""

    try:
        return await redis_cache.remember(
            key,
            ex=settings.CACHE_TTL_SECONDS,
            fetcher=_fetch,
        )
    except Exception as exc:                # noqa: BLE001
        logger.warning("[details] Cache error %s: %s", key, exc)
        return ""


def _build_games_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура со списком активных игр (без остановленных)."""
    kb = InlineKeyboardBuilder()
    for d in state.current_poll_deals:
        if d["id"] in state.deal_force_closed:
            continue
        kb.button(
            text=f"{d['game_name']} · {d['event_datetime']:%d.%m.%Y} · {d.get('event_time','—')}",
            callback_data=f"show_deal_{d['id']}",
        )
    kb.adjust(1)
    return kb.as_markup()


def _is_leader(uid: int) -> bool:
    """True, если *uid* — текущий лидер опроса."""
    return uid == state.current_poll_leader


async def _purge_msgs(uid: int, coll: Dict) -> None:
    """
    Удаляет сообщения пользователя из *coll*
    (state.detail_blocks или state.last_user_messages).
    """
    for key, lst in list(coll.items()):
        if isinstance(key, tuple) and key[0] != uid:
            continue
        if key == uid or (isinstance(key, tuple) and key[0] == uid):
            for m in lst:
                try:
                    await m.delete()
                except Exception:
                    state.messages_to_delete.setdefault(uid, []).append(m.message_id)
            coll.pop(key, None)


# ─────────────────────────────────────────────────────────────────────
async def _test() -> None:
    """Smoke-тест: функция должна отрабатывать без ошибок."""
    res = await _status_cached(0, "Fake Game")
    assert isinstance(res, str)
    print("_status_cached ✅", res or "''")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_test())


# ════════════════════════════════════════════════════════════════════
# 2.  Detail-view: основной callback-handler
# ════════════════════════════════════════════════════════════════════
@router.callback_query(lambda c: c.data.startswith("show_deal_"))
async def show_deal_callback_handler(callback: CallbackQuery) -> None:
    """
    Строит (или перерисовывает) карточку игры.

    • «Живые» callback’и моментально ACK-аются, чтобы Telegram не оборвал
      соединение по timeout’у.  
    • Для внутренних redraw-ов (callback.id == 'redraw') ACK не нужен.
    • В начале выводится DEBUG-сообщение для упрощения диагностики.
    """
    is_redraw = callback.id == "redraw"
    if not is_redraw:
        await callback.answer()                       # instant ACK

    uid     = callback.from_user.id
    deal_id = int(callback.data.split("_", 2)[-1])

    logger.debug("[details] show_deal uid=%d deal=%d redraw=%s",
                 uid, deal_id, is_redraw)

    # ────────────────────────────────────────────────────────────────
    #  per-user Lock исключает гонки redraw-ов
    # ────────────────────────────────────────────────────────────────
    async with user_lock(uid):

        # 0. Валидация цикла и сделки --------------------------------
        if state.force_closed:
            await Bot.get_current().send_message(uid, "⚠️ Цикл распределения завершён.")
            return

        deal = next((d for d in state.current_poll_deals if d["id"] == deal_id), None)
        if deal is None:
            await Bot.get_current().send_message(uid, "⚠️ Игра не найдена.")
            return

        # 1. «Пылесос» — удаляем старые сообщения --------------------
        await _purge_msgs(uid, state.last_user_messages)
        await _purge_msgs(uid, state.detail_blocks)

        bot   = Bot.get_current()
        msgs: List[types.Message] = []

        # 2.1 Заголовок ---------------------------------------------
        g_name   = deal["game_name"]
        date_s   = deal["event_datetime"].strftime("%d.%m.%Y")
        time_s   = deal.get("event_time", "—")
        players  = truncate(deal.get("players") or "—", 40)
        pkg_raw  = (deal.get("package") or "—").strip().lower()
        pkg_icon = {
            "компакт": "🎒", "стандарт": "📦", "стандарт+": "📦➕",
            "премиум": "💎", "vip": "👑", "вип": "👑",
        }.get(pkg_raw, "🎁")

        header = (
            f"🎮 *{g_name}*\n"
            f"📅 {date_s} · 🕒 {time_s}\n"
            f"📦 *Пакет:* {pkg_icon} {pkg_raw.capitalize()}\n"
            f"👥 *Игроки:* {players}"
        )
        msgs.append(await bot.send_message(uid, header, parse_mode="Markdown"))

        # 2.2 Подтверждения «+» --------------------------------------
        if state.manual_confirm_requested:
            confirmed = len(state.confirmed_users)
            total     = len(await get_all_leader_uids())
            msgs.append(
                await bot.send_message(
                    uid,
                    f"👥 *Подтвердили участие:* {confirmed}/{total}",
                    parse_mode="Markdown",
                )
            )

        # 2.3 Состав команды ----------------------------------------
        cfg  = _role_cfg(g_name)
        need = {
            "main":   cfg["main_leaders"],
            "assist": cfg["assistants"],
            "admin":  int(pkg_raw in _ADMIN_PKGS),
        }
        dist = state.distribution_cache.setdefault(str(deal_id), {})

        # собираем откликнувшихся
        respondents: Dict[int, Dict] = {}
        for pdata in state.responses.values():
            # отклики на эту игру
            for u in pdata["deals"].get(deal_id, []):
                respondents[u["user_id"]] = u
            # лидеры, доступные как админы
            if deal_id in pdata.get("deal_indices", {}).values():
                for adm in pdata["admin_available"]:
                    respondents[adm["user_id"]] = {
                        **respondents.get(adm["user_id"], {}),
                        **adm,
                    }

        chosen_global: Set[int] = set()          # предотвращаем дубли

        async def _fits(user: Dict, role: str) -> bool:
            """Проверяет, подходит ли пользователь под роль и статус."""
            if role == "admin":
                return user.get("is_admin_eligible", False)
            st = await _status_cached(user["user_id"], g_name)
            return st == "green" if role == "main" else st in _OK_STATUSES

        async def _render(role: str, title: str, icon: str) -> None:
            """Рисует блок роли + список альтернатив."""
            chosen: List[Tuple[Dict, str]] = []

            # 1) ручные назначения -----------------------------------
            if role == "admin":
                tag_uid = _tag_uid(dist.get("admin", ""))
                if tag_uid and tag_uid in respondents:
                    chosen.append((respondents[tag_uid], "🛡️"))
                    chosen_global.add(tag_uid)
            else:
                prefix = "lead" if role == "main" else "assistant"
                for i in range(1, need[role] + 1):
                    tag_uid = _tag_uid(dist.get(f"{prefix}{i}", ""))
                    if tag_uid and tag_uid in respondents:
                        st = await _status_cached(tag_uid, g_name)
                        chosen.append((respondents[tag_uid], "🟢" if st == "green" else "🟡"))
                        chosen_global.add(tag_uid)

            # 2) автоподбор -----------------------------------------
            for u in respondents.values():
                if len(chosen) >= need[role] or u["user_id"] in chosen_global:
                    continue
                if await _fits(u, role):
                    st   = await _status_cached(u["user_id"], g_name)
                    mark = "🛡️" if role == "admin" else ("🟢" if st == "green" else "🟡")
                    chosen.append((u, mark))
                    chosen_global.add(u["user_id"])

            ready = len(chosen) >= need[role]
            block = [
                f"───── {icon} *{title.upper()}* ─────",
                f"{'✅' if ready else '❌'} {len(chosen)}/{need[role]}",
                *[f"– {u['first_name']} {u.get('last_name_initial','')} {m}" for u, m in chosen],
            ]
            msgs.append(await bot.send_message(uid, "\n".join(block), parse_mode="Markdown"))

            # 3) альтернативы ---------------------------------------
            alts = [
                u for u in respondents.values()
                if u["user_id"] not in chosen_global and await _fits(u, role)
            ]
            if alts:
                kb_alt = InlineKeyboardBuilder()
                for u in alts:
                    st = "" if role == "admin" else await _status_cached(u["user_id"], g_name)
                    mark = "🛡️" if role == "admin" else ("🟢" if st == "green" else "🟡")
                    kb_alt.button(
                        text=f"{u['first_name']} {u.get('last_name_initial','')} {mark}",
                        callback_data=f"swap_{deal_id}_{role}_{u['user_id']}",
                    )
                kb_alt.adjust(1)
                msgs.append(
                    await bot.send_message(uid, "🔁 Альтернатива:", reply_markup=kb_alt.as_markup())
                )

        # рисуем блоки ролей
        await _render("main",   "Ведущие",   "🧭")
        await _render("assist", "Помощники", "🛟")
        await _render("admin",  "Админ",     "🛡️")

        # 2.4 Стажёры ----------------------------------------------
        red_users = [
            u for u in respondents.values()
            if u["user_id"] not in chosen_global
            and await _status_cached(u["user_id"], g_name) == "red"
        ]
        if red_users:
            block = ["───── 👷 *СТАЖЁРЫ* ─────"] + [
                f"– {u['first_name']} {u.get('last_name_initial','')} 🔴" for u in red_users
            ]
            msgs.append(await bot.send_message(uid, "\n".join(block), parse_mode="Markdown"))

        # 2.5 Стоп-набор -------------------------------------------
        if deal_id in state.deal_force_closed:
            msgs.append(
                await bot.send_message(uid, "⚠️ Набор на эту игру остановлен.", parse_mode="Markdown")
            )

        # 2.6 Кнопки лидера ----------------------------------------
        if _is_leader(uid) and deal_id not in state.deal_force_closed:
            kb_mgr = InlineKeyboardBuilder()
            kb_mgr.button(text="✅ Утвердить игру", callback_data=f"poll_approve_{deal_id}")
            kb_mgr.button(text="⏹️ Стоп набор",    callback_data=f"poll_stop_{deal_id}")
            kb_mgr.adjust(1)
            msgs.append(
                await bot.send_message(uid, "🛠 Управление:", reply_markup=kb_mgr.as_markup())
            )

        # 2.7 «Назад к списку» -------------------------------------
        kb_back = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к списку", callback_data=POLL_BACK)]]
        )
        msgs.append(await bot.send_message(uid, "\u2060", reply_markup=kb_back))

        # 3. Сохраняем detail-view для последующего удаления -------
        state.detail_blocks[(uid, deal_id)] = msgs




# ════════════════════════════════════════════════════════════════════
# 3.  API: «быстрый» refresh detail-view   (FIX dead-lock)
# ════════════════════════════════════════════════════════════════════
async def refresh_deal_details(user_id: int, deal_id: int) -> None:
    """
    Перерисовывает карточку игры у *user_id*.

    ▸ Сначала, под user-Lock, удаляем старые сообщения.
    ▸ Затем отпускаем Lock и запускаем show_deal_callback_handler()
      – так избегается рекурсивная блокировка.
    """
    lock = state.lock_for(user_id)

    # ── stage 1: vacuum под замком ──────────────────────────────────
    async with lock:
        await _purge_msgs(user_id, state.detail_blocks)
        logger.debug("[refresh] purged old detail-view for %d/%d", user_id, deal_id)

    # ── stage 2: перерисовка без замка (show_deal сам возьмёт lock) ─
    dummy = CallbackQuery(
        id="redraw",
        from_user=User(id=user_id, is_bot=False, first_name=""),
        chat_instance="",
        message=None,
        data=f"show_deal_{deal_id}",
    ).as_(Bot.get_current())

    try:
        await show_deal_callback_handler(dummy)
    except Exception as exc:            # защита от скрытых ошибок
        logger.exception("[refresh] redraw FAILED for %d/%d: %s",
                         user_id, deal_id, exc)


# ════════════════════════════════════════════════════════════════════
# 4.  HANDLERS: approve / stop / swap / back
# ════════════════════════════════════════════════════════════════════
@router.callback_query(lambda c: c.data.startswith("poll_approve_"))
async def poll_approve_game_handler(callback: CallbackQuery) -> None:
    """Лидер утвердил состав игры."""
    await callback.answer("Игра утверждена.")            # мгновенный ACK
    deal_id = int(callback.data.rsplit("_", 1)[-1])

    if deal_id in state.deal_force_closed:
        await callback.answer("Набор остановлен.", show_alert=True)
        return

    from handlers.polls_lifecycle import _sync_leader_report
    await _sync_leader_report()

    # безопасная перерисовка detail-view
    try:
        await refresh_deal_details(callback.from_user.id, deal_id)
    except Exception as exc:
        logger.exception("[details] refresh after approve failed: %s", exc)

    logger.info("[details] deal %d approved by %d", deal_id, callback.from_user.id)


@router.callback_query(lambda c: c.data.startswith("poll_stop_"))
async def poll_stop_game_handler(callback: CallbackQuery) -> None:
    """Лидер остановил набор на игру."""
    await callback.answer("Набор остановлен.")           # мгновенный ACK
    deal_id = int(callback.data.rsplit("_", 1)[-1])

    state.deal_force_closed.add(deal_id)
    for pdata in state.responses.values():                # чистим отклики
        pdata["deals"].pop(deal_id, None)

    from handlers.polls_lifecycle import _sync_leader_report
    await _sync_leader_report()

    try:
        await refresh_deal_details(callback.from_user.id, deal_id)
    except Exception as exc:
        logger.exception("[details] refresh after stop failed: %s", exc)

    logger.info("[details] deal %d force-closed by %d", deal_id, callback.from_user.id)


@router.callback_query(lambda c: c.data.startswith("swap_"))
async def assign_swap_handler(callback: CallbackQuery) -> None:
    """Переназначение ведущего / помощника / админа из выпадающего списка."""
    await callback.answer("✅ Назначено.")                 # ACK сразу

    _, deal_id_s, role_key, new_uid_s = callback.data.split("_", 3)
    deal_id, new_uid = int(deal_id_s), int(new_uid_s)

    deal = next((d for d in state.current_poll_deals if d["id"] == deal_id), None)
    if deal is None or deal_id in state.deal_force_closed:
        await callback.answer("Набор остановлен.", show_alert=True)
        return

    # — обновляем distribution_cache —
    dist = state.distribution_cache.setdefault(str(deal_id), {})
    cfg  = _role_cfg(deal["game_name"])
    slot_pool = {
        "admin":  ["admin"],
        "main":   [f"lead{i}"      for i in range(1, cfg["main_leaders"] + 1)],
        "assist": [f"assistant{i}" for i in range(1, cfg["assistants"]   + 1)],
    }[role_key]

    # снимаем старые назначения этого пользователя
    for s, tag in list(dist.items()):
        if _tag_uid(tag) == new_uid:
            dist[s] = ""

    target_slot = next((s for s in slot_pool if not dist.get(s)), slot_pool[0])

    info   = await get_user_info(new_uid) or {}
    name   = f"{info.get('first_name','')} {info.get('last_name_initial','')}".strip()
    suffix = ".Адм" if role_key == "admin" else ".1" if role_key == "main" else ".2"
    dist[target_slot] = f"{name}{suffix}|{new_uid}"

    # перерисовываем карточку безопасно
    try:
        await refresh_deal_details(callback.from_user.id, deal_id)
    except Exception as exc:
        logger.exception("[details] refresh after swap failed: %s", exc)


@router.callback_query(lambda c: c.data == POLL_BACK)
async def back_to_games_list_handler(callback: CallbackQuery) -> None:
    """Возврат из карточки к списку игр / отчёту."""
    await callback.answer()                                # ACK

    uid = callback.from_user.id
    # удаляем ВСЕ старые личные сообщения (detail + меню)
    await _purge_msgs(uid, state.detail_blocks)
    await _purge_msgs(uid, state.last_user_messages)

    # пытаемся показать актуальный отчёт лидеру
    try:
        from handlers.polls_lifecycle import generate_poll_report
        text = await generate_poll_report()
        kb   = state.distribution_keyboard or _build_games_keyboard()
    except Exception:
        logger.exception("[details] fallback list build")
        if not state.current_poll_deals:
            text, kb = "ℹ️ Сейчас игр нет.", None
        else:
            visible = [d for d in state.current_poll_deals if d["id"] not in state.deal_force_closed]
            lines   = ["*Список игр:*"] + [
                f"• {d['game_name']} · {d['event_datetime']:%d.%m.%Y} · {d.get('event_time','—')}"
                for d in visible
            ]
            text, kb = "\n".join(lines), _build_games_keyboard()

    msg = await Bot.get_current().send_message(uid, text, parse_mode="Markdown", reply_markup=kb)
    state.last_user_messages[uid] = [msg]
