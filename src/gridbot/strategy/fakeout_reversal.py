"""Opening-range fakeout reversal backtest."""

from __future__ import annotations

from dataclasses import dataclass

from src.gridbot.strategy.long_breakout import _simulate_breakout
from src.gridbot.strategy.long_orb import (
    OrbConfig,
    SignalPlan,
    build_orb_context,
    simulate_orb_short,
    _to_breakout_proxy,
)
from src.gridbot.strategy.long_pullback import (
    BacktestSummary,
    Candle,
    StrategyConfig,
    TradeResult,
    _daily_guard_reason,
    _day_key,
    _drawdown_pct,
    _empty_daily_pnls,
    _position_sizing,
    _risk_adjusted_config,
    _summary,
)


@dataclass(frozen=True)
class FakeoutReversalConfig:
    orb: OrbConfig = OrbConfig()
    side: str = "both"
    min_probe_atr: float = 0.10
    max_close_outside_atr: float = 0.03
    min_wick_ratio: float = 0.38
    min_volume_ratio: float = 0.65
    min_orb_width_atr: float = 0.45
    max_orb_width_atr: float = 4.50
    reject_strong_trend: bool = True


def run_fakeout_reversal_backtest(candles: list[Candle], config: FakeoutReversalConfig) -> BacktestSummary:
    orb = config.orb
    base = orb.base
    context = build_orb_context(candles, orb)
    warmup = max(orb.volume_lookback, base.ema_slow_period, base.vwap_period, orb.opening_range_bars) + 2
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
        runtime_orb = orb if runtime_base is base else _replace_base(orb, runtime_base)
        signal = generate_fakeout_reversal_signal_at(candles, index, _replace_base(config, runtime_base), context)
        if signal.action == "WAIT":
            index += 1
            continue

        if signal.action == "PLAN_SHORT":
            trade, next_index = simulate_orb_short(candles, index + 1, signal, runtime_orb)
        else:
            trade, next_index = _simulate_breakout(candles, index + 1, signal, _to_breakout_proxy(runtime_orb))
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
            "fakeout_side": config.side,
            "fakeout_min_probe_atr": config.min_probe_atr,
            "fakeout_max_close_outside_atr": config.max_close_outside_atr,
            "fakeout_min_wick_ratio": config.min_wick_ratio,
            "fakeout_min_volume_ratio": config.min_volume_ratio,
            "fakeout_min_orb_width_atr": config.min_orb_width_atr,
            "fakeout_max_orb_width_atr": config.max_orb_width_atr,
        }
    )
    return summary


def generate_fakeout_reversal_signal_at(
    candles: list[Candle],
    index: int,
    config: FakeoutReversalConfig,
    context=None,
) -> SignalPlan:
    orb = config.orb
    base = orb.base
    warmup = max(orb.volume_lookback, base.ema_slow_period, base.vwap_period, orb.opening_range_bars)
    if index < warmup or index >= len(candles):
        return _wait(base, candles, index, "not enough candles")

    context = context or build_orb_context(candles, orb)
    candle = candles[index]
    atr = context.atr_values[index]
    avg_volume = context.avg_volume_values[index]
    ema_fast = context.ema_fast_values[index]
    ema_slow = context.ema_slow_values[index]
    rsi = context.rsi_values[index]
    vwap = context.vwap_values[index]
    orb_high = context.opening_range_high_values[index] if context.opening_range_high_values else None
    orb_low = context.opening_range_low_values[index] if context.opening_range_low_values else None
    orb_width_atr = context.opening_range_width_atr_values[index] if context.opening_range_width_atr_values else None
    session_bar = context.session_bar_values[index] if context.session_bar_values else 0
    if atr is None or atr <= 0 or orb_high is None or orb_low is None or orb_width_atr is None:
        return _wait(base, candles, index, "fakeout context unavailable")
    if session_bar < orb.opening_range_bars:
        return _wait(base, candles, index, "opening range not finished")
    if orb_width_atr < config.min_orb_width_atr or orb_width_atr > config.max_orb_width_atr:
        return _wait(base, candles, index, "opening range width outside fakeout band")

    candle_range = max(candle.high - candle.low, 0.0001)
    upper_wick = (candle.high - max(candle.open, candle.close)) / candle_range
    lower_wick = (min(candle.open, candle.close) - candle.low) / candle_range
    close_position = (candle.close - candle.low) / candle_range
    volume_ratio = candle.volume / avg_volume if avg_volume and avg_volume > 0 else 1.0
    if volume_ratio < config.min_volume_ratio:
        return _wait(base, candles, index, "fakeout volume too thin")

    side_pref = config.side.lower()
    upper_probe_atr = (candle.high - orb_high) / atr
    lower_probe_atr = (orb_low - candle.low) / atr
    closed_back_below_high = candle.close <= orb_high + config.max_close_outside_atr * atr
    closed_back_above_low = candle.close >= orb_low - config.max_close_outside_atr * atr

    short_signal = (
        side_pref in {"both", "short"}
        and upper_probe_atr >= config.min_probe_atr
        and closed_back_below_high
        and upper_wick >= config.min_wick_ratio
        and close_position <= 0.55
    )
    long_signal = (
        side_pref in {"both", "long"}
        and lower_probe_atr >= config.min_probe_atr
        and closed_back_above_low
        and lower_wick >= config.min_wick_ratio
        and close_position >= 0.45
    )
    if short_signal and _reject_short_trend(candle.close, ema_fast, ema_slow, rsi, config):
        short_signal = False
    if long_signal and _reject_long_trend(candle.close, ema_fast, ema_slow, rsi, config):
        long_signal = False
    if not short_signal and not long_signal:
        return _wait(base, candles, index, "no OR fakeout reversal")

    if short_signal and (not long_signal or upper_probe_atr >= lower_probe_atr):
        return _short_signal(candles, index, base, candle, atr, rsi, vwap, orb_high, orb_low, upper_probe_atr, upper_wick, volume_ratio)
    return _long_signal(candles, index, base, candle, atr, rsi, vwap, orb_high, orb_low, lower_probe_atr, lower_wick, volume_ratio)


def _short_signal(
    candles: list[Candle],
    index: int,
    base: StrategyConfig,
    candle: Candle,
    atr: float,
    rsi: float | None,
    vwap: float | None,
    orb_high: float,
    orb_low: float,
    probe_atr: float,
    wick: float,
    volume_ratio: float,
) -> SignalPlan:
    score, reasons = _base_score("short", probe_atr, wick, volume_ratio, rsi, vwap, candle.close)
    risk_notes: list[str] = []
    entry = round(candle.close * 1.0005, 4)
    stop_loss = round(candle.high + atr * 0.22, 4)
    risk_per_unit = max(stop_loss - entry, 0.0)
    if risk_per_unit <= 0:
        return _wait(base, candles, index, "invalid fakeout short stop")
    sizing = _position_sizing(entry, entry - risk_per_unit, score, base, risk_notes)
    if score < base.min_score or sizing.planned_qty <= 0:
        return _wait(base, candles, index, "fakeout short score below threshold")
    midpoint = (orb_high + orb_low) / 2
    first_tp = min(midpoint, entry - risk_per_unit * 0.45)
    take_profits = sorted([first_tp, entry - risk_per_unit * 0.9, entry - risk_per_unit * 1.6], reverse=True)
    return SignalPlan(
        action="PLAN_SHORT",
        confidence=min(score, 100),
        score=score,
        symbol=base.symbol,
        price=candle.close,
        rsi=rsi,
        atr=atr,
        support=orb_high,
        vwap=vwap,
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


def _long_signal(
    candles: list[Candle],
    index: int,
    base: StrategyConfig,
    candle: Candle,
    atr: float,
    rsi: float | None,
    vwap: float | None,
    orb_high: float,
    orb_low: float,
    probe_atr: float,
    wick: float,
    volume_ratio: float,
) -> SignalPlan:
    score, reasons = _base_score("long", probe_atr, wick, volume_ratio, rsi, vwap, candle.close)
    risk_notes: list[str] = []
    entry = round(candle.close * 0.9995, 4)
    stop_loss = round(candle.low - atr * 0.22, 4)
    risk_per_unit = max(entry - stop_loss, 0.0)
    if risk_per_unit <= 0:
        return _wait(base, candles, index, "invalid fakeout long stop")
    sizing = _position_sizing(entry, stop_loss, score, base, risk_notes)
    if score < base.min_score or sizing.planned_qty <= 0:
        return _wait(base, candles, index, "fakeout long score below threshold")
    midpoint = (orb_high + orb_low) / 2
    first_tp = max(midpoint, entry + risk_per_unit * 0.45)
    take_profits = sorted([first_tp, entry + risk_per_unit * 0.9, entry + risk_per_unit * 1.6])
    return SignalPlan(
        action="PLAN_LONG",
        confidence=min(score, 100),
        score=score,
        symbol=base.symbol,
        price=candle.close,
        rsi=rsi,
        atr=atr,
        support=orb_low,
        vwap=vwap,
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


def _base_score(
    side: str,
    probe_atr: float,
    wick: float,
    volume_ratio: float,
    rsi: float | None,
    vwap: float | None,
    price: float,
) -> tuple[int, list[str]]:
    score = 34
    reasons = [f"{side} fakeout probe {probe_atr:.2f} ATR"]
    if wick >= 0.50:
        score += 24
    else:
        score += 16
    reasons.append(f"rejection wick {wick:.2f}")
    if volume_ratio >= 1.0:
        score += 12
        reasons.append(f"fakeout volume {volume_ratio:.2f}x")
    elif volume_ratio >= 0.75:
        score += 6
        reasons.append(f"moderate fakeout volume {volume_ratio:.2f}x")
    if rsi is not None:
        if side == "short" and 52 <= rsi <= 74:
            score += 12
        elif side == "long" and 28 <= rsi <= 48:
            score += 12
        elif 42 <= rsi <= 58:
            score += 6
    if vwap is not None:
        if (side == "short" and price >= vwap) or (side == "long" and price <= vwap):
            score += 8
            reasons.append("VWAP mean-reversion target is aligned")
    return score, reasons


def _reject_short_trend(price: float, ema_fast: float | None, ema_slow: float | None, rsi: float | None, config: FakeoutReversalConfig) -> bool:
    if not config.reject_strong_trend or ema_fast is None or ema_slow is None or rsi is None:
        return False
    return price > ema_fast > ema_slow and rsi >= 72


def _reject_long_trend(price: float, ema_fast: float | None, ema_slow: float | None, rsi: float | None, config: FakeoutReversalConfig) -> bool:
    if not config.reject_strong_trend or ema_fast is None or ema_slow is None or rsi is None:
        return False
    return price < ema_fast < ema_slow and rsi <= 28


def _replace_base(config: OrbConfig | FakeoutReversalConfig, base: StrategyConfig):
    from dataclasses import replace

    if isinstance(config, FakeoutReversalConfig):
        return replace(config, orb=replace(config.orb, base=base))
    return replace(config, base=base)


def _equity_base(base: StrategyConfig, equity: float) -> StrategyConfig:
    if not base.compounding_enabled:
        return base
    from dataclasses import replace

    return replace(base, equity_usdc=max(equity, 0.0))


def _wait(config: StrategyConfig, candles: list[Candle], index: int, reason: str) -> SignalPlan:
    price = candles[index].close if candles else 0.0
    return SignalPlan(
        action="WAIT",
        confidence=0,
        score=0,
        symbol=config.symbol,
        price=price,
        rsi=None,
        atr=None,
        support=None,
        vwap=None,
        daily_target_usdc=(config.daily_target_min_usdc, config.daily_target_max_usdc),
        reasons=[reason],
    )
