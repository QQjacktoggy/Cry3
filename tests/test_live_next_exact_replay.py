import pytest

from src.gridbot.strategy.live_next.contracts import Decision, Opportunity, OutcomeStatus
from src.gridbot.strategy.live_next.exact_replay import (
    ExactAggTrade,
    ExactReplayConfig,
    MakerFillModel,
    VerifiedAggTradeWindow,
    replay_exact_aggtrades,
)
from src.gridbot.strategy.live_next.replay import (
    ExecutionProfile,
    ExitProfile,
    ReplayCostModel,
    ReplayDataError,
)


def _opportunity(*, side="LONG", anchor="anchor"):
    return Opportunity.create(
        session_id="exact_train_1",
        observed_at_ms=1_000,
        market_data_max_event_ms=999,
        symbol="ETHUSDC",
        side=side,
        expert_family="impulse_retest",
        anchor_event_id=anchor,
        regime="TREND",
        regime_version="regime_v1",
        cooldown_bucket=1,
        features={"flow_3s": 1.2},
        config_hash="config_sha",
    )


def _decision(opportunity):
    return Decision.create(
        opportunity,
        decided_at_ms=1_000,
        action="ACCEPT",
        reason="score_passed",
        score=75.0,
        threshold=70.0,
        policy_version="selector_v1",
        expert_id="impulse_retest_v1",
        execution_profile_id="maker_10s",
        exit_profile_id="tp8_sl6_t1t2",
    )


def _execution():
    return ExecutionProfile("maker_10s", entry_offset_bps=1.0, entry_ttl_ms=10_000)


def _exit():
    return ExitProfile("tp8_sl6_t1t2", 8.0, 6.0, 5_000, 2.0, 20_000)


def _cost():
    return ReplayCostModel(0.5, 0.5, 0.5, 1.0)


def _window(rows, *, contiguous=True):
    return VerifiedAggTradeWindow(
        trades=tuple(ExactAggTrade(*row) for row in rows),
        source_artifact_sha256="a" * 64,
        require_contiguous_ids=contiguous,
    )


def _run(opportunity, rows, *, config=None, notional="50"):
    return replay_exact_aggtrades(
        candidate_id="candidate_1",
        opportunity=opportunity,
        decision=_decision(opportunity),
        reference_price="100",
        window=_window(rows),
        execution=_execution(),
        exit_profile=_exit(),
        cost_model=_cost(),
        notional_usdc=notional,
        config=config or ExactReplayConfig(tick_size="0.01"),
    )


def test_trade_through_and_aggressor_side_are_required_for_entry_and_tp():
    opportunity = _opportunity()
    result = _run(
        opportunity,
        [
            (900, 10, "100.00", "1", False),
            (1_100, 11, "99.99", "1", True),
            (1_200, 12, "99.98", "1", False),
            (1_300, 13, "99.98", "1", True),
            (2_000, 14, "100.06", "1", False),
            (2_100, 15, "100.07", "1", True),
            (2_200, 16, "100.07", "1", False),
            (25_000, 17, "100.07", "1", False),
        ],
    )

    assert result.entry_limit_price == pytest.approx(99.99)
    assert result.entry_fill_trade_id == 13
    assert result.exit_trade_id == 16
    assert result.outcome.exit_reason == "TP"
    assert result.outcome.net_pnl_usdc > 0


def test_touch_model_is_explicitly_separate_from_canonical_trade_through():
    opportunity = _opportunity(anchor="touch")
    result = _run(
        opportunity,
        [
            (900, 20, "100.00", "1", False),
            (1_100, 21, "99.99", "1", True),
            (2_000, 22, "100.06", "1", False),
            (25_000, 23, "100.06", "1", False),
        ],
        config=ExactReplayConfig(
            tick_size="0.01",
            entry_fill_model=MakerFillModel.TOUCH,
            tp_fill_model=MakerFillModel.TOUCH,
        ),
    )

    assert result.entry_fill_trade_id == 21
    assert result.exit_trade_id == 22
    assert result.outcome.exit_reason == "TP"


def test_partial_first_fill_cancels_remainder_and_records_fraction():
    opportunity = _opportunity(anchor="partial")
    result = _run(
        opportunity,
        [
            (900, 30, "100.00", "1", False),
            (1_100, 31, "99.98", "0.10", True),
            (2_000, 32, "100.07", "1", False),
            (25_000, 33, "100.07", "1", False),
        ],
    )

    assert 0 < result.entry_filled_fraction < 1
    assert result.outcome.quantity == pytest.approx(0.10)
    assert result.outcome.exit_reason == "TP"


def test_no_fill_stays_in_terminal_denominator_and_needs_post_ttl_coverage():
    opportunity = _opportunity(anchor="expiry")
    result = _run(
        opportunity,
        [
            (900, 40, "100.00", "1", False),
            (2_000, 41, "100.01", "1", True),
            (11_001, 42, "100.01", "1", True),
        ],
    )
    assert result.outcome.status is OutcomeStatus.ENTRY_EXPIRED

    with pytest.raises(ReplayDataError, match="coverage"):
        _run(
            _opportunity(anchor="incomplete"),
            [
                (900, 50, "100.00", "1", False),
                (2_000, 51, "100.01", "1", True),
            ],
        )


def test_short_side_quantization_and_aggressor_direction():
    opportunity = _opportunity(side="SHORT", anchor="short")
    result = _run(
        opportunity,
        [
            (900, 60, "100.00", "1", True),
            (1_100, 61, "100.02", "1", True),
            (1_200, 62, "100.02", "1", False),
            (2_000, 63, "99.93", "1", False),
            (2_100, 64, "99.92", "1", True),
            (25_000, 65, "99.92", "1", True),
        ],
    )

    assert result.entry_limit_price == pytest.approx(100.01)
    assert result.entry_fill_trade_id == 62
    assert result.outcome.exit_reason == "TP"


def test_window_rejects_id_gap_and_same_millisecond_order_is_by_agg_id():
    with pytest.raises(ReplayDataError, match="contiguous"):
        _window(
            [
                (900, 70, "100.00", "1", False),
                (1_000, 72, "100.00", "1", False),
            ]
        )
    with pytest.raises(ReplayDataError, match="strictly ordered"):
        _window(
            [
                (900, 80, "100.00", "1", False),
                (900, 79, "100.00", "1", False),
            ],
            contiguous=False,
        )
