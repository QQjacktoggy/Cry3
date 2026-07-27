"""Process-lifetime fail-closed latch around v1.4.59 manager hooks."""

from __future__ import annotations

from typing import Any, Mapping

from src.gridbot.mainnet.v1459_lifecycle_runtime import (
    V1459LifecycleObservationRuntime,
)
from src.gridbot.mainnet.v1459_manager_hooks import (
    V1459HookResult,
    V1459ManagerObservationHooks,
)


class V1459ManagerObservationGuard:
    """Once evidence safety fails, no later cycle can silently resume."""

    permits_order_mutation = False

    def __init__(
        self, runtime: V1459LifecycleObservationRuntime | None
    ) -> None:
        self._hooks = V1459ManagerObservationHooks(runtime)
        self._blocked: V1459HookResult | None = None

    @property
    def enabled(self) -> bool:
        return self._hooks.enabled

    @property
    def blocked(self) -> bool:
        return self._blocked is not None

    @property
    def blocked_reason(self) -> str | None:
        if self._blocked is None:
            return None
        return self._blocked.reason or self._blocked.status

    @property
    def entry_paused(self) -> bool:
        """Any latched evidence failure forbids a new entry or re-arm."""

        return self._blocked is not None

    @property
    def identity_unsafe(self) -> bool:
        """Identity mismatch forbids every exchange mutation, even exits."""

        return bool(
            self._blocked is not None
            and self._blocked.status == "PAUSED_REQUIRES_ACK"
        )

    @property
    def permits_known_owned_risk_reduction(self) -> bool:
        """Evidence outages may manage a proven-owned run; identity failures may not."""

        return not self.identity_unsafe

    def restored_session(self) -> dict[str, Any] | None:
        return self._hooks.restored_session()

    def _latch(self, result: V1459HookResult) -> V1459HookResult:
        if not result.continue_live and self._blocked is None:
            self._blocked = result
        return result

    def blocked_result(self) -> V1459HookResult | None:
        return self._blocked

    async def checkpoint(
        self,
        session: Mapping[str, Any],
        *,
        checkpoint_at_ms: int,
    ) -> V1459HookResult:
        if self._blocked is not None:
            return self._blocked
        return self._latch(
            await self._hooks.checkpoint(
                session, checkpoint_at_ms=checkpoint_at_ms
            )
        )

    async def retire_durable_session(
        self, *, checkpoint_at_ms: int, stop_reason: str
    ) -> V1459HookResult:
        if self._blocked is not None:
            return self._blocked
        return self._latch(
            await self._hooks.retire_durable_session(
                checkpoint_at_ms=checkpoint_at_ms,
                stop_reason=stop_reason,
            )
        )

    async def record_opportunity(
        self,
        *,
        session_id: str,
        decision_payload: Mapping[str, Any],
        observed_at_ms: int,
    ) -> V1459HookResult:
        if self._blocked is not None:
            return self._blocked
        return self._latch(
            await self._hooks.record_opportunity(
                session_id=session_id,
                decision_payload=decision_payload,
                observed_at_ms=observed_at_ms,
            )
        )


__all__ = ["V1459ManagerObservationGuard"]
