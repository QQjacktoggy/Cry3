"""Durable, observational persistence for the v1.4.59 adaptive evidence layer.

The repository only depends on :class:`Database`.  It cannot submit, cancel,
or amend an exchange order; later runtime wiring must remain behind explicit
feature flags.
"""

from __future__ import annotations

from hashlib import sha256
import json
import math
import time

from src.gridbot.storage.database import Database


class AdaptiveEvidenceRepository:
    """Idempotent storage for session snapshots and raw opportunities."""

    _IDENTITY_TEXT_FIELDS = (
        "environment",
        "account_fingerprint",
        "database_identity",
        "exchange_endpoint",
        "symbol",
        "account_mode",
        "deployment_commit",
        "code_version",
        "config_sha256",
    )

    def __init__(self, db: Database) -> None:
        self._db = db

    @staticmethod
    def _required_text(payload: dict, key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} must be a non-empty string")
        return value

    @staticmethod
    def _nonnegative_int(value: object, key: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{key} must be a non-negative integer")
        return value

    @staticmethod
    def _finite_number(value: object, key: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a finite number")
        normalized = float(value)
        if not math.isfinite(normalized):
            raise ValueError(f"{key} must be a finite number")
        return normalized

    @staticmethod
    def _flag(value: object, key: str) -> int:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int) and value in (0, 1):
            return value
        raise ValueError(f"{key} must be a boolean or 0/1")

    @staticmethod
    def _canonical_json(value: object, key: str, expected_type: type) -> str:
        if not isinstance(value, expected_type):
            raise ValueError(f"{key} must be a {expected_type.__name__}")
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{key} must be JSON-serializable") from exc

    async def upsert_session(self, session: dict) -> bool:
        """Insert a new session or replace its mutable snapshot at a newer revision.

        The identity fields and creation timestamp are immutable. A retry with a
        same/lower revision, or an identity mismatch, returns ``False`` without
        changing stored evidence.
        """

        if not isinstance(session, dict):
            raise ValueError("session must be a dict")
        session_id = self._required_text(session, "session_id")
        identity = {
            key: self._required_text(session, key) for key in self._IDENTITY_TEXT_FIELDS
        }
        is_testnet = self._flag(session.get("is_testnet"), "is_testnet")
        status = self._required_text(session, "status")
        revision = self._nonnegative_int(session.get("revision", 0), "revision")
        now_ms = int(time.time() * 1000)
        started_at_ms = self._nonnegative_int(session.get("started_at_ms", now_ms), "started_at_ms")
        checkpoint_at_ms = self._nonnegative_int(
            session.get("last_checkpoint_at_ms", now_ms), "last_checkpoint_at_ms"
        )
        stopped_at_ms = session.get("stopped_at_ms")
        if stopped_at_ms is not None:
            stopped_at_ms = self._nonnegative_int(stopped_at_ms, "stopped_at_ms")
        terminal_runs = self._nonnegative_int(session.get("terminal_runs", 0), "terminal_runs")
        pnl = {
            key: self._finite_number(session.get(key, 0), key)
            for key in (
                "gross_pnl_usdc",
                "commission_usdc",
                "funding_usdc",
                "net_pnl_usdc",
                "high_water_net_pnl_usdc",
            )
        }
        counters_json = self._canonical_json(session.get("counters", {}), "counters", dict)
        disabled_states_json = self._canonical_json(
            session.get("disabled_states", []), "disabled_states", list
        )
        route_stats_json = self._canonical_json(session.get("route_stats", {}), "route_stats", dict)
        rearm_pending = self._flag(session.get("rearm_pending", False), "rearm_pending")
        pause_reason = session.get("pause_reason")
        stop_reason = session.get("stop_reason")
        if pause_reason is not None and not isinstance(pause_reason, str):
            raise ValueError("pause_reason must be a string or None")
        if stop_reason is not None and not isinstance(stop_reason, str):
            raise ValueError("stop_reason must be a string or None")

        cursor = await self._db.execute(
            """INSERT INTO adaptive_sessions (
                session_id, environment, account_fingerprint, database_identity,
                exchange_endpoint, is_testnet, symbol, account_mode,
                deployment_commit, code_version, config_sha256, status,
                started_at_ms, last_checkpoint_at_ms, stopped_at_ms, terminal_runs,
                gross_pnl_usdc, commission_usdc, funding_usdc, net_pnl_usdc,
                high_water_net_pnl_usdc, rearm_pending, pause_reason, stop_reason,
                counters_json, disabled_states_json, route_stats_json, revision,
                created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                status=excluded.status,
                last_checkpoint_at_ms=excluded.last_checkpoint_at_ms,
                stopped_at_ms=excluded.stopped_at_ms,
                terminal_runs=excluded.terminal_runs,
                gross_pnl_usdc=excluded.gross_pnl_usdc,
                commission_usdc=excluded.commission_usdc,
                funding_usdc=excluded.funding_usdc,
                net_pnl_usdc=excluded.net_pnl_usdc,
                high_water_net_pnl_usdc=excluded.high_water_net_pnl_usdc,
                rearm_pending=excluded.rearm_pending,
                pause_reason=excluded.pause_reason,
                stop_reason=excluded.stop_reason,
                counters_json=excluded.counters_json,
                disabled_states_json=excluded.disabled_states_json,
                route_stats_json=excluded.route_stats_json,
                revision=excluded.revision,
                updated_at_ms=excluded.updated_at_ms
            WHERE excluded.revision > adaptive_sessions.revision
              AND adaptive_sessions.environment = excluded.environment
              AND adaptive_sessions.account_fingerprint = excluded.account_fingerprint
              AND adaptive_sessions.database_identity = excluded.database_identity
              AND adaptive_sessions.exchange_endpoint = excluded.exchange_endpoint
              AND adaptive_sessions.is_testnet = excluded.is_testnet
              AND adaptive_sessions.symbol = excluded.symbol
              AND adaptive_sessions.account_mode = excluded.account_mode
              AND adaptive_sessions.deployment_commit = excluded.deployment_commit
              AND adaptive_sessions.code_version = excluded.code_version
              AND adaptive_sessions.config_sha256 = excluded.config_sha256""",
            (
                session_id,
                identity["environment"],
                identity["account_fingerprint"],
                identity["database_identity"],
                identity["exchange_endpoint"],
                is_testnet,
                identity["symbol"],
                identity["account_mode"],
                identity["deployment_commit"],
                identity["code_version"],
                identity["config_sha256"],
                status,
                started_at_ms,
                checkpoint_at_ms,
                stopped_at_ms,
                terminal_runs,
                pnl["gross_pnl_usdc"],
                pnl["commission_usdc"],
                pnl["funding_usdc"],
                pnl["net_pnl_usdc"],
                pnl["high_water_net_pnl_usdc"],
                rearm_pending,
                pause_reason,
                stop_reason,
                counters_json,
                disabled_states_json,
                route_stats_json,
                revision,
                now_ms,
                now_ms,
            ),
        )
        return cursor.rowcount == 1

    async def get_session(self, session_id: str) -> dict | None:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a non-empty string")
        return await self._db.fetchone(
            "SELECT * FROM adaptive_sessions WHERE session_id = ?", (session_id,)
        )

    async def get_open_session(
        self,
        *,
        environment: str,
        account_fingerprint: str,
        database_identity: str,
        symbol: str,
    ) -> dict | None:
        scope = {
            "environment": environment,
            "account_fingerprint": account_fingerprint,
            "database_identity": database_identity,
            "symbol": symbol,
        }
        for key, value in scope.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} must be a non-empty string")
        return await self._db.fetchone(
            """SELECT * FROM adaptive_sessions
            WHERE environment = ? AND account_fingerprint = ?
              AND database_identity = ? AND symbol = ?
              AND status IN ('ACTIVE', 'PAUSED_REQUIRES_ACK')
            ORDER BY last_checkpoint_at_ms DESC, session_id DESC LIMIT 1""",
            (environment, account_fingerprint, database_identity, symbol),
        )

    async def record_opportunity(self, opportunity: dict) -> bool:
        """Record the first immutable raw observation for a session opportunity."""

        if not isinstance(opportunity, dict):
            raise ValueError("opportunity must be a dict")
        now_ms = int(time.time() * 1000)
        session_id = self._required_text(opportunity, "session_id")
        opportunity_id = self._required_text(opportunity, "opportunity_id")
        observed_at_ms = self._nonnegative_int(
            opportunity.get("observed_at_ms", now_ms), "observed_at_ms"
        )
        decision_at_ms = self._nonnegative_int(
            opportunity.get("decision_at_ms"), "decision_at_ms"
        )
        if decision_at_ms > observed_at_ms:
            raise ValueError("decision_at_ms cannot follow observed_at_ms")
        opportunity_bucket = self._nonnegative_int(
            opportunity.get("opportunity_bucket"), "opportunity_bucket"
        )
        required = {
            key: self._required_text(opportunity, key)
            for key in (
                "feature_hash",
                "source_run_id",
                "symbol",
                "side",
                "lane_code",
                "market_state",
                "decision_schema_version",
                "evidence_contract_version",
            )
        }
        outcome_blind = self._flag(
            opportunity.get("outcome_blind"), "outcome_blind"
        )
        if outcome_blind != 1:
            raise ValueError("new opportunity evidence must be outcome_blind")
        reject_reason = opportunity.get("reject_reason")
        promotion_source = opportunity.get("promotion_source")
        if reject_reason is not None and not isinstance(reject_reason, str):
            raise ValueError("reject_reason must be a string or None")
        if promotion_source is not None and not isinstance(promotion_source, str):
            raise ValueError("promotion_source must be a string or None")
        quality_status = opportunity.get("quality_status", "OBSERVED")
        if not isinstance(quality_status, str) or not quality_status:
            raise ValueError("quality_status must be a non-empty string")
        action_schema_json = self._canonical_json(
            opportunity.get("action_schema", {}), "action_schema", dict
        )
        raw_decision_json = self._canonical_json(
            opportunity.get("raw_decision", {}), "raw_decision", dict
        )
        effective_decision_json = self._canonical_json(
            opportunity.get("effective_decision", {}), "effective_decision", dict
        )
        feature_snapshot_json = self._canonical_json(
            opportunity.get("feature_snapshot", {}), "feature_snapshot", dict
        )
        raw_feature_timestamps = opportunity.get("feature_timestamps", {})
        if not isinstance(raw_feature_timestamps, dict):
            raise ValueError("feature_timestamps must be a dict")
        feature_timestamps: dict[str, int] = {}
        for key, value in raw_feature_timestamps.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("feature timestamp keys must be non-empty strings")
            timestamp_ms = self._nonnegative_int(
                value, f"feature_timestamps[{key}]"
            )
            if timestamp_ms > decision_at_ms:
                raise ValueError("feature timestamp cannot follow decision_at_ms")
            feature_timestamps[key] = timestamp_ms
        feature_timestamps_json = self._canonical_json(
            feature_timestamps, "feature_timestamps", dict
        )
        computed_feature_hash = sha256(
            feature_snapshot_json.encode("utf-8")
        ).hexdigest()
        if required["feature_hash"] != computed_feature_hash:
            raise ValueError("feature_hash does not match feature_snapshot")
        cursor = await self._db.execute(
            """INSERT INTO adaptive_opportunities (
                session_id, opportunity_id, observed_at_ms, feature_hash, symbol,
                side, lane_code, market_state, reject_reason, promotion_source,
                decision_schema_version, action_schema_json, raw_decision_json,
                effective_decision_json, quality_status, recorded_at_ms,
                source_run_id, opportunity_bucket, decision_at_ms,
                feature_snapshot_json, feature_timestamps_json,
                evidence_contract_version, outcome_blind
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, opportunity_id) DO NOTHING""",
            (
                session_id,
                opportunity_id,
                observed_at_ms,
                required["feature_hash"],
                required["symbol"],
                required["side"],
                required["lane_code"],
                required["market_state"],
                reject_reason,
                promotion_source,
                required["decision_schema_version"],
                action_schema_json,
                raw_decision_json,
                effective_decision_json,
                quality_status,
                now_ms,
                required["source_run_id"],
                opportunity_bucket,
                decision_at_ms,
                feature_snapshot_json,
                feature_timestamps_json,
                required["evidence_contract_version"],
                outcome_blind,
            ),
        )
        return cursor.rowcount == 1

    async def get_opportunity(self, session_id: str, opportunity_id: str) -> dict | None:
        self._required_text({"session_id": session_id}, "session_id")
        self._required_text({"opportunity_id": opportunity_id}, "opportunity_id")
        return await self._db.fetchone(
            """SELECT * FROM adaptive_opportunities
            WHERE session_id = ? AND opportunity_id = ?""",
            (session_id, opportunity_id),
        )

    async def list_opportunities(
        self,
        session_id: str,
        *,
        quality_status: str | None = None,
        since_ms: int = 0,
        limit: int = 1_000,
    ) -> list[dict]:
        self._required_text({"session_id": session_id}, "session_id")
        since_ms = self._nonnegative_int(since_ms, "since_ms")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit must be an integer from 1 to 10000")
        if quality_status is not None and (not isinstance(quality_status, str) or not quality_status):
            raise ValueError("quality_status must be a non-empty string or None")
        if quality_status is None:
            return await self._db.fetchall(
                """SELECT * FROM adaptive_opportunities
                WHERE session_id = ? AND observed_at_ms >= ?
                ORDER BY observed_at_ms ASC, opportunity_id ASC LIMIT ?""",
                (session_id, since_ms, limit),
            )
        return await self._db.fetchall(
            """SELECT * FROM adaptive_opportunities
            WHERE session_id = ? AND quality_status = ? AND observed_at_ms >= ?
            ORDER BY observed_at_ms ASC, opportunity_id ASC LIMIT ?""",
            (session_id, quality_status, since_ms, limit),
        )

    async def count_opportunities(
        self, session_id: str, *, quality_status: str | None = None
    ) -> int:
        self._required_text({"session_id": session_id}, "session_id")
        if quality_status is None:
            row = await self._db.fetchone(
                "SELECT COUNT(*) AS count FROM adaptive_opportunities WHERE session_id = ?",
                (session_id,),
            )
        else:
            if not isinstance(quality_status, str) or not quality_status:
                raise ValueError("quality_status must be a non-empty string or None")
            row = await self._db.fetchone(
                """SELECT COUNT(*) AS count FROM adaptive_opportunities
                WHERE session_id = ? AND quality_status = ?""",
                (session_id, quality_status),
            )
        return int(row["count"]) if row else 0
