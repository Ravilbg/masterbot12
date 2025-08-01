"""
core package: settings, global state, db‑access

from core import settings, state, db
"""

from .config import settings
from .state import state
from . import db  # noqa: F401 – side‑effect import (init DB)
