from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from src.gridbot.mainnet.v1469_adaptive_identity import MarketStateIdentity
from src.gridbot.mainnet.v1469_arm_profiles import (
    RANGE_SCALP,
    RISK_OFF,
    TREND_PARTIAL,
    get_arm_profile,
)
from src.gridbot.mainnet.v1469_paired_evaluator import (
    AggTradePathTick,
    BookPathTick,
    MatchedArmOpportunity,
    ShadowCostModel,
    TickEnvelope,
    evaluate_arm_profile,
    evaluate_paired_arms,
)


OBSERVED = 1_000_000


def _identity(regime: str = "RANGE", side: str = "LONG") -> MarketStateIdentity:
    return MarketStateIdentity(
        environment="MAINNET",
        symbol="BTCUSDT",
        lane_code="W6A",
        effective_side=side,
        strategy="CODEX_V1",
        coarse_regime=regime,
        market_state="mixed",
    )


def _opportunity(
    *,
    regime: str = "RANGE",
    side: str = "LONG",
    status: str = "SAFE",
) -> MatchedArmOpportunity:
    return MatchedArmOpportunity(
        opportunity_id="opp-1",
        candidate_status=status,
        market_identity=_identity(regime, side),
        signal_price=100.0,
    )


def _costs(
    maker: float = 2.0,
    taker: float = 5.0,
    slippage: float = 1.0,
) -> ShadowCostModel:
    return ShadowCostModel(
        maker_fee_bp=maker,
        taker_fee_bp=taker,
        adverse_slippage_bp=slippage,
        provenance="unit-test-fees",
    )


def _agg_tick(
    age_ms: int,
    price: float,
    aggregate_trade_id: int,
    *,
    available_at_ms: int | None = None,
) -> AggTradePathTick:
    timestamp = OBSERVED + age_ms
    return AggTradePathTick(
        timestamp_ms=timestamp,
        available_at_ms=(
            timestamp if available_at_ms is None else available_at_ms
        ),
        aggregate_trade_id=aggregate_trade_id,
        price=price,
    )


def _envelope(
    ticks,
    *,
    coverage_age_ms: int,
    decision_age_ms: int | None = None,
) -> TickEnvelope:
    decision_age = (
        coverage_age_ms + 1_000
        if decision_age_ms is None
        else decision_age_ms
    )
    return TickEnvelope(
        opportunity_id="opp-1",
        observed_at_ms=OBSERVED,
        decision_at_ms=OBSERVED + decision_age,
        coverage_through_ms=OBSERVED + coverage_age_ms,
        ticks=tuple(ticks),
        provenance="immutable-test-envelope",
    )


def _evaluate_range(ticks, *, coverage_age_ms: int):
    return evaluate_arm_profile(
        _opportunity(),
        get_arm_profile(RANGE_SCALP),
        _envelope(ticks, coverage_age_ms=coverage_age_ms),
        _costs(),
    )


def test_paired_profiles_share_the_exact_same_immutable_envelope() -> None:
    envelope = _envelope(
        (
            _agg_tick(1_000, 99.97, 1),
            _agg_tick(2_000, 100.08, 2),
        ),
        coverage_age_ms=3_000,
    )
    paired = evaluate_paired_arms(_opportunity(), envelope, _costs())

    assert paired.envelope is envelope
    assert tuple(result.profile_id for result in paired.results) == (
        "RANGE_SCALP",
        "PASSIVE_BALANCED",
        "RISK_OFF",
    )
    assert {result.envelope_hash for result in paired.results} == {
        envelope.envelope_hash
    }
    with pytest.raises(FrozenInstanceError):
        envelope.coverage_through_ms = OBSERVED  # type: ignore[misc]


def test_tp_reward_deducts_entry_and_exit_maker_fees() -> None:
    result = _evaluate_range(
        (
            _agg_tick(1_000, 99.98, 1),
            _agg_tick(2_000, 100.08, 2),
        ),
        coverage_age_ms=3_000,
    )

    assert result.fill_status == "FILLED"
    assert result.terminal_reason == "TP"
    assert result.gross_reward_bp == pytest.approx(8.0, abs=1e-5)
    assert result.maker_fee_cost_bp == pytest.approx(4.0016, abs=1e-5)
    assert result.taker_fee_cost_bp == 0.0
    assert result.slippage_cost_bp == 0.0
    assert result.reward_net_bp == pytest.approx(3.9984, abs=1e-5)
    assert result.data_complete is True
    assert result.evaluable is True


def test_sl_reward_deducts_taker_fee_and_adverse_slippage() -> None:
    result = _evaluate_range(
        (
            _agg_tick(1_000, 99.98, 1),
            _agg_tick(2_000, 99.80, 2),
        ),
        coverage_age_ms=3_000,
    )

    assert result.terminal_reason == "SL"
    assert result.gross_reward_bp == pytest.approx(-8.0, abs=1e-5)
    assert result.maker_fee_cost_bp == 2.0
    assert result.taker_fee_cost_bp == pytest.approx(4.996, abs=1e-5)
    assert result.slippage_cost_bp == 1.0
    assert result.reward_net_bp == pytest.approx(-15.996, abs=1e-5)
    assert result.mae_bp is not None and result.mae_bp > 8.0


def test_complete_no_fill_has_zero_reward_and_is_evaluable() -> None:
    result = _evaluate_range(
        (_agg_tick(1_000, 100.20, 1),),
        coverage_age_ms=90_000,
    )

    assert result.fill_status == "NO_FILL"
    assert result.terminal_reason == "NO_FILL"
    assert result.reward_net_bp == 0.0
    assert result.gross_reward_bp == 0.0
    assert result.data_complete is True
    assert result.evaluable is True
    assert result.mfe_bp is None
    assert result.mae_bp is None
    repository_payload = result.to_repository_terminal_payload(
        updated_at_ms=OBSERVED + 91_000
    )
    assert (
        repository_payload["outcome"],
        repository_payload["fill_status"],
        repository_payload["reward_net_bp"],
    ) == ("no_fill", "NO_FILL", 0.0)


def test_same_timestamp_tp_and_sl_is_ambiguous_and_not_evaluable() -> None:
    result = _evaluate_range(
        (
            _agg_tick(1_000, 99.98, 1),
            _agg_tick(2_000, 100.20, 2),
            _agg_tick(2_000, 99.80, 3),
        ),
        coverage_age_ms=3_000,
    )

    assert result.fill_status == "FILLED"
    assert result.terminal_reason == "AMBIGUOUS_BOTH"
    assert result.data_complete is True
    assert result.evaluable is False
    assert result.reward_net_bp is None
    assert result.mfe_bp is not None and result.mfe_bp > 0
    assert result.mae_bp is not None and result.mae_bp > 0


@pytest.mark.parametrize(
    ("side", "fill_price", "tp_price"),
    [
        ("LONG", 99.98, 100.08),
        ("SHORT", 100.02, 99.92),
    ],
)
def test_long_and_short_paths_terminalize_symmetrically(
    side: str, fill_price: float, tp_price: float
) -> None:
    result = evaluate_arm_profile(
        _opportunity(side=side),
        get_arm_profile(RANGE_SCALP),
        _envelope(
            (
                _agg_tick(1_000, fill_price, 1),
                _agg_tick(2_000, tp_price, 2),
            ),
            coverage_age_ms=3_000,
        ),
        _costs(),
    )

    assert result.side == side
    assert result.terminal_reason == "TP"
    assert result.gross_reward_bp == pytest.approx(8.0, abs=1e-5)
    assert result.mfe_bp is not None and result.mfe_bp > 0


def test_book_bid_ask_mark_path_supports_maker_fill_and_tp() -> None:
    fill = BookPathTick(
        timestamp_ms=OBSERVED + 1_000,
        available_at_ms=OBSERVED + 1_000,
        bid_price=99.96,
        ask_price=99.98,
        mark_price=99.97,
    )
    take_profit = BookPathTick(
        timestamp_ms=OBSERVED + 2_000,
        available_at_ms=OBSERVED + 2_000,
        bid_price=100.08,
        ask_price=100.10,
        mark_price=100.09,
    )

    result = evaluate_arm_profile(
        _opportunity(),
        get_arm_profile(RANGE_SCALP),
        _envelope((take_profit, fill), coverage_age_ms=3_000),
        _costs(),
    )

    assert result.fill_status == "FILLED"
    assert result.terminal_reason == "TP"
    assert result.envelope_hash


def test_long_book_tp_requires_executable_bid_not_ask_or_mark() -> None:
    fill = BookPathTick(
        timestamp_ms=OBSERVED + 1_000,
        available_at_ms=OBSERVED + 1_000,
        bid_price=99.96,
        ask_price=99.98,
        mark_price=99.97,
    )
    non_executable_tp = BookPathTick(
        timestamp_ms=OBSERVED + 2_000,
        available_at_ms=OBSERVED + 2_000,
        bid_price=100.05,
        ask_price=100.08,
        mark_price=100.07,
    )

    result = evaluate_arm_profile(
        _opportunity(),
        get_arm_profile(RANGE_SCALP),
        _envelope(
            (fill, non_executable_tp),
            coverage_age_ms=3_000,
        ),
        _costs(),
    )

    assert result.terminal_reason == "DATA_INCOMPLETE"
    assert result.evaluable is False


def test_trend_partial_records_tp1_then_stops_remaining_fraction() -> None:
    result = evaluate_arm_profile(
        _opportunity(regime="TREND_UP"),
        get_arm_profile(TREND_PARTIAL),
        _envelope(
            (
                _agg_tick(1_000, 99.97, 1),
                _agg_tick(2_000, 100.05, 2),
                _agg_tick(3_000, 99.85, 3),
            ),
            coverage_age_ms=4_000,
        ),
        _costs(),
    )

    assert result.terminal_reason == "SL"
    assert [(item.level_id, item.fraction) for item in result.exits] == [
        ("TP1", 0.7),
        ("SL", 0.3),
    ]
    assert sum(item.fraction for item in result.exits) == pytest.approx(1.0)
    assert result.maker_fee_cost_bp is not None
    assert result.taker_fee_cost_bp is not None
    assert result.repository_outcome == "tp1_first"


def test_max_hold_uses_deadline_point_and_taker_costs() -> None:
    fill_age = 1_000
    hold_age = fill_age + 360_000
    result = _evaluate_range(
        (
            _agg_tick(fill_age, 99.98, 1),
            _agg_tick(hold_age, 100.00, 2),
        ),
        coverage_age_ms=hold_age,
    )

    assert result.terminal_reason == "MAX_HOLD"
    assert result.terminal_at_ms == OBSERVED + hold_age
    assert result.taker_fee_cost_bp is not None
    assert result.taker_fee_cost_bp > 0.0
    assert result.slippage_cost_bp == 1.0
    assert result.repository_outcome == "max_hold"


@pytest.mark.parametrize(
    ("side", "fill_price", "last_price", "post_deadline_prices"),
    [
        ("LONG", 99.98, 100.01, (99.00, 101.00)),
        ("SHORT", 100.02, 99.99, (101.00, 99.00)),
    ],
)
def test_sparse_max_hold_ignores_post_deadline_move(
    side: str,
    fill_price: float,
    last_price: float,
    post_deadline_prices: tuple[float, float],
) -> None:
    fill_age = 1_000
    hold_age = fill_age + 360_000

    def evaluate(post_deadline_price: float):
        return evaluate_arm_profile(
            _opportunity(side=side),
            get_arm_profile(RANGE_SCALP),
            _envelope(
                (
                    _agg_tick(fill_age, fill_price, 1),
                    _agg_tick(hold_age - 1_000, last_price, 2),
                    _agg_tick(
                        hold_age + 10_000,
                        post_deadline_price,
                        3,
                    ),
                ),
                coverage_age_ms=hold_age + 10_000,
            ),
            _costs(),
        )

    first = evaluate(post_deadline_prices[0])
    second = evaluate(post_deadline_prices[1])

    for result in (first, second):
        assert result.terminal_reason == "MAX_HOLD"
        assert result.terminal_at_ms == OBSERVED + hold_age
        assert result.terminal_price == last_price
        assert result.exits[-1].timestamp_ms == OBSERVED + hold_age
    assert first.gross_reward_bp == pytest.approx(second.gross_reward_bp)
    assert first.reward_net_bp == pytest.approx(second.reward_net_bp)
    assert first.mfe_bp == pytest.approx(second.mfe_bp)
    assert first.mae_bp == pytest.approx(second.mae_bp)


def test_max_hold_without_post_fill_reference_price_fails_closed() -> None:
    fill_age = 1_000
    hold_age = fill_age + 360_000
    result = _evaluate_range(
        (_agg_tick(fill_age, 99.98, 1),),
        coverage_age_ms=hold_age,
    )

    assert result.terminal_reason == "DATA_INCOMPLETE"
    assert result.evaluable is False
    assert result.data_complete is False
    assert result.reward_net_bp is None


def test_incomplete_coverage_never_manufactures_no_fill_or_max_hold() -> None:
    before_ttl = _evaluate_range(
        (_agg_tick(1_000, 100.20, 1),),
        coverage_age_ms=30_000,
    )
    filled_without_terminal = _evaluate_range(
        (
            _agg_tick(1_000, 99.98, 1),
            _agg_tick(2_000, 100.00, 2),
        ),
        coverage_age_ms=3_000,
    )

    assert before_ttl.terminal_reason == "DATA_INCOMPLETE"
    assert before_ttl.fill_status == "INCOMPLETE"
    assert filled_without_terminal.terminal_reason == "DATA_INCOMPLETE"
    assert filled_without_terminal.fill_status == "FILLED"
    assert before_ttl.data_complete is False
    assert filled_without_terminal.evaluable is False


def test_anti_lookahead_ignores_pre_observation_and_unavailable_ticks() -> None:
    decision_age = 100_000
    decision_at = OBSERVED + decision_age
    envelope = _envelope(
        (
            _agg_tick(-1, 99.0, 1),
            _agg_tick(0, 99.0, 2),
            _agg_tick(
                1_000,
                99.0,
                3,
                available_at_ms=decision_at + 1,
            ),
            _agg_tick(2_000, 100.20, 4),
        ),
        coverage_age_ms=90_000,
        decision_age_ms=decision_age,
    )

    result = evaluate_arm_profile(
        _opportunity(),
        get_arm_profile(RANGE_SCALP),
        envelope,
        _costs(),
    )

    assert envelope.rejected_tick_count == 3
    assert result.fill_status == "NO_FILL"
    assert result.reward_net_bp == 0.0


def test_tick_input_order_and_terminal_hash_are_deterministic() -> None:
    ticks = (
        _agg_tick(1_000, 99.98, 1),
        _agg_tick(2_000, 100.08, 2),
    )
    first_envelope = _envelope(ticks, coverage_age_ms=3_000)
    second_envelope = _envelope(
        tuple(reversed(ticks)), coverage_age_ms=3_000
    )
    first = evaluate_arm_profile(
        _opportunity(),
        get_arm_profile(RANGE_SCALP),
        first_envelope,
        _costs(),
    )
    second = evaluate_arm_profile(
        _opportunity(),
        get_arm_profile(RANGE_SCALP),
        second_envelope,
        _costs(),
    )

    assert first_envelope.envelope_hash == second_envelope.envelope_hash
    assert first.to_payload() == second.to_payload()
    assert first.terminal_hash == second.terminal_hash
    assert len(first.terminal_hash) == 64


def test_terminal_results_map_to_repository_enums_without_storage_imports() -> None:
    tp = _evaluate_range(
        (
            _agg_tick(1_000, 99.98, 1),
            _agg_tick(2_000, 100.08, 2),
        ),
        coverage_age_ms=3_000,
    )
    ambiguous = _evaluate_range(
        (
            _agg_tick(1_000, 99.98, 3),
            _agg_tick(2_000, 100.20, 4),
            _agg_tick(2_000, 99.80, 5),
        ),
        coverage_age_ms=3_000,
    )
    incomplete = _evaluate_range(
        (_agg_tick(1_000, 100.20, 6),),
        coverage_age_ms=30_000,
    )

    tp_payload = tp.to_repository_terminal_payload(
        updated_at_ms=OBSERVED + 4_000
    )
    ambiguous_payload = ambiguous.to_repository_terminal_payload(
        updated_at_ms=OBSERVED + 4_000
    )
    incomplete_payload = incomplete.to_repository_terminal_payload(
        updated_at_ms=OBSERVED + 31_000
    )

    assert (
        tp_payload["status"],
        tp_payload["outcome"],
        tp_payload["fill_status"],
        tp_payload["ambiguous"],
    ) == ("TERMINAL", "tp_first", "FILLED", False)
    assert (
        ambiguous_payload["outcome"],
        ambiguous_payload["fill_status"],
        ambiguous_payload["ambiguous"],
    ) == ("ambiguous_both", "FILLED", True)
    assert (
        incomplete_payload["status"],
        incomplete_payload["outcome"],
        incomplete_payload["fill_status"],
        incomplete_payload["data_complete"],
    ) == ("DROPPED", "data_incomplete", "UNKNOWN", False)


def test_risk_off_is_zero_reward_without_market_path_dependency() -> None:
    envelope = _envelope((), coverage_age_ms=0)
    result = evaluate_arm_profile(
        _opportunity(regime="SHOCK", status="NOT_EVALUATED"),
        get_arm_profile(RISK_OFF),
        envelope,
        _costs(),
    )

    assert result.fill_status == "RISK_OFF"
    assert result.terminal_reason == "RISK_OFF"
    assert result.reward_net_bp == 0.0
    assert result.data_complete is True
    assert result.execution_profile_hash is None
    assert result.repository_outcome is None
    with pytest.raises(ValueError, match="not persistable"):
        result.to_repository_terminal_payload(updated_at_ms=OBSERVED)


def test_mixed_book_and_aggtrade_envelope_fails_closed() -> None:
    book = BookPathTick(
        timestamp_ms=OBSERVED + 1_000,
        available_at_ms=OBSERVED + 1_000,
        bid_price=99.9,
        ask_price=100.0,
        mark_price=99.95,
    )
    agg = _agg_tick(2_000, 100.0, 1)

    with pytest.raises(ValueError, match="one deterministic path kind"):
        _envelope((book, agg), coverage_age_ms=3_000)


def test_duplicate_aggregate_trade_id_fails_closed() -> None:
    with pytest.raises(ValueError, match="aggregate_trade_id"):
        _envelope(
            (
                _agg_tick(1_000, 99.98, 7),
                _agg_tick(2_000, 100.08, 7),
            ),
            coverage_age_ms=3_000,
        )


def test_cost_model_is_part_of_deterministic_terminal_identity() -> None:
    envelope = _envelope(
        (
            _agg_tick(1_000, 99.98, 1),
            _agg_tick(2_000, 100.08, 2),
        ),
        coverage_age_ms=3_000,
    )
    cheap = evaluate_arm_profile(
        _opportunity(),
        get_arm_profile(RANGE_SCALP),
        envelope,
        _costs(maker=1.0),
    )
    expensive = evaluate_arm_profile(
        _opportunity(),
        get_arm_profile(RANGE_SCALP),
        envelope,
        _costs(maker=3.0),
    )

    assert cheap.reward_net_bp > expensive.reward_net_bp
    assert cheap.cost_model_hash != expensive.cost_model_hash
    assert cheap.terminal_hash != expensive.terminal_hash
