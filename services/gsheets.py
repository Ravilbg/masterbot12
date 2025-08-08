# services/gsheets.py — Google Sheets «Светофор»
# ─────────────────────────────────────────────────────────────────────
"""
Статусы ведущих (‘green’ | ‘yellow’ | ‘red’) читаются из листа «Светофор».

• Метаданные листа кешируются на 24 ч, чтобы не бомбить API.
• При любой сетевой/аутентификационной ошибке функции возвращают '',
  а предупреждение пишется в лог. Конфигурационные ошибки (нет SID /
  service-account JSON) вызывают RuntimeError — их нужно чинить.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from google.auth.exceptions import RefreshError, GoogleAuthError

from core.config import settings

logger = logging.getLogger(__name__)

__all__ = [
    "get_user_status_from_svetofor",
    "get_user_row_from_svetofor",
    "get_game_column_from_svetofor",
]

# ███ [GLOBAL CACHE] ────────────────────────────────────────────────
_svetofor_cache: Dict[str, Any] = {
    "sheet": None,            # gspread.Worksheet
    "service": None,          # Sheets API client
    "spreadsheet_id": None,   # str
    "rows": None,             # list[rowData]
    "headers": None,          # list[str]
    "last_refresh": None,     # datetime
}

_CACHE_TTL_HOURS = 24


# ███ [1] INITIALIZATION ────────────────────────────────────────────
def _init_svetofor() -> None:
    """
    Однократная инициализация gspread + Sheets API.

    Конфигурационные ошибки (пустой SID / creds) → RuntimeError,
    остальные исключения ловятся и логируются, но sheet остаётся None.
    """
    if _svetofor_cache["sheet"] is not None:
        return  # уже инициализировано

    sid = settings.SVETOFOR_SPREAD_ID
    creds_file = settings.GOOGLE_CREDENTIALS_FILE
    if not sid:
        raise RuntimeError("[gsheets] SVETOFOR_SPREAD_ID не задан в настройках")
    if not creds_file:
        raise RuntimeError("[gsheets] GOOGLE_CREDENTIALS_FILE не задан в настройках")

    scopes: List[str] = settings.GOOGLE_SHEETS_SCOPES or [
        "https://www.googleapis.com/auth/spreadsheets.readonly"
    ]

    try:
        creds = Credentials.from_service_account_file(creds_file, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(sid).sheet1
        service = build("sheets", "v4", credentials=creds)

        _svetofor_cache.update(
            sheet=sheet,
            service=service,
            spreadsheet_id=sid,
            rows=None,
            headers=None,
            last_refresh=None,
        )
        logger.info("[gsheets] init OK, sheet id=%s", sheet.id)
    except (RefreshError, GoogleAuthError, Exception) as exc:      # noqa: BLE001
        # Ловим любые проблемы авторизации/сети, но не роняем бот
        logger.warning("[gsheets] init failed: %s — статусы будут пустыми", exc)
        # sheet остаётся None → вызовы вернут ''


# ███ [2] DATA LOADING ──────────────────────────────────────────────
def _load_svetofor_data() -> None:
    """
    Загружает includeGridData → rows + headers, не чаще 1 раза в сутки.
    При ошибке записывает предупреждение и оставляет rows=None.
    """
    if _svetofor_cache["sheet"] is None:
        return  # не инициализировано / ошибка авторизации

    now = datetime.now()
    last: datetime | None = _svetofor_cache["last_refresh"]
    if _svetofor_cache["rows"] is not None and last and now - last < timedelta(hours=_CACHE_TTL_HOURS):
        return  # кеш ещё свежий

    try:
        service = _svetofor_cache["service"]
        sid = _svetofor_cache["spreadsheet_id"]
        meta = (
            service.spreadsheets()
            .get(spreadsheetId=sid, includeGridData=True)
            .execute()
        )

        sheet_id = _svetofor_cache["sheet"].id
        sheet_meta = next(
            s for s in meta["sheets"]
            if s["properties"]["sheetId"] == sheet_id
        )
        rows = sheet_meta["data"][0].get("rowData", [])
        headers = [
            c.get("formattedValue", "").strip()
            for c in rows[0].get("values", [])
        ]

        _svetofor_cache.update(rows=rows, headers=headers, last_refresh=now)
        logger.debug("[gsheets] data refreshed, %d rows", len(rows))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[gsheets] load failed: %s — статусы будут пустыми", exc)
        _svetofor_cache.update(rows=None, headers=None, last_refresh=None)


# ███ [3] HELPERS ───────────────────────────────────────────────────
def _rgb_to_status(r: float, g: float, b: float) -> str:
    """RGB → 'green' | 'yellow' | 'red' | ''."""
    if g > 0.5 and r < 0.5:
        return "green"
    if r > 0.5 and g > 0.5:
        return "yellow"
    if r > 0.5 and g < 0.5:
        return "red"
    return ""


async def get_user_row_from_svetofor(user_id: int) -> Optional[int]:
    """Ищет user_id во втором столбце и возвращает 1-based row или None."""
    _init_svetofor()
    _load_svetofor_data()
    rows = _svetofor_cache["rows"]
    if rows is None:
        return None

    target = str(user_id)
    for idx, row in enumerate(rows, start=1):
        vals = row.get("values", [])
        if len(vals) > 1 and vals[1].get("formattedValue", "").strip() == target:
            return idx
    return None


async def get_game_column_from_svetofor(game_name: str) -> Optional[int]:
    """Ищет game_name в заголовках и возвращает 1-based col или None."""
    _init_svetofor()
    _load_svetofor_data()
    headers = _svetofor_cache["headers"]
    if headers is None:
        return None

    norm = game_name.strip().lower()
    for idx, title in enumerate(headers, start=1):
        if title.lower() == norm:
            return idx
    return None


# ███ [4] PUBLIC API ────────────────────────────────────────────────
async def get_user_status_from_svetofor(user_id: int, game_name: str) -> str:
    """
    Возвращает 'green' | 'yellow' | 'red' | ''.

    • При недоступной таблице или ошибке чтения — ''.
    • Кеш таблицы обновляется раз в 24 ч.
    """
    try:
        _init_svetofor()
        _load_svetofor_data()

        if _svetofor_cache["rows"] is None:
            return ""  # таблица недоступна

        row = await get_user_row_from_svetofor(user_id)
        col = await get_game_column_from_svetofor(game_name)
        if not row or not col:
            return ""

        cell = _svetofor_cache["rows"][row - 1].get("values", [])[col - 1]
        bg = cell.get("effectiveFormat", {}).get("backgroundColor", {})
        return _rgb_to_status(
            bg.get("red", 0.0),
            bg.get("green", 0.0),
            bg.get("blue", 0.0),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[gsheets] status fetch failed: %s", exc)
        return ""


# ███ [5] TESTS ─────────────────────────────────────────────────────
async def _test() -> None:
    """Smoke-тест: функция работает без падений при любой конфигурации."""
    assert await get_user_status_from_svetofor(0, "Fake") in {"", "green", "yellow", "red"}
    print("gsheets ✅ tests passed")


if __name__ == "__main__":
    import asyncio

    asyncio.run(_test())
