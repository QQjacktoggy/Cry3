"""Fail-closed runtime-scope reads for durable adaptive sessions."""

from __future__ import annotations

from src.gridbot.storage.database import Database


class AdaptiveSessionScopeConflict(RuntimeError):
    """More than one open account identity exists in one runtime scope."""


class AdaptiveSessionRuntimeReader:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_open_session_for_runtime_scope(
        self,
        *,
        environment: str,
        database_identity: str,
        symbol: str,
    ) -> dict | None:
        scope = {
            "environment": environment,
            "database_identity": database_identity,
            "symbol": symbol,
        }
        for key, value in scope.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} must be a non-empty string")
        rows = await self._db.fetchall(
            """SELECT * FROM adaptive_sessions
            WHERE environment = ? AND database_identity = ? AND symbol = ?
              AND status IN ('ACTIVE', 'PAUSED_REQUIRES_ACK')
            ORDER BY last_checkpoint_at_ms DESC, session_id DESC""",
            (environment, database_identity, symbol),
        )
        if len(rows) > 1:
            raise AdaptiveSessionScopeConflict(
                "multiple open adaptive sessions exist in one runtime scope"
            )
        return rows[0] if rows else None


__all__ = ["AdaptiveSessionRuntimeReader", "AdaptiveSessionScopeConflict"]
