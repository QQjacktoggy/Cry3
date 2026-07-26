"""Compact, fail-closed persistence for v1.4.69 Adaptive Arm observations.

This repository is deliberately passive.  It stores opportunities, candidate
matches, shadow/paid observations, and audit metadata; it exposes no exchange
client and no order-placement or cancellation API.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from math import isfinite
import sqlite3
from typing import Any, Mapping, Sequence

from src.gridbot.storage.database import Database


_REGIMES = frozenset(
    {
        "TREND_UP",
        "TREND_DOWN",
        "TREND",
        "RANGE",
        "SHOCK",
        "UNCERTAIN",
        "UNKNOWN",
    }
)
_MATCH_STATUSES = frozenset({"MATCH", "NEAR_MATCH", "NO_MATCH"})
_SAFETY_STATUSES = frozenset(
    {"SAFE", "HARD_BLOCK", "DATA_BLOCKED", "NOT_EVALUATED"}
)
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
        "data_incomplete",
        "dropped",
    }
)
_FILL_STATUSES = frozenset({"FILLED", "NO_FILL", "UNKNOWN"})
_EVENT_TYPES = frozenset(
    {
        "OBSERVED",
        "EVIDENCE_STARTED",
        "EVIDENCE_TERMINAL",
        "EVALUATED",
        "PROBATION_GRANTED",
        "LIVE_GRANTED",
        "LEASE_RENEWED",
        "LEASE_REVOKED",
        "COOLDOWN",
        "DEMOTED",
        "EXPIRED",
        "HALTED",
    }
)
_SOURCE_TYPES = frozenset({"SHADOW", "PAID"})
_QUALITY = frozenset({"COMPLETE", "DATA_INCOMPLETE"})
_MAX_FEATURE_JSON_BYTES = 32_768
_MAX_COMPACT_JSON_BYTES = 4_096
_MAX_CANDIDATES_PER_OBSERVATION = 256
_MAX_EVIDENCE_PER_BUNDLE = 1_024
_SCHEMA_TABLES: dict[str, frozenset[str]] = {
    "v1469_market_opportunities": frozenset({
        "opportunity_id",
        "environment",
        "symbol",
        "observed_at_ms",
        "feature_at_ms",
        "coarse_regime",
        "feature_hash",
        "feature_snapshot_json",
        "data_quality",
    }),
    "v1469_lane_candidates": frozenset({
        "candidate_id",
        "opportunity_id",
        "lane_code",
        "effective_side",
        "match_status",
        "safety_status",
        "is_selected",
        "suppression_reason",
        "matcher_hash",
        "data_complete",
    }),
    "v1469_arm_evidence": frozenset({
        "evidence_id",
        "opportunity_id",
        "candidate_id",
        "arm_key",
        "execution_profile_hash",
        "status",
        "outcome",
        "reward_net_bp",
    }),
    "v1469_arm_leases": frozenset({
        "arm_key",
        "lease_id",
        "generation",
        "phase",
        "status",
        "notional_cap_usdc",
        "risk_policy_hash",
        "expires_at_ms",
    }),
    "v1469_arm_events": frozenset({
        "id",
        "idempotency_key",
        "arm_key",
        "event_time_ms",
        "event_type",
        "actor",
        "payload_json",
    }),
}
_SCHEMA_TRIGGERS = frozenset({
    "trg_v1469_arm_evidence_no_delete",
    "trg_v1469_arm_evidence_terminal_once",
    "trg_v1469_arm_evidence_identity_immutable",
    "trg_v1469_arm_events_no_update",
    "trg_v1469_arm_events_no_delete",
})


class ArmObservationPersistenceError(RuntimeError):
    """Base error for unsafe or unavailable v1.4.69 persistence."""


class ArmObservationConflictError(ArmObservationPersistenceError):
    """A durable opportunity or candidate identity was reused differently."""


class ArmEvidenceConflictError(ArmObservationPersistenceError):
    """An evidence identity or terminal result was reused differently."""


def _canonical_json(value: Any, *, max_bytes: int, field: str) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be valid JSON") from exc
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError(f"{field} exceeds {max_bytes} bytes")
    return encoded


def _text(payload: Mapping[str, Any], name: str) -> str:
    value = str(payload.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} must be non-empty")
    return value


def _optional_text(payload: Mapping[str, Any], name: str) -> str | None:
    value = payload.get(name)
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


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


def _optional_integer(
    payload: Mapping[str, Any],
    name: str,
    *,
    minimum: int = 0,
) -> int | None:
    if payload.get(name) is None:
        return None
    return _integer(payload, name, minimum=minimum)


def _flag(payload: Mapping[str, Any], name: str, *, default: bool = False) -> int:
    value = payload.get(name, default)
    if value not in (False, True, 0, 1):
        raise ValueError(f"{name} must be boolean")
    return int(bool(value))


def _finite(
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


def _enum(
    payload: Mapping[str, Any],
    name: str,
    values: frozenset[str],
    *,
    lower: bool = False,
) -> str:
    value = _text(payload, name)
    value = value.lower() if lower else value.upper()
    if value not in values:
        raise ValueError(f"unsupported {name}: {value}")
    return value


def _hash(prefix: str, payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return prefix + hashlib.sha256(encoded).hexdigest()


def candidate_identity(payload: Mapping[str, Any]) -> str:
    """Return the stable per-opportunity lane-candidate identity."""

    identity = {
        "opportunity_id": _text(payload, "opportunity_id"),
        "lane_code": _text(payload, "lane_code"),
        "effective_side": _enum(
            payload, "effective_side", frozenset({"LONG", "SHORT"})
        ),
        "strategy": _text(payload, "strategy"),
        "matcher_hash": _text(payload, "matcher_hash"),
    }
    return _hash("v1469c_", identity)


def arm_identity(payload: Mapping[str, Any]) -> str:
    """Return the stable arm key; cap and absolute prices are intentionally absent."""

    identity = {
        "lane_code": _text(payload, "lane_code"),
        "effective_side": _enum(
            payload, "effective_side", frozenset({"LONG", "SHORT"})
        ),
        "strategy": _text(payload, "strategy"),
        "coarse_regime": _enum(payload, "coarse_regime", _REGIMES),
        "execution_profile_id": _text(payload, "execution_profile_id"),
        "execution_profile_schema": _text(
            payload, "execution_profile_schema"
        ),
        "execution_profile_hash": _text(payload, "execution_profile_hash"),
    }
    return _hash("v1469a_", identity)


def evidence_identity(payload: Mapping[str, Any]) -> str:
    """Return the stable candidate/profile/source evidence identity."""

    return _hash(
        "v1469e_",
        {
            "opportunity_id": _text(payload, "opportunity_id"),
            "candidate_id": _text(payload, "candidate_id"),
            "execution_profile_hash": _text(
                payload, "execution_profile_hash"
            ),
            "source_type": _enum(payload, "source_type", _SOURCE_TYPES),
        },
    )


class V1469ArmObservationRepository:
    """Normalized, idempotent observation repository with bounded JSON."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._write_lock = asyncio.Lock()

    async def schema_fingerprint(self) -> str:
        objects = await self._db.fetchall(
            """SELECT type, name, sql FROM sqlite_master
            WHERE name LIKE 'v1469_%' OR name LIKE 'trg_v1469_%'
            ORDER BY type, name"""
        )
        columns = {
            table: await self._db.fetchall(f"PRAGMA table_info({table})")
            for table in sorted(_SCHEMA_TABLES)
        }
        payload = json.dumps(
            {"objects": objects, "columns": columns},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def assert_schema_ready(self) -> str:
        """Validate passive observation storage without granting authority."""

        objects = await self._db.fetchall(
            """SELECT type, name, sql FROM sqlite_master
            WHERE name LIKE 'v1469_%' OR name LIKE 'trg_v1469_%'"""
        )
        by_name = {str(row.get("name") or ""): row for row in objects}
        problems: list[str] = []
        for table, required in _SCHEMA_TABLES.items():
            actual = {
                str(row.get("name") or "")
                for row in await self._db.fetchall(
                    f"PRAGMA table_info({table})"
                )
            }
            missing = sorted(required - actual)
            if missing:
                problems.append(
                    f"{table}:missing_columns={','.join(missing)}"
                )
        absent_triggers = sorted(_SCHEMA_TRIGGERS - set(by_name))
        if absent_triggers:
            problems.append(
                "missing_triggers=" + ",".join(absent_triggers)
            )
        candidate_sql = " ".join(
            str(
                (by_name.get("v1469_lane_candidates") or {}).get("sql")
                or ""
            ).upper().split()
        )
        if "'NOT_EVALUATED'" not in candidate_sql:
            problems.append(
                "v1469_lane_candidates:missing_contract=NOT_EVALUATED"
            )
        if problems:
            raise ArmObservationPersistenceError(
                "unsafe v1.4.69 observation schema: "
                + "; ".join(problems)
            )
        return await self.schema_fingerprint()

    @staticmethod
    def _normalize_opportunity(payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise TypeError("opportunity must be a mapping")
        observed = _integer(payload, "observed_at_ms")
        feature_at = _integer(payload, "feature_at_ms")
        if feature_at > observed:
            raise ValueError("feature_at_ms must be <= observed_at_ms")
        snapshot = payload.get(
            "feature_snapshot", payload.get("feature_snapshot_json", {})
        )
        if isinstance(snapshot, str):
            try:
                snapshot = json.loads(snapshot)
            except json.JSONDecodeError as exc:
                raise ValueError("feature_snapshot_json must contain JSON") from exc
        snapshot_json = _canonical_json(
            snapshot,
            max_bytes=_MAX_FEATURE_JSON_BYTES,
            field="feature_snapshot",
        )
        calculated_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        supplied_hash = str(payload.get("feature_hash") or "").strip()
        if supplied_hash and supplied_hash != calculated_hash:
            raise ValueError("feature_hash does not match feature_snapshot")
        confidence = _finite(payload, "regime_confidence", allow_none=True)
        if confidence is not None and not 0 <= confidence <= 1:
            raise ValueError("regime_confidence must be in [0, 1]")
        quality = _enum(payload, "data_quality", _QUALITY)
        return {
            "opportunity_id": _text(payload, "opportunity_id"),
            "environment": _text(payload, "environment").upper(),
            "symbol": _text(payload, "symbol").upper(),
            "observed_at_ms": observed,
            "feature_at_ms": feature_at,
            "coarse_regime": _enum(payload, "coarse_regime", _REGIMES),
            "regime_confidence": confidence,
            "feature_schema": _text(payload, "feature_schema"),
            "feature_hash": calculated_hash,
            "feature_snapshot_json": snapshot_json,
            "source_run_id": _optional_text(payload, "source_run_id"),
            "source_event_id": _optional_text(payload, "source_event_id"),
            "data_quality": quality,
            "created_at_ms": _integer(
                payload, "created_at_ms", minimum=observed
            ),
        }

    async def insert_opportunity(self, payload: Mapping[str, Any]) -> bool:
        """Insert one immutable feature snapshot; exact retries are no-ops."""

        row = self._normalize_opportunity(payload)
        columns = tuple(row)
        async with self._write_lock:
            began = False
            try:
                await self._db.conn.execute("BEGIN IMMEDIATE")
                began = True
                existing = await self._db.fetchone(
                    """SELECT * FROM v1469_market_opportunities
                    WHERE opportunity_id = ?""",
                    (row["opportunity_id"],),
                )
                if existing is None and row["source_event_id"] is not None:
                    existing_source = await self._db.fetchone(
                        """SELECT opportunity_id
                        FROM v1469_market_opportunities
                        WHERE environment = ? AND symbol = ?
                          AND source_event_id = ?""",
                        (
                            row["environment"],
                            row["symbol"],
                            row["source_event_id"],
                        ),
                    )
                    if existing_source is not None:
                        raise ArmObservationConflictError(
                            "source event already belongs to another opportunity"
                        )
                if existing is None:
                    await self._db.conn.execute(
                        f"""INSERT INTO v1469_market_opportunities
                        ({", ".join(columns)})
                        VALUES ({", ".join("?" for _ in columns)})""",
                        tuple(row[name] for name in columns),
                    )
                    await self._db.conn.commit()
                    began = False
                    return True
                comparable = {
                    name: existing.get(name)
                    for name in row
                    if name != "created_at_ms"
                }
                expected = {
                    name: value for name, value in row.items()
                    if name != "created_at_ms"
                }
                if comparable != expected:
                    raise ArmObservationConflictError(
                        "conflicting durable opportunity"
                    )
                await self._db.conn.rollback()
                began = False
                return False
            except asyncio.CancelledError:
                if began:
                    await asyncio.shield(self._db.conn.rollback())
                raise
            except sqlite3.IntegrityError as exc:
                if began:
                    await self._db.conn.rollback()
                raise ArmObservationConflictError(str(exc)) from exc
            except Exception:
                if began:
                    await self._db.conn.rollback()
                raise

    @staticmethod
    def _normalize_candidate(payload: Mapping[str, Any]) -> dict[str, Any]:
        annotations = payload.get(
            "annotations", payload.get("annotations_json", {})
        )
        if isinstance(annotations, str):
            try:
                annotations = json.loads(annotations)
            except json.JSONDecodeError as exc:
                raise ValueError("annotations_json must contain JSON") from exc
        normalized: dict[str, Any] = {
            "opportunity_id": _text(payload, "opportunity_id"),
            "lane_code": _text(payload, "lane_code"),
            "effective_side": _enum(
                payload, "effective_side", frozenset({"LONG", "SHORT"})
            ),
            "strategy": _text(payload, "strategy"),
            "match_status": _enum(payload, "match_status", _MATCH_STATUSES),
            "safety_status": _enum(
                payload, "safety_status", _SAFETY_STATUSES
            ),
            "is_selected": _flag(payload, "is_selected"),
            "selection_rank": _optional_integer(payload, "selection_rank"),
            "suppression_reason": _optional_text(
                payload, "suppression_reason"
            ),
            "suppressed_by_lane_code": _optional_text(
                payload, "suppressed_by_lane_code"
            ),
            "matcher_version": _text(payload, "matcher_version"),
            "matcher_hash": _text(payload, "matcher_hash"),
            "data_complete": _flag(payload, "data_complete"),
            "annotations_json": _canonical_json(
                annotations,
                max_bytes=_MAX_COMPACT_JSON_BYTES,
                field="annotations",
            ),
            "created_at_ms": _integer(payload, "created_at_ms"),
        }
        if (
            normalized["is_selected"]
            and normalized["match_status"] != "MATCH"
        ):
            raise ValueError("selected candidate must be matched")
        expected_id = candidate_identity(normalized)
        supplied_id = str(payload.get("candidate_id") or "").strip()
        if supplied_id and supplied_id != expected_id:
            raise ValueError("candidate_id does not match candidate identity")
        return {"candidate_id": expected_id, **normalized}

    async def insert_candidate(self, payload: Mapping[str, Any]) -> bool:
        """Insert one lane assessment without changing any routing decision."""

        row = self._normalize_candidate(payload)
        columns = tuple(row)
        async with self._write_lock:
            began = False
            try:
                await self._db.conn.execute("BEGIN IMMEDIATE")
                began = True
                opportunity = await self._db.fetchone(
                    """SELECT opportunity_id
                    FROM v1469_market_opportunities
                    WHERE opportunity_id = ?""",
                    (row["opportunity_id"],),
                )
                if opportunity is None:
                    raise ArmObservationConflictError(
                        "candidate opportunity does not exist"
                    )
                existing = await self._db.fetchone(
                    """SELECT * FROM v1469_lane_candidates
                    WHERE candidate_id = ?""",
                    (row["candidate_id"],),
                )
                if existing is None:
                    await self._db.conn.execute(
                        f"""INSERT INTO v1469_lane_candidates
                        ({", ".join(columns)})
                        VALUES ({", ".join("?" for _ in columns)})""",
                        tuple(row[name] for name in columns),
                    )
                    await self._db.conn.commit()
                    began = False
                    return True
                comparable = {
                    name: existing.get(name)
                    for name in row
                    if name != "created_at_ms"
                }
                expected = {
                    name: value for name, value in row.items()
                    if name != "created_at_ms"
                }
                if comparable != expected:
                    raise ArmObservationConflictError(
                        "conflicting lane candidate"
                    )
                await self._db.conn.rollback()
                began = False
                return False
            except sqlite3.IntegrityError as exc:
                if began:
                    await self._db.conn.rollback()
                raise ArmObservationConflictError(str(exc)) from exc
            except Exception:
                if began:
                    await self._db.conn.rollback()
                raise

    async def insert_observation(
        self,
        opportunity: Mapping[str, Any],
        candidates: list[Mapping[str, Any]]
        | tuple[Mapping[str, Any], ...],
    ) -> dict[str, int | bool | str]:
        """Atomically insert one opportunity and its complete candidate fan-out.

        Exact rows already present are retained as no-ops.  Missing exact rows
        are repaired in the same transaction.  Any opportunity, source, or
        candidate conflict rolls back every insert performed by this call.
        The result contains only bounded counts, never snapshots or payloads.
        """

        opportunity_row = self._normalize_opportunity(opportunity)
        if not isinstance(candidates, (list, tuple)):
            raise TypeError("candidates must be a list or tuple")
        if len(candidates) > _MAX_CANDIDATES_PER_OBSERVATION:
            raise ValueError(
                "candidates exceeds "
                f"{_MAX_CANDIDATES_PER_OBSERVATION} rows"
            )
        normalized_by_id: dict[str, dict[str, Any]] = {}
        for payload in candidates:
            candidate = self._normalize_candidate(payload)
            if candidate["opportunity_id"] != opportunity_row["opportunity_id"]:
                raise ValueError(
                    "every candidate must reference the observation opportunity"
                )
            existing_batch = normalized_by_id.get(candidate["candidate_id"])
            if existing_batch is not None:
                comparable_existing = {
                    name: value
                    for name, value in existing_batch.items()
                    if name != "created_at_ms"
                }
                comparable_candidate = {
                    name: value
                    for name, value in candidate.items()
                    if name != "created_at_ms"
                }
                if comparable_existing != comparable_candidate:
                    raise ArmObservationConflictError(
                        "conflicting duplicate candidate in observation"
                    )
                continue
            normalized_by_id[candidate["candidate_id"]] = candidate
        candidate_rows = list(normalized_by_id.values())

        async with self._write_lock:
            began = False
            opportunity_inserted = False
            candidates_inserted = 0
            candidates_existing = 0
            try:
                await self._db.conn.execute("BEGIN IMMEDIATE")
                began = True
                existing_opportunity = await self._db.fetchone(
                    """SELECT * FROM v1469_market_opportunities
                    WHERE opportunity_id = ?""",
                    (opportunity_row["opportunity_id"],),
                )
                if (
                    existing_opportunity is None
                    and opportunity_row["source_event_id"] is not None
                ):
                    source_existing = await self._db.fetchone(
                        """SELECT opportunity_id
                        FROM v1469_market_opportunities
                        WHERE environment = ? AND symbol = ?
                          AND source_event_id = ?""",
                        (
                            opportunity_row["environment"],
                            opportunity_row["symbol"],
                            opportunity_row["source_event_id"],
                        ),
                    )
                    if source_existing is not None:
                        if str(
                            opportunity_row["source_event_id"]
                        ).startswith("v1469d_"):
                            # A v1.4.69 source event is the durable bucket
                            # identity, not an external exact event ID.  The
                            # first snapshot wins; later scheduler/restart
                            # retries must not become another opportunity.
                            await self._db.conn.rollback()
                            began = False
                            return {
                                "opportunity_inserted": False,
                                "opportunity_existing": True,
                                "source_replay": True,
                                "durable_opportunity_id": str(
                                    source_existing["opportunity_id"]
                                ),
                                "candidates_inserted": 0,
                                "candidates_existing": 0,
                                "candidate_count": 0,
                            }
                        raise ArmObservationConflictError(
                            "source event already belongs to another opportunity"
                        )
                if existing_opportunity is None:
                    columns = tuple(opportunity_row)
                    await self._db.conn.execute(
                        f"""INSERT INTO v1469_market_opportunities
                        ({", ".join(columns)})
                        VALUES ({", ".join("?" for _ in columns)})""",
                        tuple(opportunity_row[name] for name in columns),
                    )
                    opportunity_inserted = True
                else:
                    existing_comparable = {
                        name: existing_opportunity.get(name)
                        for name in opportunity_row
                        if name != "created_at_ms"
                    }
                    expected_comparable = {
                        name: value
                        for name, value in opportunity_row.items()
                        if name != "created_at_ms"
                    }
                    if existing_comparable != expected_comparable:
                        raise ArmObservationConflictError(
                            "conflicting durable opportunity"
                        )

                for candidate in candidate_rows:
                    existing_candidate = await self._db.fetchone(
                        """SELECT * FROM v1469_lane_candidates
                        WHERE candidate_id = ?""",
                        (candidate["candidate_id"],),
                    )
                    if existing_candidate is None:
                        columns = tuple(candidate)
                        await self._db.conn.execute(
                            f"""INSERT INTO v1469_lane_candidates
                            ({", ".join(columns)})
                            VALUES ({", ".join("?" for _ in columns)})""",
                            tuple(candidate[name] for name in columns),
                        )
                        candidates_inserted += 1
                        continue
                    existing_comparable = {
                        name: existing_candidate.get(name)
                        for name in candidate
                        if name != "created_at_ms"
                    }
                    expected_comparable = {
                        name: value
                        for name, value in candidate.items()
                        if name != "created_at_ms"
                    }
                    if existing_comparable != expected_comparable:
                        raise ArmObservationConflictError(
                            "conflicting lane candidate"
                        )
                    candidates_existing += 1

                await self._db.conn.commit()
                began = False
                return {
                    "opportunity_inserted": opportunity_inserted,
                    "opportunity_existing": not opportunity_inserted,
                    "candidates_inserted": candidates_inserted,
                    "candidates_existing": candidates_existing,
                    "candidate_count": len(candidate_rows),
                }
            except asyncio.CancelledError:
                if began:
                    await asyncio.shield(self._db.conn.rollback())
                raise
            except sqlite3.IntegrityError as exc:
                if began:
                    await self._db.conn.rollback()
                raise ArmObservationConflictError(str(exc)) from exc
            except Exception:
                if began:
                    await self._db.conn.rollback()
                raise

    async def append_evidence(self, payload: Mapping[str, Any]) -> bool:
        """Append one pending arm evaluation; exact retries are no-ops."""

        opportunity_id = _text(payload, "opportunity_id")
        candidate_id = _text(payload, "candidate_id")
        profile = {
            "execution_profile_id": _text(payload, "execution_profile_id"),
            "execution_profile_schema": _text(
                payload, "execution_profile_schema"
            ),
            "execution_profile_hash": _text(
                payload, "execution_profile_hash"
            ),
        }
        source_type = _enum(payload, "source_type", _SOURCE_TYPES)
        observed = _integer(payload, "observed_at_ms")
        created = _integer(payload, "created_at_ms", minimum=observed)
        diagnostic = _flag(payload, "diagnostic_only")
        async with self._write_lock:
            began = False
            try:
                await self._db.conn.execute("BEGIN IMMEDIATE")
                began = True
                identity = await self._db.fetchone(
                    """SELECT c.opportunity_id, c.lane_code, c.effective_side,
                              c.strategy, o.coarse_regime, o.observed_at_ms
                    FROM v1469_lane_candidates c
                    JOIN v1469_market_opportunities o
                      ON o.opportunity_id = c.opportunity_id
                    WHERE c.candidate_id = ?""",
                    (candidate_id,),
                )
                if identity is None or identity["opportunity_id"] != opportunity_id:
                    raise ArmEvidenceConflictError(
                        "candidate does not belong to opportunity"
                    )
                if int(identity["observed_at_ms"]) != observed:
                    raise ArmEvidenceConflictError(
                        "evidence must use the opportunity observation time"
                    )
                arm_key = arm_identity({**identity, **profile})
                evidence_id = evidence_identity(
                    {
                        "opportunity_id": opportunity_id,
                        "candidate_id": candidate_id,
                        "execution_profile_hash": profile[
                            "execution_profile_hash"
                        ],
                        "source_type": source_type,
                    }
                )
                supplied_id = str(payload.get("evidence_id") or "").strip()
                if supplied_id and supplied_id != evidence_id:
                    raise ValueError(
                        "evidence_id does not match evidence identity"
                    )
                row = {
                    "evidence_id": evidence_id,
                    "opportunity_id": opportunity_id,
                    "candidate_id": candidate_id,
                    "arm_key": arm_key,
                    **profile,
                    "source_type": source_type,
                    "diagnostic_only": diagnostic,
                    "observed_at_ms": observed,
                    "status": "PENDING",
                    "terminal_at_ms": None,
                    "outcome": None,
                    "fill_status": None,
                    "data_complete": 0,
                    "ambiguous": 0,
                    "reward_net_bp": None,
                    "mfe_bp": None,
                    "mae_bp": None,
                    "terminal_reason": None,
                    "terminal_payload_json": None,
                    "evidence_hash": None,
                    "created_at_ms": created,
                    "updated_at_ms": created,
                }
                existing = await self._db.fetchone(
                    """SELECT * FROM v1469_arm_evidence
                    WHERE evidence_id = ?""",
                    (evidence_id,),
                )
                if existing is None:
                    columns = tuple(row)
                    await self._db.conn.execute(
                        f"""INSERT INTO v1469_arm_evidence
                        ({", ".join(columns)})
                        VALUES ({", ".join("?" for _ in columns)})""",
                        tuple(row[name] for name in columns),
                    )
                    await self._db.conn.commit()
                    began = False
                    return True
                comparable = {
                    name: existing.get(name)
                    for name in row
                    if name not in {"created_at_ms", "updated_at_ms"}
                }
                expected = {
                    name: value for name, value in row.items()
                    if name not in {"created_at_ms", "updated_at_ms"}
                }
                if comparable != expected:
                    raise ArmEvidenceConflictError(
                        "conflicting or already-terminal arm evidence"
                    )
                await self._db.conn.rollback()
                began = False
                return False
            except sqlite3.IntegrityError as exc:
                if began:
                    await self._db.conn.rollback()
                raise ArmEvidenceConflictError(str(exc)) from exc
            except Exception:
                if began:
                    await self._db.conn.rollback()
                raise

    async def append_evidence_bundle(
        self,
        payloads: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Atomically start every profile in one paired opportunity bundle.

        A crash must not leave only the first profile of a paired comparison
        durable.  Exact retries are no-ops; any conflicting or terminal member
        rolls the entire bundle back.
        """

        if isinstance(payloads, (str, bytes)) or not isinstance(
            payloads, Sequence
        ):
            raise TypeError("payloads must be a sequence of mappings")
        if not payloads:
            raise ValueError("payloads must not be empty")
        if len(payloads) > _MAX_EVIDENCE_PER_BUNDLE:
            raise ValueError(
                "payloads exceeds "
                f"{_MAX_EVIDENCE_PER_BUNDLE} evidence rows"
            )

        normalized: list[dict[str, Any]] = []
        for payload in payloads:
            if not isinstance(payload, Mapping):
                raise TypeError("every evidence payload must be a mapping")
            observed = _integer(payload, "observed_at_ms")
            profile = {
                "execution_profile_id": _text(
                    payload, "execution_profile_id"
                ),
                "execution_profile_schema": _text(
                    payload, "execution_profile_schema"
                ),
                "execution_profile_hash": _text(
                    payload, "execution_profile_hash"
                ),
            }
            identity_payload = {
                "opportunity_id": _text(payload, "opportunity_id"),
                "candidate_id": _text(payload, "candidate_id"),
                "execution_profile_hash": profile[
                    "execution_profile_hash"
                ],
                "source_type": _enum(
                    payload, "source_type", _SOURCE_TYPES
                ),
            }
            evidence_id = evidence_identity(identity_payload)
            supplied_id = str(payload.get("evidence_id") or "").strip()
            if supplied_id and supplied_id != evidence_id:
                raise ValueError(
                    "evidence_id does not match evidence identity"
                )
            normalized.append(
                {
                    **identity_payload,
                    **profile,
                    "evidence_id": evidence_id,
                    "observed_at_ms": observed,
                    "created_at_ms": _integer(
                        payload, "created_at_ms", minimum=observed
                    ),
                    "diagnostic_only": _flag(
                        payload, "diagnostic_only"
                    ),
                }
            )

        async with self._write_lock:
            began = False
            inserted = 0
            existing_count = 0
            durable_rows: list[dict[str, Any]] = []
            seen: dict[str, dict[str, Any]] = {}
            try:
                await self._db.conn.execute("BEGIN IMMEDIATE")
                began = True
                for item in normalized:
                    candidate = await self._db.fetchone(
                        """SELECT c.opportunity_id, c.lane_code,
                                  c.effective_side, c.strategy,
                                  o.coarse_regime, o.observed_at_ms
                        FROM v1469_lane_candidates c
                        JOIN v1469_market_opportunities o
                          ON o.opportunity_id = c.opportunity_id
                        WHERE c.candidate_id = ?""",
                        (item["candidate_id"],),
                    )
                    if (
                        candidate is None
                        or candidate["opportunity_id"]
                        != item["opportunity_id"]
                    ):
                        raise ArmEvidenceConflictError(
                            "candidate does not belong to opportunity"
                        )
                    if int(candidate["observed_at_ms"]) != int(
                        item["observed_at_ms"]
                    ):
                        raise ArmEvidenceConflictError(
                            "evidence must use the opportunity observation time"
                        )
                    arm_key = arm_identity({**candidate, **item})
                    row = {
                        "evidence_id": item["evidence_id"],
                        "opportunity_id": item["opportunity_id"],
                        "candidate_id": item["candidate_id"],
                        "arm_key": arm_key,
                        "execution_profile_id": item[
                            "execution_profile_id"
                        ],
                        "execution_profile_schema": item[
                            "execution_profile_schema"
                        ],
                        "execution_profile_hash": item[
                            "execution_profile_hash"
                        ],
                        "source_type": item["source_type"],
                        "diagnostic_only": item["diagnostic_only"],
                        "observed_at_ms": item["observed_at_ms"],
                        "status": "PENDING",
                        "terminal_at_ms": None,
                        "outcome": None,
                        "fill_status": None,
                        "data_complete": 0,
                        "ambiguous": 0,
                        "reward_net_bp": None,
                        "mfe_bp": None,
                        "mae_bp": None,
                        "terminal_reason": None,
                        "terminal_payload_json": None,
                        "evidence_hash": None,
                        "created_at_ms": item["created_at_ms"],
                        "updated_at_ms": item["created_at_ms"],
                    }
                    duplicate = seen.get(row["evidence_id"])
                    if duplicate is not None:
                        comparable_duplicate = {
                            name: value
                            for name, value in duplicate.items()
                            if name not in {"created_at_ms", "updated_at_ms"}
                        }
                        comparable_row = {
                            name: value
                            for name, value in row.items()
                            if name not in {"created_at_ms", "updated_at_ms"}
                        }
                        if comparable_duplicate != comparable_row:
                            raise ArmEvidenceConflictError(
                                "conflicting duplicate evidence in bundle"
                            )
                        continue
                    seen[row["evidence_id"]] = row

                    existing = await self._db.fetchone(
                        """SELECT * FROM v1469_arm_evidence
                        WHERE evidence_id = ?""",
                        (row["evidence_id"],),
                    )
                    if existing is None:
                        columns = tuple(row)
                        await self._db.conn.execute(
                            f"""INSERT INTO v1469_arm_evidence
                            ({", ".join(columns)})
                            VALUES ({", ".join("?" for _ in columns)})""",
                            tuple(row[name] for name in columns),
                        )
                        inserted += 1
                    else:
                        comparable_existing = {
                            name: existing.get(name)
                            for name in row
                            if name not in {
                                "created_at_ms",
                                "updated_at_ms",
                            }
                        }
                        comparable_row = {
                            name: value
                            for name, value in row.items()
                            if name not in {
                                "created_at_ms",
                                "updated_at_ms",
                            }
                        }
                        if comparable_existing != comparable_row:
                            raise ArmEvidenceConflictError(
                                "conflicting or already-terminal arm evidence"
                            )
                        existing_count += 1
                    durable_rows.append(
                        {
                            "evidence_id": row["evidence_id"],
                            "opportunity_id": row["opportunity_id"],
                            "candidate_id": row["candidate_id"],
                            "arm_key": row["arm_key"],
                            "execution_profile_id": row[
                                "execution_profile_id"
                            ],
                            "execution_profile_schema": row[
                                "execution_profile_schema"
                            ],
                            "execution_profile_hash": row[
                                "execution_profile_hash"
                            ],
                            "source_type": row["source_type"],
                            "diagnostic_only": row["diagnostic_only"],
                            "observed_at_ms": row["observed_at_ms"],
                        }
                    )
                await self._db.conn.commit()
                began = False
                return {
                    "inserted": inserted,
                    "existing": existing_count,
                    "count": len(durable_rows),
                    "evidence": tuple(durable_rows),
                }
            except asyncio.CancelledError:
                if began:
                    await asyncio.shield(self._db.conn.rollback())
                raise
            except sqlite3.IntegrityError as exc:
                if began:
                    await self._db.conn.rollback()
                raise ArmEvidenceConflictError(str(exc)) from exc
            except Exception:
                if began:
                    await self._db.conn.rollback()
                raise

    async def list_pending_evidence(
        self,
        *,
        environment: str,
        symbol: str,
        source_run_id: str | None = None,
        observed_after_ms: int = 0,
        limit: int = 2_048,
    ) -> list[dict[str, Any]]:
        """Return bounded compact pending rows for deterministic restart."""

        scope_environment = str(environment or "").strip().upper()
        scope_symbol = str(symbol or "").strip().upper()
        if not scope_environment or not scope_symbol:
            raise ValueError("environment and symbol must be non-empty")
        after = int(observed_after_ms)
        bounded_limit = int(limit)
        if after < 0:
            raise ValueError("observed_after_ms must be non-negative")
        if not 1 <= bounded_limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")
        params: list[Any] = [
            scope_environment,
            scope_symbol,
            after,
        ]
        run_clause = ""
        if source_run_id is not None:
            run_clause = " AND o.source_run_id = ?"
            params.append(str(source_run_id or "").strip())
        params.append(bounded_limit)
        rows = await self._db.fetchall(
            f"""WITH candidate_groups AS (
                SELECT e.candidate_id,
                       MIN(e.observed_at_ms) AS first_observed_at_ms,
                       COUNT(*) AS evidence_count
                FROM v1469_arm_evidence e
                JOIN v1469_lane_candidates c
                  ON c.candidate_id = e.candidate_id
                JOIN v1469_market_opportunities o
                  ON o.opportunity_id = e.opportunity_id
                WHERE e.status = 'PENDING'
                  AND e.source_type = 'SHADOW'
                  AND o.environment = ? AND o.symbol = ?
                  AND e.observed_at_ms >= ?
                  {run_clause}
                GROUP BY e.candidate_id
            ), bounded_groups AS (
                SELECT candidate_id, first_observed_at_ms, evidence_count,
                       SUM(evidence_count) OVER (
                           ORDER BY first_observed_at_ms, candidate_id
                       ) AS cumulative_count
                FROM candidate_groups
            )
            SELECT
                e.evidence_id, e.opportunity_id, e.candidate_id, e.arm_key,
                e.execution_profile_id, e.execution_profile_schema,
                e.execution_profile_hash, e.source_type,
                e.diagnostic_only, e.observed_at_ms,
                c.lane_code, c.effective_side, c.strategy,
                c.safety_status AS candidate_status, c.data_complete,
                o.environment, o.symbol, o.feature_at_ms, o.coarse_regime,
                o.feature_snapshot_json, o.source_run_id, o.data_quality
            FROM v1469_arm_evidence e
            JOIN v1469_lane_candidates c
              ON c.candidate_id = e.candidate_id
            JOIN v1469_market_opportunities o
              ON o.opportunity_id = e.opportunity_id
            JOIN bounded_groups g
              ON g.candidate_id = e.candidate_id
            WHERE e.status = 'PENDING'
              AND e.source_type = 'SHADOW'
              AND g.cumulative_count <= ?
            ORDER BY e.observed_at_ms, e.evidence_id
            """,
            tuple(params),
        )
        for row in rows:
            raw_snapshot = row.pop("feature_snapshot_json", "{}")
            try:
                row["feature_snapshot"] = json.loads(
                    str(raw_snapshot or "{}")
                )
            except json.JSONDecodeError:
                row["feature_snapshot"] = {}
                row["data_quality"] = "DATA_INCOMPLETE"
        return rows

    async def count_pending_evidence(
        self,
        *,
        environment: str,
        symbol: str,
        source_run_id: str | None = None,
        observed_after_ms: int = 0,
    ) -> int:
        """Count pending rows so restart code can detect bounded truncation."""

        scope_environment = str(environment or "").strip().upper()
        scope_symbol = str(symbol or "").strip().upper()
        after = int(observed_after_ms)
        if not scope_environment or not scope_symbol:
            raise ValueError("environment and symbol must be non-empty")
        if after < 0:
            raise ValueError("observed_after_ms must be non-negative")
        params: list[Any] = [scope_environment, scope_symbol, after]
        run_clause = ""
        if source_run_id is not None:
            run_clause = " AND o.source_run_id = ?"
            params.append(str(source_run_id or "").strip())
        row = await self._db.fetchone(
            f"""SELECT COUNT(*) AS n
            FROM v1469_arm_evidence e
            JOIN v1469_market_opportunities o
              ON o.opportunity_id = e.opportunity_id
            WHERE e.status = 'PENDING'
              AND e.source_type = 'SHADOW'
              AND o.environment = ? AND o.symbol = ?
              AND e.observed_at_ms >= ?
              {run_clause}""",
            tuple(params),
        )
        return int((row or {}).get("n") or 0)

    async def terminal_evidence_window(
        self,
        *,
        environment: str,
        symbol: str,
        window_start_ms: int,
        as_of_ms: int,
        limit: int = 10_000,
    ) -> list[dict[str, Any]]:
        """Return bounded terminal arm rows needed by the pure arbiter."""

        scope_environment = str(environment or "").strip().upper()
        scope_symbol = str(symbol or "").strip().upper()
        start, end, bounded_limit = (
            int(window_start_ms),
            int(as_of_ms),
            int(limit),
        )
        if not scope_environment or not scope_symbol:
            raise ValueError("environment and symbol must be non-empty")
        if start < 0 or end < start:
            raise ValueError("invalid evidence window")
        if not 1 <= bounded_limit <= 50_000:
            raise ValueError("limit must be between 1 and 50000")
        return await self._db.fetchall(
            """SELECT
                e.evidence_id, e.opportunity_id, e.candidate_id, e.arm_key,
                e.execution_profile_id, e.execution_profile_schema,
                e.execution_profile_hash, e.observed_at_ms,
                e.terminal_at_ms, e.outcome, e.fill_status,
                e.data_complete, e.ambiguous, e.reward_net_bp,
                e.mfe_bp, e.mae_bp, e.terminal_reason, e.evidence_hash,
                c.lane_code, c.effective_side, c.strategy,
                c.safety_status AS candidate_status,
                o.coarse_regime, o.feature_at_ms, o.data_quality
            FROM v1469_arm_evidence e
            JOIN v1469_lane_candidates c
              ON c.candidate_id = e.candidate_id
            JOIN v1469_market_opportunities o
              ON o.opportunity_id = e.opportunity_id
            WHERE e.source_type = 'SHADOW'
              AND e.diagnostic_only = 0
              AND e.status = 'TERMINAL'
              AND o.environment = ? AND o.symbol = ?
              AND e.observed_at_ms BETWEEN ? AND ?
            ORDER BY e.observed_at_ms, e.opportunity_id, e.arm_key
            LIMIT ?""",
            (
                scope_environment,
                scope_symbol,
                start,
                end,
                bounded_limit,
            ),
        )

    async def durable_terminal_evidence_ledger(
        self,
        *,
        environment: str,
        symbol: str,
        as_of_ms: int,
        limit: int = 50_000,
    ) -> dict[str, Any]:
        """Return a proven-complete bounded SHADOW terminal ledger.

        The extra row is a truncation sentinel.  Callers must pass
        ``scope_complete`` to the durable evidence mapper; a ledger that
        exceeds the explicit bound can be monitored, but can never authorize
        an arm.
        """

        scope_environment = str(environment or "").strip().upper()
        scope_symbol = str(symbol or "").strip().upper()
        end = int(as_of_ms)
        bounded_limit = int(limit)
        if not scope_environment or not scope_symbol:
            raise ValueError("environment and symbol must be non-empty")
        if end < 0:
            raise ValueError("as_of_ms must be non-negative")
        if not 1 <= bounded_limit <= 100_000:
            raise ValueError("limit must be between 1 and 100000")
        rows = await self._db.fetchall(
            """SELECT
                e.evidence_id, e.opportunity_id, e.candidate_id, e.arm_key,
                e.execution_profile_id, e.execution_profile_schema,
                e.execution_profile_hash, e.source_type,
                e.diagnostic_only, e.observed_at_ms, e.status,
                e.terminal_at_ms, e.outcome, e.fill_status,
                e.data_complete, e.ambiguous, e.reward_net_bp,
                e.mfe_bp, e.mae_bp, e.terminal_reason,
                e.terminal_payload_json, e.evidence_hash,
                c.lane_code, c.effective_side, c.strategy,
                c.safety_status AS candidate_status,
                o.coarse_regime, o.feature_at_ms, o.data_quality
            FROM v1469_arm_evidence e
            JOIN v1469_lane_candidates c
              ON c.candidate_id = e.candidate_id
            JOIN v1469_market_opportunities o
              ON o.opportunity_id = e.opportunity_id
            WHERE e.source_type = 'SHADOW'
              AND e.status IN ('TERMINAL', 'DROPPED')
              AND o.environment = ? AND o.symbol = ?
              AND e.terminal_at_ms <= ?
            ORDER BY
                e.observed_at_ms, e.opportunity_id, e.candidate_id,
                e.execution_profile_id, e.evidence_id
            LIMIT ?""",
            (
                scope_environment,
                scope_symbol,
                end,
                bounded_limit + 1,
            ),
        )
        scope_complete = len(rows) <= bounded_limit
        visible = rows[:bounded_limit]
        return {
            "rows": visible,
            "scope_complete": scope_complete,
            "row_count": len(visible),
            "limit": bounded_limit,
            "truncated": not scope_complete,
            "as_of_ms": end,
        }

    @staticmethod
    def _normalize_terminal(
        existing: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        terminal_at = _integer(payload, "terminal_at_ms")
        if terminal_at < int(existing["observed_at_ms"]):
            raise ValueError("terminal_at_ms must be >= observed_at_ms")
        status = _enum(
            payload, "status", frozenset({"TERMINAL", "DROPPED"})
        )
        outcome = _enum(payload, "outcome", _OUTCOMES, lower=True)
        fill_status = _enum(payload, "fill_status", _FILL_STATUSES)
        data_complete = _flag(payload, "data_complete")
        ambiguous = _flag(payload, "ambiguous")
        if outcome == "ambiguous_both" and not ambiguous:
            raise ValueError("ambiguous_both must set ambiguous=true")
        if status == "DROPPED" and data_complete:
            raise ValueError("dropped evidence cannot be data complete")
        reward_net_bp = _finite(
            payload, "reward_net_bp", allow_none=True
        )
        if outcome == "no_fill":
            if fill_status != "NO_FILL":
                raise ValueError("no_fill outcome must use NO_FILL fill_status")
            if reward_net_bp not in (None, 0.0):
                raise ValueError("no_fill reward_net_bp must be zero")
            reward_net_bp = 0.0
        elif outcome in {
            "tp1_first",
            "tp_first",
            "tp",
            "sl_first",
            "sl",
            "max_hold",
        } and fill_status != "FILLED":
            raise ValueError(f"{outcome} outcome must use FILLED fill_status")
        terminal_payload = payload.get(
            "terminal_payload", payload.get("terminal_payload_json", {})
        )
        if isinstance(terminal_payload, str):
            try:
                terminal_payload = json.loads(terminal_payload)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "terminal_payload_json must contain JSON"
                ) from exc
        terminal = {
            "status": status,
            "terminal_at_ms": terminal_at,
            "outcome": outcome,
            "fill_status": fill_status,
            "data_complete": data_complete,
            "ambiguous": ambiguous,
            "reward_net_bp": reward_net_bp,
            "mfe_bp": _finite(payload, "mfe_bp", allow_none=True),
            "mae_bp": _finite(payload, "mae_bp", allow_none=True),
            "terminal_reason": _optional_text(payload, "terminal_reason"),
            "terminal_payload_json": _canonical_json(
                terminal_payload,
                max_bytes=_MAX_COMPACT_JSON_BYTES,
                field="terminal_payload",
            ),
        }
        evidence_hash = hashlib.sha256(
            json.dumps(
                {
                    "evidence_id": existing["evidence_id"],
                    "opportunity_id": existing["opportunity_id"],
                    "candidate_id": existing["candidate_id"],
                    "arm_key": existing["arm_key"],
                    "execution_profile_hash": existing[
                        "execution_profile_hash"
                    ],
                    "source_type": existing["source_type"],
                    "diagnostic_only": existing["diagnostic_only"],
                    "observed_at_ms": existing["observed_at_ms"],
                    **terminal,
                },
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        supplied_hash = str(payload.get("evidence_hash") or "").strip()
        if supplied_hash and supplied_hash != evidence_hash:
            raise ValueError("evidence_hash does not match terminal evidence")
        return {
            **terminal,
            "evidence_hash": evidence_hash,
            "updated_at_ms": _integer(
                payload, "updated_at_ms", minimum=terminal_at
            ),
        }

    async def terminal_evidence(
        self,
        evidence_id: str,
        payload: Mapping[str, Any],
    ) -> bool:
        """Terminalize pending evidence once; an exact replay is a no-op."""

        key = str(evidence_id or "").strip()
        if not key:
            raise ValueError("evidence_id must be non-empty")
        async with self._write_lock:
            began = False
            try:
                await self._db.conn.execute("BEGIN IMMEDIATE")
                began = True
                existing = await self._db.fetchone(
                    """SELECT * FROM v1469_arm_evidence
                    WHERE evidence_id = ?""",
                    (key,),
                )
                if existing is None:
                    raise ArmEvidenceConflictError("unknown evidence_id")
                terminal = self._normalize_terminal(existing, payload)
                compare_names = tuple(
                    name for name in terminal if name != "updated_at_ms"
                )
                if existing["status"] != "PENDING":
                    if all(
                        existing.get(name) == terminal[name]
                        for name in compare_names
                    ):
                        await self._db.conn.rollback()
                        began = False
                        return False
                    raise ArmEvidenceConflictError(
                        "conflicting terminal evidence"
                    )
                cursor = await self._db.conn.execute(
                    f"""UPDATE v1469_arm_evidence
                    SET {", ".join(f"{name} = ?" for name in terminal)}
                    WHERE evidence_id = ? AND status = 'PENDING'""",
                    (*tuple(terminal.values()), key),
                )
                if cursor.rowcount != 1:
                    raise ArmEvidenceConflictError(
                        "evidence terminal compare-and-set failed"
                    )
                await self._db.conn.commit()
                began = False
                return True
            except sqlite3.IntegrityError as exc:
                if began:
                    await self._db.conn.rollback()
                raise ArmEvidenceConflictError(str(exc)) from exc
            except Exception:
                if began:
                    await self._db.conn.rollback()
                raise

    async def terminal_evidence_bundle(
        self,
        terminal_rows: Sequence[Mapping[str, Any]],
    ) -> dict[str, int]:
        """Atomically terminalize every profile sharing one paired envelope."""

        if isinstance(terminal_rows, (str, bytes)) or not isinstance(
            terminal_rows, Sequence
        ):
            raise TypeError("terminal_rows must be a sequence of mappings")
        if not terminal_rows:
            raise ValueError("terminal_rows must not be empty")
        if len(terminal_rows) > _MAX_EVIDENCE_PER_BUNDLE:
            raise ValueError(
                "terminal_rows exceeds "
                f"{_MAX_EVIDENCE_PER_BUNDLE} evidence rows"
            )
        supplied: list[tuple[str, Mapping[str, Any]]] = []
        seen: set[str] = set()
        for item in terminal_rows:
            if not isinstance(item, Mapping):
                raise TypeError("every terminal row must be a mapping")
            evidence_id = _text(item, "evidence_id")
            if evidence_id in seen:
                raise ValueError("duplicate evidence_id in terminal bundle")
            seen.add(evidence_id)
            terminal_payload = item.get("terminal")
            if not isinstance(terminal_payload, Mapping):
                raise TypeError("terminal must be a mapping")
            supplied.append((evidence_id, terminal_payload))

        async with self._write_lock:
            began = False
            updated = 0
            existing_count = 0
            try:
                await self._db.conn.execute("BEGIN IMMEDIATE")
                began = True
                normalized: list[tuple[str, dict[str, Any]]] = []
                for evidence_id, payload in supplied:
                    existing = await self._db.fetchone(
                        """SELECT * FROM v1469_arm_evidence
                        WHERE evidence_id = ?""",
                        (evidence_id,),
                    )
                    if existing is None:
                        raise ArmEvidenceConflictError(
                            "unknown evidence_id"
                        )
                    terminal = self._normalize_terminal(existing, payload)
                    compare_names = tuple(
                        name for name in terminal if name != "updated_at_ms"
                    )
                    if existing["status"] != "PENDING":
                        if all(
                            existing.get(name) == terminal[name]
                            for name in compare_names
                        ):
                            existing_count += 1
                            continue
                        raise ArmEvidenceConflictError(
                            "conflicting terminal evidence"
                        )
                    normalized.append((evidence_id, terminal))

                for evidence_id, terminal in normalized:
                    cursor = await self._db.conn.execute(
                        f"""UPDATE v1469_arm_evidence
                        SET {", ".join(f"{name} = ?" for name in terminal)}
                        WHERE evidence_id = ? AND status = 'PENDING'""",
                        (*tuple(terminal.values()), evidence_id),
                    )
                    if cursor.rowcount != 1:
                        raise ArmEvidenceConflictError(
                            "evidence terminal compare-and-set failed"
                        )
                    updated += 1
                await self._db.conn.commit()
                began = False
                return {
                    "updated": updated,
                    "existing": existing_count,
                    "count": len(supplied),
                }
            except asyncio.CancelledError:
                if began:
                    await asyncio.shield(self._db.conn.rollback())
                raise
            except sqlite3.IntegrityError as exc:
                if began:
                    await self._db.conn.rollback()
                raise ArmEvidenceConflictError(str(exc)) from exc
            except Exception:
                if began:
                    await self._db.conn.rollback()
                raise

    async def append_arm_event(self, payload: Mapping[str, Any]) -> bool:
        """Append a compact lifecycle event; exact idempotent retries are no-ops."""

        event_payload = payload.get(
            "payload", payload.get("payload_json", {})
        )
        if isinstance(event_payload, str):
            try:
                event_payload = json.loads(event_payload)
            except json.JSONDecodeError as exc:
                raise ValueError("payload_json must contain JSON") from exc
        row = {
            "idempotency_key": _text(payload, "idempotency_key"),
            "arm_key": _text(payload, "arm_key"),
            "lease_id": _optional_text(payload, "lease_id"),
            "opportunity_id": _optional_text(payload, "opportunity_id"),
            "candidate_id": _optional_text(payload, "candidate_id"),
            "generation_before": _optional_integer(
                payload, "generation_before"
            ),
            "generation_after": _optional_integer(
                payload, "generation_after", minimum=1
            ),
            "event_time_ms": _integer(payload, "event_time_ms"),
            "event_type": _enum(payload, "event_type", _EVENT_TYPES),
            "actor": _text(payload, "actor"),
            "payload_json": _canonical_json(
                event_payload,
                max_bytes=_MAX_COMPACT_JSON_BYTES,
                field="payload",
            ),
        }
        before, after = row["generation_before"], row["generation_after"]
        if before is not None and after is not None and after <= before:
            raise ValueError("generation_after must exceed generation_before")
        async with self._write_lock:
            began = False
            try:
                await self._db.conn.execute("BEGIN IMMEDIATE")
                began = True
                existing = await self._db.fetchone(
                    """SELECT idempotency_key, arm_key, lease_id,
                              opportunity_id, candidate_id,
                              generation_before, generation_after,
                              event_time_ms, event_type, actor, payload_json
                    FROM v1469_arm_events WHERE idempotency_key = ?""",
                    (row["idempotency_key"],),
                )
                if existing is not None:
                    if existing != row:
                        raise ArmObservationConflictError(
                            "idempotency key reused for a different arm event"
                        )
                    await self._db.conn.rollback()
                    began = False
                    return False
                columns = tuple(row)
                await self._db.conn.execute(
                    f"""INSERT INTO v1469_arm_events
                    ({", ".join(columns)})
                    VALUES ({", ".join("?" for _ in columns)})""",
                    tuple(row[name] for name in columns),
                )
                await self._db.conn.commit()
                began = False
                return True
            except sqlite3.IntegrityError as exc:
                if began:
                    await self._db.conn.rollback()
                raise ArmObservationConflictError(str(exc)) from exc
            except Exception:
                if began:
                    await self._db.conn.rollback()
                raise

    async def get_monitor_summary(
        self,
        *,
        environment: str,
        symbol: str,
        window_start_ms: int,
        as_of_ms: int,
    ) -> dict[str, Any]:
        """Return bounded aggregate rows for Lane Monitor; never raw snapshots."""

        scope_environment = str(environment or "").strip().upper()
        scope_symbol = str(symbol or "").strip().upper()
        if not scope_environment or not scope_symbol:
            raise ValueError("environment and symbol must be non-empty")
        start, end = int(window_start_ms), int(as_of_ms)
        if start < 0 or end < start:
            raise ValueError("invalid monitor window")
        totals = await self._db.fetchone(
            """SELECT
                COUNT(*) AS opportunities,
                COALESCE(SUM(
                    CASE WHEN data_quality = 'COMPLETE' THEN 1 ELSE 0 END
                ), 0)
                    AS complete_opportunities,
                COUNT(DISTINCT coarse_regime) AS regimes,
                COALESCE(MAX(observed_at_ms), 0) AS last_observed_at_ms
            FROM v1469_market_opportunities
            WHERE environment = ? AND symbol = ?
              AND observed_at_ms BETWEEN ? AND ?""",
            (scope_environment, scope_symbol, start, end),
        )
        lanes = await self._db.fetchall(
            """SELECT
                c.lane_code,
                COUNT(DISTINCT c.candidate_id) AS candidates,
                COUNT(DISTINCT CASE
                    WHEN c.match_status = 'MATCH' THEN c.candidate_id END)
                    AS matched,
                COUNT(DISTINCT CASE
                    WHEN c.match_status = 'NEAR_MATCH' THEN c.candidate_id END)
                    AS near_matched,
                COUNT(DISTINCT CASE
                    WHEN c.safety_status = 'SAFE' THEN c.candidate_id END)
                    AS safe,
                COUNT(DISTINCT CASE
                    WHEN c.safety_status = 'HARD_BLOCK' THEN c.candidate_id END)
                    AS hard_blocked,
                COUNT(DISTINCT CASE
                    WHEN c.safety_status = 'DATA_BLOCKED'
                    THEN c.candidate_id END) AS data_blocked,
                COUNT(DISTINCT CASE
                    WHEN c.safety_status = 'NOT_EVALUATED'
                    THEN c.candidate_id END) AS not_evaluated,
                COUNT(DISTINCT CASE
                    WHEN c.is_selected = 1 THEN c.candidate_id END)
                    AS selected,
                COUNT(DISTINCT CASE
                    WHEN c.suppression_reason IS NOT NULL
                    THEN c.candidate_id END)
                    AS suppressed,
                COUNT(DISTINCT e.evidence_id) AS evidence,
                COUNT(DISTINCT CASE
                    WHEN e.status = 'PENDING' THEN e.evidence_id END)
                    AS pending,
                COUNT(DISTINCT CASE
                    WHEN e.status = 'TERMINAL' THEN e.evidence_id END)
                    AS terminal,
                COUNT(DISTINCT CASE
                    WHEN e.status = 'TERMINAL'
                     AND e.data_complete = 1
                     AND e.ambiguous = 0
                     AND e.diagnostic_only = 0
                    THEN e.evidence_id END) AS evaluable,
                SUM(CASE
                    WHEN e.status = 'TERMINAL'
                     AND e.data_complete = 1
                     AND e.ambiguous = 0
                     AND e.diagnostic_only = 0
                    THEN COALESCE(e.reward_net_bp, 0) ELSE 0 END)
                    AS evaluable_reward_net_bp,
                COALESCE(MAX(o.observed_at_ms), 0) AS last_observed_at_ms
            FROM v1469_market_opportunities o
            JOIN v1469_lane_candidates c
              ON c.opportunity_id = o.opportunity_id
            LEFT JOIN v1469_arm_evidence e
              ON e.candidate_id = c.candidate_id
            WHERE o.environment = ? AND o.symbol = ?
              AND o.observed_at_ms BETWEEN ? AND ?
            GROUP BY c.lane_code
            ORDER BY c.lane_code""",
            (scope_environment, scope_symbol, start, end),
        )
        suppressed_by = await self._db.fetchall(
            """SELECT
                c.lane_code,
                COALESCE(NULLIF(c.suppressed_by_lane_code, ''), 'UNSPECIFIED')
                    AS suppressed_by_lane_code,
                COUNT(DISTINCT c.candidate_id) AS candidates
            FROM v1469_market_opportunities o
            JOIN v1469_lane_candidates c
              ON c.opportunity_id = o.opportunity_id
            WHERE o.environment = ? AND o.symbol = ?
              AND o.observed_at_ms BETWEEN ? AND ?
              AND c.suppression_reason IS NOT NULL
            GROUP BY c.lane_code, suppressed_by_lane_code
            ORDER BY c.lane_code, candidates DESC, suppressed_by_lane_code""",
            (scope_environment, scope_symbol, start, end),
        )
        outcomes = await self._db.fetchall(
            """SELECT c.lane_code, e.outcome, COUNT(*) AS samples
            FROM v1469_market_opportunities o
            JOIN v1469_lane_candidates c
              ON c.opportunity_id = o.opportunity_id
            JOIN v1469_arm_evidence e
              ON e.candidate_id = c.candidate_id
            WHERE o.environment = ? AND o.symbol = ?
              AND o.observed_at_ms BETWEEN ? AND ?
              AND e.status IN ('TERMINAL', 'DROPPED')
            GROUP BY c.lane_code, e.outcome
            ORDER BY c.lane_code, e.outcome""",
            (scope_environment, scope_symbol, start, end),
        )
        arms = await self._db.fetchall(
            """SELECT
                e.arm_key,
                c.lane_code,
                c.effective_side,
                c.strategy,
                o.coarse_regime,
                e.execution_profile_id,
                e.execution_profile_schema,
                e.execution_profile_hash,
                COUNT(*) AS evidence,
                SUM(CASE WHEN e.status = 'PENDING' THEN 1 ELSE 0 END)
                    AS pending,
                SUM(CASE WHEN e.status = 'TERMINAL' THEN 1 ELSE 0 END)
                    AS terminal,
                SUM(CASE WHEN e.status = 'DROPPED' THEN 1 ELSE 0 END)
                    AS dropped,
                SUM(CASE
                    WHEN e.status = 'TERMINAL'
                     AND e.data_complete = 1
                     AND e.ambiguous = 0
                     AND e.diagnostic_only = 0
                    THEN 1 ELSE 0 END) AS evaluable,
                SUM(CASE
                    WHEN e.status = 'TERMINAL'
                     AND e.data_complete = 1
                     AND e.ambiguous = 0
                     AND e.diagnostic_only = 0
                    THEN COALESCE(e.reward_net_bp, 0) ELSE 0 END)
                    AS evaluable_reward_net_bp,
                SUM(CASE
                    WHEN e.status = 'TERMINAL'
                     AND e.outcome IN ('tp1_first', 'tp_first', 'tp')
                    THEN 1 ELSE 0 END) AS tp_first,
                SUM(CASE
                    WHEN e.status = 'TERMINAL'
                     AND e.outcome IN ('sl_first', 'sl')
                    THEN 1 ELSE 0 END) AS sl_first,
                SUM(CASE
                    WHEN e.status = 'TERMINAL'
                     AND e.outcome = 'no_fill'
                    THEN 1 ELSE 0 END) AS no_fill,
                COALESCE(MAX(e.updated_at_ms), 0) AS last_evidence_at_ms
            FROM v1469_market_opportunities o
            JOIN v1469_lane_candidates c
              ON c.opportunity_id = o.opportunity_id
            JOIN v1469_arm_evidence e
              ON e.candidate_id = c.candidate_id
            WHERE o.environment = ? AND o.symbol = ?
              AND o.observed_at_ms BETWEEN ? AND ?
            GROUP BY
                e.arm_key, c.lane_code, c.effective_side, c.strategy,
                o.coarse_regime, e.execution_profile_id,
                e.execution_profile_schema, e.execution_profile_hash
            ORDER BY c.lane_code, e.execution_profile_id, e.arm_key""",
            (scope_environment, scope_symbol, start, end),
        )
        leases = await self._db.fetchall(
            """SELECT arm_key, lease_id, lane_code, effective_side,
                      coarse_regime, execution_profile_id, phase, status,
                      notional_cap_usdc, issued_at_ms, expires_at_ms,
                      evidence_revision
            FROM v1469_arm_leases
            WHERE environment = ? AND symbol = ?
              AND status IN ('ACTIVE', 'COOLDOWN')
            ORDER BY status, lane_code, arm_key""",
            (scope_environment, scope_symbol),
        )
        return {
            "environment": scope_environment,
            "symbol": scope_symbol,
            "window_start_ms": start,
            "as_of_ms": end,
            "opportunities": totals or {
                "opportunities": 0,
                "complete_opportunities": 0,
                "regimes": 0,
                "last_observed_at_ms": 0,
            },
            "lanes": lanes,
            "arms": arms,
            "outcomes": outcomes,
            "suppressed_by": suppressed_by,
            "leases": leases,
        }


__all__ = [
    "ArmEvidenceConflictError",
    "ArmObservationConflictError",
    "ArmObservationPersistenceError",
    "V1469ArmObservationRepository",
    "arm_identity",
    "candidate_identity",
    "evidence_identity",
]
