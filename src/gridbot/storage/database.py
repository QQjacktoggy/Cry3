import os
from pathlib import Path

import aiosqlite

from src.gridbot.utils.logging import get_logger

logger = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Database:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path, timeout=30)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._run_migrations()
        logger.info("database_initialized", path=self._db_path)

    async def _run_migrations(self) -> None:
        await self._conn.execute(
            "CREATE TABLE IF NOT EXISTS _migrations (filename TEXT PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        await self._conn.commit()

        applied = {
            row[0]
            for row in await self._conn.execute_fetchall("SELECT filename FROM _migrations")
        }

        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if sql_file.name in applied:
                continue
            sql = sql_file.read_text(encoding="utf-8")
            await self._conn.executescript(sql)
            await self._conn.execute("INSERT INTO _migrations (filename) VALUES (?)", (sql_file.name,))
            await self._conn.commit()
            logger.info("migration_applied", filename=sql_file.name)

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        return self._conn

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        cursor = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cursor

    async def execute_many(self, sql: str, params_list: list[tuple]) -> None:
        await self.conn.executemany(sql, params_list)
        await self.conn.commit()

    async def fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        rows = await self.conn.execute_fetchall(sql, params)
        return [dict(row) for row in rows]

    async def fetchone(self, sql: str, params: tuple = ()) -> dict | None:
        async with self.conn.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("database_closed")
