"""Observation-only persistence coordinator for the v1.4.59 foundation.

The coordinator owns no exchange client and exposes no order operation.  Its
only side effect is writing evidence through the two dedicated repositories.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping, Sequence

from src.gridbot.mainnet.run_reconciler import RunReconciliation, reconcile_run
from src.gridbot.mainnet.shadow_simulator_v3_result import ShadowTradeOutcomeV3
from src.gridbot.mainnet.v1459_observation_contract import (
    ObservationContractError,
    V1459ObservationFlags,
    V1459SessionCheckpoint,
)
from src.gridbot.mainnet.v1459_observation_mapper import (
    reconciliation_parent_payload,
    session_checkpoint_payload,
    shadow_evaluation_payload,
)


@dataclass(frozen=True)
class ObservationWriteResult:
    attempted: bool
    inserted: bool
    status: str
    reason: str | None = None


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ObservationContractError("evidence must be canonical JSON") from exc


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ObservationContractError(f"{key} is required")
    return value.strip()


class V1459ObservationCoordinator:
    """Coordinates durable evidence while remaining incapable of trading."""

    def __init__(self, *, flags: V1459ObservationFlags, evidence_repo: Any, result_repo: Any) -> None:
        if not isinstance(flags, V1459ObservationFlags):
            raise ObservationContractError("formal observation flags are required")
        self.flags = flags
        self.evidence_repo = evidence_repo
        self.result_repo = result_repo

    @property
    def permits_order_mutation(self) -> bool:
        return False

    @staticmethod
    def _stored_session_matches(
        stored: Mapping[str, Any], expected: Mapping[str, Any]
    ) -> bool:
        scalar_keys = (
            "session_id",
            "environment",
            "account_fingerprint",
            "database_identity",
            "exchange_endpoint",
            "is_testnet",
            "symbol",
            "account_mode",
            "deployment_commit",
            "code_version",
            "config_sha256",
            "status",
            "started_at_ms",
            "last_checkpoint_at_ms",
            "stopped_at_ms",
            "terminal_runs",
            "gross_pnl_usdc",
            "commission_usdc",
            "funding_usdc",
            "net_pnl_usdc",
            "high_water_net_pnl_usdc",
            "rearm_pending",
            "pause_reason",
            "stop_reason",
            "revision",
        )
        if any(stored.get(key) != expected.get(key) for key in scalar_keys):
            return False
        json_pairs = (
            ("counters_json", "counters"),
            ("disabled_states_json", "disabled_states"),
            ("route_stats_json", "route_stats"),
        )
        return all(
            stored.get(db_key) == _canonical(expected.get(input_key))
            for db_key, input_key in json_pairs
        )

    async def persist_checkpoint(
        self, checkpoint: V1459SessionCheckpoint
    ) -> ObservationWriteResult:
        if not self.flags.enabled or not self.flags.persist_session:
            return ObservationWriteResult(False, False, "DISABLED")
        payload, identity_reason = session_checkpoint_payload(checkpoint)
        inserted = bool(await self.evidence_repo.upsert_session(payload))
        reason = identity_reason if payload["status"] == "PAUSED_REQUIRES_ACK" else None
        if not inserted:
            stored = await self.evidence_repo.get_session(payload["session_id"])
            if stored is None or not self._stored_session_matches(stored, payload):
                raise ObservationContractError(
                    "conflicting session checkpoint revision"
                )
            if payload["status"] in {"ACTIVE", "STOPPED"}:
                reason = "IDEMPOTENT_RETRY"
        return ObservationWriteResult(
            True,
            inserted,
            payload["status"],
            reason,
        )

    @staticmethod
    def _stored_opportunity_matches(
        stored: Mapping[str, Any], expected: Mapping[str, Any]
    ) -> bool:
        scalar_keys = (
            "session_id",
            "opportunity_id",
            "observed_at_ms",
            "decision_at_ms",
            "source_run_id",
            "opportunity_bucket",
            "feature_hash",
            "evidence_contract_version",
            "outcome_blind",
            "symbol",
            "side",
            "lane_code",
            "market_state",
            "reject_reason",
            "promotion_source",
            "decision_schema_version",
            "quality_status",
        )
        if any(stored.get(key) != expected.get(key) for key in scalar_keys):
            return False
        json_pairs = (
            ("feature_snapshot_json", "feature_snapshot"),
            ("feature_timestamps_json", "feature_timestamps"),
            ("action_schema_json", "action_schema"),
            ("raw_decision_json", "raw_decision"),
            ("effective_decision_json", "effective_decision"),
        )
        return all(stored.get(db_key) == _canonical(expected.get(input_key, {})) for db_key, input_key in json_pairs)

    async def persist_opportunity(
        self, opportunity: Mapping[str, Any]
    ) -> ObservationWriteResult:
        if not self.flags.enabled or not self.flags.record_opportunities:
            return ObservationWriteResult(False, False, "DISABLED")
        if not isinstance(opportunity, Mapping):
            raise ObservationContractError("opportunity must be a mapping")
        payload = dict(opportunity)
        session_id = _required_text(payload, "session_id")
        opportunity_id = _required_text(payload, "opportunity_id")
        inserted = bool(await self.evidence_repo.record_opportunity(payload))
        if not inserted:
            stored = await self.evidence_repo.get_opportunity(session_id, opportunity_id)
            if stored is None or not self._stored_opportunity_matches(stored, payload):
                raise ObservationContractError(
                    "conflicting immutable opportunity evidence"
                )
        accepted = bool((payload.get("effective_decision") or {}).get("accepted"))
        return ObservationWriteResult(
            True,
            inserted,
            "ACCEPTED_OBSERVED" if accepted else "BLOCKED_OBSERVED",
        )

    async def persist_shadow(
        self,
        session_id: str,
        outcome: ShadowTradeOutcomeV3,
        *,
        recorded_at_ms: int,
        extra_input: Mapping[str, Any] | None = None,
    ) -> ObservationWriteResult:
        if not self.flags.enabled or not self.flags.record_shadow:
            return ObservationWriteResult(False, False, "DISABLED")
        payload = shadow_evaluation_payload(
            session_id,
            outcome,
            recorded_at_ms=recorded_at_ms,
            extra_input=extra_input,
        )
        inserted = bool(await self.result_repo.record_shadow_evaluation(payload))
        return ObservationWriteResult(
            True, inserted, str(payload["data_quality"])
        )

    @staticmethod
    def _evidence_ids(
        rows: Sequence[Mapping[str, Any]], key: str
    ) -> tuple[str, ...]:
        if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
            raise ObservationContractError("reconciliation evidence must be sequences")
        values = tuple(sorted(_required_text(row, key) for row in rows))
        if len(values) != len(set(values)):
            raise ObservationContractError(f"duplicate {key}")
        return values

    async def reconcile_and_persist(
        self,
        *,
        trades: Sequence[Mapping[str, Any]],
        incomes: Sequence[Mapping[str, Any]],
        persistence_trades: Sequence[Mapping[str, Any]],
        persistence_incomes: Sequence[Mapping[str, Any]],
        run_id: str,
        reconciliation_revision: int,
        environment: str,
        account_fingerprint: str,
        symbol: str,
        reconciled_at_ms: int,
        source: Mapping[str, Any] | None = None,
    ) -> tuple[RunReconciliation, ObservationWriteResult]:
        # A terminal Live run is only eligible for WR/EV when exchange
        # evidence proves both sides of the round trip. Empty or one-sided
        # history must never become a zero-PnL COMPLETE reconciliation.
        result = reconcile_run(trades, incomes, require_closed_run=True)
        if not self.flags.enabled or not self.flags.record_reconciliation:
            return result, ObservationWriteResult(False, False, "DISABLED")
        if self._evidence_ids(persistence_trades, "exchange_trade_id") != result.exchange_trade_ids:
            raise ObservationContractError("persisted trade IDs differ from reconciliation")
        if self._evidence_ids(persistence_incomes, "exchange_income_id") != result.exchange_income_ids:
            raise ObservationContractError("persisted income IDs differ from reconciliation")
        parent = reconciliation_parent_payload(
            result,
            run_id=run_id,
            reconciliation_revision=reconciliation_revision,
            environment=environment,
            account_fingerprint=account_fingerprint,
            symbol=symbol,
            reconciled_at_ms=reconciled_at_ms,
            source=source,
        )
        inserted = bool(
            await self.result_repo.record_reconciliation(
                parent,
                trades=persistence_trades,
                incomes=persistence_incomes,
            )
        )
        return result, ObservationWriteResult(
            True, inserted, result.reconciliation_status, result.completeness_reason
        )


__all__ = ["ObservationWriteResult", "V1459ObservationCoordinator"]
