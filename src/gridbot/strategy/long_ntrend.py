"""Long-only N-trend continuation engine using MA20 structure."""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import product

from src.gridbot.strategy.long_pullback import (
    BacktestSummary,
    Candle,
    SignalPlan,
    StrategyConfig,
    TradeResult,
    _atr_series,
    _daily_guard_reason,
    _day_key,
    _drawdown_pct,
    _empty_daily_pnls,
    _position_sizing,
    _rank_score,
    _risk_adjusted_config,
    _rsi_series,
    _summary as _base_summary,
)


@dataclass(frozen=True)
class NTrendConfig:
    base: StrategyConfig = StrategyConfig(
        risk_per_trade_pct=0.9,
        min_score=58,
        max_holding_bars=84,
        cooldown_bars=8,
        take_profit_r=(0.6, 1.1, 1.9),
        exit_weights=(0.30, 0.35, 0.35),
    )
    ma_period: int = 20
    pattern_lookback: int = 36
    min_breakout_atr: float = 0.05
    min_retrace_ratio: float = 0.18
    max_retrace_ratio: float = 0.68
    min_ma_slope_atr: float = 0.03
    stop_atr: float = 1.0
    stop_buffer_atr: float = 0.25
    entry_buffer_atr: float = 0.03
    trail_atr: float = 1.2
    min_volume_ratio: float = 0.9


@dataclass(frozen=True)
class NTrendContext:
    ma_values: list[float | None]
    atr_values: list[float | None]
    rsi_values: list[float | None]
    avg_volume_values: list[float | None]


def generate_ntrend_signal(candles: list[Candle], config: NTrendConfig | None = None) -> SignalPlan:
    config = config or NTrendConfig()
    context = build_ntrend_context(candles, config)
    return generate_ntrend_signal_at(candles, len(candles) - 1, config, context)


def generate_ntrend_signal_at(
    candles: list[Candle],
    index: int,
    config: NTrendConfig,
    context: NTrendContext | None = None,
) -> SignalPlan:
    base = config.base
    warmup = max(config.pattern_lookback, config.ma_period + 3, base.atr_period + 1, base.rsi_period + 1)
    if index < warmup or index >= len(candles):
        return _wait(base, candles, index, "not enough candles")

    context = context or build_ntrend_context(candles, config)
    candle = candles[index]
    price = candle.close
    atr_value = context.atr_values[index]
    ma_value = context.ma_values[index]
    prev_ma = context.ma_values[index - 3] if index >= 3 else None
    rsi_value = context.rsi_values[index]
    avg_volume = context.avg_volume_values[index]

    if atr_value is None or atr_value <= 0 or ma_value is None or prev_ma is None:
        return _wait(base, candles, index, "trend context unavailable")

    pattern = _find_n_pattern(candles, index, config)
    if pattern is None:
        return SignalPlan(
            action="WAIT",
            confidence=0,
            score=0,
            symbol=base.symbol,
            price=price,
            rsi=rsi_value,
            atr=atr_value,
            support=ma_value,
            vwap=None,
            daily_target_usdc=(base.daily_target_min_usdc, base.daily_target_max_usdc),
            reasons=["no valid N-trend continuation"],
        )

    anchor_low_idx, impulse_high_idx, pullback_low_idx = pattern
    anchor_low = candles[anchor_low_idx].low
    impulse_high = candles[impulse_high_idx].high
    pullback_low = candles[pullback_low_idx].low
    impulse_range = impulse_high - anchor_low
    if impulse_range <= 0:
        return _wait(base, candles, index, "invalid pattern range")

    breakout_over_atr = (price - impulse_high) / atr_value
    retrace_ratio = (impulse_high - pullback_low) / impulse_range
    ma_slope_atr = (ma_value - prev_ma) / atr_value
    volume_ratio = candle.volume / avg_volume if avg_volume and avg_volume > 0 else 1.0

    score = 0
    reasons: list[str] = []
    risk_notes: list[str] = []

    if price > impulse_high and breakout_over_atr >= config.min_breakout_atr:
        score += 28
        reasons.append(f"N breakout cleared prior high by {breakout_over_atr:.2f} ATR")
    else:
        return SignalPlan(
            action="WAIT",
            confidence=0,
            score=0,
            symbol=base.symbol,
            price=price,
            rsi=rsi_value,
            atr=atr_value,
            support=pullback_low,
            vwap=None,
            daily_target_usdc=(base.daily_target_min_usdc, base.daily_target_max_usdc),
            reasons=["second leg has not confirmed"],
        )

    if price >= ma_value:
        score += 18
        reasons.append("price is above MA20")
    else:
        risk_notes.append("price slipped under MA20")

    if ma_slope_atr >= config.min_ma_slope_atr:
        score += 20
        reasons.append("MA20 slope is rising")
    else:
        risk_notes.append("MA20 slope is flat")

    if config.min_retrace_ratio <= retrace_ratio <= config.max_retrace_ratio:
        score += 18
        reasons.append(f"pullback retraced {retrace_ratio:.2f} of the impulse")
    elif retrace_ratio < config.min_retrace_ratio:
        risk_notes.append(f"pullback is shallow ({retrace_ratio:.2f})")
    else:
        risk_notes.append(f"pullback is too deep ({retrace_ratio:.2f})")

    if pullback_low > ma_value - atr_value * 0.6:
        score += 10
        reasons.append("pullback held near MA20")
    else:
        risk_notes.append("pullback stretched below MA20 support zone")

    if rsi_value is not None:
        if 50 <= rsi_value <= 68:
            score += 12
            reasons.append(f"RSI {rsi_value:.1f} confirms trend continuation")
        elif 45 <= rsi_value < 50:
            score += 5
            reasons.append(f"RSI {rsi_value:.1f} is rebuilding momentum")
        elif rsi_value > 75:
            risk_notes.append(f"RSI {rsi_value:.1f} is hot")

    if volume_ratio >= 1.15:
        score += 10
        reasons.append(f"volume expansion {volume_ratio:.2f}x")
    elif volume_ratio >= config.min_volume_ratio:
        score += 5
        reasons.append(f"volume acceptable {volume_ratio:.2f}x")
    else:
        risk_notes.append(f"volume is thin {volume_ratio:.2f}x")

    if score < base.min_score:
        return SignalPlan(
            action="WAIT",
            confidence=min(score, 100),
            score=score,
            symbol=base.symbol,
            price=price,
            rsi=rsi_value,
            atr=atr_value,
            support=pullback_low,
            vwap=ma_value,
            daily_target_usdc=(base.daily_target_min_usdc, base.daily_target_max_usdc),
            reasons=reasons or ["score below threshold"],
            risk_notes=risk_notes,
        )

    entry = round(min(price * 0.999, impulse_high + atr_value * config.entry_buffer_atr), 4)
    stop_loss = round(min(pullback_low - atr_value * config.stop_buffer_atr, ma_value - atr_value * config.stop_atr), 4)
    if stop_loss >= entry:
        stop_loss = round(entry - atr_value * max(config.stop_buffer_atr, 0.5), 4)
    sizing = _position_sizing(entry, stop_loss, score, base, risk_notes)
    if sizing.planned_qty <= 0:
        return _wait(base, candles, index, "invalid N-trend position sizing")

    risk_per_unit = entry - stop_loss
    take_profits = [entry + risk_per_unit * r for r in base.take_profit_r]
    return SignalPlan(
        action="PLAN_LONG",
        confidence=min(score, 100),
        score=score,
        symbol=base.symbol,
        price=price,
        rsi=rsi_value,
        atr=atr_value,
        support=pullback_low,
        vwap=ma_value,
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


def run_ntrend_backtest(candles: list[Candle], config: NTrendConfig | None = None) -> BacktestSummary:
    config = config or NTrendConfig()
    context = build_ntrend_context(candles, config)
    base = config.base
    warmup = max(config.pattern_lookback, config.ma_period + 3, base.atr_period + 1, base.rsi_period + 1) + 2
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
        signal = generate_ntrend_signal_at(candles, index, runtime_config, context)
        if signal.action != "PLAN_LONG":
            index += 1
            continue

        trade, next_index = _simulate_ntrend(candles, index + 1, signal, runtime_config)
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

    summary = _base_summary(base, trades, max_drawdown, max_drawdown_pct, daily)
    return replace(summary, params={
        "strategy": "ntrend_ma20",
        "ma_period": config.ma_period,
        "pattern_lookback": config.pattern_lookback,
        "min_breakout_atr": config.min_breakout_atr,
        "min_retrace_ratio": config.min_retrace_ratio,
        "max_retrace_ratio": config.max_retrace_ratio,
        "min_ma_slope_atr": config.min_ma_slope_atr,
        "min_volume_ratio": config.min_volume_ratio,
    })


def sweep_ntrend_configs(
    candles: list[Candle],
    base: StrategyConfig | None = None,
    profile: str = "balanced",
) -> list[BacktestSummary]:
    base = base or NTrendConfig().base
    if profile == "aggressive":
        risk_values = (1.2, 1.8, 2.4)
        lookbacks = (24, 30, 36)
        retraces = ((0.15, 0.60), (0.18, 0.68), (0.22, 0.75))
        scores = (52, 56, 60)
    elif profile == "spec":
        base = replace(
            base,
            daily_soft_loss_pct=4.0,
            daily_max_loss_pct=10.0,
            daily_loss_risk_scale=0.60,
            daily_target_stop_pct=max(base.daily_target_stop_pct, base.daily_target_min_pct),
            max_position_margin_pct=50.0,
            max_effective_leverage=28.0,
            take_profit_r=(0.5, 0.95, 1.7),
            exit_weights=(0.25, 0.35, 0.40),
        )
        risk_values = (2.0, 2.8, 3.6)
        lookbacks = (24, 30, 42)
        retraces = ((0.15, 0.55), (0.18, 0.68), (0.22, 0.75))
        scores = (48, 52, 56)
    else:
        risk_values = (0.6, 0.9, 1.2)
        lookbacks = (30, 36, 42)
        retraces = ((0.18, 0.60), (0.18, 0.68), (0.22, 0.72))
        scores = (56, 60, 64)

    results: list[BacktestSummary] = []
    for risk, lookback, retrace, min_score in product(risk_values, lookbacks, retraces, scores):
        retrace_min, retrace_max = retrace
        cfg = NTrendConfig(
            base=replace(base, risk_per_trade_pct=risk, min_score=min_score, max_holding_bars=84),
            pattern_lookback=lookback,
            min_retrace_ratio=retrace_min,
            max_retrace_ratio=retrace_max,
        )
        results.append(run_ntrend_backtest(candles, cfg))
    return sorted(results, key=_rank_score, reverse=True)


def build_ntrend_context(candles: list[Candle], config: NTrendConfig) -> NTrendContext:
    base = config.base
    closes = [c.close for c in candles]
    return NTrendContext(
        ma_values=_sma_series(closes, config.ma_period),
        atr_values=_atr_series(candles, base.atr_period),
        rsi_values=_rsi_series(closes, base.rsi_period),
        avg_volume_values=_avg_volume_series(candles, max(config.ma_period, 12)),
    )


def _simulate_ntrend(
    candles: list[Candle],
    start_index: int,
    signal: SignalPlan,
    config: NTrendConfig,
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
        bar_range = candle.high - candle.low
        if bar_range > 0 and highest > entry + risk_per_unit:
            stop = max(stop, highest - bar_range * config.trail_atr)

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

        if remaining_qty <= qty * 0.001:
            remaining_qty = 0
            break

    if remaining_qty > 0:
        exit_price = candles[exit_index].close
        fees += remaining_qty * exit_price * base.taker_fee_rate
        realized += remaining_qty * (exit_price - entry)

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


def _find_n_pattern(candles: list[Candle], index: int, config: NTrendConfig) -> tuple[int, int, int] | None:
    start = max(1, index - config.pattern_lookback)
    if index - start < 6:
        return None

    anchor_low_idx = min(range(start, index - 4), key=lambda i: candles[i].low)
    impulse_high_start = anchor_low_idx + 2
    impulse_high_end = index - 2
    if impulse_high_end - impulse_high_start < 2:
        return None
    impulse_high_idx = max(range(impulse_high_start, impulse_high_end + 1), key=lambda i: candles[i].high)
    if impulse_high_idx >= index - 1:
        return None

    pullback_start = impulse_high_idx + 1
    pullback_end = index - 1
    if pullback_end - pullback_start < 1:
        return None
    pullback_low_idx = min(range(pullback_start, pullback_end + 1), key=lambda i: candles[i].low)

    anchor_low = candles[anchor_low_idx].low
    impulse_high = candles[impulse_high_idx].high
    pullback_low = candles[pullback_low_idx].low
    if pullback_low <= anchor_low:
        return None
    if impulse_high <= max(candles[impulse_high_idx - 1].high, candles[impulse_high_idx - 2].high):
        return None
    return anchor_low_idx, impulse_high_idx, pullback_low_idx


def _avg_volume_series(candles: list[Candle], period: int) -> list[float | None]:
    series: list[float | None] = [None] * len(candles)
    running = 0.0
    for index, candle in enumerate(candles):
        running += candle.volume
        if index >= period:
            running -= candles[index - period].volume
        if index >= period - 1:
            series[index] = running / period
    return series


def _sma_series(values: list[float], period: int) -> list[float | None]:
    series: list[float | None] = [None] * len(values)
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= period:
            running -= values[index - period]
        if index >= period - 1:
            series[index] = running / period
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
