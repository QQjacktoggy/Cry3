"""Fail-closed persistence for v1.4.65 W6A profile evidence and selection."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import hashlib
import json
from math import isfinite
import sqlite3
from typing import Any, Mapping

from src.gridbot.storage.database import Database


_IDENTITY_FIELDS = (
    "environment", "symbol", "lane_code", "market_state", "effective_side",
    "strategy",
)
_OUTCOMES = frozenset({
    "tp1_first", "tp_first", "tp", "sl_first", "sl", "max_hold",
    "no_fill", "ambiguous_both",
})
_STATUSES = frozenset({"SHADOW", "PROBATION", "LIVE", "EXPIRED", "DEMOTED"})
_EVENTS = frozenset({"GRANTED", "RENEWED", "SWITCHED", "DEMOTED", "EXPIRED"})
_TABLES = {
    "v1465_w6a_profile_evidence": frozenset({
        "evidence_id", "opportunity_id", *_IDENTITY_FIELDS, "profile_id",
        "resolved_profile_hash", "profile_plan_hash", "observed_at_ms",
        "terminal_at_ms", "outcome", "data_complete", "ambiguous",
        "diagnostic_only", "net_pnl_bp", "source_payload_json", "evidence_hash",
        "created_at_ms",
    }),
    "v1465_w6a_profile_selections": frozenset({
        "selector_key", *_IDENTITY_FIELDS, "winner_profile_id",
        "winner_resolved_profile_hash", "generation", "status", "notional_cap_usdc",
        "issued_at_ms", "renewed_at_ms", "expires_at_ms", "evidence_revision",
        "evidence_snapshot_hash", "evidence_snapshot_json", "policy_hash",
        "owner_id", "boot_id", "demotion_reason", "demoted_at_ms",
        "cooldown_until_ms", "created_at_ms", "updated_at_ms",
    }),
    "v1465_w6a_profile_selection_events": frozenset({
        "id", "idempotency_key", "selector_key", "generation_before",
        "generation_after", "event_time_ms", "event_type", "actor", "payload_json",
    }),
}
_TRIGGERS = frozenset({
    "trg_v1465_w6a_profile_evidence_no_update",
    "trg_v1465_w6a_profile_evidence_no_delete",
    "trg_v1465_w6a_selection_events_no_update",
    "trg_v1465_w6a_selection_events_no_delete",
})
_SCHEMA_MARKERS = {
    "v1465_w6a_profile_evidence": (
        "EVIDENCE_ID TEXT PRIMARY KEY",
        "CHECK(LANE_CODE = 'W6A')",
        "UNIQUE(OPPORTUNITY_ID, RESOLVED_PROFILE_HASH)",
    ),
    "v1465_w6a_profile_selections": (
        "SELECTOR_KEY TEXT PRIMARY KEY",
        "'SHADOW', 'PROBATION', 'LIVE', 'EXPIRED', 'DEMOTED'",
        "CHECK(STATUS NOT IN ('PROBATION', 'LIVE') OR NOTIONAL_CAP_USDC > 0)",
    ),
    "v1465_w6a_profile_selection_events": (
        "'GRANTED', 'RENEWED', 'SWITCHED', 'DEMOTED', 'EXPIRED'",
        "GENERATION_AFTER > GENERATION_BEFORE",
    ),
}


class W6AProfilePersistenceError(RuntimeError):
    """The persistence contract is unsafe and selection must be blocked."""


class W6AProfileConflictError(W6AProfilePersistenceError):
    """An immutable row or idempotency key was reused differently."""


class W6ASelectionConflictError(W6AProfilePersistenceError):
    """The requested selector generation is stale (CAS failed)."""


@dataclass(frozen=True, slots=True)
class W6ASelector:
    environment: str
    symbol: str
    lane_code: str
    market_state: str
    effective_side: str
    strategy: str

    @property
    def key(self) -> str:
        return w6a_selector_key(asdict(self))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _text(source: Mapping[str, Any], name: str) -> str:
    value = str(source.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} must be non-empty")
    return value


def _integer(source: Mapping[str, Any], name: str, *, minimum: int = 0) -> int:
    value = source.get(name)
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return parsed


def _flag(source: Mapping[str, Any], name: str, *, default: bool = False) -> int:
    value = source.get(name, default)
    if not isinstance(value, (bool, int)) or value not in (False, True, 0, 1):
        raise ValueError(f"{name} must be boolean")
    return int(bool(value))


def _finite(source: Mapping[str, Any], name: str, *, none: bool = False) -> float | None:
    value = source.get(name)
    if value is None and none:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _identity(source: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(source, Mapping):
        raise TypeError("selector must be a mapping")
    identity = {name: _text(source, name) for name in _IDENTITY_FIELDS}
    identity["environment"] = identity["environment"].upper()
    identity["symbol"] = identity["symbol"].upper()
    identity["lane_code"] = identity["lane_code"].upper()
    identity["market_state"] = identity["market_state"].lower()
    identity["effective_side"] = identity["effective_side"].upper()
    if identity["lane_code"] != "W6A":
        raise ValueError("lane_code must be W6A")
    if identity["effective_side"] not in {"LONG", "SHORT"}:
        raise ValueError("effective_side must be LONG or SHORT")
    return identity


def w6a_selector_key(source: Mapping[str, Any] | W6ASelector) -> str:
    """Stable key for the one selector that arbitrates W6A profiles."""

    payload = asdict(source) if isinstance(source, W6ASelector) else source
    return "v1465_w6a_" + hashlib.sha256(_json(_identity(payload)).encode()).hexdigest()


def _decode(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    decoded = dict(row)
    for name in ("source_payload_json", "evidence_snapshot_json", "payload_json"):
        if name in decoded:
            try:
                decoded[name.removesuffix("_json")] = json.loads(decoded[name])
            except (TypeError, json.JSONDecodeError):
                decoded[name.removesuffix("_json")] = None
    return decoded


class V1465W6AProfileRepository:
    """Immutable W6A evidence plus transactional single-winner selection."""

    def __init__(self, db: Database) -> None:
        self._db = db
        self._write_lock = asyncio.Lock()

    async def schema_fingerprint(self) -> str:
        objects = await self._db.fetchall(
            """SELECT type, name, sql FROM sqlite_master WHERE name LIKE 'v1465_w6a_%'
               OR name LIKE 'trg_v1465_w6a_%' ORDER BY type, name"""
        )
        columns = {
            table: await self._db.fetchall(f"PRAGMA table_info({table})")
            for table in sorted(_TABLES)
        }
        return hashlib.sha256(_json({"objects": objects, "columns": columns}).encode()).hexdigest()

    async def assert_schema_ready(self) -> str:
        objects = await self._db.fetchall(
            """SELECT type, name, sql FROM sqlite_master WHERE name LIKE 'v1465_w6a_%'
               OR name LIKE 'trg_v1465_w6a_%'"""
        )
        names = {str(row.get("name") or ""): row for row in objects}
        problems: list[str] = []
        for table, required in _TABLES.items():
            actual = {str(r.get("name") or "") for r in await self._db.fetchall(f"PRAGMA table_info({table})")}
            missing = sorted(required - actual)
            if missing:
                problems.append(f"{table}:missing_columns={','.join(missing)}")
            sql = " ".join(str((names.get(table) or {}).get("sql") or "").upper().split())
            for marker in _SCHEMA_MARKERS[table]:
                if marker not in sql:
                    problems.append(f"{table}:missing_contract={marker}")
        absent = sorted(_TRIGGERS - set(names))
        if absent:
            problems.append("missing_triggers=" + ",".join(absent))
        if problems:
            raise W6AProfilePersistenceError("unsafe v1.4.65 W6A schema: " + "; ".join(problems))
        return await self.schema_fingerprint()

    @staticmethod
    def _normalize_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
        identity = _identity(payload)
        observed = _integer(payload, "observed_at_ms")
        terminal = _integer(payload, "terminal_at_ms")
        if terminal < observed:
            raise ValueError("terminal_at_ms precedes observed_at_ms")
        outcome = _text(payload, "outcome")
        if outcome not in _OUTCOMES:
            raise ValueError("unsupported outcome")
        ambiguous = _flag(payload, "ambiguous")
        if outcome == "ambiguous_both" and not ambiguous:
            raise ValueError("ambiguous_both requires ambiguous")
        source = payload.get("source_payload", payload.get("source_payload_json", {}))
        if isinstance(source, str):
            try:
                source = json.loads(source)
            except json.JSONDecodeError as exc:
                raise ValueError("source_payload_json must contain JSON") from exc
        immutable = {
            "evidence_id": _text(payload, "evidence_id"),
            "opportunity_id": _text(payload, "opportunity_id"),
            **identity,
            "profile_id": _text(payload, "profile_id"),
            "resolved_profile_hash": _text(payload, "resolved_profile_hash"),
            "profile_plan_hash": _text(payload, "profile_plan_hash"),
            "observed_at_ms": observed, "terminal_at_ms": terminal, "outcome": outcome,
            "data_complete": _flag(payload, "data_complete"), "ambiguous": ambiguous,
            "diagnostic_only": _flag(payload, "diagnostic_only"),
            "net_pnl_bp": _finite(payload, "net_pnl_bp", none=True),
            "source_payload_json": _json(source),
        }
        digest = hashlib.sha256(_json(immutable).encode()).hexdigest()
        supplied = str(payload.get("evidence_hash") or "").strip()
        if supplied and supplied != digest:
            raise ValueError("evidence_hash does not match normalized evidence")
        created = _integer(payload, "created_at_ms")
        if created < terminal:
            raise ValueError("created_at_ms precedes terminal_at_ms")
        return {**immutable, "evidence_hash": digest, "created_at_ms": created}

    async def get_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        key = str(evidence_id or "").strip()
        if not key:
            raise ValueError("evidence_id must be non-empty")
        return _decode(await self._db.fetchone("SELECT * FROM v1465_w6a_profile_evidence WHERE evidence_id = ?", (key,)))

    async def upsert_evidence(self, payload: Mapping[str, Any]) -> bool:
        """Insert immutable evidence; only a byte-for-byte logical replay is a no-op."""
        evidence = self._normalize_evidence(payload)
        columns = tuple(evidence)
        async with self._write_lock:
            started = False
            try:
                await self._db.conn.execute("BEGIN IMMEDIATE"); started = True
                existing = await self._db.fetchone("SELECT * FROM v1465_w6a_profile_evidence WHERE evidence_id = ?", (evidence["evidence_id"],))
                if existing is not None:
                    expected = dict(evidence)
                    actual = {k: existing.get(k) for k in expected}
                    if actual != expected:
                        raise W6AProfileConflictError("conflicting immutable evidence_id")
                    await self._db.conn.rollback(); started = False
                    return False
                duplicate = await self._db.fetchone(
                    "SELECT evidence_id FROM v1465_w6a_profile_evidence WHERE opportunity_id = ? AND resolved_profile_hash = ?",
                    (evidence["opportunity_id"], evidence["resolved_profile_hash"]),
                )
                if duplicate is not None:
                    raise W6AProfileConflictError("opportunity/profile evidence already belongs to another evidence_id")
                await self._db.conn.execute(
                    f"INSERT INTO v1465_w6a_profile_evidence ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
                    tuple(evidence[c] for c in columns),
                )
                await self._db.conn.commit(); started = False
                return True
            except sqlite3.IntegrityError as exc:
                if started: await self._db.conn.rollback()
                raise W6AProfileConflictError(str(exc)) from exc
            except Exception:
                if started: await self._db.conn.rollback()
                raise

    async def list_evidence(self, selector: Mapping[str, Any] | W6ASelector, *, window_start_ms: int, as_of_ms: int, resolved_profile_hash: str | None = None, eligible_only: bool = True) -> list[dict[str, Any]]:
        identity = _identity(asdict(selector) if isinstance(selector, W6ASelector) else selector)
        start, end = int(window_start_ms), int(as_of_ms)
        if start < 0 or end < start:
            raise ValueError("invalid evidence time window")
        predicates = [*(f"{name} = ?" for name in _IDENTITY_FIELDS), "observed_at_ms >= ?", "observed_at_ms <= ?", "terminal_at_ms <= ?"]
        params: list[Any] = [*(identity[n] for n in _IDENTITY_FIELDS), start, end, end]
        if resolved_profile_hash is not None:
            predicates.append("resolved_profile_hash = ?"); params.append(_text({"resolved_profile_hash": resolved_profile_hash}, "resolved_profile_hash"))
        if eligible_only:
            predicates.extend(("data_complete = 1", "ambiguous = 0", "diagnostic_only = 0"))
        rows = await self._db.fetchall(f"SELECT * FROM v1465_w6a_profile_evidence WHERE {' AND '.join(predicates)} ORDER BY observed_at_ms, terminal_at_ms, evidence_id", tuple(params))
        return [_decode(row) or {} for row in rows]

    @staticmethod
    def _normalize_selection(payload: Mapping[str, Any], *, generation: int, created: int, updated: int) -> dict[str, Any]:
        identity = _identity(payload)
        selector_key = w6a_selector_key(identity)
        supplied = str(payload.get("selector_key") or "").strip()
        if supplied and supplied != selector_key:
            raise ValueError("selector_key does not match selector identity")
        status = _text(payload, "status").upper()
        if status not in _STATUSES:
            raise ValueError("unsupported selection status")
        issued, renewed, expires = (_integer(payload, n) for n in ("issued_at_ms", "renewed_at_ms", "expires_at_ms"))
        if renewed < issued or expires <= renewed:
            raise ValueError("invalid selection issue/renew/expiry ordering")
        cap = _finite(payload, "notional_cap_usdc")
        assert cap is not None
        if cap < 0 or (status in {"PROBATION", "LIVE"} and cap <= 0):
            raise ValueError("invalid selection notional cap")
        reason = payload.get("demotion_reason")
        reason = str(reason).strip() if reason is not None else None
        demoted = payload.get("demoted_at_ms")
        demoted_at = None if demoted is None else _integer({"demoted_at_ms": demoted}, "demoted_at_ms")
        cooldown = payload.get("cooldown_until_ms")
        cooldown_until = None if cooldown is None else _integer({"cooldown_until_ms": cooldown}, "cooldown_until_ms")
        if status in {"SHADOW", "PROBATION", "LIVE"} and (reason or demoted_at is not None or cooldown_until is not None):
            raise ValueError("active selection cannot carry demotion state")
        if status == "EXPIRED" and (reason != "selector_expired" or demoted_at is None or cooldown_until is not None):
            raise ValueError("expired selection requires selector_expired demotion state")
        if status == "DEMOTED" and (not reason or demoted_at is None or (cooldown_until is not None and cooldown_until <= demoted_at)):
            raise ValueError("invalid demoted selection state")
        snapshot = payload.get("evidence_snapshot", payload.get("evidence_snapshot_json", {}))
        if isinstance(snapshot, str):
            try: snapshot = json.loads(snapshot)
            except json.JSONDecodeError as exc: raise ValueError("evidence_snapshot_json must contain JSON") from exc
        snapshot_json = _json(snapshot)
        snapshot_hash = hashlib.sha256(snapshot_json.encode()).hexdigest()
        supplied_hash = str(payload.get("evidence_snapshot_hash") or "").strip()
        if supplied_hash and supplied_hash != snapshot_hash:
            raise ValueError("evidence_snapshot_hash does not match evidence_snapshot")
        return {
            "selector_key": selector_key, **identity,
            "winner_profile_id": _text(payload, "winner_profile_id"),
            "winner_resolved_profile_hash": _text(payload, "winner_resolved_profile_hash"),
            "generation": generation, "status": status, "notional_cap_usdc": cap,
            "issued_at_ms": issued, "renewed_at_ms": renewed, "expires_at_ms": expires,
            "evidence_revision": _text(payload, "evidence_revision"),
            "evidence_snapshot_hash": snapshot_hash, "evidence_snapshot_json": snapshot_json,
            "policy_hash": _text(payload, "policy_hash"), "owner_id": _text(payload, "owner_id"),
            "boot_id": _text(payload, "boot_id"), "demotion_reason": reason,
            "demoted_at_ms": demoted_at, "cooldown_until_ms": cooldown_until,
            "created_at_ms": created, "updated_at_ms": updated,
        }

    async def get_selection(self, selector: str | Mapping[str, Any] | W6ASelector) -> dict[str, Any] | None:
        key = selector if isinstance(selector, str) else w6a_selector_key(selector)
        return _decode(await self._db.fetchone("SELECT * FROM v1465_w6a_profile_selections WHERE selector_key = ?", (key,)))

    async def _event(self, key: str) -> dict[str, Any] | None:
        return await self._db.fetchone("SELECT * FROM v1465_w6a_profile_selection_events WHERE idempotency_key = ?", (key,))

    async def cas_selection(self, selection: Mapping[str, Any], *, expected_generation: int | None, event_type: str, event_time_ms: int, idempotency_key: str, actor: str, event_payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Create or CAS-replace the one winner, atomically with its audit event."""
        event, key, who = _text({"event_type": event_type}, "event_type").upper(), _text({"idempotency_key": idempotency_key}, "idempotency_key"), _text({"actor": actor}, "actor")
        when = int(event_time_ms)
        if event not in _EVENTS or when < 0:
            raise ValueError("unsupported event type or event_time_ms")
        identity = _identity(selection)
        selector_key = w6a_selector_key(identity)
        request_json = _json({"expected_generation": expected_generation, "selection": dict(selection), "details": dict(event_payload or {})})
        async with self._write_lock:
            started = False
            try:
                await self._db.conn.execute("BEGIN IMMEDIATE"); started = True
                prior_event = await self._event(key)
                if prior_event is not None:
                    if prior_event.get("selector_key") != selector_key or prior_event.get("event_type") != event or prior_event.get("actor") != who or prior_event.get("payload_json") != request_json:
                        raise W6AProfileConflictError("idempotency key reused for a different selection event")
                    row = await self.get_selection(selector_key)
                    if row is None: raise W6AProfilePersistenceError("replayed selection event has no selection")
                    try:
                        prior_request = json.loads(str(prior_event["payload_json"]))
                        prior_selection = prior_request["selection"]
                        generation_after = int(prior_event["generation_after"])
                        created_at_ms = int(
                            prior_selection.get("created_at_ms")
                            or row["created_at_ms"]
                        )
                        replayed = self._normalize_selection(
                            prior_selection,
                            generation=generation_after,
                            created=created_at_ms,
                            updated=int(prior_event["event_time_ms"]),
                        )
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise W6AProfilePersistenceError(
                            "replayed selection event payload is invalid"
                        ) from exc
                    await self._db.conn.rollback(); started = False
                    return _decode(replayed) or {}
                existing = await self._db.fetchone("SELECT * FROM v1465_w6a_profile_selections WHERE selector_key = ?", (selector_key,))
                before = 0 if existing is None else int(existing["generation"])
                if expected_generation is None or int(expected_generation) != before:
                    raise W6ASelectionConflictError("selector generation mismatch")
                normalized = self._normalize_selection(selection, generation=before + 1, created=when if existing is None else int(existing["created_at_ms"]), updated=when)
                columns = tuple(normalized)
                if existing is None:
                    await self._db.conn.execute(f"INSERT INTO v1465_w6a_profile_selections ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", tuple(normalized[c] for c in columns))
                else:
                    updates = tuple(c for c in columns if c not in {"selector_key", "created_at_ms"})
                    cursor = await self._db.conn.execute(f"UPDATE v1465_w6a_profile_selections SET {', '.join(f'{c} = ?' for c in updates)} WHERE selector_key = ? AND generation = ?", (*[normalized[c] for c in updates], selector_key, before))
                    if cursor.rowcount != 1: raise W6ASelectionConflictError("selector generation mismatch")
                await self._db.conn.execute("INSERT INTO v1465_w6a_profile_selection_events (idempotency_key, selector_key, generation_before, generation_after, event_time_ms, event_type, actor, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (key, selector_key, before, before + 1, when, event, who, request_json))
                await self._db.conn.commit(); started = False
                return _decode(normalized) or {}
            except sqlite3.IntegrityError as exc:
                if started: await self._db.conn.rollback()
                raise W6AProfileConflictError(str(exc)) from exc
            except Exception:
                if started: await self._db.conn.rollback()
                raise

    async def grant_selection(self, selection: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        return await self.cas_selection(selection, event_type="GRANTED", **kwargs)

    async def renew_selection(self, selection: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        return await self.cas_selection(selection, event_type="RENEWED", **kwargs)

    async def switch_selection(self, selection: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        return await self.cas_selection(selection, event_type="SWITCHED", **kwargs)

    async def demote_selection(self, selector: str | Mapping[str, Any] | W6ASelector, *, expected_generation: int, reason: str, event_time_ms: int, idempotency_key: str, actor: str, cooldown_until_ms: int | None = None) -> dict[str, Any] | None:
        current = await self.get_selection(selector)
        if current is None: return None
        changed = {**current, "status": "DEMOTED", "demotion_reason": _text({"reason": reason}, "reason"), "demoted_at_ms": int(event_time_ms), "cooldown_until_ms": cooldown_until_ms}
        return await self.cas_selection(changed, expected_generation=expected_generation, event_type="DEMOTED", event_time_ms=event_time_ms, idempotency_key=idempotency_key, actor=actor, event_payload={"reason": reason, "cooldown_until_ms": cooldown_until_ms})

    async def expire_selection(self, selector: str | Mapping[str, Any] | W6ASelector, *, expected_generation: int, now_ms: int, idempotency_key: str, actor: str) -> dict[str, Any] | None:
        current = await self.get_selection(selector)
        if current is None: return None
        if int(now_ms) < int(current["expires_at_ms"]): return current
        changed = {**current, "status": "EXPIRED", "demotion_reason": "selector_expired", "demoted_at_ms": int(now_ms), "cooldown_until_ms": None}
        return await self.cas_selection(changed, expected_generation=expected_generation, event_type="EXPIRED", event_time_ms=now_ms, idempotency_key=idempotency_key, actor=actor, event_payload={"reason": "selector_expired"})

    async def list_selection_events(self, *, selector_key: str, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= int(limit) <= 10_000: raise ValueError("limit must be in [1, 10000]")
        rows = await self._db.fetchall("SELECT * FROM v1465_w6a_profile_selection_events WHERE selector_key = ? ORDER BY event_time_ms, id LIMIT ?", (selector_key, int(limit)))
        return [_decode(row) or {} for row in rows]


__all__ = [
    "V1465W6AProfileRepository", "W6AProfileConflictError",
    "W6AProfilePersistenceError", "W6ASelectionConflictError", "W6ASelector",
    "w6a_selector_key",
]
