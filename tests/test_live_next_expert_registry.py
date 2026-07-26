import pytest

from scripts.freeze_live_next_research import EXPERT_PARAMETER_SETS
from src.gridbot.strategy.live_next.contracts import Side
from src.gridbot.strategy.live_next.expert_registry import evaluate_expert
from src.gridbot.strategy.live_next.features import (
    FeatureSnapshot,
    OutcomeLeakageError,
)
from src.gridbot.strategy.live_next.regime_state import RegimeState


def _snapshot(**changes):
    values = {
        "anchor_event_id": "agg-10",
        "execution_quality": 0.75,
        "exit_economics": 0.80,
    }
    values.update(changes)
    return FeatureSnapshot.create(
        observed_at_ms=1_000,
        market_data_max_event_ms=999,
        values=values,
        feature_version="live_next.features.v1",
    )


def _state(regime, direction="NONE", confidence=0.9):
    return RegimeState(
        regime=regime,
        direction=direction,
        confidence=confidence,
        since_ms=500,
        last_decision_time_ms=1_000,
        last_event_time_ms=999,
        last_available_at_ms=1_000,
    )


@pytest.mark.parametrize(
    ("family", "snapshot", "state", "side"),
    [
        (
            "impulse_retest",
            _snapshot(
                move_3s_bps=15.0,
                retrace_fraction=0.35,
                impulse_flow_ratio=0.65,
            ),
            _state("TREND", "UP"),
            Side.LONG,
        ),
        (
            "trend_pullback",
            _snapshot(
                move_30s_bps=-25.0,
                move_2s_bps=-4.0,
                pullback_fraction=0.30,
                trend_flow_ratio=0.63,
            ),
            _state("TREND", "DOWN"),
            Side.SHORT,
        ),
        (
            "range_reclaim",
            _snapshot(
                range_position_60s=0.05,
                move_2s_bps=1.5,
                false_break_bps=5.0,
                reclaim_bps=4.0,
                range_inward_flow_ratio=0.62,
            ),
            _state("RANGE"),
            Side.LONG,
        ),
        (
            "shock_exhaustion",
            _snapshot(
                move_2s_bps=30.0,
                retrace_fraction=0.30,
                shock_reversal_flow_ratio=0.65,
            ),
            _state("SHOCK", "UP"),
            Side.SHORT,
        ),
    ],
)
def test_four_experts_are_deterministic_and_score_within_bounds(
    family, snapshot, state, side
):
    first = evaluate_expert(
        family=family,
        snapshot=snapshot,
        regime_state=state,
        parameters=EXPERT_PARAMETER_SETS[family],
    )
    second = evaluate_expert(
        family=family,
        snapshot=snapshot,
        regime_state=state,
        parameters=EXPERT_PARAMETER_SETS[family],
    )

    assert first == second
    assert first.eligible
    assert first.side is side
    assert 0.0 <= first.score.total <= 100.0
    assert first.score.uncertainty_penalty <= 0.0


def test_regime_mismatch_and_structural_minimum_fail_closed():
    mismatch = evaluate_expert(
        family="impulse_retest",
        snapshot=_snapshot(
            move_3s_bps=20.0,
            retrace_fraction=0.30,
            impulse_flow_ratio=0.70,
        ),
        regime_state=_state("RANGE"),
        parameters=EXPERT_PARAMETER_SETS["impulse_retest"],
    )
    weak = evaluate_expert(
        family="impulse_retest",
        snapshot=_snapshot(
            move_3s_bps=4.0,
            retrace_fraction=0.30,
            impulse_flow_ratio=0.70,
        ),
        regime_state=_state("TREND", "UP"),
        parameters=EXPERT_PARAMETER_SETS["impulse_retest"],
    )

    assert not mismatch.eligible and mismatch.side is None
    assert mismatch.reason.startswith("regime_mismatch")
    assert not weak.eligible and weak.reason == "structural_minimum_not_met"


def test_outcome_feature_is_rejected_before_expert_evaluation():
    with pytest.raises(OutcomeLeakageError):
        _snapshot(realized_pnl=1.0)
