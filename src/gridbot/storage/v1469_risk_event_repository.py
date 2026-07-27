"""Durable append-only paid-close events for the v1.4.69 risk reducer."""

from __future__ import annotations

import asyncio
import json
from math import isfinite
import sqlite3
from typing import Any, Mapping

from src.gridbot.mainnet.v1469_risk_policy import (
    DailyRiskEvent,
    active_day_key,
)
from src.gridbot.storage.database import Database


class V1469RiskEventConflictError(RuntimeError):
    """An idempotency or source identity was reused with different data."""


def _compact_json(value: Mapping[str, Any] | None) -> str:
    try:
        encoded = json.dumps(
            dict(value or {}),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("payload must be valid JSON") from exc
    if len(encoded.encode("utf-8")) > 2_048:
        raise ValueError("payload exceeds 2048 bytes")
    return encoded


class V1469RiskEventRepository:
    """Persist exactly the fields needed to reconstruct the active-day latch."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._write_lock = asyncio.Lock()

    async def assert_schema_ready(self) -> None:
        required = {
            "event_id",
            "environment",
            "symbol",
            "active_day",
            "occurred_at_ms",
            "event_type",
            "fee_net_pnl_delta_usdc",
            "risk_policy_hash",
            "created_at_ms",
        }
        actual = {
            str(row.get("name") or "")
            for row in await self._db.fetchall(
                "PRAGMA table_info(v1469_daily_risk_events)"
            )
        }
        missing = sorted(required - actual)
        triggers = {
            str(row.get("name") or "")
            for row in await self._db.fetchall(
                """SELECT name FROM sqlite_master
                WHERE type = 'trigger'
                  AND name LIKE 'trg_v1469_daily_risk_events_%'"""
            )
        }
        required_triggers = {
            "trg_v1469_daily_risk_events_no_update",
            "trg_v1469_daily_risk_events_no_delete",
        }
        if missing or not required_triggers.issubset(triggers):
            raise RuntimeError(
                "unsafe v1.4.69 daily-risk schema: "
                f"missing_columns={missing}, "
                f"missing_triggers={sorted(required_triggers - triggers)}"
            )

    async def append_event(
        self,
        event: DailyRiskEvent,
        *,
        environment: str,
        symbol: str,
        source_run_id: str | None = None,
        source_trade_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        created_at_ms: int | None = None,
    ) -> bool:
        if not isinstance(event, DailyRiskEvent):
            raise TypeError("event must be DailyRiskEvent")
        event_id = str(event.event_id or "").strip()
        scope_environment = str(environment or "").strip().upper()
        scope_symbol = str(symbol or "").strip().upper()
        occurred_at = int(event.occurred_at_ms)
        try:
            pnl_delta = float(event.fee_net_pnl_delta_usdc)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("fee_net_pnl_delta_usdc must be finite") from exc
        policy_hash = str(event.risk_policy_hash or "").strip().lower()
        event_type = str(event.event_type or "").strip().upper()
        if not event_id or not scope_environment or not scope_symbol:
            raise ValueError("event/environment/symbol must be non-empty")
        if occurred_at < 0 or not isfinite(pnl_delta):
            raise ValueError("event time and PnL delta must be valid")
        if event_type != "PAID_CLOSED":
            raise ValueError("event_type must be PAID_CLOSED")
        if len(policy_hash) != 64 or any(
            character not in "0123456789abcdef"
            for character in policy_hash
        ):
            raise ValueError("risk_policy_hash must be lowercase SHA-256")
        created = (
            occurred_at if created_at_ms is None else int(created_at_ms)
        )
        if created < occurred_at:
            raise ValueError("created_at_ms must be >= occurred_at_ms")
        row = {
            "event_id": event_id,
            "environment": scope_environment,
            "symbol": scope_symbol,
            "active_day": active_day_key(occurred_at),
            "occurred_at_ms": occurred_at,
            "event_type": event_type,
            "fee_net_pnl_delta_usdc": pnl_delta,
            "risk_policy_hash": policy_hash,
            "source_run_id": (
                str(source_run_id).strip() if source_run_id else None
            ),
            "source_trade_id": (
                str(source_trade_id).strip() if source_trade_id else None
            ),
            "payload_json": _compact_json(payload),
            "created_at_ms": created,
        }
        async with self._write_lock:
            began = False
            try:
                await self._db.conn.execute("BEGIN IMMEDIATE")
                began = True
                existing = await self._db.fetchone(
                    """SELECT event_id, environment, symbol, active_day,
                              occurred_at_ms, event_type,
                              fee_net_pnl_delta_usdc, risk_policy_hash,
                              source_run_id, source_trade_id, payload_json,
                              created_at_ms
                    FROM v1469_daily_risk_events
                    WHERE event_id = ?""",
                    (event_id,),
                )
                if existing is not None:
                    comparable_existing = {
                        key: value
                        for key, value in existing.items()
                        if key != "created_at_ms"
                    }
                    comparable_row = {
                        key: value
                        for key, value in row.items()
                        if key != "created_at_ms"
                    }
                    if comparable_existing != comparable_row:
                        raise V1469RiskEventConflictError(
                            "event_id reused with different paid-close data"
                        )
                    await self._db.conn.rollback()
                    began = False
                    return False
                columns = tuple(row)
                await self._db.conn.execute(
                    f"""INSERT INTO v1469_daily_risk_events
                    ({", ".join(columns)})
                    VALUES ({", ".join("?" for _ in columns)})""",
                    tuple(row[name] for name in columns),
                )
                await self._db.conn.commit()
                began = False
                return True
            except asyncio.CancelledError:
                if began:
                    await asyncio.shield(self._db.conn.rollback())
                raise
            except sqlite3.IntegrityError as exc:
                if began:
                    await self._db.conn.rollback()
                raise V1469RiskEventConflictError(str(exc)) from exc
            except Exception:
                if began:
                    await self._db.conn.rollback()
                raise

    async def load_active_day_events(
        self,
        *,
        environment: str,
        symbol: str,
        as_of_ms: int,
        limit: int = 10_000,
    ) -> tuple[DailyRiskEvent, ...]:
        scope_environment = str(environment or "").strip().upper()
        scope_symbol = str(symbol or "").strip().upper()
        as_of = int(as_of_ms)
        bounded_limit = int(limit)
        if not scope_environment or not scope_symbol:
            raise ValueError("environment and symbol must be non-empty")
        if as_of < 0:
            raise ValueError("as_of_ms must be non-negative")
        if not 1 <= bounded_limit <= 50_000:
            raise ValueError("limit must be between 1 and 50000")
        rows = await self._db.fetchall(
            """SELECT event_id, occurred_at_ms, fee_net_pnl_delta_usdc,
                      risk_policy_hash, event_type
            FROM v1469_daily_risk_events
            WHERE environment = ? AND symbol = ? AND active_day = ?
              AND occurred_at_ms <= ?
            ORDER BY occurred_at_ms, event_id
            LIMIT ?""",
            (
                scope_environment,
                scope_symbol,
                active_day_key(as_of),
                as_of,
                bounded_limit + 1,
            ),
        )
        if len(rows) > bounded_limit:
            raise RuntimeError(
                "unsafe v1.4.69 daily-risk ledger: active-day event "
                f"count exceeds bounded load limit ({bounded_limit})"
            )
        return tuple(
            DailyRiskEvent(
                event_id=str(row["event_id"]),
                occurred_at_ms=int(row["occurred_at_ms"]),
                fee_net_pnl_delta_usdc=float(
                    row["fee_net_pnl_delta_usdc"]
                ),
                risk_policy_hash=str(row["risk_policy_hash"]),
                event_type=str(row["event_type"]),
            )
            for row in rows
        )


__all__ = [
    "V1469RiskEventConflictError",
    "V1469RiskEventRepository",
]
