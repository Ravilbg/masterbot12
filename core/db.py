# core/db.py
# ─────────────────────────────────────────────────────────────────────────────
"""Async-доступ к общей базе пользователей `checklists.db` (v12.93-cycle — 2025-07-22)."""

from __future__ import annotations

# ███ [1.0] IMPORTS
# --------------------------------------------------------------------
import aiosqlite
from pathlib import Path
from typing import Dict, Optional, List

from .config import settings

# ███ [2.0] CONSTANTS
# --------------------------------------------------------------------
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

# ███ [3.0] ИНИЦИАЛИЗАЦИЯ БД & ГАРАНТИРОВАННОЕ ДОБАВЛЕНИЕ ЛИДЕРА
# --------------------------------------------------------------------
async def init_db() -> None:
    """
    Создаёт схему таблицы users, если её нет,
    и вставляет запись для settings.LEADER_ID с ролью 'руководитель'.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_USERS_SQL)
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, role) VALUES (?, 'руководитель')",
            (settings.LEADER_ID,),
        )
        await db.commit()

# ███ [4.0] CRUD-ОПЕРАЦИИ
# --------------------------------------------------------------------
async def get_user_info(user_id: int) -> Optional[Dict]:
    """
    Возвращает словарь с keys: first_name, last_name_initial, role
    или None, если user_id не найден.
    """
    await init_db()
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
    user_id: int,
    first_name: str = "",
    last_name: str = "",
    role: str = "",
) -> None:
    """
    Вставляет новую запись или обновляет существующую
    о пользователе в таблице users.
    """
    await init_db()
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

async def get_all_leader_uids() -> List[int]:
    """
    Возвращает список всех Telegram-UID пользователей из таблицы `users`,
    которые считаются ведущими по данным БД.
    """
    await init_db()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT user_id FROM users")
        rows = await cur.fetchall()
        return [int(row["user_id"]) for row in rows]

# ███ [5.0] ТЕСТЫ
# --------------------------------------------------------------------
async def _test():
    """
    Простейшая проверка работоспособности CRUD-операций.
    """
    test_id = 99999999
    await upsert_user(test_id, "Тест", "Пользователь", "ведущий")
    info = await get_user_info(test_id)
    assert info is not None and info["first_name"] == "Тест"
    uids = await get_all_leader_uids()
    assert isinstance(uids, list) and test_id in uids
    print("core.db OK")

if __name__ == "__main__":
    import asyncio
    asyncio.run(_test())
