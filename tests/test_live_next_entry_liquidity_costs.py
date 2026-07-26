import pytest

from src.gridbot.strategy.live_next.contracts import ContractError
from src.gridbot.strategy.live_next.replay import ReplayCostModel


def _cost() -> ReplayCostModel:
    return ReplayCostModel(
        entry_fee_bps=2.0,
        taker_entry_fee_bps=5.0,
        taker_entry_slippage_bps=1.0,
        exit_fee_bps=2.0,
        spread_slippage_bps=0.0,
        active_exit_fee_bps=5.0,
        active_exit_slippage_bps=1.0,
        funding_cost_usdc_per_fill=0.005,
    )


def test_taker_confirmation_costs_more_than_maker_without_changing_exit_reason_costs() -> None:
    cost = _cost()

    maker = cost.all_in_cost_usdc(50.0, 50.0, "TP", "MAKER")
    taker = cost.all_in_cost_usdc(50.0, 50.0, "TP", "TAKER")

    assert maker == pytest.approx(0.025)
    assert taker == pytest.approx(0.045)
    assert cost.all_in_cost_usdc(50.0, 50.0, "SL", "TAKER") == pytest.approx(
        0.065
    )


def test_fee_stress_scales_both_maker_and_taker_fees() -> None:
    stressed = _cost().stressed(fee_multiplier=1.5)

    assert stressed.entry_fee_bps_for("MAKER") == 3.0
    assert stressed.entry_fee_bps_for("TAKER") == 7.5
    assert stressed.taker_entry_slippage_bps == 1.0


def test_unknown_entry_liquidity_fails_closed() -> None:
    with pytest.raises(ContractError, match="entry_liquidity"):
        _cost().all_in_cost_usdc(50.0, 50.0, "TP", "UNKNOWN")
