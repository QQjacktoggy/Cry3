"""Fail-closed persistence for v1.4.64 adaptive-promotion evidence and leases."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import hashlib
import json
from math import isfinite
import sqlite3
from typing import Any, Mapping

from src.gridbot.storage.database import Database


_EVIDENCE_IDENTITY_FIELDS = (
    "environment",
    "symbol",
    "lane_code",
    "market_state",
    "effective_side",
    "strategy",
    "resolved_profile_hash",
    "profile_identity_schema",
    "registry_version",
    "registry_hash",
    "lane_definition_hash",
    "admission_policy_hash",
)
_LEASE_IDENTITY_FIELDS = (*_EVIDENCE_IDENTITY_FIELDS, "promotion_policy_hash")
_OUTCOMES = frozenset(
    {
        "tp1_first",
        "tp_first",
        "tp",
        "sl_first",
        "sl",
        "max_hold",
        "no_fill",
        "ambiguous_both",
    }
)
_LEASE_PHASES = frozenset({"PROBATION", "CONTROL"})
V1464_EVIDENCE_SCHEMA_VERSION = "v1464.sliding-evidence.1"
V1464_PROFILE_IDENTITY_SCHEMA = "v1464.stable-profile.1"
_EVIDENCE_SOURCE_TYPES = frozenset({"SHADOW", "SHADOW_DROP", "PAID"})
_LEASE_STATUSES = frozenset(
    {"ACTIVE", "COOLDOWN", "DEMOTED", "EXPIRED", "REVOKED", "HALTED"}
)
# Storage calls the fully promoted phase CONTROL; the pure lifecycle engine
# calls the same paid state LIVE.  Keep this translation centralized.
ENGINE_STATE_BY_STORAGE_PHASE = {
    "PROBATION": "PROBATION",
    "CONTROL": "LIVE",
}
_EVENT_TYPES = frozenset(
    {
        "EVALUATED",
        "PROBATION_GRANTED",
        "LEASE_RENEWED",
        "CONTROL_GRANTED",
        "COOLDOWN",
        "DEMOTED",
        "EXPIRED",
        "REVOKED",
        "ADMISSION_CONSUMED",
        "ADMISSION_BLOCKED",
        "HALTED",
    }
)

_SCHEMA_REQUIRED_COLUMNS = {
    "v1464_promotion_evidence": frozenset(
        {
            "opportunity_id",
            *_EVIDENCE_IDENTITY_FIELDS,
            "evidence_schema_version",
            "observed_at_ms",
            "terminal_at_ms",
            "outcome",
            "data_complete",
            "ambiguous",
            "diagnostic_only",
            "net_pnl_usdc",
            "source_type",
            "source_id",
            "source_payload_json",
            "evidence_hash",
            "created_at_ms",
        }
    ),
    "v1464_lane_promotion_leases": frozenset(
        {
            "cohort_key",
            "lease_id",
            "generation",
            *_LEASE_IDENTITY_FIELDS,
            "phase",
            "status",
            "notional_cap_usdc",
            "evidence_window_start_ms",
            "evidence_as_of_ms",
            "evidence_watermark",
            "evidence_snapshot_hash",
            "evidence_snapshot_json",
            "issued_at_ms",
            "renewed_at_ms",
            "expires_at_ms",
            "boot_id",
            "owner_id",
            "soft_failures",
            "demotion_reason",
            "demoted_at_ms",
            "cooldown_until_ms",
            "created_at_ms",
            "updated_at_ms",
        }
    ),
    "v1464_lane_promotion_events": frozenset(
        {
            "id",
            "idempotency_key",
            "cohort_key",
            "lease_id",
            "generation_before",
            "generation_after",
            "event_time_ms",
            "event_type",
            "actor",
            "payload_json",
        }
    ),
}
_SCHEMA_REQUIRED_TRIGGERS = frozenset(
    {
        "trg_v1464_promotion_events_no_update",
        "trg_v1464_promotion_events_no_delete",
    }
)
_SCHEMA_REQUIRED_SQL_MARKERS = {
    "v1464_promotion_evidence": (
        "V1464.SLIDING-EVIDENCE.1",
        "V1464.STABLE-PROFILE.1",
        "'SHADOW', 'SHADOW_DROP', 'PAID'",
    ),
    "v1464_lane_promotion_leases": (
        "V1464.STABLE-PROFILE.1",
        "'ACTIVE', 'COOLDOWN', 'DEMOTED', 'EXPIRED', 'REVOKED', 'HALTED'",
        "NOTIONAL_CAP_USDC <= 50.0",
        "COOLDOWN_UNTIL_MS > DEMOTED_AT_MS",
    ),
    "v1464_lane_promotion_events": ("'COOLDOWN'", "'HALTED'"),
}


class PromotionPersistenceError(RuntimeError):
    """Base class for persistence failures that must block promotion."""


class PromotionConflictError(PromotionPersistenceError):
    """An immutable evidence row or idempotency key was reused differently."""


class LeaseConflictError(PromotionPersistenceError):
    """A lease compare-and-swap generation did not match."""


class AdmissionClaimError(PromotionPersistenceError):
    """A paid admission could not atomically consume the requested lease."""


@dataclass(frozen=True, slots=True)
class PromotionCohort:
    environment: str
    symbol: str
    lane_code: str
    market_state: str
    effective_side: str
    strategy: str
    resolved_profile_hash: str
    profile_identity_schema: str
    registry_version: str
    registry_hash: str
    lane_definition_hash: str
    admission_policy_hash: str
    promotion_policy_hash: str = ""

    @property
    def key(self) -> str:
        if not self.promotion_policy_hash:
            raise ValueError("promotion_policy_hash is required for a lease key")
        return promotion_cohort_key(asdict(self))


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _required_text(payload: Mapping[str, Any], name: str) -> str:
    value = str(payload.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} must be non-empty")
    return value


def _integer(
    payload: Mapping[str, Any],
    name: str,
    *,
    minimum: int = 0,
) -> int:
    value = payload.get(name)
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return parsed


def _flag(payload: Mapping[str, Any], name: str, *, default: bool = False) -> int:
    value = payload.get(name, default)
    if not isinstance(value, (bool, int)) or value not in (False, True, 0, 1):
        raise ValueError(f"{name} must be boolean")
    return int(bool(value))


def _finite_number(
    payload: Mapping[str, Any],
    name: str,
    *,
    allow_none: bool = False,
) -> float | None:
    value = payload.get(name)
    if value is None and allow_none:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    return parsed


def _normalize_side(value: str) -> str:
    side = value.upper()
    if side not in {"LONG", "SHORT"}:
        raise ValueError("effective_side must be LONG or SHORT")
    return side


def _identity(payload: Mapping[str, Any], *, lease: bool) -> dict[str, str]:
    fields = _LEASE_IDENTITY_FIELDS if lease else _EVIDENCE_IDENTITY_FIELDS
    identity = {name: _required_text(payload, name) for name in fields}
    identity["effective_side"] = _normalize_side(identity["effective_side"])
    if identity["profile_identity_schema"] != V1464_PROFILE_IDENTITY_SCHEMA:
        raise ValueError(
            "unsupported profile_identity_schema: "
            f"{identity['profile_identity_schema']}"
        )
    return identity


def promotion_cohort_key(payload: Mapping[str, Any] | PromotionCohort) -> str:
    """Return the stable exact-cohort key, including the promotion policy."""

    source = asdict(payload) if isinstance(payload, PromotionCohort) else payload
    identity = _identity(source, lease=True)
    encoded = _canonical_json(identity).encode("utf-8")
    return "v1464_" + hashlib.sha256(encoded).hexdigest()


def _decode_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    decoded = dict(row)
    for name in (
        "source_payload_json",
        "evidence_snapshot_json",
        "payload_json",
    ):
        if name in decoded:
            raw = decoded.get(name)
            try:
                decoded[name.removesuffix("_json")] = json.loads(raw) if raw else {}
            except (TypeError, json.JSONDecodeError):
                decoded[name.removesuffix("_json")] = None
    return decoded


def lease_row_to_engine_snapshot(row: Mapping[str, Any]):
    """Convert one persisted lease row to the pure engine's lease snapshot.

    Inactive terminal rows intentionally re-enter the engine as SHADOW.  A
    persisted HALTED row remains HALTED.  Importing lazily keeps the storage
    module usable in migration/repository-only contexts.
    """

    from src.gridbot.mainnet.v1464_adaptive_promotion import (  # local import
        PromotionLeaseSnapshot,
        PromotionState,
    )

    status = _required_text(row, "status").upper()
    phase = _required_text(row, "phase").upper()
    if status == "ACTIVE":
        try:
            state = PromotionState(ENGINE_STATE_BY_STORAGE_PHASE[phase])
        except KeyError as exc:
            raise ValueError(f"unsupported storage lease phase: {phase}") from exc
    elif status == "COOLDOWN":
        state = PromotionState.COOLDOWN
    elif status == "HALTED":
        state = PromotionState.HALTED
    else:
        state = PromotionState.SHADOW
    return PromotionLeaseSnapshot(
        state=state,
        lease_id=_required_text(row, "lease_id"),
        cohort_key=_required_text(row, "cohort_key"),
        policy_hash=_required_text(row, "promotion_policy_hash"),
        issued_at_ms=_integer(row, "issued_at_ms"),
        expires_at_ms=_integer(row, "expires_at_ms"),
        evidence_revision=_required_text(row, "evidence_snapshot_hash"),
        evidence_as_of_ms=_integer(row, "evidence_as_of_ms"),
        soft_breach_count=_integer(row, "soft_failures"),
        cooldown_until_ms=(
            _integer(row, "cooldown_until_ms")
            if row.get("cooldown_until_ms") is not None
            else None
        ),
    )


class V1464PromotionRepository:
    """Repository whose mutation methods pair state and audit atomically."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._write_lock = asyncio.Lock()

    async def get_evidence(
        self,
        opportunity_id: str,
    ) -> dict[str, Any] | None:
        """Return one immutable evidence row by its durable opportunity id."""

        key = str(opportunity_id or "").strip()
        if not key:
            raise ValueError("opportunity_id must be non-empty")
        return await self._db.fetchone(
            """SELECT * FROM v1464_promotion_evidence
            WHERE opportunity_id = ?""",
            (key,),
        )

    async def schema_fingerprint(self) -> str:
        """Return a deterministic fingerprint of the installed v1.4.64 schema."""

        objects = await self._db.fetchall(
            """SELECT type, name, sql FROM sqlite_master
            WHERE name IN (
                'v1464_promotion_evidence',
                'v1464_lane_promotion_leases',
                'v1464_lane_promotion_events',
                'trg_v1464_promotion_events_no_update',
                'trg_v1464_promotion_events_no_delete'
            )
            ORDER BY type, name"""
        )
        columns: dict[str, list[dict[str, Any]]] = {}
        for table in sorted(_SCHEMA_REQUIRED_COLUMNS):
            rows = await self._db.fetchall(f"PRAGMA table_info({table})")
            columns[table] = [
                {
                    "name": str(row.get("name") or ""),
                    "type": str(row.get("type") or "").upper(),
                    "notnull": int(row.get("notnull") or 0),
                    "default": row.get("dflt_value"),
                    "pk": int(row.get("pk") or 0),
                }
                for row in rows
            ]
        payload = {
            "objects": [
                {
                    "type": str(row.get("type") or ""),
                    "name": str(row.get("name") or ""),
                    "sql": " ".join(str(row.get("sql") or "").split()),
                }
                for row in objects
            ],
            "columns": columns,
        }
        return hashlib.sha256(
            _canonical_json(payload).encode("utf-8")
        ).hexdigest()

    async def assert_schema_ready(self) -> str:
        """Fail closed unless columns, constraints, and append-only guards exist."""

        objects = await self._db.fetchall(
            """SELECT type, name, sql FROM sqlite_master
            WHERE name LIKE 'v1464_%'
               OR name LIKE 'trg_v1464_promotion_events_%'"""
        )
        by_name = {
            str(row.get("name") or ""): row
            for row in objects
            if str(row.get("name") or "")
        }
        problems: list[str] = []
        for table, required in _SCHEMA_REQUIRED_COLUMNS.items():
            rows = await self._db.fetchall(f"PRAGMA table_info({table})")
            actual = {
                str(row.get("name") or "")
                for row in rows
                if str(row.get("name") or "")
            }
            missing = sorted(required - actual)
            if missing:
                problems.append(f"{table}:missing_columns={','.join(missing)}")
            sql = " ".join(
                str((by_name.get(table) or {}).get("sql") or "")
                .upper()
                .split()
            )
            for marker in _SCHEMA_REQUIRED_SQL_MARKERS[table]:
                normalized_marker = " ".join(marker.upper().split())
                if normalized_marker not in sql:
                    problems.append(f"{table}:missing_contract={marker}")
        missing_triggers = sorted(
            _SCHEMA_REQUIRED_TRIGGERS - set(by_name)
        )
        if missing_triggers:
            problems.append(
                "missing_triggers=" + ",".join(missing_triggers)
            )
        if problems:
            raise PromotionPersistenceError(
                "unsafe v1.4.64 promotion schema: " + "; ".join(problems)
            )
        return await self.schema_fingerprint()

    @staticmethod
    def _normalize_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise TypeError("evidence must be a mapping")
        identity = _identity(payload, lease=False)
        observed_at_ms = _integer(payload, "observed_at_ms")
        terminal_at_ms = _integer(payload, "terminal_at_ms")
        if terminal_at_ms < observed_at_ms:
            raise ValueError("terminal_at_ms must be >= observed_at_ms")
        created_at_ms = _integer(
            payload,
            "created_at_ms",
            minimum=terminal_at_ms,
        )
        outcome = _required_text(payload, "outcome").lower()
        if outcome not in _OUTCOMES:
            raise ValueError(f"unsupported outcome: {outcome}")
        ambiguous = _flag(payload, "ambiguous")
        if outcome == "ambiguous_both" and not ambiguous:
            raise ValueError("ambiguous_both must set ambiguous=true")
        source_payload = payload.get("source_payload", payload.get("source_payload_json", {}))
        if isinstance(source_payload, str):
            try:
                source_payload = json.loads(source_payload)
            except json.JSONDecodeError as exc:
                raise ValueError("source_payload_json must contain JSON") from exc
        source_payload_json = _canonical_json(source_payload)
        evidence_schema_version = _required_text(
            payload, "evidence_schema_version"
        )
        if evidence_schema_version != V1464_EVIDENCE_SCHEMA_VERSION:
            raise ValueError(
                "unsupported evidence_schema_version: "
                f"{evidence_schema_version}"
            )
        source_type = _required_text(payload, "source_type").upper()
        if source_type not in _EVIDENCE_SOURCE_TYPES:
            raise ValueError(f"unsupported source_type: {source_type}")
        immutable = {
            "opportunity_id": _required_text(payload, "opportunity_id"),
            **identity,
            "evidence_schema_version": evidence_schema_version,
            "observed_at_ms": observed_at_ms,
            "terminal_at_ms": terminal_at_ms,
            "outcome": outcome,
            "data_complete": _flag(payload, "data_complete"),
            "ambiguous": ambiguous,
            "diagnostic_only": _flag(payload, "diagnostic_only"),
            "net_pnl_usdc": _finite_number(
                payload, "net_pnl_usdc", allow_none=True
            ),
            "source_type": source_type,
            "source_id": _required_text(payload, "source_id"),
            "source_payload_json": source_payload_json,
        }
        evidence_hash = hashlib.sha256(
            _canonical_json(immutable).encode("utf-8")
        ).hexdigest()
        supplied_hash = str(payload.get("evidence_hash") or "").strip()
        if supplied_hash and supplied_hash != evidence_hash:
            raise ValueError("evidence_hash does not match normalized evidence")
        return {
            **immutable,
            "evidence_hash": evidence_hash,
            "created_at_ms": created_at_ms,
        }

    async def upsert_evidence(self, payload: Mapping[str, Any]) -> bool:
        """Insert immutable evidence; exact retries are no-ops, conflicts raise."""

        evidence = self._normalize_evidence(payload)
        columns = tuple(evidence)
        connection = self._db.conn
        async with self._write_lock:
            began = False
            try:
                await connection.execute("BEGIN IMMEDIATE")
                began = True
                existing = await self._db.fetchone(
                    """SELECT * FROM v1464_promotion_evidence
                    WHERE opportunity_id = ?""",
                    (evidence["opportunity_id"],),
                )
                if existing is None:
                    source_existing = await self._db.fetchone(
                        """SELECT * FROM v1464_promotion_evidence
                        WHERE source_type = ? AND source_id = ?""",
                        (evidence["source_type"], evidence["source_id"]),
                    )
                    if source_existing is not None:
                        raise PromotionConflictError(
                            "source identity already belongs to another opportunity"
                        )
                    await connection.execute(
                        f"""INSERT INTO v1464_promotion_evidence
                        ({", ".join(columns)})
                        VALUES ({", ".join("?" for _ in columns)})""",
                        tuple(evidence[name] for name in columns),
                    )
                    await connection.commit()
                    began = False
                    return True

                comparable = {
                    name: existing.get(name)
                    for name in evidence
                    if name != "created_at_ms"
                }
                expected = {
                    name: value
                    for name, value in evidence.items()
                    if name != "created_at_ms"
                }
                if comparable != expected:
                    raise PromotionConflictError(
                        "conflicting identity or terminal evidence for opportunity"
                    )
                await connection.rollback()
                began = False
                return False
            except sqlite3.IntegrityError as exc:
                if began:
                    await connection.rollback()
                raise PromotionConflictError(str(exc)) from exc
            except Exception:
                if began:
                    await connection.rollback()
                raise

    async def list_sliding_evidence(
        self,
        cohort: Mapping[str, Any] | PromotionCohort,
        *,
        window_start_ms: int,
        as_of_ms: int,
        activation_cutoff_ms: int = 0,
        max_terminal_latency_ms: int | None = None,
        eligible_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Read one exact cohort using observation time, never terminal recency."""

        source = asdict(cohort) if isinstance(cohort, PromotionCohort) else cohort
        identity = _identity(source, lease=False)
        start = max(int(window_start_ms), int(activation_cutoff_ms), 0)
        end = int(as_of_ms)
        if end < start:
            raise ValueError("as_of_ms must be >= effective window start")
        predicates = [
            *(f"{name} = ?" for name in _EVIDENCE_IDENTITY_FIELDS),
            "observed_at_ms >= ?",
            "observed_at_ms <= ?",
            "terminal_at_ms <= ?",
        ]
        params: list[Any] = [
            *(identity[name] for name in _EVIDENCE_IDENTITY_FIELDS),
            start,
            end,
            end,
        ]
        if max_terminal_latency_ms is not None:
            latency = int(max_terminal_latency_ms)
            if latency < 0:
                raise ValueError("max_terminal_latency_ms must be non-negative")
            predicates.append("(terminal_at_ms - observed_at_ms) <= ?")
            params.append(latency)
        if eligible_only:
            predicates.extend(
                (
                    "data_complete = 1",
                    "ambiguous = 0",
                    "diagnostic_only = 0",
                )
            )
        rows = await self._db.fetchall(
            f"""SELECT * FROM v1464_promotion_evidence
            WHERE {" AND ".join(predicates)}
            ORDER BY observed_at_ms, terminal_at_ms, opportunity_id""",
            tuple(params),
        )
        return [_decode_row(row) or {} for row in rows]

    async def list_lane_paid_evidence(
        self,
        *,
        environment: str,
        symbol: str,
        lane_code: str,
        window_start_ms: int,
        as_of_ms: int,
        activation_cutoff_ms: int = 0,
    ) -> list[dict[str, Any]]:
        """Read authoritative paid rows across every exact cohort in one lane.

        The window is anchored to opportunity observation time, while terminal
        ordering is used so callers can calculate a real lane-wide consecutive
        loss streak.  State/profile/cohort predicates are intentionally absent.
        """

        scope = {
            "environment": _required_text(
                {"environment": environment}, "environment"
            ),
            "symbol": _required_text({"symbol": symbol}, "symbol"),
            "lane_code": _required_text(
                {"lane_code": lane_code}, "lane_code"
            ),
        }
        start = max(int(window_start_ms), int(activation_cutoff_ms), 0)
        end = int(as_of_ms)
        if end < start:
            raise ValueError("as_of_ms must be >= effective window start")
        rows = await self._db.fetchall(
            """SELECT * FROM v1464_promotion_evidence
            WHERE environment = ?
              AND symbol = ?
              AND lane_code = ?
              AND source_type = 'PAID'
              AND evidence_schema_version = ?
              AND observed_at_ms >= ?
              AND observed_at_ms <= ?
              AND terminal_at_ms <= ?
              AND data_complete = 1
              AND ambiguous = 0
              AND diagnostic_only = 0
              AND net_pnl_usdc IS NOT NULL
            ORDER BY terminal_at_ms, observed_at_ms, opportunity_id""",
            (
                scope["environment"],
                scope["symbol"],
                scope["lane_code"],
                V1464_EVIDENCE_SCHEMA_VERSION,
                start,
                end,
                end,
            ),
        )
        return [_decode_row(row) or {} for row in rows]

    @staticmethod
    def _normalize_lease(
        payload: Mapping[str, Any],
        *,
        generation: int,
        created_at_ms: int,
        updated_at_ms: int,
    ) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise TypeError("lease must be a mapping")
        identity = _identity(payload, lease=True)
        computed_key = promotion_cohort_key(identity)
        supplied_key = str(payload.get("cohort_key") or "").strip()
        if supplied_key and supplied_key != computed_key:
            raise ValueError("cohort_key does not match exact identity")
        phase = _required_text(payload, "phase").upper()
        status = _required_text(payload, "status").upper()
        if phase not in _LEASE_PHASES:
            raise ValueError(f"unsupported lease phase: {phase}")
        if status not in _LEASE_STATUSES:
            raise ValueError(f"unsupported lease status: {status}")
        cap = _finite_number(payload, "notional_cap_usdc")
        assert cap is not None
        if (
            cap <= 0
            or cap > 50.0
            or (phase == "PROBATION" and cap > 25.0)
        ):
            raise ValueError("invalid lease notional cap")
        window_start = _integer(payload, "evidence_window_start_ms")
        evidence_as_of = _integer(payload, "evidence_as_of_ms")
        if evidence_as_of < window_start:
            raise ValueError("evidence_as_of_ms precedes evidence_window_start_ms")
        issued = _integer(payload, "issued_at_ms")
        renewed = _integer(payload, "renewed_at_ms")
        expires = _integer(payload, "expires_at_ms")
        if renewed < issued or expires <= renewed:
            raise ValueError("invalid lease issue/renew/expiry ordering")
        demoted_raw = payload.get("demoted_at_ms")
        demoted_at = None if demoted_raw is None else int(demoted_raw)
        reason = (
            str(payload.get("demotion_reason")).strip()
            if payload.get("demotion_reason") is not None
            else None
        )
        cooldown_raw = payload.get("cooldown_until_ms")
        cooldown_until = (
            None if cooldown_raw is None else int(cooldown_raw)
        )
        if status == "ACTIVE":
            if demoted_at is not None or reason or cooldown_until is not None:
                raise ValueError(
                    "active lease cannot carry terminal guard state"
                )
        else:
            if demoted_at is None or demoted_at < 0 or not reason:
                raise ValueError(
                    "inactive lease requires demotion time and reason"
                )
            if status == "COOLDOWN":
                if cooldown_until is None or cooldown_until <= demoted_at:
                    raise ValueError(
                        "cooldown lease requires cooldown_until_ms "
                        "after demoted_at_ms"
                    )
            elif cooldown_until is not None:
                raise ValueError(
                    "only COOLDOWN may carry cooldown_until_ms"
                )
        snapshot = payload.get(
            "evidence_snapshot",
            payload.get("evidence_snapshot_json", {}),
        )
        if isinstance(snapshot, str):
            try:
                snapshot = json.loads(snapshot)
            except json.JSONDecodeError as exc:
                raise ValueError("evidence_snapshot_json must contain JSON") from exc
        snapshot_json = _canonical_json(snapshot)
        snapshot_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        supplied_snapshot_hash = str(
            payload.get("evidence_snapshot_hash") or ""
        ).strip()
        if supplied_snapshot_hash and supplied_snapshot_hash != snapshot_hash:
            raise ValueError(
                "evidence_snapshot_hash does not match evidence_snapshot"
            )
        return {
            "cohort_key": computed_key,
            "lease_id": _required_text(payload, "lease_id"),
            "generation": generation,
            **identity,
            "phase": phase,
            "status": status,
            "notional_cap_usdc": cap,
            "evidence_window_start_ms": window_start,
            "evidence_as_of_ms": evidence_as_of,
            "evidence_watermark": _integer(payload, "evidence_watermark"),
            "evidence_snapshot_hash": snapshot_hash,
            "evidence_snapshot_json": snapshot_json,
            "issued_at_ms": issued,
            "renewed_at_ms": renewed,
            "expires_at_ms": expires,
            "boot_id": _required_text(payload, "boot_id"),
            "owner_id": _required_text(payload, "owner_id"),
            "soft_failures": _integer(payload, "soft_failures"),
            "demotion_reason": reason,
            "demoted_at_ms": demoted_at,
            "cooldown_until_ms": cooldown_until,
            "created_at_ms": created_at_ms,
            "updated_at_ms": updated_at_ms,
        }

    async def get_lease(
        self, cohort: str | Mapping[str, Any] | PromotionCohort
    ) -> dict[str, Any] | None:
        key = (
            cohort
            if isinstance(cohort, str)
            else promotion_cohort_key(cohort)
        )
        return _decode_row(
            await self._db.fetchone(
                """SELECT * FROM v1464_lane_promotion_leases
                WHERE cohort_key = ?""",
                (key,),
            )
        )

    async def _existing_event(
        self, idempotency_key: str
    ) -> dict[str, Any] | None:
        return await self._db.fetchone(
            """SELECT * FROM v1464_lane_promotion_events
            WHERE idempotency_key = ?""",
            (idempotency_key,),
        )

    @staticmethod
    def _event_matches(
        existing: Mapping[str, Any],
        *,
        cohort_key: str,
        lease_id: str | None,
        generation_before: int | None,
        generation_after: int | None,
        event_time_ms: int,
        event_type: str,
        actor: str,
        payload_json: str,
    ) -> bool:
        return bool(
            existing.get("cohort_key") == cohort_key
            and existing.get("lease_id") == lease_id
            and existing.get("generation_before") == generation_before
            and existing.get("generation_after") == generation_after
            and int(existing.get("event_time_ms") or 0) == event_time_ms
            and existing.get("event_type") == event_type
            and existing.get("actor") == actor
            and existing.get("payload_json") == payload_json
        )

    async def _insert_event(
        self,
        *,
        idempotency_key: str,
        cohort_key: str,
        lease_id: str | None,
        generation_before: int | None,
        generation_after: int | None,
        event_time_ms: int,
        event_type: str,
        actor: str,
        payload_json: str,
    ) -> None:
        await self._db.conn.execute(
            """INSERT INTO v1464_lane_promotion_events (
                idempotency_key, cohort_key, lease_id,
                generation_before, generation_after,
                event_time_ms, event_type, actor, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                idempotency_key,
                cohort_key,
                lease_id,
                generation_before,
                generation_after,
                event_time_ms,
                event_type,
                actor,
                payload_json,
            ),
        )

    async def append_event(
        self,
        *,
        idempotency_key: str,
        cohort_key: str,
        event_time_ms: int,
        event_type: str,
        actor: str,
        payload: Mapping[str, Any],
        lease_id: str | None = None,
        generation_before: int | None = None,
        generation_after: int | None = None,
    ) -> bool:
        """Append one audit event; exact idempotent retries are no-ops."""

        key = str(idempotency_key or "").strip()
        cohort = str(cohort_key or "").strip()
        event = str(event_type or "").strip().upper()
        event_actor = str(actor or "").strip()
        when = int(event_time_ms)
        if not key or not cohort or not event_actor or when < 0:
            raise ValueError("event identity, actor, and time are required")
        if event not in _EVENT_TYPES:
            raise ValueError(f"unsupported event type: {event}")
        payload_json = _canonical_json(dict(payload))
        connection = self._db.conn
        async with self._write_lock:
            began = False
            try:
                await connection.execute("BEGIN IMMEDIATE")
                began = True
                existing = await self._existing_event(key)
                if existing is not None:
                    if not self._event_matches(
                        existing,
                        cohort_key=cohort,
                        lease_id=lease_id,
                        generation_before=generation_before,
                        generation_after=generation_after,
                        event_time_ms=when,
                        event_type=event,
                        actor=event_actor,
                        payload_json=payload_json,
                    ):
                        raise PromotionConflictError(
                            "idempotency key reused for a different event"
                        )
                    await connection.rollback()
                    began = False
                    return False
                await self._insert_event(
                    idempotency_key=key,
                    cohort_key=cohort,
                    lease_id=lease_id,
                    generation_before=generation_before,
                    generation_after=generation_after,
                    event_time_ms=when,
                    event_type=event,
                    actor=event_actor,
                    payload_json=payload_json,
                )
                await connection.commit()
                began = False
                return True
            except sqlite3.IntegrityError as exc:
                if began:
                    await connection.rollback()
                raise PromotionConflictError(str(exc)) from exc
            except Exception:
                if began:
                    await connection.rollback()
                raise

    async def upsert_lease(
        self,
        lease: Mapping[str, Any],
        *,
        expected_generation: int | None,
        event_type: str,
        event_time_ms: int,
        idempotency_key: str,
        actor: str,
        event_payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or CAS-update a lease and append its event in one transaction."""

        event = str(event_type or "").strip().upper()
        if event not in _EVENT_TYPES:
            raise ValueError(f"unsupported event type: {event}")
        when = int(event_time_ms)
        if when < 0:
            raise ValueError("event_time_ms must be non-negative")
        key = str(idempotency_key or "").strip()
        event_actor = str(actor or "").strip()
        if not key or not event_actor:
            raise ValueError("idempotency_key and actor are required")

        source_identity = _identity(lease, lease=True)
        cohort_key = promotion_cohort_key(source_identity)
        requested_event_json = _canonical_json(
            {
                "expected_generation": expected_generation,
                "lease": dict(lease),
                "details": dict(event_payload or {}),
            }
        )
        connection = self._db.conn
        async with self._write_lock:
            began = False
            try:
                await connection.execute("BEGIN IMMEDIATE")
                began = True
                prior_event = await self._existing_event(key)
                if prior_event is not None:
                    if not self._event_matches(
                        prior_event,
                        cohort_key=cohort_key,
                        lease_id=str(lease.get("lease_id") or "").strip() or None,
                        generation_before=(
                            None
                            if expected_generation in (None, 0)
                            else expected_generation
                        ),
                        generation_after=(
                            1
                            if expected_generation in (None, 0)
                            else expected_generation + 1
                        ),
                        event_time_ms=when,
                        event_type=event,
                        actor=event_actor,
                        payload_json=requested_event_json,
                    ):
                        raise PromotionConflictError(
                            "idempotency key reused for a different lease mutation"
                        )
                    current = await self._db.fetchone(
                        """SELECT * FROM v1464_lane_promotion_leases
                        WHERE cohort_key = ?""",
                        (cohort_key,),
                    )
                    await connection.rollback()
                    began = False
                    if current is None:
                        raise PromotionPersistenceError(
                            "idempotent lease event has no current lease"
                        )
                    return _decode_row(current) or {}

                current = await self._db.fetchone(
                    """SELECT * FROM v1464_lane_promotion_leases
                    WHERE cohort_key = ?""",
                    (cohort_key,),
                )
                if current is None:
                    if expected_generation not in (None, 0):
                        raise LeaseConflictError(
                            "lease does not exist at expected generation"
                        )
                    generation_before = None
                    generation_after = 1
                    created_at_ms = when
                else:
                    actual_generation = int(current["generation"])
                    if expected_generation != actual_generation:
                        raise LeaseConflictError(
                            f"lease generation mismatch: expected "
                            f"{expected_generation}, actual {actual_generation}"
                        )
                    generation_before = actual_generation
                    generation_after = actual_generation + 1
                    created_at_ms = int(current["created_at_ms"])

                normalized = self._normalize_lease(
                    lease,
                    generation=generation_after,
                    created_at_ms=created_at_ms,
                    updated_at_ms=when,
                )
                columns = tuple(normalized)
                if current is None:
                    await connection.execute(
                        f"""INSERT INTO v1464_lane_promotion_leases
                        ({", ".join(columns)})
                        VALUES ({", ".join("?" for _ in columns)})""",
                        tuple(normalized[name] for name in columns),
                    )
                else:
                    assignments = ", ".join(
                        f"{name} = ?" for name in columns if name != "cohort_key"
                    )
                    cursor = await connection.execute(
                        f"""UPDATE v1464_lane_promotion_leases
                        SET {assignments}
                        WHERE cohort_key = ? AND generation = ?""",
                        (
                            *(
                                normalized[name]
                                for name in columns
                                if name != "cohort_key"
                            ),
                            cohort_key,
                            expected_generation,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise LeaseConflictError("lease CAS update lost")

                await self._insert_event(
                    idempotency_key=key,
                    cohort_key=cohort_key,
                    lease_id=normalized["lease_id"],
                    generation_before=generation_before,
                    generation_after=generation_after,
                    event_time_ms=when,
                    event_type=event,
                    actor=event_actor,
                    payload_json=requested_event_json,
                )
                await connection.commit()
                began = False
                return _decode_row(normalized) or {}
            except sqlite3.IntegrityError as exc:
                if began:
                    await connection.rollback()
                raise PromotionConflictError(str(exc)) from exc
            except Exception:
                if began:
                    await connection.rollback()
                raise

    async def claim_admission(
        self,
        cohort_key: str,
        *,
        lease_id: str,
        expected_generation: int,
        current_identity: Mapping[str, Any] | PromotionCohort,
        now_ms: int,
        actual_notional_usdc: float,
        idempotency_key: str,
        actor: str,
    ) -> dict[str, Any]:
        """Atomically consume one active lease generation before order submit.

        A successful first call increments ``generation`` and appends exactly
        one ``ADMISSION_CONSUMED`` event in the same transaction.  An exact
        idempotent replay returns the current row with ``claim_granted=False``;
        callers must never submit a second order for such a replay.
        """

        supplied_cohort_key = str(cohort_key or "").strip()
        claimed_lease_id = str(lease_id or "").strip()
        event_key = str(idempotency_key or "").strip()
        event_actor = str(actor or "").strip()
        try:
            generation_before = int(expected_generation)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("expected_generation must be a positive integer") from exc
        if (
            isinstance(expected_generation, bool)
            or generation_before < 1
            or not supplied_cohort_key
            or not claimed_lease_id
            or not event_key
            or not event_actor
        ):
            raise ValueError(
                "cohort_key, lease_id, positive generation, "
                "idempotency_key, and actor are required"
            )
        when = int(now_ms)
        if isinstance(now_ms, bool) or when < 0:
            raise ValueError("now_ms must be a non-negative integer")
        amount = _finite_number(
            {"actual_notional_usdc": actual_notional_usdc},
            "actual_notional_usdc",
        )
        assert amount is not None
        if amount <= 0:
            raise ValueError("actual_notional_usdc must be positive")
        identity_source = (
            asdict(current_identity)
            if isinstance(current_identity, PromotionCohort)
            else current_identity
        )
        identity = _identity(identity_source, lease=True)
        computed_cohort_key = promotion_cohort_key(identity)
        if supplied_cohort_key != computed_cohort_key:
            raise AdmissionClaimError(
                "current exact identity or policy does not match cohort_key"
            )
        generation_after = generation_before + 1
        request_payload = {
            "cohort_key": supplied_cohort_key,
            "lease_id": claimed_lease_id,
            "expected_generation": generation_before,
            "generation_after": generation_after,
            "current_identity": identity,
            "actual_notional_usdc": amount,
            "claimed_at_ms": when,
        }
        payload_json = _canonical_json(request_payload)
        connection = self._db.conn
        async with self._write_lock:
            began = False
            try:
                await connection.execute("BEGIN IMMEDIATE")
                began = True
                prior_event = await self._existing_event(event_key)
                if prior_event is not None:
                    if not self._event_matches(
                        prior_event,
                        cohort_key=supplied_cohort_key,
                        lease_id=claimed_lease_id,
                        generation_before=generation_before,
                        generation_after=generation_after,
                        event_time_ms=when,
                        event_type="ADMISSION_CONSUMED",
                        actor=event_actor,
                        payload_json=payload_json,
                    ):
                        raise PromotionConflictError(
                            "idempotency key reused for a different admission claim"
                        )
                    current = await self._db.fetchone(
                        """SELECT * FROM v1464_lane_promotion_leases
                        WHERE cohort_key = ?""",
                        (supplied_cohort_key,),
                    )
                    await connection.rollback()
                    began = False
                    if current is None:
                        raise PromotionPersistenceError(
                            "idempotent admission event has no current lease"
                        )
                    replay = _decode_row(current) or {}
                    replay.update(
                        {
                            "claim_granted": False,
                            "claim_replayed": True,
                            "claim_generation": generation_after,
                            "claim_idempotency_key": event_key,
                            "claimed_notional_usdc": amount,
                        }
                    )
                    return replay

                current = await self._db.fetchone(
                    """SELECT * FROM v1464_lane_promotion_leases
                    WHERE cohort_key = ?""",
                    (supplied_cohort_key,),
                )
                if current is None:
                    raise AdmissionClaimError("lease_missing")
                if str(current.get("lease_id") or "") != claimed_lease_id:
                    raise AdmissionClaimError("lease_id_changed")
                if int(current.get("generation") or 0) != generation_before:
                    raise AdmissionClaimError("lease_generation_changed")
                if str(current.get("status") or "").upper() != "ACTIVE":
                    raise AdmissionClaimError("lease_not_active")
                if int(current.get("expires_at_ms") or 0) <= when:
                    raise AdmissionClaimError("lease_expired")
                if float(current.get("notional_cap_usdc") or 0.0) < amount:
                    raise AdmissionClaimError("actual_notional_exceeds_lease_cap")
                for name in _LEASE_IDENTITY_FIELDS:
                    if str(current.get(name) or "") != identity[name]:
                        raise AdmissionClaimError(
                            f"current_identity_changed:{name}"
                        )

                identity_predicates = " AND ".join(
                    f"{name} = ?" for name in _LEASE_IDENTITY_FIELDS
                )
                cursor = await connection.execute(
                    f"""UPDATE v1464_lane_promotion_leases
                    SET generation = ?, updated_at_ms = ?
                    WHERE cohort_key = ?
                      AND lease_id = ?
                      AND generation = ?
                      AND status = 'ACTIVE'
                      AND expires_at_ms > ?
                      AND notional_cap_usdc >= ?
                      AND {identity_predicates}""",
                    (
                        generation_after,
                        when,
                        supplied_cohort_key,
                        claimed_lease_id,
                        generation_before,
                        when,
                        amount,
                        *(identity[name] for name in _LEASE_IDENTITY_FIELDS),
                    ),
                )
                if cursor.rowcount != 1:
                    raise AdmissionClaimError("admission_claim_cas_lost")
                await self._insert_event(
                    idempotency_key=event_key,
                    cohort_key=supplied_cohort_key,
                    lease_id=claimed_lease_id,
                    generation_before=generation_before,
                    generation_after=generation_after,
                    event_time_ms=when,
                    event_type="ADMISSION_CONSUMED",
                    actor=event_actor,
                    payload_json=payload_json,
                )
                claimed = await self._db.fetchone(
                    """SELECT * FROM v1464_lane_promotion_leases
                    WHERE cohort_key = ?""",
                    (supplied_cohort_key,),
                )
                if claimed is None:
                    raise PromotionPersistenceError(
                        "claimed lease disappeared before commit"
                    )
                await connection.commit()
                began = False
                result = _decode_row(claimed) or {}
                result.update(
                    {
                        "claim_granted": True,
                        "claim_replayed": False,
                        "claim_generation": generation_after,
                        "claim_idempotency_key": event_key,
                        "claimed_notional_usdc": amount,
                    }
                )
                return result
            except sqlite3.IntegrityError as exc:
                if began:
                    await connection.rollback()
                raise PromotionConflictError(str(exc)) from exc
            except Exception:
                if began:
                    await connection.rollback()
                raise

    async def _close_lease(
        self,
        cohort_key: str,
        *,
        expected_generation: int,
        status: str,
        reason: str,
        event_type: str,
        event_time_ms: int,
        idempotency_key: str,
        actor: str,
        require_expired: bool,
        cooldown_until_ms: int | None = None,
    ) -> dict[str, Any] | None:
        current = await self.get_lease(cohort_key)
        if current is None:
            return None
        if current["status"] != "ACTIVE":
            return current
        if require_expired and int(current["expires_at_ms"]) > int(event_time_ms):
            return current
        lease = {
            name: current[name]
            for name in _LEASE_IDENTITY_FIELDS
        }
        lease.update(
            {
                "lease_id": current["lease_id"],
                "phase": current["phase"],
                "status": status,
                "notional_cap_usdc": current["notional_cap_usdc"],
                "evidence_window_start_ms": current["evidence_window_start_ms"],
                "evidence_as_of_ms": current["evidence_as_of_ms"],
                "evidence_watermark": current["evidence_watermark"],
                "evidence_snapshot": current.get("evidence_snapshot") or {},
                "issued_at_ms": current["issued_at_ms"],
                "renewed_at_ms": current["renewed_at_ms"],
                "expires_at_ms": current["expires_at_ms"],
                "boot_id": current["boot_id"],
                "owner_id": current["owner_id"],
                "soft_failures": current["soft_failures"],
                "demotion_reason": str(reason or "").strip() or status.lower(),
                "demoted_at_ms": int(event_time_ms),
                "cooldown_until_ms": cooldown_until_ms,
            }
        )
        return await self.upsert_lease(
            lease,
            expected_generation=expected_generation,
            event_type=event_type,
            event_time_ms=event_time_ms,
            idempotency_key=idempotency_key,
            actor=actor,
            event_payload={"reason": lease["demotion_reason"]},
        )

    async def demote_lease(
        self,
        cohort_key: str,
        *,
        expected_generation: int,
        reason: str,
        event_time_ms: int,
        idempotency_key: str,
        actor: str,
    ) -> dict[str, Any] | None:
        return await self._close_lease(
            cohort_key,
            expected_generation=expected_generation,
            status="DEMOTED",
            reason=reason,
            event_type="DEMOTED",
            event_time_ms=event_time_ms,
            idempotency_key=idempotency_key,
            actor=actor,
            require_expired=False,
            cooldown_until_ms=None,
        )

    async def revoke_lease(
        self,
        cohort_key: str,
        *,
        expected_generation: int,
        reason: str,
        event_time_ms: int,
        idempotency_key: str,
        actor: str,
    ) -> dict[str, Any] | None:
        return await self._close_lease(
            cohort_key,
            expected_generation=expected_generation,
            status="REVOKED",
            reason=reason,
            event_type="REVOKED",
            event_time_ms=event_time_ms,
            idempotency_key=idempotency_key,
            actor=actor,
            require_expired=False,
            cooldown_until_ms=None,
        )

    async def expire_lease(
        self,
        cohort_key: str,
        *,
        expected_generation: int,
        now_ms: int,
        idempotency_key: str,
        actor: str,
    ) -> dict[str, Any] | None:
        return await self._close_lease(
            cohort_key,
            expected_generation=expected_generation,
            status="EXPIRED",
            reason="lease_expired",
            event_type="EXPIRED",
            event_time_ms=now_ms,
            idempotency_key=idempotency_key,
            actor=actor,
            require_expired=True,
            cooldown_until_ms=None,
        )

    async def cooldown_lease(
        self,
        cohort_key: str,
        *,
        expected_generation: int,
        reason: str,
        event_time_ms: int,
        cooldown_until_ms: int,
        idempotency_key: str,
        actor: str,
    ) -> dict[str, Any] | None:
        """CAS-close an active lease into a durable timed cooldown."""

        return await self._close_lease(
            cohort_key,
            expected_generation=expected_generation,
            status="COOLDOWN",
            reason=reason,
            event_type="COOLDOWN",
            event_time_ms=event_time_ms,
            idempotency_key=idempotency_key,
            actor=actor,
            require_expired=False,
            cooldown_until_ms=int(cooldown_until_ms),
        )

    async def halt_lease(
        self,
        cohort_key: str,
        *,
        expected_generation: int,
        reason: str,
        event_time_ms: int,
        idempotency_key: str,
        actor: str,
    ) -> dict[str, Any] | None:
        """CAS-close an active lease into a durable sticky halt."""

        return await self._close_lease(
            cohort_key,
            expected_generation=expected_generation,
            status="HALTED",
            reason=reason,
            event_type="HALTED",
            event_time_ms=event_time_ms,
            idempotency_key=idempotency_key,
            actor=actor,
            require_expired=False,
            cooldown_until_ms=None,
        )

    async def upsert_guard_state(
        self,
        lease: Mapping[str, Any],
        *,
        expected_generation: int | None,
        status: str,
        reason: str,
        event_time_ms: int,
        idempotency_key: str,
        actor: str,
        cooldown_until_ms: int | None = None,
    ) -> dict[str, Any]:
        """Persist COOLDOWN/HALTED even when the cohort has no active lease."""

        guard_status = str(status or "").strip().upper()
        if guard_status not in {"COOLDOWN", "HALTED"}:
            raise ValueError("guard status must be COOLDOWN or HALTED")
        guarded = dict(lease)
        guarded.update(
            {
                "status": guard_status,
                "demotion_reason": str(reason or "").strip(),
                "demoted_at_ms": int(event_time_ms),
                "cooldown_until_ms": (
                    int(cooldown_until_ms)
                    if cooldown_until_ms is not None
                    else None
                ),
            }
        )
        return await self.upsert_lease(
            guarded,
            expected_generation=expected_generation,
            event_type=guard_status,
            event_time_ms=event_time_ms,
            idempotency_key=idempotency_key,
            actor=actor,
            event_payload={
                "reason": guarded["demotion_reason"],
                "cooldown_until_ms": guarded["cooldown_until_ms"],
            },
        )

    async def list_events(
        self, *, cohort_key: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        bounded = int(limit)
        if not 1 <= bounded <= 10_000:
            raise ValueError("limit must be in [1, 10000]")
        rows = await self._db.fetchall(
            """SELECT * FROM v1464_lane_promotion_events
            WHERE cohort_key = ?
            ORDER BY event_time_ms, id
            LIMIT ?""",
            (cohort_key, bounded),
        )
        return [_decode_row(row) or {} for row in rows]


__all__ = [
    "AdmissionClaimError",
    "ENGINE_STATE_BY_STORAGE_PHASE",
    "LeaseConflictError",
    "PromotionCohort",
    "PromotionConflictError",
    "PromotionPersistenceError",
    "V1464_EVIDENCE_SCHEMA_VERSION",
    "V1464_PROFILE_IDENTITY_SCHEMA",
    "V1464PromotionRepository",
    "lease_row_to_engine_snapshot",
    "promotion_cohort_key",
]
