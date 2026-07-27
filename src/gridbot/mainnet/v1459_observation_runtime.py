"""Runtime boundary for v1.4.59 observation-only evidence.

The adapter deliberately receives only the persistence coordinator.  It owns
no exchange, Telegram, or order client and therefore cannot mutate trading
state.  Callers must pass already-observed runtime identity and decisions.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Mapping, Sequence

from src.gridbot.mainnet.runtime_identity import RuntimeIdentity
from src.gridbot.mainnet.shadow_simulator_v3_result import ShadowTradeOutcomeV3
from src.gridbot.mainnet.v1459_observation_contract import (
    OPPORTUNITY_EVIDENCE_CONTRACT_VERSION,
    ObservationContractError,
    V1459SessionCheckpoint,
)
from src.gridbot.mainnet.v1459_observation_coordinator import (
    ObservationWriteResult,
    V1459ObservationCoordinator,
)


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ObservationContractError(f"{name} is required")
    return value.strip()


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ObservationContractError(f"{name} must be non-negative")
    return value


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ObservationContractError(f"{name} must be a mapping")
    return value


def _canonical_hash(value: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ObservationContractError("features must be canonical JSON") from exc
    return sha256(encoded).hexdigest()


@dataclass(frozen=True)
class V1459RuntimeContext:
    """Immutable, explicit identity inputs for durable checkpoints."""

    expected_identity: RuntimeIdentity
    observed_identity: RuntimeIdentity
    code_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.expected_identity, RuntimeIdentity):
            raise ObservationContractError("expected RuntimeIdentity is required")
        if not isinstance(self.observed_identity, RuntimeIdentity):
            raise ObservationContractError("observed RuntimeIdentity is required")
        _required_text(self.code_version, "code_version")


class V1459ObservationRuntime:
    """Small, serialised persistence facade with no trading capabilities."""

    permits_order_mutation = False

    def __init__(
        self,
        *,
        coordinator: V1459ObservationCoordinator,
        context: V1459RuntimeContext,
    ) -> None:
        if not isinstance(coordinator, V1459ObservationCoordinator):
            raise ObservationContractError("V1459ObservationCoordinator is required")
        if not isinstance(context, V1459RuntimeContext):
            raise ObservationContractError("V1459RuntimeContext is required")
        if coordinator.permits_order_mutation is not False:
            raise ObservationContractError("coordinator must not permit orders")
        self._coordinator = coordinator
        self._context = context
        self._session_revisions: dict[str, int] = {}
        self._checkpoint_lock = asyncio.Lock()

    @property
    def flags(self):
        return self._coordinator.flags

    async def checkpoint_session(
        self,
        session: Mapping[str, Any],
        *,
        checkpoint_at_ms: int,
    ) -> ObservationWriteResult:
        """Persist one monotonic revision after validating the live snapshot."""

        snapshot = _mapping(session, "session")
        session_id = _required_text(snapshot.get("session_id"), "session_id")
        checkpoint_at_ms = _non_negative_int(
            checkpoint_at_ms, "checkpoint_at_ms"
        )
        async with self._checkpoint_lock:
            revision = self._session_revisions.get(session_id, -1) + 1
            counters = dict(_mapping(snapshot.get("counters", {}), "counters"))
            route_stats = dict(
                _mapping(
                    snapshot.get("route_stats", counters.get("route_state_action_pnl", {})),
                    "route_stats",
                )
            )
            gross = float(
                snapshot.get("gross_pnl_usdc", counters.get("gross_pnl_usdc", 0.0))
                or 0.0
            )
            commission = float(
                snapshot.get("commission_usdc", counters.get("commission_usdc", 0.0))
                or 0.0
            )
            funding = float(
                snapshot.get("funding_usdc", counters.get("funding_usdc", 0.0))
                or 0.0
            )
            checkpoint = V1459SessionCheckpoint(
                session_id=session_id,
                expected_identity=self._context.expected_identity,
                observed_identity=self._context.observed_identity,
                code_version=self._context.code_version,
                revision=revision,
                started_at_ms=_non_negative_int(
                    snapshot.get("started_at_ms"), "started_at_ms"
                ),
                checkpoint_at_ms=checkpoint_at_ms,
                terminal_runs=_non_negative_int(
                    int(snapshot.get("terminal_runs", 0) or 0), "terminal_runs"
                ),
                gross_pnl_usdc=gross,
                commission_usdc=commission,
                funding_usdc=funding,
                net_pnl_usdc=float(snapshot.get("net_pnl_usdc", 0.0) or 0.0),
                high_water_net_pnl_usdc=float(
                    snapshot.get("high_water_net_pnl_usdc", 0.0) or 0.0
                ),
                rearm_pending=bool(snapshot.get("rearm_enabled", False)),
                counters=counters,
                disabled_states=tuple(
                    sorted(str(value) for value in snapshot.get("disabled_states", ()))
                ),
                route_stats=route_stats,
                stopped_at_ms=snapshot.get("stopped_at_ms"),
                stop_reason=snapshot.get("stop_reason"),
            )
            result = await self._coordinator.persist_checkpoint(checkpoint)
            if result.attempted:
                self._session_revisions[session_id] = revision
            return result

    async def record_opportunity(
        self,
        *,
        session_id: str,
        decision_payload: Mapping[str, Any],
        raw_decision: Mapping[str, Any],
        effective_decision: Mapping[str, Any],
        observed_at_ms: int,
        symbol: str,
        side: str,
        source_run_id: str,
        opportunity_bucket: int,
        decision_at_ms: int,
        features: Mapping[str, Any],
        feature_timestamps: Mapping[str, Any] | None = None,
        action_schema: Mapping[str, Any] | None = None,
        quality_status: str = "OBSERVED",
    ) -> ObservationWriteResult:
        """Persist one immutable accepted or blocked decision opportunity."""

        decision = _mapping(decision_payload, "decision_payload")
        raw = _mapping(raw_decision, "raw_decision")
        effective = _mapping(effective_decision, "effective_decision")
        feature_map = _mapping(features, "features")
        observed_at_ms = _non_negative_int(observed_at_ms, "observed_at_ms")
        decision_at_ms = _non_negative_int(decision_at_ms, "decision_at_ms")
        if decision_at_ms > observed_at_ms:
            raise ObservationContractError(
                "decision_at_ms cannot follow observed_at_ms"
            )
        raw_timestamps = _mapping(
            feature_timestamps or {}, "feature_timestamps"
        )
        timestamp_map: dict[str, int] = {}
        for key, value in raw_timestamps.items():
            if not isinstance(key, str) or not key.strip():
                raise ObservationContractError(
                    "feature timestamp keys must be non-empty"
                )
            timestamp_ms = _non_negative_int(value, f"feature_timestamps[{key}]")
            if timestamp_ms > decision_at_ms:
                raise ObservationContractError(
                    "feature timestamp cannot follow decision_at_ms"
                )
            timestamp_map[key] = timestamp_ms
        opportunity = {
            "session_id": _required_text(session_id, "session_id"),
            "opportunity_id": _required_text(
                decision.get("opportunity_id"), "opportunity_id"
            ),
            "observed_at_ms": observed_at_ms,
            "decision_at_ms": decision_at_ms,
            "source_run_id": _required_text(source_run_id, "source_run_id"),
            "opportunity_bucket": _non_negative_int(
                opportunity_bucket, "opportunity_bucket"
            ),
            "feature_hash": _canonical_hash(feature_map),
            "feature_snapshot": dict(feature_map),
            "feature_timestamps": dict(timestamp_map),
            "evidence_contract_version": OPPORTUNITY_EVIDENCE_CONTRACT_VERSION,
            "outcome_blind": True,
            "symbol": _required_text(symbol, "symbol"),
            "side": _required_text(side, "side"),
            "lane_code": str(decision.get("lane_code") or "UNKNOWN"),
            "market_state": str(decision.get("market_state") or "UNKNOWN"),
            "reject_reason": raw.get("reason"),
            "promotion_source": decision.get("promotion_source"),
            "decision_schema_version": self._context.code_version,
            "action_schema": dict(
                action_schema
                or decision.get("live_effective_action")
                or decision.get("selected_action")
                or {}
            ),
            "raw_decision": dict(raw),
            "effective_decision": dict(effective),
            "quality_status": _required_text(quality_status, "quality_status"),
            "recorded_at_ms": observed_at_ms,
        }
        return await self._coordinator.persist_opportunity(opportunity)

    async def record_shadow(
        self,
        *,
        session_id: str,
        outcome: ShadowTradeOutcomeV3,
        recorded_at_ms: int,
        extra_input: Mapping[str, Any] | None = None,
    ) -> ObservationWriteResult:
        return await self._coordinator.persist_shadow(
            session_id,
            outcome,
            recorded_at_ms=recorded_at_ms,
            extra_input=extra_input,
        )

    async def record_reconciliation(
        self,
        *,
        trades: Sequence[Mapping[str, Any]],
        incomes: Sequence[Mapping[str, Any]],
        persistence_trades: Sequence[Mapping[str, Any]],
        persistence_incomes: Sequence[Mapping[str, Any]],
        run_id: str,
        reconciliation_revision: int,
        reconciled_at_ms: int,
        source: Mapping[str, Any] | None = None,
    ):
        identity = self._context.observed_identity
        return await self._coordinator.reconcile_and_persist(
            trades=trades,
            incomes=incomes,
            persistence_trades=persistence_trades,
            persistence_incomes=persistence_incomes,
            run_id=run_id,
            reconciliation_revision=reconciliation_revision,
            environment=identity.environment,
            account_fingerprint=identity.account_fingerprint,
            symbol=identity.symbol,
            reconciled_at_ms=reconciled_at_ms,
            source=source,
        )


__all__ = ["V1459ObservationRuntime", "V1459RuntimeContext"]
