"""Long-only ETH pullback signal and backtest engine.

The engine is intentionally pure: it consumes candles, produces signals or
backtest results, and never places orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from itertools import product
from math import inf
from statistics import mean


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float = 0.0

    @classmethod
    def from_binance_kline(cls, row: list) -> "Candle":
        return cls(
            open_time_ms=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            quote_volume=float(row[7]) if len(row) > 7 else 0.0,
        )


@dataclass(frozen=True)
class StrategyConfig:
    symbol: str = "ETHUSDC"
    equity_usdc: float = 200.0
    compounding_enabled: bool = False
    daily_target_min_pct: float = 3.0
    daily_target_max_pct: float = 3.0
    risk_per_trade_pct: float = 0.5
    max_effective_leverage: float = 20.0
    maker_fee_rate: float = 0.0
    taker_fee_rate: float = 0.0004
    daily_soft_loss_pct: float = 4.0
    daily_max_loss_pct: float = 8.0
    daily_loss_risk_scale: float = 0.55
    daily_target_stop_pct: float = 3.0
    stop_trading_after_daily_target: bool = True
    max_open_positions: int = 1
    max_position_margin_pct: float = 35.0
    max_consecutive_losses_before_cooldown: int = 2
    consecutive_loss_cooldown_bars: int = 36
    accelerator_enabled: bool = True
    accelerator_min_score: int = 85
    accelerator_risk_per_trade_pct: float = 0.35
    accelerator_margin_pct: float = 8.0
    accelerator_max_effective_leverage: float = 30.0
    rsi_period: int = 14
    atr_period: int = 14
    ema_fast_period: int = 21
    ema_slow_period: int = 55
    vwap_period: int = 96
    support_lookback: int = 72
    min_score: int = 55
    entry_spacing_atr: float = 0.35
    stop_atr: float = 1.6
    stop_support_buffer_atr: float = 0.35
    entry_expiry_bars: int = 8
    max_holding_bars: int = 96
    cooldown_bars: int = 12
    take_profit_r: tuple[float, float, float] = (0.6, 1.0, 1.5)
    entry_weights: tuple[float, float, float] = (0.40, 0.35, 0.25)
    exit_weights: tuple[float, float, float] = (0.40, 0.35, 0.25)
    breakeven_after_tp: int = 0
    breakeven_lock_r: float = 0.0

    @property
    def risk_amount_usdc(self) -> float:
        return self.equity_usdc * self.risk_per_trade_pct / 100

    @property
    def daily_target_min_usdc(self) -> float:
        return self.equity_usdc * self.daily_target_min_pct / 100

    @property
    def daily_target_max_usdc(self) -> float:
        return self.equity_usdc * self.daily_target_max_pct / 100

    @property
    def daily_max_loss_usdc(self) -> float:
        return self.equity_usdc * self.daily_max_loss_pct / 100

    @property
    def daily_soft_loss_usdc(self) -> float:
        return self.equity_usdc * self.daily_soft_loss_pct / 100

    @property
    def daily_target_stop_usdc(self) -> float:
        return self.equity_usdc * self.daily_target_stop_pct / 100


@dataclass(frozen=True)
class SignalPlan:
    action: str
    confidence: int
    score: int
    symbol: str
    price: float
    rsi: float | None
    atr: float | None
    support: float | None
    vwap: float | None
    entries: list[float] = field(default_factory=list)
    entry_weights: list[float] = field(default_factory=list)
    stop_loss: float | None = None
    take_profits: list[float] = field(default_factory=list)
    planned_notional_usdc: float = 0.0
    planned_margin_usdc: float = 0.0
    planned_qty: float = 0.0
    risk_amount_usdc: float = 0.0
    sizing_mode: str = "core"
    leverage_cap: float = 0.0
    daily_target_usdc: tuple[float, float] = (0.0, 0.0)
    reasons: list[str] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TradeResult:
    entry_time_ms: int
    exit_time_ms: int
    entry_price: float
    exit_price: float
    qty: float
    pnl_usdc: float
    fees_usdc: float
    r_multiple: float
    reason: str
    hold_bars: int


@dataclass(frozen=True)
class BacktestSummary:
    config: StrategyConfig
    trades: list[TradeResult]
    net_pnl_usdc: float
    return_pct: float
    max_drawdown_usdc: float
    max_drawdown_pct: float
    win_rate_pct: float
    profit_factor: float
    expectancy_usdc: float
    max_consecutive_losses: int
    avg_daily_return_pct: float
    daily_target_min_hit_rate_pct: float
    daily_target_max_hit_rate_pct: float
    daily_pnls: dict[str, float]
    params: dict[str, float | int | str] = field(default_factory=dict)

    @property
    def total_trades(self) -> int:
        return len(self.trades)


@dataclass(frozen=True)
class PositionSizing:
    planned_notional_usdc: float
    planned_margin_usdc: float
    planned_qty: float
    risk_amount_usdc: float
    sizing_mode: str
    leverage_cap: float


@dataclass(frozen=True)
class IndicatorContext:
    rsi_values: list[float | None]
    atr_values: list[float | None]
    ema_fast_values: list[float | None]
    ema_slow_values: list[float | None]
    vwap_values: list[float | None]
    support_values: list[float | None]
    recent_high_values: list[float | None]
    fast_drop_values: list[bool]


def generate_signal(candles: list[Candle], config: StrategyConfig | None = None) -> SignalPlan:
    """Generate a long-only pullback plan from the latest closed candle."""
    config = config or StrategyConfig()
    context = build_indicator_context(candles, config)
    return generate_signal_at(candles, len(candles) - 1, config, context)


def generate_signal_at(
    candles: list[Candle],
    index: int,
    config: StrategyConfig,
    context: IndicatorContext | None = None,
) -> SignalPlan:
    """Generate a long-only pullback plan for a specific candle index."""
    min_bars = max(
        config.rsi_period + 1,
        config.atr_period + 1,
        config.ema_slow_period,
        config.vwap_period,
        config.support_lookback,
    )
    if index < 0 or index >= len(candles):
        return _wait(config, candles, "not enough candles")
    if index + 1 < min_bars:
        return _wait_at(config, candles, index, "not enough candles")

    context = context or build_indicator_context(candles, config)
    price = candles[index].close
    rsi_value = context.rsi_values[index]
    atr_value = context.atr_values[index]
    ema_fast = context.ema_fast_values[index]
    ema_slow = context.ema_slow_values[index]
    vwap_value = context.vwap_values[index]
    support = context.support_values[index]
    recent_high = context.recent_high_values[index]
    pullback_pct = (recent_high - price) / recent_high * 100 if recent_high else 0.0

    reasons: list[str] = []
    risk_notes: list[str] = []
    score = 0

    if atr_value is None or atr_value <= 0:
        return _wait_at(config, candles, index, "ATR unavailable")

    if context.fast_drop_values[index]:
        return _context_wait_at(
            config, candles, index, "fast drop detected; avoid catching first impulse",
            rsi_value, atr_value, support, vwap_value, risk_notes,
        )

    hard_downtrend = ema_slow is not None and price < ema_slow - (1.8 * atr_value)
    if hard_downtrend:
        return _context_wait_at(
            config, candles, index, "hard downtrend; wait for stabilization",
            rsi_value, atr_value, support, vwap_value, risk_notes,
        )

    if rsi_value is not None:
        if 34 <= rsi_value <= 52:
            score += 25
            reasons.append(f"RSI {rsi_value:.1f} in medium pullback zone")
        elif 28 <= rsi_value < 34 or 52 < rsi_value <= 58:
            score += 12
            reasons.append(f"RSI {rsi_value:.1f} acceptable but not ideal")
        elif rsi_value > 65:
            risk_notes.append(f"RSI {rsi_value:.1f} is hot; avoid chasing")

    if 0.25 <= pullback_pct <= 3.2:
        score += 20
        reasons.append(f"pullback {pullback_pct:.2f}% from recent high")
    elif pullback_pct > 3.2:
        score += 8
        risk_notes.append(f"deep pullback {pullback_pct:.2f}%; size conservatively")

    support_distance_atr = (price - support) / atr_value if support is not None else inf
    if support is not None:
        if -0.2 <= support_distance_atr <= 1.2:
            score += 25
            reasons.append("price is close to recent support")
        elif support_distance_atr <= 2.0:
            score += 10
            reasons.append("support is nearby")

    if vwap_value is not None:
        vwap_distance_pct = (price - vwap_value) / vwap_value * 100
        if -1.6 <= vwap_distance_pct <= 0.4:
            score += 15
            reasons.append(f"price near/below VWAP ({vwap_distance_pct:.2f}%)")
        elif vwap_distance_pct > 1.2:
            risk_notes.append("price is extended above VWAP")

    if ema_fast is not None and ema_slow is not None:
        if ema_fast >= ema_slow or price >= ema_slow:
            score += 15
            reasons.append("trend background is not hostile")
        else:
            score += 5
            risk_notes.append("trend is soft; require cleaner entry")

    if score < config.min_score:
        return SignalPlan(
            action="WAIT",
            confidence=min(score, 100),
            score=score,
            symbol=config.symbol,
            price=price,
            rsi=rsi_value,
            atr=atr_value,
            support=support,
            vwap=vwap_value,
            daily_target_usdc=(config.daily_target_min_usdc, config.daily_target_max_usdc),
            reasons=reasons or ["score below threshold"],
            risk_notes=risk_notes,
        )

    entries = _entry_levels(price, support, atr_value, config)
    stop_loss = _stop_loss(entries[-1], support, atr_value, config)
    weighted_entry = _weighted_average(entries, list(config.entry_weights))
    risk_per_unit = max(weighted_entry - stop_loss, 0)
    if risk_per_unit <= 0:
        return _wait_at(config, candles, index, "invalid stop distance")

    sizing = _position_sizing(weighted_entry, stop_loss, score, config, risk_notes)
    if sizing.planned_qty <= 0:
        return _wait_at(config, candles, index, "invalid position sizing")

    take_profits = [weighted_entry + risk_per_unit * r for r in config.take_profit_r]

    if sizing.planned_notional_usdc > config.equity_usdc * 8:
        risk_notes.append("notional is high versus 200 USDC equity; keep testnet-only")

    return SignalPlan(
        action="PLAN_LONG",
        confidence=min(score, 100),
        score=score,
        symbol=config.symbol,
        price=price,
        rsi=rsi_value,
        atr=atr_value,
        support=support,
        vwap=vwap_value,
        entries=entries,
        entry_weights=list(config.entry_weights),
        stop_loss=stop_loss,
        take_profits=take_profits,
        planned_notional_usdc=sizing.planned_notional_usdc,
        planned_margin_usdc=sizing.planned_margin_usdc,
        planned_qty=sizing.planned_qty,
        risk_amount_usdc=sizing.risk_amount_usdc,
        sizing_mode=sizing.sizing_mode,
        leverage_cap=sizing.leverage_cap,
        daily_target_usdc=(config.daily_target_min_usdc, config.daily_target_max_usdc),
        reasons=reasons,
        risk_notes=risk_notes,
    )


def run_backtest(candles: list[Candle], config: StrategyConfig | None = None) -> BacktestSummary:
    """Run a simple maker-limit long-only backtest."""
    config = config or StrategyConfig()
    context = build_indicator_context(candles, config)
    return run_backtest_with_context(candles, config, context)


def run_backtest_with_context(
    candles: list[Candle],
    config: StrategyConfig,
    context: IndicatorContext,
) -> BacktestSummary:
    """Run a backtest with precomputed indicators."""
    warmup = max(config.support_lookback, config.vwap_period, config.ema_slow_period) + 2
    trades: list[TradeResult] = []
    equity = config.equity_usdc
    peak_equity = equity
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    daily_pnls = _empty_daily_pnls(candles)
    consecutive_losses = 0
    cooldown = 0
    index = warmup

    while index < len(candles) - 2:
        if config.max_open_positions < 1:
            break

        if cooldown > 0:
            cooldown -= 1
            index += 1
            continue

        equity_config = replace(config, equity_usdc=equity) if config.compounding_enabled else config
        day = _day_key(candles[index].open_time_ms)
        day_pnl = daily_pnls.get(day, 0.0)
        if _daily_guard_reason(equity_config, day_pnl):
            index += 1
            continue

        runtime_config = _risk_adjusted_config(equity_config, day_pnl)
        signal = generate_signal_at(candles, index, runtime_config, context)
        if signal.action != "PLAN_LONG":
            index += 1
            continue

        trade, next_index = _simulate_plan(candles, index + 1, signal, runtime_config)
        if trade is None:
            index += max(next_index - index, 1)
            continue

        trades.append(trade)
        exit_day = _day_key(trade.exit_time_ms)
        daily_pnls[exit_day] = daily_pnls.get(exit_day, 0.0) + trade.pnl_usdc
        equity += trade.pnl_usdc
        peak_equity = max(peak_equity, equity)
        max_drawdown = min(max_drawdown, equity - peak_equity)
        max_drawdown_pct = min(max_drawdown_pct, _drawdown_pct(equity, peak_equity))
        cooldown = max(cooldown, runtime_config.cooldown_bars)
        if trade.pnl_usdc < 0:
            consecutive_losses += 1
            if (
                runtime_config.max_consecutive_losses_before_cooldown > 0
                and consecutive_losses >= runtime_config.max_consecutive_losses_before_cooldown
            ):
                cooldown = max(cooldown, runtime_config.consecutive_loss_cooldown_bars)
                consecutive_losses = 0
        else:
            consecutive_losses = 0
        index = max(next_index, index + 1)

    return _summary(config, trades, max_drawdown, max_drawdown_pct, daily_pnls)


def sweep_configs(
    candles: list[Candle],
    base: StrategyConfig | None = None,
    profile: str = "balanced",
) -> list[BacktestSummary]:
    """Run a compact parameter sweep sorted by risk-adjusted score."""
    base = base or StrategyConfig()
    context = build_indicator_context(candles, base)
    results: list[BacktestSummary] = []
    if profile == "aggressive":
        risk_values = (1.0, 1.5, 2.0)
        stop_values = (0.8, 1.0, 1.2, 1.6)
        spacing_values = (0.20, 0.30, 0.40)
        score_values = (45, 50, 55)
    else:
        risk_values = (0.5, 0.8, 1.0)
        stop_values = (1.2, 1.6, 2.0)
        spacing_values = (0.25, 0.40, 0.60)
        score_values = (50, 55, 60)

    for risk, stop, spacing, min_score in product(
        risk_values,
        stop_values,
        spacing_values,
        score_values,
    ):
        cfg = replace(
            base,
            risk_per_trade_pct=risk,
            stop_atr=stop,
            entry_spacing_atr=spacing,
            min_score=min_score,
        )
        results.append(run_backtest_with_context(candles, cfg, context))

    return sorted(results, key=_rank_score, reverse=True)


def build_indicator_context(candles: list[Candle], config: StrategyConfig) -> IndicatorContext:
    """Precompute indicators once for fast backtests and sweeps."""
    closes = [c.close for c in candles]
    atr_values = _atr_series(candles, config.atr_period)
    return IndicatorContext(
        rsi_values=_rsi_series(closes, config.rsi_period),
        atr_values=atr_values,
        ema_fast_values=_ema_series(closes, config.ema_fast_period),
        ema_slow_values=_ema_series(closes, config.ema_slow_period),
        vwap_values=_vwap_series(candles, config.vwap_period),
        support_values=_support_series(candles, config.support_lookback),
        recent_high_values=_recent_high_series(candles, config.support_lookback),
        fast_drop_values=_fast_drop_series(candles, atr_values),
    )


def rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    alpha = 2 / (period + 1)
    current = sum(values[:period]) / period
    for value in values[period:]:
        current = alpha * value + (1 - alpha) * current
    return current


def atr(candles: list[Candle], period: int = 14) -> float | None:
    if len(candles) <= period:
        return None
    true_ranges: list[float] = []
    for index in range(1, len(candles)):
        current = candles[index]
        previous = candles[index - 1]
        true_ranges.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
    if len(true_ranges) < period:
        return None
    return sum(true_ranges[-period:]) / period


def rolling_vwap(candles: list[Candle], period: int = 96) -> float | None:
    if len(candles) < period:
        return None
    window = candles[-period:]
    total_volume = sum(c.volume for c in window)
    if total_volume <= 0:
        return None
    return sum(((c.high + c.low + c.close) / 3) * c.volume for c in window) / total_volume


def rolling_support(candles: list[Candle], lookback: int = 72) -> float | None:
    if len(candles) < lookback:
        return None
    return min(c.low for c in candles[-lookback:-1])


def _rsi_series(values: list[float], period: int) -> list[float | None]:
    series: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return series

    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    series[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)

    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0)) / period
        series[index] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return series


def _ema_series(values: list[float], period: int) -> list[float | None]:
    series: list[float | None] = [None] * len(values)
    if len(values) < period:
        return series
    alpha = 2 / (period + 1)
    current = sum(values[:period]) / period
    series[period - 1] = current
    for index in range(period, len(values)):
        current = alpha * values[index] + (1 - alpha) * current
        series[index] = current
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

    rolling_sum = sum(true_ranges[1: period + 1])
    series[period] = rolling_sum / period
    for index in range(period + 1, len(candles)):
        rolling_sum += true_ranges[index] - true_ranges[index - period]
        series[index] = rolling_sum / period
    return series


def _vwap_series(candles: list[Candle], period: int) -> list[float | None]:
    series: list[float | None] = [None] * len(candles)
    if len(candles) < period:
        return series

    price_volume = [((c.high + c.low + c.close) / 3) * c.volume for c in candles]
    volume = [c.volume for c in candles]
    pv_sum = 0.0
    volume_sum = 0.0
    for index, candle in enumerate(candles):
        pv_sum += price_volume[index]
        volume_sum += volume[index]
        if index >= period:
            pv_sum -= price_volume[index - period]
            volume_sum -= volume[index - period]
        if index >= period - 1 and volume_sum > 0:
            series[index] = pv_sum / volume_sum
    return series


def _support_series(candles: list[Candle], lookback: int) -> list[float | None]:
    series: list[float | None] = [None] * len(candles)
    for index in range(lookback - 1, len(candles)):
        start = index + 1 - lookback
        end = index
        if start < end:
            series[index] = min(c.low for c in candles[start:end])
    return series


def _recent_high_series(candles: list[Candle], lookback: int) -> list[float | None]:
    series: list[float | None] = [None] * len(candles)
    for index in range(lookback - 1, len(candles)):
        start = index + 1 - lookback
        series[index] = max(c.high for c in candles[start:index + 1])
    return series


def _fast_drop_series(candles: list[Candle], atr_values: list[float | None]) -> list[bool]:
    series = [False] * len(candles)
    for index in range(3, len(candles)):
        atr_value = atr_values[index]
        if atr_value is None or atr_value <= 0:
            continue
        drop = candles[index - 3].close - candles[index].close
        red_candles = sum(1 for c in candles[index - 2:index + 1] if c.close < c.open)
        series[index] = drop > atr_value * 1.4 and red_candles >= 2
    return series


def _wait(config: StrategyConfig, candles: list[Candle], reason: str) -> SignalPlan:
    price = candles[-1].close if candles else 0.0
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


def _wait_at(config: StrategyConfig, candles: list[Candle], index: int, reason: str) -> SignalPlan:
    price = candles[index].close if 0 <= index < len(candles) else 0.0
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


def _context_wait_at(
    config: StrategyConfig,
    candles: list[Candle],
    index: int,
    reason: str,
    rsi_value: float | None,
    atr_value: float | None,
    support: float | None,
    vwap_value: float | None,
    risk_notes: list[str],
) -> SignalPlan:
    price = candles[index].close if 0 <= index < len(candles) else 0.0
    return SignalPlan(
        action="WAIT",
        confidence=0,
        score=0,
        symbol=config.symbol,
        price=price,
        rsi=rsi_value,
        atr=atr_value,
        support=support,
        vwap=vwap_value,
        daily_target_usdc=(config.daily_target_min_usdc, config.daily_target_max_usdc),
        reasons=[reason],
        risk_notes=risk_notes,
    )


def _is_fast_drop(candles: list[Candle], atr_value: float) -> bool:
    if len(candles) < 4 or atr_value <= 0:
        return False
    drop = candles[-4].close - candles[-1].close
    red_candles = sum(1 for c in candles[-3:] if c.close < c.open)
    return drop > atr_value * 1.4 and red_candles >= 2


def _entry_levels(price: float, support: float | None, atr_value: float, config: StrategyConfig) -> list[float]:
    support_anchor = support if support is not None else price - atr_value
    first = min(price * 0.999, max(support_anchor + atr_value * 0.15, price - atr_value * 0.25))
    return [
        round(first, 4),
        round(first - atr_value * config.entry_spacing_atr, 4),
        round(first - atr_value * config.entry_spacing_atr * 2, 4),
    ]


def _stop_loss(last_entry: float, support: float | None, atr_value: float, config: StrategyConfig) -> float:
    support_stop = (support - atr_value * config.stop_support_buffer_atr) if support is not None else inf
    atr_stop = last_entry - atr_value * config.stop_atr
    return round(min(support_stop, atr_stop), 4)


def _weighted_average(values: list[float], weights: list[float]) -> float:
    total_weight = sum(weights)
    return sum(v * w for v, w in zip(values, weights)) / total_weight


def _position_sizing(
    entry_price: float,
    stop_loss: float,
    score: int,
    config: StrategyConfig,
    risk_notes: list[str],
) -> PositionSizing:
    risk_per_unit = max(entry_price - stop_loss, 0)
    if entry_price <= 0 or risk_per_unit <= 0:
        return PositionSizing(0.0, 0.0, 0.0, 0.0, "invalid", 0.0)

    core_notional, core_leverage = _sizing_component(
        entry_price,
        risk_per_unit,
        config.risk_amount_usdc,
        config.equity_usdc,
        config.max_position_margin_pct,
        config.max_effective_leverage,
        "core",
        risk_notes,
    )

    use_accelerator = config.accelerator_enabled and score >= config.accelerator_min_score
    if use_accelerator:
        accelerator_notional, accelerator_leverage = _sizing_component(
            entry_price,
            risk_per_unit,
            config.equity_usdc * config.accelerator_risk_per_trade_pct / 100,
            config.equity_usdc,
            config.accelerator_margin_pct,
            config.accelerator_max_effective_leverage,
            "accelerator",
            risk_notes,
        )
        planned_notional = core_notional + accelerator_notional
        planned_margin = (
            core_notional / core_leverage if core_leverage > 0 else 0.0
        ) + (
            accelerator_notional / accelerator_leverage if accelerator_leverage > 0 else 0.0
        )
        leverage_cap = max(core_leverage, accelerator_leverage)
        sizing_mode = "core+accelerator" if accelerator_notional > 0 else "core"
        risk_notes.append(
            f"accelerator add-on: margin cap {config.accelerator_margin_pct:.1f}% "
            f"at up to {config.accelerator_max_effective_leverage:.1f}x"
        )
    else:
        planned_notional = core_notional
        planned_margin = core_notional / core_leverage if core_leverage > 0 else 0.0
        leverage_cap = core_leverage
        sizing_mode = "core"

    planned_qty = planned_notional / entry_price
    actual_risk = planned_qty * risk_per_unit

    return PositionSizing(
        planned_notional_usdc=planned_notional,
        planned_margin_usdc=planned_margin,
        planned_qty=planned_qty,
        risk_amount_usdc=actual_risk,
        sizing_mode=sizing_mode,
        leverage_cap=leverage_cap,
    )


def _sizing_component(
    entry_price: float,
    risk_per_unit: float,
    risk_budget: float,
    equity_usdc: float,
    margin_pct: float,
    leverage_cap: float,
    label: str,
    risk_notes: list[str],
) -> tuple[float, float]:
    if risk_budget <= 0 or margin_pct <= 0 or leverage_cap <= 0:
        return 0.0, max(leverage_cap, 0.0)

    notional_by_risk = risk_budget / (risk_per_unit / entry_price)
    max_notional = equity_usdc * margin_pct / 100 * leverage_cap
    if notional_by_risk > max_notional:
        risk_notes.append(f"{label} size capped by margin guard")
    return min(notional_by_risk, max_notional), leverage_cap


def _simulate_plan(
    candles: list[Candle],
    start_index: int,
    signal: SignalPlan,
    config: StrategyConfig,
) -> tuple[TradeResult | None, int]:
    entries = signal.entries
    if not entries or signal.stop_loss is None:
        return None, start_index + 1

    fills: list[tuple[float, float]] = []
    planned_qty = signal.planned_qty
    entry_weights = signal.entry_weights
    last_index = min(start_index + config.entry_expiry_bars, len(candles) - 1)
    index = start_index

    while index <= last_index and not fills:
        candle = candles[index]
        for entry, weight in zip(entries, entry_weights):
            if candle.low <= entry:
                fills.append((entry, planned_qty * weight))
        index += 1

    if not fills:
        return None, last_index

    avg_entry = sum(price * qty for price, qty in fills) / sum(qty for _, qty in fills)
    total_qty = sum(qty for _, qty in fills)
    remaining_qty = total_qty
    fees = sum(price * qty * config.maker_fee_rate for price, qty in fills)
    realized = 0.0
    first_fill_index = max(start_index, index - 1)
    risk_per_unit = avg_entry - signal.stop_loss
    take_profits = signal.take_profits
    tp_hit = [False] * len(take_profits)

    exit_price = candles[first_fill_index].close
    exit_reason = "max_hold"
    exit_index = min(first_fill_index + config.max_holding_bars, len(candles) - 1)

    for index in range(first_fill_index, min(first_fill_index + config.max_holding_bars, len(candles) - 1) + 1):
        candle = candles[index]
        if candle.low <= signal.stop_loss:
            exit_price = signal.stop_loss
            fees += remaining_qty * exit_price * config.taker_fee_rate
            realized += remaining_qty * (exit_price - avg_entry)
            remaining_qty = 0
            exit_reason = "stop_loss"
            exit_index = index
            break

        for tp_idx, tp in enumerate(take_profits):
            if tp_hit[tp_idx] or candle.high < tp:
                continue
            qty_to_exit = min(total_qty * config.exit_weights[tp_idx], remaining_qty)
            if qty_to_exit <= 0:
                continue
            fees += qty_to_exit * tp * config.maker_fee_rate
            realized += qty_to_exit * (tp - avg_entry)
            remaining_qty -= qty_to_exit
            tp_hit[tp_idx] = True
            exit_price = tp
            exit_reason = f"take_profit_{tp_idx + 1}"
            exit_index = index

        if remaining_qty <= total_qty * 0.001:
            remaining_qty = 0
            break

    if remaining_qty > 0:
        candle = candles[exit_index]
        exit_price = candle.close
        fees += remaining_qty * exit_price * config.taker_fee_rate
        realized += remaining_qty * (exit_price - avg_entry)
        remaining_qty = 0

    pnl = realized - fees
    planned_risk = max(signal.risk_amount_usdc, 0.0001)
    return (
        TradeResult(
            entry_time_ms=candles[first_fill_index].open_time_ms,
            exit_time_ms=candles[exit_index].open_time_ms,
            entry_price=avg_entry,
            exit_price=exit_price,
            qty=total_qty,
            pnl_usdc=pnl,
            fees_usdc=fees,
            r_multiple=pnl / planned_risk,
            reason=exit_reason,
            hold_bars=max(exit_index - first_fill_index, 0),
        ),
        exit_index + 1,
    )


def _daily_pnls(trades: list[TradeResult]) -> dict[str, float]:
    daily: dict[str, float] = {}
    for trade in trades:
        day = _day_key(trade.exit_time_ms)
        daily[day] = daily.get(day, 0.0) + trade.pnl_usdc
    return daily


def _empty_daily_pnls(candles: list[Candle]) -> dict[str, float]:
    return dict.fromkeys((_day_key(candle.open_time_ms) for candle in candles), 0.0)


def _day_key(open_time_ms: int) -> str:
    return datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc).date().isoformat()


def _daily_guard_reason(config: StrategyConfig, realized_day_pnl: float) -> str | None:
    if config.daily_max_loss_usdc > 0 and realized_day_pnl <= -config.daily_max_loss_usdc:
        return "daily max loss reached"
    if config.stop_trading_after_daily_target and realized_day_pnl >= config.daily_target_stop_usdc:
        return "daily target reached"
    return None


def _risk_adjusted_config(config: StrategyConfig, realized_day_pnl: float) -> StrategyConfig:
    if config.daily_soft_loss_usdc <= 0 or realized_day_pnl > -config.daily_soft_loss_usdc:
        return config

    scale = max(min(config.daily_loss_risk_scale, 1.0), 0.0)
    if scale <= 0:
        return replace(
            config,
            risk_per_trade_pct=0.0,
            accelerator_enabled=False,
            max_position_margin_pct=0.0,
        )

    return replace(
        config,
        risk_per_trade_pct=config.risk_per_trade_pct * scale,
        max_position_margin_pct=config.max_position_margin_pct * scale,
        accelerator_risk_per_trade_pct=config.accelerator_risk_per_trade_pct * scale,
        accelerator_margin_pct=config.accelerator_margin_pct * scale,
        accelerator_enabled=config.accelerator_enabled and scale >= 0.5,
    )


def _summary(
    config: StrategyConfig,
    trades: list[TradeResult],
    max_drawdown_usdc: float,
    max_drawdown_pct: float,
    daily_pnls: dict[str, float],
) -> BacktestSummary:
    net_pnl = float(sum(t.pnl_usdc for t in trades))
    wins = [t for t in trades if t.pnl_usdc > 0]
    losses = [t for t in trades if t.pnl_usdc < 0]
    gross_profit = sum(t.pnl_usdc for t in wins)
    gross_loss = abs(sum(t.pnl_usdc for t in losses))
    avg_daily_return_pct, min_hit_rate_pct, max_hit_rate_pct = _daily_performance(config, daily_pnls)

    return BacktestSummary(
        config=config,
        trades=trades,
        net_pnl_usdc=net_pnl,
        return_pct=(net_pnl / config.equity_usdc * 100) if config.equity_usdc else 0.0,
        max_drawdown_usdc=max_drawdown_usdc,
        max_drawdown_pct=max_drawdown_pct,
        win_rate_pct=(len(wins) / len(trades) * 100) if trades else 0.0,
        profit_factor=(gross_profit / gross_loss) if gross_loss > 0 else (inf if gross_profit > 0 else 0.0),
        expectancy_usdc=mean([t.pnl_usdc for t in trades]) if trades else 0.0,
        max_consecutive_losses=_max_consecutive_losses(trades),
        avg_daily_return_pct=avg_daily_return_pct,
        daily_target_min_hit_rate_pct=min_hit_rate_pct,
        daily_target_max_hit_rate_pct=max_hit_rate_pct,
        daily_pnls=daily_pnls,
    )


def _max_consecutive_losses(trades: list[TradeResult]) -> int:
    current = 0
    max_seen = 0
    for trade in trades:
        if trade.pnl_usdc < 0:
            current += 1
            max_seen = max(max_seen, current)
        else:
            current = 0
    return max_seen


def _drawdown_pct(equity: float, peak_equity: float) -> float:
    if peak_equity <= 0:
        return 0.0
    return (equity - peak_equity) / peak_equity * 100


def _daily_performance(config: StrategyConfig, daily_pnls: dict[str, float]) -> tuple[float, float, float]:
    if not daily_pnls:
        return 0.0, 0.0, 0.0

    equity = config.equity_usdc
    total_return_pct = 0.0
    min_hits = 0
    max_hits = 0
    total_days = 0

    for pnl in daily_pnls.values():
        start_equity = equity if config.compounding_enabled else config.equity_usdc
        if start_equity > 0:
            day_return_pct = pnl / start_equity * 100
            if pnl >= start_equity * config.daily_target_min_pct / 100:
                min_hits += 1
            if pnl >= start_equity * config.daily_target_max_pct / 100:
                max_hits += 1
        else:
            day_return_pct = 0.0
        total_return_pct += day_return_pct
        total_days += 1
        equity += pnl

    return total_return_pct / total_days, min_hits / total_days * 100, max_hits / total_days * 100


def _rank_score(summary: BacktestSummary) -> float:
    drawdown = abs(summary.max_drawdown_pct)
    drawdown_penalty = drawdown * 1.35
    if drawdown > 15:
        drawdown_penalty += (drawdown - 15) * 2.0
    loss_streak_penalty = summary.max_consecutive_losses * 1.5
    trade_bonus = min(summary.total_trades, 40) * 0.05
    target_bonus = summary.daily_target_min_hit_rate_pct * 0.45
    avg_daily = sum(summary.daily_pnls.values()) / len(summary.daily_pnls) if summary.daily_pnls else 0.0
    avg_daily_bonus = min(max(avg_daily / max(summary.config.daily_target_min_usdc, 0.0001), 0.0), 2.0) * 8
    profit_factor_bonus = min(summary.profit_factor, 3.0) * 4 if summary.profit_factor != inf else 12
    return (
        summary.return_pct
        - drawdown_penalty
        - loss_streak_penalty
        + trade_bonus
        + target_bonus
        + avg_daily_bonus
        + profit_factor_bonus
    )
