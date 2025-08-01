"""
AmoCRM API client — обновление токена, чтение сделок, PATCH тегов.
Полностью повторяет логику исходника 12.92, но без использования Redis.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Union

import aiohttp

from core.config import settings
from core.state import state

logger = logging.getLogger(__name__)

TOKENS_FILE = settings.TOKENS_FILE
DATE_FILTER_DAYS = settings.DATE_FILTER_DAYS

# ——— вспомогательные IO‑утилиты ——————————————————————————————
async def _load_json(path: str) -> Dict:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

async def _save_json(path: str, data: Dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ——— token helpers ——————————————————————————————————————————————
async def ensure_valid_token() -> bool:
    """
    Проверяет, жив ли access-token; при необходимости обновляет.
    """
    if not state.tokens:
        state.tokens = await _load_json(TOKENS_FILE)
        if not state.tokens:
            logger.error("[Amo] Empty tokens file %s", TOKENS_FILE)
            return False

    expires_at = state.tokens.get("expires_at", 0)
    if datetime.now().timestamp() >= expires_at - 300:
        return await refresh_amocrm_token()
    return True

async def refresh_amocrm_token() -> bool:
    """
    Обновляет refresh-токен и сохраняет.
    """
    cfg = state.config or {}
    required = ("client_id", "client_secret", "redirect_uri", "domain")
    if any(k not in cfg for k in required):
        logger.error("[Amo] Incomplete config for token refresh: %s", cfg)
        return False

    async with aiohttp.ClientSession() as ses:
        payload = {
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "grant_type": "refresh_token",
            "refresh_token": state.tokens.get("refresh_token", ""),
            "redirect_uri": cfg["redirect_uri"],
        }
        async with ses.post(
            f"https://{cfg['domain']}/oauth2/access_token", json=payload
        ) as resp:
            if resp.status != 200:
                logger.error("[Amo] refresh_token HTTP %d – %s", resp.status, await resp.text())
                return False
            data = await resp.json()
            data["expires_at"] = (
                datetime.now() + timedelta(seconds=data.get("expires_in", 86400))
            ).timestamp()
            state.tokens.update(data)
            await _save_json(TOKENS_FILE, state.tokens)
            logger.info("[Amo] token refreshed, expires: %s", data["expires_at"])
            return True

# ——— helpers for common GETs ————————————————————————————————
async def _auth_headers() -> Dict[str, str]:
    return {"Authorization": f"{state.tokens['token_type']} {state.tokens['access_token']}"}

async def get_pipeline_stages() -> Dict[str, str]:
    """
    Возвращает ID → Название статусов всех воронок.
    Без кэширования.
    """
    if not await ensure_valid_token():
        return {}
    async with aiohttp.ClientSession() as ses:
        async with ses.get(
            f"https://{state.config['domain']}/api/v4/leads/pipelines",
            headers=await _auth_headers(),
        ) as resp:
            if resp.status != 200:
                logger.error("[Amo] pipelines HTTP %d", resp.status)
                return {}
            js = await resp.json()
            return {
                str(st["id"]): st["name"]
                for pl in js["_embedded"]["pipelines"]
                for st in pl["_embedded"]["statuses"]
            }

async def get_custom_fields() -> Dict[str, str]:
    """
    Возвращает ID → Название всех кастомных полей сделок.
    Без кэширования.
    """
    if not await ensure_valid_token():
        return {}
    async with aiohttp.ClientSession() as ses:
        async with ses.get(
            f"https://{state.config['domain']}/api/v4/leads/custom_fields",
            headers=await _auth_headers(),
        ) as resp:
            if resp.status != 200:
                logger.error("[Amo] custom_fields HTTP %d", resp.status)
                return {}
            js = await resp.json()
            return {str(f["id"]): f["name"] for f in js["_embedded"]["custom_fields"]}

# ——— deals ——————————————————————————————————————————————
async def _process_deal(
    deal: Dict,
    stages: Dict[str, str],
    custom_fields: Dict[str, str],
) -> Optional[Dict]:
    """
    Приводит «сырую» сделку к унифицированному dict (копия исходника).
    """
    from core.utils import parse_players_count  # локальный импорт, чтобы избежать циклов
    from pytz import timezone
    MSK_TZ = timezone("Europe/Moscow")

    def _parse_event_datetime(raw_date: Any, raw_time: Any):
        from datetime import datetime as _dt

        try:
            ts = float(raw_date)
            if ts > 1e12:
                ts /= 1000
            return MSK_TZ.localize(_dt.fromtimestamp(int(ts)))
        except (TypeError, ValueError):
            pass

        date_str = str(raw_date or "").strip()
        time_str = str(raw_time or "").strip()
        if time_str.lower() == "не указано":
            time_str = ""
        candidate = f"{date_str} {time_str}".strip()
        for fmt in (
            "%Y-%m-%d %H:%M",
            "%d.%m.%Y %H:%M",
            "%d/%m/%Y %H:%M",
            "%Y.%m.%d %H:%M",
            "%Y-%m-%d",
            "%d.%m.%Y",
            "%d/%m/%Y",
            "%Y.%m.%d",
        ):
            try:
                dt = _dt.strptime(candidate, fmt)
                if " %H:%M" not in fmt:
                    dt = dt.replace(hour=0, minute=0)
                return MSK_TZ.localize(dt)
            except ValueError:
                continue
        return None

    AMOCRM_FIELDS = settings.AMOCRM_FIELDS  # тянут из config.json
    deal_id = deal.get("id")
    if not deal_id:
        return None

    try:
        cf_raw = deal.get("custom_fields_values") or []
        cf = {
            str(f.get("field_id")): [v["value"] for v in f.get("values", []) if v.get("value") is not None]
            for f in cf_raw
        }

        raw_date = cf.get(AMOCRM_FIELDS["event_date"], [None])[0]
        raw_time = cf.get(AMOCRM_FIELDS["event_time"], [""])[0]
        event_dt = _parse_event_datetime(raw_date, raw_time)
        if not event_dt:
            return None
        event_time = (
            str(raw_time).strip()
            if raw_time and str(raw_time).strip().lower() != "не указано"
            else event_dt.strftime("%H:%M")
        )

        def to_int(val, default=0):
            try:
                return int(float(str(val).replace(",", ".")))
            except (TypeError, ValueError):
                return default

        total_budget = deal.get("price", 0)
        prepayment = to_int(cf.get(AMOCRM_FIELDS["prepayment"], [0])[0])
        to_calculate = total_budget - prepayment

        leads_raw = cf.get(AMOCRM_FIELDS["team_leads"], [])
        from core.db import get_user_info
        team_leads = [
            {
                "id": lid,
                "name": (get_user_info(int(lid)) or {}).get("first_name", "Unknown"),
            }
            for lid in map(str, leads_raw)
            if lid.strip()
        ]

        pinata_list = cf.get(AMOCRM_FIELDS["extra_services"], [])
        other_services = cf.get("413875", [])
        all_services = pinata_list + other_services
        extra_services = ", ".join(map(str, all_services)) if all_services else ""

        package_list = cf.get(AMOCRM_FIELDS["package"], [""])
        package = str(package_list[0]) if package_list else ""

        processed = {
            "id": deal_id,
            "name": deal.get("name", f"Сделка {deal_id}"),
            "game_name": cf.get(AMOCRM_FIELDS["game_name"], [""])[0],
            "status_id": str(deal.get("status_id", "")),
            "status": stages.get(str(deal.get("status_id", "")), "Неизвестно"),
            "event_datetime": event_dt,
            "event_datetime_str": f"{event_dt.strftime('%d.%m.%Y')}, {event_time}",
            "event_time": event_time,
            "players": cf.get(AMOCRM_FIELDS["players"], [""])[0],
            "players_count": parse_players_count(cf.get(AMOCRM_FIELDS["players"], [""])[0]),
            "age": cf.get(AMOCRM_FIELDS["age"], [""])[0],
            "package": package,
            "extra_services": extra_services,
            "comment": cf.get(AMOCRM_FIELDS["comment"], [""])[0] or "",
            "total_budget": total_budget,
            "prepayment": prepayment,
            "to_calculate": to_calculate,
            "team_leads": team_leads,
            "photographer": cf.get(AMOCRM_FIELDS["photographer"], [""])[0] or "",
        }
        return processed
    except Exception as e:
        logger.exception("[Amo] process_deal error %s: %s", deal_id, e)
        return None

async def fetch_amocrm_deals(spreadsheet_id: str) -> List[Dict]:
    """
    Загружает сделки из AmoCRM с фильтрацией и пагинацией.
    """
    if not await ensure_valid_token():
        return []

    custom_fields, stages = await asyncio.gather(
        get_custom_fields(), get_pipeline_stages()
    )
    if not custom_fields or not stages:
        return []

    deals: List[Dict] = []
    page, limit = 1, 250
    updated_from = int(
        (datetime.now() - timedelta(days=settings.DATE_FILTER_DAYS)).timestamp()
    )
    pipeline_id = state.config.get("pipeline_id")

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as ses:
        while True:
            params: Dict[str, Union[str, int]] = {
                "with": "contacts,tags",
                "limit": limit,
                "page": page,
                "filter[updated_at][from]": updated_from,
            }
            if pipeline_id:
                params["filter[pipeline_id]"] = pipeline_id

            for i, sid in enumerate(settings.ALLOWED_STATUS_IDS):
                params[f"filter[statuses][{i}][pipeline_id][0][statuses][0]"] = sid

            async with ses.get(
                f"https://{state.config['domain']}/api/v4/leads",
                headers=await _auth_headers(),
                params=params,
            ) as resp:
                if resp.status in (204, 404):
                    break
                if resp.status == 401 and await refresh_amocrm_token():
                    continue
                if resp.status != 200:
                    logger.error("[Amo] deals page %d HTTP %d – %s", page, resp.status, await resp.text())
                    return []

                js = await resp.json()
                leads = js.get("_embedded", {}).get("leads", [])
                if not leads:
                    break

                for raw in leads:
                    if str(raw.get("status_id")) in settings.ALLOWED_STATUS_IDS:
                        deal = await _process_deal(raw, stages, custom_fields)
                        if deal:
                            deals.append(deal)

                page += 1
                await asyncio.sleep(1)  # soft-limit

    deals.sort(key=lambda d: d["event_datetime"])
    return deals

async def get_amocrm_deals(spreadsheet_id: str) -> List[Dict]:
    """
    Возвращает сделки из AmoCRM.
    """
    try:
        deals = await fetch_amocrm_deals(spreadsheet_id)
    except Exception as e:
        logger.error("[Amo] Failed to fetch deals: %s", e, exc_info=True)
        deals = []
    return deals

# ——— tags PATCH —————————————————————————————————————————————
async def update_amocrm_tags(deal_id: int, tags: List[str]) -> bool:
    """
    Обновляет теги сделки.
    """
    if not await ensure_valid_token():
        return False

    payload = {"_embedded": {"tags": [{"name": t} for t in tags]}}
    async with aiohttp.ClientSession() as ses:
        async with ses.patch(
            f"https://{state.config['domain']}/api/v4/leads/{deal_id}",
            headers=await _auth_headers(),
            json=payload,
        ) as resp:
            if resp.status == 429:
                await asyncio.sleep(1)
                return await update_amocrm_tags(deal_id, tags)
            if resp.status != 200:
                logger.error("[Amo] update_tags %d HTTP %d – %s", deal_id, resp.status, await resp.text())
                return False
            logger.info("[Amo] tags updated for deal %d → %s", deal_id, tags)
            return True