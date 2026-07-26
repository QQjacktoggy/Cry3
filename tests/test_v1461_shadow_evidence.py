from __future__ import annotations

import math

import pytest

from src.gridbot.mainnet.v1461_shadow_evidence import (
    ShadowCostModel,
    evaluate_v1461_shadow_evidence,
)


def _sample(**overrides):
    values = {
        "side": "LONG",
        "start_ms": 1_000,
        "entry_price": 100.0,
        "tp_price": 101.0,
        "sl_price": 99.0,
        "entry_ttl_s": 1,
        "outcome_ttl_s": 3,
        "requested_notional_usdc": 50.0,
        "fill_model": "limit_touch",
    }
    values.update(overrides)
    return values


def _evaluate(rows, *, sample=None, cost_model=None, **coverage):
    values = {
        "coverage_start_ms": 1_000,
        "coverage_end_ms": 4_000,
        "coverage_complete": True,
    }
    values.update(coverage)
    return evaluate_v1461_shadow_evidence(
        sample or _sample(),
        rows,
        cost_model=cost_model,
        **values,
    )


def test_same_timestamp_tp_sl_ambiguity_is_conservatively_sl_first() -> None:
    result = _evaluate(
        [
            {"a": 10, "T": 1_100, "p": "100.0"},
            {"a": 11, "T": 2_000, "p": "101.1"},
            {"a": 12, "T": 2_000, "p": "98.9"},
        ],
        cost_model=ShadowCostModel(maker_fee_rate=0.0002, taker_fee_rate=0.0004),
    )

    assert result["shadow_outcome"] == "sl_first"
    assert result["first_touch_result"] == "SL_FIRST"
    assert result["ambiguity_flag"] is True
    assert result["ambiguity_resolution"] == "SL_FIRST"
    assert result["exit_liquidity"] == "TAKER"
    assert result["data_complete"] is True


def test_fill_tick_gap_through_stop_is_not_skipped() -> None:
    result = _evaluate([{"a": 10, "T": 1_100, "p": "98.8"}])

    assert result["filled_ts"] == 1_100
    assert result["shadow_outcome"] == "sl_first"
    assert result["touch_trade_id"] == result["fill_trade_id"] == 10


def test_sl_uses_maker_entry_taker_exit_and_conservative_slippage() -> None:
    result = _evaluate(
        [
            {"a": 1, "T": 1_100, "p": "100.0"},
            {"a": 2, "T": 2_100, "p": "99.0"},
        ],
        cost_model=ShadowCostModel(
            maker_fee_rate=0.0002,
            taker_fee_rate=0.0004,
            slippage_bp=1.0,
        ),
    )

    assert result["shadow_outcome"] == "sl_first"
    assert result["paper_pnl_usdc_before_cost"] == pytest.approx(-0.5)
    assert result["entry_fee_usdc"] == pytest.approx(0.01)
    assert result["exit_fee_usdc"] == pytest.approx(0.0198)
    assert result["estimated_slippage_usdc"] == pytest.approx(0.00495)
    assert result["paper_pnl_usdc_after_fee"] == pytest.approx(-0.53475)
    assert result["estimated_fee_bp"] == pytest.approx(5.96)


def test_tp_uses_maker_fee_for_both_legs() -> None:
    result = _evaluate(
        [
            {"a": 1, "T": 1_100, "p": "100.0"},
            {"a": 2, "T": 2_100, "p": "101.0"},
        ],
        cost_model=ShadowCostModel(
            maker_fee_rate=0.0002,
            taker_fee_rate=0.0004,
        ),
    )

    assert result["shadow_outcome"] == "tp1_first"
    assert result["exit_liquidity"] == "MAKER"
    assert result["paper_pnl_usdc_before_cost"] == pytest.approx(0.5)
    assert result["entry_fee_usdc"] == pytest.approx(0.01)
    assert result["exit_fee_usdc"] == pytest.approx(0.0101)
    assert result["paper_pnl_usdc_after_fee"] == pytest.approx(0.4799)


def test_max_hold_uses_last_aggtrade_and_taker_exit() -> None:
    result = _evaluate(
        [
            {"a": 1, "T": 1_100, "p": "100.0"},
            {"a": 2, "T": 3_500, "p": "100.4"},
        ],
        cost_model=ShadowCostModel(maker_fee_rate=0.0002, taker_fee_rate=0.0004),
    )

    assert result["shadow_outcome"] == "max_hold"
    assert result["exit_reference_price"] == pytest.approx(100.4)
    assert result["exit_liquidity"] == "TAKER"
    assert result["paper_pnl_usdc_after_fee"] == pytest.approx(0.16992)


@pytest.mark.parametrize(
    ("rows", "coverage", "reason"),
    [
        (None, {}, "agg_trades_missing"),
        ([{"a": 1, "T": 1_100, "p": "bad"}], {}, "invalid_agg_trade"),
        ([{"a": 1, "T": 1_100, "p": "100"}], {"coverage_complete": False}, "coverage_not_complete"),
        ([{"a": 1, "T": 1_100, "p": "100"}], {"coverage_end_ms": 1_500}, "coverage_ends_before"),
        (
            [{"a": 1, "T": 1_100, "p": "100"}, {"a": 3, "T": 2_100, "p": "101"}],
            {},
            "agg_trade_id_gap",
        ),
        (
            [{"a": 1, "T": 1_100, "p": "100"}, {"a": 2, "T": 2_100, "p": "101"}],
            {"coverage_gaps_ms": [(1_500, 1_700)]},
            "coverage_time_gap",
        ),
    ],
)
def test_missing_invalid_or_gapped_trade_evidence_is_not_evaluable(rows, coverage, reason) -> None:
    result = _evaluate(rows, **coverage)

    assert result["shadow_outcome"] == "data_incomplete"
    assert result["evaluable"] is False
    assert result["data_complete"] is False
    assert result["paper_pnl_usdc_after_fee"] is None
    assert reason in result["data_quality"]["reason"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "not-a-number"])
@pytest.mark.parametrize("field", ["entry_price", "tp_price", "sl_price", "requested_notional_usdc"])
def test_invalid_sample_numbers_never_become_completed_zero(field: str, bad) -> None:
    result = _evaluate([], sample=_sample(**{field: bad}))

    assert result["data_complete"] is False
    assert result["evaluable"] is False
    assert result["paper_pnl_bp_before_fee"] is None
    assert result["paper_pnl_usdc_after_fee"] is None
    assert result["data_quality"]["reason"].startswith("invalid_sample:")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_aggtrade_values_are_incomplete(bad: float) -> None:
    result = _evaluate([{"a": 1, "T": 1_100, "p": bad}])

    assert result["data_complete"] is False
    assert result["paper_pnl_usdc_after_fee"] is None
    assert "invalid_agg_trade" in result["data_quality"]["reason"]


@pytest.mark.parametrize(
    "row",
    [
        {"a": 1.5, "T": 1_100, "p": "100"},
        {"a": 1, "T": 1_100.5, "p": "100"},
    ],
)
def test_fractional_aggtrade_ids_or_timestamps_are_incomplete(row) -> None:
    result = _evaluate([row])

    assert result["data_complete"] is False
    assert result["paper_pnl_usdc_after_fee"] is None
    assert "invalid_agg_trade" in result["data_quality"]["reason"]


def test_finite_but_overflowing_calculation_is_incomplete_not_infinite() -> None:
    result = _evaluate(
        [{"a": 1, "T": 1_100, "p": "1e-308"}],
        sample=_sample(
            entry_price=1e-308,
            sl_price=5e-309,
            tp_price=2e-308,
            requested_notional_usdc=1e308,
        ),
    )

    assert result["data_complete"] is False
    assert result["paper_pnl_usdc_after_fee"] is None
    assert "invalid_calculation" in result["data_quality"]["reason"]


def test_complete_empty_window_is_no_fill_not_missing_data() -> None:
    result = _evaluate([])

    assert result["shadow_outcome"] == "no_fill"
    assert result["data_complete"] is True
    assert result["paper_pnl_usdc_after_fee"] == 0.0


def test_cost_model_rejects_nan_inf_and_negative_values() -> None:
    for bad in (math.nan, math.inf, -math.inf, -0.1):
        with pytest.raises(ValueError):
            ShadowCostModel(slippage_bp=bad)
    with pytest.raises(ValueError):
        ShadowCostModel(maker_fee_rate=math.nan)
    with pytest.raises(ValueError):
        ShadowCostModel(taker_fee_rate=math.inf)
