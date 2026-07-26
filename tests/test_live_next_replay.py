import pytest

from src.gridbot.strategy.live_next.contracts import (
    ContractError,
    Decision,
    Opportunity,
    OutcomeStatus,
)
from src.gridbot.strategy.live_next.replay import (
    ExecutionProfile,
    ExitProfile,
    PricePoint,
    ReplayCostModel,
    ReplayDataError,
    replay_decision,
    summarize_results,
)


def _opportunity(*, anchor="anchor_1", side="LONG"):
    return Opportunity.create(
        session_id="replay_train_1",
        observed_at_ms=1_000,
        market_data_max_event_ms=999,
        symbol="ETHUSDC",
        side=side,
        expert_family="impulse_retest",
        anchor_event_id=anchor,
        regime="TREND",
        regime_version="regime_v1",
        cooldown_bucket=1,
        features={"impulse_bps": 12.0},
        config_hash="config_sha",
    )


def _decision(opportunity=None, *, action="ACCEPT"):
    opportunity = opportunity or _opportunity()
    return Decision.create(
        opportunity,
        decided_at_ms=1_000,
        action=action,
        reason="score_passed" if action == "ACCEPT" else "score_low",
        score=75.0 if action == "ACCEPT" else 60.0,
        threshold=70.0,
        policy_version="selector_v1",
        expert_id="impulse_retest_v1",
        execution_profile_id="maker_10s" if action == "ACCEPT" else None,
        exit_profile_id="tp8_sl6_t1t2" if action == "ACCEPT" else None,
    )


def _execution():
    return ExecutionProfile("maker_10s", entry_offset_bps=1.0, entry_ttl_ms=10_000)


def _exit():
    return ExitProfile(
        "tp8_sl6_t1t2",
        take_profit_bps=8.0,
        stop_loss_bps=6.0,
        t1_ms=5_000,
        t1_min_mfe_bps=2.0,
        t2_ms=20_000,
    )


def _cost():
    return ReplayCostModel(
        entry_fee_bps=0.5,
        exit_fee_bps=0.5,
        spread_slippage_bps=0.5,
        adverse_selection_buffer_bps=1.0,
    )


def _replay(opportunity, decision, points):
    return replay_decision(
        candidate_id="candidate_1",
        opportunity=opportunity,
        decision=decision,
        reference_price=100.0,
        price_points=[PricePoint(at_ms, price) for at_ms, price in points],
        execution=_execution(),
        exit_profile=_exit(),
        cost_model=_cost(),
        notional_usdc=50.0,
    )


def test_long_first_touch_tp_is_cost_after_positive():
    opportunity = _opportunity()
    result = _replay(
        opportunity,
        _decision(opportunity),
        [(1_100, 99.99), (2_000, 100.00), (3_000, 100.08), (25_000, 100.08)],
    )

    assert result.entry_limit_price == pytest.approx(99.99)
    assert result.outcome.status is OutcomeStatus.CLOSED
    assert result.outcome.exit_reason == "TP"
    assert result.outcome.gross_pnl_usdc == pytest.approx(0.04)
    assert result.outcome.all_in_cost_usdc > 0
    assert result.outcome.net_pnl_usdc > 0


def test_stop_loss_and_t1_no_mfe_are_distinct_failures():
    sl_opp = _opportunity(anchor="sl")
    sl = _replay(
        sl_opp,
        _decision(sl_opp),
        [(1_100, 99.99), (2_000, 99.92), (25_000, 99.92)],
    )
    t1_opp = _opportunity(anchor="t1")
    t1 = _replay(
        t1_opp,
        _decision(t1_opp),
        [(1_100, 99.99), (3_000, 100.00), (6_100, 100.00), (25_000, 100.00)],
    )

    assert sl.outcome.exit_reason == "SL"
    assert sl.outcome.net_pnl_usdc < 0
    assert t1.outcome.exit_reason == "T1_NO_MFE"


def test_t2_max_hold_and_entry_expiry_are_terminal():
    hold_opp = _opportunity(anchor="hold")
    hold = _replay(
        hold_opp,
        _decision(hold_opp),
        [(1_100, 99.99), (4_000, 100.02), (7_000, 100.02), (21_100, 100.02)],
    )
    expiry_opp = _opportunity(anchor="expiry")
    expiry = _replay(
        expiry_opp,
        _decision(expiry_opp),
        [(2_000, 100.01), (11_000, 100.01), (25_000, 100.01)],
    )

    assert hold.outcome.exit_reason == "T2_MAX_HOLD"
    assert expiry.outcome.status is OutcomeStatus.ENTRY_EXPIRED
    assert expiry.outcome.filled is False


def test_metrics_use_deduplicated_opportunity_denominator_and_exit_decomposition():
    win_opp = _opportunity(anchor="win")
    win = _replay(
        win_opp,
        _decision(win_opp),
        [(1_100, 99.99), (2_000, 100.08), (25_000, 100.08)],
    )
    expiry_opp = _opportunity(anchor="expiry_metrics")
    expiry = _replay(
        expiry_opp,
        _decision(expiry_opp),
        [(2_000, 100.01), (11_000, 100.01), (25_000, 100.01)],
    )

    metrics = summarize_results([win, expiry])

    assert metrics.opportunities == 2
    assert metrics.placed == 2
    assert metrics.fills == 1
    assert metrics.closed == 1
    assert metrics.raw_win_rate == 1.0
    assert metrics.ev_per_opportunity_usdc == pytest.approx(metrics.net_pnl_usdc / 2)
    assert metrics.exit_reason_counts == {"TP": 1}
    with pytest.raises(ContractError, match="one paid decision"):
        summarize_results([win, win])


def test_replay_rejects_incomplete_stream_future_order_and_uneconomic_tp():
    opportunity = _opportunity()
    decision = _decision(opportunity)
    with pytest.raises(ReplayDataError, match="before entry TTL"):
        _replay(opportunity, decision, [(2_000, 100.01)])
    with pytest.raises(ReplayDataError, match="pre-decision"):
        _replay(opportunity, decision, [(999, 99.99), (25_000, 100.0)])

    expensive = ReplayCostModel(3.0, 3.0, 1.0, 2.0)
    with pytest.raises(ContractError, match="round-trip cost"):
        replay_decision(
            candidate_id="candidate_1",
            opportunity=opportunity,
            decision=decision,
            reference_price=100.0,
            price_points=[PricePoint(1_100, 99.99), PricePoint(25_000, 100.0)],
            execution=_execution(),
            exit_profile=_exit(),
            cost_model=expensive,
            notional_usdc=50.0,
        )
