from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_manual_eth_scalp import (
    BINANCE_FAPI_BASE,
    ONE_MINUTE_MS,
    TAIPEI,
    Candle,
    ManualScalpConfig,
    PendingEntry,
    aggregate_five_minute,
    aggregate_one_hour,
    anchored_daily_vwap,
    atr_series,
    close_position,
    date_range_to_ms,
    ema_series,
    fetch_klines,
    hourly_bias,
    open_position,
    time_label,
    try_exit,
    try_fill_pending_entry,
    try_rescue_add,
    volume_sma_series,
)


@dataclass(frozen=True)
class ManualGuardConfig:
    symbol: str = "ETHUSDC"
    equity_usdc: float = 150.0
    leverage: float = 100.0
    margin_pct: float = 8.0
    maker_fee_rate: float = 0.0
    taker_fee_rate: float = 0.0004
    max_notional_usdc: float = 8000.0
    target_usdc: float = 0.8
    stop_usdc: float = 0.45
    high_conf_target_usdc: float = 1.5
    high_conf_stop_usdc: float = 0.75
    high_conf_notional_multiplier: float = 1.45
    rescue_enabled: bool = True
    rescue_max_adds: int = 1
    rescue_step_atr: float = 0.55
    rescue_max_adverse_atr: float = 1.45
    rescue_add_notional_fractions: tuple[float, ...] = (0.45,)
    rescue_max_notional_usdc: float = 2500.0
    rescue_total_stop_usdc: float = 1.35
    rescue_partial_fraction: float = 0.45
    rescue_partial_take_usdc: float = 0.45
    rescue_runner_target_usdc: float = 0.75
    rescue_runner_stop_usdc: float = 0.65
    limit_offset_atr: float = 0.06
    limit_ttl_minutes: int = 2
    max_hold_minutes: int = 10
    cooldown_minutes: int = 30
    max_consecutive_losses: int = 2
    trade_spacing_minutes: int = 4
    same_side_window_minutes: int = 180
    same_side_max_trades: int = 3
    max_trades_per_day: int = 28
    daily_max_loss_pct: float = 6.0
    daily_target_pct: float = 5.0
    stop_after_daily_target: bool = True
    max_directional_ema_atr: float = 2.0
    max_directional_move_15m_atr: float = 3.0
    min_session_range_30m_atr: float = 3.0
    max_session_range_30m_atr: float = 10.0
    max_directional_breakout_3bar_atr: float = 0.5
    reversion_breakout_3bar_max_atr: float = -0.55
    reversion_move_5m_max_atr: float = -0.35
    reversion_min_directional_ema_atr: float = -3.5
    min_volume_ratio: float = 0.2
    max_volume_ratio: float = 2.5
    close_reversion_enabled: bool = False
    range_edge_enabled: bool = True
    range_lookback_minutes: int = 45
    range_min_width_atr: float = 2.0
    range_max_width_atr: float = 10.0
    range_max_drift_width_ratio: float = 0.75
    range_entry_zone: float = 0.35
    range_reclaim_zone: float = 0.10
    range_limit_buffer_atr: float = 0.08
    range_min_move_5m_atr: float = 0.35
    range_touch_tolerance_atr: float = 0.45
    range_min_touches_each_side: int = 1
    range_min_distance_from_mid: float = 0.10
    range_reject_wick_ratio: float = 0.20
    range_min_body_ratio: float = 0.20
    range_reject_lookback_minutes: int = 4
    range_limit_from_close: bool = True
    allowed_hours_taipei: tuple[int, ...] = ()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest a manual-style ETHUSDC guard model.")
    parser.add_argument("--symbol", default="ETHUSDC")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--daily-target-pct", type=float, default=5.0)
    parser.add_argument("--output-json", default="")
    parser.add_argument("--allowed-hours", default="")
    return parser.parse_args()


def _hour(ms: int) -> int:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(TAIPEI).hour


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(TAIPEI).date().isoformat()


def _manual_runtime_config(config: ManualGuardConfig) -> ManualScalpConfig:
    return ManualScalpConfig(
        symbol=config.symbol,
        equity_usdc=config.equity_usdc,
        leverage=config.leverage,
        margin_pct=config.margin_pct,
        maker_fee_rate=config.maker_fee_rate,
        taker_fee_rate=config.taker_fee_rate,
        max_initial_notional_usdc=config.max_notional_usdc,
        daily_max_loss_pct=config.daily_max_loss_pct,
        daily_target_pct=config.daily_target_pct,
        stop_after_daily_target=config.stop_after_daily_target,
        cooldown_minutes=config.cooldown_minutes,
        max_consecutive_losses=config.max_consecutive_losses,
        rescue_enabled=config.rescue_enabled,
        rescue_range_enabled=config.rescue_enabled,
        rescue_trend_enabled=False,
        rescue_max_adds=config.rescue_max_adds,
        rescue_step_atr=config.rescue_step_atr,
        rescue_max_adverse_atr=config.rescue_max_adverse_atr,
        rescue_add_notional_fractions=config.rescue_add_notional_fractions,
        rescue_max_notional_usdc=config.rescue_max_notional_usdc,
        rescue_total_stop_usdc=config.rescue_total_stop_usdc,
        rescue_partial_fraction=config.rescue_partial_fraction,
        rescue_partial_take_usdc=config.rescue_partial_take_usdc,
        rescue_runner_target_usdc=config.rescue_runner_target_usdc,
        rescue_runner_stop_usdc=config.rescue_runner_stop_usdc,
        range_target_usdc=config.target_usdc,
        range_stop_usdc=config.stop_usdc,
        range_max_hold_minutes=config.max_hold_minutes,
    )


def _manual_features(
    index: int,
    side: str,
    candles: list[Candle],
    ema21: list[float | None],
    atr_1m: list[float | None],
    volume_sma: list[float | None],
    vwap_1m: list[float | None],
) -> dict[str, float] | None:
    if index < 30:
        return None
    candle = candles[index]
    atr = atr_1m[index]
    ema = ema21[index]
    avg_vol = volume_sma[index]
    vwap = vwap_1m[index]
    if atr is None or atr <= 0 or ema is None or avg_vol is None or avg_vol <= 0 or vwap is None:
        return None
    direction = 1 if side == "LONG" else -1
    prior_3 = candles[index - 3:index]
    breakout_level = max(item.high for item in prior_3) if direction > 0 else min(item.low for item in prior_3)
    breakout = (candle.close - breakout_level) * direction / atr
    move_5m = (candle.close - candles[index - 5].close) * direction / atr
    move_15m = (candle.close - candles[index - 15].close) * direction / atr
    session_range = (max(item.high for item in candles[index - 30:index]) - min(item.low for item in candles[index - 30:index])) / atr
    distance_ema = (candle.close - ema) * direction / atr
    distance_vwap = (candle.close - vwap) * direction / atr
    return {
        "directional_distance_to_ema21_atr": distance_ema,
        "directional_distance_to_vwap_atr": distance_vwap,
        "directional_breakout_3bar_atr": breakout,
        "directional_move_5m_atr": move_5m,
        "directional_move_15m_atr": move_15m,
        "session_range_30m_atr": session_range,
        "volume_ratio": candle.volume / avg_vol,
        "atr_1m": atr,
    }


def _is_high_confidence(features: dict[str, float]) -> bool:
    return (
        -2.5 <= features["directional_breakout_3bar_atr"] <= -1.0
        and abs(features["directional_distance_to_ema21_atr"]) <= 1.2
        and features["directional_move_15m_atr"] <= 2.0
        and features["volume_ratio"] >= 0.7
    )


def _range_edge_features(
    index: int,
    side: str,
    candles: list[Candle],
    ema21: list[float | None],
    atr_1m: list[float | None],
    volume_sma: list[float | None],
    vwap_1m: list[float | None],
    config: ManualGuardConfig,
) -> dict[str, float] | None:
    features = _manual_features(index, side, candles, ema21, atr_1m, volume_sma, vwap_1m)
    if features is None:
        return None
    lookback = config.range_lookback_minutes
    if index < lookback + 15:
        return None
    atr = features["atr_1m"]
    current = candles[index]
    window = candles[index - lookback:index]
    range_low = min(item.low for item in window)
    range_high = max(item.high for item in window)
    width = range_high - range_low
    if width <= 0:
        return None
    width_atr = width / atr
    if width_atr < config.range_min_width_atr or width_atr > config.range_max_width_atr:
        return None
    drift_ratio = abs(window[-1].close - window[0].open) / width
    if drift_ratio > config.range_max_drift_width_ratio:
        return None
    tolerance = atr * config.range_touch_tolerance_atr
    low_touches = sum(1 for item in window if item.low <= range_low + tolerance)
    high_touches = sum(1 for item in window if item.high >= range_high - tolerance)
    if low_touches < config.range_min_touches_each_side or high_touches < config.range_min_touches_each_side:
        return None
    position_in_range = (current.close - range_low) / width
    box_mid = range_low + width / 2
    features.update(
        {
            "range_low": range_low,
            "range_high": range_high,
            "range_mid": box_mid,
            "range_width": width,
            "range_width_atr": width_atr,
            "range_drift_width_ratio": drift_ratio,
            "range_position": position_in_range,
            "range_low_touches": float(low_touches),
            "range_high_touches": float(high_touches),
        }
    )
    return features


def build_range_edge_pending(
    index: int,
    side: str,
    candles: list[Candle],
    ema21: list[float | None],
    atr_1m: list[float | None],
    volume_sma: list[float | None],
    vwap_1m: list[float | None],
    config: ManualGuardConfig,
) -> tuple[PendingEntry, dict[str, float]] | None:
    if not config.range_edge_enabled:
        return None
    features = _range_edge_features(index, side, candles, ema21, atr_1m, volume_sma, vwap_1m, config)
    if features is None:
        return None
    current = candles[index]
    atr = features["atr_1m"]
    width = features["range_width"]
    position_in_range = features["range_position"]

    if (
        features["directional_distance_to_ema21_atr"] > config.max_directional_ema_atr
        or features["directional_distance_to_ema21_atr"] < config.reversion_min_directional_ema_atr
        or features["directional_move_15m_atr"] > config.max_directional_move_15m_atr
        or features["directional_breakout_3bar_atr"] > config.max_directional_breakout_3bar_atr
        or features["directional_move_5m_atr"] > -config.range_min_move_5m_atr
        or features["session_range_30m_atr"] < config.min_session_range_30m_atr
        or features["session_range_30m_atr"] > config.max_session_range_30m_atr
        or features["volume_ratio"] < config.min_volume_ratio
        or features["volume_ratio"] > config.max_volume_ratio
    ):
        return None

    if side == "LONG":
        recent = candles[max(0, index - config.range_reject_lookback_minutes):index + 1]
        candle_range = max(current.high - current.low, 1e-9)
        lower_wick = min(current.open, current.close) - current.low
        body_ratio = abs(current.close - current.open) / candle_range
        recent_low = min(item.low for item in recent)
        if position_in_range > config.range_entry_zone:
            return None
        if (0.5 - position_in_range) < config.range_min_distance_from_mid:
            return None
        if not (
            current.low <= features["range_low"] + width * config.range_entry_zone
            and current.close >= features["range_low"] + width * config.range_reclaim_zone
            and current.close >= candles[index - 1].close
            and current.close > current.open
            and lower_wick / candle_range >= config.range_reject_wick_ratio
            and body_ratio >= config.range_min_body_ratio
            and recent_low <= features["range_low"] + width * config.range_entry_zone
        ):
            return None
        if config.range_limit_from_close:
            limit_price = current.close - atr * config.limit_offset_atr
        else:
            limit_price = features["range_low"] + atr * config.range_limit_buffer_atr
        if limit_price >= current.close:
            limit_price = current.close - atr * config.limit_offset_atr
        if limit_price > features["range_low"] + width * config.range_entry_zone:
            return None
    else:
        recent = candles[max(0, index - config.range_reject_lookback_minutes):index + 1]
        candle_range = max(current.high - current.low, 1e-9)
        upper_wick = current.high - max(current.open, current.close)
        body_ratio = abs(current.close - current.open) / candle_range
        recent_high = max(item.high for item in recent)
        if position_in_range < 1 - config.range_entry_zone:
            return None
        if (position_in_range - 0.5) < config.range_min_distance_from_mid:
            return None
        if not (
            current.high >= features["range_high"] - width * config.range_entry_zone
            and current.close <= features["range_high"] - width * config.range_reclaim_zone
            and current.close <= candles[index - 1].close
            and current.close < current.open
            and upper_wick / candle_range >= config.range_reject_wick_ratio
            and body_ratio >= config.range_min_body_ratio
            and recent_high >= features["range_high"] - width * config.range_entry_zone
        ):
            return None
        if config.range_limit_from_close:
            limit_price = current.close + atr * config.limit_offset_atr
        else:
            limit_price = features["range_high"] - atr * config.range_limit_buffer_atr
        if limit_price <= current.close:
            limit_price = current.close + atr * config.limit_offset_atr
        if limit_price < features["range_high"] - width * config.range_entry_zone:
            return None

    high_conf = _is_high_confidence(features) and features["range_drift_width_ratio"] <= 0.55
    return (
        PendingEntry(
            setup="range_edge_scalp",
            side=side,
            created_at_ms=current.open_time_ms,
            expiry_ms=current.open_time_ms + config.limit_ttl_minutes * ONE_MINUTE_MS,
            limit_price=limit_price,
            archetype="manual_range_edge_high_conf" if high_conf else "manual_range_edge",
            target_usdc=config.high_conf_target_usdc if high_conf else config.target_usdc,
            stop_usdc=config.high_conf_stop_usdc if high_conf else config.stop_usdc,
            range_low=features["range_low"],
            range_high=features["range_high"],
            range_mid=features["range_mid"],
            notional_multiplier=config.high_conf_notional_multiplier if high_conf else 1.0,
        ),
        features,
    )


def build_manual_pending(
    index: int,
    side: str,
    candles: list[Candle],
    ema21: list[float | None],
    atr_1m: list[float | None],
    volume_sma: list[float | None],
    vwap_1m: list[float | None],
    config: ManualGuardConfig,
) -> tuple[PendingEntry, dict[str, float]] | None:
    range_pending = build_range_edge_pending(index, side, candles, ema21, atr_1m, volume_sma, vwap_1m, config)
    if range_pending is not None:
        return range_pending
    if not config.close_reversion_enabled:
        return None
    features = _manual_features(index, side, candles, ema21, atr_1m, volume_sma, vwap_1m)
    if features is None:
        return None
    if (
        features["directional_distance_to_ema21_atr"] > config.max_directional_ema_atr
        or features["directional_distance_to_ema21_atr"] < config.reversion_min_directional_ema_atr
        or features["directional_move_15m_atr"] > config.max_directional_move_15m_atr
        or features["directional_breakout_3bar_atr"] > config.max_directional_breakout_3bar_atr
        or features["directional_breakout_3bar_atr"] > config.reversion_breakout_3bar_max_atr
        or features["directional_move_5m_atr"] > config.reversion_move_5m_max_atr
        or features["session_range_30m_atr"] < config.min_session_range_30m_atr
        or features["session_range_30m_atr"] > config.max_session_range_30m_atr
        or features["volume_ratio"] < config.min_volume_ratio
    ):
        return None
    candle = candles[index]
    atr = features["atr_1m"]
    high_conf = _is_high_confidence(features)
    if side == "LONG":
        limit_price = candle.close - atr * config.limit_offset_atr
    else:
        limit_price = candle.close + atr * config.limit_offset_atr
    return (
        PendingEntry(
            setup="manual_guard_style",
            side=side,
            created_at_ms=candle.open_time_ms,
            expiry_ms=candle.open_time_ms + config.limit_ttl_minutes * ONE_MINUTE_MS,
            limit_price=limit_price,
            archetype="manual_guard_high_conf" if high_conf else "manual_guard",
            target_usdc=config.high_conf_target_usdc if high_conf else config.target_usdc,
            stop_usdc=config.high_conf_stop_usdc if high_conf else config.stop_usdc,
            notional_multiplier=config.high_conf_notional_multiplier if high_conf else 1.0,
        ),
        features,
    )


def run_backtest(
    candles: list[Candle],
    config: ManualGuardConfig,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> dict:
    runtime_config = _manual_runtime_config(config)
    closes = [candle.close for candle in candles]
    ema21 = ema_series(closes, 21)
    atr_1m = atr_series(candles, 14)
    volume_sma = volume_sma_series(candles, 20)
    vwap_1m = anchored_daily_vwap(candles)
    one_hour, one_hour_map = aggregate_one_hour(candles)
    closes_1h = [item.close for item in one_hour]
    ema_fast_1h = ema_series(closes_1h, 8)
    ema_slow_1h = ema_series(closes_1h, 21)
    aggregate_five_minute(candles)  # Keep parity with indicator warmup used elsewhere.

    position = None
    pending_entry: PendingEntry | None = None
    max_hold_until_ms = 0
    cooldown_until_ms = 0
    consecutive_losses = 0
    equity = config.equity_usdc
    trades = []
    realized_by_day: defaultdict[str, float] = defaultdict(float)
    day_start_equity: dict[str, float] = {}
    event_counts: defaultdict[str, int] = defaultdict(int)
    events_by_day: defaultdict[str, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))
    recent_by_side: defaultdict[str, list[int]] = defaultdict(list)
    last_trade_ms = -10**18
    daily_trade_count: defaultdict[str, int] = defaultdict(int)
    daily_stop_days: set[str] = set()
    daily_target_days: set[str] = set()
    max_same_side = 0
    warmup = 260

    for index in range(warmup, len(candles)):
        candle = candles[index]
        if start_ms is not None and candle.open_time_ms < start_ms:
            continue
        if end_ms is not None and candle.open_time_ms >= end_ms:
            break
        day = _day(candle.open_time_ms)
        day_start_equity.setdefault(day, equity)
        day_pnl = realized_by_day[day]
        day_base_equity = day_start_equity[day]

        if position is not None:
            closed = try_exit(position, candle, ema21[index], vwap_1m[index], max_hold_until_ms, runtime_config)
            if closed is not None:
                trades.append(closed)
                realized_by_day[day] += closed.pnl_usdc
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
            if try_rescue_add(position, candle, ema21[index], atr_1m[index], runtime_config):
                event_counts["rescue_add"] += 1
                events_by_day[day]["rescue_add"] += 1
            continue

        if pending_entry is not None:
            if candle.open_time_ms > pending_entry.expiry_ms:
                pending_entry = None
            else:
                filled = try_fill_pending_entry(pending_entry, candle, equity, runtime_config)
                if filled is not None:
                    position = filled
                    max_hold_until_ms = candle.open_time_ms + config.max_hold_minutes * ONE_MINUTE_MS
                    last_trade_ms = candle.open_time_ms
                    daily_trade_count[day] += 1
                    recent_by_side[filled.side].append(candle.open_time_ms)
                    pending_entry = None
                    continue

        if position is not None or pending_entry is not None:
            continue
        if config.allowed_hours_taipei and _hour(candle.open_time_ms) not in set(config.allowed_hours_taipei):
            continue
        if candle.open_time_ms < cooldown_until_ms:
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
        if daily_trade_count[day] >= config.max_trades_per_day:
            continue
        if candle.open_time_ms < last_trade_ms + config.trade_spacing_minutes * ONE_MINUTE_MS:
            continue

        side_candidates = ("LONG", "SHORT")
        bias = hourly_bias(index, one_hour, one_hour_map, ema_fast_1h, ema_slow_1h, runtime_config)
        if bias == "up":
            side_candidates = ("LONG", "SHORT")
        elif bias == "down":
            side_candidates = ("SHORT", "LONG")
        for side in side_candidates:
            recent = [
                opened_ms
                for opened_ms in recent_by_side[side]
                if candle.open_time_ms - opened_ms < config.same_side_window_minutes * ONE_MINUTE_MS
            ]
            recent_by_side[side] = recent
            max_same_side = max(max_same_side, len(recent))
            if len(recent) >= config.same_side_max_trades:
                event_counts["same_side_window_block"] += 1
                events_by_day[day]["same_side_window_block"] += 1
                continue
            built = build_manual_pending(index, side, candles, ema21, atr_1m, volume_sma, vwap_1m, config)
            if built is not None:
                pending_entry, features = built
                if (
                    features["directional_distance_to_ema21_atr"] > config.max_directional_ema_atr
                    or features["directional_move_15m_atr"] > config.max_directional_move_15m_atr
                ):
                    event_counts["overheat_block"] += 1
                    events_by_day[day]["overheat_block"] += 1
                    pending_entry = None
                    continue
                break

    if position is not None:
        last = candles[-1]
        closed = close_position(position, last, last.close, "force_close", config.taker_fee_rate)
        trades.append(closed)
        realized_by_day[_day(last.open_time_ms)] += closed.pnl_usdc

    wins = [trade for trade in trades if trade.pnl_usdc > 0]
    losses = [trade for trade in trades if trade.pnl_usdc < 0]
    gross_profit = sum(trade.pnl_usdc for trade in wins)
    gross_loss = abs(sum(trade.pnl_usdc for trade in losses))
    by_day = {}
    trades_by_day: defaultdict[str, list] = defaultdict(list)
    for trade in trades:
        trades_by_day[_day(trade.opened_at_ms)].append(trade)
    for day, pnl in sorted(realized_by_day.items()):
        day_trades = trades_by_day[day]
        by_day[day] = {
            "pnl_usdc": pnl,
            "pnl_pct": (pnl / day_start_equity.get(day, config.equity_usdc) * 100),
            "trades": len(day_trades),
            "maker_entry_ratio": (
                sum(1 for trade in day_trades if trade.entry_fee_rate <= config.maker_fee_rate) / len(day_trades)
                if day_trades
                else 0.0
            ),
            "taker_entry_trades": sum(1 for trade in day_trades if trade.entry_fee_rate > config.maker_fee_rate),
            "worst_trade_usdc": min((trade.pnl_usdc for trade in day_trades), default=0.0),
            "events": dict(sorted(events_by_day[day].items())),
        }
    return {
        "trades": [asdict(trade) for trade in trades],
        "by_day": by_day,
        "summary": {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(trades) if trades else 0.0,
            "net_pnl_usdc": sum(trade.pnl_usdc for trade in trades),
            "fees_usdc": sum(trade.fees_usdc for trade in trades),
            "maker_entry_ratio": (
                sum(1 for trade in trades if trade.entry_fee_rate <= config.maker_fee_rate) / len(trades)
                if trades
                else 0.0
            ),
            "taker_entry_trades": sum(1 for trade in trades if trade.entry_fee_rate > config.maker_fee_rate),
            "avg_trade_usdc": mean([trade.pnl_usdc for trade in trades]) if trades else 0.0,
            "best_trade_usdc": max((trade.pnl_usdc for trade in trades), default=0.0),
            "worst_trade_usdc": min((trade.pnl_usdc for trade in trades), default=0.0),
            "profit_factor": gross_profit / gross_loss if gross_loss else None,
            "avg_daily_pct": mean(item["pnl_pct"] for item in by_day.values()) if by_day else 0.0,
            "max_same_side_trades_in_window": max_same_side,
            "risk_event_counts": dict(sorted(event_counts.items())),
        },
        "config": asdict(config),
    }


def print_result(result: dict) -> None:
    print("Summary")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
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
    allowed_hours = tuple(int(item.strip()) for item in args.allowed_hours.split(",") if item.strip())
    config = ManualGuardConfig(symbol=args.symbol, daily_target_pct=args.daily_target_pct, allowed_hours_taipei=allowed_hours)
    start_ms, end_ms = date_range_to_ms(args.start_date, args.end_date)
    padded_start = int((datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc) - timedelta(days=3)).timestamp() * 1000)
    candles = fetch_klines(BINANCE_FAPI_BASE, args.symbol, "1m", padded_start, end_ms)
    result = run_backtest(candles, config, start_ms=start_ms, end_ms=end_ms)
    print_result(result)
    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
