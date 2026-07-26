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
import sqlite3
from typing import Any, Mapping

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
    status: str
    generation: int
    claimed_at_ms: int
    terminal_at_ms: int | None
    terminal_reason: str | None
    result_payload: Mapping[str, Any] | None
    created_at_ms: int
    updated_at_ms: int


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
    if status == "CLAIMED" and generation != 1:
        raise V1469PaidClaimPersistenceError(
            "CLAIMED row must have generation 1"
        )
    if status not in {"CLAIMED", "SUBMITTING", "UNKNOWN", "SUBMITTED", "TERMINAL", "ABANDONED"}:
        raise V1469PaidClaimPersistenceError(
            f"unknown durable paid-claim status: {status}"
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
        trigger_rows = await self._db.fetchall(
            """SELECT name FROM sqlite_master
            WHERE type = 'trigger' AND name LIKE 'trg_v1469_paid_claim_%'"""
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
        }
        missing_claim = sorted(required_claim_columns - claim_columns)
        missing_event = sorted(required_event_columns - event_columns)
        missing_triggers = sorted(required_triggers - triggers)
        if missing_claim or missing_event or missing_triggers:
            raise V1469PaidClaimPersistenceError(
                "unsafe v1.4.69 paid-claim schema: "
                f"missing_claim_columns={missing_claim}, "
                f"missing_event_columns={missing_event}, "
                f"missing_triggers={missing_triggers}"
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
            f"""SELECT {", ".join(_CLAIM_COLUMNS)}
            FROM v1469_paid_execution_claims
            WHERE environment = ? AND symbol = ? AND opportunity_id = ?""",
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
                    await self._db.conn.rollback()
                    began = False
                    return PaidClaimMutationResult(
                        claim=durable,
                        applied=False,
                        replayed=True,
                    )

                await self._assert_claim_inputs_exist(
                    environment=scope_environment,
                    symbol=scope_symbol,
                    opportunity_id=opportunity,
                    arm_key=normalized_arm,
                    lease_id=normalized_lease,
                    claimed_at_ms=claimed_at,
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
                    claim=_claim_from_row(row),
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

    async def _assert_claim_inputs_exist(
        self,
        *,
        environment: str,
        symbol: str,
        opportunity_id: str,
        arm_key: str,
        lease_id: str,
        claimed_at_ms: int,
    ) -> None:
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
            """SELECT arm_key
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

    async def _scope_row(
        self,
        environment: str,
        symbol: str,
        opportunity_id: str,
    ) -> dict[str, Any] | None:
        return await self._db.fetchone(
            f"""SELECT {", ".join(_CLAIM_COLUMNS)}
            FROM v1469_paid_execution_claims
            WHERE environment = ? AND symbol = ? AND opportunity_id = ?""",
            (environment, symbol, opportunity_id),
        )

    async def _claim_row(
        self,
        claim_id: str,
    ) -> dict[str, Any] | None:
        return await self._db.fetchone(
            f"""SELECT {", ".join(_CLAIM_COLUMNS)}
            FROM v1469_paid_execution_claims
            WHERE claim_id = ?""",
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
