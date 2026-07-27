from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from src.gridbot.strategy.live_next.features import (
    CausalityViolation,
    FeatureLeakageError,
    FeatureObservation,
    FeatureRole,
    FeatureSnapshot,
    OutcomeLeakageError,
)
from src.gridbot.strategy.live_next.regime_state import (
    Regime,
    RegimeConfig,
    RegimeDirection,
    RegimeEvidence,
    RegimeStateMachine,
    TimestampOrderError,
    transition_regime,
)


def evidence(
    decision_time_ms: int,
    *,
    trend: float = 0.0,
    range_: float = 0.0,
    shock: float = 0.0,
    age_ms: int = 0,
    direction: float | None = None,
) -> RegimeEvidence:
    return RegimeEvidence(
        decision_time_ms=decision_time_ms,
        event_time_ms=decision_time_ms - age_ms,
        available_at_ms=decision_time_ms,
        trend_score=trend,
        range_score=range_,
        shock_score=shock,
        direction_score=direction,
    )


def test_feature_snapshot_is_causal_immutable_and_deterministic() -> None:
    source = {
        "trend_score": 0.8,
        "range_score": 0.2,
        "nested": {"spread_bps": 1.5},
    }
    snapshot = FeatureSnapshot.create(
        observed_at_ms=96,
        market_data_max_event_ms=91,
        values=source,
        feature_version="live_next_test_v1",
    )
    reverse_order = FeatureSnapshot.create(
        observed_at_ms=96,
        market_data_max_event_ms=91,
        values=dict(reversed(tuple(source.items()))),
        feature_version="live_next_test_v1",
    )

    source["trend_score"] = -1.0
    source["nested"]["spread_bps"] = 99.0
    returned_values = snapshot.values
    returned_values["trend_score"] = -1.0

    assert snapshot.values["trend_score"] == 0.8
    assert snapshot.values["nested"]["spread_bps"] == 1.5
    assert snapshot.feature_hash == reverse_order.feature_hash
    assert snapshot.values_json == reverse_order.values_json
    assert snapshot.data_age_ms == 5
    snapshot.assert_usable_at(100)
    with pytest.raises(FrozenInstanceError):
        snapshot.decision_time_ms = 101  # type: ignore[misc]


def test_feature_snapshot_rejects_future_and_outcome_inputs() -> None:
    with pytest.raises(FeatureLeakageError, match="timestamp"):
        FeatureSnapshot.create(
            observed_at_ms=100,
            market_data_max_event_ms=101,
            values={"trend_score": 1.0},
            feature_version="live_next_test_v1",
        )

    snapshot = FeatureSnapshot.create(
        observed_at_ms=101,
        market_data_max_event_ms=100,
        values={"trend_score": 1.0},
        feature_version="live_next_test_v1",
    )
    with pytest.raises(FeatureLeakageError, match="future"):
        snapshot.assert_usable_at(100)

    with pytest.raises(FeatureLeakageError, match="outcome-derived"):
        FeatureSnapshot.create(
            observed_at_ms=100,
            market_data_max_event_ms=99,
            values={"realized_pnl": 1.0},
            feature_version="live_next_test_v1",
        )


def test_per_feature_availability_and_outcome_role_fail_closed() -> None:
    with pytest.raises(CausalityViolation, match="after decision time"):
        FeatureSnapshot(
            decision_time_ms=100,
            features={
                "trend_score": FeatureObservation(0.8, 90, 101),
            },
        )

    with pytest.raises(OutcomeLeakageError, match="outcome-role"):
        FeatureSnapshot(
            decision_time_ms=100,
            features={
                "trend_score": FeatureObservation(
                    0.8,
                    90,
                    95,
                    role=FeatureRole.OUTCOME,
                ),
            },
        )


def test_normal_transition_needs_consecutive_confirmation() -> None:
    config = RegimeConfig(confirmations=2, min_dwell_ms=0, stale_after_ms=1_000)
    machine = RegimeStateMachine(config)

    first = machine.update(evidence(100, trend=0.80, range_=0.10))

    assert first.regime is Regime.UNCERTAIN
    assert first.pending_regime is Regime.TREND
    assert first.pending_direction is RegimeDirection.UP
    assert first.pending_count == 1

    second_input = evidence(101, trend=0.82, range_=0.10)
    pure_result = transition_regime(first, second_input, config)
    second = machine.update(second_input)

    assert second == pure_result
    assert second.regime is Regime.TREND
    assert second.direction is RegimeDirection.UP
    assert second.transitioned is True
    assert second.state_name == "TREND_UP"


def test_hysteresis_holds_trend_between_enter_and_exit_thresholds() -> None:
    machine = RegimeStateMachine(
        RegimeConfig(confirmations=1, min_dwell_ms=0, stale_after_ms=1_000)
    )
    entered = machine.update(evidence(100, trend=0.80, range_=0.10))
    held = machine.update(evidence(101, trend=0.50, range_=0.60))

    assert entered.regime is Regime.TREND
    assert held.regime is Regime.TREND
    assert held.direction is RegimeDirection.UP
    assert held.transitioned is False
    assert held.reason == "trend_exit_hysteresis"


def test_minimum_dwell_blocks_normal_switch_until_elapsed() -> None:
    config = RegimeConfig(confirmations=1, min_dwell_ms=100, stale_after_ms=1_000)
    machine = RegimeStateMachine(config)

    # The first candidate waits for the initial dwell, then becomes TREND.
    machine.update(evidence(0, trend=0.90, range_=0.05))
    trend = machine.update(evidence(100, trend=0.90, range_=0.05))
    too_early = machine.update(evidence(110, trend=0.05, range_=0.90))
    switched = machine.update(evidence(200, trend=0.05, range_=0.90))

    assert trend.regime is Regime.TREND
    assert too_early.regime is Regime.TREND
    assert too_early.pending_regime is Regime.RANGE
    assert too_early.reason == "minimum_dwell"
    assert switched.regime is Regime.RANGE
    assert switched.transitioned is True


def test_shock_preempts_dwell_and_confirmation_but_stale_data_wins() -> None:
    config = RegimeConfig(
        confirmations=3,
        min_dwell_ms=10_000,
        stale_after_ms=50,
    )
    machine = RegimeStateMachine(config)
    machine.update(evidence(0, trend=0.90))
    machine.update(evidence(5_000, trend=0.90))
    trend = machine.update(evidence(10_000, trend=0.90))

    shock = machine.update(
        evidence(10_001, trend=-0.20, shock=0.95, direction=-0.90)
    )
    stale = machine.update(
        evidence(
            10_002,
            trend=-1.0,
            shock=1.0,
            direction=-1.0,
            age_ms=51,
        )
    )

    assert trend.regime is Regime.TREND
    assert shock.regime is Regime.SHOCK
    assert shock.direction is RegimeDirection.DOWN
    assert shock.transitioned is True
    assert shock.reason == "shock_preempted"
    assert stale.regime is Regime.UNCERTAIN
    assert stale.confidence == 0.0
    assert stale.transitioned is True
    assert stale.reason == "stale_data"


def test_shock_exit_uses_lower_hysteresis_threshold() -> None:
    machine = RegimeStateMachine(
        RegimeConfig(confirmations=1, min_dwell_ms=0, stale_after_ms=1_000)
    )
    entered = machine.update(evidence(100, shock=0.90, direction=0.80))
    held = machine.update(evidence(101, trend=0.90, shock=0.55))
    exited = machine.update(evidence(102, trend=0.90, shock=0.49))

    assert entered.regime is Regime.SHOCK
    assert held.regime is Regime.SHOCK
    assert held.reason == "shock_exit_hysteresis"
    assert exited.regime is Regime.TREND
    assert exited.direction is RegimeDirection.UP


def test_trend_direction_flip_is_confirmed_not_immediate() -> None:
    machine = RegimeStateMachine(
        RegimeConfig(confirmations=2, min_dwell_ms=0, stale_after_ms=1_000)
    )
    machine.update(evidence(100, trend=0.90))
    up = machine.update(evidence(101, trend=0.90))
    pending_down = machine.update(evidence(102, trend=-0.90))
    down = machine.update(evidence(103, trend=-0.90))

    assert up.state_name == "TREND_UP"
    assert pending_down.state_name == "TREND_UP"
    assert pending_down.pending_regime is Regime.TREND
    assert pending_down.pending_direction is RegimeDirection.DOWN
    assert down.state_name == "TREND_DOWN"
    assert down.transitioned is True


def test_duplicate_decision_is_idempotent_and_backwards_time_is_rejected() -> None:
    config = RegimeConfig(confirmations=2, min_dwell_ms=0, stale_after_ms=1_000)
    first = transition_regime(None, evidence(100, trend=0.90), config)

    duplicate = transition_regime(first, evidence(100, trend=-0.90), config)

    assert duplicate is first
    with pytest.raises(TimestampOrderError):
        transition_regime(first, evidence(99, trend=0.90), config)


def test_snapshot_integration_uses_oldest_required_feature_for_staleness() -> None:
    snapshot = FeatureSnapshot(
        decision_time_ms=100,
        features={
            "trend_score": FeatureObservation(0.90, 90, 99),
            "range_score": FeatureObservation(0.10, 99, 99),
            "shock_score": FeatureObservation(0.00, 99, 99),
        },
    )
    machine = RegimeStateMachine(
        RegimeConfig(confirmations=1, min_dwell_ms=0, stale_after_ms=5)
    )

    state = machine.update(snapshot)

    assert snapshot.is_stale(
        5,
        feature_names=("trend_score", "range_score", "shock_score"),
    )
    assert state.regime is Regime.UNCERTAIN
    assert state.reason == "stale_data"


def test_event_time_inversion_fails_closed() -> None:
    config = RegimeConfig(confirmations=1, min_dwell_ms=0, stale_after_ms=1_000)
    current = transition_regime(None, evidence(100, trend=0.90), config)
    inverted = RegimeEvidence(
        decision_time_ms=110,
        event_time_ms=99,
        available_at_ms=110,
        trend_score=0.90,
        range_score=0.0,
        shock_score=0.0,
    )

    state = transition_regime(current, inverted, config)

    assert state.regime is Regime.UNCERTAIN
    assert state.reason == "event_time_inversion"
    assert state.transitioned is True
