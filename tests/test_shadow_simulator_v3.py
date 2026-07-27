from __future__ import annotations

from decimal import Decimal

import pytest

from src.gridbot.mainnet.shadow_simulator_v3_engine import (
    CoverageIntervalV3, FeeScheduleV3, METRIC_CONTRACT_V3,
    PRICE_QUANTIZATION_POLICY_V3, SIMULATION_SCOPE_V3,
    ShadowSimulationInputErrorV3, ShadowTickV3, ShadowTradeSpecV3,
    TargetLevelV3, VerifiedCoverageV3, quantize_tick_price_v3,
    simulate_shadow_v3,
)

D = lambda value: Decimal(str(value))


def cov(start=900, requested=1200, proof=1201):
    item = CoverageIntervalV3(start, requested, proof, "continuous_ids_to_sentinel")
    return VerifiedCoverageV3((item,), "v4-sha256")


def fee(**changes):
    values = dict(entry_fee_rate=D(0), tp_exit_fee_rate=D(0), sl_exit_fee_rate=D(0),
                  max_hold_exit_fee_rate=D(0), sl_adverse_slippage_bp=D(0),
                  max_hold_adverse_slippage_bp=D(0), funding_cost_usdc=D(0),
                  fee_provenance="account", funding_provenance="boundary-audit")
    values.update(changes)
    return FeeScheduleV3(**values)


def target(anchor="ENTRY", value="100"):
    return (TargetLevelV3("ABSOLUTE", absolute_price=D(value)) if anchor == "ABSOLUTE"
            else TargetLevelV3(anchor, distance_bp=D(value)))


def trade_spec(**changes):
    values = dict(opportunity_id="opp", variant="E0", fill_model="TOUCH_UPPER_BOUND",
                  simulation_version="v1459-fixed-v3", side="BUY", start_ms=1000,
                  decision_latency_ms=0, entry_ttl_ms=100, outcome_deadline_ms=1200,
                  signal_price=D(100), tick_size=D(1), quantity=D(1),
                  tp=target(), sl=target(), fees=fee())
    values.update(changes)
    return ShadowTradeSpecV3(**values)


def t(identifier, timestamp, price):
    return ShadowTickV3(timestamp, identifier, D(price))


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_rounding_mirrors_client_half_up_for_both_sides(side):
    item = trade_spec(side=side, signal_price=D("100.005"), tick_size=D("0.01"))
    assert item.entry_limit_price == D("100.01")
    assert quantize_tick_price_v3("100.004", "0.01") == D("100.00")
    assert PRICE_QUANTIZATION_POLICY_V3.endswith("ROUND_HALF_UP")


def test_latency_is_inclusive_but_does_not_extend_start_anchored_ttl():
    out = simulate_shadow_v3(trade_spec(decision_latency_ms=10),
                             (t(1, 1005, 100), t(2, 1010, 100), t(3, 1020, 101)), cov())
    assert (out.first_fill_aggregate_trade_id, out.entry_eligible_ms, out.entry_deadline_ms) == (2, 1010, 1100)
    late = simulate_shadow_v3(trade_spec(decision_latency_ms=90), (t(1, 1101, 100),), cov())
    assert late.fill_status == "UNFILLED_EXPIRED"


def test_v4_proof_end_sentinel_produces_complete_max_hold():
    item = trade_spec(tp=target("ABSOLUTE", "110"), sl=target("ABSOLUTE", "90"))
    out = simulate_shadow_v3(item, (t(10, 1010, 100), t(11, 1199, 100.5), t(12, 1201, 100.4)), cov())
    assert (out.exit_reason, out.exit_at_ms, out.data_quality, out.coverage_proof_end_ms) == ("MAX_HOLD", 1201, "COMPLETE", 1201)


def test_coverage_unions_adjacent_proof_intervals_and_rejects_gap():
    good = VerifiedCoverageV3((CoverageIntervalV3(1000, 1050, 1051, "a"), CoverageIntervalV3(1052, 1100, 1101, "b")), "v4")
    bad = VerifiedCoverageV3((CoverageIntervalV3(1000, 1050, 1051, "a"), CoverageIntervalV3(1053, 1100, 1101, "b")), "v4")
    assert good.covers(1000, 1101) and not bad.covers(1000, 1101)


def test_absolute_source_targets_are_preserved_beside_executable_ticks():
    item = trade_spec(signal_price=D("100.005"), tick_size=D("0.01"),
                      tp=target("ABSOLUTE", "100.015"), sl=target("ABSOLUTE", "99.995"))
    out = simulate_shadow_v3(item, (), cov(1000, 1100, 1101))
    assert (out.entry_limit_price, out.tp_source_trigger_price, out.tp_price) == (D("100.01"), D("100.015"), D("100.02"))
    assert (out.sl_source_trigger_price, out.sl_price) == (D("99.995"), D("100.00"))


def test_mfe_mae_include_fill_tick_and_exclude_synthetic_slippage():
    through = simulate_shadow_v3(trade_spec(fill_model="TRADE_THROUGH"), (t(1, 1010, 99), t(2, 1020, 101)), cov())
    assert (through.mfe_bp, through.mae_bp) == (D(100), D(100))
    slipped = simulate_shadow_v3(trade_spec(fees=fee(sl_adverse_slippage_bp=D(10))), (t(1, 1010, 100), t(2, 1020, 98)), cov())
    assert (slipped.exit_price, slipped.mae_bp) == (D("97.902"), D(200))


def test_complete_no_fill_is_wr_excluded_and_zero_ev_contribution():
    out = simulate_shadow_v3(trade_spec(), (), cov(1000, 1100, 1101))
    assert out.data_quality == "COMPLETE" and not out.wr_eligible
    assert out.ev_opportunity_eligible and out.ev_opportunity_contribution_usdc == D(0)
    assert out.eligible_for_ranking and out.metric_contract == METRIC_CONTRACT_V3
    incomplete = simulate_shadow_v3(trade_spec(), (), cov(1000, 1099, 1099))
    assert not incomplete.ev_opportunity_eligible and incomplete.ev_opportunity_contribution_usdc is None


def test_signed_funding_credit_and_short_adverse_signs():
    credit = simulate_shadow_v3(trade_spec(fees=fee(funding_cost_usdc=D("-0.5"))), (t(1, 1010, 100), t(2, 1020, 101)), cov())
    assert (credit.gross_pnl_usdc, credit.funding_usdc, credit.net_pnl_usdc) == (D(1), D("-0.5"), D("1.5"))
    short = trade_spec(side="SELL", tp=target("ABSOLUTE", "99"), sl=target("ABSOLUTE", "101"), fees=fee(sl_adverse_slippage_bp=D(10)))
    loss = simulate_shadow_v3(short, (t(1, 1010, 100), t(2, 1020, 102)), cov())
    assert (loss.exit_reason, loss.exit_price, loss.gross_pnl_usdc) == ("SL", D("102.102"), D("-2.102"))
    with pytest.raises(ShadowSimulationInputErrorV3, match="non-negative"):
        fee(entry_fee_rate=D("-0.1"))


def test_same_ms_id_order_deadline_priority_and_deterministic_scope():
    same_ms = simulate_shadow_v3(trade_spec(), (t(10, 1010, 100), t(11, 1010, 101)), cov())
    assert (same_ms.first_fill_aggregate_trade_id, same_ms.exit_aggregate_trade_id) == (10, 11)
    deadline_args = (trade_spec(), (t(1, 1010, 100), t(2, 1200, 101)), cov())
    first, second = simulate_shadow_v3(*deadline_args), simulate_shadow_v3(*deadline_args)
    assert first == second and first.exit_reason == "MAX_HOLD"
    assert first.simulation_scope == SIMULATION_SCOPE_V3 and first.as_dict()["net_pnl_usdc"] == "1"


def test_bad_id_or_timestamp_order_fails_closed():
    with pytest.raises(ShadowSimulationInputErrorV3, match="strictly increasing"):
        simulate_shadow_v3(trade_spec(), (t(2, 1010, 100), t(1, 1011, 101)), cov())
    with pytest.raises(ShadowSimulationInputErrorV3, match="nondecreasing"):
        simulate_shadow_v3(trade_spec(), (t(1, 1010, 100), t(2, 1009, 101)), cov())
