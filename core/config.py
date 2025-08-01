from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Optional

# ── пути ---------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_JSON = BASE_DIR / "config.json"

# ── bootstrap: json → env ----------------------------------------------------
if CONFIG_JSON.exists():
    try:
        with open(CONFIG_JSON, "r", encoding="utf-8") as _f:
            _raw = json.load(_f)
        for _k, _v in _raw.items():
            env_key = _k.upper()
            # пересылаем только «простые» типы и если var ещё не определён
            if env_key not in os.environ and not isinstance(_v, (dict, list)):
                os.environ[env_key] = str(_v)
    except Exception:
        pass

# ── dual-import для двух версий Pydantic -------------------------------------
try:
    from pydantic_settings import BaseSettings
    from pydantic import Field, field_validator
    _V2 = True
except ModuleNotFoundError:
    from pydantic import BaseSettings, Field, validator  # type: ignore
    _V2 = False

def _make_validator(field_name: str):
    if _V2:
        def wrap(fn):
            return field_validator(field_name, mode="before")(fn)  # type: ignore
        return wrap
    else:
        def wrap(fn):
            return validator(field_name, pre=True, always=True)(fn)  # type: ignore
        return wrap

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

    # — time & cache —
    DATE_FILTER_DAYS: int = 30
    CACHE_TTL_SECONDS: int = 300
    POLL_DURATION_HOURS: int = 24
    GOOGLE_API_RATE_LIMIT_SECONDS: int = 180

    # — Google Sheets scopes —
    GOOGLE_SHEETS_SCOPES: List[str] = Field(default_factory=list)

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

    # — AmoCRM fields —
    AMOCRM_FIELDS: Dict[str, str] = Field(default_factory=dict)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    @_make_validator("GOOGLE_CREDENTIALS_FILE")
    def _detect_creds(cls, v: Optional[str]) -> str:
        if v:
            return v
        # пробуем типовые имена в BASE_DIR
        for name in ("svetofor-credentials.json", "service-account-key.json", "credentials.json"):
            p = BASE_DIR / name
            if p.exists():
                return str(p)
        return ""

    @_make_validator("GAME_ROLE_MAPPING")
    def _load_role_map(cls, v: Dict | None):
        if v:
            return v
        if CONFIG_JSON.exists():
            try:
                data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
                if isinstance(data.get("GAME_ROLE_MAPPING"), dict):
                    return data["GAME_ROLE_MAPPING"]
            except Exception:
                pass
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
    def _load_amocrm_fields(cls, v: Dict | None):
        if v:
            return v
        if CONFIG_JSON.exists():
            try:
                data = json.loads(CONFIG_JSON.read_text(encoding="utf-8"))
                if isinstance(data.get("AMOCRM_FIELDS"), dict):
                    return data["AMOCRM_FIELDS"]
            except Exception:
                pass
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
