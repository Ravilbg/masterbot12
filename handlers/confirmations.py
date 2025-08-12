# handlers/confirmations.py
# ─────────────────────────────────────────────────────────────────────────────
"""
Подтверждения участия ведущими и дашборд «🎲 Мои игры».

Версия v14.8 · 2025-08-10
──────────────────────────────────────────────────────────────────────────────
• Источник правды — detail-кэш (handlers/poll_details.refresh_deal_details).
• «🎲 Мои игры» показывает сделки в статусах «Бронь» и «Завершение сделки»,
  где пользователь утверждён на роль (state.locked_distribution / assigned_index),
  либо назначен legacy через team_leads, либо уже имеет тег подтверждения в CRM.
• Кнопка «✅ Подтвердить» ставит тег в AmoCRM и шлёт заметку в общий чат.
• Когда все обязательные роли подтверждены — статус меняется на
  «Завершение сделки», игра выходит из цикла опроса.
• Все async-вызовы — awaited, подробное логирование, устойчивость к API.
"""

from __future__ import annotations

# ███ [0] IMPORTS
# --------------------------------------------------------------------
from __future__ import annotations

import asyncio
import logging
import contextlib
from typing import Any, Dict, List, Optional, Set

from aiogram import Bot, Router, types
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder  # если не используется — не мешает

from core.config import settings
from core.db import get_user_info  # синхронная утилита профиля
from core.state import state
from core.utils import delete_previous_private_messages, truncate

# ── AmoCRM API (безопасный импорт под разные версии клиента) ─────────
try:
    from services.amocrm import (  # type: ignore
        update_amocrm_tags,
        update_deal_status,
        get_amocrm_deals,
    )
except Exception as e:  # pragma: no cover
    raise RuntimeError("services.amocrm отсутствует или несовместим") from e

# ── GAME_ROLE_MAPPING из gsheets (для требований по ролям) ───────────
try:
    from services.gsheets import GAME_ROLE_MAPPING  # словарь требуемых ролей
except Exception:
    GAME_ROLE_MAPPING = {}

# ── детали сделки — единый UI и источник правды по составу ──────────
try:
    from handlers.poll_details import refresh_deal_details  # истина состава
except Exception as e:  # pragma: no cover
    raise RuntimeError("handlers.poll_details.refresh_deal_details не найден") from e

# корректный импорт меню (модуль handlers.menu отсутствует)
try:
    from core.menu import get_main_menu  # type: ignore
except ModuleNotFoundError:
    try:
        from menu import get_main_menu  # type: ignore
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "get_main_menu не найден: ожидается core/menu.py или menu.py. "
            "Обнови путь импорта в handlers/confirmations.py."
        ) from e

logger = logging.getLogger(__name__)
router = Router(name="confirmations")

# единая точка для групповых уведомлений
ADMIN_CHAT_ID = (
    getattr(settings, "POLLS_CHAT_ID", None)
    or getattr(settings, "LEADERS_CHAT_ID", None)
    or getattr(settings, "ADMIN_CHAT_ID", None)
)

# ███ [0.1] CALLBACK PREFIXES
# --------------------------------------------------------------------
# Формат callback_data для подтверждения участия:
#   confirm_role_<deal_id>_<role>
# где role ∈ {"main","assist","admin"}; uid берём из callback.from_user.id
CONFIRM_PREFIX = "confirm_role_"

def build_confirm_cb(deal_id: int | str, role: str) -> str:
    return f"{CONFIRM_PREFIX}{int(deal_id)}_{role}"

# ════════════════════════════════════════════════════════════════════
# [1.0] УТИЛИТЫ DETAIL/STATE
# ════════════════════════════════════════════════════════════════════
def _short_name_from_profile(uid: int) -> str:
    """Возвращает «Имя Ф.» из БД/состояния; если данных нет — str(uid)."""
    try:
        ui = get_user_info(uid) or {}
    except Exception:
        ui = {}
    first = (ui.get("first_name") or "").strip()
    last_i = (ui.get("last_name_initial") or ui.get("last_name_i") or "").strip()
    if first or last_i:
        return f"{first} {last_i}".strip()
    # fallback на state.users
    u = (state.users or {}).get(uid) or {}
    first = (u.get("first_name") or "").strip()
    last_i = (u.get("last_name_initial") or "").strip()
    return f"{first} {last_i}".strip() or str(uid)


def _role_key_alias(k: str) -> str:
    """Нормализует ключ роли из деталей в {'main','assist','admin','trainee'}."""
    k = (k or "").lower()
    if k == "admin" or "admin" in k:
        return "admin"
    if k == "trainee" or "intern" in k or "стаж" in k:
        return "trainee"
    if k.startswith("assist"):
        return "assist"
    if k.startswith("lead") or k == "main":
        return "main"
    return k


def _to_uid_list(v: Any) -> List[int]:
    """Переводит значение из деталей в список uid (int). Поддерживает int, 'Имя|uid', списки."""
    out: List[int] = []
    if v is None:
        return out
    if isinstance(v, int):
        return [v]
    if isinstance(v, str):
        s = v.strip()
        if "|" in s:
            try:
                out.append(int(s.rsplit("|", 1)[-1]))
            except ValueError:
                pass
        else:
            try:
                out.append(int(s))
            except ValueError:
                pass
        return out
    if isinstance(v, (list, tuple, set)):
        for x in v:
            out.extend(_to_uid_list(x))
    return out


def _extract_user_roles_from_details(details: Dict, uid: int) -> Set[str]:
    """
    Возвращает набор ролей пользователя в деталях сделки.
    Поддерживает форматы:
      {'team': {'main': [uids], 'assist': [uids], 'admin': [uids], 'trainees': [uids]}}
      {'roles': {'lead1':[...], 'assistant1':[...], 'admin': ...}}
    """
    roles_set: Set[str] = set()
    team = (details or {}).get("team")
    roles = (details or {}).get("roles")

    source = team if isinstance(team, dict) else roles if isinstance(roles, dict) else {}
    for k, v in source.items():
        alias = _role_key_alias(k)
        if alias not in {"main", "assist", "admin", "trainee"}:
            continue
        uids = set(_to_uid_list(v))
        if uid in uids:
            roles_set.add(alias)
    return roles_set


def _required_counts(game_name: str) -> Dict[str, int]:
    """
    Возвращает требуемые количества по ролям по имени игры.
    Поддерживает разные ключи в GAME_ROLE_MAPPING.
    """
    req = GAME_ROLE_MAPPING.get(game_name, {}) or {}
    return {
        "main": int(req.get("main_leaders", req.get("main", 1))),
        "assist": int(req.get("assistants", req.get("assist", 0))),
        "admin": int(req.get("admins", req.get("admin", 0))),
    }


def _role_suffix(role: str) -> str:
    """
    Возвращает суффикс к тегу в AmoCRM для данной роли.
    Принятый стандарт:
      main   → ".1"
      assist → ".2"
      admin  → ".Ад"
      trainee→ ".Стаж"
    """
    return {"main": ".1", "assist": ".2", "admin": ".Ад", "trainee": ".Стаж"}.get(role, "")


def _tags_from_details(details: Dict) -> Set[str]:
    """Возвращает множество имён тегов из деталей, если есть."""
    tags = (details or {}).get("tags") or (details or {}).get("deal", {}).get("tags") or []
    return {t.get("name") for t in tags if isinstance(t, dict) and t.get("name")}


async def _is_full_confirmation(details: Dict, deal_id: int) -> bool:
    """
    Проверяет, все ли обязательные роли подтверждены.
    1) Если есть details['confirmed'] — используем его напрямую.
    2) Иначе сверяем теги фактически в CRM против назначенных по state.locked_distribution.
    """
    game_name = (details or {}).get("game_name") or (details or {}).get("title") or ""
    need = _required_counts(game_name)

    # 1) прямые confirmed из деталей, если движок их считает
    confirmed = (details or {}).get("confirmed", {})
    if isinstance(confirmed, dict):
        ok_main = len(confirmed.get("main", []) or []) >= need["main"]
        ok_assist = len(confirmed.get("assist", []) or []) >= need["assist"]
        ok_admin = len(confirmed.get("admin", []) or []) >= need["admin"]
        if ok_main and ok_assist and ok_admin:
            return True

    # 2) проверка по тегам CRM
    #    строим набор обязательных тегов по зафиксированному составу и сравниваем
    locked = (state.locked_distribution or {}).get(deal_id) or {}
    must: Set[str] = set()
    for kind, suf in (("main", ".1"), ("assist", ".2"), ("admin", ".Ад")):
        uids = set(map(int, locked.get(kind, []) or []))
        # если требований 0 — пропускаем
        if need.get(kind, 0) <= 0:
            continue
        for u in uids:
            base = _short_name_from_profile(u)
            if base:
                must.add(f"{base}{suf}")

    # фактические теги
    tags: Set[str] = set()
    try:
        # пробуем взять теги из уже полученных деталей
        tags = _tags_from_details(details)
        if not tags:
            # либо запросить сделку напрямую
            deals = await get_amocrm_deals(ids=[int(deal_id)])  # допускается фильтр по ids
            if deals:
                tags = {t.get("name") for t in deals[0].get("tags", []) if isinstance(t, dict)}
    except Exception:
        logger.exception("[confirm] failed to fetch tags for deal=%s", deal_id)

    # требуем ровно «не меньше» по каждой роли (то есть все назначенные должны подтвердить)
    # Если назначенных больше, чем требуется, ждём подтверждения всех назначенных в locked_distribution.
    return must.issubset(tags)


# ███ [2.0] Рендер «🎲 Мои игры»
# ────────────────────────────────────────────────────────────────────
async def _render_my_games(uid: int, bot: Bot) -> None:
    """
    Собирает и отправляет список игр пользователя (Бронь/Завершение сделки),
    где он утверждён на роль. Открытие деталей — отдельной кнопкой.
    ВАЖНО: «вакуум→отправка» под per-user локом против гонок.
    """
    short = _short_name_from_profile(uid)
    logger.info("[my_games] render for uid=%s (%s)", uid, short)

    deals_index: Dict[int, Dict] = getattr(state, "deals_index", {}) or {}

    kb_rows: List[List[InlineKeyboardButton]] = []
    for deal_id, meta in deals_index.items():
        status = (meta or {}).get("status", "")
        if status not in ("Бронь", "Завершение сделки"):
            continue

        # Подтягиваем свежие детали (источник правды)
        try:
            details = await _safe_refresh(uid, int(deal_id), silent=True)
        except Exception:
            logger.exception("[my_games] refresh failed for deal_id=%s", deal_id)
            continue

        roles = _extract_user_roles_from_details(details, uid)
        if not roles:
            continue

        title = (details or {}).get("game_name") or (details or {}).get("title") or f"Сделка #{deal_id}"
        btn_text = f"ℹ️ {truncate(title, 28)} · {status}"
        kb_rows.append([InlineKeyboardButton(text=btn_text, callback_data=f"my_deal_open_{deal_id}")])

    lock = state.lock_for(uid)
    async with lock:
        try:
            await delete_previous_private_messages(uid)
        except Exception:
            logger.exception("[my_games] delete_previous_private_messages failed")

        if not kb_rows:
            await bot.send_message(uid, "Пока нет утверждённых игр для подтверждения.", reply_markup=await get_main_menu(uid))
            return

        markup = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        msg = await bot.send_message(uid, "🎲 Ваши игры:", reply_markup=markup)
        state.last_user_messages[uid] = [msg]  # объект сообщения

# История изменений: добавлен per-user lock вокруг vacuum+send (2025-08-12)


# ════════════════════════════════════════════════════════════════════
# [2.1] Хендлеры «🎲 Мои игры»
# ════════════════════════════════════════════════════════════════════
@router.message(Command("my"), flags={"private_only": True})
@router.message(lambda m: m.text and m.text.strip() == "🎲 Мои игры", flags={"private_only": True})
async def my_games_dashboard(message: types.Message, bot: Bot) -> None:
    await _render_my_games(message.from_user.id, bot)
    with contextlib.suppress(Exception):
        await message.delete()


@router.callback_query(lambda c: c.data and c.data.startswith("my_deal_open_"))
async def my_deal_open(callback: CallbackQuery, bot: Bot) -> None:
    """
    Открывает карточку конкретной игры (перерисовку делает refresh_deal_details).
    """
    uid = callback.from_user.id
    deal_id = int((callback.data or "").rsplit("_", 1)[-1])
    await callback.answer()
    await _safe_refresh(uid, deal_id, silent=False)  # он же нарисует блоки по ролям
    logger.info("[my_games] open details deal_id=%s by uid=%s", deal_id, uid)


# ════════════════════════════════════════════════════════════════════
# [3.0] Кнопка «✅ Подтвердить» в карточке роли
# ════════════════════════════════════════════════════════════════════
@router.callback_query(lambda c: c.data and c.data.startswith(CONFIRM_PREFIX))
async def confirm_role_handler(callback: CallbackQuery, bot: Bot) -> None:
    """
    confirm_role_{deal_id}_{role}
    • Ставит тег в AmoCRM для пользователя в роли (совместимость с разными сигнатурами).
    • Шлёт уведомление в общий чат: «Имя Ф. подтвердил выход на игру».
    • Если все роли подтверждены — переводит статус сделки в «Завершение сделки»,
      удаляет игру из активного опроса и пытается завершить цикл.
    • Перерисовывает детали и дашборд «Мои игры».
    """
    uid = callback.from_user.id
    parts = (callback.data or "").split("_")
    if len(parts) < 4:
        await callback.answer("Некорректные данные кнопки.", show_alert=True)
        return

    # confirm_role_{deal_id}_{role}
    try:
        deal_id = int(parts[2])
    except Exception:
        await callback.answer("Некорректный идентификатор игры.", show_alert=True)
        return

    role = parts[3]  # 'main' | 'assist' | 'admin'
    if role not in {"main", "assist", "admin"}:
        await callback.answer("Неизвестная роль.", show_alert=True)
        return

    short = _short_name_from_profile(uid)
    await callback.answer("✅ Принято")

    # 1) Фактическая проверка назначения роли пользователю
    details = await _safe_refresh(uid, deal_id, silent=True)
    roles = _extract_user_roles_from_details(details, uid)
    if role not in roles:
        await callback.answer("Эта роль не назначена на вас.", show_alert=True)
        return

    # 2) Проставляем тег в AmoCRM (поддержка двух сигнатур клиента)
    tag_value = f"{short}{_role_suffix(role)}"
    try:
        ok = False
        try:
            # вариант 1: словарь {deal_id: {confirm: tag}}
            ok = await update_amocrm_tags({str(deal_id): {"confirm": tag_value}})
        except TypeError:
            ok = False
        if not ok:
            # вариант 2: позиционные аргументы (deal_id, add=[...])
            try:
                ok = await update_amocrm_tags(deal_id, add=[tag_value])  # type: ignore[arg-type]
            except TypeError:
                ok = False
        if not ok:
            raise RuntimeError("update_amocrm_tags returned False")
        logger.info("[confirm] tag set deal=%s uid=%s role=%s tag=%s", deal_id, uid, role, tag_value)
    except Exception:
        logger.exception("[confirm] failed to update tags (deal=%s uid=%s)", deal_id, uid)
        await callback.answer("Не удалось проставить тег. Попробуйте позже.", show_alert=True)
        return

    # 2.1) Локально кэшируем тег (ускоряет повторные проверки)
    try:
        cache = getattr(state, "deal_tags_cache", {}) or {}
        tags = set(cache.get(deal_id, []))
        tags.add(tag_value)
        cache[deal_id] = sorted(tags)
        state.deal_tags_cache = cache
    except Exception:
        logger.debug("[confirm] deal_tags_cache update skipped")

    # 3) Сообщение в общий чат
    if ADMIN_CHAT_ID:
        try:
            title = (details or {}).get("title") or f"Сделка {deal_id}"
            txt = f"{short} подтвердил выход на игру: «{title}»."
            await bot.send_message(ADMIN_CHAT_ID, txt)
        except Exception:
            logger.exception("[confirm] failed to notify leaders chat")

    # 4) Если все обязательные роли подтверждены — переводим в «Завершение сделки»
    details_after = await _safe_refresh(uid, deal_id, silent=True)
    try:
        all_ok = await _is_full_confirmation(details_after, deal_id)
    except Exception:
        logger.exception("[confirm] _is_full_confirmation failed (deal=%s)", deal_id)
        all_ok = False

    if all_ok:
        try:
            moved = False
            # попытка по имени стадии
            try:
                moved = await update_deal_status(deal_id, stage_name="Завершение сделки")  # type: ignore[call-arg]
            except TypeError:
                moved = False
            # fallback по ID из настроек (если клиент ожидает id)
            if not moved:
                stage_id = getattr(settings, "FINISH_STAGE_ID", None) or getattr(settings, "SUCCESSFUL_STATUS_ID", None)
                if stage_id is not None:
                    moved = await update_deal_status(deal_id, stage_id)  # type: ignore[arg-type]
            if moved:
                logger.info("[confirm] deal=%s moved to 'Завершение сделки'", deal_id)
                # выводим игру из активного опроса
                try:
                    state.current_poll_deals = [d for d in (state.current_poll_deals or []) if d.get("id") != deal_id]
                except Exception:
                    pass
                # пингуем автозавершение цикла
                try:
                    from handlers.polls_lifecycle import finish_if_all_deals_completed, _sync_leader_report  # local import
                    await finish_if_all_deals_completed(bot)
                    await _sync_leader_report()
                except Exception:
                    logger.exception("[confirm] finish_if_all_deals_completed/_sync_leader_report failed")
            else:
                logger.warning("[confirm] status update returned False (deal=%s)", deal_id)
        except Exception:
            logger.exception("[confirm] failed to update deal status")

    # 5) Перерисовки UI
    await _safe_refresh(uid, deal_id, silent=False)
    await _render_my_games(uid, bot)


# ════════════════════════════════════════════════════════════════════
# [4.0] Утилита массового завершения цикла (выход из опроса)
# ════════════════════════════════════════════════════════════════════
async def finish_if_all_deals_completed(bot: Bot) -> None:
    """
    Проверяет текущий опрос: если все входящие сделки перешли
    в «Завершение сделки» (или выведены вручную), закрывает цикл опроса.
    Вызывается-хуком из poll_details после утверждений/подтверждений.
    """
    try:
        deals: Dict[int, Dict] = getattr(state, "deals_index", {}) or {}
        active = [d for d in deals.values() if (d or {}).get("in_poll")]
        if not active:
            return
        if all((d.get("status") == "Завершение сделки") or (d.get("removed_from_poll")) for d in active):
            state.current_poll_id = None
            logger.info("[poll] cycle finished: all deals completed")
    except Exception:
        logger.exception("finish_if_all_deals_completed error")


# ════════════════════════════════════════════════════════════════════
# [5.0] SAFE REFRESH WRAPPER
# ════════════════════════════════════════════════════════════════════
async def _safe_refresh(user_id: int, deal_id: int, *, silent: bool = False, context: Optional[str] = None) -> Dict:
    """
    Универсальная обёртка над refresh_deal_details с поддержкой разных сигнатур:
      refresh_deal_details(user_id, deal_id)
      refresh_deal_details(user_id, deal_id, silent=True/False)
      refresh_deal_details(user_id=user_id, deal_id=deal_id, context="my_games")
    Возвращает dict деталей или пустой словарь.
    """
    # Попытка №1 — с именованными параметрами и silent/context
    try:
        return await refresh_deal_details(user_id=user_id, deal_id=deal_id, silent=silent, context=context)
    except TypeError:
        pass
    # Попытка №2 — только с silent
    try:
        return await refresh_deal_details(user_id, deal_id, silent=silent)  # type: ignore
    except TypeError:
        pass
    # Попытка №3 — базовый контракт (без silent)
    try:
        return await refresh_deal_details(user_id, deal_id)  # type: ignore
    except Exception:
        logger.exception("[safe_refresh] refresh_deal_details failed (deal=%s)", deal_id)
        return {}


# ════════════════════════════════════════════════════════════════════
# [99.0] Тесты (минимальные)
# ════════════════════════════════════════════════════════════════════
async def _test():
    # Нормализация ролей из деталей
    d = {"roles": {"lead1": ["Иван|101", 202], "assistant1": [303], "admin": 404}}
    assert _extract_user_roles_from_details(d, 101) == {"main"}
    assert _extract_user_roles_from_details(d, 303) == {"assist"}
    assert _extract_user_roles_from_details(d, 404) == {"admin"}

    # Сокращённое имя
    s = _short_name_from_profile(999999)  # неизвестный
    assert isinstance(s, str) and len(s) > 0

    # Суффиксы тега
    assert _role_suffix("main") == ".1"
    assert _role_suffix("assist") == ".2"
    assert _role_suffix("admin") == ".Ад"

    print("handlers.confirmations ✅ tests passed")


if __name__ == "__main__":
    asyncio.run(_test())

# История изменений:
# 2025-08-10: v14.8 — безопасный вызов refresh_deal_details (_safe_refresh);
#                    проверка «все подтвердили» по тегам CRM при отсутствии details.confirmed;
#                    исправлен вызов delete_previous_private_messages(uid);
#                    логика «Мои игры» основана на detail-кэше + legacy + тегах, без записи в CRM при утверждении;
#                    стабильные уведомления в общий чат.
