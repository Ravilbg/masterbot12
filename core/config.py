# core/config.py — Конфиг MasterBot
# ─────────────────────────────────────────────────────────────────────
"""
Единая точка конфигурации бота (SSOT для настроек).

Особенности:
• Значения могут подхватываться из config.json (в корне) → env.
• Корректная работа и с pydantic v2, и без pydantic (заглушки/фолбэки).
• Безопасные дефолты: бот не падает без переменных окружения.
• Проверка/нормализация chat_id, Google scopes, загрузка GAME_ROLE_MAPPING/AMOCRM_FIELDS.

Дополнительно:
• WON_STATUS_ID — ID статуса «Успешно реализовано» (финальный). Берётся из ENV/JSON.
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

_DEFAULT_RATING_WEIGHTS: Dict[str, int] = {
    "poll_reply_d12": 2,
    "poll_reply_d36": 1,
    "poll_reply_late": 0,
    "confirm_d24": 2,
    "confirm_d36": 1,
    "confirm_late": 0,
    "no_reply_penalty_w1": -20,
    "no_reply_penalty_w2": -30,
    "cant_work_2w_row": -10,
    "cant_work_3w_row": -50,
    "game_main": 4,
    "game_assist": 3,
    "game_admin": 2,
    "game_trainee": 1,
    "learn_game": 10,
    "urgent_replacement": 10,
}

def _default_rating_config() -> Dict[str, Any]:
    base = {
        "WINDOW_DAYS": 30,
        "CAP_MIN": 0,
        "CAP_MAX": 100,
        "YELLOW_WEIGHT": 0.0,
        "WEEKLY_CHECK_CRON": "0 6 * * MON",
    }
    base["WEIGHTS"] = dict(_DEFAULT_RATING_WEIGHTS)
    return base

def _merge_rating_config(raw: Any) -> Dict[str, Any]:
    merged = _default_rating_config()
    if isinstance(raw, dict):
        for key in ("WINDOW_DAYS", "CAP_MIN", "CAP_MAX"):
            if key in raw and raw[key] is not None:
                try:
                    merged[key] = int(raw[key])
                except Exception:
                    pass
        if "YELLOW_WEIGHT" in raw and raw["YELLOW_WEIGHT"] is not None:
            try:
                merged["YELLOW_WEIGHT"] = float(raw["YELLOW_WEIGHT"])
            except Exception:
                pass
        if "WEEKLY_CHECK_CRON" in raw and raw["WEEKLY_CHECK_CRON"]:
            merged["WEEKLY_CHECK_CRON"] = str(raw["WEEKLY_CHECK_CRON"])
        weights = raw.get("WEIGHTS")
        if isinstance(weights, dict):
            for w_key, w_val in weights.items():
                if w_val is None:
                    continue
                try:
                    merged["WEIGHTS"][str(w_key)] = int(w_val)
                    continue
                except Exception:
                    try:
                        merged["WEIGHTS"][str(w_key)] = float(w_val)
                    except Exception:
                        continue
    return merged

# ── bootstrap: json → env ----------------------------------------------------
# ВАЖНО: читаем с utf-8-sig, чтобы не падать на BOM
if CONFIG_JSON.exists():
    try:
        with open(CONFIG_JSON, "r", encoding="utf-8-sig") as _f:
            _raw = json.load(_f)
        for _k, _v in (_raw or {}).items():
            env_key = str(_k).upper()
            # в env кладём только скаляры; dict/list читаем валидаторами напрямую
            if env_key not in os.environ and not isinstance(_v, (dict, list)):
                os.environ[env_key] = str(_v)
    except Exception:
        # не мешаем запуску, просто игнорируем битый config.json
        pass

# Поддержка альтернативного имени переменной для токена
if "API_TOKEN" not in os.environ and "TELEGRAM_BOT_TOKEN" in os.environ:
    os.environ["API_TOKEN"] = os.environ["TELEGRAM_BOT_TOKEN"]

# ── dual-import для Pydantic или заглушек ------------------------------------
_T = TypeVar("_T")
try:
    from pydantic_settings import BaseSettings
    from pydantic import Field, field_validator
    _V2 = True
except (ModuleNotFoundError, ImportError):
    # Заглушки для среды без pydantic
    class BaseSettings:  # type: ignore
        def __init_subclass__(cls, **kwargs):
            super().__init_subclass__(**kwargs)
        def __init__(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)
        # для совместимости с main.py (model_dump()/dict())
        def dict(self) -> Dict[str, Any]:
            return dict(self.__dict__)

    def Field(  # type: ignore
        default: Any = None,
        *,
        env: str = "",
        default_factory: Callable[[], Any] | None = None
    ) -> Any:
        # имитируем чтение из окружения для скаляров (как делает pydantic)
        if env:
            val = os.environ.get(env)
            if val is not None:
                return val
        return default_factory() if default_factory is not None else default

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
        # В заглушке просто вернём функцию как есть
        def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
            return fn
        return wrap


# ── Settings -----------------------------------------------------------------
class Settings(BaseSettings):
    """Все конфигурационные параметры бота (SSOT)."""

    # — basic —
    VERSION: str = "MasterBot 12.93-SSOT"
    API_TOKEN: str = Field(default="", env="API_TOKEN")
    LEADER_ID: int = Field(default=0, env="LEADER_ID")

    # — paths & creds —
    GOOGLE_CREDENTIALS_FILE: str = Field(default="", env="GOOGLE_CREDENTIALS_PATH")
    GOOGLE_CREDENTIALS_JSON: Optional[str] = Field(default=None, env="GOOGLE_CREDENTIALS_JSON")
    CHECKLISTS_DB_PATH: str = str(BASE_DIR / "checklists.db")
    TOKENS_FILE: str = str(BASE_DIR / "tokens.json")
    LOG_DIR: str = str(BASE_DIR / "logs")

    # — AmoCRM / Sheets ids —
    AMO_DOMAIN: str = Field(default="", env="AMO_DOMAIN")
    PIPELINE_ID: Optional[int] = Field(default=None, env="PIPELINE_ID")
    SVETOFOR_SPREAD_ID: str = Field(default="", env="SVETOFOR_SPREAD_ID")

    # — интеграция чатов / справки —
    POLLS_CHAT_ID: int = Field(default=-1001234567890, env="POLLS_CHAT_ID")
    LEADERS_CHAT_ID: int = Field(default=0, env="LEADERS_CHAT_ID")
    ADMIN_CHAT_ID: int = Field(default=0, env="ADMIN_CHAT_ID")
    GUIDE_BOT_LINK: str = Field(default="https://t.me/guide_bot_link", env="GUIDE_BOT_LINK")

    # — time & cache —
    DATE_FILTER_DAYS: int = 30
    POLL_WINDOW_DAYS: int = 10  # окно дат для выборки игр в опрос
    CACHE_TTL_SECONDS: int = 300
    POLL_DURATION_HOURS: int = 24
    GOOGLE_API_RATE_LIMIT_SECONDS: int = 180

    # — Google Sheets scopes —
    # по умолчанию только readonly — устраняет invalid_scope
    GOOGLE_SHEETS_SCOPES: List[str] = Field(
        default_factory=lambda: ["https://www.googleapis.com/auth/spreadsheets.readonly"],
        env="GOOGLE_SHEETS_SCOPES",
    )

    # — role-mapping, статусы —
    GAME_ROLE_MAPPING: Dict[str, Dict[str, int]] = Field(default_factory=dict)
    ALLOWED_STATUS_IDS: List[str] = Field(default_factory=lambda: ["18913933", "18960415", "18913930"])
    NEW_GAMES_STATUS_IDS: List[str] = Field(default_factory=lambda: ["18913930", "18913933"])
    BRON_STATUS_ID: str = Field(default="18913933", env="BRON_STATUS_ID")
    SUCCESSFUL_STATUS_ID: str = Field(default="18960415", env="SUCCESSFUL_STATUS_ID")
    # NEW: финальный статус «Успешно реализовано»
    WON_STATUS_ID: str = Field(default="", env="WON_STATUS_ID")
    
    # AmoCRM custom field for "Photographer" and enum id for "нет"
    PHOTOGRAPHER_CF_ID: Optional[int] = Field(default=None, env="PHOTOGRAPHER_CF_ID")
    PHOTOGRAPHER_ENUM_NO: Optional[int] = Field(default=None, env="PHOTOGRAPHER_ENUM_NO")

# 2025-01-19: enum-поддержка фотографа

    # — access matrix —
    ACCESS: Dict[str, List[str]] = Field(default_factory=lambda: {
        "games": ["руководитель", "администратор", "ведущий", "ведущий новичок"],
        "poll": ["руководитель", "администратор"],
        "distribution": ["руководитель", "администратор"],
    })

    # — AmoCRM fields (ID кастом-полей) —
    AMOCRM_FIELDS: Dict[str, str] = Field(default_factory=dict)
    RATING: Dict[str, Any] = Field(default_factory=_default_rating_config)

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

    # ── validators ------------------------------------------------------------
    @_make_validator("GOOGLE_CREDENTIALS_FILE")
    def _detect_creds(cls, v: Optional[str]) -> str:
        """Автопоиск ключа сервис-аккаунта, если путь не задан."""
        if v:
            return v
        for name in ("svetofor-credentials.json", "service-account-key.json", "google-credentials.json", "credentials.json"):
            p = BASE_DIR / name
            if p.exists():
                return str(p)
        return ""

    def _normalize_chat_id(v: Any) -> int:
        """Общий нормализатор chat_id: int | '123' | '@name' → int/0."""
        if v is None:
            return 0
        if isinstance(v, int):
            return v
        s = str(v).strip()
        if s.startswith("@") or s.startswith("https://"):
            return 0
        try:
            return int(s)
        except Exception:
            return 0

    @_make_validator("POLLS_CHAT_ID")
    def _normalize_polls_chat_id(cls, v: Any) -> int:
        return Settings._normalize_chat_id(v)  # type: ignore[attr-defined]

    @_make_validator("LEADERS_CHAT_ID")
    def _normalize_leaders_chat_id(cls, v: Any) -> int:
        return Settings._normalize_chat_id(v)  # type: ignore[attr-defined]

    @_make_validator("ADMIN_CHAT_ID")
    def _normalize_admin_chat_id(cls, v: Any) -> int:
        return Settings._normalize_chat_id(v)  # type: ignore[attr-defined]

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
        """Читаем GAME_ROLE_MAPPING из config.json при отсутствии в env."""
        if v:
            return v
        if CONFIG_JSON.exists():
            try:
                data = json.loads(CONFIG_JSON.read_text(encoding="utf-8-sig"))
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
        """Читаем AMOCRM_FIELDS из config.json при отсутствии в env."""
        if v:
            return v
        if CONFIG_JSON.exists():
            try:
                data = json.loads(CONFIG_JSON.read_text(encoding="utf-8-sig"))
                if isinstance(data.get("AMOCRM_FIELDS"), dict):
                    return data["AMOCRM_FIELDS"]  # type: ignore[return-value]
            except Exception:
                pass
        # дефолтные ID кастом-полей (настрой в проде!)
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
settings.RATING = _merge_rating_config(getattr(settings, "RATING", {}))

# ── Fallback для окружения/типов при работе без pydantic ───────────
# 1) API_TOKEN: если пуст — попробуем TELEGRAM_BOT_TOKEN
if not getattr(settings, "API_TOKEN", ""):
    settings.API_TOKEN = (os.getenv("API_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()

# 2) Приведём к int ID'шники, если pydantic не сделал это сам
def _as_int(x: Any) -> int:
    try:
        return int(str(x).strip())
    except Exception:
        return 0

for _name in ("LEADER_ID", "POLLS_CHAT_ID", "LEADERS_CHAT_ID", "ADMIN_CHAT_ID"):
    setattr(settings, _name, _as_int(getattr(settings, _name, 0)))

# 3) Если dict-поля пустые — подхватим из config.json (учитываем BOM)
try:
    if CONFIG_JSON.exists():
        _cfg = json.loads(CONFIG_JSON.read_text(encoding="utf-8-sig"))
        if not getattr(settings, "GAME_ROLE_MAPPING", {}):
            grm = _cfg.get("GAME_ROLE_MAPPING")
            if isinstance(grm, dict):
                settings.GAME_ROLE_MAPPING = grm
        if not getattr(settings, "AMOCRM_FIELDS", {}):
            af = _cfg.get("AMOCRM_FIELDS")
            if isinstance(af, dict):
                settings.AMOCRM_FIELDS = af
        rating_raw = _cfg.get("RATING")
        if isinstance(rating_raw, dict):
            settings.RATING = _merge_rating_config(rating_raw)
        else:
            settings.RATING = _merge_rating_config(settings.RATING)
except Exception:
    pass

# Фолбэк: если POLLS_CHAT_ID не задан/плейсхолдер, пробуем LEADERS_CHAT_ID → ADMIN_CHAT_ID.
# Это устраняет 'Bad Request: chat not found' при публикации «Замены».
try:
    if (not isinstance(settings.POLLS_CHAT_ID, int)) or settings.POLLS_CHAT_ID in (0, -1001234567890):
        fallback = 0
        for _k in ("LEADERS_CHAT_ID", "ADMIN_CHAT_ID"):
            _cid = getattr(settings, _k, 0)
            if isinstance(_cid, int) and _cid < 0:
                fallback = _cid
                break
        if fallback:
            settings.POLLS_CHAT_ID = fallback
except Exception:
    # не блокируем запуск при ошибке конфигурации
    pass

os.makedirs(settings.LOG_DIR, exist_ok=True)


# ════════════════════════════════════════════════════════════════
#                              ТЕСТЫ
# ════════════════════════════════════════════════════════════════
def _test():
    s = Settings()
    # базовые типы/значения
    assert isinstance(s.VERSION, str)
    assert isinstance(s.POLLS_CHAT_ID, int)
    assert s.GUIDE_BOT_LINK.startswith("https://")
    assert isinstance(s.GAME_ROLE_MAPPING, dict)
    assert isinstance(s.AMOCRM_FIELDS, dict)
    # scopes должны быть spreadsheets.* и не пустыми
    assert isinstance(s.GOOGLE_SHEETS_SCOPES, list) and s.GOOGLE_SHEETS_SCOPES
    assert all(str(x).startswith("https://www.googleapis.com/auth/spreadsheets") for x in s.GOOGLE_SHEETS_SCOPES)
    # новые поля присутствуют
    assert isinstance(s.POLL_WINDOW_DAYS, int) and s.POLL_WINDOW_DAYS > 0
    assert isinstance(s.BRON_STATUS_ID, str) and s.BRON_STATUS_ID
    assert isinstance(s.SUCCESSFUL_STATUS_ID, str) and s.SUCCESSFUL_STATUS_ID
    # WON_STATUS_ID может быть пустым, но тип должен быть строка
    assert isinstance(s.WON_STATUS_ID, str)
    # Новые поля фотографа
    assert hasattr(s, 'PHOTOGRAPHER_CF_ID')
    assert hasattr(s, 'PHOTOGRAPHER_ENUM_NO')
    print("✅ core/config.py tests passed")
# 2025-01-19: тесты констант фотографа


if __name__ == "__main__":
    _test()

# История изменений:
# 2025-08-18 — выровнено под SSOT/фиксы Pylance: добавлены BRON_STATUS_ID, POLL_WINDOW_DAYS, GOOGLE_CREDENTIALS_JSON,
#               безопасные дефолты вместо обязательных полей, нормализаторы chat_id/scopes, самотест.
# 2025-08-26 — добавлен WON_STATUS_ID (финальный статус «Успешно реализовано») с поддержкой ENV/config.json.
# 2025-09-12 — чтение config.json с utf-8-sig (BOM), фиксы ENV для режима без pydantic, фолбэки API_TOKEN/ID/словари.
# 2025-09-17 — модуль рейтинга: выровнено под SSOT.
