"""Outcome-blind expert assessments for the bounded Live Next registry."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Mapping

from .contracts import ContractError, Side
from .features import FeatureSnapshot
from .regime_state import RegimeState
from .selector import ScoreBreakdown


SUPPORTED_EXPERTS = frozenset(
    {"impulse_retest", "trend_pullback", "range_reclaim", "shock_exhaustion"}
)
_FATAL_QUALITY_FLAGS = frozenset(
    {"data_gap", "incomplete", "stale", "timestamp_inversion"}
)


@dataclass(frozen=True, slots=True)
class ExpertAssessment:
    family: str
    eligible: bool
    side: Side | None
    anchor_event_id: str
    reason: str
    score: ScoreBreakdown

    def __post_init__(self) -> None:
        if self.family not in SUPPORTED_EXPERTS:
            raise ContractError("unsupported expert family")
        if not isinstance(self.eligible, bool):
            raise ContractError("eligible must be boolean")
        if self.eligible and self.side is None:
            raise ContractError("eligible expert assessment requires a side")
        if self.side is not None:
            object.__setattr__(self, "side", Side(self.side))
        if not isinstance(self.anchor_event_id, str) or not self.anchor_event_id:
            raise ContractError("anchor_event_id is required")
        if not isinstance(self.reason, str) or not self.reason:
            raise ContractError("assessment reason is required")


def _number(values: Mapping[str, Any], name: str) -> float:
    value = values.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"missing or non-numeric expert feature: {name}")
    result = float(value)
    if not isfinite(result):
        raise ContractError(f"expert feature must be finite: {name}")
    return result


def _parameter(parameters: Mapping[str, Any], name: str) -> float:
    value = parameters.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"missing or non-numeric expert parameter: {name}")
    result = float(value)
    if not isfinite(result):
        raise ContractError(f"expert parameter must be finite: {name}")
    return result


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def _ratio_score(value: float, minimum: float) -> float:
    if minimum >= 1.0:
        return 1.0 if value >= minimum else 0.0
    return _clamp((value - 0.5) / max(1e-9, 1.0 - 0.5))


def _state_name(state: RegimeState) -> str:
    if not isinstance(state, RegimeState):
        raise ContractError("expert registry requires a RegimeState")
    return state.state_name


def _anchor(values: Mapping[str, Any], snapshot: FeatureSnapshot) -> str:
    value = values.get("anchor_event_id", snapshot.feature_snapshot_id)
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ContractError("anchor_event_id must be a string or integer")
    result = str(value)
    if not result:
        raise ContractError("anchor_event_id cannot be empty")
    return result


def _ineligible(
    family: str,
    anchor: str,
    reason: str,
    *,
    confidence: float,
) -> ExpertAssessment:
    return ExpertAssessment(
        family=family,
        eligible=False,
        side=None,
        anchor_event_id=anchor,
        reason=reason,
        score=ScoreBreakdown(
            regime_fit=25.0 * _clamp(confidence),
            signal_quality=0.0,
            microstructure=0.0,
            execution_quality=0.0,
            exit_economics=0.0,
            uncertainty_penalty=-15.0,
        ),
    )


def evaluate_expert(
    *,
    family: str,
    snapshot: FeatureSnapshot,
    regime_state: RegimeState,
    parameters: Mapping[str, Any],
) -> ExpertAssessment:
    """Evaluate one fixed expert without reading any outcome or future label."""

    if family not in SUPPORTED_EXPERTS:
        raise ContractError(f"unsupported expert family: {family}")
    if not isinstance(snapshot, FeatureSnapshot):
        raise ContractError("expert registry requires a FeatureSnapshot")
    snapshot.assert_usable_at(snapshot.decision_time_ms)
    values = snapshot.values
    anchor = _anchor(values, snapshot)
    state_name = _state_name(regime_state)
    confidence = _clamp(float(regime_state.confidence))
    fatal_flags = _FATAL_QUALITY_FLAGS.intersection(snapshot.quality_flags)
    if fatal_flags:
        return _ineligible(
            family,
            anchor,
            "fatal_quality:" + ",".join(sorted(fatal_flags)),
            confidence=confidence,
        )

    expected_regime = {
        "impulse_retest": "TREND_",
        "trend_pullback": "TREND_",
        "range_reclaim": "RANGE",
        "shock_exhaustion": "SHOCK_",
    }[family]
    regime_match = (
        state_name.startswith(expected_regime)
        if expected_regime.endswith("_")
        else state_name == expected_regime
    )
    if not regime_match:
        return _ineligible(
            family,
            anchor,
            f"regime_mismatch:{state_name}",
            confidence=confidence,
        )

    if family == "impulse_retest":
        move = _number(values, "move_3s_bps")
        retrace = _number(values, "retrace_fraction")
        flow = _number(values, "impulse_flow_ratio")
        minimum_move = _parameter(parameters, "min_impulse_bps")
        minimum_flow = _parameter(parameters, "min_impulse_flow_ratio")
        minimum_retrace = _parameter(parameters, "min_retrace_fraction")
        maximum_retrace = _parameter(parameters, "max_retrace_fraction")
        side = Side.LONG if move > 0 else Side.SHORT
        structural = (
            abs(move) >= minimum_move
            and minimum_retrace <= retrace <= maximum_retrace
            and flow >= minimum_flow
        )
        signal_strength = (
            _clamp(abs(move) / minimum_move - 0.5)
            + _clamp((retrace - minimum_retrace) / max(1e-9, maximum_retrace - minimum_retrace))
        ) / 2.0
        flow_strength = _ratio_score(flow, minimum_flow)
    elif family == "trend_pullback":
        move = _number(values, "move_30s_bps")
        resume = _number(values, "move_2s_bps")
        retrace = _number(values, "pullback_fraction")
        flow = _number(values, "trend_flow_ratio")
        minimum_move = _parameter(parameters, "min_trend_bps")
        minimum_flow = _parameter(parameters, "min_resume_flow_ratio")
        minimum_retrace = _parameter(parameters, "min_retrace_fraction")
        maximum_retrace = _parameter(parameters, "max_retrace_fraction")
        side = Side.LONG if move > 0 else Side.SHORT
        resumed = resume > 0 if side is Side.LONG else resume < 0
        structural = (
            abs(move) >= minimum_move
            and resumed
            and minimum_retrace <= retrace <= maximum_retrace
            and flow >= minimum_flow
        )
        signal_strength = (
            _clamp(abs(move) / minimum_move - 0.5)
            + _clamp(abs(resume) / max(1.0, minimum_move / 4.0))
        ) / 2.0
        flow_strength = _ratio_score(flow, minimum_flow)
    elif family == "range_reclaim":
        position = _number(values, "range_position_60s")
        false_break = _number(values, "false_break_bps")
        reclaim = _number(values, "reclaim_bps")
        inward_move = _number(values, "move_2s_bps")
        flow = _number(values, "range_inward_flow_ratio")
        boundary = _parameter(parameters, "boundary_fraction")
        minimum_boundary_reversal = _parameter(parameters, "min_boundary_reversal_bps")
        minimum_break = _parameter(parameters, "min_false_break_bps")
        minimum_reclaim = _parameter(parameters, "min_reclaim_bps")
        minimum_flow = _parameter(parameters, "min_reversal_flow_ratio")
        lower = position <= boundary
        upper = position >= 1.0 - boundary
        side = Side.LONG if lower else Side.SHORT
        inward = (lower and inward_move > 0) or (upper and inward_move < 0)
        failed_break_path = (
            false_break >= minimum_break and reclaim >= minimum_reclaim
        )
        boundary_reversal_path = (
            inward and abs(inward_move) >= minimum_boundary_reversal
        )
        structural = (
            (lower or upper)
            and flow >= minimum_flow
            and (failed_break_path or boundary_reversal_path)
        )
        signal_strength = (
            _clamp(max(false_break / minimum_break, abs(inward_move) / minimum_boundary_reversal) - 0.5)
            + _clamp(max(reclaim / minimum_reclaim, abs(inward_move) / minimum_boundary_reversal) - 0.5)
        ) / 2.0
        flow_strength = _ratio_score(flow, minimum_flow)
    else:
        shock_move = _number(values, "move_2s_bps")
        retrace = _number(values, "retrace_fraction")
        flow = _number(values, "shock_reversal_flow_ratio")
        minimum_move = _parameter(parameters, "min_shock_bps")
        minimum_flow = _parameter(parameters, "min_reversal_flow_ratio")
        minimum_retrace = _parameter(parameters, "min_retrace_fraction")
        maximum_retrace = _parameter(parameters, "max_retrace_fraction")
        side = Side.SHORT if shock_move > 0 else Side.LONG
        structural = (
            abs(shock_move) >= minimum_move
            and minimum_retrace <= retrace <= maximum_retrace
            and flow >= minimum_flow
        )
        signal_strength = (
            _clamp(abs(shock_move) / minimum_move - 0.5)
            + _clamp((retrace - minimum_retrace) / max(1e-9, maximum_retrace - minimum_retrace))
        ) / 2.0
        flow_strength = _ratio_score(flow, minimum_flow)

    if not structural:
        return _ineligible(
            family,
            anchor,
            "structural_minimum_not_met",
            confidence=confidence,
        )
    execution_quality = _clamp(float(values.get("execution_quality", 0.60)))
    exit_economics = _clamp(float(values.get("exit_economics", 0.60)))
    optional_missing = sum(
        name not in values for name in ("execution_quality", "exit_economics")
    )
    uncertainty = min(
        15.0,
        (1.0 - confidence) * 5.0
        + len(snapshot.quality_flags) * 3.0
        + optional_missing * 2.0,
    )
    score = ScoreBreakdown(
        regime_fit=25.0 * confidence,
        signal_quality=25.0 * signal_strength,
        microstructure=20.0 * flow_strength,
        execution_quality=15.0 * execution_quality,
        exit_economics=15.0 * exit_economics,
        uncertainty_penalty=-uncertainty,
    )
    return ExpertAssessment(
        family=family,
        eligible=True,
        side=side,
        anchor_event_id=anchor,
        reason="structural_minimum_met",
        score=score,
    )


__all__ = [
    "ExpertAssessment",
    "SUPPORTED_EXPERTS",
    "evaluate_expert",
]
