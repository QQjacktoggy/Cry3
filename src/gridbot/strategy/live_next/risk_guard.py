"""Deterministic pre-entry hard gates for Live Next.

Only catastrophic safety/integrity conditions belong here.  Strategy quality
and ordinary market preferences remain soft selector inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite

from .contracts import ContractError


class HardGate(str, Enum):
    OWNERSHIP_FLATNESS = "OWNERSHIP_FLATNESS"
    DATA_FRESHNESS_CAUSALITY = "DATA_FRESHNESS_CAUSALITY"
    EXTREME_COST_LATENCY = "EXTREME_COST_LATENCY"
    LOSS_DRAWDOWN_BREAKER = "LOSS_DRAWDOWN_BREAKER"
    POSITION_ORDER_CONSISTENCY = "POSITION_ORDER_CONSISTENCY"


@dataclass(frozen=True, slots=True)
class RiskSnapshot:
    ownership_ok: bool
    account_flat_for_entry: bool
    data_age_ms: int
    max_data_age_ms: int
    causality_ok: bool
    predicted_all_in_cost_bps: float
    max_all_in_cost_bps: float
    observed_latency_ms: int
    max_latency_ms: int
    session_net_pnl_usdc: float
    max_session_loss_usdc: float
    session_drawdown_usdc: float
    max_session_drawdown_usdc: float
    position_count: int
    working_entry_order_count: int
    position_order_consistent: bool

    def __post_init__(self) -> None:
        for name in (
            "ownership_ok",
            "account_flat_for_entry",
            "causality_ok",
            "position_order_consistent",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ContractError(f"{name} must be boolean")
        for name in (
            "data_age_ms",
            "max_data_age_ms",
            "observed_latency_ms",
            "max_latency_ms",
            "position_count",
            "working_entry_order_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractError(f"{name} must be a non-negative integer")
        for name in (
            "predicted_all_in_cost_bps",
            "max_all_in_cost_bps",
            "session_net_pnl_usdc",
            "max_session_loss_usdc",
            "session_drawdown_usdc",
            "max_session_drawdown_usdc",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ContractError(f"{name} must be numeric")
            if not isfinite(float(value)):
                raise ContractError(f"{name} must be finite")
        for name in (
            "predicted_all_in_cost_bps",
            "max_all_in_cost_bps",
            "max_session_loss_usdc",
            "session_drawdown_usdc",
            "max_session_drawdown_usdc",
        ):
            if float(getattr(self, name)) < 0:
                raise ContractError(f"{name} must be non-negative")


@dataclass(frozen=True, slots=True)
class RiskDecision:
    allowed: bool
    failed_gates: tuple[HardGate, ...]
    reasons: tuple[str, ...]

    @property
    def permits_order_mutation(self) -> bool:
        return self.allowed


def evaluate_hard_gates(snapshot: RiskSnapshot) -> RiskDecision:
    failures: list[HardGate] = []
    reasons: list[str] = []
    if not snapshot.ownership_ok or not snapshot.account_flat_for_entry:
        failures.append(HardGate.OWNERSHIP_FLATNESS)
        reasons.append("ownership_not_proven_or_account_not_flat")
    if not snapshot.causality_ok or snapshot.data_age_ms > snapshot.max_data_age_ms:
        failures.append(HardGate.DATA_FRESHNESS_CAUSALITY)
        reasons.append("data_stale_or_non_causal")
    if (
        snapshot.predicted_all_in_cost_bps > snapshot.max_all_in_cost_bps
        or snapshot.observed_latency_ms > snapshot.max_latency_ms
    ):
        failures.append(HardGate.EXTREME_COST_LATENCY)
        reasons.append("cost_or_latency_extreme")
    if (
        snapshot.session_net_pnl_usdc <= -snapshot.max_session_loss_usdc
        or snapshot.session_drawdown_usdc >= snapshot.max_session_drawdown_usdc
    ):
        failures.append(HardGate.LOSS_DRAWDOWN_BREAKER)
        reasons.append("session_loss_or_drawdown_breaker")
    if (
        not snapshot.position_order_consistent
        or snapshot.position_count > 1
        or snapshot.working_entry_order_count > 1
    ):
        failures.append(HardGate.POSITION_ORDER_CONSISTENCY)
        reasons.append("position_or_order_state_inconsistent")
    return RiskDecision(
        allowed=not failures,
        failed_gates=tuple(failures),
        reasons=tuple(reasons),
    )


__all__ = [
    "HardGate",
    "RiskDecision",
    "RiskSnapshot",
    "evaluate_hard_gates",
]
