# services/gsheets.py
# ─────────────────────────────────────────────────────────────────────────────
# Google Sheets – «Светофор»
# Кешируем данные на 24 часа, чтобы не дергать API при каждом запросе.
# ─────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
from typing import Optional, Dict, List, Any
from datetime import datetime, timedelta
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from core.config import settings

logger = logging.getLogger(__name__)

# Модуль-уровневый кеш метаданных (sheet, service, spreadsheet_id, rows, headers, last_refresh)
_svetofor_cache: Dict[str, Any] = {
    "sheet": None,           # gspread.Worksheet
    "service": None,         # Google Sheets API client
    "spreadsheet_id": None,  # используемый ID
    "rows": None,            # list[rowData]
    "headers": None,         # list[str]
    "last_refresh": None,    # datetime последней загрузки
}


def _init_svetofor() -> None:
    """
    Инициализирует gspread и Google Sheets API client единожды.
    Проверяет наличие настроек, подбирает scopes по умолчанию, если в конфиге пусто.
    """
    if _svetofor_cache["sheet"] is None:
        sid = settings.SVETOFOR_SPREAD_ID
        if not sid:
            raise RuntimeError("[gsheets] SVETOFOR_SPREAD_ID не задан в настройках")

        creds_file = settings.GOOGLE_CREDENTIALS_FILE
        if not creds_file:
            raise RuntimeError("[gsheets] GOOGLE_CREDENTIALS_FILE не задан в настройках")

        # если scopes не сконфигурированы, используем чтение таблиц по-умолчанию
        scopes: List[str] = settings.GOOGLE_SHEETS_SCOPES or [
            "https://www.googleapis.com/auth/spreadsheets.readonly"
        ]

        creds = Credentials.from_service_account_file(creds_file, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(sid).sheet1
        service = build("sheets", "v4", credentials=creds)

        _svetofor_cache.update({
            "sheet": sheet,
            "service": service,
            "spreadsheet_id": sid,
            "rows": None,
            "headers": None,
            "last_refresh": None,
        })


def _load_svetofor_data() -> None:
    """
    Загружает данные includeGridData и сохраняет строки и заголовки.
    Перезагружает не чаще чем раз в 24 часа.
    """
    now = datetime.now()
    last = _svetofor_cache.get("last_refresh")
    # Если уже загружено и прошло меньше суток — пропускаем
    if _svetofor_cache["rows"] is not None and last and now - last < timedelta(hours=24):
        return

    service = _svetofor_cache["service"]
    sid = _svetofor_cache["spreadsheet_id"]
    meta = service.spreadsheets().get(spreadsheetId=sid, includeGridData=True).execute()

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

    _svetofor_cache["rows"] = rows
    _svetofor_cache["headers"] = headers
    _svetofor_cache["last_refresh"] = now


def _rgb_to_status(r: float, g: float, b: float) -> str:
    """
    Преобразует RGB в один из статусов:
      • 'green', 'yellow', 'red' или ''.
    """
    if g > 0.5 and r < 0.5:
        return "green"
    if r > 0.5 and g > 0.5:
        return "yellow"
    if r > 0.5 and g < 0.5:
        return "red"
    return ""


async def get_user_row_from_svetofor(user_id: int) -> Optional[int]:
    """
    Ищет user_id (как текст) во втором столбце (index 1)
    и возвращает номер строки (1-based), либо None.
    """
    _init_svetofor()
    _load_svetofor_data()
    rows = _svetofor_cache["rows"]
    target = str(user_id)
    for idx, row in enumerate(rows, start=1):
        vals = row.get("values", [])
        if len(vals) > 1 and vals[1].get("formattedValue", "").strip() == target:
            return idx
    return None


async def get_game_column_from_svetofor(game_name: str) -> Optional[int]:
    """
    Ищет название игры в заголовках (первой строке, без учёта регистра)
    и возвращает номер столбца (1-based), либо None.
    """
    _init_svetofor()
    _load_svetofor_data()
    norm = game_name.strip().lower()
    for idx, title in enumerate(_svetofor_cache["headers"], start=1):
        if title.lower() == norm:
            return idx
    return None


async def get_user_status_from_svetofor(user_id: int, game_name: str) -> str:
    """
    Возвращает 'green'|'yellow'|'red'|'' по состоянию ячейки.
    Использует суточный кеш для таблицы.
    """
    _init_svetofor()
    _load_svetofor_data()
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


async def _test():
    """Простейший тест: без ошибок при несуществующем юзере/игре."""
    assert await get_user_status_from_svetofor(0, "неизвестно") == ""
    print("gsheets tests passed")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_test())
