"""Outcome-blind soft selector with bounded threshold adaptation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite

from .contracts import ContractError, Decision, DecisionAction, Opportunity
from .risk_guard import RiskDecision


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    regime_fit: float
    signal_quality: float
    microstructure: float
    execution_quality: float
    exit_economics: float
    uncertainty_penalty: float = 0.0

    def __post_init__(self) -> None:
        bounds = {
            "regime_fit": (0.0, 25.0),
            "signal_quality": (0.0, 25.0),
            "microstructure": (0.0, 20.0),
            "execution_quality": (0.0, 15.0),
            "exit_economics": (0.0, 15.0),
            "uncertainty_penalty": (-15.0, 0.0),
        }
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ContractError(f"{name} must be numeric")
            normalized = float(value)
            if not isfinite(normalized) or not low <= normalized <= high:
                raise ContractError(f"{name} must be in [{low}, {high}]")

    @property
    def total(self) -> float:
        raw = (
            self.regime_fit
            + self.signal_quality
            + self.microstructure
            + self.execution_quality
            + self.exit_economics
            + self.uncertainty_penalty
        )
        return max(0.0, min(100.0, float(raw)))


class AdaptationMode(str, Enum):
    TRAIN = "TRAIN"
    PREQUENTIAL = "PREQUENTIAL"
    FROZEN = "FROZEN"


@dataclass(frozen=True, slots=True)
class ThresholdConfig:
    base: float = 70.0
    minimum: float = 66.0
    maximum: float = 76.0
    max_step_per_epoch: float = 2.0
    target_fills_per_day_low: float = 6.0
    target_fills_per_day_high: float = 10.0

    def __post_init__(self) -> None:
        for name in (
            "base",
            "minimum",
            "maximum",
            "max_step_per_epoch",
            "target_fills_per_day_low",
            "target_fills_per_day_high",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
                raise ContractError(f"{name} must be finite")
        if not 0.0 <= self.minimum <= self.base <= self.maximum <= 100.0:
            raise ContractError("threshold envelope must contain base within 0..100")
        if self.max_step_per_epoch <= 0:
            raise ContractError("max_step_per_epoch must be positive")
        if self.target_fills_per_day_low <= 0 or self.target_fills_per_day_high < self.target_fills_per_day_low:
            raise ContractError("fill-rate target envelope is invalid")


@dataclass(frozen=True, slots=True)
class ThresholdUpdate:
    previous: float
    current: float
    delta: float
    reason: str
    mode: AdaptationMode


def update_threshold(
    *,
    current: float,
    fills_per_day: float,
    prior_epoch_net_pnl_usdc: float,
    guardrail_breaches: int,
    mode: AdaptationMode | str,
    config: ThresholdConfig | None = None,
) -> ThresholdUpdate:
    """Apply one bounded, prior-epoch-only update; frozen evaluation never adapts."""

    config = config or ThresholdConfig()
    mode = AdaptationMode(mode)
    for name, value in (
        ("current", current),
        ("fills_per_day", fills_per_day),
        ("prior_epoch_net_pnl_usdc", prior_epoch_net_pnl_usdc),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(float(value)):
            raise ContractError(f"{name} must be finite")
    if not config.minimum <= current <= config.maximum:
        raise ContractError("current threshold is outside bounded envelope")
    if fills_per_day < 0:
        raise ContractError("fills_per_day must be non-negative")
    if isinstance(guardrail_breaches, bool) or not isinstance(guardrail_breaches, int) or guardrail_breaches < 0:
        raise ContractError("guardrail_breaches must be a non-negative integer")
    if mode is AdaptationMode.FROZEN:
        return ThresholdUpdate(current, current, 0.0, "frozen_evaluation", mode)

    step = float(config.max_step_per_epoch)
    if guardrail_breaches or prior_epoch_net_pnl_usdc < 0:
        proposed = current + step
        reason = "prior_risk_or_negative_net"
    elif fills_per_day < config.target_fills_per_day_low:
        proposed = current - step
        reason = "frequency_below_target"
    elif fills_per_day > config.target_fills_per_day_high:
        proposed = current + step
        reason = "frequency_above_target"
    else:
        proposed = current
        reason = "within_target_envelope"
    bounded = max(config.minimum, min(config.maximum, proposed))
    return ThresholdUpdate(current, bounded, bounded - current, reason, mode)


def select_decision(
    *,
    opportunity: Opportunity,
    decided_at_ms: int,
    risk: RiskDecision,
    score: ScoreBreakdown,
    threshold: float,
    policy_version: str,
    expert_id: str,
    execution_profile_id: str,
    exit_profile_id: str,
) -> Decision:
    """Convert hard safety and soft quality evidence into one paid decision."""

    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ContractError("threshold must be numeric")
    threshold = float(threshold)
    if not 0.0 <= threshold <= 100.0:
        raise ContractError("threshold must be between 0 and 100")
    if not risk.allowed:
        reason = "hard_gate:" + ",".join(gate.value for gate in risk.failed_gates)
        action = DecisionAction.BLOCK
        execution_id = None
        exit_id = None
    elif score.total >= threshold:
        reason = "soft_score_passed"
        action = DecisionAction.ACCEPT
        execution_id = execution_profile_id
        exit_id = exit_profile_id
    else:
        reason = "soft_score_below_threshold"
        action = DecisionAction.SKIP
        execution_id = None
        exit_id = None
    return Decision.create(
        opportunity,
        decided_at_ms=decided_at_ms,
        action=action,
        reason=reason,
        score=score.total,
        threshold=threshold,
        policy_version=policy_version,
        expert_id=expert_id,
        execution_profile_id=execution_id,
        exit_profile_id=exit_id,
    )


__all__ = [
    "AdaptationMode",
    "ScoreBreakdown",
    "ThresholdConfig",
    "ThresholdUpdate",
    "select_decision",
    "update_threshold",
]
