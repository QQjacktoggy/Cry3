import pytest

from src.gridbot.strategy.live_next.contracts import Decision, Opportunity
from src.gridbot.strategy.live_next.exact_replay import (
    ExactAggTrade,
    ExactReplayConfig,
    VerifiedAggTradeWindow,
    replay_exact_aggtrades,
)
from src.gridbot.strategy.live_next.execution_policy import EntryExecutionMode
from src.gridbot.strategy.live_next.replay import (
    ExecutionProfile,
    ExitProfile,
    ReplayCostModel,
)


def _opportunity(anchor: str) -> Opportunity:
    return Opportunity.create(
        session_id="exact_entry_modes",
        observed_at_ms=1_000,
        market_data_max_event_ms=999,
        symbol="ETHUSDC",
        side="LONG",
        expert_family="impulse_retest",
        anchor_event_id=anchor,
        regime="TREND",
        regime_version="regime_v1",
        cooldown_bucket=1,
        features={"flow_3s": 1.2},
        config_hash="config_sha",
    )


def _window(rows) -> VerifiedAggTradeWindow:
    return VerifiedAggTradeWindow(
        trades=tuple(ExactAggTrade(*row) for row in rows),
        source_artifact_sha256="b" * 64,
    )


def _run(*, anchor: str, mode: EntryExecutionMode, rows, maker_phase_ms: int = 0):
    opportunity = _opportunity(anchor)
    execution = ExecutionProfile("entry_mode", entry_offset_bps=0.0, entry_ttl_ms=2_000)
    exit_profile = ExitProfile("tp24_sl8", 24.0, 8.0, 1_000, 0.0, 3_000)
    decision = Decision.create(
        opportunity,
        decided_at_ms=1_000,
        action="ACCEPT",
        reason="score_passed",
        score=75.0,
        threshold=70.0,
        policy_version="selector_v1",
        expert_id="impulse_retest_v1",
        execution_profile_id=execution.profile_id,
        exit_profile_id=exit_profile.profile_id,
    )
    return replay_exact_aggtrades(
        candidate_id=f"candidate_{anchor}",
        opportunity=opportunity,
        decision=decision,
        reference_price="100",
        window=_window(rows),
        execution=execution,
        exit_profile=exit_profile,
        cost_model=ReplayCostModel(
            entry_fee_bps=2.0,
            exit_fee_bps=2.0,
            spread_slippage_bps=0.0,
            taker_entry_fee_bps=5.0,
            taker_entry_slippage_bps=1.0,
        ),
        notional_usdc="50",
        config=ExactReplayConfig(
            tick_size="0.01",
            order_latency_ms=100,
            entry_execution_mode=mode,
            maker_phase_ms=maker_phase_ms,
        ),
    )


def test_taker_confirm_fills_first_post_latency_trade_at_traded_price_and_full_size():
    result = _run(
        anchor="taker",
        mode=EntryExecutionMode.TAKER_CONFIRM,
        rows=[
            (900, 1, "100.00", "1", False),
            (1_101, 2, "100.03", "0.01", True),
            (1_200, 3, "100.30", "1", False),
            (3_100, 4, "100.30", "1", False),
        ],
    )

    assert result.entry_liquidity == "TAKER"
    assert result.entry_fill_trade_id == 2
    assert result.entry_limit_price == pytest.approx(100.03)
    assert result.entry_filled_fraction == pytest.approx(1.0)
    assert result.outcome.quantity == pytest.approx(50 / 100.03)
    assert result.outcome.exit_reason == "TP"
    assert result.outcome.all_in_cost_usdc > 0.04


def test_hybrid_keeps_maker_liquidity_when_trade_through_arrives_during_phase():
    result = _run(
        anchor="hybrid_maker",
        mode=EntryExecutionMode.HYBRID,
        maker_phase_ms=500,
        rows=[
            (900, 10, "100.00", "1", False),
            (1_300, 11, "99.99", "1", True),
            (1_800, 12, "100.26", "1", False),
            (3_100, 13, "100.26", "1", False),
        ],
    )

    assert result.entry_liquidity == "MAKER"
    assert result.entry_fill_trade_id == 11
    assert result.entry_limit_price == pytest.approx(100.0)
    assert result.entry_filled_fraction == pytest.approx(1.0)
    assert result.outcome.exit_reason == "TP"


def test_hybrid_uses_first_trade_after_maker_phase_as_taker_fallback():
    result = _run(
        anchor="hybrid_taker",
        mode=EntryExecutionMode.HYBRID,
        maker_phase_ms=500,
        rows=[
            (900, 20, "100.00", "1", False),
            (1_300, 21, "100.01", "1", False),
            (1_700, 22, "100.05", "0.01", True),
            (1_800, 23, "100.31", "1", False),
            (3_100, 24, "100.31", "1", False),
        ],
    )

    assert result.entry_liquidity == "TAKER"
    assert result.entry_fill_trade_id == 22
    assert result.entry_limit_price == pytest.approx(100.05)
    assert result.entry_filled_fraction == pytest.approx(1.0)
    assert result.outcome.exit_reason == "TP"


def test_exact_config_fails_closed_on_invalid_hybrid_phase_contract():
    with pytest.raises(ValueError, match="positive maker_phase_ms"):
        ExactReplayConfig(
            tick_size="0.01",
            entry_execution_mode=EntryExecutionMode.HYBRID,
        )

    with pytest.raises(ValueError, match="only valid for HYBRID"):
        ExactReplayConfig(
            tick_size="0.01",
            entry_execution_mode=EntryExecutionMode.MAKER,
            maker_phase_ms=500,
        )
