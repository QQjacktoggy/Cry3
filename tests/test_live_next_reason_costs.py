import pytest

from src.gridbot.strategy.live_next.contracts import ContractError
from src.gridbot.strategy.live_next.replay import ReplayCostModel


def _reason_aware_cost_model() -> ReplayCostModel:
    return ReplayCostModel(
        2.0,
        3.0,
        4.0,
        5.0,
        active_exit_fee_bps=7.0,
        active_exit_slippage_bps=1.5,
        funding_cost_usdc_per_fill=0.005,
    )


def test_legacy_positional_cost_model_defaults_preserve_tp_costs() -> None:
    model = ReplayCostModel(2.0, 3.0, 4.0, 5.0)

    assert model.active_exit_fee_bps == 3.0
    assert model.active_exit_slippage_bps == 0.0
    assert model.funding_cost_usdc_per_fill == 0.0
    assert model.round_trip_bps == 9.0
    assert model.minimum_economic_tp_bps == 14.0
    assert model.cost_components(50.0, 50.04, "TP") == pytest.approx(
        {
            "entry_fee_usdc": 0.01,
            "exit_fee_usdc": 0.015012,
            "spread_slippage_usdc": 0.02,
            "active_exit_slippage_usdc": 0.0,
            "funding_cost_usdc": 0.0,
        }
    )


@pytest.mark.parametrize("exit_reason", ["SL", "T1_NO_MFE", "T2_MAX_HOLD"])
def test_active_exit_reasons_use_active_fee_slippage_and_flat_funding(
    exit_reason: str,
) -> None:
    model = _reason_aware_cost_model()

    assert model.exit_fee_bps_for(exit_reason) == 7.0
    assert model.slippage_bps_for(exit_reason) == 1.5
    assert model.cost_components(50.0, 49.0, exit_reason) == pytest.approx(
        {
            "entry_fee_usdc": 0.01,
            "exit_fee_usdc": 0.0343,
            "spread_slippage_usdc": 0.02,
            "active_exit_slippage_usdc": 0.00735,
            "funding_cost_usdc": 0.005,
        }
    )


def test_tp_uses_existing_exit_fee_without_active_exit_slippage() -> None:
    model = _reason_aware_cost_model()

    assert model.exit_fee_bps_for("TP") == 3.0
    assert model.slippage_bps_for("TP") == 0.0
    assert model.cost_components(50.0, 52.0, "TP") == pytest.approx(
        {
            "entry_fee_usdc": 0.01,
            "exit_fee_usdc": 0.0156,
            "spread_slippage_usdc": 0.02,
            "active_exit_slippage_usdc": 0.0,
            "funding_cost_usdc": 0.005,
        }
    )


@pytest.mark.parametrize("exit_reason", ["UNKNOWN", "tp", "", None])
def test_unknown_exit_reasons_fail_closed(exit_reason: object) -> None:
    model = _reason_aware_cost_model()

    with pytest.raises(ContractError, match="unknown exit_reason"):
        model.exit_fee_bps_for(exit_reason)  # type: ignore[arg-type]
    with pytest.raises(ContractError, match="unknown exit_reason"):
        model.slippage_bps_for(exit_reason)  # type: ignore[arg-type]
    with pytest.raises(ContractError, match="unknown exit_reason"):
        model.cost_components(50.0, 49.0, exit_reason)  # type: ignore[arg-type]


def test_stressed_scales_all_fees_and_preserves_active_slippage_and_funding() -> None:
    stressed = _reason_aware_cost_model().stressed(
        fee_multiplier=1.25,
        extra_latency_cost_bps=2.0,
    )

    assert stressed.entry_fee_bps == 2.5
    assert stressed.exit_fee_bps == 3.75
    assert stressed.active_exit_fee_bps == 8.75
    assert stressed.spread_slippage_bps == 6.0
    assert stressed.adverse_selection_buffer_bps == 5.0
    assert stressed.active_exit_slippage_bps == 1.5
    assert stressed.funding_cost_usdc_per_fill == 0.005
    assert stressed.cost_components(50.0, 49.0, "SL") == pytest.approx(
        {
            "entry_fee_usdc": 0.0125,
            "exit_fee_usdc": 0.042875,
            "spread_slippage_usdc": 0.03,
            "active_exit_slippage_usdc": 0.00735,
            "funding_cost_usdc": 0.005,
        }
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("active_exit_fee_bps", -0.1),
        ("active_exit_slippage_bps", float("inf")),
        ("funding_cost_usdc_per_fill", True),
    ],
)
def test_reason_aware_cost_fields_are_non_negative_and_finite(
    field: str, value: object
) -> None:
    with pytest.raises(ContractError, match=field):
        ReplayCostModel(2.0, 3.0, 4.0, 5.0, **{field: value})
