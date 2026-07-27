import pytest

from src.gridbot.strategy.live_next.contracts import DecisionAction, Opportunity
from src.gridbot.strategy.live_next.risk_guard import (
    HardGate,
    RiskSnapshot,
    evaluate_hard_gates,
)
from src.gridbot.strategy.live_next.selector import (
    AdaptationMode,
    ScoreBreakdown,
    select_decision,
    update_threshold,
)


def _risk(**overrides):
    values = {
        "ownership_ok": True,
        "account_flat_for_entry": True,
        "data_age_ms": 100,
        "max_data_age_ms": 1_000,
        "causality_ok": True,
        "predicted_all_in_cost_bps": 2.0,
        "max_all_in_cost_bps": 6.0,
        "observed_latency_ms": 80,
        "max_latency_ms": 500,
        "session_net_pnl_usdc": 0.2,
        "max_session_loss_usdc": 1.0,
        "session_drawdown_usdc": 0.1,
        "max_session_drawdown_usdc": 1.0,
        "position_count": 0,
        "working_entry_order_count": 0,
        "position_order_consistent": True,
    }
    values.update(overrides)
    return evaluate_hard_gates(RiskSnapshot(**values))


def _opportunity():
    return Opportunity.create(
        session_id="shadow_1",
        observed_at_ms=1_000,
        market_data_max_event_ms=999,
        symbol="ETHUSDC",
        side="LONG",
        expert_family="impulse_retest",
        anchor_event_id="anchor_1",
        regime="TREND",
        regime_version="regime_v1",
        cooldown_bucket=1,
        features={"velocity": 1.2},
        config_hash="config_sha",
    )


def _score(**overrides):
    values = {
        "regime_fit": 20.0,
        "signal_quality": 20.0,
        "microstructure": 14.0,
        "execution_quality": 11.0,
        "exit_economics": 10.0,
        "uncertainty_penalty": 0.0,
    }
    values.update(overrides)
    return ScoreBreakdown(**values)


def _select(risk, score):
    return select_decision(
        opportunity=_opportunity(),
        decided_at_ms=1_001,
        risk=risk,
        score=score,
        threshold=70.0,
        policy_version="selector_v1",
        expert_id="impulse_retest_v1",
        execution_profile_id="maker_fast",
        exit_profile_id="tp_sl_t1t2",
    )


def test_only_five_hard_gate_categories_and_healthy_path_allows():
    assert set(HardGate) == {
        HardGate.OWNERSHIP_FLATNESS,
        HardGate.DATA_FRESHNESS_CAUSALITY,
        HardGate.EXTREME_COST_LATENCY,
        HardGate.LOSS_DRAWDOWN_BREAKER,
        HardGate.POSITION_ORDER_CONSISTENCY,
    }
    assert _risk().allowed is True


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"account_flat_for_entry": False}, HardGate.OWNERSHIP_FLATNESS),
        ({"data_age_ms": 2_000}, HardGate.DATA_FRESHNESS_CAUSALITY),
        ({"observed_latency_ms": 501}, HardGate.EXTREME_COST_LATENCY),
        ({"session_net_pnl_usdc": -1.0}, HardGate.LOSS_DRAWDOWN_BREAKER),
        ({"position_count": 2}, HardGate.POSITION_ORDER_CONSISTENCY),
    ],
)
def test_each_hard_gate_fails_closed(overrides, expected):
    decision = _risk(**overrides)
    assert decision.allowed is False
    assert expected in decision.failed_gates
    assert decision.permits_order_mutation is False


def test_soft_score_accepts_or_skips_but_hard_gate_always_blocks():
    accepted = _select(_risk(), _score())
    skipped = _select(_risk(), _score(signal_quality=10.0))
    blocked = _select(_risk(causality_ok=False), _score())

    assert accepted.action is DecisionAction.ACCEPT
    assert accepted.score == 75.0
    assert skipped.action is DecisionAction.SKIP
    assert blocked.action is DecisionAction.BLOCK
    assert blocked.reason.startswith("hard_gate:")


def test_threshold_moves_at_most_two_inside_66_76_and_frozen_never_moves():
    lowered = update_threshold(
        current=70.0,
        fills_per_day=3.0,
        prior_epoch_net_pnl_usdc=0.2,
        guardrail_breaches=0,
        mode=AdaptationMode.TRAIN,
    )
    raised = update_threshold(
        current=75.0,
        fills_per_day=8.0,
        prior_epoch_net_pnl_usdc=-0.1,
        guardrail_breaches=0,
        mode="PREQUENTIAL",
    )
    frozen = update_threshold(
        current=70.0,
        fills_per_day=1.0,
        prior_epoch_net_pnl_usdc=-10.0,
        guardrail_breaches=5,
        mode="FROZEN",
    )

    assert lowered.current == 68.0
    assert lowered.delta == -2.0
    assert raised.current == 76.0
    assert raised.delta == 1.0
    assert frozen.current == 70.0
    assert frozen.reason == "frozen_evaluation"
