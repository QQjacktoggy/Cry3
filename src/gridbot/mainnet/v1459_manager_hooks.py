"""Fail-closed manager hooks for the orderless v1.4.59 runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from src.gridbot.mainnet.v1459_lifecycle_runtime import (
    V1459LifecycleObservationRuntime,
)
from src.gridbot.mainnet.v1459_observation_composition import (
    validate_observation_coordinator_for_manager,
)


@dataclass(frozen=True)
class V1459HookResult:
    continue_live: bool
    status: str
    reason: str | None = None


class V1459ManagerObservationHooks:
    """Translates persistence outcomes into an explicit continue/stop gate."""

    permits_order_mutation = False

    def __init__(
        self, runtime: V1459LifecycleObservationRuntime | None
    ) -> None:
        self._runtime = validate_observation_coordinator_for_manager(runtime)

    @property
    def enabled(self) -> bool:
        return self._runtime is not None

    def restored_session(self) -> dict[str, Any] | None:
        if self._runtime is None:
            return None
        return self._runtime.durable_session

    async def retire_durable_session(
        self, *, checkpoint_at_ms: int, stop_reason: str
    ) -> V1459HookResult:
        """Close restart-leftover evidence only after the caller found no run."""

        if self._runtime is None:
            return V1459HookResult(True, "DISABLED")
        try:
            write = await self._runtime.retire_durable_session(
                checkpoint_at_ms=checkpoint_at_ms,
                stop_reason=stop_reason,
            )
        except Exception as exc:  # noqa: BLE001 - no durable closure means no new entry
            return V1459HookResult(False, "PERSISTENCE_ERROR", type(exc).__name__)
        if write.status == "PAUSED_REQUIRES_ACK":
            return V1459HookResult(False, write.status, write.reason)
        if write.attempted and not write.inserted and write.reason != "IDEMPOTENT_RETRY":
            return V1459HookResult(False, "CHECKPOINT_NOT_WRITTEN", write.reason)
        return V1459HookResult(True, write.status, write.reason)

    async def checkpoint(
        self,
        session: Mapping[str, Any],
        *,
        checkpoint_at_ms: int,
    ) -> V1459HookResult:
        if self._runtime is None:
            return V1459HookResult(True, "DISABLED")
        try:
            write = await self._runtime.checkpoint_session(
                session, checkpoint_at_ms=checkpoint_at_ms
            )
        except Exception as exc:  # noqa: BLE001 - fail closed at the boundary
            return V1459HookResult(False, "PERSISTENCE_ERROR", type(exc).__name__)
        if write.status == "PAUSED_REQUIRES_ACK":
            return V1459HookResult(False, write.status, write.reason)
        if write.attempted and not write.inserted and write.reason == "IDEMPOTENT_RETRY":
            return V1459HookResult(True, write.status, write.reason)
        if write.attempted and not write.inserted:
            return V1459HookResult(False, "CHECKPOINT_NOT_WRITTEN", write.reason)
        return V1459HookResult(True, write.status, write.reason)

    async def record_opportunity(
        self,
        *,
        session_id: str,
        decision_payload: Mapping[str, Any],
        observed_at_ms: int,
    ) -> V1459HookResult:
        if self._runtime is None:
            return V1459HookResult(True, "DISABLED")
        raw = {
            "accepted": decision_payload.get("raw_classifier_accepted"),
            "reason": decision_payload.get("raw_classifier_reason"),
        }
        route = str(decision_payload.get("live_effective_route") or "")
        effective = {
            "accepted": route not in {"BLOCK", "OBSERVE_ONLY", ""},
            "route": route,
            "enforcement_applied": bool(
                decision_payload.get("enforcement_applied")
            ),
            "gate_reason": decision_payload.get("live_gate_reason"),
        }
        try:
            write = await self._runtime.record_opportunity(
                session_id=session_id,
                decision_payload=decision_payload,
                raw_decision=raw,
                effective_decision=effective,
                observed_at_ms=observed_at_ms,
                symbol=str(decision_payload.get("symbol") or ""),
                side=str(decision_payload.get("side") or ""),
                source_run_id=str(
                    decision_payload.get("source_run_id") or ""
                ),
                opportunity_bucket=decision_payload.get(
                    "opportunity_bucket"
                ),
                decision_at_ms=decision_payload.get(
                    "decision_at_ms", observed_at_ms
                ),
                features=decision_payload.get("observation_features") or {},
                feature_timestamps=(
                    decision_payload.get("observation_feature_timestamps")
                    or {}
                ),
                action_schema=(
                    decision_payload.get("live_effective_action")
                    or decision_payload.get("selected_action")
                    or {}
                ),
                quality_status=str(
                    decision_payload.get("execution_quality") or "OBSERVED"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - no evidence means no submit
            return V1459HookResult(False, "PERSISTENCE_ERROR", type(exc).__name__)
        if write.attempted and not write.inserted:
            # Immutable duplicate evidence is safe: coordinator already proved
            # the stored row matches this retry.
            return V1459HookResult(True, write.status, "IDEMPOTENT_RETRY")
        return V1459HookResult(True, write.status, write.reason)


__all__ = ["V1459HookResult", "V1459ManagerObservationHooks"]
