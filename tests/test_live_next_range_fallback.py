from scripts.freeze_live_next_research import EXPERT_PARAMETER_SETS
from src.gridbot.strategy.live_next.contracts import Side
from src.gridbot.strategy.live_next.expert_registry import evaluate_expert
from src.gridbot.strategy.live_next.features import FeatureSnapshot
from src.gridbot.strategy.live_next.regime_state import RegimeState


def _snapshot(*, position: float, move_2s_bps: float, flow: float) -> FeatureSnapshot:
    return FeatureSnapshot.create(
        observed_at_ms=1_000,
        market_data_max_event_ms=999,
        values={
            "anchor_event_id": "agg-10",
            "execution_quality": 0.75,
            "exit_economics": 0.80,
            "false_break_bps": 0.0,
            "move_2s_bps": move_2s_bps,
            "range_position_60s": position,
            "reclaim_bps": 0.0,
            "range_inward_flow_ratio": flow,
        },
        feature_version="live_next.features.v1",
    )


def _range_state() -> RegimeState:
    return RegimeState(
        regime="RANGE",
        direction="NONE",
        confidence=0.9,
        since_ms=500,
        last_decision_time_ms=1_000,
        last_event_time_ms=999,
        last_available_at_ms=1_000,
    )


def test_boundary_reversal_fallback_accepts_inward_move_without_failed_break() -> None:
    result = evaluate_expert(
        family="range_reclaim",
        snapshot=_snapshot(position=0.05, move_2s_bps=1.2, flow=0.62),
        regime_state=_range_state(),
        parameters=EXPERT_PARAMETER_SETS["range_reclaim"],
    )

    assert result.eligible
    assert result.side is Side.LONG


def test_boundary_reversal_fallback_rejects_outward_or_weak_move() -> None:
    outward = evaluate_expert(
        family="range_reclaim",
        snapshot=_snapshot(position=0.05, move_2s_bps=-1.2, flow=0.62),
        regime_state=_range_state(),
        parameters=EXPERT_PARAMETER_SETS["range_reclaim"],
    )
    weak = evaluate_expert(
        family="range_reclaim",
        snapshot=_snapshot(position=0.05, move_2s_bps=0.8, flow=0.62),
        regime_state=_range_state(),
        parameters=EXPERT_PARAMETER_SETS["range_reclaim"],
    )

    assert not outward.eligible
    assert not weak.eligible
