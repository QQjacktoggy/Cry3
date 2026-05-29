"""Backtest a rule-based ETHUSDC short-term scalp strategy inspired by manual trading.

The strategy is designed to preserve the user's observed edge:
- follow the 5m trend
- take small momentum continuation profits
- use tightly capped rescue adds only while structure is still valid
- enforce hard daily loss and cooldowns
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_signal import fetch_klines
from src.gridbot.strategy.long_pullback import Candle, StrategyConfig as PullbackStrategyConfig
from src.gridbot.strategy.market_state import build_market_state_context, classify_market_state

TAIPEI = ZoneInfo("Asia/Taipei")
ONE_MINUTE_MS = 60_000
FIVE_MINUTE_MS = 5 * ONE_MINUTE_MS
ONE_HOUR_MS = 60 * ONE_MINUTE_MS
BINANCE_FAPI_BASE = "https://fapi.binance.com"


@dataclass(frozen=True)
class ManualScalpConfig:
    symbol: str = "ETHUSDC"
    equity_usdc: float = 150.0
    compounding: bool = False
    allow_long: bool = True
    allow_short: bool = False
    margin_pct: float = 8.0
    leverage: float = 50.0
    maker_fee_rate: float = 0.0
    taker_fee_rate: float = 0.0004
    breakout_lookback: int = 3
    volume_lookback: int = 20
    volume_ratio_min: float = 1.1
    ema_fast_period: int = 21
    atr_period: int = 14
    trend_ema_fast_5m: int = 20
    trend_ema_slow_5m: int = 50
    trend_slope_bars_5m: int = 2
    max_extension_atr: float = 1.3
    min_breakout_atr: float = 0.03
    take_profit_pct: float = 0.40
    stop_loss_pct: float = 0.28
    max_hold_minutes: int = 8
    cooldown_minutes: int = 30
    max_consecutive_losses: int = 2
    trend_target_usdc: float = 2.5
    trend_stop_usdc: float = 0.85
    trend_max_hold_minutes: int = 5
    trend_scratch_minutes: int = 4
    trend_scratch_progress_ratio: float = 0.12
    trend_scratch_min_adverse_usdc: float = 0.25
    trend_breakeven_progress_ratio: float = 0.55
    trend_breakeven_buffer_usdc: float = 0.08
    rescue_enabled: bool = True
    rescue_trend_enabled: bool = False
    rescue_range_enabled: bool = True
    rescue_max_adds: int = 2
    rescue_step_atr: float = 0.55
    rescue_max_adverse_atr: float = 1.35
    rescue_add_notional_fraction: float = 0.50
    rescue_add_notional_fractions: tuple[float, ...] = (0.50, 0.35)
    rescue_max_notional_usdc: float = 700.0
    rescue_min_spacing_minutes: int = 2
    rescue_total_stop_usdc: float = 1.15
    rescue_partial_fraction: float = 0.45
    rescue_partial_take_usdc: float = 0.45
    rescue_runner_target_usdc: float = 0.75
    rescue_runner_stop_usdc: float = 0.55
    strong_trend_enabled: bool = True
    strong_trend_notional_multiplier: float = 1.35
    strong_trend_target_usdc: float = 3.25
    strong_trend_stop_usdc: float = 1.15
    strong_trend_min_volume_ratio: float = 1.25
    strong_trend_min_move_15m_atr: float = 1.45
    strong_trend_min_extension_atr: float = 0.65
    strong_trend_max_extension_atr: float = 1.25
    max_initial_notional_usdc: float = 900.0
    trend_allow_breakout: bool = False
    trend_allow_reclaim: bool = True
    enable_trend_pullback: bool = False
    enable_trend_continuation: bool = True
    trend_reclaim_max_extension_atr: float = 0.55
    trend_reclaim_min_extension_atr: float = -0.35
    trend_max_directional_move_15m_atr: float = 1.8
    trend_max_recent_breakout_atr: float = 0.4
    overheat_max_distance_ema_atr: float = 2.0
    overheat_max_move_15m_atr: float = 3.0
    trend_limit_ttl_minutes: int = 3
    trend_limit_offset_atr: float = 0.20
    trend_limit_ema_buffer_atr: float = 0.05
    continuation_min_extension_atr: float = 0.45
    continuation_max_extension_atr: float = 1.35
    continuation_min_move_15m_atr: float = 0.8
    continuation_max_move_15m_atr: float = 2.8
    continuation_min_volume_ratio: float = 0.8
    continuation_limit_ttl_minutes: int = 2
    continuation_limit_offset_atr: float = 0.10
    trend_pullback_lookback: int = 4
    trend_pullback_touch_atr: float = 0.45
    trend_reclaim_atr: float = 0.06
    hourly_bias_fast_period: int = 8
    hourly_bias_slow_period: int = 21
    trend_min_market_confidence: float = 0.65
    trend_alignment_lookback_5m: int = 2
    trend_min_alignment_bars: int = 1
    range_alignment_lookback_5m: int = 3
    range_min_alignment_bars: int = 2
    trend_trade_spacing_minutes: int = 4
    trend_same_side_spacing_minutes: int = 15
    trend_same_side_window_minutes: int = 180
    trend_same_side_max_trades_in_window: int = 3
    range_trade_spacing_minutes: int = 5
    max_trend_trades_per_day: int = 28
    max_range_trades_per_day: int = 10
    daily_max_loss_pct: float = 3.0
    daily_target_pct: float = 5.0
    stop_after_daily_target: bool = True
    start_hour_taipei: int = 0
    end_hour_taipei: int = 23
    allowed_hours_taipei: tuple[int, ...] = (4, 5, 6, 20, 22, 23)
    range_enabled: bool = True
    range_lookback_5m: int = 12
    range_max_drift_width_ratio: float = 0.55
    range_min_width_atr: float = 1.2
    range_max_width_atr: float = 8.0
    range_entry_zone: float = 0.35
    range_reentry_buffer: float = 0.08
    range_reject_wick_ratio: float = 0.22
    range_touch_tolerance_atr: float = 0.45
    range_min_touches_each_side: int = 2
    range_max_midline_distance: float = 0.52
    range_target_usdc: float = 0.75
    range_stop_usdc: float = 0.65
    range_max_hold_minutes: int = 8
    high_low_micro_enabled: bool = False
    high_low_micro_lookback_minutes: int = 45
    high_low_micro_min_width_atr: float = 1.3
    high_low_micro_max_width_atr: float = 7.0
    high_low_micro_max_drift_width_ratio: float = 0.48
    high_low_micro_entry_zone: float = 0.24
    high_low_micro_reclaim_zone: float = 0.08
    high_low_micro_touch_tolerance_atr: float = 0.40
    high_low_micro_min_touches_each_side: int = 2
    high_low_micro_reject_wick_ratio: float = 0.20
    high_low_micro_min_body_ratio: float = 0.28
    high_low_micro_min_volume_ratio: float = 0.70
    high_low_micro_limit_offset_atr: float = 0.04
    high_low_micro_ttl_minutes: int = 2
    high_low_micro_target_usdc: float = 0.60
    high_low_micro_stop_usdc: float = 0.45
    n_shape_enabled: bool = True
    n_shape_lookback_minutes: int = 18
    n_shape_target_usdc: float = 0.75
    n_shape_stop_usdc: float = 0.45
    n_shape_min_pullback_atr: float = 0.70
    n_shape_min_reclaim_ratio: float = 0.60
    n_shape_ma_touch_atr: float = 0.40
    n_shape_max_extension_atr: float = 0.95
    n_shape_min_volume_ratio: float = 0.90
    n_shape_min_body_ratio: float = 0.45
    n_shape_allowed_hours_taipei: tuple[int, ...] = (22,)


@dataclass(frozen=True)
class Trade:
    setup: str
    side: str
    opened_at_ms: int
    closed_at_ms: int
    entry_price: float
    exit_price: float
    qty: float
    margin_used: float
    pnl_usdc: float
    fees_usdc: float
    reason: str
    entry_fee_rate: float
    exit_fee_rate: float


@dataclass
class Position:
    setup: str
    side: str
    opened_at_ms: int
    entry_price: float
    qty: float
    margin_used: float
    tp_price: float
    stop_price: float
    entry_fee_rate: float
    best_price: float
    scale_count: int = 0
    partial_taken: bool = False
    partial_take_count: int = 0
    last_partial_scale_count: int = 0
    last_rescue_ms: int = 0
    range_low: float | None = None
    range_high: float | None = None
    range_mid: float | None = None
    notional_multiplier: float = 1.0


@dataclass
class PendingEntry:
    setup: str
    side: str
    created_at_ms: int
    expiry_ms: int
    limit_price: float
    archetype: str
    target_usdc: float | None = None
    stop_usdc: float | None = None
    range_low: float | None = None
    range_high: float | None = None
    range_mid: float | None = None
    notional_multiplier: float = 1.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest an ETHUSDC short-term scalp strategy.")
    parser.add_argument("--symbol", default="ETHUSDC")
    parser.add_argument("--start-date", required=True, help="Start date in YYYY-MM-DD (Taipei).")
    parser.add_argument("--end-date", required=True, help="End date in YYYY-MM-DD (Taipei, inclusive).")
    parser.add_argument("--equity", type=float, default=150.0)
    parser.add_argument("--compounding", action="store_true")
    parser.add_argument("--long-only", action="store_true", help="Only allow long entries.")
    parser.add_argument("--short-only", action="store_true", help="Only allow short entries.")
    parser.add_argument("--allow-short", action="store_true", help="Allow short entries in addition to longs.")
    parser.add_argument("--margin-pct", type=float, default=8.0)
    parser.add_argument("--leverage", type=float, default=50.0)
    parser.add_argument("--take-profit-pct", type=float, default=0.40)
    parser.add_argument("--stop-loss-pct", type=float, default=0.28)
    parser.add_argument("--daily-max-loss-pct", type=float, default=3.0)
    parser.add_argument("--daily-target-pct", type=float, default=5.0)
    parser.add_argument("--max-hold-minutes", type=int, default=15)
    parser.add_argument("--cooldown-minutes", type=int, default=30)
    parser.add_argument("--max-consecutive-losses", type=int, default=2)
    parser.add_argument("--volume-ratio-min", type=float, default=1.1)
    parser.add_argument("--max-extension-atr", type=float, default=1.3)
    parser.add_argument("--min-breakout-atr", type=float, default=0.03)
    parser.add_argument("--allowed-hours", default="4,5,6,20,22,23", help="Comma-separated Taipei hours to allow, e.g. 4,5,6,20,22,23.")
    parser.add_argument("--output-json", default="")
    return parser.parse_args()


def date_range_to_ms(start_date: str, end_date: str) -> tuple[int, int]:
    start = datetime.fromisoformat(start_date).replace(tzinfo=TAIPEI)
    end = datetime.fromisoformat(end_date).replace(tzinfo=TAIPEI) + timedelta(days=1)
    return int(start.astimezone(timezone.utc).timestamp() * 1000), int(end.astimezone(timezone.utc).timestamp() * 1000)


def aggregate_timeframe(candles: list[Candle], bucket_ms: int) -> tuple[list[Candle], list[int | None]]:
    groups: dict[int, list[Candle]] = defaultdict(list)
    for candle in candles:
        bucket = candle.open_time_ms - (candle.open_time_ms % bucket_ms)
        groups[bucket].append(candle)
    aggregated: list[Candle] = []
    for bucket in sorted(groups):
        bars = groups[bucket]
        aggregated.append(
            Candle(
                open_time_ms=bucket,
                open=bars[0].open,
                high=max(item.high for item in bars),
                low=min(item.low for item in bars),
                close=bars[-1].close,
                volume=sum(item.volume for item in bars),
                quote_volume=sum(item.quote_volume for item in bars),
            )
        )
    bucket_to_index = {bar.open_time_ms: idx for idx, bar in enumerate(aggregated)}
    mapping: list[int | None] = []
    for candle in candles:
        current_bucket = candle.open_time_ms - (candle.open_time_ms % bucket_ms)
        prev_bucket = current_bucket - bucket_ms
        mapping.append(bucket_to_index.get(prev_bucket))
    return aggregated, mapping


def aggregate_five_minute(candles: list[Candle]) -> tuple[list[Candle], list[int | None]]:
    return aggregate_timeframe(candles, FIVE_MINUTE_MS)


def aggregate_one_hour(candles: list[Candle]) -> tuple[list[Candle], list[int | None]]:
    return aggregate_timeframe(candles, ONE_HOUR_MS)


def ema_series(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    multiplier = 2 / (period + 1)
    seed = mean(values[:period])
    result[period - 1] = seed
    prev = seed
    for idx in range(period, len(values)):
        prev = (values[idx] - prev) * multiplier + prev
        result[idx] = prev
    return result


def atr_series(candles: list[Candle], period: int) -> list[float | None]:
    if not candles:
        return []
    true_ranges: list[float] = []
    prev_close = candles[0].close
    for candle in candles:
        tr = max(candle.high - candle.low, abs(candle.high - prev_close), abs(candle.low - prev_close))
        true_ranges.append(tr)
        prev_close = candle.close
    result: list[float | None] = [None] * len(candles)
    if len(candles) < period:
        return result
    prev_atr = mean(true_ranges[:period])
    result[period - 1] = prev_atr
    for idx in range(period, len(candles)):
        prev_atr = ((prev_atr * (period - 1)) + true_ranges[idx]) / period
        result[idx] = prev_atr
    return result


def volume_sma_series(candles: list[Candle], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(candles)
    running = 0.0
    for idx, candle in enumerate(candles):
        running += candle.volume
        if idx >= period:
            running -= candles[idx - period].volume
        if idx >= period - 1:
            result[idx] = running / period
    return result


def anchored_daily_vwap(candles: list[Candle]) -> list[float | None]:
    result: list[float | None] = [None] * len(candles)
    current_day = None
    pv_sum = 0.0
    volume_sum = 0.0
    for idx, candle in enumerate(candles):
        day = datetime.fromtimestamp(candle.open_time_ms / 1000, tz=timezone.utc).astimezone(TAIPEI).date()
        if day != current_day:
            current_day = day
            pv_sum = 0.0
            volume_sum = 0.0
        typical = (candle.high + candle.low + candle.close) / 3
        pv_sum += typical * candle.volume
        volume_sum += candle.volume
        result[idx] = (pv_sum / volume_sum) if volume_sum > 0 else None
    return result


def day_key(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(TAIPEI).date().isoformat()


def time_label(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(TAIPEI).isoformat()


def in_session(ms: int, start_hour: int, end_hour: int, allowed_hours: tuple[int, ...] = ()) -> bool:
    hour = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(TAIPEI).hour
    if allowed_hours:
        return hour in set(allowed_hours)
    return start_hour <= hour <= end_hour


def is_trend_setup(setup: str) -> bool:
    return setup.startswith("trend") or setup == "strong_trend_follow"


def is_range_setup(setup: str) -> bool:
    return setup in {"range", "range_edge_scalp", "high_low_micro_scalp", "n_shape_scalp"}


def position_notional(equity: float, config: ManualScalpConfig) -> float:
    margin = equity * config.margin_pct / 100
    return margin * config.leverage


def open_position(
    setup: str,
    side: str,
    candle: Candle,
    equity: float,
    config: ManualScalpConfig,
    *,
    target_usdc: float | None = None,
    stop_usdc: float | None = None,
    range_low: float | None = None,
    range_high: float | None = None,
    range_mid: float | None = None,
    entry_price: float | None = None,
    notional_multiplier: float = 1.0,
) -> Position:
    fill_price = entry_price if entry_price is not None else candle.close
    notional = min(position_notional(equity, config) * notional_multiplier, config.max_initial_notional_usdc)
    qty = notional / fill_price
    margin = notional / max(config.leverage, 1e-9)
    if target_usdc is not None and stop_usdc is not None:
        tp_delta = target_usdc / qty
        stop_delta = stop_usdc / qty
        if side == "LONG":
            tp_price = fill_price + tp_delta
            stop_price = fill_price - stop_delta
        else:
            tp_price = fill_price - tp_delta
            stop_price = fill_price + stop_delta
    elif side == "LONG":
        tp_price = fill_price * (1 + config.take_profit_pct / 100)
        stop_price = fill_price * (1 - config.stop_loss_pct / 100)
    else:
        tp_price = fill_price * (1 - config.take_profit_pct / 100)
        stop_price = fill_price * (1 + config.stop_loss_pct / 100)
    return Position(
        setup=setup,
        side=side,
        opened_at_ms=candle.open_time_ms,
        entry_price=fill_price,
        qty=qty,
        margin_used=margin,
        tp_price=tp_price,
        stop_price=stop_price,
        entry_fee_rate=config.maker_fee_rate,
        best_price=fill_price,
        range_low=range_low,
        range_high=range_high,
        range_mid=range_mid,
        notional_multiplier=notional_multiplier,
    )


def close_position(position: Position, candle: Candle, exit_price: float, reason: str, exit_fee_rate: float) -> Trade:
    side_mult = 1 if position.side == "LONG" else -1
    gross = (exit_price - position.entry_price) * position.qty * side_mult
    entry_fees = position.entry_price * position.qty * position.entry_fee_rate
    exit_fees = exit_price * position.qty * exit_fee_rate
    fees = entry_fees + exit_fees
    return Trade(
        setup=position.setup,
        side=position.side,
        opened_at_ms=position.opened_at_ms,
        closed_at_ms=candle.open_time_ms,
        entry_price=position.entry_price,
        exit_price=exit_price,
        qty=position.qty,
        margin_used=position.margin_used,
        pnl_usdc=gross - fees,
        fees_usdc=fees,
        reason=reason,
        entry_fee_rate=position.entry_fee_rate,
        exit_fee_rate=exit_fee_rate,
    )


def close_position_fraction(
    position: Position,
    candle: Candle,
    exit_price: float,
    reason: str,
    exit_fee_rate: float,
    fraction: float,
    config: ManualScalpConfig,
) -> Trade:
    fraction = min(max(fraction, 0.0), 1.0)
    closed_qty = position.qty * fraction
    closed_margin = position.margin_used * fraction
    side_mult = 1 if position.side == "LONG" else -1
    gross = (exit_price - position.entry_price) * closed_qty * side_mult
    entry_fees = position.entry_price * closed_qty * position.entry_fee_rate
    exit_fees = exit_price * closed_qty * exit_fee_rate
    fees = entry_fees + exit_fees
    trade = Trade(
        setup=position.setup,
        side=position.side,
        opened_at_ms=position.opened_at_ms,
        closed_at_ms=candle.open_time_ms,
        entry_price=position.entry_price,
        exit_price=exit_price,
        qty=closed_qty,
        margin_used=closed_margin,
        pnl_usdc=gross - fees,
        fees_usdc=fees,
        reason=reason,
        entry_fee_rate=position.entry_fee_rate,
        exit_fee_rate=exit_fee_rate,
    )
    position.qty -= closed_qty
    position.margin_used -= closed_margin
    position.partial_taken = True
    position.partial_take_count += 1
    position.last_partial_scale_count = position.scale_count
    if position.qty > 0:
        if position.side == "LONG":
            position.tp_price = position.entry_price + config.rescue_runner_target_usdc / position.qty
            position.stop_price = position.entry_price - config.rescue_runner_stop_usdc / position.qty
        else:
            position.tp_price = position.entry_price - config.rescue_runner_target_usdc / position.qty
            position.stop_price = position.entry_price + config.rescue_runner_stop_usdc / position.qty
    return trade


def _retarget_position(position: Position, target_usdc: float, stop_usdc: float) -> None:
    if position.qty <= 0:
        return
    if position.side == "LONG":
        position.tp_price = position.entry_price + target_usdc / position.qty
        position.stop_price = position.entry_price - stop_usdc / position.qty
    else:
        position.tp_price = position.entry_price - target_usdc / position.qty
        position.stop_price = position.entry_price + stop_usdc / position.qty


def rescue_add_fraction(position: Position, config: ManualScalpConfig) -> float:
    index = max(position.scale_count, 0)
    if index < len(config.rescue_add_notional_fractions):
        return config.rescue_add_notional_fractions[index]
    return config.rescue_add_notional_fraction


def add_rescue_position(position: Position, candle: Candle, fill_price: float, add_notional: float, config: ManualScalpConfig) -> None:
    add_qty = add_notional / fill_price
    if add_qty <= 0:
        return
    total_qty = position.qty + add_qty
    position.entry_price = ((position.entry_price * position.qty) + (fill_price * add_qty)) / total_qty
    position.qty = total_qty
    position.margin_used += add_notional / max(config.leverage, 1e-9)
    position.scale_count += 1
    position.last_rescue_ms = candle.open_time_ms
    position.best_price = max(position.best_price, position.entry_price) if position.side == "LONG" else min(position.best_price, position.entry_price)
    target_usdc = config.range_target_usdc if is_range_setup(position.setup) else config.trend_target_usdc
    _retarget_position(position, target_usdc, config.rescue_total_stop_usdc)


def build_trend_pending_entry(
    side: str,
    candle: Candle,
    ema_fast: float,
    atr: float,
    archetype: str,
    config: ManualScalpConfig,
) -> PendingEntry:
    ttl_minutes = config.trend_limit_ttl_minutes
    if archetype in {"continuation", "strong_follow"}:
        ttl_minutes = config.continuation_limit_ttl_minutes
    if side == "LONG":
        if archetype in {"continuation", "strong_follow"}:
            limit_price = candle.close - atr * config.continuation_limit_offset_atr
        elif candle.close >= ema_fast:
            limit_price = ema_fast + atr * config.trend_limit_ema_buffer_atr
        else:
            limit_price = candle.close + atr * config.trend_limit_offset_atr
    else:
        if archetype in {"continuation", "strong_follow"}:
            limit_price = candle.close + atr * config.continuation_limit_offset_atr
        elif candle.close <= ema_fast:
            limit_price = ema_fast - atr * config.trend_limit_ema_buffer_atr
        else:
            limit_price = candle.close - atr * config.trend_limit_offset_atr
    setup = "strong_trend_follow" if archetype == "strong_follow" else f"trend_{archetype}"
    target_usdc = config.strong_trend_target_usdc if archetype == "strong_follow" else config.trend_target_usdc
    stop_usdc = config.strong_trend_stop_usdc if archetype == "strong_follow" else config.trend_stop_usdc
    notional_multiplier = config.strong_trend_notional_multiplier if archetype == "strong_follow" else 1.0
    return PendingEntry(
        setup=setup,
        side=side,
        created_at_ms=candle.open_time_ms,
        expiry_ms=candle.open_time_ms + ttl_minutes * ONE_MINUTE_MS,
        limit_price=limit_price,
        archetype=archetype,
        target_usdc=target_usdc,
        stop_usdc=stop_usdc,
        notional_multiplier=notional_multiplier,
    )


def try_fill_pending_entry(
    pending: PendingEntry,
    candle: Candle,
    equity: float,
    config: ManualScalpConfig,
) -> Position | None:
    if pending.side == "LONG":
        if candle.low > pending.limit_price:
            return None
    else:
        if candle.high < pending.limit_price:
            return None
    return open_position(
        pending.setup,
        pending.side,
        candle,
        equity,
        config,
        target_usdc=pending.target_usdc,
        stop_usdc=pending.stop_usdc,
        range_low=pending.range_low,
        range_high=pending.range_high,
        range_mid=pending.range_mid,
        entry_price=pending.limit_price,
        notional_multiplier=pending.notional_multiplier,
    )


def try_rescue_add(
    position: Position,
    candle: Candle,
    ema_fast: float | None,
    atr: float | None,
    config: ManualScalpConfig,
) -> bool:
    if (
        not config.rescue_enabled
        or position.setup in {"strong_trend_follow", "n_shape_scalp"}
        or (is_trend_setup(position.setup) and not config.rescue_trend_enabled)
        or (is_range_setup(position.setup) and not config.rescue_range_enabled)
        or position.scale_count >= config.rescue_max_adds
        or atr is None
        or atr <= 0
    ):
        return False
    if candle.open_time_ms < position.last_rescue_ms + config.rescue_min_spacing_minutes * ONE_MINUTE_MS:
        return False
    current_notional = position.entry_price * position.qty
    remaining_notional = config.rescue_max_notional_usdc - current_notional
    if remaining_notional <= 0:
        return False
    add_notional = min(current_notional * rescue_add_fraction(position, config), remaining_notional)
    if add_notional <= 0:
        return False

    step = config.rescue_step_atr * (position.scale_count + 1) * atr
    if position.side == "LONG":
        rescue_price = position.entry_price - step
        adverse_atr = (position.entry_price - candle.close) / atr
        structure_ok = ema_fast is None or candle.close >= ema_fast - atr * 0.45
        if (
            adverse_atr > config.rescue_max_adverse_atr
            or not structure_ok
            or candle.low > rescue_price
        ):
            return False
    else:
        rescue_price = position.entry_price + step
        adverse_atr = (candle.close - position.entry_price) / atr
        structure_ok = ema_fast is None or candle.close <= ema_fast + atr * 0.45
        if (
            adverse_atr > config.rescue_max_adverse_atr
            or not structure_ok
            or candle.high < rescue_price
        ):
            return False
    add_rescue_position(position, candle, rescue_price, add_notional, config)
    return True


def try_exit(
    position: Position,
    candle: Candle,
    ema_fast: float | None,
    vwap: float | None,
    max_hold_until_ms: int,
    config: ManualScalpConfig,
) -> Trade | None:
    elapsed_ms = candle.open_time_ms - position.opened_at_ms
    target_delta = abs(position.tp_price - position.entry_price)
    scratch_window_ms = config.trend_scratch_minutes * ONE_MINUTE_MS
    breakeven_delta = config.trend_breakeven_buffer_usdc / max(position.qty, 1e-9)

    def favorable_exit_fee_rate(exit_price: float) -> float:
        if position.side == "LONG":
            return config.maker_fee_rate if exit_price >= position.entry_price else config.taker_fee_rate
        return config.maker_fee_rate if exit_price <= position.entry_price else config.taker_fee_rate

    if position.side == "LONG":
        position.best_price = max(position.best_price, candle.high)
        best_progress = (
            (position.best_price - position.entry_price) / target_delta
            if target_delta > 0
            else 0.0
        )
        if candle.low <= position.stop_price:
            return close_position(position, candle, position.stop_price, "stop_loss", config.taker_fee_rate)
        if position.scale_count > position.last_partial_scale_count:
            partial_qty = position.qty * config.rescue_partial_fraction
            partial_target = position.entry_price + config.rescue_partial_take_usdc / max(partial_qty, 1e-9)
            if candle.high >= partial_target:
                return close_position_fraction(
                    position,
                    candle,
                    partial_target,
                    "partial_take_profit",
                    config.maker_fee_rate,
                    config.rescue_partial_fraction,
                    config,
                )
        if is_range_setup(position.setup):
            range_target = position.tp_price
            if position.range_mid is not None:
                range_target = min(range_target, position.range_mid)
            if candle.high >= range_target:
                return close_position(position, candle, range_target, "take_profit", config.maker_fee_rate)
        elif candle.high >= position.tp_price:
            return close_position(position, candle, position.tp_price, "take_profit", config.maker_fee_rate)
        if (
            is_trend_setup(position.setup)
            and elapsed_ms >= scratch_window_ms
            and best_progress < config.trend_scratch_progress_ratio
            and candle.close <= position.entry_price
            and (position.entry_price - candle.close) * position.qty >= config.trend_scratch_min_adverse_usdc
        ):
            return close_position(position, candle, candle.close, "scratch_exit", favorable_exit_fee_rate(candle.close))
        if (
            is_trend_setup(position.setup)
            and best_progress >= config.trend_breakeven_progress_ratio
            and candle.close <= position.entry_price + breakeven_delta
        ):
            return close_position(position, candle, candle.close, "giveback_exit", favorable_exit_fee_rate(candle.close))
        if candle.open_time_ms >= max_hold_until_ms:
            return close_position(position, candle, candle.close, "time_exit", favorable_exit_fee_rate(candle.close))
        if is_range_setup(position.setup):
            return None
        if ema_fast is not None and vwap is not None and candle.close < ema_fast and candle.close < vwap:
            return close_position(position, candle, candle.close, "trend_fail", config.taker_fee_rate)
        return None
    position.best_price = min(position.best_price, candle.low)
    best_progress = (
        (position.entry_price - position.best_price) / target_delta
        if target_delta > 0
        else 0.0
    )
    if candle.high >= position.stop_price:
        return close_position(position, candle, position.stop_price, "stop_loss", config.taker_fee_rate)
    if position.scale_count > position.last_partial_scale_count:
        partial_qty = position.qty * config.rescue_partial_fraction
        partial_target = position.entry_price - config.rescue_partial_take_usdc / max(partial_qty, 1e-9)
        if candle.low <= partial_target:
            return close_position_fraction(
                position,
                candle,
                partial_target,
                "partial_take_profit",
                config.maker_fee_rate,
                config.rescue_partial_fraction,
                config,
            )
    if is_range_setup(position.setup):
        range_target = position.tp_price
        if position.range_mid is not None:
            range_target = max(range_target, position.range_mid)
        if candle.low <= range_target:
            return close_position(position, candle, range_target, "take_profit", config.maker_fee_rate)
    elif candle.low <= position.tp_price:
        return close_position(position, candle, position.tp_price, "take_profit", config.maker_fee_rate)
    if (
        is_trend_setup(position.setup)
        and elapsed_ms >= scratch_window_ms
        and best_progress < config.trend_scratch_progress_ratio
        and candle.close >= position.entry_price
        and (candle.close - position.entry_price) * position.qty >= config.trend_scratch_min_adverse_usdc
    ):
        return close_position(position, candle, candle.close, "scratch_exit", favorable_exit_fee_rate(candle.close))
    if (
        is_trend_setup(position.setup)
        and best_progress >= config.trend_breakeven_progress_ratio
        and candle.close >= position.entry_price - breakeven_delta
    ):
        return close_position(position, candle, candle.close, "giveback_exit", favorable_exit_fee_rate(candle.close))
    if candle.open_time_ms >= max_hold_until_ms:
        return close_position(position, candle, candle.close, "time_exit", favorable_exit_fee_rate(candle.close))
    if is_range_setup(position.setup):
        return None
    if ema_fast is not None and vwap is not None and candle.close > ema_fast and candle.close > vwap:
        return close_position(position, candle, candle.close, "trend_fail", config.taker_fee_rate)
    return None


def _recent_market_alignment(
    five_idx: int | None,
    market_decisions_5m: list[object | None],
    *,
    predicate,
    lookback: int,
) -> int:
    if five_idx is None or five_idx < 0:
        return 0
    start = max(0, five_idx - lookback + 1)
    count = 0
    for idx in range(start, five_idx + 1):
        decision = market_decisions_5m[idx]
        if decision is not None and predicate(decision):
            count += 1
    return count


def classify_trend_long_entry(
    index: int,
    candles: list[Candle],
    ema_fast_1m: list[float | None],
    atr_1m: list[float | None],
    volume_sma: list[float | None],
    vwap_1m: list[float | None],
    five: list[Candle],
    five_map: list[int | None],
    ema20_5m: list[float | None],
    ema50_5m: list[float | None],
    config: ManualScalpConfig,
) -> bool:
    five_idx = five_map[index]
    if five_idx is None or five_idx < max(config.trend_ema_slow_5m - 1, config.trend_slope_bars_5m):
        return False
    ema20 = ema20_5m[five_idx]
    ema50 = ema50_5m[five_idx]
    ema20_prev = ema20_5m[five_idx - config.trend_slope_bars_5m]
    ema_fast = ema_fast_1m[index]
    atr = atr_1m[index]
    avg_vol = volume_sma[index]
    vwap = vwap_1m[index]
    if None in (ema20, ema50, ema20_prev, ema_fast, atr, avg_vol, vwap):
        return None
    if atr is None or atr <= 0:
        return None
    current = candles[index]
    previous = candles[index - 1]
    prior = candles[index - config.breakout_lookback:index]
    if len(prior) < config.breakout_lookback:
        return None
    if index < 15:
        return None
    breakout = current.close - max(item.high for item in prior)
    extension = (current.close - ema_fast) / atr
    directional_move_15m = (current.close - candles[index - 15].close) / atr
    recent_lows = [item.low for item in candles[max(0, index - config.trend_pullback_lookback): index]]
    pullback_touched = bool(recent_lows) and min(recent_lows) <= ema_fast + atr * config.trend_pullback_touch_atr
    reclaim = (current.close - ema_fast) / atr
    breakout_ok = (
        breakout / atr >= config.min_breakout_atr
        and 0 < extension <= config.max_extension_atr
        and current.volume >= avg_vol * config.volume_ratio_min
    )
    reclaim_ok = (
        pullback_touched
        and reclaim >= config.trend_reclaim_atr
        and reclaim <= config.trend_reclaim_max_extension_atr
        and reclaim >= config.trend_reclaim_min_extension_atr
        and directional_move_15m <= config.trend_max_directional_move_15m_atr
        and breakout <= config.trend_max_recent_breakout_atr * atr
        and extension <= config.trend_reclaim_max_extension_atr
        and current.close > current.open
        and current.close > previous.close
        and current.volume >= avg_vol * 0.85
    )
    trigger_ok = (
        (config.trend_allow_breakout and breakout_ok)
        or (config.trend_allow_reclaim and reclaim_ok)
    )
    base_ok = (
        ema20 > ema50
        and ema20 > ema20_prev
        and current.close >= ema_fast + atr * config.trend_reclaim_min_extension_atr
    )
    if config.enable_trend_pullback and base_ok and trigger_ok:
        return "pullback"
    continuation_ok = (
        ema20 > ema50
        and ema20 > ema20_prev
        and current.close > current.open
        and current.close > previous.close
        and current.volume >= avg_vol * config.continuation_min_volume_ratio
        and config.continuation_min_extension_atr <= extension <= config.continuation_max_extension_atr
        and config.continuation_min_move_15m_atr <= directional_move_15m <= config.continuation_max_move_15m_atr
    )
    strong_ok = (
        continuation_ok
        and config.strong_trend_enabled
        and current.volume >= avg_vol * config.strong_trend_min_volume_ratio
        and directional_move_15m >= config.strong_trend_min_move_15m_atr
        and config.strong_trend_min_extension_atr <= extension <= config.strong_trend_max_extension_atr
    )
    if strong_ok:
        return "strong_follow"
    return "continuation" if config.enable_trend_continuation and continuation_ok else None


def classify_trend_short_entry(
    index: int,
    candles: list[Candle],
    ema_fast_1m: list[float | None],
    atr_1m: list[float | None],
    volume_sma: list[float | None],
    vwap_1m: list[float | None],
    five: list[Candle],
    five_map: list[int | None],
    ema20_5m: list[float | None],
    ema50_5m: list[float | None],
    config: ManualScalpConfig,
) -> bool:
    five_idx = five_map[index]
    if five_idx is None or five_idx < max(config.trend_ema_slow_5m - 1, config.trend_slope_bars_5m):
        return False
    ema20 = ema20_5m[five_idx]
    ema50 = ema50_5m[five_idx]
    ema20_prev = ema20_5m[five_idx - config.trend_slope_bars_5m]
    ema_fast = ema_fast_1m[index]
    atr = atr_1m[index]
    avg_vol = volume_sma[index]
    vwap = vwap_1m[index]
    if None in (ema20, ema50, ema20_prev, ema_fast, atr, avg_vol, vwap):
        return None
    if atr is None or atr <= 0:
        return None
    current = candles[index]
    previous = candles[index - 1]
    prior = candles[index - config.breakout_lookback:index]
    if len(prior) < config.breakout_lookback:
        return None
    if index < 15:
        return None
    breakout = min(item.low for item in prior) - current.close
    extension = (ema_fast - current.close) / atr
    directional_move_15m = (candles[index - 15].close - current.close) / atr
    recent_highs = [item.high for item in candles[max(0, index - config.trend_pullback_lookback): index]]
    pullback_touched = bool(recent_highs) and max(recent_highs) >= ema_fast - atr * config.trend_pullback_touch_atr
    reclaim = (ema_fast - current.close) / atr
    breakout_ok = (
        breakout / atr >= config.min_breakout_atr
        and 0 < extension <= config.max_extension_atr
        and current.volume >= avg_vol * config.volume_ratio_min
    )
    reclaim_ok = (
        pullback_touched
        and reclaim >= config.trend_reclaim_atr
        and reclaim <= config.trend_reclaim_max_extension_atr
        and reclaim >= config.trend_reclaim_min_extension_atr
        and directional_move_15m <= config.trend_max_directional_move_15m_atr
        and breakout <= config.trend_max_recent_breakout_atr * atr
        and extension <= config.trend_reclaim_max_extension_atr
        and current.close < current.open
        and current.close < previous.close
        and current.volume >= avg_vol * 0.85
    )
    trigger_ok = (
        (config.trend_allow_breakout and breakout_ok)
        or (config.trend_allow_reclaim and reclaim_ok)
    )
    base_ok = (
        ema20 < ema50
        and ema20 < ema20_prev
        and current.close <= ema_fast - atr * config.trend_reclaim_min_extension_atr
    )
    if config.enable_trend_pullback and base_ok and trigger_ok:
        return "pullback"
    continuation_ok = (
        ema20 < ema50
        and ema20 < ema20_prev
        and current.close < current.open
        and current.close < previous.close
        and current.volume >= avg_vol * config.continuation_min_volume_ratio
        and config.continuation_min_extension_atr <= extension <= config.continuation_max_extension_atr
        and config.continuation_min_move_15m_atr <= directional_move_15m <= config.continuation_max_move_15m_atr
    )
    strong_ok = (
        continuation_ok
        and config.strong_trend_enabled
        and current.volume >= avg_vol * config.strong_trend_min_volume_ratio
        and directional_move_15m >= config.strong_trend_min_move_15m_atr
        and config.strong_trend_min_extension_atr <= extension <= config.strong_trend_max_extension_atr
    )
    if strong_ok:
        return "strong_follow"
    return "continuation" if config.enable_trend_continuation and continuation_ok else None


def hourly_bias(
    index: int,
    candles_1h: list[Candle],
    one_hour_map: list[int | None],
    ema_fast_1h: list[float | None],
    ema_slow_1h: list[float | None],
    config: ManualScalpConfig,
) -> str:
    hour_idx = one_hour_map[index]
    if hour_idx is None or hour_idx < max(config.hourly_bias_slow_period - 1, 1):
        return "neutral"
    ema_fast = ema_fast_1h[hour_idx]
    ema_slow = ema_slow_1h[hour_idx]
    prev_fast = ema_fast_1h[hour_idx - 1] if hour_idx >= 1 else None
    candle = candles_1h[hour_idx]
    if None in (ema_fast, ema_slow, prev_fast):
        return "neutral"
    if candle.close > ema_fast > ema_slow and ema_fast > prev_fast:
        return "up"
    if candle.close < ema_fast < ema_slow and ema_fast < prev_fast:
        return "down"
    return "neutral"


def trend_overheat_reason(
    index: int,
    side: str,
    candles: list[Candle],
    ema_fast_1m: list[float | None],
    atr_1m: list[float | None],
    config: ManualScalpConfig,
) -> str | None:
    if index < 15:
        return None
    ema_fast = ema_fast_1m[index]
    atr = atr_1m[index]
    if ema_fast is None or atr is None or atr <= 0:
        return None
    current = candles[index]
    if side == "LONG":
        distance_atr = (current.close - ema_fast) / atr
        move_15m_atr = (current.close - candles[index - 15].close) / atr
    else:
        distance_atr = (ema_fast - current.close) / atr
        move_15m_atr = (candles[index - 15].close - current.close) / atr
    distance_hot = distance_atr > config.overheat_max_distance_ema_atr
    move_hot = move_15m_atr > config.overheat_max_move_15m_atr
    if distance_hot and move_hot:
        return "distance_and_15m_move"
    if distance_hot:
        return "distance_to_ema"
    if move_hot:
        return "move_15m"
    return None


def _range_metrics(
    index: int,
    candles: list[Candle],
    atr_1m: list[float | None],
    five: list[Candle],
    five_map: list[int | None],
    config: ManualScalpConfig,
) -> tuple[float, float, float, float, float, int, int, float] | None:
    five_idx = five_map[index]
    atr = atr_1m[index]
    if five_idx is None or atr is None or atr <= 0:
        return None
    if five_idx < config.range_lookback_5m - 1:
        return None
    window = five[five_idx - config.range_lookback_5m + 1: five_idx + 1]
    range_low = min(item.low for item in window)
    range_high = max(item.high for item in window)
    width = range_high - range_low
    if width <= 0:
        return None
    width_atr = width / atr
    if width_atr < config.range_min_width_atr or width_atr > config.range_max_width_atr:
        return None
    drift = abs(window[-1].close - window[0].open)
    drift_width_ratio = drift / width if width > 0 else 1.0
    if drift_width_ratio > config.range_max_drift_width_ratio:
        return None
    tolerance = atr * config.range_touch_tolerance_atr
    low_touches = sum(1 for item in window if item.low <= range_low + tolerance)
    high_touches = sum(1 for item in window if item.high >= range_high - tolerance)
    if low_touches < config.range_min_touches_each_side or high_touches < config.range_min_touches_each_side:
        return None
    box_mid = range_low + width / 2
    return range_low, range_high, width, atr, box_mid, low_touches, high_touches, drift_width_ratio


def _supports_trend_long(decision: object | None, min_confidence: float) -> bool:
    if decision is None:
        return False
    playbook = getattr(decision, "playbook", "no_trade")
    risk_mode = getattr(decision, "risk_mode", "off")
    trend = getattr(decision, "trend", "unknown")
    confidence = float(getattr(decision, "confidence", 0.0) or 0.0)
    return (
        (playbook in {"long_breakout", "long_pullback"} and risk_mode != "off")
        or (trend == "trend_up" and confidence >= min_confidence)
    )


def _supports_trend_short(decision: object | None, min_confidence: float) -> bool:
    if decision is None:
        return False
    playbook = getattr(decision, "playbook", "no_trade")
    risk_mode = getattr(decision, "risk_mode", "off")
    trend = getattr(decision, "trend", "unknown")
    confidence = float(getattr(decision, "confidence", 0.0) or 0.0)
    return (
        (playbook == "short_breakdown" and risk_mode != "off")
        or (trend == "trend_down" and confidence >= min_confidence)
    )


def should_open_range_long(
    index: int,
    candles: list[Candle],
    atr_1m: list[float | None],
    five: list[Candle],
    five_map: list[int | None],
    config: ManualScalpConfig,
) -> bool:
    metrics = _range_metrics(index, candles, atr_1m, five, five_map, config)
    if metrics is None:
        return False
    current = candles[index]
    previous = candles[index - 1]
    range_low, range_high, width, _atr, box_mid, _low_touches, _high_touches, _drift_ratio = metrics
    position = (current.close - range_low) / width
    recent = candles[max(0, index - 3): index + 1]
    recent_low = min(item.low for item in recent)
    lower_wick = min(current.open, current.close) - current.low
    candle_range = max(current.high - current.low, 1e-9)
    return (
        position <= config.range_entry_zone
        and current.low <= range_low + width * config.range_entry_zone
        and current.close >= range_low + width * config.range_reentry_buffer
        and current.close > current.open
        and current.close > previous.close
        and ((current.close - box_mid) / width) <= config.range_max_midline_distance
        and lower_wick / candle_range >= config.range_reject_wick_ratio
        and recent_low <= range_low + width * config.range_entry_zone
    )


def should_open_range_short(
    index: int,
    candles: list[Candle],
    atr_1m: list[float | None],
    five: list[Candle],
    five_map: list[int | None],
    config: ManualScalpConfig,
) -> bool:
    metrics = _range_metrics(index, candles, atr_1m, five, five_map, config)
    if metrics is None:
        return False
    current = candles[index]
    previous = candles[index - 1]
    range_low, range_high, width, _atr, box_mid, _low_touches, _high_touches, _drift_ratio = metrics
    position = (current.close - range_low) / width
    recent = candles[max(0, index - 3): index + 1]
    recent_high = max(item.high for item in recent)
    upper_wick = current.high - max(current.open, current.close)
    candle_range = max(current.high - current.low, 1e-9)
    return (
        position >= 1 - config.range_entry_zone
        and current.high >= range_high - width * config.range_entry_zone
        and current.close <= range_high - width * config.range_reentry_buffer
        and current.close < current.open
        and current.close < previous.close
        and ((box_mid - current.close) / width) <= config.range_max_midline_distance
        and upper_wick / candle_range >= config.range_reject_wick_ratio
        and recent_high >= range_high - width * config.range_entry_zone
    )


def _high_low_micro_metrics(
    index: int,
    candles: list[Candle],
    atr_1m: list[float | None],
    volume_sma: list[float | None],
    config: ManualScalpConfig,
) -> tuple[float, float, float, float, float, int, int] | None:
    atr = atr_1m[index]
    avg_vol = volume_sma[index]
    if atr is None or atr <= 0 or avg_vol is None or avg_vol <= 0:
        return None
    lookback = config.high_low_micro_lookback_minutes
    if index < lookback + 1:
        return None
    window = candles[index - lookback:index]
    range_low = min(item.low for item in window)
    range_high = max(item.high for item in window)
    width = range_high - range_low
    if width <= 0:
        return None
    width_atr = width / atr
    if width_atr < config.high_low_micro_min_width_atr or width_atr > config.high_low_micro_max_width_atr:
        return None
    drift_ratio = abs(window[-1].close - window[0].open) / width
    if drift_ratio > config.high_low_micro_max_drift_width_ratio:
        return None
    tolerance = atr * config.high_low_micro_touch_tolerance_atr
    low_touches = sum(1 for item in window if item.low <= range_low + tolerance)
    high_touches = sum(1 for item in window if item.high >= range_high - tolerance)
    if (
        low_touches < config.high_low_micro_min_touches_each_side
        or high_touches < config.high_low_micro_min_touches_each_side
    ):
        return None
    return range_low, range_high, width, atr, range_low + width / 2, low_touches, high_touches


def build_high_low_micro_long_pending(
    index: int,
    candles: list[Candle],
    atr_1m: list[float | None],
    volume_sma: list[float | None],
    config: ManualScalpConfig,
) -> PendingEntry | None:
    if not config.high_low_micro_enabled:
        return None
    metrics = _high_low_micro_metrics(index, candles, atr_1m, volume_sma, config)
    if metrics is None:
        return None
    current = candles[index]
    previous = candles[index - 1]
    range_low, range_high, width, atr, box_mid, _low_touches, _high_touches = metrics
    position = (current.close - range_low) / width
    candle_range = max(current.high - current.low, 1e-9)
    lower_wick = min(current.open, current.close) - current.low
    body_ratio = abs(current.close - current.open) / candle_range
    recent_low = min(item.low for item in candles[max(0, index - 4): index + 1])
    avg_vol = volume_sma[index]
    if avg_vol is None or avg_vol <= 0:
        return None
    if not (
        position <= config.high_low_micro_entry_zone
        and current.low <= range_low + width * config.high_low_micro_entry_zone
        and current.close >= range_low + width * config.high_low_micro_reclaim_zone
        and current.close >= previous.close
        and current.close > current.open
        and lower_wick / candle_range >= config.high_low_micro_reject_wick_ratio
        and body_ratio >= config.high_low_micro_min_body_ratio
        and current.volume >= avg_vol * config.high_low_micro_min_volume_ratio
        and recent_low <= range_low + width * config.high_low_micro_entry_zone
        and current.close <= box_mid
    ):
        return None
    limit_price = current.close - atr * config.high_low_micro_limit_offset_atr
    return PendingEntry(
        setup="high_low_micro_scalp",
        side="LONG",
        created_at_ms=current.open_time_ms,
        expiry_ms=current.open_time_ms + config.high_low_micro_ttl_minutes * ONE_MINUTE_MS,
        limit_price=limit_price,
        archetype="high_low_micro",
        target_usdc=config.high_low_micro_target_usdc,
        stop_usdc=config.high_low_micro_stop_usdc,
        range_low=range_low,
        range_high=range_high,
        range_mid=box_mid,
    )


def should_open_n_shape_long(
    index: int,
    candles: list[Candle],
    ema_fast_1m: list[float | None],
    atr_1m: list[float | None],
    volume_sma: list[float | None],
    config: ManualScalpConfig,
) -> bool:
    if not config.n_shape_enabled or index < config.n_shape_lookback_minutes + 2:
        return False
    current = candles[index]
    previous = candles[index - 1]
    hour = datetime.fromtimestamp(current.open_time_ms / 1000, tz=timezone.utc).astimezone(TAIPEI).hour
    if config.n_shape_allowed_hours_taipei and hour not in set(config.n_shape_allowed_hours_taipei):
        return False
    ema_fast = ema_fast_1m[index]
    atr = atr_1m[index]
    avg_vol = volume_sma[index]
    if ema_fast is None or atr is None or atr <= 0 or avg_vol is None or avg_vol <= 0:
        return False

    window = candles[index - config.n_shape_lookback_minutes:index]
    swing_high_offset = max(range(len(window)), key=lambda item: window[item].high)
    if swing_high_offset >= len(window) - 3:
        return False
    swing_high = window[swing_high_offset].high
    after_high = window[swing_high_offset + 1:]
    swing_low = min(item.low for item in after_high)
    pullback = swing_high - swing_low
    if pullback < atr * config.n_shape_min_pullback_atr:
        return False

    body_ratio = abs(current.close - current.open) / max(current.high - current.low, 1e-9)
    reclaim_ratio = (current.close - swing_low) / max(pullback, 1e-9)
    recent_low = min(item.low for item in candles[max(0, index - 4): index + 1])
    touched_ma = recent_low <= ema_fast + atr * config.n_shape_ma_touch_atr
    extension = (current.close - ema_fast) / atr
    recent_break = current.close > max(item.high for item in candles[index - 3:index])
    return (
        touched_ma
        and current.close > current.open
        and current.close > previous.close
        and recent_break
        and body_ratio >= config.n_shape_min_body_ratio
        and reclaim_ratio >= config.n_shape_min_reclaim_ratio
        and current.volume >= avg_vol * config.n_shape_min_volume_ratio
        and -0.20 <= extension <= config.n_shape_max_extension_atr
        and current.close <= swing_high + atr * 0.25
    )


def run_backtest(candles: list[Candle], config: ManualScalpConfig) -> dict:
    if not candles:
        return {"trades": [], "summary": {}, "by_day": {}}
    five, five_map = aggregate_five_minute(candles)
    one_hour, one_hour_map = aggregate_one_hour(candles)
    closes_1m = [item.close for item in candles]
    closes_5m = [item.close for item in five]
    closes_1h = [item.close for item in one_hour]
    ema_fast_1m = ema_series(closes_1m, config.ema_fast_period)
    atr_1m = atr_series(candles, config.atr_period)
    volume_sma = volume_sma_series(candles, config.volume_lookback)
    vwap_1m = anchored_daily_vwap(candles)
    ema20_5m = ema_series(closes_5m, config.trend_ema_fast_5m)
    ema50_5m = ema_series(closes_5m, config.trend_ema_slow_5m)
    ema_fast_1h = ema_series(closes_1h, config.hourly_bias_fast_period)
    ema_slow_1h = ema_series(closes_1h, config.hourly_bias_slow_period)
    market_runtime = PullbackStrategyConfig(symbol=config.symbol)
    market_context_5m = build_market_state_context(five, market_runtime)
    market_decisions_5m = [
        classify_market_state(five, idx, market_context_5m, market_runtime)
        for idx in range(len(five))
    ]

    equity = config.equity_usdc
    position: Position | None = None
    pending_entry: PendingEntry | None = None
    max_hold_until_ms = 0
    cooldown_until_ms = 0
    consecutive_losses = 0
    day_start_equity: dict[str, float] = {}
    realized_by_day: defaultdict[str, float] = defaultdict(float)
    events_by_day: defaultdict[str, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))
    event_counts: defaultdict[str, int] = defaultdict(int)
    daily_setup_counts: defaultdict[str, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))
    last_trade_open_ms_by_setup: dict[str, int] = {}
    last_trend_open_ms_by_side: dict[str, int] = {}
    recent_trend_open_ms_by_side: defaultdict[str, list[int]] = defaultdict(list)
    daily_stop_days: set[str] = set()
    daily_target_days: set[str] = set()
    max_same_side_entries_in_window = 0
    trades: list[Trade] = []

    warmup = max(
        config.ema_fast_period,
        config.atr_period,
        config.volume_lookback,
        config.trend_ema_slow_5m * 5,
    ) + 2

    for index in range(warmup, len(candles)):
        candle = candles[index]
        day = day_key(candle.open_time_ms)
        day_start_equity.setdefault(day, equity if not config.compounding else max(equity - realized_by_day[day], 1e-9))
        day_pnl = realized_by_day[day]
        day_base_equity = day_start_equity[day]
        if position is not None:
            closed = try_exit(position, candle, ema_fast_1m[index], vwap_1m[index], max_hold_until_ms, config)
            if closed is not None:
                trades.append(closed)
                realized_by_day[day] += closed.pnl_usdc
                if config.compounding:
                    equity += closed.pnl_usdc
                is_partial_exit = closed.reason == "partial_take_profit"
                if not is_partial_exit:
                    position = None
                if closed.pnl_usdc < 0:
                    consecutive_losses += 1
                    if consecutive_losses >= config.max_consecutive_losses:
                        cooldown_until_ms = candle.open_time_ms + config.cooldown_minutes * ONE_MINUTE_MS
                        event_counts["cooldown_triggered"] += 1
                        events_by_day[day]["cooldown_triggered"] += 1
                else:
                    consecutive_losses = 0
                continue

        if position is not None:
            if try_rescue_add(position, candle, ema_fast_1m[index], atr_1m[index], config):
                event_counts["rescue_add"] += 1
                events_by_day[day]["rescue_add"] += 1
            continue
        if pending_entry is not None:
            if candle.open_time_ms > pending_entry.expiry_ms:
                pending_entry = None
            else:
                filled = try_fill_pending_entry(pending_entry, candle, equity, config)
                if filled is not None:
                    position = filled
                    day = day_key(candle.open_time_ms)
                    daily_setup_counts[day][filled.setup] += 1
                    if is_range_setup(filled.setup):
                        daily_setup_counts[day]["range"] += 1
                        last_trade_open_ms_by_setup["range"] = candle.open_time_ms
                    if is_trend_setup(filled.setup):
                        daily_setup_counts[day]["trend"] += 1
                    last_trade_open_ms_by_setup[filled.setup] = candle.open_time_ms
                    if is_trend_setup(filled.setup):
                        last_trade_open_ms_by_setup["trend"] = candle.open_time_ms
                        last_trend_open_ms_by_side[filled.side] = candle.open_time_ms
                        recent_trend_open_ms_by_side[filled.side].append(candle.open_time_ms)
                        max_same_side_entries_in_window = max(
                            max_same_side_entries_in_window,
                            len(recent_trend_open_ms_by_side[filled.side]),
                        )
                    hold_minutes = config.range_max_hold_minutes if is_range_setup(filled.setup) else config.trend_max_hold_minutes
                    max_hold_until_ms = candle.open_time_ms + hold_minutes * ONE_MINUTE_MS
                    pending_entry = None
                    continue
        if candle.open_time_ms < cooldown_until_ms:
            continue
        if not in_session(
            candle.open_time_ms,
            config.start_hour_taipei,
            config.end_hour_taipei,
            config.allowed_hours_taipei,
        ):
            continue
        if day_base_equity > 0 and day_pnl <= -(day_base_equity * config.daily_max_loss_pct / 100):
            if day not in daily_stop_days:
                daily_stop_days.add(day)
                event_counts["daily_stop_triggered"] += 1
                events_by_day[day]["daily_stop_triggered"] += 1
            continue
        if config.stop_after_daily_target and day_base_equity > 0 and day_pnl >= day_base_equity * config.daily_target_pct / 100:
            if day not in daily_target_days:
                daily_target_days.add(day)
                event_counts["daily_target_triggered"] += 1
                events_by_day[day]["daily_target_triggered"] += 1
            continue
        five_idx = five_map[index]
        market_decision = (
            market_decisions_5m[five_idx]
            if five_idx is not None and five_idx >= 0
            else None
        )
        bias_1h = hourly_bias(index, one_hour, one_hour_map, ema_fast_1h, ema_slow_1h, config)
        range_alignment = _recent_market_alignment(
            five_idx,
            market_decisions_5m,
            predicate=lambda decision: getattr(decision, "playbook", "no_trade") == "vwap_reversion"
            or getattr(decision, "trend", "unknown") == "range",
            lookback=config.range_alignment_lookback_5m,
        )
        long_alignment = _recent_market_alignment(
            five_idx,
            market_decisions_5m,
            predicate=lambda decision: _supports_trend_long(decision, config.trend_min_market_confidence),
            lookback=config.trend_alignment_lookback_5m,
        )
        short_alignment = _recent_market_alignment(
            five_idx,
            market_decisions_5m,
            predicate=lambda decision: _supports_trend_short(decision, config.trend_min_market_confidence),
            lookback=config.trend_alignment_lookback_5m,
        )
        long_recent_entries = [
            opened_ms
            for opened_ms in recent_trend_open_ms_by_side["LONG"]
            if candle.open_time_ms - opened_ms < config.trend_same_side_window_minutes * ONE_MINUTE_MS
        ]
        recent_trend_open_ms_by_side["LONG"] = long_recent_entries
        short_recent_entries = [
            opened_ms
            for opened_ms in recent_trend_open_ms_by_side["SHORT"]
            if candle.open_time_ms - opened_ms < config.trend_same_side_window_minutes * ONE_MINUTE_MS
        ]
        recent_trend_open_ms_by_side["SHORT"] = short_recent_entries
        max_same_side_entries_in_window = max(
            max_same_side_entries_in_window,
            len(long_recent_entries),
            len(short_recent_entries),
        )

        opened = None
        micro_pending_entry = None
        if (
            config.range_enabled
            and config.allow_long
            and daily_setup_counts[day]["range"] < config.max_range_trades_per_day
            and candle.open_time_ms >= last_trade_open_ms_by_setup.get("range", -10**18) + config.range_trade_spacing_minutes * ONE_MINUTE_MS
            and (market_decision is None or getattr(market_decision, "trend", "unknown") != "trend_down")
            and bias_1h != "down"
            and trend_overheat_reason(index, "LONG", candles, ema_fast_1m, atr_1m, config) is None
        ):
            micro_pending_entry = build_high_low_micro_long_pending(index, candles, atr_1m, volume_sma, config)
        if (
            config.range_enabled
            and config.allow_long
            and market_decision is not None
            and (market_decision.playbook == "vwap_reversion" or market_decision.trend == "range")
            and range_alignment >= config.range_min_alignment_bars
            and daily_setup_counts[day]["range"] < config.max_range_trades_per_day
            and candle.open_time_ms >= last_trade_open_ms_by_setup.get("range", -10**18) + config.range_trade_spacing_minutes * ONE_MINUTE_MS
            and should_open_range_long(index, candles, atr_1m, five, five_map, config)
        ):
            range_metrics = _range_metrics(index, candles, atr_1m, five, five_map, config)
            opened = open_position(
                "range_edge_scalp",
                "LONG",
                candle,
                equity,
                config,
                target_usdc=config.range_target_usdc,
                stop_usdc=config.range_stop_usdc,
                range_low=range_metrics[0] if range_metrics is not None else None,
                range_high=range_metrics[1] if range_metrics is not None else None,
                range_mid=range_metrics[4] if range_metrics is not None else None,
            )
        elif (
            config.range_enabled
            and config.allow_long
            and (market_decision is None or getattr(market_decision, "trend", "unknown") != "trend_down")
            and (bias_1h == "up" or long_alignment >= config.trend_min_alignment_bars)
            and daily_setup_counts[day]["range"] < config.max_range_trades_per_day
            and candle.open_time_ms >= last_trade_open_ms_by_setup.get("range", -10**18) + config.range_trade_spacing_minutes * ONE_MINUTE_MS
            and trend_overheat_reason(index, "LONG", candles, ema_fast_1m, atr_1m, config) is None
            and should_open_n_shape_long(index, candles, ema_fast_1m, atr_1m, volume_sma, config)
        ):
            opened = open_position(
                "n_shape_scalp",
                "LONG",
                candle,
                equity,
                config,
                target_usdc=config.n_shape_target_usdc,
                stop_usdc=config.n_shape_stop_usdc,
            )
        elif micro_pending_entry is not None:
            pending_entry = micro_pending_entry
        elif (
            config.range_enabled
            and config.allow_short
            and market_decision is not None
            and (market_decision.playbook == "vwap_reversion" or market_decision.trend == "range")
            and range_alignment >= config.range_min_alignment_bars
            and daily_setup_counts[day]["range"] < config.max_range_trades_per_day
            and candle.open_time_ms >= last_trade_open_ms_by_setup.get("range", -10**18) + config.range_trade_spacing_minutes * ONE_MINUTE_MS
            and should_open_range_short(index, candles, atr_1m, five, five_map, config)
        ):
            range_metrics = _range_metrics(index, candles, atr_1m, five, five_map, config)
            opened = open_position(
                "range_edge_scalp",
                "SHORT",
                candle,
                equity,
                config,
                target_usdc=config.range_target_usdc,
                stop_usdc=config.range_stop_usdc,
                range_low=range_metrics[0] if range_metrics is not None else None,
                range_high=range_metrics[1] if range_metrics is not None else None,
                range_mid=range_metrics[4] if range_metrics is not None else None,
            )
        else:
            long_archetype = None
            short_archetype = None
            long_bias_ok = (
                bias_1h == "up"
                or (
                    bias_1h == "neutral"
                    and long_alignment >= config.trend_min_alignment_bars
                )
            )
            short_bias_ok = (
                bias_1h == "down"
                or (
                    bias_1h == "neutral"
                    and short_alignment >= config.trend_min_alignment_bars
                )
            )
            long_gate_base = (
                config.allow_long
                and long_bias_ok
                and (market_decision is None or getattr(market_decision, "trend", "unknown") != "trend_down")
                and daily_setup_counts[day]["trend"] < config.max_trend_trades_per_day
                and candle.open_time_ms >= last_trade_open_ms_by_setup.get("trend", -10**18) + config.trend_trade_spacing_minutes * ONE_MINUTE_MS
                and candle.open_time_ms >= last_trend_open_ms_by_side.get("LONG", -10**18) + config.trend_same_side_spacing_minutes * ONE_MINUTE_MS
            )
            if long_gate_base:
                if len(long_recent_entries) >= config.trend_same_side_max_trades_in_window:
                    event_counts["same_side_window_block"] += 1
                    events_by_day[day]["same_side_window_block"] += 1
                else:
                    overheat_reason = trend_overheat_reason(index, "LONG", candles, ema_fast_1m, atr_1m, config)
                    if overheat_reason is not None:
                        event_counts["overheat_block"] += 1
                        event_counts[f"overheat_block_{overheat_reason}"] += 1
                        events_by_day[day]["overheat_block"] += 1
                    else:
                        long_archetype = classify_trend_long_entry(
                            index, candles, ema_fast_1m, atr_1m, volume_sma, vwap_1m, five, five_map, ema20_5m, ema50_5m, config
                        )
            short_gate_base = (
                config.allow_short
                and short_bias_ok
                and (market_decision is None or getattr(market_decision, "trend", "unknown") != "trend_up")
                and daily_setup_counts[day]["trend"] < config.max_trend_trades_per_day
                and candle.open_time_ms >= last_trade_open_ms_by_setup.get("trend", -10**18) + config.trend_trade_spacing_minutes * ONE_MINUTE_MS
                and candle.open_time_ms >= last_trend_open_ms_by_side.get("SHORT", -10**18) + config.trend_same_side_spacing_minutes * ONE_MINUTE_MS
            )
            if short_gate_base:
                if len(short_recent_entries) >= config.trend_same_side_max_trades_in_window:
                    event_counts["same_side_window_block"] += 1
                    events_by_day[day]["same_side_window_block"] += 1
                else:
                    overheat_reason = trend_overheat_reason(index, "SHORT", candles, ema_fast_1m, atr_1m, config)
                    if overheat_reason is not None:
                        event_counts["overheat_block"] += 1
                        event_counts[f"overheat_block_{overheat_reason}"] += 1
                        events_by_day[day]["overheat_block"] += 1
                    else:
                        short_archetype = classify_trend_short_entry(
                            index, candles, ema_fast_1m, atr_1m, volume_sma, vwap_1m, five, five_map, ema20_5m, ema50_5m, config
                        )
            if long_archetype is not None:
                pending_entry = build_trend_pending_entry("LONG", candle, ema_fast_1m[index], atr_1m[index], long_archetype, config)
            elif short_archetype is not None:
                pending_entry = build_trend_pending_entry("SHORT", candle, ema_fast_1m[index], atr_1m[index], short_archetype, config)
        if opened is not None:
            position = opened
            daily_setup_counts[day][opened.setup] += 1
            if is_range_setup(opened.setup):
                daily_setup_counts[day]["range"] += 1
                last_trade_open_ms_by_setup["range"] = candle.open_time_ms
            if is_trend_setup(opened.setup):
                daily_setup_counts[day]["trend"] += 1
            last_trade_open_ms_by_setup[opened.setup] = candle.open_time_ms
            if is_trend_setup(opened.setup):
                last_trade_open_ms_by_setup["trend"] = candle.open_time_ms
                last_trend_open_ms_by_side[opened.side] = candle.open_time_ms
                recent_trend_open_ms_by_side[opened.side].append(candle.open_time_ms)
                max_same_side_entries_in_window = max(
                    max_same_side_entries_in_window,
                    len(recent_trend_open_ms_by_side[opened.side]),
                )
            hold_minutes = config.range_max_hold_minutes if is_range_setup(opened.setup) else config.trend_max_hold_minutes
            max_hold_until_ms = candle.open_time_ms + hold_minutes * ONE_MINUTE_MS

    if position is not None:
        last = candles[-1]
        closed = close_position(position, last, last.close, "force_close", config.taker_fee_rate)
        trades.append(closed)
        realized_by_day[day_key(last.open_time_ms)] += closed.pnl_usdc
        if config.compounding:
            equity += closed.pnl_usdc

    wins = [trade for trade in trades if trade.pnl_usdc > 0]
    losses = [trade for trade in trades if trade.pnl_usdc < 0]
    gross_profit = sum(trade.pnl_usdc for trade in wins)
    gross_loss = abs(sum(trade.pnl_usdc for trade in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    trades_by_day: defaultdict[str, list[Trade]] = defaultdict(list)
    for trade in trades:
        trades_by_day[day_key(trade.opened_at_ms)].append(trade)
    by_day = {}
    for day, pnl in sorted(realized_by_day.items()):
        day_trades = trades_by_day.get(day, [])
        day_entry_makers = sum(1 for trade in day_trades if trade.entry_fee_rate <= config.maker_fee_rate)
        by_day[day] = {
            "pnl_usdc": pnl,
            "pnl_pct": (pnl / day_start_equity[day] * 100) if day_start_equity[day] else 0.0,
            "trades": len(day_trades),
            "maker_entry_ratio": (day_entry_makers / len(day_trades)) if day_trades else 0.0,
            "taker_entry_trades": sum(1 for trade in day_trades if trade.entry_fee_rate > config.maker_fee_rate),
            "worst_trade_usdc": min((trade.pnl_usdc for trade in day_trades), default=0.0),
            "events": dict(sorted(events_by_day[day].items())),
        }
    maker_entry_trades = sum(1 for trade in trades if trade.entry_fee_rate <= config.maker_fee_rate)
    taker_entry_trades = sum(1 for trade in trades if trade.entry_fee_rate > config.maker_fee_rate)
    maker_exit_trades = sum(1 for trade in trades if trade.exit_fee_rate <= config.maker_fee_rate)
    risk_event_counts = {
        "overheat_block": event_counts["overheat_block"],
        "overheat_block_distance_to_ema": event_counts["overheat_block_distance_to_ema"],
        "overheat_block_move_15m": event_counts["overheat_block_move_15m"],
        "overheat_block_distance_and_15m_move": event_counts["overheat_block_distance_and_15m_move"],
        "same_side_window_block": event_counts["same_side_window_block"],
        "rescue_add": event_counts["rescue_add"],
        "cooldown_triggered": event_counts["cooldown_triggered"],
        "daily_stop_triggered": event_counts["daily_stop_triggered"],
        "daily_target_triggered": event_counts["daily_target_triggered"],
    }
    return {
        "trades": [asdict(trade) for trade in trades],
        "by_day": by_day,
        "summary": {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / len(trades)) if trades else 0.0,
            "net_pnl_usdc": sum(trade.pnl_usdc for trade in trades),
            "fees_usdc": sum(trade.fees_usdc for trade in trades),
            "maker_entry_trades": maker_entry_trades,
            "taker_entry_trades": taker_entry_trades,
            "maker_entry_ratio": (maker_entry_trades / len(trades)) if trades else 0.0,
            "maker_exit_trades": maker_exit_trades,
            "maker_exit_ratio": (maker_exit_trades / len(trades)) if trades else 0.0,
            "avg_trade_usdc": (sum(trade.pnl_usdc for trade in trades) / len(trades)) if trades else 0.0,
            "best_trade_usdc": max((trade.pnl_usdc for trade in trades), default=0.0),
            "worst_trade_usdc": min((trade.pnl_usdc for trade in trades), default=0.0),
            "profit_factor": profit_factor,
            "avg_daily_pct": mean(item["pnl_pct"] for item in by_day.values()) if by_day else 0.0,
            "trend_trades": sum(1 for trade in trades if is_trend_setup(trade.setup)),
            "trend_pullback_trades": sum(1 for trade in trades if trade.setup == "trend_pullback"),
            "trend_continuation_trades": sum(1 for trade in trades if trade.setup == "trend_continuation"),
            "strong_trend_follow_trades": sum(1 for trade in trades if trade.setup == "strong_trend_follow"),
            "range_trades": sum(1 for trade in trades if is_range_setup(trade.setup)),
            "range_edge_scalp_trades": sum(1 for trade in trades if trade.setup == "range_edge_scalp"),
            "high_low_micro_scalp_trades": sum(1 for trade in trades if trade.setup == "high_low_micro_scalp"),
            "n_shape_scalp_trades": sum(1 for trade in trades if trade.setup == "n_shape_scalp"),
            "partial_take_profit_trades": sum(1 for trade in trades if trade.reason == "partial_take_profit"),
            "max_same_side_trades_in_window": max_same_side_entries_in_window,
            "risk_event_counts": risk_event_counts,
        },
    }


def print_result(result: dict) -> None:
    summary = result["summary"]
    print("Summary")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print()
    print("By Day")
    print(json.dumps(result["by_day"], ensure_ascii=False, indent=2))
    print()
    print("Recent Trades")
    for trade in result["trades"][-12:]:
        print(
            json.dumps(
                {
                    "opened_at": time_label(trade["opened_at_ms"]),
                    "closed_at": time_label(trade["closed_at_ms"]),
                    "setup": trade["setup"],
                    "side": trade["side"],
                    "entry": round(trade["entry_price"], 4),
                    "exit": round(trade["exit_price"], 4),
                    "pnl_usdc": round(trade["pnl_usdc"], 4),
                    "reason": trade["reason"],
                },
                ensure_ascii=False,
            )
        )


def main() -> int:
    args = parse_args()
    start_ms, end_ms = date_range_to_ms(args.start_date, args.end_date)
    candles = fetch_klines(BINANCE_FAPI_BASE, args.symbol, "1m", start_ms, end_ms)
    allowed_hours = tuple(
        int(item.strip())
        for item in args.allowed_hours.split(",")
        if item.strip()
    )
    config = ManualScalpConfig(
        symbol=args.symbol,
        equity_usdc=args.equity,
        compounding=args.compounding,
        allow_long=not args.short_only,
        allow_short=args.short_only or (args.allow_short and not args.long_only),
        margin_pct=args.margin_pct,
        leverage=args.leverage,
        take_profit_pct=args.take_profit_pct,
        stop_loss_pct=args.stop_loss_pct,
        daily_max_loss_pct=args.daily_max_loss_pct,
        daily_target_pct=args.daily_target_pct,
        max_hold_minutes=args.max_hold_minutes,
        cooldown_minutes=args.cooldown_minutes,
        max_consecutive_losses=args.max_consecutive_losses,
        volume_ratio_min=args.volume_ratio_min,
        max_extension_atr=args.max_extension_atr,
        min_breakout_atr=args.min_breakout_atr,
        allowed_hours_taipei=allowed_hours,
    )
    result = run_backtest(candles, config)
    result["config"] = asdict(config)
    print_result(result)
    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
