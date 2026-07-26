"""Orderless, durable cohort tracking for the v1.4.59 live canary.

The adaptive loop's session is an operational risk boundary: it may stop for
an entry TTL, a loss cap, or an operator restart.  It must therefore not also
be the measurement boundary for a strategy version.  This module persists one
separate tracking session in ``app_config`` and derives its metrics from the
immutable run ledger and the latest formal reconciliation of each run.

It deliberately has no exchange or Telegram dependency and cannot place,
cancel, or amend orders.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Mapping


TRACKING_CONFIG_KEY = "mainnet_v1459_cohort_tracking_session_v1"
TRACKING_SCHEMA_VERSION = 1
ADAPTIVE_MODE = "adaptive_continuous"
TERMINAL_STATUSES = {
    "COMPLETED",
    "ENTRY_EXPIRED",
    "FAILED",
    "CANCELLED",
    "EMERGENCY_CLOSED",
}


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _number(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class V1459CohortTrackingSession:
    """Immutable definition of one strategy-performance measurement cohort."""

    session_id: str
    code_version: str
    config_sha: str
    symbol: str
    canary_contract: str
    started_at_ms: int
    created_at_ms: int
    target_paid_closed_fills: int

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "V1459CohortTrackingSession | None":
        try:
            session_id = str(payload["session_id"])
            code_version = str(payload["code_version"])
            config_sha = str(payload["config_sha"])
            symbol = str(payload["symbol"])
            canary_contract = str(payload["canary_contract"])
            started_at_ms = int(payload["started_at_ms"])
            created_at_ms = int(payload["created_at_ms"])
            target = int(payload["target_paid_closed_fills"])
        except (KeyError, TypeError, ValueError):
            return None
        if not all((session_id, code_version, config_sha, symbol, canary_contract)):
            return None
        if started_at_ms < 0 or created_at_ms < 0 or target < 1:
            return None
        return cls(
            session_id=session_id,
            code_version=code_version,
            config_sha=config_sha,
            symbol=symbol,
            canary_contract=canary_contract,
            started_at_ms=started_at_ms,
            created_at_ms=created_at_ms,
            target_paid_closed_fills=target,
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "schema_version": TRACKING_SCHEMA_VERSION,
            "kind": "v1459_cohort_tracking",
            "session_id": self.session_id,
            "code_version": self.code_version,
            "config_sha": self.config_sha,
            "symbol": self.symbol,
            "canary_contract": self.canary_contract,
            "started_at_ms": self.started_at_ms,
            "created_at_ms": self.created_at_ms,
            "target_paid_closed_fills": self.target_paid_closed_fills,
        }


@dataclass(frozen=True)
class V1459CohortSnapshot:
    """Latest official metrics for one immutable cohort definition."""

    attempts: int
    active_runs: int
    entry_expired: int
    paid_closed_fills: int
    wins: int
    net_pnl_usdc: float
    unreconciled_completed: int
    operational_session_count: int

    @property
    def wr_pct(self) -> float | None:
        if self.paid_closed_fills <= 0:
            return None
        return self.wins * 100.0 / self.paid_closed_fills

    @property
    def ev_per_attempt_usdc(self) -> float | None:
        if self.attempts <= 0:
            return None
        return self.net_pnl_usdc / self.attempts


class V1459CohortTracker:
    """Keeps one independent v1.4.59 measurement session in app config."""

    permits_order_mutation = False

    def __init__(self, *, db: Any, config_repo: Any | None) -> None:
        self._db = db
        self._config_repo = config_repo
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._db is not None and self._config_repo is not None

    @staticmethod
    def _matches(
        row: Mapping[str, Any], session: V1459CohortTrackingSession
    ) -> bool:
        params = _json_object(row.get("params_json"))
        adaptive = params.get("adaptive")
        if not isinstance(adaptive, Mapping):
            return False
        return (
            params.get("mode") == ADAPTIVE_MODE
            and str(params.get("symbol") or row.get("symbol") or "") == session.symbol
            and str(adaptive.get("codex_v1_version") or "") == session.code_version
            and str(adaptive.get("config_sha") or "") == session.config_sha
            and str(adaptive.get("canary_contract") or "") == session.canary_contract
            and int(row.get("armed_at_ms") or 0) >= session.started_at_ms
        )

    async def _adaptive_rows(self) -> list[dict[str, Any]]:
        if self._db is None:
            return []
        return await self._db.fetchall(
            """SELECT run_id, symbol, status, params_json, armed_at_ms
            FROM mainnet_runs
            WHERE params_json LIKE '%adaptive_continuous%'
            ORDER BY armed_at_ms ASC, run_id ASC"""
        )

    async def get_session(self) -> V1459CohortTrackingSession | None:
        if not self.enabled:
            return None
        raw = await self._config_repo.get(TRACKING_CONFIG_KEY)
        payload = _json_object(raw)
        if int(payload.get("schema_version") or 0) != TRACKING_SCHEMA_VERSION:
            return None
        return V1459CohortTrackingSession.from_payload(payload)

    async def ensure_session(
        self,
        *,
        code_version: str,
        config_sha: str,
        symbol: str,
        canary_contract: str,
        target_paid_closed_fills: int,
        now_ms: int | None = None,
    ) -> V1459CohortTrackingSession | None:
        """Create or return a matching tracker; changing a boundary creates a new one.

        The initial tracker deliberately adopts earlier rows with exactly the
        same immutable cohort definition.  This fixes operational session
        restarts from resetting the 20-fill evaluation sample.
        """

        if not self.enabled:
            return None
        required = (code_version, config_sha, symbol, canary_contract)
        if not all(isinstance(value, str) and value for value in required):
            raise ValueError("cohort identity fields are required")
        if isinstance(target_paid_closed_fills, bool) or target_paid_closed_fills < 1:
            raise ValueError("target_paid_closed_fills must be positive")
        now = int(time.time() * 1000) if now_ms is None else int(now_ms)

        async with self._lock:
            existing = await self.get_session()
            if existing and (
                existing.code_version == code_version
                and existing.config_sha == config_sha
                and existing.symbol == symbol
                and existing.canary_contract == canary_contract
                and existing.target_paid_closed_fills == target_paid_closed_fills
            ):
                return existing

            provisional = V1459CohortTrackingSession(
                session_id=f"v1459_cohort_{now}",
                code_version=code_version,
                config_sha=config_sha,
                symbol=symbol,
                canary_contract=canary_contract,
                started_at_ms=0,
                created_at_ms=now,
                target_paid_closed_fills=target_paid_closed_fills,
            )
            rows = [row for row in await self._adaptive_rows() if self._matches(row, provisional)]
            historical_start = min(
                (int(row.get("armed_at_ms") or now) for row in rows), default=now
            )
            session = V1459CohortTrackingSession(
                session_id=provisional.session_id,
                code_version=code_version,
                config_sha=config_sha,
                symbol=symbol,
                canary_contract=canary_contract,
                started_at_ms=historical_start,
                created_at_ms=now,
                target_paid_closed_fills=target_paid_closed_fills,
            )
            await self._config_repo.set(
                TRACKING_CONFIG_KEY,
                json.dumps(session.as_payload(), ensure_ascii=False, sort_keys=True),
            )
            return session

    async def snapshot(
        self, session: V1459CohortTrackingSession
    ) -> V1459CohortSnapshot:
        if not isinstance(session, V1459CohortTrackingSession):
            raise ValueError("tracking session is required")
        rows = [row for row in await self._adaptive_rows() if self._matches(row, session)]
        run_ids = [str(row["run_id"]) for row in rows if row.get("run_id")]
        reconciliations: dict[str, dict[str, Any]] = {}
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            reconciliation_rows = await self._db.fetchall(
                f"""WITH latest AS (
                    SELECT run_id, MAX(reconciliation_revision) AS reconciliation_revision
                    FROM run_reconciliations
                    WHERE run_id IN ({placeholders})
                    GROUP BY run_id
                )
                SELECT rr.run_id, rr.reconciliation_status, rr.net_pnl_usdc
                FROM latest
                JOIN run_reconciliations rr
                  ON rr.run_id = latest.run_id
                 AND rr.reconciliation_revision = latest.reconciliation_revision""",
                tuple(run_ids),
            )
            reconciliations = {
                str(row["run_id"]): row for row in reconciliation_rows
            }

        attempts = sum(1 for row in rows if str(row.get("status") or "") in TERMINAL_STATUSES)
        entry_expired = sum(1 for row in rows if str(row.get("status") or "") == "ENTRY_EXPIRED")
        active_runs = sum(1 for row in rows if str(row.get("status") or "") not in TERMINAL_STATUSES)
        complete_rows = [
            row
            for row in rows
            if str(row.get("status") or "") == "COMPLETED"
            and str((reconciliations.get(str(row.get("run_id"))) or {}).get("reconciliation_status") or "") == "COMPLETE"
        ]
        net_pnl = sum(
            _number(reconciliations[str(row["run_id"])].get("net_pnl_usdc"))
            for row in complete_rows
        )
        operational_session_ids: set[str] = set()
        for row in rows:
            adaptive = _json_object(row.get("params_json")).get("adaptive")
            if isinstance(adaptive, Mapping):
                operational_session_ids.add(str(adaptive.get("session_id") or ""))
        complete_run_ids = {str(row["run_id"]) for row in complete_rows}
        return V1459CohortSnapshot(
            attempts=attempts,
            active_runs=active_runs,
            entry_expired=entry_expired,
            paid_closed_fills=len(complete_rows),
            wins=sum(
                1
                for row in complete_rows
                if _number(reconciliations[str(row["run_id"])].get("net_pnl_usdc")) > 0
            ),
            net_pnl_usdc=net_pnl,
            unreconciled_completed=sum(
                1
                for row in rows
                if str(row.get("status") or "") == "COMPLETED"
                and str(row.get("run_id") or "") not in complete_run_ids
            ),
            operational_session_count=len(operational_session_ids - {""}),
        )

    @staticmethod
    def format_status(
        session: V1459CohortTrackingSession,
        snapshot: V1459CohortSnapshot,
    ) -> str:
        wr = "-" if snapshot.wr_pct is None else f"{snapshot.wr_pct:.1f}%"
        ev = "-" if snapshot.ev_per_attempt_usdc is None else f"{snapshot.ev_per_attempt_usdc:+.5f} USDC"
        return (
            "📡 <b>v1.4.59 cohort tracking（獨立，不下單）</b>\n"
            f"Tracking session：<code>{session.session_id}</code>\n"
            f"Cohort：<code>{session.code_version}</code> | cfg=<code>{session.config_sha[:12]}</code>\n"
            f"正式 closed fills：<b>{snapshot.paid_closed_fills}</b> / {session.target_paid_closed_fills} | "
            f"WR：<b>{snapshot.wins}/{snapshot.paid_closed_fills} ({wr})</b> | "
            f"淨 PnL：<b>{snapshot.net_pnl_usdc:+.5f} USDC</b>\n"
            f"Attempts：<b>{snapshot.attempts}</b> | entry 未成交：<b>{snapshot.entry_expired}</b> | "
            f"active：<b>{snapshot.active_runs}</b> | EV/attempt：<b>{ev}</b>\n"
            f"合併 operational sessions：<b>{snapshot.operational_session_count}</b> | "
            f"待正式 reconciliation：<b>{snapshot.unreconciled_completed}</b>"
        )


__all__ = [
    "TRACKING_CONFIG_KEY",
    "V1459CohortSnapshot",
    "V1459CohortTracker",
    "V1459CohortTrackingSession",
]
