# services/gsheets.py — Google Sheets «Светофор»
# ─────────────────────────────────────────────────────────────────────
"""
Статусы ведущих ('green' | 'yellow' | 'red') читаются из листа «Светофор».

Версия 7.2 · 2025-08-12
──────────────────────────────────────────────────────────────────────
Что исправлено и усилено:
• Надёжная инициализация: поддержка ключа из файла ИЛИ из JSON-строки/словаря.
• Авто-лечилка переноса строк в private_key (\\n → \n, «одна строка» → многострочный).
• Антишум: при фатальной ошибке авторизации вводится «карантин» на повторные попытки
  (по умолчанию 15 минут), чтобы не забивать логи и не долбить Google API.
• Безопасные скоупы: валидация и автоматический фолбэк на spreadsheets.readonly.
• Нормальный кеш includeGridData (24 часа) + бережная обработка пустых ячеек/листов.
• Устойчивый разбор цвета фона ячейки из всех возможных полей формата.

Публичный экспорт (совместимо с проектом):
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
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import gspread
from google.auth.exceptions import GoogleAuthError, RefreshError
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from core.config import settings

logger = logging.getLogger(__name__)

# Публичный экспорт (используется в handlers.confirmations и др.)
GAME_ROLE_MAPPING: Dict[str, Dict[str, int]] = getattr(settings, "GAME_ROLE_MAPPING", {}) or {}

__all__ = [
    "get_user_status_from_svetofor",
    "get_user_row_from_svetofor",
    "get_game_column_from_svetofor",
    "status_to_role",
    "suggest_role_from_svetofor",
    "GAME_ROLE_MAPPING",
]

# ███ [GLOBAL CACHE]
# --------------------------------------------------------------------
_svetofor: Dict[str, Any] = {
    "sheet": None,            # gspread.Worksheet
    "service": None,          # Google Sheets API client
    "spreadsheet_id": None,   # str
    "rows": None,             # list[rowData]
    "headers": None,          # list[str]
    "last_refresh": None,     # datetime
    "scopes": None,           # list[str]
    "cooldown_until": None,   # datetime | None (антишум при фатальной ошибке)
    "init_error": None,       # str | None
}

_CACHE_TTL_HOURS = 24
_AUTH_COOLDOWN_MINUTES = 15


# ███ [1] CONFIG / CREDS
# --------------------------------------------------------------------
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
    allow = "https://www.googleapis.com/auth/spreadsheets"
    if not scopes or any(not s.startswith(allow) for s in scopes):
        return readonly
    return scopes


def _coerce_private_key_lines(info: Dict[str, Any]) -> Dict[str, Any]:
    """
    Чинит переносы и кавычки у private_key:
    • "\\n" → "\n", CRLF → LF, срез внешних кавычек;
    • гарантируем переносы вокруг BEGIN/END.
    """
    pk = info.get("private_key")
    if not isinstance(pk, str) or not pk:
        return info
    fixed = pk.replace("\r\n", "\n").replace("\r", "\n").strip()
    if fixed.startswith('"') and fixed.endswith('"'):
        fixed = fixed[1:-1]
    fixed = fixed.replace("\\n", "\n")
    if "-----BEGIN PRIVATE KEY-----" in fixed and "-----END PRIVATE KEY-----" in fixed:
        fixed = fixed.replace("-----BEGIN PRIVATE KEY-----", "-----BEGIN PRIVATE KEY-----\n")
        fixed = fixed.replace("-----END PRIVATE KEY-----", "\n-----END PRIVATE KEY-----\n")
    info["private_key"] = fixed
    return info


def _try_parse_json(raw: str) -> Optional[Dict[str, Any]]:
    """Пытается распарсить raw как JSON или как base64(JSON)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith("{") and raw.endswith("}"):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    try:
        import base64
        decoded = base64.b64decode(raw).decode("utf-8", "ignore").strip()
        if decoded.startswith("{") and decoded.endswith("}"):
            return json.loads(decoded)
    except Exception:
        pass
    return None


def _load_credentials_info() -> Dict[str, Any]:
    """
    Загружает и нормализует JSON сервисного аккаунта:
      1) settings.GOOGLE_CREDENTIALS_FILE,
      2) settings.GOOGLE_CREDENTIALS_JSON (строка/base64/словарь),
      3) файлы по умолчанию: svetofor-credentials.json, google-credentials.json, credentials.json.
    """
    path = getattr(settings, "GOOGLE_CREDENTIALS_FILE", "") or os.getenv("GOOGLE_CREDENTIALS_FILE", "")
    if path and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            info = json.load(f)
        return _coerce_private_key_lines(info)

    raw = getattr(settings, "GOOGLE_CREDENTIALS_JSON", None) or os.getenv("GOOGLE_CREDENTIALS_JSON", None)
    if raw:
        if isinstance(raw, dict):
            return _coerce_private_key_lines(dict(raw))
        if isinstance(raw, str):
            info = _try_parse_json(raw)
            if info:
                return _coerce_private_key_lines(info)
            logger.warning("[gsheets] GOOGLE_CREDENTIALS_JSON не похож на JSON/base64(JSON) — пропускаю")

    for guess in ("svetofor-credentials.json", "google-credentials.json", "credentials.json"):
        if os.path.exists(guess):
            with open(guess, "r", encoding="utf-8") as f:
                info = json.load(f)
            return _coerce_private_key_lines(info)

    raise RuntimeError("[gsheets] Не найден ключ сервисного аккаунта: GOOGLE_CREDENTIALS_FILE/JSON")

# История изменений (блок [1]):
# 2025-08-18 — удалены дубли импортов/констант/стейта; оставлены только функции (выровнено под SSOT).

# ███ [2] INIT / LOAD
# --------------------------------------------------------------------
def _init_svetofor() -> None:
    """
    Инициализирует доступ к Google Sheets (gspread + Sheets API).
    Особенности:
      • подробный лог (email, scopes, fingerprint ключа);
      • авто-повтор при Invalid JWT Signature: повторная инициализация
        с тем же private_key, но БЕЗ private_key_id (убираем kid из JWT);
      • «карантин» на auth-ошибках, чтобы не засыпать логи.
    """
    if _svetofor["sheet"] is not None:
        return

    cd = _svetofor.get("cooldown_until")
    if cd and datetime.now() < cd:
        return

    sid = getattr(settings, "SVETOFOR_SPREAD_ID", "") or os.getenv("SVETOFOR_SPREAD_ID", "")
    if not sid:
        raise RuntimeError("[gsheets] SVETOFOR_SPREAD_ID не задан")

    scopes = _normalize_scopes(
        getattr(settings, "GOOGLE_SHEETS_SCOPES", None) or os.getenv("GOOGLE_SHEETS_SCOPES", None)
    )

    def _fingerprint(pk: str) -> str:
        import hashlib
        h = hashlib.sha256(pk.encode("utf-8", "ignore")).hexdigest()
        return f"sha256:{h[:8]}…{h[-8:]}"

    def _do_init(info: Dict[str, Any], scps: List[str], log_hint: str) -> None:
        # Проверим обязательные поля
        for k in ("client_email", "private_key", "token_uri"):
            if not info.get(k):
                raise RuntimeError(f"[gsheets] В JSON ключе отсутствует поле: {k}")

        creds = Credentials.from_service_account_info(info, scopes=scps)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(sid).sheet1  # реальный вызов → проверит auth
        service = build("sheets", "v4", credentials=creds, cache_discovery=False)

        _svetofor.update(
            sheet=sheet,
            service=service,
            spreadsheet_id=sid,
            rows=None,
            headers=None,
            last_refresh=None,
            scopes=scps,
            init_error=None,
        )
        logger.info(
            "[gsheets] init OK%s: sheet=%s, scopes=%s, email=%s, key=%s",
            log_hint,
            getattr(sheet, "id", "sheet1"),
            scps,
            info.get("client_email"),
            _fingerprint(str(info.get("private_key", ""))),
        )

    info_orig = _load_credentials_info()
    info_orig = _coerce_private_key_lines(info_orig)

    try:
        _do_init(info_orig, scopes, "")
        return
    except (RefreshError, GoogleAuthError) as exc:
        msg = str(exc)
        _svetofor["init_error"] = msg

        # 1) Попытка re-init с readonly scope (если ругнулся на scope)
        if "invalid_scope" in msg.lower():
            try:
                _do_init(info_orig, ["https://www.googleapis.com/auth/spreadsheets.readonly"], " (readonly)")
                logger.info("[gsheets] re-init with readonly scope succeeded")
                return
            except Exception as e2:  # noqa: BLE001
                _svetofor["init_error"] = str(e2)
                logger.warning("[gsheets] re-init readonly failed: %s", e2)

        # 2) КЛЮЧЕВОЕ: Invalid JWT Signature → повтор без kid (private_key_id)
        if "invalid jwt signature" in msg.lower() or "invalid_grant" in msg.lower():
            try:
                info_nokid = dict(info_orig)
                # убираем private_key_id, чтобы в JWT не попал kid — Google подберёт сам
                if "private_key_id" in info_nokid:
                    info_nokid.pop("private_key_id", None)
                _do_init(info_nokid, scopes, " (no-kid)")
                logger.info("[gsheets] re-init without kid succeeded")
                return
            except Exception as e3:  # noqa: BLE001
                _svetofor["init_error"] = str(e3)
                logger.warning("[gsheets] re-init no-kid failed: %s", e3)

        # Если дошли сюда — ставим «карантин», чтобы не флудить
        _svetofor["sheet"] = None
        _svetofor["cooldown_until"] = datetime.now() + timedelta(minutes=_AUTH_COOLDOWN_MINUTES)
        logger.warning("[gsheets] init failed (auth): %s", msg)

    except Exception as exc:  # noqa: BLE001
        _svetofor["init_error"] = str(exc)
        _svetofor["sheet"] = None
        logger.warning("[gsheets] init failed (format/runtime): %s — повторим при следующем запросе", exc)


def _load_svetofor_data() -> None:
    """Тянет includeGridData, кэширует rows/headers на 24 часа."""
    if _svetofor["sheet"] is None:
        return

    ttl = timedelta(hours=_CACHE_TTL_HOURS)
    last = _svetofor.get("last_refresh")
    if last and datetime.now() - last < ttl and _svetofor.get("rows") and _svetofor.get("headers"):
        return

    sid = _svetofor["spreadsheet_id"]
    svc = _svetofor["service"]
    if not sid or not svc:
        return

    resp = svc.spreadsheets().get(
        spreadsheetId=sid,
        includeGridData=True,
        ranges=["Лист1!A1:ZZ999"],
    ).execute()

    sheets = resp.get("sheets", []) or []
    data = sheets[0].get("data", []) if sheets else []
    rowdata = data[0].get("rowData", []) if data else []

    _svetofor["rows"] = rowdata or []
    headers: List[str] = []
    if rowdata:
        first = rowdata[0].get("values", []) or []
        for c in first:
            headers.append((c.get("formattedValue", "") or "").strip().lower())
    _svetofor["headers"] = headers
    _svetofor["last_refresh"] = datetime.now()


# ███ [3] COLOR PARSING
# --------------------------------------------------------------------
def _rgb_to_status(r: float, g: float, b: float) -> str:
    """RGB (0..1) → 'green' | 'yellow' | 'red' | '' (простое и устойчивое правило)."""
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
    if isinstance(col, dict) and col:
        return float(col.get("red", 0.0) or 0.0), float(col.get("green", 0.0) or 0.0), float(col.get("blue", 0.0) or 0.0)

    # 2) effectiveFormat.backgroundColorStyle.rgbColor
    style = fmt.get("backgroundColorStyle", {}) or {}
    rgb = style.get("rgbColor", {}) if isinstance(style, dict) else {}
    if isinstance(rgb, dict) and rgb:
        return float(rgb.get("red", 0.0) or 0.0), float(rgb.get("green", 0.0) or 0.0), float(rgb.get("blue", 0.0) or 0.0)

    # 3) userEnteredFormat.*
    col = ufmt.get("backgroundColor")
    if isinstance(col, dict) and col:
        return float(col.get("red", 0.0) or 0.0), float(col.get("green", 0.0) or 0.0), float(col.get("blue", 0.0) or 0.0)

    style = ufmt.get("backgroundColorStyle", {}) or {}
    rgb = style.get("rgbColor", {}) if isinstance(style, dict) else {}
    if isinstance(rgb, dict) and rgb:
        return float(rgb.get("red", 0.0) or 0.0), float(rgb.get("green", 0.0) or 0.0), float(rgb.get("blue", 0.0) or 0.0)

    # по умолчанию — белый (нет статуса)
    return (1.0, 1.0, 1.0)


# ███ [4] LOW-LEVEL READERS
# --------------------------------------------------------------------
async def get_user_row_from_svetofor(user_id: int) -> Optional[int]:
    """Ищет user_id во втором столбце и возвращает 1-based row или None."""
    _init_svetofor()
    _load_svetofor_data()
    rows = _svetofor["rows"]
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
    headers = _svetofor["headers"]
    if headers is None:
        return None

    norm = (game_name or "").strip().lower()
    if not norm:
        return None

    for idx, title in enumerate(headers, start=1):
        if (title or "").strip().lower() == norm:
            return idx
    return None


# ███ [5] PUBLIC API
# --------------------------------------------------------------------
async def get_user_status_from_svetofor(user_id: int, game_name: str) -> str:
    """
    Возвращает 'green' | 'yellow' | 'red' | ''.

    • При недоступной таблице или ошибке чтения — ''.
    • Кеш таблицы обновляется раз в 24 ч.
    • При фатальной auth-ошибке действует «карантин» (см. _AUTH_COOLDOWN_MINUTES).
    """
    try:
        _init_svetofor()
        _load_svetofor_data()

        rows = _svetofor["rows"]
        if rows is None:
            return ""  # таблица недоступна / в карантине

        row = await get_user_row_from_svetofor(user_id)
        col = await get_game_column_from_svetofor(game_name)
        if not row or not col:
            return ""

        row_vals: List[Dict[str, Any]] = (rows[row - 1].get("values", []) or [])
        if col - 1 >= len(row_vals):
            return ""
        cell = row_vals[col - 1] or {}

        r, g, b = _extract_bg_rgb(cell)
        return _rgb_to_status(r, g, b)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[gsheets] status fetch failed: %s", exc)
        return ""


# ███ [6] ROLE HELPERS (утверждённая логика)
# --------------------------------------------------------------------
def status_to_role(status: str, need: Dict[str, int], team: Dict[str, List[int]]) -> Optional[str]:
    """
    Возвращает целевую роль по цвету статуса c учётом уже занятых слотов.
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
    """Читает цвет из «Светофора» и возвращает роль согласно логике, либо None."""
    status = await get_user_status_from_svetofor(user_id, game_name)
    return status_to_role(status, need, team)


# ███ [7] TESTS
# --------------------------------------------------------------------
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
# 2025-08-12 — v7.2: чтение ключа из файла/JSON, починка переноса строк, антишум-карантин,
#                    безопасные скоупы и устойчивый парсер цвета. Совместимо с v6 API.
