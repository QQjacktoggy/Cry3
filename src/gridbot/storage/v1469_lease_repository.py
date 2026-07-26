"""Durable CAS persistence for v1.4.69 Adaptive Arm leases.

The pure arbiter proposes authority; this repository is the only component in
this module that can durably grant, renew, or revoke that authority.  Every
successful lease mutation and its append-only audit event share one SQLite
transaction.  The repository owns no exchange or order API.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import hashlib
import json
from math import isfinite
import sqlite3
from typing import Any, Mapping

from src.gridbot.mainnet.v1469_arm_arbiter import (
    ArmIdentity,
    CurrentLease,
    LeaseAction,
    LeasePhase,
    LeaseProposal,
    LeaseRevocation,
)
from src.gridbot.storage.database import Database


class V1469LeaseConflictError(RuntimeError):
    """A CAS predicate, active-arm invariant, or idempotency key conflicted."""


class V1469LeasePersistenceError(RuntimeError):
    """The durable lease schema or an existing durable row is unsafe."""


@dataclass(frozen=True, slots=True)
class LeaseContext:
    """Runtime fields that are deliberately outside the pure arm identity."""

    environment: str
    symbol: str
    execution_profile_schema: str
    notional_cap_usdc: float
    risk_policy_hash: str
    evidence_as_of_ms: int
    owner_id: str
    boot_id: str


@dataclass(frozen=True, slots=True)
class DurableArmLease:
    arm_key: str
    lease_id: str
    generation: int
    environment: str
    symbol: str
    lane_code: str
    effective_side: str
    strategy: str
    coarse_regime: str
    execution_profile_id: str
    execution_profile_schema: str
    execution_profile_hash: str
    phase: LeasePhase
    status: str
    notional_cap_usdc: float
    risk_policy_hash: str
    evidence_revision: str
    evidence_as_of_ms: int
    issued_at_ms: int
    renewed_at_ms: int
    expires_at_ms: int
    owner_id: str
    boot_id: str
    demotion_reason: str | None
    demoted_at_ms: int | None
    cooldown_until_ms: int | None
    created_at_ms: int
    updated_at_ms: int

    def as_current_lease(self) -> CurrentLease:
        return CurrentLease(
            arm_key=self.arm_key,
            phase=self.phase,
            regime=self.coarse_regime,
            evidence_revision=self.evidence_revision,
            issued_at_ms=self.issued_at_ms,
            expires_at_ms=self.expires_at_ms,
        )


@dataclass(frozen=True, slots=True)
class LeaseMutationResult:
    lease: DurableArmLease
    applied: bool
    replayed: bool
    event_generation: int


_LEASE_COLUMNS = (
    "arm_key",
    "lease_id",
    "generation",
    "environment",
    "symbol",
    "lane_code",
    "effective_side",
    "strategy",
    "coarse_regime",
    "execution_profile_id",
    "execution_profile_schema",
    "execution_profile_hash",
    "phase",
    "status",
    "notional_cap_usdc",
    "risk_policy_hash",
    "evidence_revision",
    "evidence_as_of_ms",
    "issued_at_ms",
    "renewed_at_ms",
    "expires_at_ms",
    "owner_id",
    "boot_id",
    "demotion_reason",
    "demoted_at_ms",
    "cooldown_until_ms",
    "created_at_ms",
    "updated_at_ms",
)

_EVENT_COLUMNS = (
    "idempotency_key",
    "arm_key",
    "lease_id",
    "opportunity_id",
    "candidate_id",
    "generation_before",
    "generation_after",
    "event_time_ms",
    "event_type",
    "actor",
    "payload_json",
)

_IDENTITY_COLUMN_VALUES = (
    ("lane_code", "lane_code"),
    ("effective_side", "side"),
    ("strategy", "strategy"),
    ("coarse_regime", "regime"),
    ("execution_profile_id", "execution_profile_id"),
    ("execution_profile_hash", "execution_profile_hash"),
)


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("lease event payload must be valid JSON") from exc
    if len(encoded.encode("utf-8")) > 4_096:
        raise ValueError("lease event payload exceeds 4096 bytes")
    return encoded


def _required_text(value: Any, name: str, *, upper: bool = False) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} must be non-empty")
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


def _expected_generation(value: int | None) -> int:
    if value is None:
        return 0
    return _non_negative_int(value, "expected_generation")


def _lease_id(arm_key: str) -> str:
    digest = hashlib.sha256(
        f"v1469.lease.1|{arm_key}".encode("utf-8")
    ).hexdigest()
    # Stable per arm_key because historical arm events have a foreign key to
    # lease_id; changing it on a later re-grant would break that audit chain.
    return f"v1469l_{digest}"


def _normalize_identity(identity: ArmIdentity) -> ArmIdentity:
    if not isinstance(identity, ArmIdentity):
        raise TypeError("identity must be ArmIdentity")
    arm_key = _required_text(identity.arm_key, "identity.arm_key")
    side = _required_text(identity.side, "identity.side", upper=True)
    regime = _required_text(identity.regime, "identity.regime", upper=True)
    if side not in {"LONG", "SHORT"}:
        raise ValueError("identity.side must be LONG or SHORT")
    if regime not in {
        "TREND_UP",
        "TREND_DOWN",
        "TREND",
        "RANGE",
        "SHOCK",
        "UNCERTAIN",
        "UNKNOWN",
    }:
        raise ValueError("identity.regime is unsupported")
    return ArmIdentity(
        arm_key=arm_key,
        lane_code=_required_text(
            identity.lane_code, "identity.lane_code", upper=True
        ),
        side=side,
        strategy=_required_text(identity.strategy, "identity.strategy"),
        regime=regime,
        execution_profile_id=_required_text(
            identity.execution_profile_id,
            "identity.execution_profile_id",
            upper=True,
        ),
        execution_profile_hash=_required_text(
            identity.execution_profile_hash,
            "identity.execution_profile_hash",
        ),
    )


def _normalize_context(context: LeaseContext) -> LeaseContext:
    if not isinstance(context, LeaseContext):
        raise TypeError("context must be LeaseContext")
    try:
        cap = float(context.notional_cap_usdc)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("notional_cap_usdc must be finite and positive") from exc
    if not isfinite(cap) or cap <= 0 or cap > 50:
        raise ValueError("notional_cap_usdc must be in (0, 50]")
    return LeaseContext(
        environment=_required_text(
            context.environment, "context.environment", upper=True
        ),
        symbol=_required_text(context.symbol, "context.symbol", upper=True),
        execution_profile_schema=_required_text(
            context.execution_profile_schema,
            "context.execution_profile_schema",
        ),
        notional_cap_usdc=cap,
        risk_policy_hash=_required_text(
            context.risk_policy_hash, "context.risk_policy_hash"
        ),
        evidence_as_of_ms=_non_negative_int(
            context.evidence_as_of_ms, "context.evidence_as_of_ms"
        ),
        owner_id=_required_text(context.owner_id, "context.owner_id"),
        boot_id=_required_text(context.boot_id, "context.boot_id"),
    )


def _lease_from_row(row: Mapping[str, Any]) -> DurableArmLease:
    try:
        return DurableArmLease(
            arm_key=str(row["arm_key"]),
            lease_id=str(row["lease_id"]),
            generation=int(row["generation"]),
            environment=str(row["environment"]),
            symbol=str(row["symbol"]),
            lane_code=str(row["lane_code"]),
            effective_side=str(row["effective_side"]),
            strategy=str(row["strategy"]),
            coarse_regime=str(row["coarse_regime"]),
            execution_profile_id=str(row["execution_profile_id"]),
            execution_profile_schema=str(
                row["execution_profile_schema"]
            ),
            execution_profile_hash=str(row["execution_profile_hash"]),
            phase=LeasePhase(str(row["phase"])),
            status=str(row["status"]),
            notional_cap_usdc=float(row["notional_cap_usdc"]),
            risk_policy_hash=str(row["risk_policy_hash"]),
            evidence_revision=str(row["evidence_revision"]),
            evidence_as_of_ms=int(row["evidence_as_of_ms"]),
            issued_at_ms=int(row["issued_at_ms"]),
            renewed_at_ms=int(row["renewed_at_ms"]),
            expires_at_ms=int(row["expires_at_ms"]),
            owner_id=str(row["owner_id"]),
            boot_id=str(row["boot_id"]),
            demotion_reason=(
                None
                if row["demotion_reason"] is None
                else str(row["demotion_reason"])
            ),
            demoted_at_ms=(
                None
                if row["demoted_at_ms"] is None
                else int(row["demoted_at_ms"])
            ),
            cooldown_until_ms=(
                None
                if row["cooldown_until_ms"] is None
                else int(row["cooldown_until_ms"])
            ),
            created_at_ms=int(row["created_at_ms"]),
            updated_at_ms=int(row["updated_at_ms"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise V1469LeasePersistenceError(
            "malformed durable v1.4.69 lease row"
        ) from exc


class V1469LeaseRepository:
    """Persist arbiter lease proposals with generation/revision CAS."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._write_lock = asyncio.Lock()

    async def assert_schema_ready(self) -> None:
        lease_columns = {
            str(row.get("name") or "")
            for row in await self._db.fetchall(
                "PRAGMA table_info(v1469_arm_leases)"
            )
        }
        event_columns = {
            str(row.get("name") or "")
            for row in await self._db.fetchall(
                "PRAGMA table_info(v1469_arm_events)"
            )
        }
        missing_lease = sorted(set(_LEASE_COLUMNS) - lease_columns)
        missing_event = sorted(set(_EVENT_COLUMNS) - event_columns)
        schema_rows = await self._db.fetchall(
            """SELECT type, name, sql FROM sqlite_master
            WHERE name IN (
                'idx_v1469_one_active_arm_per_symbol',
                'trg_v1469_arm_events_no_update',
                'trg_v1469_arm_events_no_delete'
            )"""
        )
        schema_names = {str(row["name"]) for row in schema_rows}
        required_schema = {
            "idx_v1469_one_active_arm_per_symbol",
            "trg_v1469_arm_events_no_update",
            "trg_v1469_arm_events_no_delete",
        }
        index_sql = next(
            (
                str(row.get("sql") or "").upper()
                for row in schema_rows
                if row["name"] == "idx_v1469_one_active_arm_per_symbol"
            ),
            "",
        )
        partial_unique_ok = (
            "CREATE UNIQUE INDEX" in index_sql
            and "WHERE STATUS = 'ACTIVE'" in " ".join(index_sql.split())
        )
        if (
            missing_lease
            or missing_event
            or not required_schema.issubset(schema_names)
            or not partial_unique_ok
        ):
            raise V1469LeasePersistenceError(
                "unsafe v1.4.69 lease schema: "
                f"missing_lease_columns={missing_lease}, "
                f"missing_event_columns={missing_event}, "
                f"missing_objects={sorted(required_schema - schema_names)}, "
                f"partial_unique_ok={partial_unique_ok}"
            )

    async def get_lease(self, arm_key: str) -> DurableArmLease | None:
        key = _required_text(arm_key, "arm_key")
        row = await self._db.fetchone(
            "SELECT * FROM v1469_arm_leases WHERE arm_key = ?",
            (key,),
        )
        return None if row is None else _lease_from_row(row)

    async def get_active_lease(
        self,
        *,
        environment: str,
        symbol: str,
        now_ms: int,
    ) -> DurableArmLease | None:
        """Return usable authority only; stale ACTIVE rows fail closed."""

        scope_environment = _required_text(
            environment, "environment", upper=True
        )
        scope_symbol = _required_text(symbol, "symbol", upper=True)
        now = _non_negative_int(now_ms, "now_ms")
        await self.expire_stale_active(
            environment=scope_environment,
            symbol=scope_symbol,
            now_ms=now,
            actor="v1469_lease_repository",
        )
        rows = await self._db.fetchall(
            """SELECT * FROM v1469_arm_leases
            WHERE environment = ? AND symbol = ?
              AND status = 'ACTIVE' AND expires_at_ms > ?
            ORDER BY arm_key
            LIMIT 2""",
            (scope_environment, scope_symbol, now),
        )
        if len(rows) > 1:
            raise V1469LeasePersistenceError(
                "multiple usable ACTIVE leases violate symbol authority"
        )
        return None if not rows else _lease_from_row(rows[0])

    async def expire_stale_active(
        self,
        *,
        environment: str,
        symbol: str,
        now_ms: int,
        actor: str,
    ) -> LeaseMutationResult | None:
        """Close one stale ACTIVE row and its EXPIRED event atomically."""

        scope_environment = _required_text(
            environment, "environment", upper=True
        )
        scope_symbol = _required_text(symbol, "symbol", upper=True)
        now = _non_negative_int(now_ms, "now_ms")
        event_actor = _required_text(actor, "actor")
        async with self._write_lock:
            began = False
            try:
                await self._db.conn.execute("BEGIN IMMEDIATE")
                began = True
                result = await self._expire_stale_active_scope(
                    environment=scope_environment,
                    symbol=scope_symbol,
                    now_ms=now,
                    actor=event_actor,
                    exclude_arm_key=None,
                )
                if result is None:
                    await self._db.conn.rollback()
                    began = False
                    return None
                await self._db.conn.commit()
                began = False
                return result
            except asyncio.CancelledError:
                if began:
                    await asyncio.shield(self._db.conn.rollback())
                raise
            except sqlite3.IntegrityError as exc:
                if began:
                    await self._db.conn.rollback()
                raise V1469LeaseConflictError(str(exc)) from exc
            except Exception:
                if began:
                    await self._db.conn.rollback()
                raise

    async def load_current_lease(
        self,
        *,
        environment: str,
        symbol: str,
        now_ms: int,
    ) -> CurrentLease | None:
        lease = await self.get_active_lease(
            environment=environment,
            symbol=symbol,
            now_ms=now_ms,
        )
        return None if lease is None else lease.as_current_lease()

    async def apply_proposal(
        self,
        identity: ArmIdentity,
        proposal: LeaseProposal,
        context: LeaseContext,
        *,
        expected_generation: int | None,
        expected_evidence_revision: str | None,
        event_time_ms: int,
        idempotency_key: str,
        actor: str,
    ) -> LeaseMutationResult:
        """Atomically apply a GRANT or RENEW proposal and its audit event."""

        normalized_identity = _normalize_identity(identity)
        normalized_context = _normalize_context(context)
        if not isinstance(proposal, LeaseProposal):
            raise TypeError("proposal must be LeaseProposal")
        if proposal.action not in {LeaseAction.GRANT, LeaseAction.RENEW}:
            raise ValueError("only GRANT or RENEW proposals are durable mutations")
        if proposal.arm_key != normalized_identity.arm_key:
            raise ValueError("proposal arm_key does not match identity")
        if proposal.phase is None:
            raise ValueError("proposal phase is required")
        if not isinstance(proposal.phase, LeasePhase):
            raise ValueError("proposal phase is unsupported")
        new_revision = _required_text(
            proposal.evidence_revision, "proposal.evidence_revision"
        )
        if proposal.expires_at_ms is None:
            raise ValueError("proposal.expires_at_ms is required")
        event_time = _non_negative_int(event_time_ms, "event_time_ms")
        expires_at = _non_negative_int(
            proposal.expires_at_ms, "proposal.expires_at_ms"
        )
        if expires_at <= event_time:
            raise ValueError("proposal expiry must be after event_time_ms")
        if normalized_context.evidence_as_of_ms > event_time:
            raise ValueError("evidence_as_of_ms cannot be in the future")
        if (
            proposal.phase == LeasePhase.PROBATION
            and normalized_context.notional_cap_usdc > 25
        ):
            raise ValueError("PROBATION notional cap cannot exceed 25 USDC")

        generation_before = _expected_generation(expected_generation)
        if proposal.action == LeaseAction.RENEW and generation_before < 1:
            raise ValueError("RENEW requires a positive expected_generation")
        expected_revision = (
            None
            if expected_evidence_revision is None
            else _required_text(
                expected_evidence_revision, "expected_evidence_revision"
            )
        )
        if generation_before > 0 and expected_revision is None:
            raise ValueError(
                "existing lease CAS requires expected_evidence_revision"
            )
        if generation_before == 0 and expected_revision is not None:
            raise ValueError(
                "new lease CAS cannot have expected_evidence_revision"
            )
        if proposal.action == LeaseAction.RENEW and new_revision == expected_revision:
            raise V1469LeaseConflictError(
                "renewal requires a new evidence revision"
            )

        key = _required_text(idempotency_key, "idempotency_key")
        event_actor = _required_text(actor, "actor")
        generation_after = generation_before + 1
        lease_id = _lease_id(normalized_identity.arm_key)
        event_type = (
            "LEASE_RENEWED"
            if proposal.action == LeaseAction.RENEW
            else (
                "LIVE_GRANTED"
                if proposal.phase == LeasePhase.LIVE
                else "PROBATION_GRANTED"
            )
        )
        payload_json = _canonical_json(
            {
                "schema": "v1469.lease-mutation.1",
                "operation": proposal.action.value,
                "identity": asdict(normalized_identity),
                "context": asdict(normalized_context),
                "proposal": {
                    "arm_key": proposal.arm_key,
                    "phase": proposal.phase.value,
                    "evidence_revision": new_revision,
                    "expires_at_ms": expires_at,
                    "blockers": list(proposal.blockers),
                },
                "expected_generation": generation_before,
                "expected_evidence_revision": expected_revision,
            }
        )
        event_row = {
            "idempotency_key": key,
            "arm_key": normalized_identity.arm_key,
            "lease_id": lease_id,
            "opportunity_id": None,
            "candidate_id": None,
            "generation_before": (
                None if generation_before == 0 else generation_before
            ),
            "generation_after": generation_after,
            "event_time_ms": event_time,
            "event_type": event_type,
            "actor": event_actor,
            "payload_json": payload_json,
        }
        async with self._write_lock:
            began = False
            try:
                await self._db.conn.execute("BEGIN IMMEDIATE")
                began = True
                replay = await self._exact_event_replay(event_row)
                if replay:
                    current = await self._lease_row(
                        normalized_identity.arm_key
                    )
                    if current is None:
                        raise V1469LeasePersistenceError(
                            "idempotent lease event has no durable lease"
                        )
                    await self._db.conn.rollback()
                    began = False
                    return LeaseMutationResult(
                        lease=_lease_from_row(current),
                        applied=False,
                        replayed=True,
                        event_generation=generation_after,
                    )

                if proposal.action == LeaseAction.GRANT:
                    # Release a stale *other* arm before applying the new
                    # symbol authority.  The expiry row/event and the new
                    # grant row/event share this transaction, so a failed
                    # grant cannot leave a partially-cleaned authority state.
                    await self._expire_stale_active_scope(
                        environment=normalized_context.environment,
                        symbol=normalized_context.symbol,
                        now_ms=event_time,
                        actor=event_actor,
                        exclude_arm_key=normalized_identity.arm_key,
                    )
                current = await self._lease_row(normalized_identity.arm_key)
                if proposal.action == LeaseAction.GRANT:
                    row = await self._grant_row(
                        identity=normalized_identity,
                        proposal=proposal,
                        context=normalized_context,
                        current=current,
                        generation_before=generation_before,
                        generation_after=generation_after,
                        expected_revision=expected_revision,
                        event_time_ms=event_time,
                        expires_at_ms=expires_at,
                        evidence_revision=new_revision,
                        lease_id=lease_id,
                    )
                else:
                    row = await self._renew_row(
                        identity=normalized_identity,
                        proposal=proposal,
                        context=normalized_context,
                        current=current,
                        generation_before=generation_before,
                        generation_after=generation_after,
                        expected_revision=expected_revision or "",
                        event_time_ms=event_time,
                        expires_at_ms=expires_at,
                        evidence_revision=new_revision,
                    )
                await self._insert_event(event_row)
                await self._db.conn.commit()
                began = False
                return LeaseMutationResult(
                    lease=_lease_from_row(row),
                    applied=True,
                    replayed=False,
                    event_generation=generation_after,
                )
            except asyncio.CancelledError:
                if began:
                    await asyncio.shield(self._db.conn.rollback())
                raise
            except sqlite3.IntegrityError as exc:
                if began:
                    await self._db.conn.rollback()
                raise V1469LeaseConflictError(str(exc)) from exc
            except Exception:
                if began:
                    await self._db.conn.rollback()
                raise

    async def revoke(
        self,
        revocation: LeaseRevocation,
        *,
        expected_generation: int,
        expected_evidence_revision: str,
        idempotency_key: str,
        actor: str,
    ) -> LeaseMutationResult:
        """Atomically revoke one exact ACTIVE generation and append its event."""

        if not isinstance(revocation, LeaseRevocation):
            raise TypeError("revocation must be LeaseRevocation")
        arm_key = _required_text(revocation.arm_key, "revocation.arm_key")
        reason = _required_text(revocation.reason, "revocation.reason")
        event_time = _non_negative_int(
            revocation.revoke_at_ms, "revocation.revoke_at_ms"
        )
        generation_before = _expected_generation(expected_generation)
        if generation_before < 1:
            raise ValueError("revoke requires a positive expected_generation")
        expected_revision = _required_text(
            expected_evidence_revision, "expected_evidence_revision"
        )
        generation_after = generation_before + 1
        lease_id = _lease_id(arm_key)
        key = _required_text(idempotency_key, "idempotency_key")
        event_actor = _required_text(actor, "actor")
        payload_json = _canonical_json(
            {
                "schema": "v1469.lease-mutation.1",
                "operation": "REVOKE",
                "arm_key": arm_key,
                "reason": reason,
                "revoke_at_ms": event_time,
                "expected_generation": generation_before,
                "expected_evidence_revision": expected_revision,
            }
        )
        event_row = {
            "idempotency_key": key,
            "arm_key": arm_key,
            "lease_id": lease_id,
            "opportunity_id": None,
            "candidate_id": None,
            "generation_before": generation_before,
            "generation_after": generation_after,
            "event_time_ms": event_time,
            "event_type": "LEASE_REVOKED",
            "actor": event_actor,
            "payload_json": payload_json,
        }
        async with self._write_lock:
            began = False
            try:
                await self._db.conn.execute("BEGIN IMMEDIATE")
                began = True
                replay = await self._exact_event_replay(event_row)
                if replay:
                    current = await self._lease_row(arm_key)
                    if current is None:
                        raise V1469LeasePersistenceError(
                            "idempotent revoke event has no durable lease"
                        )
                    await self._db.conn.rollback()
                    began = False
                    return LeaseMutationResult(
                        lease=_lease_from_row(current),
                        applied=False,
                        replayed=True,
                        event_generation=generation_after,
                    )
                current = await self._lease_row(arm_key)
                if current is None:
                    raise V1469LeaseConflictError("lease_missing")
                if int(current["generation"]) != generation_before:
                    raise V1469LeaseConflictError(
                        "lease_generation_changed"
                    )
                if str(current["evidence_revision"]) != expected_revision:
                    raise V1469LeaseConflictError(
                        "lease_evidence_revision_changed"
                    )
                if str(current["lease_id"]) != lease_id:
                    raise V1469LeasePersistenceError(
                        "durable lease_id is not deterministic"
                    )
                if str(current["status"]) != "ACTIVE":
                    raise V1469LeaseConflictError("lease_not_active")
                if event_time < int(current["renewed_at_ms"]):
                    raise V1469LeaseConflictError(
                        "revocation_precedes_last_renewal"
                    )
                cursor = await self._db.conn.execute(
                    """UPDATE v1469_arm_leases
                    SET generation = ?, status = 'REVOKED',
                        demotion_reason = ?, demoted_at_ms = ?,
                        cooldown_until_ms = NULL, updated_at_ms = ?
                    WHERE arm_key = ? AND lease_id = ?
                      AND generation = ? AND evidence_revision = ?
                      AND status = 'ACTIVE'""",
                    (
                        generation_after,
                        reason,
                        event_time,
                        event_time,
                        arm_key,
                        lease_id,
                        generation_before,
                        expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise V1469LeaseConflictError("lease_revoke_cas_lost")
                await self._insert_event(event_row)
                row = await self._lease_row(arm_key)
                if row is None:
                    raise V1469LeasePersistenceError(
                        "revoked lease disappeared before commit"
                    )
                await self._db.conn.commit()
                began = False
                return LeaseMutationResult(
                    lease=_lease_from_row(row),
                    applied=True,
                    replayed=False,
                    event_generation=generation_after,
                )
            except asyncio.CancelledError:
                if began:
                    await asyncio.shield(self._db.conn.rollback())
                raise
            except sqlite3.IntegrityError as exc:
                if began:
                    await self._db.conn.rollback()
                raise V1469LeaseConflictError(str(exc)) from exc
            except Exception:
                if began:
                    await self._db.conn.rollback()
                raise

    async def _lease_row(self, arm_key: str) -> dict[str, Any] | None:
        return await self._db.fetchone(
            "SELECT * FROM v1469_arm_leases WHERE arm_key = ?",
            (arm_key,),
        )

    async def _exact_event_replay(
        self, event_row: Mapping[str, Any]
    ) -> bool:
        existing = await self._db.fetchone(
            f"""SELECT {", ".join(_EVENT_COLUMNS)}
            FROM v1469_arm_events WHERE idempotency_key = ?""",
            (event_row["idempotency_key"],),
        )
        if existing is None:
            return False
        if existing != dict(event_row):
            raise V1469LeaseConflictError(
                "idempotency key reused for a different lease mutation"
            )
        return True

    async def _insert_event(self, event_row: Mapping[str, Any]) -> None:
        await self._db.conn.execute(
            f"""INSERT INTO v1469_arm_events
            ({", ".join(_EVENT_COLUMNS)})
            VALUES ({", ".join("?" for _ in _EVENT_COLUMNS)})""",
            tuple(event_row[column] for column in _EVENT_COLUMNS),
        )

    async def _expire_stale_active_scope(
        self,
        *,
        environment: str,
        symbol: str,
        now_ms: int,
        actor: str,
        exclude_arm_key: str | None,
    ) -> LeaseMutationResult | None:
        predicates = [
            "environment = ?",
            "symbol = ?",
            "status = 'ACTIVE'",
            "expires_at_ms <= ?",
        ]
        params: list[Any] = [environment, symbol, now_ms]
        if exclude_arm_key is not None:
            predicates.append("arm_key <> ?")
            params.append(exclude_arm_key)
        rows = await self._db.fetchall(
            f"""SELECT * FROM v1469_arm_leases
            WHERE {" AND ".join(predicates)}
            ORDER BY arm_key
            LIMIT 2""",
            tuple(params),
        )
        if not rows:
            return None
        if len(rows) > 1:
            raise V1469LeasePersistenceError(
                "multiple stale ACTIVE leases violate symbol authority"
            )
        current = rows[0]
        arm_key = str(current["arm_key"])
        lease_id = str(current["lease_id"])
        expected_lease_id = _lease_id(arm_key)
        if lease_id != expected_lease_id:
            raise V1469LeasePersistenceError(
                "durable lease_id is not deterministic"
            )
        generation_before = int(current["generation"])
        generation_after = generation_before + 1
        evidence_revision = str(current["evidence_revision"])
        expires_at_ms = int(current["expires_at_ms"])
        event_key_digest = hashlib.sha256(
            (
                "v1469.expire.1|"
                f"{lease_id}|{generation_before}|{evidence_revision}|"
                f"{expires_at_ms}"
            ).encode("utf-8")
        ).hexdigest()
        event_row = {
            "idempotency_key": f"v1469x_{event_key_digest}",
            "arm_key": arm_key,
            "lease_id": lease_id,
            "opportunity_id": None,
            "candidate_id": None,
            "generation_before": generation_before,
            "generation_after": generation_after,
            "event_time_ms": now_ms,
            "event_type": "EXPIRED",
            "actor": actor,
            "payload_json": _canonical_json(
                {
                    "schema": "v1469.lease-mutation.1",
                    "operation": "EXPIRE",
                    "arm_key": arm_key,
                    "expired_at_ms": expires_at_ms,
                    "observed_expired_at_ms": now_ms,
                    "expected_generation": generation_before,
                    "expected_evidence_revision": evidence_revision,
                }
            ),
        }
        if await self._exact_event_replay(event_row):
            # A row and event are committed together, so finding the event
            # while its source row is still ACTIVE indicates corruption.
            raise V1469LeasePersistenceError(
                "EXPIRED event exists while lease remains ACTIVE"
            )
        cursor = await self._db.conn.execute(
            """UPDATE v1469_arm_leases
            SET generation = ?, status = 'EXPIRED',
                demotion_reason = 'lease_expired',
                demoted_at_ms = ?, cooldown_until_ms = NULL,
                updated_at_ms = ?
            WHERE arm_key = ? AND lease_id = ?
              AND generation = ? AND evidence_revision = ?
              AND status = 'ACTIVE' AND expires_at_ms <= ?""",
            (
                generation_after,
                now_ms,
                now_ms,
                arm_key,
                lease_id,
                generation_before,
                evidence_revision,
                now_ms,
            ),
        )
        if cursor.rowcount != 1:
            raise V1469LeaseConflictError("lease_expiry_cas_lost")
        await self._insert_event(event_row)
        row = await self._lease_row(arm_key)
        if row is None:
            raise V1469LeasePersistenceError(
                "expired lease disappeared before commit"
            )
        return LeaseMutationResult(
            lease=_lease_from_row(row),
            applied=True,
            replayed=False,
            event_generation=generation_after,
        )

    @staticmethod
    def _assert_existing_identity(
        current: Mapping[str, Any],
        identity: ArmIdentity,
        context: LeaseContext,
    ) -> None:
        if str(current["arm_key"]) != identity.arm_key:
            raise V1469LeaseConflictError("lease_arm_key_changed")
        for column, attribute in _IDENTITY_COLUMN_VALUES:
            if str(current[column]) != str(getattr(identity, attribute)):
                raise V1469LeaseConflictError(
                    f"lease_identity_changed:{column}"
                )
        if (
            str(current["execution_profile_schema"])
            != context.execution_profile_schema
        ):
            raise V1469LeaseConflictError(
                "lease_identity_changed:execution_profile_schema"
            )
        if (
            str(current["environment"]) != context.environment
            or str(current["symbol"]) != context.symbol
        ):
            raise V1469LeaseConflictError("lease_scope_changed")

    async def _grant_row(
        self,
        *,
        identity: ArmIdentity,
        proposal: LeaseProposal,
        context: LeaseContext,
        current: Mapping[str, Any] | None,
        generation_before: int,
        generation_after: int,
        expected_revision: str | None,
        event_time_ms: int,
        expires_at_ms: int,
        evidence_revision: str,
        lease_id: str,
    ) -> dict[str, Any]:
        active_other = await self._db.fetchone(
            """SELECT arm_key FROM v1469_arm_leases
            WHERE environment = ? AND symbol = ? AND status = 'ACTIVE'
              AND arm_key <> ?""",
            (context.environment, context.symbol, identity.arm_key),
        )
        if active_other is not None:
            raise V1469LeaseConflictError(
                f"active_lease_exists:{active_other['arm_key']}"
            )
        if current is None:
            if generation_before != 0:
                raise V1469LeaseConflictError(
                    "lease does not exist at expected generation"
                )
            created_at_ms = event_time_ms
        else:
            self._assert_existing_identity(current, identity, context)
            if int(current["generation"]) != generation_before:
                raise V1469LeaseConflictError(
                    "lease_generation_changed"
                )
            if str(current["evidence_revision"]) != expected_revision:
                raise V1469LeaseConflictError(
                    "lease_evidence_revision_changed"
                )
            if str(current["lease_id"]) != lease_id:
                raise V1469LeasePersistenceError(
                    "durable lease_id is not deterministic"
                )
            status = str(current["status"])
            if status == "ACTIVE":
                raise V1469LeaseConflictError(
                    "existing ACTIVE lease requires RENEW"
                )
            if status in {"COOLDOWN", "HALTED"}:
                raise V1469LeaseConflictError(
                    f"lease_status_blocks_grant:{status}"
                )
            if evidence_revision == expected_revision:
                raise V1469LeaseConflictError(
                    "re-grant requires a new evidence revision"
                )
            created_at_ms = int(current["created_at_ms"])

        row = {
            "arm_key": identity.arm_key,
            "lease_id": lease_id,
            "generation": generation_after,
            "environment": context.environment,
            "symbol": context.symbol,
            "lane_code": identity.lane_code,
            "effective_side": identity.side,
            "strategy": identity.strategy,
            "coarse_regime": identity.regime,
            "execution_profile_id": identity.execution_profile_id,
            "execution_profile_schema": context.execution_profile_schema,
            "execution_profile_hash": identity.execution_profile_hash,
            "phase": proposal.phase.value,
            "status": "ACTIVE",
            "notional_cap_usdc": context.notional_cap_usdc,
            "risk_policy_hash": context.risk_policy_hash,
            "evidence_revision": evidence_revision,
            "evidence_as_of_ms": context.evidence_as_of_ms,
            "issued_at_ms": event_time_ms,
            "renewed_at_ms": event_time_ms,
            "expires_at_ms": expires_at_ms,
            "owner_id": context.owner_id,
            "boot_id": context.boot_id,
            "demotion_reason": None,
            "demoted_at_ms": None,
            "cooldown_until_ms": None,
            "created_at_ms": created_at_ms,
            "updated_at_ms": event_time_ms,
        }
        if current is None:
            await self._db.conn.execute(
                f"""INSERT INTO v1469_arm_leases
                ({", ".join(_LEASE_COLUMNS)})
                VALUES ({", ".join("?" for _ in _LEASE_COLUMNS)})""",
                tuple(row[column] for column in _LEASE_COLUMNS),
            )
        else:
            assignments = ", ".join(
                f"{column} = ?"
                for column in _LEASE_COLUMNS
                if column not in {"arm_key", "created_at_ms"}
            )
            cursor = await self._db.conn.execute(
                f"""UPDATE v1469_arm_leases SET {assignments}
                WHERE arm_key = ? AND generation = ?
                  AND evidence_revision = ? AND status <> 'ACTIVE'""",
                (
                    *(
                        row[column]
                        for column in _LEASE_COLUMNS
                        if column not in {"arm_key", "created_at_ms"}
                    ),
                    identity.arm_key,
                    generation_before,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise V1469LeaseConflictError("lease_grant_cas_lost")
        return row

    async def _renew_row(
        self,
        *,
        identity: ArmIdentity,
        proposal: LeaseProposal,
        context: LeaseContext,
        current: Mapping[str, Any] | None,
        generation_before: int,
        generation_after: int,
        expected_revision: str,
        event_time_ms: int,
        expires_at_ms: int,
        evidence_revision: str,
    ) -> dict[str, Any]:
        if current is None:
            raise V1469LeaseConflictError("lease_missing")
        self._assert_existing_identity(current, identity, context)
        if int(current["generation"]) != generation_before:
            raise V1469LeaseConflictError("lease_generation_changed")
        if str(current["evidence_revision"]) != expected_revision:
            raise V1469LeaseConflictError(
                "lease_evidence_revision_changed"
            )
        if str(current["status"]) != "ACTIVE":
            raise V1469LeaseConflictError("lease_not_active")
        if int(current["expires_at_ms"]) <= event_time_ms:
            raise V1469LeaseConflictError("lease_expired")
        if proposal.phase.value != str(current["phase"]):
            raise V1469LeaseConflictError(
                "renewal cannot change lease phase"
            )
        if context.evidence_as_of_ms <= int(current["evidence_as_of_ms"]):
            raise V1469LeaseConflictError(
                "renewal requires newer evidence_as_of_ms"
            )
        row = dict(current)
        row.update(
            {
                "generation": generation_after,
                "notional_cap_usdc": context.notional_cap_usdc,
                "risk_policy_hash": context.risk_policy_hash,
                "evidence_revision": evidence_revision,
                "evidence_as_of_ms": context.evidence_as_of_ms,
                "renewed_at_ms": event_time_ms,
                "expires_at_ms": expires_at_ms,
                "owner_id": context.owner_id,
                "boot_id": context.boot_id,
                "updated_at_ms": event_time_ms,
            }
        )
        assignments = ", ".join(
            f"{column} = ?"
            for column in (
                "generation",
                "notional_cap_usdc",
                "risk_policy_hash",
                "evidence_revision",
                "evidence_as_of_ms",
                "renewed_at_ms",
                "expires_at_ms",
                "owner_id",
                "boot_id",
                "updated_at_ms",
            )
        )
        cursor = await self._db.conn.execute(
            f"""UPDATE v1469_arm_leases SET {assignments}
            WHERE arm_key = ? AND generation = ?
              AND evidence_revision = ? AND status = 'ACTIVE'
              AND expires_at_ms > ?""",
            (
                *(
                    row[column]
                    for column in (
                        "generation",
                        "notional_cap_usdc",
                        "risk_policy_hash",
                        "evidence_revision",
                        "evidence_as_of_ms",
                        "renewed_at_ms",
                        "expires_at_ms",
                        "owner_id",
                        "boot_id",
                        "updated_at_ms",
                    )
                ),
                identity.arm_key,
                generation_before,
                expected_revision,
                event_time_ms,
            ),
        )
        if cursor.rowcount != 1:
            raise V1469LeaseConflictError("lease_renew_cas_lost")
        return row


__all__ = [
    "DurableArmLease",
    "LeaseContext",
    "LeaseMutationResult",
    "V1469LeaseConflictError",
    "V1469LeasePersistenceError",
    "V1469LeaseRepository",
]
