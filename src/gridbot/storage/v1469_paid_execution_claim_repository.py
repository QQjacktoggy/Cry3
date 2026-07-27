"""Durable single-winner paid-execution claims for v1.4.69.

This module is a persistence boundary only.  It deliberately contains no
exchange client and grants no order-placement authority unless a future,
explicitly enabled paid adapter calls :meth:`claim` successfully first.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
from math import fsum, isfinite
import sqlite3
from typing import Any, Mapping, Sequence

from src.gridbot.mainnet.v1469_adaptive_identity import canonical_sha256
from src.gridbot.mainnet.v1469_risk_policy import PHASE_C_SCHEMA, active_day_key
from src.gridbot.storage.database import Database


class V1469PaidClaimConflictError(RuntimeError):
    """A claim identity, idempotency key, or CAS predicate conflicted."""


class V1469PaidClaimPersistenceError(RuntimeError):
    """The paid-claim schema or a durable row violates the safety contract."""


@dataclass(frozen=True, slots=True)
class DurablePaidExecutionClaim:
    claim_id: str
    environment: str
    symbol: str
    opportunity_id: str
    arm_key: str
    lease_id: str
    lease_generation: int
    evidence_revision: str
    regime: str
    execution_profile_hash: str
    risk_policy_hash: str
    approved_notional_usdc: float
    reserved_loss_usdc: float
    status: str
    generation: int
    claimed_at_ms: int
    terminal_at_ms: int | None
    terminal_reason: str | None
    result_payload: Mapping[str, Any] | None
    created_at_ms: int
    updated_at_ms: int
    risk_active_day: str | None = None
    risk_evidence_revision: str | None = None


@dataclass(frozen=True, slots=True)
class PaidClaimMutationResult:
    claim: DurablePaidExecutionClaim
    applied: bool
    replayed: bool


_CLAIM_COLUMNS = (
    "claim_id",
    "environment",
    "symbol",
    "opportunity_id",
    "arm_key",
    "lease_id",
    "status",
    "generation",
    "claimed_at_ms",
    "terminal_at_ms",
    "terminal_reason",
    "result_payload_json",
    "created_at_ms",
    "updated_at_ms",
)

_AUTHORITY_COLUMNS = (
    "lease_generation",
    "evidence_revision",
    "regime",
    "execution_profile_hash",
    "risk_policy_hash",
    "approved_notional_usdc",
    "reserved_loss_usdc",
)

_CLAIM_SELECT = ", ".join(
    (
        *(f"claim.{column}" for column in _CLAIM_COLUMNS),
        *(f"authority.{column}" for column in _AUTHORITY_COLUMNS),
        "risk.risk_active_day",
        "risk.risk_evidence_revision",
    )
)

_RECONCILABLE_STATUSES = frozenset(
    {"SUBMITTING", "UNKNOWN", "SUBMITTED"}
)


def _required_text(
    value: Any,
    name: str,
    *,
    upper: bool = False,
    max_length: int | None = None,
) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} must be non-empty")
    if max_length is not None and len(normalized) > max_length:
        raise ValueError(f"{name} exceeds {max_length} characters")
    return normalized.upper() if upper else normalized


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if normalized < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return normalized

def _non_negative_float(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite non-negative number")
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            f"{name} must be a finite non-negative number"
        ) from exc
    if not isfinite(normalized) or normalized < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return normalized


def _optional_non_negative_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    return _non_negative_int(value, name)


def _optional_non_negative_float(
    value: Any,
    name: str,
) -> float | None:
    if value is None:
        return None
    return _non_negative_float(value, name)


def _optional_text(
    value: Any,
    name: str,
    *,
    upper: bool = False,
) -> str | None:
    if value is None:
        return None
    return _required_text(value, name, upper=upper)


def _canonical_json(
    value: Mapping[str, Any] | None,
    *,
    name: str,
) -> str:
    try:
        encoded = json.dumps(
            dict(value or {}),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be valid JSON") from exc
    if len(encoded.encode("utf-8")) > 4_096:
        raise ValueError(f"{name} exceeds 4096 bytes")
    return encoded


def _claim_id(
    *,
    environment: str,
    symbol: str,
    opportunity_id: str,
) -> str:
    digest = hashlib.sha256(
        (
            "v1469.paid-execution-claim.1|"
            f"{environment}|{symbol}|{opportunity_id}"
        ).encode("utf-8")
    ).hexdigest()
    return f"v1469c_{digest}"


def _claim_from_row(row: Mapping[str, Any]) -> DurablePaidExecutionClaim:
    status = str(row.get("status") or "")
    generation = int(row.get("generation") or 0)
    minimum_generation = {
        "CLAIMED": 1, "SUBMITTING": 2, "UNKNOWN": 3,
        "SUBMITTED": 3, "TERMINAL": 2, "ABANDONED": 2,
    }
    if generation < minimum_generation.get(status, 1) or (status == "CLAIMED" and generation != 1):
        raise V1469PaidClaimPersistenceError(
            f"{status or 'unknown'} row has invalid generation"
        )
    claimed_at = int(row.get("claimed_at_ms") or 0)
    created_at = int(row.get("created_at_ms") or 0)
    updated_at = int(row.get("updated_at_ms") or 0)
    if created_at < claimed_at or updated_at < created_at:
        raise V1469PaidClaimPersistenceError("paid-claim timestamps are not monotonic")
    if status not in {"CLAIMED", "SUBMITTING", "UNKNOWN", "SUBMITTED", "TERMINAL", "ABANDONED"}:
        raise V1469PaidClaimPersistenceError(
            f"unknown durable paid-claim status: {status}"
        )
    try:
        lease_generation = int(row["lease_generation"])
        approved_notional_usdc = float(row["approved_notional_usdc"])
        reserved_loss_usdc = float(row["reserved_loss_usdc"])
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise V1469PaidClaimPersistenceError(
            "paid claim has no valid authority snapshot"
        ) from exc
    authority_text = {
        name: str(row.get(name) or "").strip()
        for name in (
            "evidence_revision",
            "regime",
            "execution_profile_hash",
            "risk_policy_hash",
        )
    }
    risk_active_day = _optional_text(
        row.get("risk_active_day"), "risk_active_day"
    )
    risk_evidence_revision = _optional_text(
        row.get("risk_evidence_revision"), "risk_evidence_revision"
    )
    if (risk_active_day is None) != (risk_evidence_revision is None):
        raise V1469PaidClaimPersistenceError(
            "paid claim risk evidence snapshot is incomplete"
        )
    if (
        lease_generation < 0
        or not all(authority_text.values())
        or not isfinite(approved_notional_usdc)
        or approved_notional_usdc < 0
        or not isfinite(reserved_loss_usdc)
        or reserved_loss_usdc < 0
    ):
        raise V1469PaidClaimPersistenceError(
            "paid claim authority snapshot is invalid"
        )
    raw_payload = row.get("result_payload_json")
    payload: Mapping[str, Any] | None = None
    if raw_payload is not None:
        try:
            parsed = json.loads(str(raw_payload))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise V1469PaidClaimPersistenceError(
                "paid-claim result payload is invalid JSON"
            ) from exc
        if not isinstance(parsed, dict):
            raise V1469PaidClaimPersistenceError(
                "paid-claim result payload must be an object"
            )
        payload = parsed
    return DurablePaidExecutionClaim(
        claim_id=str(row["claim_id"]),
        environment=str(row["environment"]),
        symbol=str(row["symbol"]),
        opportunity_id=str(row["opportunity_id"]),
        arm_key=str(row["arm_key"]),
        lease_id=str(row["lease_id"]),
        lease_generation=lease_generation,
        evidence_revision=authority_text["evidence_revision"],
        regime=authority_text["regime"],
        execution_profile_hash=authority_text["execution_profile_hash"],
        risk_policy_hash=authority_text["risk_policy_hash"],
        risk_active_day=risk_active_day,
        risk_evidence_revision=risk_evidence_revision,
        approved_notional_usdc=approved_notional_usdc,
        reserved_loss_usdc=reserved_loss_usdc,
        status=status,
        generation=generation,
        claimed_at_ms=int(row["claimed_at_ms"]),
        terminal_at_ms=(
            int(row["terminal_at_ms"])
            if row.get("terminal_at_ms") is not None
            else None
        ),
        terminal_reason=(
            str(row["terminal_reason"])
            if row.get("terminal_reason") is not None
            else None
        ),
        result_payload=payload,
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
    )


class V1469PaidExecutionClaimRepository:
    """Own the atomic claim/terminal CAS and its append-only audit events."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._write_lock = asyncio.Lock()

    async def assert_schema_ready(self) -> None:
        required_claim_columns = {
            "claim_id",
            "environment",
            "symbol",
            "opportunity_id",
            "arm_key",
            "lease_id",
            "status",
            "generation",
            "claimed_at_ms",
            "terminal_at_ms",
            "terminal_reason",
            "result_payload_json",
            "created_at_ms",
            "updated_at_ms",
        }
        claim_columns = {
            str(row.get("name") or "")
            for row in await self._db.fetchall(
                "PRAGMA table_info(v1469_paid_execution_claims)"
            )
        }
        required_authority_columns = set(_AUTHORITY_COLUMNS) | {"claim_id"}
        authority_columns = {
            str(row.get("name") or "")
            for row in await self._db.fetchall(
                "PRAGMA table_info(v1469_paid_execution_claim_authority)"
            )
        }

        required_risk_evidence_columns = {
            "claim_id", "risk_active_day", "risk_evidence_revision"
        }
        risk_evidence_columns = {
            str(row.get("name") or "")
            for row in await self._db.fetchall(
                "PRAGMA table_info(v1469_paid_claim_risk_evidence)"
            )
        }

        required_event_columns = {
            "id",
            "idempotency_key",
            "claim_id",
            "opportunity_id",
            "arm_key",
            "lease_id",
            "generation_before",
            "generation_after",
            "event_time_ms",
            "event_type",
            "actor",
            "payload_json",
        }
        event_columns = {
            str(row.get("name") or "")
            for row in await self._db.fetchall(
                "PRAGMA table_info(v1469_paid_execution_claim_events)"
            )
        }
        migration = await self._db.fetchone(
            "SELECT filename FROM _migrations WHERE filename = ?",
            ("019_v1469_paid_execution_claim_upgrade.sql",),
        )
        authority_migration = await self._db.fetchone(
            "SELECT filename FROM _migrations WHERE filename = ?",
            ("021_v1469_paid_claim_authority_snapshot.sql",),
        )
        watermark_migration = await self._db.fetchone(
            "SELECT filename FROM _migrations WHERE filename = ?",
            ("023_v1469_paid_promotion_evidence_clock.sql",),
        )
        risk_evidence_migration = await self._db.fetchone(
            "SELECT filename FROM _migrations WHERE filename = ?",
            ("024_v1469_paid_claim_risk_evidence.sql",),
        )
        watermark_objects = {
            str(row.get("name") or "")
            for row in await self._db.fetchall(
                """SELECT name FROM sqlite_master
                WHERE name IN (
                    'v1469_paid_terminal_evidence_clocks',
                    'v1469_paid_promotion_evidence_snapshots'
                ) AND type = 'table'"""
            )
        }
        trigger_rows = await self._db.fetchall(
            """SELECT name FROM sqlite_master
            WHERE type = 'trigger'
              AND (
                name LIKE 'trg_v1469_paid_claim_%'
                OR name = 'trg_v1469_paid_terminal_evidence_clock'
              )"""
        )
        triggers = {str(row.get("name") or "") for row in trigger_rows}
        required_triggers = {
            "trg_v1469_paid_claim_opportunity_scope",
            "trg_v1469_paid_claim_active_lease",
            "trg_v1469_paid_claim_no_delete",
            "trg_v1469_paid_claim_terminal_once",
            "trg_v1469_paid_claim_transition_guard",
            "trg_v1469_paid_claim_event_identity",
            "trg_v1469_paid_claim_events_no_update",
            "trg_v1469_paid_claim_events_no_delete",
            "trg_v1469_paid_claim_event_requires_cid",
            "trg_v1469_paid_claim_authority_claim_exists",
            "trg_v1469_paid_claim_authority_no_update",
            "trg_v1469_paid_claim_authority_no_delete",
            "trg_v1469_paid_claim_risk_evidence_claim_exists",
            "trg_v1469_paid_claim_risk_evidence_no_update",
            "trg_v1469_paid_claim_risk_evidence_no_delete",
            "trg_v1469_paid_terminal_evidence_clock",
        }
        missing_claim = sorted(required_claim_columns - claim_columns)
        missing_authority = sorted(
            required_authority_columns - authority_columns
        )
        missing_risk_evidence = sorted(
            required_risk_evidence_columns - risk_evidence_columns
        )
        missing_event = sorted(required_event_columns - event_columns)
        missing_triggers = sorted(required_triggers - triggers)
        unbound = await self._db.fetchone(
            """SELECT COUNT(*) AS count
            FROM v1469_paid_execution_claims AS claim
            LEFT JOIN v1469_paid_execution_claim_authority AS authority
              ON authority.claim_id = claim.claim_id
            WHERE authority.claim_id IS NULL"""
        )
        unbound_count = int((unbound or {}).get("count") or 0)
        if (
            migration is None
            or authority_migration is None
            or watermark_migration is None
            or risk_evidence_migration is None
            or watermark_objects != {
                "v1469_paid_terminal_evidence_clocks",
                "v1469_paid_promotion_evidence_snapshots",
            }
            or missing_claim
            or missing_authority
            or missing_risk_evidence
            or missing_event
            or missing_triggers
            or unbound_count
        ):
            raise V1469PaidClaimPersistenceError(
                "unsafe v1.4.69 paid-claim schema: "
                f"missing_claim_columns={missing_claim}, "
                f"missing_authority_columns={missing_authority}, "
                f"missing_risk_evidence_columns={missing_risk_evidence}, "
                f"missing_event_columns={missing_event}, "
                f"missing_triggers={missing_triggers}, "
                f"unbound_claims={unbound_count}, "
                f"upgrade_019_applied={migration is not None}, "
                f"upgrade_021_applied={authority_migration is not None}, "
                f"upgrade_023_applied={watermark_migration is not None}, "
                f"upgrade_024_applied={risk_evidence_migration is not None}, "
                f"watermark_objects={sorted(watermark_objects)}"
            )

    async def get_claim(
        self,
        *,
        environment: str,
        symbol: str,
        opportunity_id: str,
    ) -> DurablePaidExecutionClaim | None:
        scope_environment = _required_text(
            environment, "environment", upper=True
        )
        scope_symbol = _required_text(symbol, "symbol", upper=True)
        opportunity = _required_text(
            opportunity_id, "opportunity_id"
        )
        row = await self._db.fetchone(
            f"""SELECT {_CLAIM_SELECT}
            FROM v1469_paid_execution_claims AS claim
            JOIN v1469_paid_execution_claim_authority AS authority
              ON authority.claim_id = claim.claim_id
            LEFT JOIN v1469_paid_claim_risk_evidence AS risk
              ON risk.claim_id = claim.claim_id
            WHERE claim.environment = ? AND claim.symbol = ?
              AND claim.opportunity_id = ?""",
            (scope_environment, scope_symbol, opportunity),
        )
        return _claim_from_row(row) if row is not None else None

    async def get_claim_by_id(
        self,
        claim_id: str,
    ) -> DurablePaidExecutionClaim | None:
        normalized_claim_id = _required_text(claim_id, "claim_id")
        row = await self._claim_row(normalized_claim_id)
        return _claim_from_row(row) if row is not None else None

    async def list_reconcilable_claims(
        self,
        *,
        environment: str,
        limit: int,
        symbol: str | None = None,
    ) -> tuple[DurablePaidExecutionClaim, ...]:
        scope_environment = _required_text(
            environment, "environment", upper=True
        )
        page_limit = _non_negative_int(limit, "limit")
        if page_limit < 1 or page_limit > 100:
            raise ValueError("limit must be an integer from 1 to 100")
        predicates = [
            "claim.environment = ?",
            "claim.status IN ('CLAIMED', 'SUBMITTING', 'UNKNOWN', 'SUBMITTED')",
        ]
        params: list[Any] = [scope_environment]
        if symbol is not None:
            predicates.append("claim.symbol = ?")
            params.append(_required_text(symbol, "symbol", upper=True))
        params.append(page_limit)
        rows = await self._db.fetchall(
            f"""SELECT {_CLAIM_SELECT}
            FROM v1469_paid_execution_claims AS claim
            JOIN v1469_paid_execution_claim_authority AS authority
              ON authority.claim_id = claim.claim_id
            LEFT JOIN v1469_paid_claim_risk_evidence AS risk
              ON risk.claim_id = claim.claim_id
            WHERE {" AND ".join(predicates)}
            ORDER BY claim.updated_at_ms ASC, claim.claim_id ASC
            LIMIT ?""",
            tuple(params),
        )
        return tuple(_claim_from_row(row) for row in rows)

    async def load_paid_probation_evidence(
        self,
        *,
        environment: str,
        symbol: str,
        arm_key: str,
        execution_profile_hash: str,
        regime: str,
        window_start_ms: int,
        as_of_ms: int,
        limit: int,
        evidence_revision: str | None = None,
    ) -> Mapping[str, Any]:
        """Aggregate and optionally snapshot an exact paid lineage.

        When ``evidence_revision`` is supplied, the aggregate and its terminal
        evidence clock are captured under ``BEGIN IMMEDIATE``.  A later LIVE
        lease CAS must find this exact durable snapshot and the same clock.
        This closes the read-evidence/promote-lease TOCTOU boundary.
        """

        scope_environment = _required_text(
            environment, "environment", upper=True
        )
        scope_symbol = _required_text(symbol, "symbol", upper=True)
        normalized_arm = _required_text(arm_key, "arm_key")
        normalized_profile = _required_text(
            execution_profile_hash, "execution_profile_hash"
        )
        normalized_regime = _required_text(regime, "regime", upper=True)
        window_start = _non_negative_int(
            window_start_ms, "window_start_ms"
        )
        as_of = _non_negative_int(as_of_ms, "as_of_ms")
        if window_start > as_of:
            raise ValueError("window_start_ms must be <= as_of_ms")
        page_limit = _non_negative_int(limit, "limit")
        if page_limit < 1 or page_limit > 1_000:
            raise ValueError("limit must be an integer from 1 to 1000")
        normalized_revision = (
            None
            if evidence_revision is None
            else _required_text(evidence_revision, "evidence_revision")
        )

        async with self._write_lock:
            began = False
            try:
                await self._db.conn.execute("BEGIN IMMEDIATE")
                began = True
                rows = await self._db.fetchall(
                    """SELECT
                        claim.claim_id,
                        claim.lease_id,
                        claim.claimed_at_ms,
                        claim.terminal_at_ms,
                        claim.result_payload_json,
                        authority.lease_generation,
                        authority.evidence_revision
                    FROM v1469_paid_execution_claims AS claim
                    JOIN v1469_paid_execution_claim_authority AS authority
                      ON authority.claim_id = claim.claim_id
                    JOIN v1469_arm_leases AS lease
                      ON lease.arm_key = claim.arm_key
                     AND lease.lease_id = claim.lease_id
                     AND lease.environment = claim.environment
                     AND lease.symbol = claim.symbol
                    WHERE claim.environment = ?
                      AND claim.symbol = ?
                      AND claim.arm_key = ?
                      AND authority.execution_profile_hash = ?
                      AND authority.regime = ?
                      AND claim.status = 'TERMINAL'
                      AND (
                        COALESCE(json_extract(
                            claim.result_payload_json, '$.schema'
                        ), '') <> 'v1469.paid-no-fill.1'
                        OR COALESCE(json_extract(
                            claim.result_payload_json, '$.outcome'
                        ), '') <> 'NO_FILL'
                      )
                      AND claim.terminal_at_ms >= ?
                      AND claim.terminal_at_ms <= ?
                    ORDER BY claim.terminal_at_ms DESC, claim.claim_id DESC
                    LIMIT ?""",
                    (
                        scope_environment,
                        scope_symbol,
                        normalized_arm,
                        normalized_profile,
                        normalized_regime,
                        window_start,
                        as_of,
                        page_limit + 1,
                    ),
                )
                truncated = len(rows) > page_limit
                bounded_rows = rows[:page_limit]

                pnl_values: list[float] = []
                evaluable_terminal_times: list[int] = []
                hard_loss_marker = False
                if bounded_rows:
                    lease_ids = {
                        _required_text(
                            row.get("lease_id"), "durable lease_id"
                        )
                        for row in bounded_rows
                    }
                    if len(lease_ids) != 1:
                        raise V1469PaidClaimPersistenceError(
                            "paid probation evidence crosses lease lineages"
                        )

                    lineage_rows = sorted(
                        bounded_rows,
                        key=lambda row: (
                            int(row.get("claimed_at_ms") or 0),
                            str(row.get("claim_id") or ""),
                        ),
                    )
                    prior_generation = 0
                    revision_by_generation: dict[int, str] = {}
                    for row in lineage_rows:
                        lease_generation = _non_negative_int(
                            row.get("lease_generation"),
                            "durable lease_generation",
                        )
                        if (
                            lease_generation < 1
                            or lease_generation < prior_generation
                        ):
                            raise V1469PaidClaimPersistenceError(
                                "paid probation lease lineage is not monotonic"
                            )
                        durable_revision = _required_text(
                            row.get("evidence_revision"),
                            "durable evidence_revision",
                        )
                        prior_revision = revision_by_generation.setdefault(
                            lease_generation, durable_revision
                        )
                        if prior_revision != durable_revision:
                            raise V1469PaidClaimPersistenceError(
                                "paid probation lease generation has mixed revisions"
                            )
                        prior_generation = lease_generation

                        raw_payload = row.get("result_payload_json")
                        try:
                            payload = json.loads(str(raw_payload))
                        except (
                            TypeError,
                            ValueError,
                            json.JSONDecodeError,
                        ) as exc:
                            raise V1469PaidClaimPersistenceError(
                                "paid probation terminal result is invalid JSON"
                            ) from exc
                        if not isinstance(payload, dict):
                            raise V1469PaidClaimPersistenceError(
                                "paid probation terminal result must be an object"
                            )
                        if payload.get("schema") == "v1469.paid-no-fill.1":
                            if payload.get("outcome") != "NO_FILL":
                                raise V1469PaidClaimPersistenceError(
                                    "paid no-fill terminal result has invalid outcome"
                                )
                            # NO_FILL is a durable terminal clock change, but
                            # not a paid fill or PnL observation.
                            continue
                        raw_pnl = payload.get("fee_net_pnl_usdc")
                        if isinstance(raw_pnl, bool):
                            raise V1469PaidClaimPersistenceError(
                                "paid probation terminal result has invalid fee-net PnL"
                            )
                        try:
                            pnl = float(raw_pnl)
                        except (
                            TypeError,
                            ValueError,
                            OverflowError,
                        ) as exc:
                            raise V1469PaidClaimPersistenceError(
                                "paid probation terminal result has invalid fee-net PnL"
                            ) from exc
                        if not isfinite(pnl):
                            raise V1469PaidClaimPersistenceError(
                                "paid probation terminal result has invalid fee-net PnL"
                            )
                        pnl_values.append(pnl)
                        evaluable_terminal_times.append(
                            int(row["terminal_at_ms"])
                        )
                        hard_loss_marker = hard_loss_marker or any(
                            payload.get(marker) is True
                            for marker in (
                                "hard_loss_marker",
                                "hard_loss",
                                "risk_policy_hard_loss",
                            )
                        )

                latest_terminal_at = (
                    max(evaluable_terminal_times)
                    if evaluable_terminal_times
                    else None
                )
                wins = sum(pnl > 0.0 for pnl in pnl_values)
                fee_net_paid_pnl = fsum(pnl_values)
                watermark_payload = {
                    "schema": "v1469.paid-promotion-evidence-watermark.1",
                    "environment": scope_environment,
                    "symbol": scope_symbol,
                    "arm_key": normalized_arm,
                    "execution_profile_hash": normalized_profile,
                    "regime": normalized_regime,
                    "window_start_ms": window_start,
                    "as_of_ms": as_of,
                    "limit": page_limit,
                    "truncated": truncated,
                    "rows": [
                        {
                            "claim_id": str(row["claim_id"]),
                            "lease_id": str(row["lease_id"]),
                            "claimed_at_ms": int(row["claimed_at_ms"]),
                            "terminal_at_ms": int(row["terminal_at_ms"]),
                            "result_payload_json": str(
                                row["result_payload_json"]
                            ),
                            "lease_generation": int(
                                row["lease_generation"]
                            ),
                            "evidence_revision": str(
                                row["evidence_revision"]
                            ),
                        }
                        for row in bounded_rows
                    ],
                }
                watermark = hashlib.sha256(
                    json.dumps(
                        watermark_payload,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest()
                clock = await self._db.fetchone(
                    """SELECT revision
                    FROM v1469_paid_terminal_evidence_clocks
                    WHERE environment = ? AND symbol = ?
                      AND arm_key = ? AND execution_profile_hash = ?
                      AND regime = ?""",
                    (
                        scope_environment,
                        scope_symbol,
                        normalized_arm,
                        normalized_profile,
                        normalized_regime,
                    ),
                )
                clock_revision = (
                    0 if clock is None else int(clock["revision"])
                )
                if normalized_revision is not None:
                    await self._db.conn.execute(
                        """INSERT INTO
                            v1469_paid_promotion_evidence_snapshots (
                                environment, symbol, arm_key,
                                execution_profile_hash, regime,
                                evidence_revision, window_start_ms, as_of_ms,
                                evidence_limit, clock_revision,
                                evidence_watermark, terminal_fills, wins,
                                fee_net_paid_pnl, hard_loss_marker,
                                latest_terminal_at_ms, truncated,
                                created_at_ms
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                      ?, ?, ?, ?, ?)
                            ON CONFLICT (
                                environment, symbol, arm_key,
                                execution_profile_hash, regime,
                                evidence_revision
                            ) DO UPDATE SET
                                window_start_ms = excluded.window_start_ms,
                                as_of_ms = excluded.as_of_ms,
                                evidence_limit = excluded.evidence_limit,
                                clock_revision = excluded.clock_revision,
                                evidence_watermark = excluded.evidence_watermark,
                                terminal_fills = excluded.terminal_fills,
                                wins = excluded.wins,
                                fee_net_paid_pnl = excluded.fee_net_paid_pnl,
                                hard_loss_marker = excluded.hard_loss_marker,
                                latest_terminal_at_ms =
                                    excluded.latest_terminal_at_ms,
                                truncated = excluded.truncated,
                                created_at_ms = excluded.created_at_ms""",
                        (
                            scope_environment,
                            scope_symbol,
                            normalized_arm,
                            normalized_profile,
                            normalized_regime,
                            normalized_revision,
                            window_start,
                            as_of,
                            page_limit,
                            clock_revision,
                            watermark,
                            len(pnl_values),
                            wins,
                            fee_net_paid_pnl,
                            int(hard_loss_marker),
                            latest_terminal_at,
                            int(truncated),
                            as_of,
                        ),
                    )
                await self._db.conn.commit()
                began = False
                result: dict[str, Any] = {
                    "terminal_fills": len(pnl_values),
                    "wins": wins,
                    "fee_net_paid_pnl": fee_net_paid_pnl,
                    "hard_loss_marker": hard_loss_marker,
                    "latest_terminal_at": latest_terminal_at,
                    "truncated": truncated,
                }
                if normalized_revision is not None:
                    result.update(
                        {
                            "evidence_watermark": watermark,
                            "evidence_clock_revision": clock_revision,
                            "evidence_snapshot_durable": True,
                        }
                    )
                return result
            except asyncio.CancelledError:
                if began:
                    await asyncio.shield(self._db.conn.rollback())
                raise
            except Exception:
                if began:
                    await self._db.conn.rollback()
                raise

    async def claim(
        self,
        *,
        environment: str,
        symbol: str,
        opportunity_id: str,
        arm_key: str,
        lease_id: str,
        claimed_at_ms: int,
        idempotency_key: str,
        actor: str,
        payload: Mapping[str, Any] | None = None,
        created_at_ms: int | None = None,
        expected_lease_generation: int | None = None,
        expected_evidence_revision: str | None = None,
        expected_regime: str | None = None,
        expected_execution_profile_hash: str | None = None,
        expected_risk_policy_hash: str | None = None,
        approved_notional_usdc: float | None = None,
        reserved_loss_usdc: float | None = None,
        global_notional_cap_usdc: float | None = None,
        lane_notional_cap_usdc: float | None = None,
        daily_reserved_loss_cap_usdc: float | None = None,
        expected_risk_evidence_revision: str | None = None,
        expected_risk_active_day: str | None = None,
    ) -> PaidClaimMutationResult:
        """Atomically claim one opportunity before any paid order submission."""

        scope_environment = _required_text(
            environment, "environment", upper=True
        )
        scope_symbol = _required_text(symbol, "symbol", upper=True)
        opportunity = _required_text(
            opportunity_id, "opportunity_id"
        )
        normalized_arm = _required_text(arm_key, "arm_key")
        normalized_lease = _required_text(lease_id, "lease_id")
        expected_lease_generation_value = _optional_non_negative_int(
            expected_lease_generation,
            "expected_lease_generation",
        )
        if (
            expected_lease_generation_value is not None
            and expected_lease_generation_value < 1
        ):
            raise ValueError("expected_lease_generation must be positive")
        expected_evidence = _optional_text(
            expected_evidence_revision,
            "expected_evidence_revision",
        )
        expected_regime_value = _optional_text(
            expected_regime,
            "expected_regime",
            upper=True,
        )
        expected_profile_hash = _optional_text(
            expected_execution_profile_hash,
            "expected_execution_profile_hash",
        )
        expected_risk_hash = _optional_text(
            expected_risk_policy_hash,
            "expected_risk_policy_hash",
        )
        requested_approved = _optional_non_negative_float(
            approved_notional_usdc,
            "approved_notional_usdc",
        )
        requested_reserved = _optional_non_negative_float(
            reserved_loss_usdc,
            "reserved_loss_usdc",
        )
        cap_values = (
            _optional_non_negative_float(
                global_notional_cap_usdc,
                "global_notional_cap_usdc",
            ),
            _optional_non_negative_float(
                lane_notional_cap_usdc,
                "lane_notional_cap_usdc",
            ),
            _optional_non_negative_float(
                daily_reserved_loss_cap_usdc,
                "daily_reserved_loss_cap_usdc",
            ),
        )
        expected_risk_revision = _optional_text(
            expected_risk_evidence_revision,
            "expected_risk_evidence_revision",
        )
        expected_active_day = _optional_text(
            expected_risk_active_day,
            "expected_risk_active_day",
        )
        if (expected_risk_revision is None) != (expected_active_day is None):
            raise ValueError(
                "risk evidence revision and active day must be provided together"
            )
        if any(value is not None for value in cap_values) and not all(
            value is not None for value in cap_values
        ):
            raise ValueError(
                "all outstanding reservation caps must be provided together"
            )
        claimed_at = _non_negative_int(claimed_at_ms, "claimed_at_ms")
        created_at = (
            claimed_at
            if created_at_ms is None
            else _non_negative_int(created_at_ms, "created_at_ms")
        )
        if created_at < claimed_at:
            raise ValueError("created_at_ms must be >= claimed_at_ms")
        event_key = _required_text(
            idempotency_key,
            "idempotency_key",
            max_length=256,
        )
        normalized_actor = _required_text(actor, "actor")
        event_payload = _canonical_json(payload, name="payload")
        deterministic_claim_id = _claim_id(
            environment=scope_environment,
            symbol=scope_symbol,
            opportunity_id=opportunity,
        )

        async with self._write_lock:
            began = False
            try:
                await self._db.conn.execute("BEGIN IMMEDIATE")
                began = True
                existing = await self._scope_row(
                    scope_environment,
                    scope_symbol,
                    opportunity,
                )
                if existing is not None:
                    durable = _claim_from_row(existing)
                    if (
                        durable.claim_id != deterministic_claim_id
                        or durable.arm_key != normalized_arm
                        or durable.lease_id != normalized_lease
                    ):
                        raise V1469PaidClaimConflictError(
                            "market opportunity is already claimed by "
                            "a different arm or lease"
                        )
                    self._assert_requested_authority_matches(
                        durable,
                        expected_lease_generation=expected_lease_generation_value,
                        expected_evidence_revision=expected_evidence,
                        expected_regime=expected_regime_value,
                        expected_execution_profile_hash=expected_profile_hash,
                        expected_risk_policy_hash=expected_risk_hash,
                        approved_notional_usdc=requested_approved,
                        reserved_loss_usdc=requested_reserved,
                        expected_risk_active_day=expected_active_day,
                        expected_risk_evidence_revision=expected_risk_revision,
                    )
                    if durable.status == "CLAIMED":
                        if (
                            durable.risk_active_day is None
                            or durable.risk_evidence_revision is None
                        ):
                            raise V1469PaidClaimConflictError(
                                "paid claim has no durable risk evidence"
                            )
                        await self._assert_risk_evidence_current(
                            environment=durable.environment,
                            symbol=durable.symbol,
                            expected_risk_policy_hash=durable.risk_policy_hash,
                            expected_active_day=durable.risk_active_day,
                            expected_evidence_revision=(
                                durable.risk_evidence_revision
                            ),
                        )
                    await self._db.conn.rollback()
                    began = False
                    return PaidClaimMutationResult(
                        claim=durable,
                        applied=False,
                        replayed=True,
                    )

                lease = await self._load_current_claim_authority(
                    environment=scope_environment,
                    symbol=scope_symbol,
                    opportunity_id=opportunity,
                    arm_key=normalized_arm,
                    lease_id=normalized_lease,
                    claimed_at_ms=claimed_at,
                )
                authority = self._authority_snapshot_from_lease(
                    lease,
                    expected_lease_generation=expected_lease_generation_value,
                    expected_evidence_revision=expected_evidence,
                    expected_regime=expected_regime_value,
                    expected_execution_profile_hash=expected_profile_hash,
                    expected_risk_policy_hash=expected_risk_hash,
                    approved_notional_usdc=requested_approved,
                    reserved_loss_usdc=requested_reserved,
                )
                risk_active_day = (
                    expected_active_day or active_day_key(claimed_at)
                )
                if expected_risk_revision is None:
                    risk_evidence_revision = (
                        await self._current_risk_evidence_revision(
                            environment=scope_environment,
                            symbol=scope_symbol,
                            risk_policy_hash=str(authority["risk_policy_hash"]),
                            active_day=risk_active_day,
                        )
                    )
                else:
                    await self._assert_risk_evidence_current(
                        environment=scope_environment,
                        symbol=scope_symbol,
                        expected_risk_policy_hash=expected_risk_hash,
                        expected_active_day=risk_active_day,
                        expected_evidence_revision=expected_risk_revision,
                    )
                    risk_evidence_revision = expected_risk_revision
                authority.update(
                    risk_active_day=risk_active_day,
                    risk_evidence_revision=risk_evidence_revision,
                )
                if cap_values[0] is not None:
                    await self._assert_outstanding_reservation_capacity(
                        environment=scope_environment,
                        lane_code=str(lease["lane_code"]),
                        claimed_at_ms=claimed_at,
                        approved_notional_usdc=authority["approved_notional_usdc"],
                        reserved_loss_usdc=authority["reserved_loss_usdc"],
                        global_notional_cap_usdc=float(cap_values[0]),
                        lane_notional_cap_usdc=float(cap_values[1]),
                        daily_reserved_loss_cap_usdc=float(cap_values[2]),
                    )
                row = {
                    "claim_id": deterministic_claim_id,
                    "environment": scope_environment,
                    "symbol": scope_symbol,
                    "opportunity_id": opportunity,
                    "arm_key": normalized_arm,
                    "lease_id": normalized_lease,
                    "status": "CLAIMED",
                    "generation": 1,
                    "claimed_at_ms": claimed_at,
                    "terminal_at_ms": None,
                    "terminal_reason": None,
                    "result_payload_json": None,
                    "created_at_ms": created_at,
                    "updated_at_ms": created_at,
                }
                await self._db.conn.execute(
                    f"""INSERT INTO v1469_paid_execution_claims
                    ({", ".join(_CLAIM_COLUMNS)})
                    VALUES ({", ".join("?" for _ in _CLAIM_COLUMNS)})""",
                    tuple(row[column] for column in _CLAIM_COLUMNS),
                )
                authority_row = {"claim_id": deterministic_claim_id, **authority}
                authority_columns = ("claim_id", *_AUTHORITY_COLUMNS)
                await self._db.conn.execute(
                    f"""INSERT INTO v1469_paid_execution_claim_authority
                    ({", ".join(authority_columns)})
                    VALUES (
                        {", ".join("?" for _ in authority_columns)}
                    )""",
                    tuple(authority_row[column] for column in authority_columns),
                )
                await self._db.conn.execute(
                    """INSERT INTO v1469_paid_claim_risk_evidence (
                        claim_id, risk_active_day, risk_evidence_revision
                    ) VALUES (?, ?, ?)""",
                    (
                        deterministic_claim_id,
                        risk_active_day,
                        risk_evidence_revision,
                    ),
                )
                await self._insert_event(
                    idempotency_key=event_key,
                    row=row,
                    generation_before=0,
                    generation_after=1,
                    event_time_ms=claimed_at,
                    event_type="CLAIMED",
                    actor=normalized_actor,
                    payload_json=event_payload,
                )
                await self._db.conn.commit()
                began = False
                return PaidClaimMutationResult(
                    claim=_claim_from_row({**row, **authority}),
                    applied=True,
                    replayed=False,
                )
            except asyncio.CancelledError:
                if began:
                    await asyncio.shield(self._db.conn.rollback())
                raise
            except sqlite3.IntegrityError as exc:
                if began:
                    await self._db.conn.rollback()
                raise V1469PaidClaimConflictError(str(exc)) from exc
            except Exception:
                if began:
                    await self._db.conn.rollback()
                raise

    async def terminalize_claim(
        self,
        *,
        claim_id: str,
        expected_generation: int,
        terminal_at_ms: int,
        terminal_reason: str,
        idempotency_key: str,
        actor: str,
        result_payload: Mapping[str, Any] | None = None,
    ) -> PaidClaimMutationResult:
        return await self._finish_claim(
            claim_id=claim_id,
            expected_generation=expected_generation,
            terminal_at_ms=terminal_at_ms,
            terminal_reason=terminal_reason,
            idempotency_key=idempotency_key,
            actor=actor,
            result_payload=result_payload,
            target_status="TERMINAL",
        )

    async def transition_submission(
        self,
        *,
        claim_id: str,
        expected_generation: int,
        target_status: str,
        transition_at_ms: int,
        idempotency_key: str,
        actor: str,
        payload: Mapping[str, Any] | None = None,
    ) -> PaidClaimMutationResult:
        """CAS a non-terminal submit state, preserving crash ambiguity.

        ``UNKNOWN`` is intentionally durable: callers may only move it to
        ``SUBMITTED`` after exchange-visible client-order-id reconciliation.
        """
        target = _required_text(target_status, "target_status", upper=True)
        allowed = {
            "CLAIMED": {"SUBMITTING"},
            "SUBMITTING": {"UNKNOWN", "SUBMITTED"},
            "UNKNOWN": {"UNKNOWN", "SUBMITTED"},
        }
        claim_key = _required_text(claim_id, "claim_id")
        expected = _non_negative_int(expected_generation, "expected_generation")
        at_ms = _non_negative_int(transition_at_ms, "transition_at_ms")
        event_key = _required_text(idempotency_key, "idempotency_key", max_length=256)
        event_actor = _required_text(actor, "actor")
        payload_json = _canonical_json(payload, name="payload")
        async with self._write_lock:
            began = False
            try:
                await self._db.conn.execute("BEGIN IMMEDIATE")
                began = True
                row = await self._claim_row(claim_key)
                if row is None:
                    raise V1469PaidClaimConflictError("paid claim is missing")
                current = _claim_from_row(row)
                if current.generation != expected or target not in allowed.get(current.status, set()):
                    raise V1469PaidClaimConflictError("invalid paid submission transition")
                if current.status == "CLAIMED" and target == "SUBMITTING":
                    await self._assert_submission_authority_current(
                        current, transition_at_ms=at_ms
                    )
                if at_ms < current.updated_at_ms:
                    raise ValueError("transition time must be monotonic")
                generation_after = expected + 1
                cursor = await self._db.conn.execute(
                    """UPDATE v1469_paid_execution_claims
                    SET status = ?, generation = ?, updated_at_ms = ?
                    WHERE claim_id = ? AND status = ? AND generation = ?""",
                    (target, generation_after, at_ms, claim_key, current.status, expected),
                )
                if cursor.rowcount != 1:
                    raise V1469PaidClaimConflictError("paid claim generation changed")
                updated = dict(row)
                updated.update(status=target, generation=generation_after, updated_at_ms=at_ms)
                await self._insert_event(
                    idempotency_key=event_key, row=updated,
                    generation_before=expected, generation_after=generation_after,
                    event_time_ms=at_ms, event_type=target, actor=event_actor,
                    payload_json=payload_json,
                )
                await self._db.conn.commit()
                began = False
                return PaidClaimMutationResult(_claim_from_row(updated), True, False)
            except asyncio.CancelledError:
                if began:
                    await asyncio.shield(self._db.conn.rollback())
                raise
            except sqlite3.IntegrityError as exc:
                if began:
                    await self._db.conn.rollback()
                raise V1469PaidClaimConflictError(str(exc)) from exc
            except Exception:
                if began:
                    await self._db.conn.rollback()
                raise

    async def abandon_claim(
        self,
        *,
        claim_id: str,
        expected_generation: int,
        abandoned_at_ms: int,
        terminal_reason: str,
        idempotency_key: str,
        actor: str,
        result_payload: Mapping[str, Any] | None = None,
    ) -> PaidClaimMutationResult:
        return await self._finish_claim(
            claim_id=claim_id,
            expected_generation=expected_generation,
            terminal_at_ms=abandoned_at_ms,
            terminal_reason=terminal_reason,
            idempotency_key=idempotency_key,
            actor=actor,
            result_payload=result_payload,
            target_status="ABANDONED",
        )

    async def _finish_claim(
        self,
        *,
        claim_id: str,
        expected_generation: int,
        terminal_at_ms: int,
        terminal_reason: str,
        idempotency_key: str,
        actor: str,
        result_payload: Mapping[str, Any] | None,
        target_status: str,
    ) -> PaidClaimMutationResult:
        normalized_claim_id = _required_text(claim_id, "claim_id")
        expected = _non_negative_int(
            expected_generation, "expected_generation"
        )
        terminal_at = _non_negative_int(
            terminal_at_ms, "terminal_at_ms"
        )
        reason = _required_text(terminal_reason, "terminal_reason")
        event_key = _required_text(
            idempotency_key,
            "idempotency_key",
            max_length=256,
        )
        normalized_actor = _required_text(actor, "actor")
        result_json = _canonical_json(
            result_payload,
            name="result_payload",
        )
        if target_status not in {"TERMINAL", "ABANDONED"}:
            raise ValueError("target_status must be terminal")

        async with self._write_lock:
            began = False
            try:
                await self._db.conn.execute("BEGIN IMMEDIATE")
                began = True
                current_row = await self._claim_row(normalized_claim_id)
                if current_row is None:
                    raise V1469PaidClaimConflictError("paid claim is missing")
                current = _claim_from_row(current_row)
                if terminal_at < max(
                    current.claimed_at_ms,
                    current.created_at_ms,
                ):
                    raise ValueError(
                        "terminal time must be >= claim creation time"
                    )
                if current.status in {"TERMINAL", "ABANDONED"}:
                    if (
                        current.status == target_status
                        and current.terminal_at_ms == terminal_at
                        and current.terminal_reason == reason
                        and _canonical_json(
                            current.result_payload,
                            name="durable result_payload",
                        )
                        == result_json
                    ):
                        await self._db.conn.rollback()
                        began = False
                        return PaidClaimMutationResult(
                            claim=current,
                            applied=False,
                            replayed=True,
                        )
                    raise V1469PaidClaimConflictError(
                        "paid claim is already terminal with "
                        "different transition data"
                    )
                if current.generation != expected:
                    raise V1469PaidClaimConflictError(
                        "paid claim generation changed"
                    )
                if (
                    target_status == "TERMINAL"
                    and current.status != "SUBMITTED"
                ):
                    raise V1469PaidClaimConflictError(
                        "paid claim must be SUBMITTED before TERMINAL"
                    )
                if (
                    target_status == "ABANDONED"
                    and current.status not in {"CLAIMED", "SUBMITTING"}
                ):
                    raise V1469PaidClaimConflictError(
                        "only unambiguous unsubmitted claim may be ABANDONED"
                    )

                generation_after = expected + 1
                cursor = await self._db.conn.execute(
                    """UPDATE v1469_paid_execution_claims
                    SET status = ?, generation = ?, terminal_at_ms = ?,
                        terminal_reason = ?, result_payload_json = ?,
                        updated_at_ms = ?
                    WHERE claim_id = ? AND status = ? AND generation = ?""",
                    (
                        target_status,
                        generation_after,
                        terminal_at,
                        reason,
                        result_json,
                        terminal_at,
                        normalized_claim_id,
                        current.status,
                        expected,
                    ),
                )
                if cursor.rowcount != 1:
                    raise V1469PaidClaimConflictError(
                        "paid claim terminal CAS lost"
                    )
                terminal_row = dict(current_row)
                terminal_row.update(
                    {
                        "status": target_status,
                        "generation": generation_after,
                        "terminal_at_ms": terminal_at,
                        "terminal_reason": reason,
                        "result_payload_json": result_json,
                        "updated_at_ms": terminal_at,
                    }
                )
                await self._insert_event(
                    idempotency_key=event_key,
                    row=terminal_row,
                    generation_before=expected,
                    generation_after=generation_after,
                    event_time_ms=terminal_at,
                    event_type=target_status,
                    actor=normalized_actor,
                    payload_json=result_json,
                )
                await self._db.conn.commit()
                began = False
                return PaidClaimMutationResult(
                    claim=_claim_from_row(terminal_row),
                    applied=True,
                    replayed=False,
                )
            except asyncio.CancelledError:
                if began:
                    await asyncio.shield(self._db.conn.rollback())
                raise
            except sqlite3.IntegrityError as exc:
                if began:
                    await self._db.conn.rollback()
                raise V1469PaidClaimConflictError(str(exc)) from exc
            except Exception:
                if began:
                    await self._db.conn.rollback()
                raise

    async def _assert_submission_authority_current(
        self,
        claim: DurablePaidExecutionClaim,
        *,
        transition_at_ms: int,
    ) -> None:
        lease = await self._load_current_claim_authority(
            environment=claim.environment,
            symbol=claim.symbol,
            opportunity_id=claim.opportunity_id,
            arm_key=claim.arm_key,
            lease_id=claim.lease_id,
            claimed_at_ms=transition_at_ms,
        )
        self._authority_snapshot_from_lease(
            lease,
            expected_lease_generation=claim.lease_generation,
            expected_evidence_revision=claim.evidence_revision,
            expected_regime=claim.regime,
            expected_execution_profile_hash=claim.execution_profile_hash,
            expected_risk_policy_hash=claim.risk_policy_hash,
            approved_notional_usdc=claim.approved_notional_usdc,
            reserved_loss_usdc=claim.reserved_loss_usdc,
        )
        if (
            claim.risk_active_day is None
            or claim.risk_evidence_revision is None
        ):
            raise V1469PaidClaimConflictError(
                "paid claim has no durable risk evidence"
            )
        if active_day_key(transition_at_ms) != claim.risk_active_day:
            raise V1469PaidClaimConflictError(
                "daily-risk active day changed before paid submission"
            )
        await self._assert_risk_evidence_current(
            environment=claim.environment,
            symbol=claim.symbol,
            expected_risk_policy_hash=claim.risk_policy_hash,
            expected_active_day=claim.risk_active_day,
            expected_evidence_revision=claim.risk_evidence_revision,
        )

    async def _assert_outstanding_reservation_capacity(
        self,
        *,
        environment: str,
        lane_code: str,
        claimed_at_ms: int,
        approved_notional_usdc: float,
        reserved_loss_usdc: float,
        global_notional_cap_usdc: float,
        lane_notional_cap_usdc: float,
        daily_reserved_loss_cap_usdc: float,
    ) -> None:
        day_ms = 24 * 60 * 60 * 1000
        tpe_offset_ms = 8 * 60 * 60 * 1000
        day_start_ms = (
            (claimed_at_ms + tpe_offset_ms) // day_ms * day_ms
            - tpe_offset_ms
        )
        day_end_ms = day_start_ms + day_ms
        aggregate = await self._db.fetchone(
            """SELECT
                COALESCE(SUM(authority.approved_notional_usdc), 0.0)
                    AS global_notional,
                COALESCE(SUM(CASE
                    WHEN claim.claimed_at_ms >= ?
                     AND claim.claimed_at_ms < ?
                    THEN authority.reserved_loss_usdc ELSE 0.0 END), 0.0)
                    AS daily_reserved_loss
            FROM v1469_paid_execution_claims AS claim
            JOIN v1469_paid_execution_claim_authority AS authority
              ON authority.claim_id = claim.claim_id
            LEFT JOIN v1469_paid_claim_risk_evidence AS risk
              ON risk.claim_id = claim.claim_id
            WHERE claim.environment = ?
              AND claim.status IN (
                  'CLAIMED', 'SUBMITTING', 'UNKNOWN', 'SUBMITTED'
              )""",
            (day_start_ms, day_end_ms, environment),
        )
        lane = await self._db.fetchone(
            """SELECT
                COALESCE(SUM(authority.approved_notional_usdc), 0.0)
                    AS lane_notional,
                SUM(CASE WHEN lease.lease_id IS NULL THEN 1 ELSE 0 END)
                    AS unattributed_count
            FROM v1469_paid_execution_claims AS claim
            JOIN v1469_paid_execution_claim_authority AS authority
              ON authority.claim_id = claim.claim_id
            LEFT JOIN v1469_paid_claim_risk_evidence AS risk
              ON risk.claim_id = claim.claim_id
            LEFT JOIN v1469_arm_leases AS lease
              ON lease.arm_key = claim.arm_key
             AND lease.lease_id = claim.lease_id
            WHERE claim.environment = ?
              AND claim.status IN (
                  'CLAIMED', 'SUBMITTING', 'UNKNOWN', 'SUBMITTED'
              )
              AND (lease.lane_code = ? OR lease.lease_id IS NULL)""",
            (environment, _required_text(lane_code, "lane_code", upper=True)),
        )
        if int((lane or {}).get("unattributed_count") or 0) > 0:
            raise V1469PaidClaimConflictError(
                "unattributed outstanding paid reservation"
            )
        global_total = float((aggregate or {}).get("global_notional") or 0.0)
        daily_total = float(
            (aggregate or {}).get("daily_reserved_loss") or 0.0
        )
        lane_total = float((lane or {}).get("lane_notional") or 0.0)
        checks = (
            (
                "global outstanding notional cap exceeded",
                global_total + approved_notional_usdc,
                global_notional_cap_usdc,
            ),
            (
                "lane outstanding notional cap exceeded",
                lane_total + approved_notional_usdc,
                lane_notional_cap_usdc,
            ),
            (
                "daily reserved loss cap exceeded",
                daily_total + reserved_loss_usdc,
                daily_reserved_loss_cap_usdc,
            ),
        )
        for reason, requested, cap in checks:
            if requested > cap + 1e-12:
                raise V1469PaidClaimConflictError(reason)

    async def _current_risk_evidence_revision(
        self,
        *,
        environment: str,
        symbol: str,
        risk_policy_hash: str,
        active_day: str,
    ) -> str:
        rows = await self._db.fetchall(
            """SELECT event_id, occurred_at_ms, fee_net_pnl_delta_usdc,
                      risk_policy_hash, event_type
            FROM v1469_daily_risk_events
            WHERE environment = ? AND symbol = ? AND active_day = ?
            ORDER BY occurred_at_ms, event_id
            LIMIT 10001""",
            (environment, symbol, active_day),
        )
        if len(rows) > 10_000:
            raise V1469PaidClaimConflictError(
                "unsafe v1.4.69 daily-risk ledger: active-day event "
                "count exceeds bounded CAS limit (10000)"
            )
        return canonical_sha256(
            {
                "schema": PHASE_C_SCHEMA,
                "active_day": active_day,
                "risk_policy_hash": risk_policy_hash,
                "events": [
                    {
                        "event_id": str(row["event_id"]),
                        "occurred_at_ms": int(row["occurred_at_ms"]),
                        "fee_net_pnl_delta_usdc": float(
                            row["fee_net_pnl_delta_usdc"]
                        ),
                        "risk_policy_hash": str(row["risk_policy_hash"]),
                        "event_type": str(row["event_type"]),
                    }
                    for row in rows
                ],
            }
        )

    async def _assert_risk_evidence_current(
        self,
        *,
        environment: str,
        symbol: str,
        expected_risk_policy_hash: str | None,
        expected_active_day: str,
        expected_evidence_revision: str,
    ) -> None:
        if expected_risk_policy_hash is None:
            raise V1469PaidClaimConflictError(
                "risk evidence CAS requires a risk-policy hash"
            )
        current_revision = await self._current_risk_evidence_revision(
            environment=environment,
            symbol=symbol,
            risk_policy_hash=expected_risk_policy_hash,
            active_day=expected_active_day,
        )
        if current_revision != expected_evidence_revision:
            raise V1469PaidClaimConflictError(
                "daily-risk evidence changed before paid submission"
            )

    @staticmethod
    def _assert_requested_authority_matches(
        durable: DurablePaidExecutionClaim,
        *,
        expected_lease_generation: int | None,
        expected_evidence_revision: str | None,
        expected_regime: str | None,
        expected_execution_profile_hash: str | None,
        expected_risk_policy_hash: str | None,
        approved_notional_usdc: float | None,
        reserved_loss_usdc: float | None,
        expected_risk_active_day: str | None,
        expected_risk_evidence_revision: str | None,
    ) -> None:
        requested = {
            "lease_generation": expected_lease_generation,
            "evidence_revision": expected_evidence_revision,
            "regime": expected_regime,
            "execution_profile_hash": expected_execution_profile_hash,
            "risk_policy_hash": expected_risk_policy_hash,
            "approved_notional_usdc": approved_notional_usdc,
            "reserved_loss_usdc": reserved_loss_usdc,
            "risk_active_day": expected_risk_active_day,
            "risk_evidence_revision": expected_risk_evidence_revision,
        }
        actual = {
            "lease_generation": durable.lease_generation,
            "evidence_revision": durable.evidence_revision,
            "regime": durable.regime,
            "execution_profile_hash": durable.execution_profile_hash,
            "risk_policy_hash": durable.risk_policy_hash,
            "approved_notional_usdc": durable.approved_notional_usdc,
            "reserved_loss_usdc": durable.reserved_loss_usdc,
            "risk_active_day": durable.risk_active_day,
            "risk_evidence_revision": durable.risk_evidence_revision,
        }
        mismatches = [
            name
            for name, expected in requested.items()
            if expected is not None and actual[name] != expected
        ]
        if mismatches:
            raise V1469PaidClaimConflictError(
                "paid claim authority snapshot differs: "
                + ",".join(sorted(mismatches))
            )
    @staticmethod
    def _authority_snapshot_from_lease(
        lease: Mapping[str, Any],
        *,
        expected_lease_generation: int | None,
        expected_evidence_revision: str | None,
        expected_regime: str | None,
        expected_execution_profile_hash: str | None,
        expected_risk_policy_hash: str | None,
        approved_notional_usdc: float | None,
        reserved_loss_usdc: float | None,
    ) -> dict[str, Any]:
        lease_generation = _non_negative_int(
            lease.get("generation"), "lease.generation"
        )
        if lease_generation < 1:
            raise V1469PaidClaimPersistenceError(
                "active lease generation must be positive"
            )
        lease_cap = _non_negative_float(
            lease.get("notional_cap_usdc"), "lease.notional_cap_usdc"
        )
        snapshot = {
            "lease_generation": lease_generation,
            "evidence_revision": _required_text(
                lease.get("evidence_revision"), "lease.evidence_revision"
            ),
            "regime": _required_text(
                lease.get("coarse_regime"), "lease.coarse_regime", upper=True
            ),
            "execution_profile_hash": _required_text(
                lease.get("execution_profile_hash"),
                "lease.execution_profile_hash",
            ),
            "risk_policy_hash": _required_text(
                lease.get("risk_policy_hash"), "lease.risk_policy_hash"
            ),
            "approved_notional_usdc": (
                lease_cap
                if approved_notional_usdc is None
                else approved_notional_usdc
            ),
            "reserved_loss_usdc": (
                0.0 if reserved_loss_usdc is None else reserved_loss_usdc
            ),
        }
        if snapshot["approved_notional_usdc"] > lease_cap + 1e-12:
            raise V1469PaidClaimConflictError(
                "approved notional exceeds current lease cap"
            )
        expected = {
            "lease_generation": expected_lease_generation,
            "evidence_revision": expected_evidence_revision,
            "regime": expected_regime,
            "execution_profile_hash": expected_execution_profile_hash,
            "risk_policy_hash": expected_risk_policy_hash,
        }
        mismatches = [
            name for name, value in expected.items()
            if value is not None and snapshot[name] != value
        ]
        if mismatches:
            raise V1469PaidClaimConflictError(
                "current lease authority differs: "
                + ",".join(sorted(mismatches))
            )
        return snapshot
    async def _load_current_claim_authority(
        self,
        *,
        environment: str,
        symbol: str,
        opportunity_id: str,
        arm_key: str,
        lease_id: str,
        claimed_at_ms: int,
    ) -> Mapping[str, Any]:
        opportunity = await self._db.fetchone(
            """SELECT opportunity_id
            FROM v1469_market_opportunities
            WHERE opportunity_id = ? AND environment = ? AND symbol = ?""",
            (opportunity_id, environment, symbol),
        )
        if opportunity is None:
            raise V1469PaidClaimConflictError(
                "market opportunity is missing or has a different scope"
            )
        lease = await self._db.fetchone(
            """SELECT arm_key, lease_id, generation, lane_code,
                      coarse_regime, execution_profile_hash,
                      risk_policy_hash, evidence_revision,
                      notional_cap_usdc
            FROM v1469_arm_leases
            WHERE arm_key = ? AND lease_id = ?
              AND environment = ? AND symbol = ?
              AND status = 'ACTIVE' AND expires_at_ms > ?""",
            (
                arm_key,
                lease_id,
                environment,
                symbol,
                claimed_at_ms,
            ),
        )
        if lease is None:
            raise V1469PaidClaimConflictError(
                "matching active, unexpired lease is required"
            )
        return lease

    async def _scope_row(
        self,
        environment: str,
        symbol: str,
        opportunity_id: str,
    ) -> dict[str, Any] | None:
        return await self._db.fetchone(
            f"""SELECT {_CLAIM_SELECT}
            FROM v1469_paid_execution_claims AS claim
            JOIN v1469_paid_execution_claim_authority AS authority
              ON authority.claim_id = claim.claim_id
            LEFT JOIN v1469_paid_claim_risk_evidence AS risk
              ON risk.claim_id = claim.claim_id
            WHERE claim.environment = ? AND claim.symbol = ?
              AND claim.opportunity_id = ?""",
            (environment, symbol, opportunity_id),
        )

    async def _claim_row(
        self,
        claim_id: str,
    ) -> dict[str, Any] | None:
        return await self._db.fetchone(
            f"""SELECT {_CLAIM_SELECT}
            FROM v1469_paid_execution_claims AS claim
            JOIN v1469_paid_execution_claim_authority AS authority
              ON authority.claim_id = claim.claim_id
            LEFT JOIN v1469_paid_claim_risk_evidence AS risk
              ON risk.claim_id = claim.claim_id
            WHERE claim.claim_id = ?""",
            (claim_id,),
        )

    async def _insert_event(
        self,
        *,
        idempotency_key: str,
        row: Mapping[str, Any],
        generation_before: int,
        generation_after: int,
        event_time_ms: int,
        event_type: str,
        actor: str,
        payload_json: str,
    ) -> None:
        await self._db.conn.execute(
            """INSERT INTO v1469_paid_execution_claim_events (
                idempotency_key, claim_id, opportunity_id, arm_key,
                lease_id, generation_before, generation_after,
                event_time_ms, event_type, actor, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                idempotency_key,
                row["claim_id"],
                row["opportunity_id"],
                row["arm_key"],
                row["lease_id"],
                generation_before,
                generation_after,
                event_time_ms,
                event_type,
                actor,
                payload_json,
            ),
        )


__all__ = [
    "DurablePaidExecutionClaim",
    "PaidClaimMutationResult",
    "V1469PaidClaimConflictError",
    "V1469PaidClaimPersistenceError",
    "V1469PaidExecutionClaimRepository",
]
