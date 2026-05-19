"""Long-only ETH momentum / volatility breakout engine.

This engine is pure signal/backtest logic. It never places orders.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import product

from src.gridbot.strategy.long_pullback import (
    BacktestSummary,
    Candle,
    SignalPlan,
    StrategyConfig,
    TradeResult,
    _daily_guard_reason,
    _day_key,
    _drawdown_pct,
    _empty_daily_pnls,
    _ema_series,
    _position_sizing,
    _rank_score,
    _risk_adjusted_config,
    _rsi_series,
    _summary,
    _vwap_series,
)


@dataclass(frozen=True)
class BreakoutConfig:
    base: StrategyConfig = StrategyConfig(
        risk_per_trade_pct=0.8,
        min_score=55,
        max_holding_bars=72,
        cooldown_bars=10,
        take_profit_r=(0.7, 1.3, 2.2),
        exit_weights=(0.35, 0.35, 0.30),
    )
    breakout_lookback: int = 48
    volume_lookback: int = 48
    min_breakout_atr: float = 0.08
    stop_atr: float = 1.35
    entry_buffer_atr: float = 0.05
    trail_atr: float = 1.4
    min_volume_ratio: float = 0.85
    require_oi_confirmation: bool = False
    min_oi_delta_pct: float = 0.5
    reject_extreme_funding: bool = False
    max_funding_rate: float = 0.0003


@dataclass(frozen=True)
class BreakoutContext:
    recent_high_values: list[float | None]
    avg_volume_values: list[float | None]
    atr_values: list[float | None]
    ema_fast_values: list[float | None]
    ema_slow_values: list[float | None]
    rsi_values: list[float | None]
    vwap_values: list[float | None]
    oi_delta_pct_values: list[float | None] | None = None
    funding_rate_values: list[float | None] | None = None


def generate_breakout_signal(
    candles: list[Candle],
    config: BreakoutConfig | None = None,
) -> SignalPlan:
    config = config or BreakoutConfig()
    context = build_breakout_context(candles, config)
    return generate_breakout_signal_at(candles, len(candles) - 1, config, context)


def generate_breakout_signal_at(
    candles: list[Candle],
    index: int,
    config: BreakoutConfig,
    context: BreakoutContext | None = None,
) -> SignalPlan:
    base = config.base
    warmup = max(config.breakout_lookback, config.volume_lookback, base.ema_slow_period, base.vwap_period)
    if index < warmup or index >= len(candles):
        return _wait(base, candles, index, "not enough candles")

    context = context or build_breakout_context(candles, config)
    candle = candles[index]
    price = candle.close
    atr_value = context.atr_values[index]
    recent_high = context.recent_high_values[index]
    avg_volume = context.avg_volume_values[index]
    ema_fast = context.ema_fast_values[index]
    ema_slow = context.ema_slow_values[index]
    rsi_value = context.rsi_values[index]
    vwap_value = context.vwap_values[index]
    oi_delta_pct = context.oi_delta_pct_values[index] if context.oi_delta_pct_values else None
    funding_rate = context.funding_rate_values[index] if context.funding_rate_values else None

    if atr_value is None or atr_value <= 0 or recent_high is None:
        return _wait(base, candles, index, "ATR or breakout level unavailable")

    breakout_over_atr = (price - recent_high) / atr_value
    volume_ratio = candle.volume / avg_volume if avg_volume and avg_volume > 0 else 1.0
    score = 0
    reasons: list[str] = []
    risk_notes: list[str] = []

    if price > recent_high and breakout_over_atr >= config.min_breakout_atr:
        score += 30
        reasons.append(f"close broke {config.breakout_lookback}-bar high by {breakout_over_atr:.2f} ATR")
    else:
        return SignalPlan(
            action="WAIT",
            confidence=0,
            score=0,
            symbol=base.symbol,
            price=price,
            rsi=rsi_value,
            atr=atr_value,
            support=recent_high,
            vwap=vwap_value,
            daily_target_usdc=(base.daily_target_min_usdc, base.daily_target_max_usdc),
            reasons=["no confirmed breakout"],
        )

    if ema_fast is not None and ema_slow is not None and ema_fast > ema_slow and price > ema_fast:
        score += 25
        reasons.append("EMA background supports momentum")
    elif ema_slow is not None and price > ema_slow:
        score += 12
        reasons.append("price is above slow EMA")
    else:
        risk_notes.append("trend background is not clean")

    if rsi_value is not None:
        if 52 <= rsi_value <= 72:
            score += 20
            reasons.append(f"RSI {rsi_value:.1f} confirms momentum without full exhaustion")
        elif 45 <= rsi_value < 52:
            score += 8
            reasons.append(f"RSI {rsi_value:.1f} is early momentum")
        elif rsi_value > 78:
            risk_notes.append(f"RSI {rsi_value:.1f} is overheated")

    if volume_ratio >= 1.2:
        score += 15
        reasons.append(f"volume expansion {volume_ratio:.2f}x")
    elif volume_ratio >= config.min_volume_ratio:
        score += 8
        reasons.append(f"volume acceptable {volume_ratio:.2f}x")
    else:
        risk_notes.append(f"weak volume {volume_ratio:.2f}x")

    if vwap_value is not None and price >= vwap_value:
        score += 10
        reasons.append("price is above VWAP")

    if config.require_oi_confirmation:
        if oi_delta_pct is None:
            risk_notes.append("OI unavailable")
        elif oi_delta_pct >= config.min_oi_delta_pct:
            score += 12
            reasons.append(f"OI rising {oi_delta_pct:.2f}%")
        else:
            oi_reason = "OI unavailable" if oi_delta_pct is None else f"OI confirmation failed ({oi_delta_pct:.2f}%)"
            return SignalPlan(
                action="WAIT",
                confidence=0,
                score=0,
                symbol=base.symbol,
                price=price,
                rsi=rsi_value,
                atr=atr_value,
                support=recent_high,
                vwap=vwap_value,
                daily_target_usdc=(base.daily_target_min_usdc, base.daily_target_max_usdc),
                reasons=[oi_reason],
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
                support=recent_high,
                vwap=vwap_value,
                daily_target_usdc=(base.daily_target_min_usdc, base.daily_target_max_usdc),
                reasons=[f"funding too hot ({funding_rate:.5f})"],
                risk_notes=risk_notes,
            )
        score += 5
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
            support=recent_high,
            vwap=vwap_value,
            daily_target_usdc=(base.daily_target_min_usdc, base.daily_target_max_usdc),
            reasons=reasons or ["score below threshold"],
            risk_notes=risk_notes,
        )

    entry = round(min(price * 0.999, recent_high + atr_value * config.entry_buffer_atr), 4)
    stop_loss = round(entry - atr_value * config.stop_atr, 4)
    risk_per_unit = max(entry - stop_loss, 0)
    if risk_per_unit <= 0:
        return _wait(base, candles, index, "invalid breakout stop distance")

    sizing = _position_sizing(entry, stop_loss, score, base, risk_notes)
    if sizing.planned_qty <= 0:
        return _wait(base, candles, index, "invalid breakout position sizing")

    take_profits = [entry + risk_per_unit * r for r in base.take_profit_r]

    if entry >= price:
        risk_notes.append("entry is near market; require post-breakout retest fill")
    if sizing.planned_notional_usdc > base.equity_usdc * 10:
        risk_notes.append("breakout notional is high; keep testnet-only until validated")

    return SignalPlan(
        action="PLAN_LONG",
        confidence=min(score, 100),
        score=score,
        symbol=base.symbol,
        price=price,
        rsi=rsi_value,
        atr=atr_value,
        support=recent_high,
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


def run_breakout_backtest(
    candles: list[Candle],
    config: BreakoutConfig | None = None,
) -> BacktestSummary:
    config = config or BreakoutConfig()
    context = build_breakout_context(candles, config)
    return run_breakout_backtest_with_context(candles, config, context)


def run_breakout_backtest_with_context(
    candles: list[Candle],
    config: BreakoutConfig,
    context: BreakoutContext,
) -> BacktestSummary:
    base = config.base
    warmup = max(config.breakout_lookback, config.volume_lookback, base.ema_slow_period, base.vwap_period) + 2
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
        signal = generate_breakout_signal_at(candles, index, runtime_config, context)
        if signal.action != "PLAN_LONG":
            index += 1
            continue

        trade, next_index = _simulate_breakout(candles, index + 1, signal, runtime_config)
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
        "strategy": "breakout_retest",
        "breakout_lookback": config.breakout_lookback,
        "volume_lookback": config.volume_lookback,
        "min_breakout_atr": config.min_breakout_atr,
        "stop_atr": config.stop_atr,
        "entry_buffer_atr": config.entry_buffer_atr,
        "trail_atr": config.trail_atr,
        "min_volume_ratio": config.min_volume_ratio,
        "accelerator_min_score": base.accelerator_min_score,
        "accelerator_risk": base.accelerator_risk_per_trade_pct,
        "accelerator_margin_pct": base.accelerator_margin_pct,
        "accelerator_max_leverage": base.accelerator_max_effective_leverage,
    })


def sweep_breakout_configs(
    candles: list[Candle],
    base: StrategyConfig | None = None,
    profile: str = "balanced",
) -> list[BacktestSummary]:
    return sweep_breakout_configs_with_context(
        candles,
        base or BreakoutConfig().base,
        None,
        profile=profile,
        template=None,
    )


def sweep_breakout_configs_with_context(
    candles: list[Candle],
    base: StrategyConfig,
    context: BreakoutContext | None,
    profile: str = "balanced",
    template: BreakoutConfig | None = None,
) -> list[BacktestSummary]:
    base = base or BreakoutConfig().base
    if profile == "aggressive":
        risk_values = (1.0, 1.5, 2.0)
        lookbacks = (24, 36, 48)
        stop_values = (0.9, 1.1, 1.35)
        score_values = (45, 50, 55)
        volume_ratios = (0.75, 0.9, 1.1)
        accelerator_sets = (
            (base.accelerator_min_score, base.accelerator_risk_per_trade_pct, base.accelerator_margin_pct, base.accelerator_max_effective_leverage),
        )
    elif profile == "spec":
        base = replace(
            base,
            daily_soft_loss_pct=4.0,
            daily_max_loss_pct=10.0,
            daily_loss_risk_scale=0.60,
            daily_target_stop_pct=max(base.daily_target_stop_pct, base.daily_target_min_pct),
            max_position_margin_pct=55.0,
            max_effective_leverage=30.0,
            max_consecutive_losses_before_cooldown=3,
            consecutive_loss_cooldown_bars=24,
            cooldown_bars=6,
            take_profit_r=(0.45, 0.9, 1.8),
            exit_weights=(0.25, 0.35, 0.40),
        )
        risk_values = (2.0, 2.8, 3.6)
        lookbacks = (18, 24, 36)
        stop_values = (0.65, 0.8, 0.95)
        score_values = (40, 45, 50)
        volume_ratios = (0.55, 0.7, 0.85)
        accelerator_sets = (
            (70, 0.7, 10.0, 35.0),
            (75, 1.0, 12.0, 40.0),
            (80, 1.3, 15.0, 45.0),
        )
    else:
        risk_values = (0.5, 0.8, 1.0)
        lookbacks = (36, 48, 72)
        stop_values = (1.1, 1.35, 1.6)
        score_values = (50, 55, 60)
        volume_ratios = (0.85, 1.0, 1.2)
        accelerator_sets = (
            (base.accelerator_min_score, base.accelerator_risk_per_trade_pct, base.accelerator_margin_pct, base.accelerator_max_effective_leverage),
        )

    results: list[BacktestSummary] = []
    for risk, lookback, stop, min_score, vol_ratio, accelerator in product(
        risk_values,
        lookbacks,
        stop_values,
        score_values,
        volume_ratios,
        accelerator_sets,
    ):
        accelerator_score, accelerator_risk, accelerator_margin, accelerator_leverage = accelerator
        cfg = BreakoutConfig(
            base=replace(
                base,
                risk_per_trade_pct=risk,
                min_score=min_score,
                max_holding_bars=72,
                accelerator_min_score=accelerator_score,
                accelerator_risk_per_trade_pct=accelerator_risk,
                accelerator_margin_pct=accelerator_margin,
                accelerator_max_effective_leverage=accelerator_leverage,
            ),
            breakout_lookback=lookback,
            volume_lookback=lookback,
            stop_atr=stop,
            min_volume_ratio=vol_ratio,
            require_oi_confirmation=template.require_oi_confirmation if template else False,
            min_oi_delta_pct=template.min_oi_delta_pct if template else 0.5,
            reject_extreme_funding=template.reject_extreme_funding if template else False,
            max_funding_rate=template.max_funding_rate if template else 0.0003,
        )
        if context is None:
            results.append(run_breakout_backtest(candles, cfg))
        else:
            results.append(run_breakout_backtest_with_context(candles, cfg, context))
    return sorted(results, key=_rank_score, reverse=True)


def build_breakout_context(candles: list[Candle], config: BreakoutConfig) -> BreakoutContext:
    base = config.base
    closes = [c.close for c in candles]
    return BreakoutContext(
        recent_high_values=_prior_high_series(candles, config.breakout_lookback),
        avg_volume_values=_avg_volume_series(candles, config.volume_lookback),
        atr_values=_atr_series(candles, base.atr_period),
        ema_fast_values=_ema_series(closes, base.ema_fast_period),
        ema_slow_values=_ema_series(closes, base.ema_slow_period),
        rsi_values=_rsi_series(closes, base.rsi_period),
        vwap_values=_vwap_series(candles, base.vwap_period),
    )


def build_breakout_context_with_derivatives(
    candles: list[Candle],
    config: BreakoutConfig,
    oi_delta_pct_values: list[float | None] | None = None,
    funding_rate_values: list[float | None] | None = None,
) -> BreakoutContext:
    context = build_breakout_context(candles, config)
    return replace(
        context,
        oi_delta_pct_values=oi_delta_pct_values,
        funding_rate_values=funding_rate_values,
    )


def _simulate_breakout(
    candles: list[Candle],
    start_index: int,
    signal: SignalPlan,
    config: BreakoutConfig,
) -> tuple[TradeResult | None, int]:
    base = config.base
    if not signal.entries or signal.stop_loss is None:
        return None, start_index + 1

    entry = signal.entries[0]
    fill_index = None
    last_entry_index = min(start_index + base.entry_expiry_bars, len(candles) - 1)
    for index in range(start_index, last_entry_index + 1):
        if candles[index].low <= entry:
            fill_index = index
            break

    if fill_index is None:
        return None, last_entry_index

    qty = signal.planned_qty
    fees = qty * entry * base.maker_fee_rate
    realized = 0.0
    remaining_qty = qty
    stop = signal.stop_loss
    risk_per_unit = entry - stop
    tp_hit = [False] * len(signal.take_profits)
    exit_price = entry
    exit_reason = "max_hold"
    exit_index = min(fill_index + base.max_holding_bars, len(candles) - 1)
    highest = entry

    for index in range(fill_index, min(fill_index + base.max_holding_bars, len(candles) - 1) + 1):
        candle = candles[index]
        highest = max(highest, candle.high)
        atr_value = (candle.high - candle.low)
        if atr_value > 0 and highest > entry + risk_per_unit:
            stop = max(stop, highest - atr_value * config.trail_atr)

        if candle.low <= stop:
            exit_price = stop
            fees += remaining_qty * exit_price * base.taker_fee_rate
            realized += remaining_qty * (exit_price - entry)
            remaining_qty = 0
            exit_reason = "stop_loss" if stop <= signal.stop_loss else "trailing_stop"
            exit_index = index
            break

        for tp_idx, tp in enumerate(signal.take_profits):
            if tp_hit[tp_idx] or candle.high < tp:
                continue
            qty_to_exit = min(qty * base.exit_weights[tp_idx], remaining_qty)
            if qty_to_exit <= 0:
                continue
            fees += qty_to_exit * tp * base.maker_fee_rate
            realized += qty_to_exit * (tp - entry)
            remaining_qty -= qty_to_exit
            tp_hit[tp_idx] = True
            exit_price = tp
            exit_reason = f"take_profit_{tp_idx + 1}"
            exit_index = index
            if base.breakeven_after_tp > 0 and (tp_idx + 1) >= base.breakeven_after_tp:
                stop = max(stop, entry + risk_per_unit * base.breakeven_lock_r)

        if remaining_qty <= qty * 0.001:
            remaining_qty = 0
            break

    if remaining_qty > 0:
        exit_price = candles[exit_index].close
        fees += remaining_qty * exit_price * base.taker_fee_rate
        realized += remaining_qty * (exit_price - entry)
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


def _prior_high_series(candles: list[Candle], lookback: int) -> list[float | None]:
    series: list[float | None] = [None] * len(candles)
    for index in range(lookback, len(candles)):
        series[index] = max(c.high for c in candles[index - lookback:index])
    return series


def _avg_volume_series(candles: list[Candle], lookback: int) -> list[float | None]:
    series: list[float | None] = [None] * len(candles)
    volume_sum = 0.0
    for index, candle in enumerate(candles):
        volume_sum += candle.volume
        if index >= lookback:
            volume_sum -= candles[index - lookback].volume
        if index >= lookback:
            series[index] = volume_sum / lookback
    return series


def _atr_series(candles: list[Candle], period: int) -> list[float | None]:
    series: list[float | None] = [None] * len(candles)
    if len(candles) <= period:
        return series
    true_ranges = [0.0] * len(candles)
    for index in range(1, len(candles)):
        current = candles[index]
        previous = candles[index - 1]
        true_ranges[index] = max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
    rolling = sum(true_ranges[1: period + 1])
    series[period] = rolling / period
    for index in range(period + 1, len(candles)):
        rolling += true_ranges[index] - true_ranges[index - period]
        series[index] = rolling / period
    return series


def _wait(base: StrategyConfig, candles: list[Candle], index: int, reason: str) -> SignalPlan:
    price = candles[index].close if 0 <= index < len(candles) else 0.0
    return SignalPlan(
        action="WAIT",
        confidence=0,
        score=0,
        symbol=base.symbol,
        price=price,
        rsi=None,
        atr=None,
        support=None,
        vwap=None,
        daily_target_usdc=(base.daily_target_min_usdc, base.daily_target_max_usdc),
        reasons=[reason],
    )
