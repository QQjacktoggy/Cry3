"""Outcome builders for fixed-exit shadow V3."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Sequence

from src.gridbot.mainnet.shadow_simulator_v3_contract import (
    METRIC_CONTRACT_V3,
    MAX_HOLD_POLICY_V3,
    PRICE_QUANTIZATION_POLICY_V3,
    SIMULATION_SCOPE_V3,
    TARGET_PRICE_POLICY_V3,
    ZERO_V3,
    ExitReasonV3,
    ResolvedTargetV3,
    ShadowTickV3,
    ShadowTradeSpecV3,
    VerifiedCoverageV3,
)
from src.gridbot.mainnet.shadow_simulator_v3_math import adverse_exit_price_v3, mfe_mae_v3
from src.gridbot.mainnet.shadow_simulator_v3_result import ShadowTradeOutcomeV3


def base_fields_v3(spec: ShadowTradeSpecV3, coverage: VerifiedCoverageV3, tp: ResolvedTargetV3, sl: ResolvedTargetV3) -> dict[str, Any]:
    return {
        "opportunity_id": spec.opportunity_id, "variant": spec.variant, "fill_model": spec.fill_model,
        "simulation_version": spec.simulation_version, "simulation_scope": SIMULATION_SCOPE_V3, "side": spec.side,
        "start_ms": spec.start_ms, "decision_latency_ms": spec.decision_latency_ms,
        "entry_eligible_ms": spec.entry_eligible_ms, "entry_deadline_ms": spec.entry_deadline_ms,
        "outcome_deadline_ms": spec.outcome_deadline_ms, "entry_offset_bp": spec.entry_offset_bp,
        "raw_entry_limit_price": spec.raw_entry_limit_price, "entry_limit_price": spec.entry_limit_price,
        "quantity": spec.quantity, "tp_anchor": tp.anchor, "tp_source_trigger_price": tp.source_trigger_price,
        "tp_price": tp.executable_price, "sl_anchor": sl.anchor, "sl_source_trigger_price": sl.source_trigger_price,
        "sl_price": sl.executable_price, "price_quantization_policy": PRICE_QUANTIZATION_POLICY_V3,
        "target_price_policy": TARGET_PRICE_POLICY_V3, "metric_contract": METRIC_CONTRACT_V3,
        "coverage_provenance": coverage.provenance, "coverage_proof_end_ms": coverage.max_proof_end_ms,
        "fee_provenance": spec.fees.fee_provenance, "funding_provenance": spec.fees.funding_provenance,
        "max_hold_policy": MAX_HOLD_POLICY_V3,
    }


def unfilled_v3(spec: ShadowTradeSpecV3, coverage: VerifiedCoverageV3, tp: ResolvedTargetV3, sl: ResolvedTargetV3) -> ShadowTradeOutcomeV3:
    complete = coverage.covers(spec.start_ms, spec.entry_deadline_ms)
    return ShadowTradeOutcomeV3(
        **base_fields_v3(spec, coverage, tp, sl), fill_status="UNFILLED_EXPIRED" if complete else "UNFILLED_INCOMPLETE",
        filled_qty=ZERO_V3, avg_fill_price=None, first_fill_at_ms=None, first_fill_aggregate_trade_id=None,
        fill_age_ms=None, exit_at_ms=None, exit_aggregate_trade_id=None, trigger_price=None,
        exit_price_before_slippage=None, exit_price=None, exit_reason=None, exit_fee_rate=None, mfe_bp=None, mae_bp=None,
        gross_pnl_usdc=None, commission_usdc=None, funding_usdc=None, net_pnl_usdc=None, wr_eligible=False,
        wr_win=None, ev_opportunity_eligible=complete, ev_opportunity_contribution_usdc=ZERO_V3 if complete else None,
        data_quality="COMPLETE" if complete else "DATA_INCOMPLETE",
        data_quality_reason="verified_entry_window_no_fill" if complete else "entry_window_coverage_gap",
    )


def incomplete_v3(spec: ShadowTradeSpecV3, coverage: VerifiedCoverageV3, tp: ResolvedTargetV3, sl: ResolvedTargetV3, fill: ShadowTickV3, market_prices: Sequence[Decimal], reason: str) -> ShadowTradeOutcomeV3:
    mfe, mae = mfe_mae_v3(spec.side, spec.entry_limit_price, market_prices)
    return ShadowTradeOutcomeV3(
        **base_fields_v3(spec, coverage, tp, sl), fill_status="FILLED", filled_qty=spec.quantity,
        avg_fill_price=spec.entry_limit_price, first_fill_at_ms=fill.timestamp_ms,
        first_fill_aggregate_trade_id=fill.aggregate_trade_id, fill_age_ms=fill.timestamp_ms - spec.start_ms,
        exit_at_ms=None, exit_aggregate_trade_id=None, trigger_price=None, exit_price_before_slippage=None,
        exit_price=None, exit_reason=None, exit_fee_rate=None, mfe_bp=mfe, mae_bp=mae, gross_pnl_usdc=None,
        commission_usdc=None, funding_usdc=spec.fees.funding_cost_usdc, net_pnl_usdc=None, wr_eligible=False,
        wr_win=None, ev_opportunity_eligible=False, ev_opportunity_contribution_usdc=None,
        data_quality="DATA_INCOMPLETE", data_quality_reason=reason,
    )


def closed_v3(spec: ShadowTradeSpecV3, coverage: VerifiedCoverageV3, tp: ResolvedTargetV3, sl: ResolvedTargetV3, fill: ShadowTickV3, exit_tick: ShadowTickV3, reason: ExitReasonV3, market_prices: Sequence[Decimal]) -> ShadowTradeOutcomeV3:
    base_exit = tp.executable_price if reason == "TP" else exit_tick.price
    exit_price = adverse_exit_price_v3(spec, base_exit, reason)
    mfe, mae = mfe_mae_v3(spec.side, spec.entry_limit_price, market_prices)  # real ticks only
    gross = (exit_price - spec.entry_limit_price) * spec.quantity if spec.side == "BUY" else (spec.entry_limit_price - exit_price) * spec.quantity
    exit_fee = spec.fees.exit_fee_rate(reason)
    commission = spec.entry_limit_price * spec.quantity * spec.fees.entry_fee_rate + exit_price * spec.quantity * exit_fee
    net = gross - commission - spec.fees.funding_cost_usdc
    return ShadowTradeOutcomeV3(
        **base_fields_v3(spec, coverage, tp, sl), fill_status="FILLED", filled_qty=spec.quantity,
        avg_fill_price=spec.entry_limit_price, first_fill_at_ms=fill.timestamp_ms,
        first_fill_aggregate_trade_id=fill.aggregate_trade_id, fill_age_ms=fill.timestamp_ms - spec.start_ms,
        exit_at_ms=exit_tick.timestamp_ms, exit_aggregate_trade_id=exit_tick.aggregate_trade_id,
        trigger_price=exit_tick.price, exit_price_before_slippage=base_exit, exit_price=exit_price,
        exit_reason=reason, exit_fee_rate=exit_fee, mfe_bp=mfe, mae_bp=mae, gross_pnl_usdc=gross,
        commission_usdc=commission, funding_usdc=spec.fees.funding_cost_usdc, net_pnl_usdc=net,
        wr_eligible=True, wr_win=net > 0, ev_opportunity_eligible=True,
        ev_opportunity_contribution_usdc=net, data_quality="COMPLETE",
        data_quality_reason="verified_closed_fixed_exit",
    )


__all__ = ["closed_v3", "incomplete_v3", "unfilled_v3"]
