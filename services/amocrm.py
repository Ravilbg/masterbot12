"""services/amocrm.py — AmoCRM REST-client (tokens, deals, tags, status)
Версия 2025-08-12 · совместима с MasterBot ≥ 15.1.

Экспортируемые функции
──────────────────────
• get_amocrm_deals()     — загрузка сделок (handlers.games)
• update_amocrm_tags()   — массовая запись тегов (handlers.confirmations …)
• update_deal_status()   — перевести сделку в другой статус
• get_pipeline_stages()  — справочник статусов
• get_custom_fields()    — справочник кастом-полей
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp
from pytz import timezone

# ── fallback-импорт core.* ──────────────────────────────────────────
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

    settings = _Settings()

    class _State:
        tokens: Dict[str, Any] = {}
        config: Dict[str, Any] = {}  # задайте 'domain', 'client_id' и т.д.

    state = _State()

    def parse_players_count(val: Any) -> int:
        nums = re.findall(r"\d+", str(val or ""))
        return int(nums[-1]) if nums else 0

# ── module-wide constants ───────────────────────────────────────────
logger = logging.getLogger(__name__)
MSK_TZ = timezone("Europe/Moscow")
TOKENS_FILE = settings.TOKENS_FILE
DATE_FILTER_DAYS = settings.DATE_FILTER_DAYS

# ════════════════════════════════════════════════════════════════════
# 1. TOKEN HELPERS
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
    if not state.tokens:
        state.tokens = await _load_json(TOKENS_FILE)
        if not state.tokens:
            logger.error("[Amo] empty tokens file %s", TOKENS_FILE)
            return False
    if datetime.now().timestamp() >= state.tokens.get("expires_at", 0) - 300:
        return await refresh_amocrm_token()
    return True


async def refresh_amocrm_token() -> bool:
    cfg = state.config or {}
    for k in ("client_id", "client_secret", "redirect_uri", "domain"):
        if k not in cfg:
            logger.error("[Amo] missing %s in config", k)
            return False
    payload = {
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "grant_type": "refresh_token",
        "refresh_token": state.tokens.get("refresh_token", ""),
        "redirect_uri": cfg["redirect_uri"],
    }
    async with aiohttp.ClientSession() as ses:
        async with ses.post(
            f"https://{cfg['domain']}/oauth2/access_token", json=payload
        ) as resp:
            if resp.status != 200:
                logger.error("[Amo] refresh HTTP %d – %s", resp.status, await resp.text())
                return False
            data = await resp.json()
            data["expires_at"] = (
                datetime.now() + timedelta(seconds=data.get("expires_in", 86400))
            ).timestamp()
            state.tokens.update(data)
            await _save_json(TOKENS_FILE, state.tokens)
            logger.info("[Amo] token refreshed, expires %s", data["expires_at"])
            return True

# ════════════════════════════════════════════════════════════════════
# 2. LOW-LEVEL HTTP
# ════════════════════════════════════════════════════════════════════
async def _auth_headers() -> Dict[str, str]:
    return {"Authorization": f"{state.tokens['token_type']} {state.tokens['access_token']}"}


async def _patch_deal(deal_id: int, payload: Dict[str, Any]) -> bool:
    if not await ensure_valid_token():
        return False
    url = f"https://{state.config['domain']}/api/v4/leads/{deal_id}"
    while True:
        async with aiohttp.ClientSession() as ses:
            async with ses.patch(url, headers=await _auth_headers(), json=payload) as resp:
                if resp.status == 429:
                    await asyncio.sleep(1)
                    continue
                if resp.status == 401 and await refresh_amocrm_token():
                    continue
                if resp.status != 200:
                    logger.error("[Amo] patch %d HTTP %d – %s",
                                 deal_id, resp.status, await resp.text())
                    return False
                return True

# ════════════════════════════════════════════════════════════════════
# 3. СПРАВОЧНИКИ
# ════════════════════════════════════════════════════════════════════
async def get_pipeline_stages() -> Dict[str, str]:
    if not await ensure_valid_token() or "domain" not in (state.config or {}):
        return {}
    async with aiohttp.ClientSession() as ses:
        async with ses.get(
            f"https://{state.config['domain']}/api/v4/leads/pipelines",
            headers=await _auth_headers(),
        ) as resp:
            if resp.status != 200:
                return {}
            js = await resp.json()
            return {
                str(st["id"]): st["name"]
                for pl in js["_embedded"]["pipelines"]
                for st in pl["_embedded"]["statuses"]
            }


async def get_custom_fields() -> Dict[str, str]:
    if not await ensure_valid_token() or "domain" not in (state.config or {}):
        return {}
    async with aiohttp.ClientSession() as ses:
        async with ses.get(
            f"https://{state.config['domain']}/api/v4/leads/custom_fields",
            headers=await _auth_headers(),
        ) as resp:
            if resp.status != 200:
                return {}
            js = await resp.json()
            return {str(f["id"]): f["name"] for f in js["_embedded"]["custom_fields"]}

# ════════════════════════════════════════════════════════════════════
# 4. PARSE / NORMALIZE
# ════════════════════════════════════════════════════════════════════
async def _parse_event_datetime(raw_date: Any, raw_time: Any) -> Optional[datetime]:
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
        cf.get(AMOF["event_date"], [None])[0],
        cf.get(AMOF["event_time"], [""])[0],
    )
    if not event_dt:
        return None
    event_time = cf.get(AMOF["event_time"], [""])[0] or event_dt.strftime("%H:%M")

    def to_int(v: Any) -> int:
        try:
            return int(float(str(v).replace(",", ".")))
        except Exception:
            return 0

    total = raw.get("price", 0)
    pre = to_int(cf.get(AMOF["prepayment"], [0])[0])

    try:
        from core.db import get_user_info  # type: ignore
    except ModuleNotFoundError:
        get_user_info = lambda _id: {"first_name": "Unknown"}  # type: ignore

    leads = [
        {
            "id": lid,
            "name": (get_user_info(int(lid)) or {}).get("first_name", "Unknown"),
        }
        for lid in map(str, cf.get(AMOF["team_leads"], []))
        if lid.strip()
    ]

    return {
        "id": did,
        "name": raw.get("name", f"Сделка {did}"),
        "game_name": cf.get(AMOF["game_name"], [""])[0],
        "status_id": str(raw.get("status_id", "")),
        "status": stages.get(str(raw.get("status_id", "")), "Неизвестно"),
        "event_datetime": event_dt,
        "event_time": event_time,
        "players": cf.get(AMOF["players"], [""])[0],
        "players_count": parse_players_count(cf.get(AMOF["players"], [""])[0]),
        "age": cf.get(AMOF["age"], [""])[0],
        "package": cf.get(AMOF["package"], [""])[0],
        "extra_services": ", ".join(cf.get(AMOF["extra_services"], [])),
        "comment": cf.get(AMOF["comment"], [""])[0],
        "total_budget": total,
        "prepayment": pre,
        "to_calculate": total - pre,
        "team_leads": leads,
        "photographer": cf.get(AMOF["photographer"], [""])[0],
    }

# ════════════════════════════════════════════════════════════════════
# 5. PUBLIC API
# ════════════════════════════════════════════════════════════════════
async def update_amocrm_tags(deals_tags: Dict[str, Dict[str, str]]) -> bool:
    if not deals_tags:
        return True
    if "domain" not in (state.config or {}):   # offline stub
        return True

    tasks = []
    for deal_id, tag_map in deals_tags.items():
        payload = {"_embedded": {"tags": [{"name": t} for t in tag_map.values() if t]}}
        tasks.append(_patch_deal(int(deal_id), payload))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return all(isinstance(r, bool) and r for r in results)


async def update_deal_status(deal_id: int, status_id: str) -> bool:
    """
    Переводит сделку в новый статус (например «Завершение сделки»).
    Используется handlers.confirmations после подтверждения всех ведущих.
    """
    if "domain" not in (state.config or {}):
        return True  # offline-stub: считаем успешно
    payload = {"status_id": int(status_id)}
    return await _patch_deal(deal_id, payload)


async def fetch_amocrm_deals() -> List[Dict[str, Any]]:
    # offline-fallback
    if "domain" not in (state.config or {}):
        stages = {"100": "Бронь"}
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

    if not await ensure_valid_token():
        return []
    stages = await get_pipeline_stages()
    deals: List[Dict[str, Any]] = []

    page, limit = 1, 250
    updated_from = int((datetime.now() - timedelta(days=DATE_FILTER_DAYS)).timestamp())
    async with aiohttp.ClientSession() as ses:
        while True:
            params = {
                "with": "contacts",
                "limit": limit,
                "page": page,
                "filter[updated_at][from]": updated_from,
            }
            async with ses.get(
                f"https://{state.config['domain']}/api/v4/leads",
                headers=await _auth_headers(),
                params=params,
            ) as resp:
                if resp.status != 200:
                    logger.error("[Amo] deals page %d HTTP %d", page, resp.status)
                    break
                js = await resp.json()
                leads = js.get("_embedded", {}).get("leads", [])
                if not leads:
                    break
                for raw in leads:
                    d = await _process_deal(raw, stages)
                    if d:
                        deals.append(d)
                page += 1
                await asyncio.sleep(0.2)
    deals.sort(key=lambda d: d["event_datetime"])
    return deals


async def get_amocrm_deals() -> List[Dict[str, Any]]:
    try:
        return await fetch_amocrm_deals()
    except Exception as exc:
        logger.exception("[Amo] get_amocrm_deals failed: %s", exc)
        return []

# ════════════════════════════════════════════════════════════════════
# 6. SELF-TEST
# ════════════════════════════════════════════════════════════════════
async def _test() -> None:
    deals = await get_amocrm_deals()
    assert deals and deals[0]["players_count"] == 8
    assert deals[0]["event_datetime"].strftime("%Y-%m-%d %H:%M") == "2025-08-15 18:00"
    assert await update_amocrm_tags({"1": {"lead1": "Иван|1"}})
    assert await update_deal_status(1, settings.SUCCESSFUL_STATUS_ID)
    print("services.amocrm ✅ self-tests passed")

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(_test())
