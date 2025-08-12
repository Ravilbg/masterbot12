# services/gsheets.py — Google Sheets «Светофор»
# ─────────────────────────────────────────────────────────────────────
"""
Статусы ведущих ('green' | 'yellow' | 'red') читаются из листа «Светофор».

Версия 6.4 · 2025-08-10
──────────────────────────────────────────────────────────────────────
• Метаданные листа кешируются на 24 часа (headers + rowData).
• При сетевых/аутентификационных ошибках функции возвращают '' (пусто),
  а предупреждение уходит в лог. Конфигурационные ошибки (нет SID / creds)
  поднимаются как RuntimeError — их надо исправлять.
• Автопочинка скоупов: принудительно используем spreadsheets.readonly при
  любом «invalid_scope», чтобы таблица не «падала».
• Устойчивый разбор цвета: effectiveFormat.backgroundColor /
  effectiveFormat.backgroundColorStyle.rgbColor / userEnteredFormat.*.
• Хелперы для логики распределения: status_to_role / suggest_role_from_svetofor.

Экспорт:
    get_user_status_from_svetofor(user_id: int, game_name: str) -> str
    get_user_row_from_svetofor(user_id: int) -> Optional[int]
    get_game_column_from_svetofor(game_name: str) -> Optional[int]
    status_to_role(status: str, need: dict, team: dict) -> Optional[str]
    suggest_role_from_svetofor(user_id: int, game_name: str, need: dict, team: dict) -> Optional[str]
    GAME_ROLE_MAPPING: Dict[str, Dict[str, int]]
"""

from __future__ import annotations

# ███ [0] IMPORTS
# --------------------------------------------------------------------
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from google.auth.exceptions import RefreshError, GoogleAuthError

from core.config import settings

logger = logging.getLogger(__name__)

# Публичный экспорт (для совместимости с handlers.confirmations и др.)
GAME_ROLE_MAPPING: Dict[str, Dict[str, int]] = getattr(settings, "GAME_ROLE_MAPPING", {})

__all__ = [
    "get_user_status_from_svetofor",
    "get_user_row_from_svetofor",
    "get_game_column_from_svetofor",
    "status_to_role",
    "suggest_role_from_svetofor",
    "GAME_ROLE_MAPPING",
]

# ███ [GLOBAL CACHE] ────────────────────────────────────────────────
_svetofor_cache: Dict[str, Any] = {
    "sheet": None,            # gspread.Worksheet
    "service": None,          # Google Sheets API client
    "spreadsheet_id": None,   # str
    "rows": None,             # list[rowData]
    "headers": None,          # list[str]
    "last_refresh": None,     # datetime
    "scopes": None,           # list[str]
}

_CACHE_TTL_HOURS = 24


# ███ [1] INITIALIZATION ────────────────────────────────────────────
def _normalize_scopes(raw: Any) -> List[str]:
    """Возвращает валидный список скоупов spreadsheets.*, иначе readonly."""
    readonly = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    if not raw:
        return readonly
    if isinstance(raw, str):
        scopes = [s.strip() for s in raw.split(",") if s.strip()]
    elif isinstance(raw, (list, tuple)):
        scopes = [str(s).strip() for s in raw if str(s).strip()]
    else:
        return readonly
    allowed_prefix = "https://www.googleapis.com/auth/spreadsheets"
    if not scopes or any(not s.startswith(allowed_prefix) for s in scopes):
        return readonly
    return scopes


def _init_svetofor() -> None:
    """
    Однократная инициализация gspread + Sheets API.

    Конфигурационные ошибки (пустой SID / creds) → RuntimeError,
    остальные исключения ловятся и логируются, но sheet остаётся None.
    При invalid_scope выполняется повторная попытка с readonly.
    """
    if _svetofor_cache["sheet"] is not None:
        return  # уже инициализировано

    sid = getattr(settings, "SVETOFOR_SPREAD_ID", "")
    creds_file = getattr(settings, "GOOGLE_CREDENTIALS_FILE", "")
    if not sid:
        raise RuntimeError("[gsheets] SVETOFOR_SPREAD_ID не задан в настройках")
    if not creds_file:
        raise RuntimeError("[gsheets] GOOGLE_CREDENTIALS_FILE не задан в настройках")

    scopes = _normalize_scopes(getattr(settings, "GOOGLE_SHEETS_SCOPES", None))

    def _init_with_scopes(scps: List[str]) -> None:
        creds = Credentials.from_service_account_file(creds_file, scopes=scps)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(sid).sheet1
        service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        _svetofor_cache.update(
            sheet=sheet,
            service=service,
            spreadsheet_id=sid,
            rows=None,
            headers=None,
            last_refresh=None,
            scopes=scps,
        )
        logger.info("[gsheets] init OK, sheet id=%s, scopes=%s", sheet.id, scps)

    try:
        _init_with_scopes(scopes)
    except (RefreshError, GoogleAuthError) as exc:
        msg = str(exc)
        logger.warning("[gsheets] init failed: %s", msg)
        # если проблема со скоупами — повторим с readonly
        if "invalid_scope" in msg or "Invalid OAuth scope" in msg:
            try:
                safe = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
                _init_with_scopes(safe)
                logger.info("[gsheets] re-init with readonly scope succeeded")
            except Exception as exc2:  # noqa: BLE001
                logger.warning("[gsheets] re-init failed: %s — статусы будут пустыми", exc2)
        # sheet остаётся None → вызовы вернут ''
    except Exception as exc:  # noqa: BLE001
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
    last: Optional[datetime] = _svetofor_cache["last_refresh"]
    if (
        _svetofor_cache["rows"] is not None
        and last is not None
        and now - last < timedelta(hours=_CACHE_TTL_HOURS)
    ):
        return  # кеш ещё свежий

    try:
        service = _svetofor_cache["service"]
        sid = _svetofor_cache["spreadsheet_id"]
        meta = (
            service.spreadsheets()  # type: ignore[attr-defined]
            .get(spreadsheetId=sid, includeGridData=True)
            .execute()
        )

        sheet_id = _svetofor_cache["sheet"].id  # type: ignore[union-attr]
        sheet_meta = next(
            (s for s in meta.get("sheets", []) if s.get("properties", {}).get("sheetId") == sheet_id),
            None,
        )
        if not sheet_meta:
            raise RuntimeError(f"sheetId={sheet_id} не найден в spreadsheet")

        data_blocks = sheet_meta.get("data", [])
        rows = (data_blocks[0] or {}).get("rowData", []) if data_blocks else []
        if not rows:
            # пустой лист — это не ошибка, просто нет данных
            _svetofor_cache.update(rows=[], headers=[], last_refresh=now)
            logger.debug("[gsheets] data refreshed: empty sheet")
            return

        # заголовки — первая строка
        header_cells = rows[0].get("values", []) if rows else []
        headers = [(c.get("formattedValue", "") or "").strip() for c in header_cells]

        _svetofor_cache.update(rows=rows, headers=headers, last_refresh=now)
        logger.debug("[gsheets] data refreshed, %d rows, %d headers", len(rows), len(headers))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[gsheets] load failed: %s — статусы будут пустыми", exc)
        _svetofor_cache.update(rows=None, headers=None, last_refresh=None)


# ███ [3] HELPERS ───────────────────────────────────────────────────
def _rgb_to_status(r: float, g: float, b: float) -> str:
    """RGB (0..1) → 'green' | 'yellow' | 'red' | '' (по простому правилу)."""
    # Простой и устойчивый разбор:
    # • зелёный, если зелёный канал доминирует и красный мал
    # • жёлтый, если красный и зелёный вместе велики
    # • красный, если красный велик и зелёный мал
    if g > 0.6 and r < 0.4:
        return "green"
    if r > 0.5 and g > 0.5:
        return "yellow"
    if r > 0.6 and g < 0.4:
        return "red"
    return ""


def _extract_bg_rgb(cell: Dict[str, Any]) -> Tuple[float, float, float]:
    """
    Достаёт цвет из ячейки максимально безопасно:
    • effectiveFormat.backgroundColor
    • effectiveFormat.backgroundColorStyle.rgbColor
    • userEnteredFormat.backgroundColor / backgroundColorStyle.rgbColor
    Возвращает (r,g,b) в [0..1].
    """
    fmt = cell.get("effectiveFormat", {}) or {}
    ufmt = cell.get("userEnteredFormat", {}) or {}
    # 1) effectiveFormat.backgroundColor
    col = fmt.get("backgroundColor")
    if isinstance(col, dict):
        return float(col.get("red", 0.0) or 0.0), float(col.get("green", 0.0) or 0.0), float(col.get("blue", 0.0) or 0.0)
    # 2) effectiveFormat.backgroundColorStyle.rgbColor
    style = fmt.get("backgroundColorStyle", {})
    rgb = style.get("rgbColor", {}) if isinstance(style, dict) else {}
    if isinstance(rgb, dict) and rgb:
        return float(rgb.get("red", 0.0) or 0.0), float(rgb.get("green", 0.0) or 0.0), float(rgb.get("blue", 0.0) or 0.0)
    # 3) userEnteredFormat.*
    col = ufmt.get("backgroundColor")
    if isinstance(col, dict):
        return float(col.get("red", 0.0) or 0.0), float(col.get("green", 0.0) or 0.0), float(col.get("blue", 0.0) or 0.0)
    style = ufmt.get("backgroundColorStyle", {})
    rgb = style.get("rgbColor", {}) if isinstance(style, dict) else {}
    if isinstance(rgb, dict) and rgb:
        return float(rgb.get("red", 0.0) or 0.0), float(rgb.get("green", 0.0) or 0.0), float(rgb.get("blue", 0.0) or 0.0)
    # по умолчанию — белый (без статуса)
    return (1.0, 1.0, 1.0)


# ███ [4] LOW-LEVEL READERS ─────────────────────────────────────────
async def get_user_row_from_svetofor(user_id: int) -> Optional[int]:
    """Ищет user_id во втором столбце и возвращает 1-based row или None."""
    _init_svetofor()
    _load_svetofor_data_alias = _load_svetofor_data  # локальная ссылка для mypy
    _load_svetofor_data_alias()
    rows = _svetofor_cache["rows"]
    if rows is None:
        return None

    target = str(user_id).strip()
    for idx, row in enumerate(rows, start=1):
        vals = row.get("values", []) or []
        if len(vals) > 1:
            v = (vals[1].get("formattedValue", "") or "").strip()
            if v == target:
                return idx
    return None


async def get_game_column_from_svetofor(game_name: str) -> Optional[int]:
    """Ищет game_name в заголовках и возвращает 1-based col или None."""
    _init_svetofor()
    _load_svetofor_data()
    headers = _svetofor_cache["headers"]
    if headers is None:
        return None

    norm = (game_name or "").strip().lower()
    if not norm:
        return None

    for idx, title in enumerate(headers, start=1):
        if (title or "").strip().lower() == norm:
            return idx
    return None


# ███ [5] PUBLIC API ────────────────────────────────────────────────
async def get_user_status_from_svetofor(user_id: int, game_name: str) -> str:
    """
    Возвращает 'green' | 'yellow' | 'red' | ''.

    • При недоступной таблице или ошибке чтения — ''.
    • Кеш таблицы обновляется раз в 24 ч.
    """
    try:
        _init_svetofor()
        _load_svetofor_data()

        rows = _svetofor_cache["rows"]
        if rows is None:
            return ""  # таблица недоступна

        row = await get_user_row_from_svetofor(user_id)
        col = await get_game_column_from_svetofor(game_name)
        if not row or not col:
            return ""

        # Безопасно достаём нужную ячейку
        row_vals: List[Dict[str, Any]] = (rows[row - 1].get("values", []) or [])
        if col - 1 >= len(row_vals):
            return ""
        cell = row_vals[col - 1] or {}

        r, g, b = _extract_bg_rgb(cell)
        return _rgb_to_status(r, g, b)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[gsheets] status fetch failed: %s", exc)
        return ""


# ███ [6] ROLE HELPERS (non-breaking extensions) ────────────────────
def status_to_role(status: str, need: Dict[str, int], team: Dict[str, List[int]]) -> Optional[str]:
    """
    Возвращает целевую роль по цвету статуса c учётом уже занятых слотов.
    Правила (утверждённая логика):
      green  → main (если есть слот), иначе assist (если есть слот), иначе trainee
      yellow → assist (если есть слот), иначе trainee
      red    → trainee
      ''     → None (не решаем за человека)
    """
    status = (status or "").strip().lower()
    main_used = len(team.get("main", []) or [])
    assist_used = len(team.get("assist", []) or [])
    main_need = int(need.get("main", need.get("main_leaders", 1)))
    assist_need = int(need.get("assist", need.get("assistants", 0)))

    if status == "green":
        if main_used < main_need:
            return "main"
        if assist_used < assist_need:
            return "assist"
        return "trainee"
    if status == "yellow":
        if assist_used < assist_need:
            return "assist"
        return "trainee"
    if status == "red":
        return "trainee"
    return None


async def suggest_role_from_svetofor(
    user_id: int,
    game_name: str,
    need: Dict[str, int],
    team: Dict[str, List[int]],
) -> Optional[str]:
    """
    Асинхронный хелпер: читает цвет из «Светофора» и возвращает роль
    согласно утверждённой логике. Возвращает None, если цвет не распознан.
    """
    status = await get_user_status_from_svetofor(user_id, game_name)
    return status_to_role(status, need, team)


# ███ [7] TESTS ─────────────────────────────────────────────────────
async def _test() -> None:
    """Smoke-тест: функции работают без падений при любой конфигурации."""
    # Допускаем пустой результат при недоступной таблице
    s = await get_user_status_from_svetofor(0, "Fake")
    assert s in {"", "green", "yellow", "red"}
    assert isinstance(GAME_ROLE_MAPPING, dict)

    # Проверка маппинга ролей по цвету
    need = {"main": 1, "assist": 1}
    team = {"main": [], "assist": []}
    assert status_to_role("green", need, team) in {"main", "assist", "trainee"}
    team = {"main": [1], "assist": []}
    assert status_to_role("green", need, team) in {"assist", "trainee"}
    assert status_to_role("yellow", need, team) in {"assist", "trainee"}
    assert status_to_role("red", need, team) == "trainee"
    print("gsheets ✅ tests passed")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_test())

# История изменений:
# 2025-08-10 — v6.4: нормализация и автопочинка скоупов; расширения статуса→роль.
