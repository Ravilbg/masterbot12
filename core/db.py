"""Async‑доступ к общей базе пользователей `checklists.db`."""

from __future__ import annotations

import aiosqlite
from pathlib import Path
from typing import Dict, Optional

from .config import settings

DB_PATH = Path(settings.CHECKLISTS_DB_PATH)

CREATE_USERS_SQL = """
CREATE TABLE IF NOT EXISTS users (
    user_id         INTEGER PRIMARY KEY,
    first_name      TEXT,
    last_name       TEXT,
    role            TEXT,
    scenario_access TEXT,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# ——— init db & ensure leader row ——————————————————
async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_USERS_SQL)
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, role) VALUES (?, 'руководитель')",
            (settings.LEADER_ID,),
        )
        await db.commit()

# ——— CRUD helpers ——————————————————————————————
async def get_user_info(user_id: int) -> Optional[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT first_name, last_name, role FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = await cur.fetchone()
        if not row:
            return None
        last_initial = (row["last_name"] or "")[:1]
        return {
            "first_name": row["first_name"] or "",
            "last_name_initial": last_initial,
            "role": row["role"] or "",
        }

async def upsert_user(
    user_id: int, first_name: str = "", last_name: str = "", role: str = ""
) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO users(user_id, first_name, last_name, role)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                first_name = COALESCE(?, first_name),
                last_name  = COALESCE(?, last_name),
                role       = COALESCE(?, role),
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                user_id,
                first_name,
                last_name,
                role,
                first_name or None,
                last_name or None,
                role or None,
            ),
        )
        await db.commit()
