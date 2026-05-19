"""Maker-first range scalping backtest for USDC pairs."""

from __future__ import annotations

from dataclasses import dataclass

from src.gridbot.strategy.long_pullback import (
    BacktestSummary,
    Candle,
    StrategyConfig,
    TradeResult,
    _atr_series,
    _daily_guard_reason,
    _day_key,
    _drawdown_pct,
    _ema_series,
    _empty_daily_pnls,
    _risk_adjusted_config,
    _summary,
    _vwap_series,
)


@dataclass(frozen=True)
class MakerGridConfig:
    base: StrategyConfig = StrategyConfig()
    side: str = "both"
    lookback_bars: int = 72
    spacing_atr: float = 0.22
    take_profit_atr: float = 0.30
    stop_atr: float = 1.10
    entry_expiry_bars: int = 6
    max_holding_bars: int = 24
    min_range_width_atr: float = 1.20
    max_range_width_atr: float = 7.50
    max_ema_spread_atr: float = 1.15
    min_volume_ratio: float = 0.40


def run_maker_grid_backtest(candles: list[Candle], config: MakerGridConfig) -> BacktestSummary:
    base = config.base
    closes = [candle.close for candle in candles]
    volumes = [candle.volume for candle in candles]
    atr_values = _atr_series(candles, base.atr_period)
    vwap_values = _vwap_series(candles, base.vwap_period)
    ema_fast = _ema_series(closes, base.ema_fast_period)
    ema_slow = _ema_series(closes, base.ema_slow_period)
    avg_volume = _sma_series(volumes, base.vwap_period)
    warmup = max(base.atr_period, base.vwap_period, base.ema_slow_period, config.lookback_bars) + 2

    trades: list[TradeResult] = []
    equity = base.equity_usdc
    peak_equity = equity
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    daily = _empty_daily_pnls(candles)
    cooldown = 0
    consecutive_losses = 0
    index = warmup

    while index < len(candles) - 2:
        if base.max_open_positions < 1:
            break
        if cooldown > 0:
            cooldown -= 1
            index += 1
            continue

        equity_base = _equity_base(base, equity)
        day = _day_key(candles[index].open_time_ms)
        day_pnl = daily.get(day, 0.0)
        if _daily_guard_reason(equity_base, day_pnl):
            index += 1
            continue

        runtime_base = _risk_adjusted_config(equity_base, day_pnl)
        side = _select_grid_side(candles, index, config, atr_values, vwap_values, ema_fast, ema_slow, avg_volume)
        if side is None:
            index += 1
            continue

        trade, next_index = _simulate_grid_trade(candles, index, runtime_base, config, side, atr_values[index])
        if trade is None:
            index = max(next_index, index + 1)
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
    summary.params.update(
        {
            "grid_side": config.side,
            "grid_spacing_atr": config.spacing_atr,
            "grid_take_profit_atr": config.take_profit_atr,
            "grid_stop_atr": config.stop_atr,
            "grid_max_holding_bars": config.max_holding_bars,
            "grid_min_range_width_atr": config.min_range_width_atr,
            "grid_max_range_width_atr": config.max_range_width_atr,
            "grid_max_ema_spread_atr": config.max_ema_spread_atr,
        }
    )
    return summary


def _select_grid_side(
    candles: list[Candle],
    index: int,
    config: MakerGridConfig,
    atr_values: list[float | None],
    vwap_values: list[float | None],
    ema_fast: list[float | None],
    ema_slow: list[float | None],
    avg_volume: list[float | None],
) -> str | None:
    candle = candles[index]
    atr = atr_values[index]
    vwap = vwap_values[index]
    fast = ema_fast[index]
    slow = ema_slow[index]
    volume_base = avg_volume[index]
    if atr is None or atr <= 0 or vwap is None or fast is None or slow is None or volume_base is None or volume_base <= 0:
        return None
    volume_ratio = candle.volume / volume_base
    if volume_ratio < config.min_volume_ratio:
        return None
    start = max(0, index - config.lookback_bars + 1)
    high = max(c.high for c in candles[start : index + 1])
    low = min(c.low for c in candles[start : index + 1])
    width_atr = (high - low) / atr
    if width_atr < config.min_range_width_atr or width_atr > config.max_range_width_atr:
        return None
    if abs(fast - slow) / atr > config.max_ema_spread_atr:
        return None

    side_pref = config.side.lower()
    if side_pref in {"long", "short"}:
        return side_pref
    if candle.close <= vwap:
        return "long"
    return "short"


def _simulate_grid_trade(
    candles: list[Candle],
    signal_index: int,
    base: StrategyConfig,
    config: MakerGridConfig,
    side: str,
    atr: float | None,
) -> tuple[TradeResult | None, int]:
    if atr is None or atr <= 0:
        return None, signal_index + 1
    signal_close = candles[signal_index].close
    if side == "long":
        entry = signal_close - config.spacing_atr * atr
        take_profit = entry + config.take_profit_atr * atr
        stop = entry - config.stop_atr * atr
        fill_check = lambda candle: candle.low <= entry
        stop_check = lambda candle: candle.low <= stop
        tp_check = lambda candle: candle.high >= take_profit
    else:
        entry = signal_close + config.spacing_atr * atr
        take_profit = entry - config.take_profit_atr * atr
        stop = entry + config.stop_atr * atr
        fill_check = lambda candle: candle.high >= entry
        stop_check = lambda candle: candle.high >= stop
        tp_check = lambda candle: candle.low <= take_profit

    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        return None, signal_index + 1
    qty = _position_qty(base, entry, stop_distance)
    if qty <= 0:
        return None, signal_index + 1

    fill_index = None
    expiry_end = min(len(candles) - 1, signal_index + config.entry_expiry_bars)
    for index in range(signal_index + 1, expiry_end + 1):
        if fill_check(candles[index]):
            fill_index = index
            break
    if fill_index is None:
        return None, expiry_end + 1

    exit_price = None
    exit_index = fill_index
    reason = "max_hold"
    max_exit = min(len(candles) - 1, fill_index + config.max_holding_bars)
    for index in range(fill_index, max_exit + 1):
        candle = candles[index]
        if stop_check(candle):
            exit_price = stop
            exit_index = index
            reason = "stop_loss"
            break
        if index > fill_index and tp_check(candle):
            exit_price = take_profit
            exit_index = index
            reason = "take_profit"
            break
    if exit_price is None:
        exit_price = candles[max_exit].close
        exit_index = max_exit

    gross = (exit_price - entry) * qty if side == "long" else (entry - exit_price) * qty
    exit_fee_rate = base.maker_fee_rate if reason == "take_profit" else base.taker_fee_rate
    fees = entry * qty * base.maker_fee_rate + exit_price * qty * exit_fee_rate
    pnl = gross - fees
    risk_amount = max(base.risk_amount_usdc, 1e-9)
    return (
        TradeResult(
            entry_time_ms=candles[fill_index].open_time_ms,
            exit_time_ms=candles[exit_index].open_time_ms,
            entry_price=entry,
            exit_price=exit_price,
            qty=qty,
            pnl_usdc=pnl,
            fees_usdc=fees,
            r_multiple=pnl / risk_amount,
            reason=f"grid_{side}_{reason}",
            hold_bars=exit_index - fill_index,
        ),
        exit_index + 1,
    )


def _position_qty(base: StrategyConfig, entry: float, stop_distance: float) -> float:
    risk_qty = base.risk_amount_usdc / stop_distance
    max_margin_notional = base.equity_usdc * base.max_position_margin_pct / 100 * base.max_effective_leverage
    return min(risk_qty, max_margin_notional / entry)


def _equity_base(base: StrategyConfig, equity: float) -> StrategyConfig:
    if not base.compounding_enabled:
        return base
    from dataclasses import replace

    return replace(base, equity_usdc=max(equity, 0.0))


def _sma_series(values: list[float], period: int) -> list[float | None]:
    series: list[float | None] = [None] * len(values)
    if period <= 0:
        return series
    rolling = 0.0
    for index, value in enumerate(values):
        rolling += value
        if index >= period:
            rolling -= values[index - period]
        if index >= period - 1:
            series[index] = rolling / period
    return series
