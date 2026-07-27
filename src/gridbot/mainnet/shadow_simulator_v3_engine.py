"""Authoritative deterministic fixed-exit E0/E1/E2 shadow engine V3.

Offline only: no app, DB, exchange account, order, Telegram, network, or
wall-clock dependency.  It deliberately makes no trailing/partial-fill claim.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from src.gridbot.mainnet.shadow_simulator_v3_builders import closed_v3, incomplete_v3, unfilled_v3
from src.gridbot.mainnet.shadow_simulator_v3_contract import (
    METRIC_CONTRACT_V3,
    MAX_HOLD_POLICY_V3,
    PRICE_QUANTIZATION_POLICY_V3,
    SIMULATION_SCOPE_V3,
    TARGET_PRICE_POLICY_V3,
    CoverageIntervalV3,
    FeeScheduleV3,
    ShadowSimulationInputErrorV3,
    ShadowTickV3,
    ShadowTradeSpecV3,
    TargetLevelV3,
    VerifiedCoverageV3,
    quantize_tick_price_v3,
)
from src.gridbot.mainnet.shadow_simulator_v3_math import (
    can_fill_v3,
    levels_v3,
    touch_reason_v3,
    validate_ticks_v3,
)
from src.gridbot.mainnet.shadow_simulator_v3_result import ShadowTradeOutcomeV3


def simulate_shadow_v3(
    spec: ShadowTradeSpecV3,
    ticks: Sequence[ShadowTickV3],
    coverage: VerifiedCoverageV3,
) -> ShadowTradeOutcomeV3:
    """Replay one immutable fixed-exit variant using V4 sentinel proof."""
    if not isinstance(spec, ShadowTradeSpecV3) or not isinstance(coverage, VerifiedCoverageV3):
        raise ShadowSimulationInputErrorV3("spec/coverage type mismatch")
    ordered, (tp, sl) = validate_ticks_v3(ticks, coverage), levels_v3(spec)
    fill_index = next(
        (
            index
            for index, tick in enumerate(ordered)
            if spec.entry_eligible_ms <= tick.timestamp_ms <= spec.entry_deadline_ms
            and can_fill_v3(spec, tick.price)
        ),
        None,
    )
    if fill_index is None:
        return unfilled_v3(spec, coverage, tp, sl)

    fill = ordered[fill_index]
    market_prices: list[Decimal] = [fill.price]  # fill tick belongs in MFE/MAE
    if not coverage.covers(spec.start_ms, fill.timestamp_ms):
        return incomplete_v3(spec, coverage, tp, sl, fill, market_prices, "coverage_gap_before_fill")

    for tick in ordered[fill_index + 1 :]:
        market_prices.append(tick.price)
        # The absolute deadline wins at its first post-fill aggregate trade.
        if tick.timestamp_ms >= spec.outcome_deadline_ms:
            if not coverage.covers(spec.start_ms, tick.timestamp_ms):
                return incomplete_v3(
                    spec, coverage, tp, sl, fill, market_prices,
                    "coverage_gap_before_max_hold_sentinel",
                )
            return closed_v3(spec, coverage, tp, sl, fill, tick, "MAX_HOLD", market_prices)
        reason = touch_reason_v3(spec, tick.price, tp, sl)
        if reason is not None:
            if not coverage.covers(spec.start_ms, tick.timestamp_ms):
                return incomplete_v3(spec, coverage, tp, sl, fill, market_prices, "coverage_gap_before_exit")
            return closed_v3(spec, coverage, tp, sl, fill, tick, reason, market_prices)
    return incomplete_v3(
        spec, coverage, tp, sl, fill, market_prices,
        "missing_post_deadline_trade_or_exit",
    )


__all__ = [
    "CoverageIntervalV3",
    "FeeScheduleV3",
    "MAX_HOLD_POLICY_V3",
    "METRIC_CONTRACT_V3",
    "PRICE_QUANTIZATION_POLICY_V3",
    "SIMULATION_SCOPE_V3",
    "ShadowSimulationInputErrorV3",
    "ShadowTickV3",
    "ShadowTradeOutcomeV3",
    "ShadowTradeSpecV3",
    "TARGET_PRICE_POLICY_V3",
    "TargetLevelV3",
    "VerifiedCoverageV3",
    "quantize_tick_price_v3",
    "simulate_shadow_v3",
]
