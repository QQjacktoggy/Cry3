"""Long-only open-range breakout engine with optional derivatives confirmation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import product

from src.gridbot.strategy.long_breakout import (
    BreakoutContext,
    _atr_series,
    _avg_volume_series,
    _prior_high_series,
    _simulate_breakout,
)
from src.gridbot.strategy.long_pullback import (
    BacktestSummary,
    Candle,
    SignalPlan,
    StrategyConfig,
    TradeResult,
    _daily_guard_reason,
    _day_key,
    _drawdown_pct,
    _ema_series,
    _empty_daily_pnls,
    _rank_score,
    _risk_adjusted_config,
    _rsi_series,
    _summary,
    _vwap_series,
    _position_sizing,
)


@dataclass(frozen=True)
class OrbConfig:
    base: StrategyConfig = StrategyConfig(
        risk_per_trade_pct=1.0,
        min_score=58,
        max_holding_bars=48,
        cooldown_bars=8,
        take_profit_r=(0.8, 1.5, 2.6),
        exit_weights=(0.30, 0.35, 0.35),
    )
    session_start_bar: int = 0
    session_end_bar: int = 288
    opening_range_bars: int = 12
    volume_lookback: int = 36
    min_breakout_atr: float = 0.05
    min_orb_range_atr: float = 0.35
    stop_atr: float = 1.0
    stop_buffer_atr: float = 0.2
    entry_buffer_atr: float = 0.03
    trail_atr: float = 1.2
    min_volume_ratio: float = 0.95
    require_oi_confirmation: bool = False
    min_oi_delta_pct: float = 0.5
    reject_extreme_funding: bool = False
    max_funding_rate: float = 0.0003


@dataclass(frozen=True)
class OrbContext(BreakoutContext):
    opening_range_high_values: list[float | None] | None = None
    opening_range_low_values: list[float | None] | None = None
    opening_range_width_atr_values: list[float | None] | None = None
    session_bar_values: list[int] | None = None


def generate_orb_signal(
    candles: list[Candle],
    config: OrbConfig | None = None,
) -> SignalPlan:
    config = config or OrbConfig()
    context = build_orb_context(candles, config)
    return generate_orb_signal_at(candles, len(candles) - 1, config, context)


def generate_orb_short_signal_at(
    candles: list[Candle],
    index: int,
    config: OrbConfig,
    context: OrbContext | None = None,
) -> SignalPlan:
    base = config.base
    warmup = max(config.volume_lookback, base.ema_slow_period, base.vwap_period, config.opening_range_bars)
    if index < warmup or index >= len(candles):
        return _wait(base, candles, index, "not enough candles")

    context = context or build_orb_context(candles, config)
    candle = candles[index]
    price = candle.close
    atr_value = context.atr_values[index]
    avg_volume = context.avg_volume_values[index]
    ema_fast = context.ema_fast_values[index]
    ema_slow = context.ema_slow_values[index]
    rsi_value = context.rsi_values[index]
    vwap_value = context.vwap_values[index]
    orb_high = context.opening_range_high_values[index] if context.opening_range_high_values else None
    orb_low = context.opening_range_low_values[index] if context.opening_range_low_values else None
    orb_width_atr = context.opening_range_width_atr_values[index] if context.opening_range_width_atr_values else None
    session_bar = context.session_bar_values[index] if context.session_bar_values else 0
    oi_delta_pct = context.oi_delta_pct_values[index] if context.oi_delta_pct_values else None
    funding_rate = context.funding_rate_values[index] if context.funding_rate_values else None

    if atr_value is None or atr_value <= 0 or orb_high is None or orb_low is None:
        return _wait(base, candles, index, "opening range unavailable")
    if session_bar < 0:
        return _wait(base, candles, index, "outside ORB session")
    if session_bar < config.opening_range_bars:
        return _wait(base, candles, index, "waiting for opening range")
    if session_bar >= config.session_end_bar - config.session_start_bar:
        return _wait(base, candles, index, "session window closed")
    if orb_width_atr is None or orb_width_atr < config.min_orb_range_atr:
        return _wait(base, candles, index, "opening range too compressed")

    breakout_over_atr = (orb_low - price) / atr_value
    volume_ratio = candle.volume / avg_volume if avg_volume and avg_volume > 0 else 1.0
    candle_range = max(candle.high - candle.low, 0.0001)
    close_position = (candle.close - candle.low) / candle_range
    score = 0
    reasons: list[str] = []
    risk_notes: list[str] = []

    if price < orb_low and breakout_over_atr >= config.min_breakout_atr:
        score += 34
        reasons.append(f"close broke session OR low by {breakout_over_atr:.2f} ATR")
    else:
        return SignalPlan(
            action="WAIT",
            confidence=0,
            score=0,
            symbol=base.symbol,
            price=price,
            rsi=rsi_value,
            atr=atr_value,
            support=orb_high,
            vwap=vwap_value,
            daily_target_usdc=(base.daily_target_min_usdc, base.daily_target_max_usdc),
            reasons=["no short ORB breakdown"],
        )

    if ema_fast is not None and ema_slow is not None and ema_fast < ema_slow and price < ema_fast:
        score += 22
        reasons.append("EMA stack supports intraday short trend")
    elif ema_slow is not None and price < ema_slow:
        score += 10
        reasons.append("price remains below slow EMA")
    else:
        risk_notes.append("short trend background is weak")

    if vwap_value is not None and price <= vwap_value:
        score += 12
        reasons.append("price is below VWAP")
    elif vwap_value is not None:
        risk_notes.append("price is above VWAP")

    if rsi_value is not None:
        if 28 <= rsi_value <= 48:
            score += 16
            reasons.append(f"RSI {rsi_value:.1f} supports downside continuation")
        elif 48 < rsi_value <= 55:
            score += 6
            reasons.append(f"RSI {rsi_value:.1f} is early downside momentum")
        elif rsi_value < 22:
            risk_notes.append(f"RSI {rsi_value:.1f} is stretched")

    if close_position <= 0.35:
        score += 8
        reasons.append("breakdown candle closed near low")

    if volume_ratio >= 1.35:
        score += 14
        reasons.append(f"volume expansion {volume_ratio:.2f}x")
    elif volume_ratio >= config.min_volume_ratio:
        score += 7
        reasons.append(f"volume acceptable {volume_ratio:.2f}x")
    else:
        risk_notes.append(f"weak volume {volume_ratio:.2f}x")

    if config.require_oi_confirmation:
        if oi_delta_pct is None:
            risk_notes.append("OI unavailable")
        elif oi_delta_pct >= config.min_oi_delta_pct:
            score += 10
            reasons.append(f"OI rising {oi_delta_pct:.2f}%")
        else:
            return SignalPlan(
                action="WAIT",
                confidence=0,
                score=0,
                symbol=base.symbol,
                price=price,
                rsi=rsi_value,
                atr=atr_value,
                support=orb_high,
                vwap=vwap_value,
                daily_target_usdc=(base.daily_target_min_usdc, base.daily_target_max_usdc),
                reasons=[f"OI confirmation failed ({oi_delta_pct:.2f}%)" if oi_delta_pct is not None else "OI unavailable"],
                risk_notes=risk_notes,
            )

    if config.reject_extreme_funding and funding_rate is not None:
        if funding_rate < -config.max_funding_rate:
            return SignalPlan(
                action="WAIT",
                confidence=0,
                score=0,
                symbol=base.symbol,
                price=price,
                rsi=rsi_value,
                atr=atr_value,
                support=orb_high,
                vwap=vwap_value,
                daily_target_usdc=(base.daily_target_min_usdc, base.daily_target_max_usdc),
                reasons=[f"funding too short-crowded ({funding_rate:.5f})"],
                risk_notes=risk_notes,
            )
        score += 4
        reasons.append(f"funding acceptable {funding_rate:.5f}")

    if score < base.min_score:
        return SignalPlan(
            action="WAIT",
            confidence=min(score, 100),
            score=score,
            symbol=base.symbol,
            price=price,
            rsi=rsi_value,
            atr=atr_value,
            support=orb_high,
            vwap=vwap_value,
            daily_target_usdc=(base.daily_target_min_usdc, base.daily_target_max_usdc),
            reasons=reasons or ["score below threshold"],
            risk_notes=risk_notes,
        )

    entry = round(max(price * 1.001, orb_low - atr_value * config.entry_buffer_atr), 4)
    structural_stop = orb_high + atr_value * config.stop_buffer_atr
    volatility_stop = entry + atr_value * config.stop_atr
    stop_loss = round(max(volatility_stop, structural_stop), 4)
    risk_per_unit = max(stop_loss - entry, 0.0)
    if risk_per_unit <= 0:
        return _wait(base, candles, index, "invalid short ORB stop distance")

    sizing = _position_sizing(entry, entry - risk_per_unit, score, base, risk_notes)
    if sizing.planned_qty <= 0:
        return _wait(base, candles, index, "invalid short ORB position sizing")

    take_profits = [entry - risk_per_unit * r for r in base.take_profit_r]
    if sizing.planned_notional_usdc > base.equity_usdc * 10:
        risk_notes.append("short ORB notional is high; keep testnet-only until validated")

    return SignalPlan(
        action="PLAN_SHORT",
        confidence=min(score, 100),
        score=score,
        symbol=base.symbol,
        price=price,
        rsi=rsi_value,
        atr=atr_value,
        support=orb_high,
        vwap=vwap_value,
        entries=[entry],
        entry_weights=[1.0],
        stop_loss=stop_loss,
        take_profits=take_profits,
        planned_notional_usdc=sizing.planned_notional_usdc,
        planned_margin_usdc=sizing.planned_margin_usdc,
        planned_qty=sizing.planned_qty,
        risk_amount_usdc=sizing.risk_amount_usdc,
        sizing_mode=sizing.sizing_mode,
        leverage_cap=sizing.leverage_cap,
        daily_target_usdc=(base.daily_target_min_usdc, base.daily_target_max_usdc),
        reasons=reasons,
        risk_notes=risk_notes,
    )


def generate_vwap_reversion_long_signal_at(
    candles: list[Candle],
    index: int,
    config: OrbConfig,
    context: OrbContext | None = None,
    min_deviation_atr: float = 1.2,
    min_wick_ratio: float = 0.35,
) -> SignalPlan:
    base = config.base
    warmup = max(config.volume_lookback, base.ema_slow_period, base.vwap_period, config.opening_range_bars)
    if index < warmup or index >= len(candles):
        return _wait(base, candles, index, "not enough candles")

    context = context or build_orb_context(candles, config)
    candle = candles[index]
    price = candle.close
    atr_value = context.atr_values[index]
    rsi_value = context.rsi_values[index]
    vwap_value = context.vwap_values[index]
    ema_fast = context.ema_fast_values[index]
    ema_slow = context.ema_slow_values[index]
    avg_volume = context.avg_volume_values[index]
    if atr_value is None or atr_value <= 0 or vwap_value is None:
        return _wait(base, candles, index, "VWAP reversion context unavailable")

    deviation_atr = (vwap_value - price) / atr_value
    candle_range = max(candle.high - candle.low, 0.0001)
    lower_wick_ratio = (min(candle.open, candle.close) - candle.low) / candle_range
    close_position = (candle.close - candle.low) / candle_range
    volume_ratio = candle.volume / avg_volume if avg_volume and avg_volume > 0 else 1.0
    score = 0
    reasons: list[str] = []
    risk_notes: list[str] = []

    if deviation_atr >= min_deviation_atr:
        score += 32
        reasons.append(f"price is {deviation_atr:.2f} ATR below VWAP")
    else:
        return _wait(base, candles, index, "not extended enough below VWAP")

    if lower_wick_ratio >= min_wick_ratio and close_position >= 0.55:
        score += 24
        reasons.append(f"lower wick rejection {lower_wick_ratio:.2f}")
    else:
        return _wait(base, candles, index, "no lower rejection candle")

    if rsi_value is not None:
        if 28 <= rsi_value <= 45:
            score += 16
            reasons.append(f"RSI {rsi_value:.1f} supports oversold reversion")
        elif 45 < rsi_value <= 52:
            score += 7
            reasons.append(f"RSI {rsi_value:.1f} is neutral enough for reversion")
        elif rsi_value < 22:
            risk_notes.append(f"RSI {rsi_value:.1f} is strongly bearish")

    if ema_fast is not None and ema_slow is not None and ema_fast < ema_slow:
        risk_notes.append("EMA stack is bearish; keep reversion small")
    elif ema_fast is not None and price >= ema_fast:
        score += 8
        reasons.append("price reclaimed fast EMA")

    if volume_ratio >= 0.8:
        score += 8
        reasons.append(f"volume supports rejection {volume_ratio:.2f}x")

    if score < base.min_score:
        return SignalPlan(
            action="WAIT",
            confidence=min(score, 100),
            score=score,
            symbol=base.symbol,
            price=price,
            rsi=rsi_value,
            atr=atr_value,
            support=vwap_value,
            vwap=vwap_value,
            daily_target_usdc=(base.daily_target_min_usdc, base.daily_target_max_usdc),
            reasons=reasons or ["VWAP reversion score below threshold"],
            risk_notes=risk_notes,
        )

    entry = round(price * 0.999, 4)
    stop_loss = round(candle.low - atr_value * 0.35, 4)
    risk_per_unit = max(entry - stop_loss, 0.0)
    if risk_per_unit <= 0:
        return _wait(base, candles, index, "invalid VWAP reversion long stop")
    sizing = _position_sizing(entry, stop_loss, score, base, risk_notes)
    if sizing.planned_qty <= 0:
        return _wait(base, candles, index, "invalid VWAP reversion long sizing")

    take_profits = sorted([
        max(vwap_value, entry + risk_per_unit * 0.45),
        entry + risk_per_unit * 0.9,
        entry + risk_per_unit * 1.6,
    ])
    return SignalPlan(
        action="PLAN_LONG",
        confidence=min(score, 100),
        score=score,
        symbol=base.symbol,
        price=price,
        rsi=rsi_value,
        atr=atr_value,
        support=vwap_value,
        vwap=vwap_value,
        entries=[entry],
        entry_weights=[1.0],
        stop_loss=stop_loss,
        take_profits=take_profits,
        planned_notional_usdc=sizing.planned_notional_usdc,
        planned_margin_usdc=sizing.planned_margin_usdc,
        planned_qty=sizing.planned_qty,
        risk_amount_usdc=sizing.risk_amount_usdc,
        sizing_mode=sizing.sizing_mode,
        leverage_cap=sizing.leverage_cap,
        daily_target_usdc=(base.daily_target_min_usdc, base.daily_target_max_usdc),
        reasons=reasons,
        risk_notes=risk_notes,
    )


def generate_vwap_reversion_short_signal_at(
    candles: list[Candle],
    index: int,
    config: OrbConfig,
    context: OrbContext | None = None,
    min_deviation_atr: float = 1.2,
    min_wick_ratio: float = 0.35,
) -> SignalPlan:
    base = config.base
    warmup = max(config.volume_lookback, base.ema_slow_period, base.vwap_period, config.opening_range_bars)
    if index < warmup or index >= len(candles):
        return _wait(base, candles, index, "not enough candles")

    context = context or build_orb_context(candles, config)
    candle = candles[index]
    price = candle.close
    atr_value = context.atr_values[index]
    rsi_value = context.rsi_values[index]
    vwap_value = context.vwap_values[index]
    ema_fast = context.ema_fast_values[index]
    ema_slow = context.ema_slow_values[index]
    avg_volume = context.avg_volume_values[index]
    if atr_value is None or atr_value <= 0 or vwap_value is None:
        return _wait(base, candles, index, "VWAP reversion context unavailable")

    deviation_atr = (price - vwap_value) / atr_value
    candle_range = max(candle.high - candle.low, 0.0001)
    upper_wick_ratio = (candle.high - max(candle.open, candle.close)) / candle_range
    close_position = (candle.close - candle.low) / candle_range
    volume_ratio = candle.volume / avg_volume if avg_volume and avg_volume > 0 else 1.0
    score = 0
    reasons: list[str] = []
    risk_notes: list[str] = []

    if deviation_atr >= min_deviation_atr:
        score += 32
        reasons.append(f"price is {deviation_atr:.2f} ATR above VWAP")
    else:
        return _wait(base, candles, index, "not extended enough above VWAP")

    if upper_wick_ratio >= min_wick_ratio and close_position <= 0.45:
        score += 24
        reasons.append(f"upper wick rejection {upper_wick_ratio:.2f}")
    else:
        return _wait(base, candles, index, "no upper rejection candle")

    if rsi_value is not None:
        if 55 <= rsi_value <= 72:
            score += 16
            reasons.append(f"RSI {rsi_value:.1f} supports overbought reversion")
        elif 48 <= rsi_value < 55:
            score += 7
            reasons.append(f"RSI {rsi_value:.1f} is neutral enough for reversion")
        elif rsi_value > 78:
            risk_notes.append(f"RSI {rsi_value:.1f} is strongly bullish")

    if ema_fast is not None and ema_slow is not None and ema_fast > ema_slow:
        risk_notes.append("EMA stack is bullish; keep reversion small")
    elif ema_fast is not None and price <= ema_fast:
        score += 8
        reasons.append("price lost fast EMA")

    if volume_ratio >= 0.8:
        score += 8
        reasons.append(f"volume supports rejection {volume_ratio:.2f}x")

    if score < base.min_score:
        return SignalPlan(
            action="WAIT",
            confidence=min(score, 100),
            score=score,
            symbol=base.symbol,
            price=price,
            rsi=rsi_value,
            atr=atr_value,
            support=vwap_value,
            vwap=vwap_value,
            daily_target_usdc=(base.daily_target_min_usdc, base.daily_target_max_usdc),
            reasons=reasons or ["VWAP reversion score below threshold"],
            risk_notes=risk_notes,
        )

    entry = round(price * 1.001, 4)
    stop_loss = round(candle.high + atr_value * 0.35, 4)
    risk_per_unit = max(stop_loss - entry, 0.0)
    if risk_per_unit <= 0:
        return _wait(base, candles, index, "invalid VWAP reversion short stop")
    sizing = _position_sizing(entry, entry - risk_per_unit, score, base, risk_notes)
    if sizing.planned_qty <= 0:
        return _wait(base, candles, index, "invalid VWAP reversion short sizing")

    take_profits = sorted([
        min(vwap_value, entry - risk_per_unit * 0.45),
        entry - risk_per_unit * 0.9,
        entry - risk_per_unit * 1.6,
    ], reverse=True)
    return SignalPlan(
        action="PLAN_SHORT",
        confidence=min(score, 100),
        score=score,
        symbol=base.symbol,
        price=price,
        rsi=rsi_value,
        atr=atr_value,
        support=vwap_value,
        vwap=vwap_value,
        entries=[entry],
        entry_weights=[1.0],
        stop_loss=stop_loss,
        take_profits=take_profits,
        planned_notional_usdc=sizing.planned_notional_usdc,
        planned_margin_usdc=sizing.planned_margin_usdc,
        planned_qty=sizing.planned_qty,
        risk_amount_usdc=sizing.risk_amount_usdc,
        sizing_mode=sizing.sizing_mode,
        leverage_cap=sizing.leverage_cap,
        daily_target_usdc=(base.daily_target_min_usdc, base.daily_target_max_usdc),
        reasons=reasons,
        risk_notes=risk_notes,
    )


def generate_orb_signal_at(
    candles: list[Candle],
    index: int,
    config: OrbConfig,
    context: OrbContext | None = None,
) -> SignalPlan:
    base = config.base
    warmup = max(config.volume_lookback, base.ema_slow_period, base.vwap_period, config.opening_range_bars)
    if index < warmup or index >= len(candles):
        return _wait(base, candles, index, "not enough candles")

    context = context or build_orb_context(candles, config)
    candle = candles[index]
    price = candle.close
    atr_value = context.atr_values[index]
    avg_volume = context.avg_volume_values[index]
    ema_fast = context.ema_fast_values[index]
    ema_slow = context.ema_slow_values[index]
    rsi_value = context.rsi_values[index]
    vwap_value = context.vwap_values[index]
    orb_high = context.opening_range_high_values[index] if context.opening_range_high_values else None
    orb_low = context.opening_range_low_values[index] if context.opening_range_low_values else None
    orb_width_atr = context.opening_range_width_atr_values[index] if context.opening_range_width_atr_values else None
    session_bar = context.session_bar_values[index] if context.session_bar_values else 0
    oi_delta_pct = context.oi_delta_pct_values[index] if context.oi_delta_pct_values else None
    funding_rate = context.funding_rate_values[index] if context.funding_rate_values else None

    if atr_value is None or atr_value <= 0 or orb_high is None or orb_low is None:
        return _wait(base, candles, index, "opening range unavailable")
    if session_bar < 0:
        return _wait(base, candles, index, "outside ORB session")
    if session_bar < config.opening_range_bars:
        return _wait(base, candles, index, "waiting for opening range")
    if session_bar >= config.session_end_bar - config.session_start_bar:
        return _wait(base, candles, index, "session window closed")
    if orb_width_atr is None or orb_width_atr < config.min_orb_range_atr:
        return _wait(base, candles, index, "opening range too compressed")

    breakout_over_atr = (price - orb_high) / atr_value
    volume_ratio = candle.volume / avg_volume if avg_volume and avg_volume > 0 else 1.0
    score = 0
    reasons: list[str] = []
    risk_notes: list[str] = []

    if price > orb_high and breakout_over_atr >= config.min_breakout_atr:
        score += 34
        reasons.append(f"close broke session OR high by {breakout_over_atr:.2f} ATR")
    else:
        return SignalPlan(
            action="WAIT",
            confidence=0,
            score=0,
            symbol=base.symbol,
            price=price,
            rsi=rsi_value,
            atr=atr_value,
            support=orb_low,
            vwap=vwap_value,
            daily_target_usdc=(base.daily_target_min_usdc, base.daily_target_max_usdc),
            reasons=["no ORB breakout"],
        )

    if ema_fast is not None and ema_slow is not None and ema_fast > ema_slow and price > ema_fast:
        score += 22
        reasons.append("EMA stack supports intraday trend")
    elif ema_slow is not None and price > ema_slow:
        score += 10
        reasons.append("price remains above slow EMA")
    else:
        risk_notes.append("trend background is weak")

    if vwap_value is not None and price >= vwap_value:
        score += 12
        reasons.append("price is above VWAP")
    elif vwap_value is not None:
        risk_notes.append("price is below VWAP")

    if rsi_value is not None:
        if 55 <= rsi_value <= 74:
            score += 16
            reasons.append(f"RSI {rsi_value:.1f} supports continuation")
        elif 48 <= rsi_value < 55:
            score += 8
            reasons.append(f"RSI {rsi_value:.1f} is building")
        elif rsi_value > 80:
            risk_notes.append(f"RSI {rsi_value:.1f} overheated")

    if volume_ratio >= 1.35:
        score += 14
        reasons.append(f"volume expansion {volume_ratio:.2f}x")
    elif volume_ratio >= config.min_volume_ratio:
        score += 7
        reasons.append(f"volume acceptable {volume_ratio:.2f}x")
    else:
        risk_notes.append(f"weak volume {volume_ratio:.2f}x")

    if config.require_oi_confirmation:
        if oi_delta_pct is None:
            risk_notes.append("OI unavailable")
        elif oi_delta_pct >= config.min_oi_delta_pct:
            score += 12
            reasons.append(f"OI rising {oi_delta_pct:.2f}%")
        else:
            return SignalPlan(
                action="WAIT",
                confidence=0,
                score=0,
                symbol=base.symbol,
                price=price,
                rsi=rsi_value,
                atr=atr_value,
                support=orb_low,
                vwap=vwap_value,
                daily_target_usdc=(base.daily_target_min_usdc, base.daily_target_max_usdc),
                reasons=[f"OI confirmation failed ({oi_delta_pct:.2f}%)" if oi_delta_pct is not None else "OI unavailable"],
                risk_notes=risk_notes,
            )

    if config.reject_extreme_funding and funding_rate is not None:
        if funding_rate > config.max_funding_rate:
            return SignalPlan(
                action="WAIT",
                confidence=0,
                score=0,
                symbol=base.symbol,
                price=price,
                rsi=rsi_value,
                atr=atr_value,
                support=orb_low,
                vwap=vwap_value,
                daily_target_usdc=(base.daily_target_min_usdc, base.daily_target_max_usdc),
                reasons=[f"funding too hot ({funding_rate:.5f})"],
                risk_notes=risk_notes,
            )
        score += 4
        reasons.append(f"funding acceptable {funding_rate:.5f}")

    if score < base.min_score:
        return SignalPlan(
            action="WAIT",
            confidence=min(score, 100),
            score=score,
            symbol=base.symbol,
            price=price,
            rsi=rsi_value,
            atr=atr_value,
            support=orb_low,
            vwap=vwap_value,
            daily_target_usdc=(base.daily_target_min_usdc, base.daily_target_max_usdc),
            reasons=reasons or ["score below threshold"],
            risk_notes=risk_notes,
        )

    entry = round(min(price * 0.999, orb_high + atr_value * config.entry_buffer_atr), 4)
    structural_stop = orb_low - atr_value * config.stop_buffer_atr
    volatility_stop = entry - atr_value * config.stop_atr
    stop_loss = round(min(volatility_stop, structural_stop), 4)
    risk_per_unit = max(entry - stop_loss, 0.0)
    if risk_per_unit <= 0:
        return _wait(base, candles, index, "invalid ORB stop distance")

    sizing = _position_sizing(entry, stop_loss, score, base, risk_notes)
    if sizing.planned_qty <= 0:
        return _wait(base, candles, index, "invalid ORB position sizing")

    take_profits = [entry + risk_per_unit * r for r in base.take_profit_r]
    if sizing.planned_notional_usdc > base.equity_usdc * 10:
        risk_notes.append("ORB notional is high; keep testnet-only until validated")

    return SignalPlan(
        action="PLAN_LONG",
        confidence=min(score, 100),
        score=score,
        symbol=base.symbol,
        price=price,
        rsi=rsi_value,
        atr=atr_value,
        support=orb_low,
        vwap=vwap_value,
        entries=[entry],
        entry_weights=[1.0],
        stop_loss=stop_loss,
        take_profits=take_profits,
        planned_notional_usdc=sizing.planned_notional_usdc,
        planned_margin_usdc=sizing.planned_margin_usdc,
        planned_qty=sizing.planned_qty,
        risk_amount_usdc=sizing.risk_amount_usdc,
        sizing_mode=sizing.sizing_mode,
        leverage_cap=sizing.leverage_cap,
        daily_target_usdc=(base.daily_target_min_usdc, base.daily_target_max_usdc),
        reasons=reasons,
        risk_notes=risk_notes,
    )


def run_orb_backtest(
    candles: list[Candle],
    config: OrbConfig | None = None,
) -> BacktestSummary:
    config = config or OrbConfig()
    context = build_orb_context(candles, config)
    return run_orb_backtest_with_context(candles, config, context)


def run_orb_backtest_with_context(
    candles: list[Candle],
    config: OrbConfig,
    context: OrbContext,
) -> BacktestSummary:
    base = config.base
    warmup = max(config.volume_lookback, base.ema_slow_period, base.vwap_period, config.opening_range_bars) + 2
    trades: list[TradeResult] = []
    equity = base.equity_usdc
    peak_equity = equity
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    daily = _empty_daily_pnls(candles)
    consecutive_losses = 0
    cooldown = 0
    index = warmup

    while index < len(candles) - 2:
        if base.max_open_positions < 1:
            break
        if cooldown > 0:
            cooldown -= 1
            index += 1
            continue

        equity_base = replace(base, equity_usdc=equity) if base.compounding_enabled else base
        day = _day_key(candles[index].open_time_ms)
        day_pnl = daily.get(day, 0.0)
        if _daily_guard_reason(equity_base, day_pnl):
            index += 1
            continue

        runtime_base = _risk_adjusted_config(equity_base, day_pnl)
        runtime_config = replace(config, base=runtime_base) if runtime_base is not base else config
        signal = generate_orb_signal_at(candles, index, runtime_config, context)
        if signal.action != "PLAN_LONG":
            index += 1
            continue

        trade, next_index = _simulate_breakout(candles, index + 1, signal, _to_breakout_proxy(runtime_config))
        if trade is None:
            index += max(next_index - index, 1)
            continue

        trades.append(trade)
        exit_day = _day_key(trade.exit_time_ms)
        daily[exit_day] = daily.get(exit_day, 0.0) + trade.pnl_usdc
        equity += trade.pnl_usdc
        peak_equity = max(peak_equity, equity)
        max_drawdown = min(max_drawdown, equity - peak_equity)
        max_drawdown_pct = min(max_drawdown_pct, _drawdown_pct(equity, peak_equity))
        cooldown = max(cooldown, runtime_base.cooldown_bars)
        if trade.pnl_usdc < 0:
            consecutive_losses += 1
            if (
                base.max_consecutive_losses_before_cooldown > 0
                and consecutive_losses >= base.max_consecutive_losses_before_cooldown
            ):
                cooldown = max(cooldown, base.consecutive_loss_cooldown_bars)
                consecutive_losses = 0
        else:
            consecutive_losses = 0
        index = max(next_index, index + 1)

    summary = _summary(base, trades, max_drawdown, max_drawdown_pct, daily)
    return replace(summary, params={
        "strategy": "open_range_breakout",
        "session_start_bar": config.session_start_bar,
        "session_end_bar": config.session_end_bar,
        "opening_range_bars": config.opening_range_bars,
        "volume_lookback": config.volume_lookback,
        "min_breakout_atr": config.min_breakout_atr,
        "min_orb_range_atr": config.min_orb_range_atr,
        "stop_atr": config.stop_atr,
        "stop_buffer_atr": config.stop_buffer_atr,
        "entry_buffer_atr": config.entry_buffer_atr,
        "trail_atr": config.trail_atr,
        "min_volume_ratio": config.min_volume_ratio,
    })


def simulate_orb_short(
    candles: list[Candle],
    start_index: int,
    signal: SignalPlan,
    config: OrbConfig,
) -> tuple[TradeResult | None, int]:
    base = config.base
    if not signal.entries or signal.stop_loss is None:
        return None, start_index + 1

    entry = signal.entries[0]
    fill_index = None
    last_entry_index = min(start_index + base.entry_expiry_bars, len(candles) - 1)
    for index in range(start_index, last_entry_index + 1):
        if candles[index].high >= entry:
            fill_index = index
            break

    if fill_index is None:
        return None, last_entry_index

    qty = signal.planned_qty
    fees = qty * entry * base.maker_fee_rate
    realized = 0.0
    remaining_qty = qty
    stop = signal.stop_loss
    risk_per_unit = stop - entry
    tp_hit = [False] * len(signal.take_profits)
    exit_price = entry
    exit_reason = "max_hold"
    exit_index = min(fill_index + base.max_holding_bars, len(candles) - 1)
    lowest = entry

    for index in range(fill_index, min(fill_index + base.max_holding_bars, len(candles) - 1) + 1):
        candle = candles[index]
        lowest = min(lowest, candle.low)
        atr_value = candle.high - candle.low
        if atr_value > 0 and lowest < entry - risk_per_unit:
            stop = min(stop, lowest + atr_value * config.trail_atr)

        if candle.high >= stop:
            exit_price = stop
            fees += remaining_qty * exit_price * base.taker_fee_rate
            realized += remaining_qty * (entry - exit_price)
            remaining_qty = 0
            exit_reason = "stop_loss" if stop >= signal.stop_loss else "trailing_stop"
            exit_index = index
            break

        for tp_idx, tp in enumerate(signal.take_profits):
            if tp_hit[tp_idx] or candle.low > tp:
                continue
            qty_to_exit = min(qty * base.exit_weights[tp_idx], remaining_qty)
            if qty_to_exit <= 0:
                continue
            fees += qty_to_exit * tp * base.maker_fee_rate
            realized += qty_to_exit * (entry - tp)
            remaining_qty -= qty_to_exit
            tp_hit[tp_idx] = True
            exit_price = tp
            exit_reason = f"take_profit_{tp_idx + 1}"
            exit_index = index
            if base.breakeven_after_tp > 0 and (tp_idx + 1) >= base.breakeven_after_tp:
                stop = min(stop, entry - risk_per_unit * base.breakeven_lock_r)

        if remaining_qty <= qty * 0.001:
            remaining_qty = 0
            break

    if remaining_qty > 0:
        exit_price = candles[exit_index].close
        fees += remaining_qty * exit_price * base.taker_fee_rate
        realized += remaining_qty * (entry - exit_price)
        remaining_qty = 0

    pnl = realized - fees
    planned_risk = max(signal.risk_amount_usdc, 0.0001)
    return (
        TradeResult(
            entry_time_ms=candles[fill_index].open_time_ms,
            exit_time_ms=candles[exit_index].open_time_ms,
            entry_price=entry,
            exit_price=exit_price,
            qty=qty,
            pnl_usdc=pnl,
            fees_usdc=fees,
            r_multiple=pnl / planned_risk,
            reason=exit_reason,
            hold_bars=max(exit_index - fill_index, 0),
        ),
        exit_index + 1,
    )


def sweep_orb_configs(
    candles: list[Candle],
    base: StrategyConfig | None = None,
    profile: str = "balanced",
    template: OrbConfig | None = None,
    context: OrbContext | None = None,
) -> list[BacktestSummary]:
    base = base or OrbConfig().base
    if profile == "aggressive":
        risk_values = (1.0, 1.5, 2.0)
        orb_bars = (6, 12, 18)
        session_starts = (0, 96, 162)
        stops = (0.75, 1.0, 1.2)
        score_values = (48, 54, 60)
        volume_ratios = (0.85, 1.0, 1.15)
    elif profile == "spec":
        base = replace(
            base,
            daily_soft_loss_pct=4.5,
            daily_max_loss_pct=10.0,
            daily_loss_risk_scale=0.65,
            daily_target_stop_pct=max(base.daily_target_stop_pct, base.daily_target_min_pct),
            max_position_margin_pct=60.0,
            max_effective_leverage=35.0,
            cooldown_bars=6,
            max_consecutive_losses_before_cooldown=3,
            consecutive_loss_cooldown_bars=18,
            take_profit_r=(0.55, 1.1, 2.2),
            exit_weights=(0.25, 0.35, 0.40),
        )
        risk_values = (2.0, 2.8, 3.6)
        orb_bars = (6, 9, 12)
        session_starts = (0, 96, 162)
        stops = (0.6, 0.8, 1.0)
        score_values = (44, 50, 56)
        volume_ratios = (0.8, 0.95, 1.1)
    else:
        risk_values = (0.6, 1.0, 1.4)
        orb_bars = (6, 12, 18)
        session_starts = (0, 96, 162)
        stops = (0.9, 1.1, 1.3)
        score_values = (54, 58, 62)
        volume_ratios = (0.9, 1.0, 1.15)

    results: list[BacktestSummary] = []
    for risk, opening_range_bars, session_start_bar, stop_atr, min_score, vol_ratio in product(
        risk_values, orb_bars, session_starts, stops, score_values, volume_ratios
    ):
        cfg = OrbConfig(
            base=replace(
                base,
                risk_per_trade_pct=risk,
                min_score=min_score,
                max_holding_bars=48,
            ),
            session_start_bar=session_start_bar,
            opening_range_bars=opening_range_bars,
            stop_atr=stop_atr,
            min_volume_ratio=vol_ratio,
            require_oi_confirmation=template.require_oi_confirmation if template else False,
            min_oi_delta_pct=template.min_oi_delta_pct if template else 0.5,
            reject_extreme_funding=template.reject_extreme_funding if template else False,
            max_funding_rate=template.max_funding_rate if template else 0.0003,
        )
        runtime_context = (
            build_orb_context_with_derivatives(
                candles,
                cfg,
                oi_delta_pct_values=context.oi_delta_pct_values if context else None,
                funding_rate_values=context.funding_rate_values if context else None,
            )
            if context is not None
            else build_orb_context(candles, cfg)
        )
        results.append(run_orb_backtest_with_context(candles, cfg, runtime_context))
    return sorted(results, key=_rank_score, reverse=True)


def build_orb_context(candles: list[Candle], config: OrbConfig) -> OrbContext:
    base = config.base
    closes = [c.close for c in candles]
    atr_values = _atr_series(candles, base.atr_period)
    return OrbContext(
        recent_high_values=_prior_high_series(candles, max(config.opening_range_bars, 6)),
        avg_volume_values=_avg_volume_series(candles, config.volume_lookback),
        atr_values=atr_values,
        ema_fast_values=_ema_series(closes, base.ema_fast_period),
        ema_slow_values=_ema_series(closes, base.ema_slow_period),
        rsi_values=_rsi_series(closes, base.rsi_period),
        vwap_values=_vwap_series(candles, base.vwap_period),
        opening_range_high_values=_opening_range_high_series(candles, config),
        opening_range_low_values=_opening_range_low_series(candles, config),
        opening_range_width_atr_values=_opening_range_width_atr_series(candles, config, atr_values),
        session_bar_values=_session_bar_series(candles, config.session_start_bar, config.session_end_bar),
    )


def build_orb_context_with_derivatives(
    candles: list[Candle],
    config: OrbConfig,
    oi_delta_pct_values: list[float | None] | None = None,
    funding_rate_values: list[float | None] | None = None,
) -> OrbContext:
    context = build_orb_context(candles, config)
    return replace(
        context,
        oi_delta_pct_values=oi_delta_pct_values,
        funding_rate_values=funding_rate_values,
    )


def _opening_range_high_series(candles: list[Candle], config: OrbConfig) -> list[float | None]:
    values: list[float | None] = []
    current_day = None
    day_bar = -1
    buffer: list[Candle] = []
    opening_high = None
    for candle in candles:
        day = _day_key(candle.open_time_ms)
        if day != current_day:
            current_day = day
            day_bar = 0
            buffer = []
            opening_high = None
        else:
            day_bar += 1
        session_bar = day_bar - config.session_start_bar
        if session_bar < 0 or session_bar >= config.session_end_bar - config.session_start_bar:
            values.append(None)
            continue
        if len(buffer) < config.opening_range_bars:
            buffer.append(candle)
            if len(buffer) == config.opening_range_bars:
                opening_high = max(item.high for item in buffer)
        values.append(opening_high)
    return values


def _opening_range_low_series(candles: list[Candle], config: OrbConfig) -> list[float | None]:
    values: list[float | None] = []
    current_day = None
    day_bar = -1
    buffer: list[Candle] = []
    opening_low = None
    for candle in candles:
        day = _day_key(candle.open_time_ms)
        if day != current_day:
            current_day = day
            day_bar = 0
            buffer = []
            opening_low = None
        else:
            day_bar += 1
        session_bar = day_bar - config.session_start_bar
        if session_bar < 0 or session_bar >= config.session_end_bar - config.session_start_bar:
            values.append(None)
            continue
        if len(buffer) < config.opening_range_bars:
            buffer.append(candle)
            if len(buffer) == config.opening_range_bars:
                opening_low = min(item.low for item in buffer)
        values.append(opening_low)
    return values


def _opening_range_width_atr_series(
    candles: list[Candle],
    config: OrbConfig,
    atr_values: list[float | None],
) -> list[float | None]:
    highs = _opening_range_high_series(candles, config)
    lows = _opening_range_low_series(candles, config)
    values: list[float | None] = []
    for high, low, atr in zip(highs, lows, atr_values):
        if high is None or low is None or atr is None or atr <= 0:
            values.append(None)
        else:
            values.append((high - low) / atr)
    return values


def _session_bar_series(candles: list[Candle], session_start_bar: int = 0, session_end_bar: int = 288) -> list[int]:
    values: list[int] = []
    current_day = None
    bar = -1
    for candle in candles:
        day = _day_key(candle.open_time_ms)
        if day != current_day:
            current_day = day
            bar = 0
        else:
            bar += 1
        session_bar = bar - session_start_bar
        if session_bar < 0 or bar >= session_end_bar:
            values.append(-1)
        else:
            values.append(session_bar)
    return values

def _to_breakout_proxy(config: OrbConfig):
    from src.gridbot.strategy.long_breakout import BreakoutConfig
    return BreakoutConfig(base=config.base, trail_atr=config.trail_atr)


def _wait(config: StrategyConfig, candles: list[Candle], index: int, reason: str) -> SignalPlan:
    candle = candles[index] if 0 <= index < len(candles) else candles[-1]
    return SignalPlan(
        action="WAIT",
        confidence=0,
        score=0,
        symbol=config.symbol,
        price=candle.close,
        rsi=None,
        atr=None,
        support=None,
        vwap=None,
        daily_target_usdc=(config.daily_target_min_usdc, config.daily_target_max_usdc),
        reasons=[reason],
    )
