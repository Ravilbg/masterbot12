# services/amocrm.py — AmoCRM REST-client (tokens, deals, tags, status)
# ─────────────────────────────────────────────────────────────────────
"""
AmoCRM REST-клиент: авторизация/рефреш токена, загрузка и нормализация сделок,
массовая установка тегов, перевод статусов. Совместим с MasterBot ≥ 15.1.

Версия 2025-08-12
──────────────────────────────────────────────────────────────────────
Экспортируемые функции:
• get_amocrm_deals()     — загрузка и нормализация сделок (для бота)
• get_deal_by_id()       — получить ОДНУ сделку по ID (нормализованную)
• update_amocrm_tags()   — массовая запись тегов в сделки
• update_deal_status()   — перевести сделку в другой статус
• get_pipeline_stages()  — справочник статусов
• get_custom_fields()    — справочник кастом-полей

Формат нормализованной сделки:
{
  "id": int,
  "name": str,
  "game_name": str,
  "status_id": str,
  "status": str,
  "event_datetime": datetime(tz=Europe/Moscow),
  "event_time": "HH:MM",
  "players": str,
  "players_count": int,
  "age": str,
  "package": str,
  "extra_services": str,     # перечисление через запятую
  "comment": str,
  "total_budget": int,
  "prepayment": int,
  "to_calculate": int,
  "team_leads": [{"id": int, "name": str}],
  "photographer": str,
}
"""

from __future__ import annotations

# ███ [0] IMPORTS
# --------------------------------------------------------------------
import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from pytz import timezone

# ── fallback-импорт core.* (для self-test/offline окружений) ────────
try:
    from core.config import settings  # type: ignore
    from core.state import state      # type: ignore
    from core.utils import parse_players_count  # type: ignore
except ModuleNotFoundError:
    logging.getLogger(__name__).warning("[Amo] core module not found, using stubs")

    class _Settings:
        TOKENS_FILE = "tokens_stub.json"
        DATE_FILTER_DAYS = 30
        SUCCESSFUL_STATUS_ID = "18913935"
        AMOCRM_FIELDS: Dict[str, str] = {
            # ВНИМАНИЕ: в реальном проекте здесь должны быть ID полей
            "event_date": "event_date",
            "event_time": "event_time",
            "players": "players",
            "age": "age",
            "package": "package",
            "extra_services": "extra_services",
            "comment": "comment",
            "photographer": "photographer",
            "game_name": "game_name",
            "prepayment": "prepayment",
            "team_leads": "team_leads",
        }

    settings = _Settings()  # type: ignore

    class _State:
        tokens: Dict[str, Any] = {}
        config: Dict[str, Any] = {}  # ожидались: domain, client_id, client_secret, redirect_uri

    state = _State()  # type: ignore

    def parse_players_count(val: Any) -> int:  # type: ignore
        nums = re.findall(r"\d+", str(val or ""))
        return int(nums[-1]) if nums else 0

# ── module-wide constants ───────────────────────────────────────────
logger = logging.getLogger(__name__)
MSK_TZ = timezone("Europe/Moscow")
TOKENS_FILE = getattr(settings, "TOKENS_FILE", "tokens.json")
DATE_FILTER_DAYS = int(getattr(settings, "DATE_FILTER_DAYS", 30))

__all__ = [
    "get_amocrm_deals",
    "get_deal_by_id",
    "update_amocrm_tags",
    "update_deal_status",
    "get_pipeline_stages",
    "get_custom_fields",
]

# ════════════════════════════════════════════════════════════════════
# [1] TOKEN HELPERS
# ════════════════════════════════════════════════════════════════════
async def _load_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


async def _save_json(path: str, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


async def ensure_valid_token() -> bool:
    """Гарантирует наличие валидного access_token в state.tokens."""
    if not getattr(state, "tokens", None):
        state.tokens = await _load_json(TOKENS_FILE)  # type: ignore[attr-defined]
        if not state.tokens:
            logger.error("[Amo] empty tokens file %s", TOKENS_FILE)
            return False

    expires_at = float(state.tokens.get("expires_at", 0))
    # Обновим за 5 минут до истечения
    if datetime.now().timestamp() >= (expires_at - 300):
        return await refresh_amocrm_token()
    return True


async def refresh_amocrm_token() -> bool:
    """Рефреш токена через /oauth2/access_token."""
    cfg = getattr(state, "config", {}) or {}
    for k in ("client_id", "client_secret", "redirect_uri", "domain"):
        if not cfg.get(k):
            logger.error("[Amo] missing %s in config", k)
            return False

    payload = {
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "grant_type": "refresh_token",
        "refresh_token": (state.tokens or {}).get("refresh_token", ""),  # type: ignore[attr-defined]
        "redirect_uri": cfg["redirect_uri"],
    }

    async with aiohttp.ClientSession() as ses:
        async with ses.post(f"https://{cfg['domain']}/oauth2/access_token", json=payload) as resp:
            if resp.status != 200:
                logger.error("[Amo] refresh HTTP %d – %s", resp.status, await resp.text())
                return False
            data = await resp.json()
            data["expires_at"] = (
                datetime.now() + timedelta(seconds=int(data.get("expires_in", 86400)))
            ).timestamp()
            state.tokens.update(data)  # type: ignore[attr-defined]
            await _save_json(TOKENS_FILE, state.tokens)  # type: ignore[attr-defined]
            logger.info("[Amo] token refreshed, expires_at=%s", data["expires_at"])
            return True

# ════════════════════════════════════════════════════════════════════
# [2] LOW-LEVEL HTTP
# ════════════════════════════════════════════════════════════════════
async def _auth_headers() -> Dict[str, str]:
    ttype = (state.tokens or {}).get("token_type", "Bearer")  # type: ignore[attr-defined]
    atok = (state.tokens or {}).get("access_token", "")       # type: ignore[attr-defined]
    return {"Authorization": f"{ttype} {atok}"}


async def _request_json(
    method: str,
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    payload: Optional[Dict[str, Any]] = None,
    ok_statuses: Tuple[int, ...] = (200,),
) -> Optional[Dict[str, Any]]:
    """Единый враппер с ретраями 429/401."""
    if not await ensure_valid_token():
        return None

    cfg = getattr(state, "config", {}) or {}
    if not cfg.get("domain"):
        # offline режим: вернём None, чтобы вызывающий сам подставил стабы
        return None

    tries = 0
    while tries < 5:
        tries += 1
        async with aiohttp.ClientSession() as ses:
            async with ses.request(
                method,
                url,
                headers=await _auth_headers(),
                params=params,
                json=payload,
            ) as resp:
                if resp.status == 429:
                    await asyncio.sleep(min(2**tries * 0.2, 3.0))
                    continue
                if resp.status == 401:
                    ok = await refresh_amocrm_token()
                    if ok:
                        continue
                    logger.error("[Amo] unauthorized and refresh failed")
                    return None
                if resp.status not in ok_statuses:
                    txt = await resp.text()
                    logger.error("[Amo] %s %s HTTP %d — %s", method, url, resp.status, txt)
                    return None
                try:
                    return await resp.json()
                except Exception:
                    return None
    return None


async def _patch_deal(deal_id: int, payload: Dict[str, Any]) -> bool:
    cfg = getattr(state, "config", {}) or {}
    url = f"https://{cfg['domain']}/api/v4/leads/{deal_id}"
    js = await _request_json("PATCH", url, payload=payload, ok_statuses=(200,))
    return bool(js is not None)

# ════════════════════════════════════════════════════════════════════
# [3] СПРАВОЧНИКИ
# ════════════════════════════════════════════════════════════════════
async def get_pipeline_stages() -> Dict[str, str]:
    """Справочник статусов (id -> name) по всем пайплайнам."""
    cfg = getattr(state, "config", {}) or {}
    if not cfg.get("domain"):
        return {}
    js = await _request_json("GET", f"https://{cfg['domain']}/api/v4/leads/pipelines")
    if not js:
        return {}
    return {
        str(st["id"]): st["name"]
        for pl in js.get("_embedded", {}).get("pipelines", [])
        for st in pl.get("_embedded", {}).get("statuses", [])
    }


async def get_custom_fields() -> Dict[str, str]:
    """Справочник кастом-полей лидов (id -> name)."""
    cfg = getattr(state, "config", {}) or {}
    if not cfg.get("domain"):
        return {}
    js = await _request_json("GET", f"https://{cfg['domain']}/api/v4/leads/custom_fields")
    if not js:
        return {}
    return {str(f["id"]): f["name"] for f in js.get("_embedded", {}).get("custom_fields", [])}

# ════════════════════════════════════════════════════════════════════
# [4] PARSE / NORMALIZE
# ════════════════════════════════════════════════════════════════════
async def _parse_event_datetime(raw_date: Any, raw_time: Any) -> Optional[datetime]:
    """Поддержка timestamp/мс, ISO/ru-форматов и 'не указано'."""
    # timestamp / ms
    try:
        ts = float(raw_date)
        if ts > 1e12:
            ts /= 1000
        return MSK_TZ.localize(datetime.fromtimestamp(int(ts)))
    except Exception:
        pass

    date_s = str(raw_date or "").strip()
    time_s = str(raw_time or "").strip()
    if time_s.lower() == "не указано":
        time_s = ""

    cand = f"{date_s} {time_s}".strip()
    for fmt in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            dt = datetime.strptime(cand, fmt)
            if " %H:%M" not in fmt:
                dt = dt.replace(hour=0, minute=0)
            return MSK_TZ.localize(dt)
        except ValueError:
            continue
    return None


async def _process_deal(raw: Dict[str, Any], stages: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Нормализует структуру сделки под MasterBot."""
    AMOF = settings.AMOCRM_FIELDS
    did = raw.get("id")
    if not did:
        return None

    cf_raw = raw.get("custom_fields_values") or []
    cf = {
        str(f.get("field_id")): [v["value"] for v in f.get("values", []) if v.get("value")]
        for f in cf_raw
    }

    event_dt = await _parse_event_datetime(
        cf.get(AMOF["event_date"], [None])[0] if AMOF.get("event_date") in cf else None,
        (cf.get(AMOF["event_time"], [""])[0] if AMOF.get("event_time") in cf else ""),
    )
    if not event_dt:
        return None
    event_time = (
        cf.get(AMOF["event_time"], [""])[0] if AMOF.get("event_time") in cf else ""
    ) or event_dt.strftime("%H:%M")

    def to_int(v: Any) -> int:
        try:
            return int(float(str(v).replace(",", ".")))
        except Exception:
            return 0

    total = raw.get("price", 0)
    pre = to_int(cf.get(AMOF.get("prepayment", ""), [0])[0] if AMOF.get("prepayment") in cf else 0)

    # имена ведущих по uid (через core.db.get_user_info)
    try:
        from core.db import get_user_info  # type: ignore
    except ModuleNotFoundError:
        get_user_info = lambda _id: {"first_name": "Unknown"}  # type: ignore

    leads_field = cf.get(AMOF.get("team_leads", ""), []) if AMOF.get("team_leads") else []
    leads: List[Dict[str, Any]] = []
    for lid in map(str, leads_field):
        lid = lid.strip()
        if not lid:
            continue
        try:
            uid = int(lid)
        except Exception:
            continue
        ui = (get_user_info(uid) or {})
        leads.append({"id": uid, "name": ui.get("first_name", "Unknown")})

    return {
        "id": int(did),
        "name": raw.get("name", f"Сделка {did}"),
        "game_name": (cf.get(AMOF.get("game_name", ""), [""])[0] if AMOF.get("game_name") in cf else ""),
        "status_id": str(raw.get("status_id", "")),
        "status": stages.get(str(raw.get("status_id", "")), "Неизвестно"),
        "event_datetime": event_dt,
        "event_time": event_time,
        "players": (cf.get(AMOF.get("players", ""), [""])[0] if AMOF.get("players") in cf else ""),
        "players_count": parse_players_count(
            (cf.get(AMOF.get("players", ""), [""])[0] if AMOF.get("players") in cf else "")
        ),
        "age": (cf.get(AMOF.get("age", ""), [""])[0] if AMOF.get("age") in cf else ""),
        "package": (cf.get(AMOF.get("package", ""), [""])[0] if AMOF.get("package") in cf else ""),
        "extra_services": ", ".join(cf.get(AMOF.get("extra_services", ""), [])) if AMOF.get("extra_services") else "",
        "comment": (cf.get(AMOF.get("comment", ""), [""])[0] if AMOF.get("comment") in cf else ""),
        "total_budget": total,
        "prepayment": pre,
        "to_calculate": max(0, int(total) - int(pre)),
        "team_leads": leads,
        "photographer": (cf.get(AMOF.get("photographer", ""), [""])[0] if AMOF.get("photographer") in cf else ""),
    }

# ════════════════════════════════════════════════════════════════════
# [5] PUBLIC API
# ════════════════════════════════════════════════════════════════════
async def _build_cf_patch(updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Собирает payload для PATCH кастом-полей AmoCRM:
    updates: {"photographer": "нет", ...} → {"custom_fields_values":[{field_id, values:[{value}]}]}
    Возвращает None, если нечего патчить или отсутствуют ID полей.
    """
    AMOF = getattr(settings, "AMOCRM_FIELDS", {}) or {}
    cf_items: List[Dict[str, Any]] = []

    for key, val in updates.items():
        if val is None or str(val).strip() == "":
            continue
        field_id = AMOF.get(key)
        if not field_id:
            logger.debug("[Amo] skip CF '%s': not configured in AMOCRM_FIELDS", key)
            continue
        try:
            fid = int(str(field_id).strip())
        except Exception:
            logger.warning("[Amo] CF '%s' has non-numeric id=%r, skip", key, field_id)
            continue
        cf_items.append({"field_id": fid, "values": [{"value": val}]})

    if not cf_items:
        return None
    return {"custom_fields_values": cf_items}


def _is_empty_like(value: Any) -> bool:
    """True, если значение пустое/не указано/дефис и т.п."""
    s = str(value or "").strip().lower()
    return s in {"", "-", "—", "не указано", "нет данных", "none", "null"}


async def preflight_before_status_change(deal_id: int) -> bool:
    """
    Pre-flight перед сменой статуса:
    • проверяем «блокирующие» поля (минимум photographer);
    • если пусто — ставим 'нет';
    • только после этого можно менять статус.
    Возвращает True даже в offline, чтобы не ломать цикл.
    """
    cfg = getattr(state, "config", {}) or {}
    if not cfg.get("domain"):
        # offline/stub режим — считаем, что всё ок
        logger.debug("[Amo] preflight offline ok for deal=%s", deal_id)
        return True

    try:
        deal = await get_deal_by_id(deal_id)
    except Exception as e:
        logger.warning("[Amo] preflight get_deal_by_id failed for %s: %s", deal_id, e)
        deal = None

    # Список обязательных полей (минимальный набор по ТЗ)
    need_updates: Dict[str, Any] = {}
    try:
        photographer_val = (deal or {}).get("photographer", "")
        if _is_empty_like(photographer_val):
            need_updates["photographer"] = "нет"
    except Exception:
        need_updates["photographer"] = "нет"

    if not need_updates:
        return True

    # FIX: обязательно await, иначе будет "coroutine is not JSON serializable"
    payload = await _build_cf_patch(need_updates)
    if not payload:
        logger.debug("[Amo] preflight: nothing to patch for deal=%s", deal_id)
        return True

    ok = await _patch_deal(deal_id, payload)
    if not ok:
        logger.warning("[Amo] preflight patch failed for deal=%s payload=%s", deal_id, need_updates)
        # Не блокируем дальнейшую смену статуса — CRM сама вернёт ошибку, если поля критичны
        return False

    logger.info("[Amo] preflight patched deal=%s with %s", deal_id, need_updates)
    return True


# alias для совместимости с внешними вызовами
async def ensure_required_fields(deal_id: int) -> bool:
    return await preflight_before_status_change(deal_id)


# ── helpers для тегов (safe merge) ──────────────────────────────────
def _normalize_tag_name(name: str) -> str:
    return " ".join((name or "").split())


def _dedup_tags(items: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for t in items:
        n = _normalize_tag_name(t)
        if not n or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


async def _get_current_tags_for_deal(deal_id: int) -> List[str]:
    """
    Возвращает список имён тегов сделки. Если 204/ошибка — пустой список.
    """
    cfg = getattr(state, "config", {}) or {}
    if not cfg.get("domain"):
        return []
    js = await _request_json(
        "GET",
        f"https://{cfg['domain']}/api/v4/leads/{int(deal_id)}",
        params={"with": "tags"},
        ok_statuses=(200, 204),
    )
    if not js or not isinstance(js, dict):
        return []
    embedded = js.get("_embedded") or {}
    tags = embedded.get("tags") or []
    names = [str(t.get("name", "")).strip() for t in tags if isinstance(t, dict)]
    return _dedup_tags(names)


async def update_amocrm_tags(deals_tags: Dict[str, Dict[str, str]]) -> bool:
    """
    Массовая запись тегов в сделки (SAFE MERGE, без затирания).
    deals_tags: {"12345": {"lead1": "Имя Ф.1", "assist1": "Имя Ф.2", ...}, ...}

    Алгоритм для каждой сделки:
    1) GET /leads/{id}?with=tags → текущие теги (может вернуть пусто/204).
    2) merged = dedup(current + new_values).
    3) PATCH /leads/{id} {"_embedded":{"tags":[{"name": "..."}]}}
    """
    if not deals_tags:
        return True

    cfg = getattr(state, "config", {}) or {}
    if not cfg.get("domain"):
        # offline stub
        logger.debug("[Amo] offline update_amocrm_tags (merge): %s", deals_tags)
        return True

    sem = asyncio.Semaphore(5)

    async def _process_one(did: int, tag_map: Dict[str, str]) -> bool:
        async with sem:
            try:
                incoming = _dedup_tags([v for v in (tag_map or {}).values() if v])
                if not incoming:
                    return True
                current = await _get_current_tags_for_deal(did)
                merged = _dedup_tags(current + incoming)
                payload = {"_embedded": {"tags": [{"name": t} for t in merged]}}
                ok = await _patch_deal(did, payload)
                if ok:
                    logger.info(
                        "[Amo] tags merged for deal=%s: current=%s, add=%s, result=%s",
                        did, current, incoming, merged
                    )
                return ok
            except Exception as e:
                logger.exception("[Amo] update_amocrm_tags failed for deal=%s: %s", did, e)
                return False

    tasks = [_process_one(int(deal_id), tag_map) for deal_id, tag_map in deals_tags.items()]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return all(isinstance(r, bool) and r for r in results)


async def update_deal_status(deal_id: int, status_id: str) -> bool:
    """
    Переводит сделку в новый статус (например «Завершение сделки»).
    Используется handlers.confirmations после подтверждения всех ведущих.
    Перед сменой статуса выполняется pre-flight заполнения обязательных полей.
    """
    cfg = getattr(state, "config", {}) or {}
    if not cfg.get("domain"):
        # offline-stub: считаем успешно
        logger.debug("[Amo] offline update_deal_status: id=%s -> %s", deal_id, status_id)
        return True

    try:
        await preflight_before_status_change(deal_id)
    except Exception as e:
        logger.warning("[Amo] preflight raised for deal=%s: %s (continue to status patch)", deal_id, e)

    payload = {"status_id": int(status_id)}
    return await _patch_deal(deal_id, payload)


async def _fetch_amocrm_deals_raw() -> List[Dict[str, Any]]:
    """Забирает «сырые» лиды из AmoCRM страницами (без нормализации)."""
    cfg = getattr(state, "config", {}) or {}
    if not cfg.get("domain"):
        return []

    # Пейджинг: фильтруем по updated_at за последние DATE_FILTER_DAYS
    page, limit = 1, 250
    updated_from = int((datetime.now() - timedelta(days=DATE_FILTER_DAYS)).timestamp())
    out: List[Dict[str, Any]] = []

    while True:
        params = {
            "with": "contacts",
            "limit": limit,
            "page": page,
            "filter[updated_at][from]": updated_from,
        }
        js = await _request_json("GET", f"https://{cfg['domain']}/api/v4/leads", params=params)
        if not js:
            break
        leads = js.get("_embedded", {}).get("leads", [])
        if not leads:
            break
        out.extend(leads)
        page += 1
        await asyncio.sleep(0.2)
    return out


async def fetch_amocrm_deals() -> List[Dict[str, Any]]:
    """
    Загружает сделки и приводит их к нормализованной форме.
    В offline-окружении возвращает один stub-дил.
    """
    cfg = getattr(state, "config", {}) or {}
    stages = await get_pipeline_stages() if cfg.get("domain") else {"100": "Бронь"}

    # offline-fallback
    if not cfg.get("domain"):
        stub = {
            "id": 1,
            "name": "Test deal",
            "status_id": 100,
            "price": 10000,
            "custom_fields_values": [
                {"field_id": settings.AMOCRM_FIELDS["event_date"], "values": [{"value": "2025-08-15"}]},
                {"field_id": settings.AMOCRM_FIELDS["event_time"], "values": [{"value": "18:00"}]},
                {"field_id": settings.AMOCRM_FIELDS["players"],     "values": [{"value": "до 8"}]},
            ],
        }
        d = await _process_deal(stub, stages)
        return [d] if d else []

    raws = await _fetch_amocrm_deals_raw()
    deals: List[Dict[str, Any]] = []
    for raw in raws:
        d = await _process_deal(raw, stages)
        if d:
            deals.append(d)
    deals.sort(key=lambda d: d["event_datetime"])
    return deals


async def get_amocrm_deals() -> List[Dict[str, Any]]:
    """Публичная обёртка с логированием ошибок."""
    try:
        return await fetch_amocrm_deals()
    except Exception as exc:
        logger.exception("[Amo] get_amocrm_deals failed: %s", exc)
        return []


async def get_deal_by_id(deal_id: int | str) -> Optional[Dict[str, Any]]:
    """
    Возвращает ОДНУ сделку по ID в нормализованном формате.
    Алгоритм:
      1) Пробуем прямой GET /api/v4/leads/{id} (быстро и точно).
      2) Если не получилось — фолбэк: забираем все сделки и фильтруем по id.
    """
    try:
        did = int(deal_id)
    except Exception:
        logger.warning("[Amo] get_deal_by_id bad id=%r", deal_id)
        return None

    cfg = getattr(state, "config", {}) or {}
    stages = await get_pipeline_stages() if cfg.get("domain") else {"100": "Бронь"}

    # Прямой запрос
    if cfg.get("domain"):
        js = await _request_json("GET", f"https://{cfg['domain']}/api/v4/leads/{did}")
        if js and isinstance(js, dict):
            # ответ одного лида — это сам лид (не в _embedded)
            raw = js if js.get("id") else None
            if raw:
                d = await _process_deal(raw, stages)
                if d:
                    return d

    # Фолбэк: общий список + фильтр
    all_deals = await fetch_amocrm_deals()
    for d in all_deals:
        if int(d.get("id", -1)) == did:
            return d
    return None
# История изменений (блок [5]):
# 2025-08-12 — добавлен preflight_before_status_change() и alias ensure_required_fields();
#              update_deal_status() теперь вызывает pre-flight перед PATCH статуса;
#              остальной публичный API сохранён без изменений.
# 2025-08-13 — FIX: await _build_cf_patch() в preflight; SAFE MERGE в update_amocrm_tags().

# ════════════════════════════════════════════════════════════════════
# [5a] SINGLE-TAG HELPERS (используется handlers.confirmations)
# ════════════════════════════════════════════════════════════════════
async def patch_lead(lead_id: int, payload: Dict[str, Any]) -> bool:
    """
    Универсальный PATCH одной сделки. В offline-режиме — no-op с True.
    Не затирает поля, если передавать _embedded/partial payload.
    """
    cfg = getattr(state, "config", {}) or {}
    if not cfg.get("domain"):
        logger.debug("[Amo] offline patch_lead: id=%s payload=%s", lead_id, payload)
        return True
    return await _patch_deal(int(lead_id), payload)


async def add_tag_to_lead(lead_id: int, tag: str) -> bool:
    """
    Безопасно добавляет ОДИН тег (merge, без перетирания других).
    """
    tag = _normalize_tag_name(tag)
    if not tag:
        return True
    # читаем текущие теги → merge → patch
    current = await _get_current_tags_for_deal(int(lead_id))
    merged = _dedup_tags(current + [tag])
    payload = {"_embedded": {"tags": [{"name": t} for t in merged]}}
    return await patch_lead(int(lead_id), payload)

# ════════════════════════════════════════════════════════════════════
# [6] SELF-TEST
# ════════════════════════════════════════════════════════════════════
async def _test() -> None:
    """
    Мини-тесты (будут работать и без реального AmoCRM — в offline-режиме).
    """
    deals = await get_amocrm_deals()
    assert isinstance(deals, list)
    if deals:
        # Проверим базовую структуру нормализации на первом элементе
        d0 = deals[0]
        assert "id" in d0 and "event_datetime" in d0 and "players_count" in d0
        # В offline-режиме у нас заранее известные значения
        if d0["id"] == 1 and d0["name"] == "Test deal":
            assert d0["players_count"] == 8
            assert d0["event_datetime"].strftime("%Y-%m-%d %H:%M") == "2025-08-15 18:00"

    # Проверим get_deal_by_id (в offline вернёт ту же тестовую сделку)
    d = await get_deal_by_id(1)
    assert (d is None) or isinstance(d, dict)

    # Триггеры PATCH как минимум не должны падать в offline
    ok_tags = await update_amocrm_tags({"1": {"lead1": "Иван|1"}})
    assert ok_tags is True

    ok_status = await update_deal_status(1, getattr(settings, "SUCCESSFUL_STATUS_ID", "18913935"))
    assert ok_status is True

    print("services.amocrm ✅ self-tests passed")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    asyncio.run(_test())

# История изменений:
# 2025-08-12 — Полный рефактор под MasterBot ≥ 15.1; добавлен get_deal_by_id;
#              единый _request_json c ретраями; аккуратные offline-stubs.
