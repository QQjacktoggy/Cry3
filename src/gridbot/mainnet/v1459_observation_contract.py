"""Fail-closed contracts for the v1.4.59 observation-only runtime.

These values describe evidence collection only.  They never authorise an
exchange request, order mutation, Telegram action, or risk-policy change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from src.gridbot.mainnet.runtime_identity import RuntimeIdentity

OPPORTUNITY_EVIDENCE_CONTRACT_VERSION = "v1459-opportunity-evidence-v2"


class ObservationContractError(ValueError):
    """Raised when observation evidence is incomplete or contradictory."""


@dataclass(frozen=True)
class V1459ObservationFlags:
    """Explicit feature flags; child flags cannot escape a disabled parent."""

    enabled: bool = False
    persist_session: bool = False
    record_opportunities: bool = False
    record_shadow: bool = False
    record_reconciliation: bool = False

    def __post_init__(self) -> None:
        values = (
            self.enabled,
            self.persist_session,
            self.record_opportunities,
            self.record_shadow,
            self.record_reconciliation,
        )
        if any(not isinstance(value, bool) for value in values):
            raise ObservationContractError("observation flags must be booleans")
        if not self.enabled and any(values[1:]):
            raise ObservationContractError(
                "observation child flags require the parent flag"
            )

    @property
    def permits_order_mutation(self) -> bool:
        return False


@dataclass(frozen=True)
class V1459SessionCheckpoint:
    """One revisioned, durable session snapshot."""

    session_id: str
    expected_identity: RuntimeIdentity
    observed_identity: RuntimeIdentity
    code_version: str
    revision: int
    started_at_ms: int
    checkpoint_at_ms: int
    terminal_runs: int = 0
    gross_pnl_usdc: float = 0.0
    commission_usdc: float = 0.0
    funding_usdc: float = 0.0
    net_pnl_usdc: float = 0.0
    high_water_net_pnl_usdc: float = 0.0
    rearm_pending: bool = False
    counters: Mapping[str, Any] = field(default_factory=dict)
    disabled_states: tuple[str, ...] = ()
    route_stats: Mapping[str, Any] = field(default_factory=dict)
    stopped_at_ms: int | None = None
    stop_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ObservationContractError("session_id is required")
        if not isinstance(self.code_version, str) or not self.code_version.strip():
            raise ObservationContractError("code_version is required")
        for name in (
            "revision",
            "started_at_ms",
            "checkpoint_at_ms",
            "terminal_runs",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ObservationContractError(f"{name} must be non-negative")
        if self.checkpoint_at_ms < self.started_at_ms:
            raise ObservationContractError("checkpoint cannot precede session start")
        if self.stopped_at_ms is not None and (
            isinstance(self.stopped_at_ms, bool)
            or not isinstance(self.stopped_at_ms, int)
            or self.stopped_at_ms < self.started_at_ms
        ):
            raise ObservationContractError("stopped_at_ms is invalid")
        if not isinstance(self.rearm_pending, bool):
            raise ObservationContractError("rearm_pending must be boolean")
        if not isinstance(self.counters, Mapping) or not isinstance(
            self.route_stats, Mapping
        ):
            raise ObservationContractError("counter and route stats must be mappings")
        if any(not isinstance(value, str) or not value for value in self.disabled_states):
            raise ObservationContractError("disabled states must be non-empty strings")


__all__ = [
    "OPPORTUNITY_EVIDENCE_CONTRACT_VERSION",
    "ObservationContractError",
    "V1459ObservationFlags",
    "V1459SessionCheckpoint",
]
