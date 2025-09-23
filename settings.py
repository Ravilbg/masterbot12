# settings.py — дополнительные настройки для MasterBot
# ─────────────────────────────────────────────────────────────────────

# AmoCRM custom field for "Photographer" and enum id for "нет"
PHOTOGRAPHER_CF_ID: int | None = None  # <ID_ПОЛЯ_ФОТОГРАФ>      # пример: 123456
PHOTOGRAPHER_ENUM_NO: int | None = None  # <ENUM_ID_ЗНАЧЕНИЯ_НЕТ> # пример: 999999

# 2025-01-19: enum-поддержка фотографа

# Импорт из основного конфига
try:
    from core.config import settings as core_settings
    # Переносим константы из core.config если они там есть
    if hasattr(core_settings, 'PHOTOGRAPHER_CF_ID'):
        PHOTOGRAPHER_CF_ID = core_settings.PHOTOGRAPHER_CF_ID
    if hasattr(core_settings, 'PHOTOGRAPHER_ENUM_NO'):
        PHOTOGRAPHER_ENUM_NO = core_settings.PHOTOGRAPHER_ENUM_NO
except ImportError:
    pass