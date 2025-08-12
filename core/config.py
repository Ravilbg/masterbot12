# core/config.py — Конфиг MasterBot
# ─────────────────────────────────────────────────────────────────────
"""
Единая точка конфигурации бота.

Особенности:
• Значения могут подхватываться из config.json (в корне) -> env.
• Поддержан запуск без pydantic (заглушки BaseSettings/Field/validator).
• Для Google Sheets задан безопасный дефолт scope (read-only) + валидация.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

# ── пути ---------------------------------------------------------------------
try:
    BASE_DIR = Path(__file__).resolve().parent.parent
except NameError:
    BASE_DIR = Path(os.getcwd()).resolve()
CONFIG_JSON = BASE_DIR / "config.json"

# ── bootstrap: json → env ----------------------------------------------------
if CONFIG_JSON.exists():
    try:
        with open(CONFIG_JSON, "r", encoding="utf-8") as _f:
            _raw = json.load(_f)
        for _k, _v in _raw.items():
            env_key = _k.upper()
            if env_key not in os.environ and not isinstance(_v, (dict, list)):
                os.environ[env_key] = str(_v)
    except Exception:
        # не мешаем запуску, просто игнорируем битый config.json
        pass

# ── dual-import для Pydantic или заглушек ------------------------------------
_T = TypeVar("_T")
try:
    from pydantic_settings import BaseSettings
    from pydantic import Field, field_validator
    _V2 = True
except (ModuleNotFoundError, ImportError):
    # Заглушки для среды без pydantic
    class BaseSettings:  # type: ignore
        def __init_subclass__(cls, **kwargs):  # noqa: D401
            super().__init_subclass__(**kwargs)
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
    def Field(default: Any = None, env: str = "", default_factory: Callable[[], Any] | None = None) -> Any:  # type: ignore
        return default_factory() if default_factory is not None else default
    def validator(field_name: str, pre: bool = False, always: bool = False):  # type: ignore
        def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
            return fn
        return wrap
    def field_validator(field_name: str, mode: str = "before"):  # type: ignore
        def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
            return fn
        return wrap
    _V2 = False


def _make_validator(field_name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    if _V2:
        def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
            return field_validator(field_name, mode="before")(fn)  # type: ignore
        return wrap
    else:
        def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
            return validator(field_name, pre=True, always=True)(fn)  # type: ignore
        return wrap


# ── Settings -----------------------------------------------------------------
class Settings(BaseSettings):
    """Все конфигурационные параметры бота."""

    # — basic —
    VERSION: str = "MasterBot 12.92-refactor"
    API_TOKEN: str = Field(..., env="API_TOKEN")
    LEADER_ID: int = Field(..., env="LEADER_ID")

    # — paths & creds —
    GOOGLE_CREDENTIALS_FILE: str = Field(default="", env="GOOGLE_CREDENTIALS_PATH")
    CHECKLISTS_DB_PATH: str = str(BASE_DIR / "checklists.db")
    TOKENS_FILE: str = str(BASE_DIR / "tokens.json")
    LOG_DIR: str = str(BASE_DIR / "logs")

    # — AmoCRM / Sheets ids —
    AMO_DOMAIN: str = Field(..., env="AMO_DOMAIN")
    PIPELINE_ID: Optional[int] = None
    SVETOFOR_SPREAD_ID: str = Field(..., env="SVETOFOR_SPREAD_ID")

    # — интеграция чатов / справки —
    POLLS_CHAT_ID: int = Field(-1001234567890, env="POLLS_CHAT_ID")
    GUIDE_BOT_LINK: str = Field("https://t.me/guide_bot_link", env="GUIDE_BOT_LINK")

    # — time & cache —
    DATE_FILTER_DAYS: int = 30
    CACHE_TTL_SECONDS: int = 300
    POLL_DURATION_HOURS: int = 24
    GOOGLE_API_RATE_LIMIT_SECONDS: int = 180

    # — Google Sheets scopes —
    # ВАЖНО: по умолчанию только readonly — это устраняет invalid_scope.
    GOOGLE_SHEETS_SCOPES: List[str] = Field(
        default_factory=lambda: ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )

    # — role-mapping, statuses —
    GAME_ROLE_MAPPING: Dict[str, Dict[str, int]] = Field(default_factory=dict)
    ALLOWED_STATUS_IDS: List[str] = Field(default_factory=lambda: ["18913933", "18960415", "18913930"])
    NEW_GAMES_STATUS_IDS: List[str] = Field(default_factory=lambda: ["18913930", "18913933"])
    SUCCESSFUL_STATUS_ID: str = "18960415"

    # — access matrix —
    ACCESS: Dict[str, List[str]] = Field(default_factory=lambda: {
        "games": ["руководитель", "администратор", "ведущий", "ведущий новичок"],
        "poll": ["руководитель", "администратор"],
        "distribution": ["руководитель", "администратор"],
    })

    # — AmoCRM fields (ID кастом-полей) —
    AMOCRM_FIELDS: Dict[str, str] = Field(default_factory=dict)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    # ── validators ------------------------------------------------------------
    @_make_validator("GOOGLE_CREDENTIALS_FILE")
    def _detect_creds(cls, v: Optional[str]) -> str:
        if v:
            return v
        for name in ("svetofor-credentials.json", "service-account-key.json", "credentials.json"):
            p = BASE_DIR / name
            if p.exists():
                return str(p)
        return ""

    @_make_validator("GOOGLE_SHEETS_SCOPES")
    def _normalize_scopes(cls, v: Any) -> List[str]:
        """
        Принимаем:
          • None/пусто → readonly
          • "a,b,c"    → ["a","b","c"]
          • ["a","b"]  → как есть
        Любые невалидные/чужие скоупы → принудительно readonly.
        """
        readonly = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
        if not v:
            return readonly
        if isinstance(v, str):
            scopes = [s.strip() for s in v.split(",") if s.strip()]
        elif isinstance(v, (list, tuple)):
            scopes = [str(s).strip() for s in v if str(s).strip()]
        else:
            return readonly

        allowed_prefix = "https://www.googleapis.com/auth/spreadsheets"
        if not scopes or any(not s.startswith(allowed_prefix) for s in scopes):
            # защищаемся от 'drive.*', 'userinfo.*' и т.п.
            return readonly
        return scopes

    @_make_validator("GAME_ROLE_MAPPING")
    def _load_role_map(cls, v: Dict | None) -> Dict[str, Dict[str, int]]:
        if v:
            return v
        if CONFIG_JSON.exists():
            try:
                data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
                if isinstance(data.get("GAME_ROLE_MAPPING"), dict):
                    return data["GAME_ROLE_MAPPING"]  # type: ignore[return-value]
            except Exception:
                pass
        # дефолтные требования по ролям
        return {
            "Петля времени": {"main_leaders": 1, "assistants": 1},
            "Хранители волшебства": {"main_leaders": 1, "assistants": 1},
            "Время приключений": {"main_leaders": 1, "assistants": 2},
            "Кланы Нью-Йорка": {"main_leaders": 1, "assistants": 3},
            "Коллекционер игр": {"main_leaders": 1, "assistants": 1},
            "Бермудский треугольник": {"main_leaders": 1, "assistants": 3},
            "Старый дом": {"main_leaders": 1, "assistants": 1},
            "Цветочная башня": {"main_leaders": 2, "assistants": 0},
        }

    @_make_validator("AMOCRM_FIELDS")
    def _load_amocrm_fields(cls, v: Dict | None) -> Dict[str, str]:
        if v:
            return v
        if CONFIG_JSON.exists():
            try:
                data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
                if isinstance(data.get("AMOCRM_FIELDS"), dict):
                    return data["AMOCRM_FIELDS"]  # type: ignore[return-value]
            except Exception:
                pass
        # дефолтные ID кастом-полей
        return {
            "event_date": "87751",
            "event_time": "88565",
            "game_name": "87791",
            "age": "899511",
            "extra_services": "452955",
            "comment": "87811",
            "prepayment": "100407",
            "team_leads": "88567",
            "photographer": "87813",
            "players": "71635",
            "package": "71673",
        }


# ── экспорт singleton --------------------------------------------------------
settings = Settings()
os.makedirs(settings.LOG_DIR, exist_ok=True)


# ════════════════════════════════════════════════════════════════
#                              ТЕСТЫ
# ════════════════════════════════════════════════════════════════
def _test():
    s = Settings()
    assert isinstance(s.VERSION, str)
    assert isinstance(s.POLLS_CHAT_ID, int) and s.POLLS_CHAT_ID < 0
    assert s.GUIDE_BOT_LINK.startswith("https://t.me/")
    assert isinstance(s.GAME_ROLE_MAPPING, dict)
    assert isinstance(s.AMOCRM_FIELDS, dict)
    # scopes должны быть spreadsheets.* и не пустыми
    assert isinstance(s.GOOGLE_SHEETS_SCOPES, list) and s.GOOGLE_SHEETS_SCOPES
    assert all(str(x).startswith("https://www.googleapis.com/auth/spreadsheets") for x in s.GOOGLE_SHEETS_SCOPES)
    print("✅ core/config.py tests passed")


if __name__ == "__main__":
    _test()
