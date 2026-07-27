"""Fail-closed, orderless terminal reconciliation hook for v1.4.59."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from src.gridbot.mainnet.v1459_observation_composition import (
    validate_observation_coordinator_for_manager,
)


@dataclass(frozen=True)
class V1459ReconciliationHookResult:
    continue_live: bool
    status: str
    reason: str | None = None
    reconciliation: Any | None = None


class V1459TerminalReconciliationHook:
    """Persist exact exchange settlement evidence before another re-arm.

    The injected runtime is observation-only.  A process-lifetime latch keeps
    an incomplete or unwritten terminal settlement from silently becoming a
    paid next run.  Flags-off remains a zero-I/O no-op.
    """

    permits_order_mutation = False

    def __init__(self, runtime: Any | None) -> None:
        self._runtime = validate_observation_coordinator_for_manager(runtime)
        self._blocked: V1459ReconciliationHookResult | None = None

    @property
    def enabled(self) -> bool:
        if self._runtime is None:
            return False
        flags = getattr(self._runtime, "flags", None)
        return bool(getattr(flags, "record_reconciliation", False))

    @property
    def entry_paused(self) -> bool:
        return self._blocked is not None

    @property
    def blocked_reason(self) -> str | None:
        if self._blocked is None:
            return None
        return self._blocked.reason or self._blocked.status

    def _latch(
        self, result: V1459ReconciliationHookResult
    ) -> V1459ReconciliationHookResult:
        if not result.continue_live and self._blocked is None:
            self._blocked = result
        return self._blocked or result

    def fail_closed(
        self,
        *,
        status: str = "COLLECTION_ERROR",
        reason: str | None = None,
    ) -> V1459ReconciliationHookResult:
        """Latch a pre-persistence collection failure before any re-arm."""

        if self._blocked is not None:
            return self._blocked
        if not self.enabled:
            return V1459ReconciliationHookResult(True, "DISABLED")
        return self._latch(
            V1459ReconciliationHookResult(False, status, reason)
        )

    async def record(
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
    ) -> V1459ReconciliationHookResult:
        if self._blocked is not None:
            return self._blocked
        if not self.enabled:
            return V1459ReconciliationHookResult(True, "DISABLED")
        try:
            reconciliation, write = await self._runtime.record_reconciliation(
                trades=trades,
                incomes=incomes,
                persistence_trades=persistence_trades,
                persistence_incomes=persistence_incomes,
                run_id=run_id,
                reconciliation_revision=reconciliation_revision,
                reconciled_at_ms=reconciled_at_ms,
                source=source,
            )
        except Exception as exc:  # noqa: BLE001 - settlement evidence is a hard boundary
            return self._latch(
                V1459ReconciliationHookResult(
                    False, "PERSISTENCE_ERROR", type(exc).__name__
                )
            )

        if reconciliation.reconciliation_status != "COMPLETE":
            return self._latch(
                V1459ReconciliationHookResult(
                    False,
                    "RECONCILIATION_INCOMPLETE",
                    reconciliation.completeness_reason,
                    reconciliation,
                )
            )
        if not write.attempted:
            return self._latch(
                V1459ReconciliationHookResult(
                    False,
                    "RECONCILIATION_NOT_WRITTEN",
                    write.reason,
                    reconciliation,
                )
            )
        if not write.inserted:
            # The immutable repository only returns False for an exact retry;
            # conflicting revisions raise before this boundary.
            return V1459ReconciliationHookResult(
                True, write.status, "IDEMPOTENT_RETRY", reconciliation
            )
        return V1459ReconciliationHookResult(
            True, write.status, write.reason, reconciliation
        )


__all__ = [
    "V1459ReconciliationHookResult",
    "V1459TerminalReconciliationHook",
]
