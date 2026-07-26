"""Pure Decimal calculations for fixed-exit shadow V3."""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from src.gridbot.mainnet.shadow_simulator_v3_contract import (
    BPS_V3,
    ZERO_V3,
    ExitReasonV3,
    ResolvedTargetV3,
    ShadowSimulationInputErrorV3,
    ShadowTickV3,
    ShadowTradeSpecV3,
    TargetLevelV3,
    VerifiedCoverageV3,
    quantize_tick_price_v3,
)


def validate_ticks_v3(ticks: Sequence[ShadowTickV3], coverage: VerifiedCoverageV3) -> tuple[ShadowTickV3, ...]:
    ordered = tuple(ticks)
    previous_id: int | None = None
    previous_time: int | None = None
    for tick in ordered:
        if not isinstance(tick, ShadowTickV3):
            raise ShadowSimulationInputErrorV3("ticks must be ShadowTickV3 records")
        if previous_id is not None and tick.aggregate_trade_id <= previous_id:
            raise ShadowSimulationInputErrorV3("aggregate trade IDs must be unique and strictly increasing")
        if previous_time is not None and tick.timestamp_ms < previous_time:
            raise ShadowSimulationInputErrorV3("tick timestamps must be nondecreasing")
        if not coverage.contains(tick.timestamp_ms):
            raise ShadowSimulationInputErrorV3("tick lies outside verified proof coverage")
        previous_id, previous_time = tick.aggregate_trade_id, tick.timestamp_ms
    return ordered


def resolve_target_v3(spec: ShadowTradeSpecV3, target: TargetLevelV3, *, is_tp: bool) -> ResolvedTargetV3:
    if target.anchor == "ABSOLUTE":
        assert target.absolute_price is not None
        source = target.absolute_price
    else:
        anchor = spec.signal_price if target.anchor == "SIGNAL" else spec.entry_limit_price
        assert target.distance_bp is not None
        favorable = (spec.side == "BUY" and is_tp) or (spec.side == "SELL" and not is_tp)
        sign = Decimal("1") if favorable else Decimal("-1")
        source = anchor * (Decimal("1") + sign * target.distance_bp / BPS_V3)
    return ResolvedTargetV3(target.anchor, source, quantize_tick_price_v3(source, spec.tick_size))


def levels_v3(spec: ShadowTradeSpecV3) -> tuple[ResolvedTargetV3, ResolvedTargetV3]:
    entry = spec.entry_limit_price
    tp, sl = resolve_target_v3(spec, spec.tp, is_tp=True), resolve_target_v3(spec, spec.sl, is_tp=False)
    valid = tp.executable_price > entry and sl.executable_price < entry if spec.side == "BUY" else tp.executable_price < entry and sl.executable_price > entry
    if not valid:
        raise ShadowSimulationInputErrorV3("executable TP/SL must lie on valid sides of the quantized entry")
    return tp, sl


def can_fill_v3(spec: ShadowTradeSpecV3, price: Decimal) -> bool:
    entry = spec.entry_limit_price
    if spec.fill_model == "TOUCH_UPPER_BOUND":
        return price <= entry if spec.side == "BUY" else price >= entry
    return price <= entry - spec.tick_size if spec.side == "BUY" else price >= entry + spec.tick_size


def touch_reason_v3(spec: ShadowTradeSpecV3, price: Decimal, tp: ResolvedTargetV3, sl: ResolvedTargetV3) -> ExitReasonV3 | None:
    if spec.side == "BUY":
        return "TP" if price >= tp.executable_price else "SL" if price <= sl.executable_price else None
    return "TP" if price <= tp.executable_price else "SL" if price >= sl.executable_price else None


def adverse_exit_price_v3(spec: ShadowTradeSpecV3, base: Decimal, reason: ExitReasonV3) -> Decimal:
    slippage = spec.fees.adverse_slippage_bp(reason)
    if slippage == 0:
        return base
    sign = Decimal("-1") if spec.side == "BUY" else Decimal("1")
    return base * (Decimal("1") + sign * slippage / BPS_V3)


def mfe_mae_v3(side: str, entry: Decimal, market_prices: Sequence[Decimal]) -> tuple[Decimal, Decimal]:
    if not market_prices:
        raise ShadowSimulationInputErrorV3("MFE/MAE needs the real fill tick")
    high, low = max(market_prices), min(market_prices)
    if side == "BUY":
        return max(ZERO_V3, (high / entry - 1) * BPS_V3), max(ZERO_V3, (1 - low / entry) * BPS_V3)
    return max(ZERO_V3, (1 - low / entry) * BPS_V3), max(ZERO_V3, (high / entry - 1) * BPS_V3)


__all__ = ["adverse_exit_price_v3", "can_fill_v3", "levels_v3", "mfe_mae_v3", "touch_reason_v3", "validate_ticks_v3"]
