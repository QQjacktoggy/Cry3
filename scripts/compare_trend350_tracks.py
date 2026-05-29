"""Compare closed-bar and intrabar trend350 live-entry tracks.

Closed track:
    Use only completed 5m candles. A signal on a completed bar can start an
    entry-limit order from the next 5m bar.

Intrabar track:
    Rebuild the current 5m candle from 1m candles at a chosen minute offset
    and run the same trend350 decision function. This approximates the current
    live bot behavior when it polls during a still-forming 5m candle.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_signal import fetch_klines
from src.gridbot.strategy.long_pullback import Candle, StrategyConfig
from src.gridbot.strategy.signal_journal import generate_router_allocator_v13_trend350_live_decision
from src.gridbot.testnet.fill_policy import entry_limit_price, reward_pct_for_entry

FIVE_MINUTES_MS = 5 * 60_000
ONE_MINUTE_MS = 60_000


@dataclass(frozen=True)
class TrackSignal:
    track: str
    signal_time_ms: int
    candle_open_time_ms: int
    direction: str
    strategy: str
    regime: str
    risk_mode: str
    market_playbook: str
    allocator_profile: str
    allocator_scale: float
    score: int
    confidence: int
    market_price: float
    planned_entry: float
    order_entry: float
    stop: float
    take_profit: float
    reward_pct: float
    gap_bps: float
    stale: bool


@dataclass(frozen=True)
class SimulatedOrder:
    signal: TrackSignal
    status: Literal["filled", "expired", "reward_blocked"]
    fill_time_ms: int | None
    exit_time_ms: int | None
    exit_reason: str


def aggregate_completed_5m(one_minute: list[Candle], close_time_ms: int) -> list[Candle]:
    buckets: dict[int, list[Candle]] = {}
    for candle in one_minute:
        bucket = _bucket_open(candle.open_time_ms)
        if bucket + FIVE_MINUTES_MS <= close_time_ms:
            buckets.setdefault(bucket, []).append(candle)
    return [_aggregate_bucket(bucket, rows) for bucket, rows in sorted(buckets.items()) if len(rows) == 5]


def aggregate_intrabar_5m(one_minute: list[Candle], signal_time_ms: int) -> list[Candle]:
    current_bucket = _bucket_open(signal_time_ms - 1)
    completed = aggregate_completed_5m(one_minute, current_bucket)
    partial = [
        candle
        for candle in one_minute
        if current_bucket <= candle.open_time_ms < signal_time_ms
    ]
    if partial:
        completed.append(_aggregate_bucket(current_bucket, partial))
    return completed


def build_track_signals(
    one_minute: list[Candle],
    *,
    track: Literal["closed", "intrabar"],
    symbol: str,
    eval_start_ms: int,
    eval_end_ms: int,
    base: StrategyConfig,
    min_score: int,
    intrabar_offset_minutes: int = 1,
    tolerance_bps: float = 0.0,
    min_reward_pct: float = 0.12,
) -> list[TrackSignal]:
    signals: list[TrackSignal] = []
    if track == "closed":
        signal_times = range(_ceil_bucket(eval_start_ms), eval_end_ms + 1, FIVE_MINUTES_MS)
    else:
        offset_ms = max(1, min(4, intrabar_offset_minutes)) * ONE_MINUTE_MS
        first_bucket = _bucket_open(eval_start_ms)
        signal_times = range(first_bucket + offset_ms, eval_end_ms + 1, FIVE_MINUTES_MS)

    for signal_time_ms in signal_times:
        if signal_time_ms < eval_start_ms:
            continue
        candles = (
            aggregate_completed_5m(one_minute, signal_time_ms)
            if track == "closed"
            else aggregate_intrabar_5m(one_minute, signal_time_ms)
        )
        if len(candles) < 300:
            continue
        decision = generate_router_allocator_v13_trend350_live_decision(candles, base, day_pnl=0.0)
        if decision is None:
            continue
        signal = decision.signal
        if signal.action not in {"PLAN_LONG", "PLAN_SHORT"} or signal.score < min_score:
            continue
        direction = "short" if signal.action == "PLAN_SHORT" else "long"
        planned_entry = float(signal.entries[0]) if signal.entries else float(signal.price)
        stop = float(signal.stop_loss or 0.0)
        take_profit = float(signal.take_profits[0]) if signal.take_profits else 0.0
        order_entry = entry_limit_price(direction, planned_entry, stop, take_profit, tolerance_bps)
        reward_pct = reward_pct_for_entry(order_entry, take_profit, direction)
        market_price = candles[-1].close
        signals.append(
            TrackSignal(
                track=track,
                signal_time_ms=signal_time_ms,
                candle_open_time_ms=candles[-1].open_time_ms,
                direction=direction,
                strategy=decision.strategy,
                regime=decision.regime,
                risk_mode=decision.risk_mode,
                market_playbook=decision.market_playbook,
                allocator_profile=decision.allocator_profile,
                allocator_scale=decision.allocator_scale,
                score=signal.score,
                confidence=signal.confidence,
                market_price=market_price,
                planned_entry=planned_entry,
                order_entry=order_entry,
                stop=stop,
                take_profit=take_profit,
                reward_pct=round(reward_pct, 5),
                gap_bps=round(_entry_gap_bps(direction, market_price, order_entry), 3),
                stale=_is_stale(direction, market_price, stop, take_profit),
            )
        )
    return signals


def simulate_track(
    signals: list[TrackSignal],
    one_minute: list[Candle],
    *,
    ttl_bars: int = 8,
    max_holding_bars: int = 48,
    min_reward_pct: float = 0.12,
) -> list[SimulatedOrder]:
    orders: list[SimulatedOrder] = []
    busy_until = 0
    for signal in sorted(signals, key=lambda item: item.signal_time_ms):
        if signal.signal_time_ms < busy_until:
            continue
        if signal.reward_pct < min_reward_pct:
            orders.append(SimulatedOrder(signal, "reward_blocked", None, None, "reward_blocked"))
            busy_until = signal.signal_time_ms + ttl_bars * FIVE_MINUTES_MS
            continue
        entry_start = signal.signal_time_ms
        expires_at = entry_start + ttl_bars * FIVE_MINUTES_MS
        fill = _find_fill(signal, one_minute, entry_start, expires_at)
        if fill is None:
            orders.append(SimulatedOrder(signal, "expired", None, None, "entry_expired"))
            busy_until = expires_at
            continue
        exit_time, exit_reason = _find_exit(signal, one_minute, fill.open_time_ms, max_holding_bars)
        orders.append(SimulatedOrder(signal, "filled", fill.open_time_ms, exit_time, exit_reason))
        busy_until = exit_time
    return orders


def summarize_orders(track: str, signals: list[TrackSignal], orders: list[SimulatedOrder]) -> dict:
    filled = [order for order in orders if order.status == "filled"]
    expired = [order for order in orders if order.status == "expired"]
    reward_blocked = [order for order in orders if order.status == "reward_blocked"]
    stale = [signal for signal in signals if signal.stale]
    return {
        "track": track,
        "raw_signals": len(signals),
        "orders_after_busy_filter": len(orders),
        "filled": len(filled),
        "expired": len(expired),
        "reward_blocked": len(reward_blocked),
        "fill_rate_pct": round(len(filled) / len(orders) * 100, 2) if orders else 0.0,
        "stale_raw_signals": len(stale),
        "avg_gap_bps": round(sum(signal.gap_bps for signal in signals) / len(signals), 3) if signals else 0.0,
        "median_gap_bps": _median([signal.gap_bps for signal in signals]),
        "strategies": dict(Counter(signal.strategy for signal in signals)),
        "regimes": dict(Counter(signal.regime for signal in signals)),
        "exits": dict(Counter(order.exit_reason for order in filled)),
        "last_orders": [_order_payload(order) for order in orders[-10:]],
    }


def live_base(
    symbol: str,
    equity_usdc: float = 150.0,
    daily_target_pct: float = 2.7,
    max_leverage: float = 70.0,
    maker_fee_rate: float = 0.0002,
    taker_fee_rate: float = 0.0004,
) -> StrategyConfig:
    return StrategyConfig(
        symbol=symbol,
        equity_usdc=equity_usdc,
        compounding_enabled=True,
        daily_target_min_pct=daily_target_pct,
        daily_target_max_pct=daily_target_pct,
        risk_per_trade_pct=100.0,
        min_score=60,
        max_effective_leverage=max_leverage,
        maker_fee_rate=maker_fee_rate,
        taker_fee_rate=taker_fee_rate,
        daily_soft_loss_pct=16.0,
        daily_max_loss_pct=36.0,
        daily_loss_risk_scale=0.55,
        daily_target_stop_pct=10.0,
        max_open_positions=1,
        max_position_margin_pct=100.0,
        cooldown_bars=4,
        max_consecutive_losses_before_cooldown=3,
        consecutive_loss_cooldown_bars=18,
        max_holding_bars=48,
        take_profit_r=(0.55, 1.1, 2.2),
        exit_weights=(0.25, 0.35, 0.40),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare closed vs intrabar trend350 signal tracks.")
    parser.add_argument("--symbol", default="ETHUSDC")
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--warmup-hours", type=float, default=30)
    parser.add_argument("--base-url", default="https://testnet.binancefuture.com")
    parser.add_argument("--min-score", type=int, default=58)
    parser.add_argument("--intrabar-offset-minutes", type=int, default=1)
    parser.add_argument("--tolerance-bps", type=float, default=0.0)
    parser.add_argument("--min-reward-pct", type=float, default=0.12)
    parser.add_argument("--ttl-bars", type=int, default=8)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    end = datetime.now(timezone.utc)
    eval_start = end - timedelta(hours=args.hours)
    fetch_start = eval_start - timedelta(hours=args.warmup_hours)
    one_minute = fetch_klines(
        args.base_url,
        args.symbol,
        "1m",
        int(fetch_start.timestamp() * 1000),
        int(end.timestamp() * 1000),
    )
    base = live_base(args.symbol)
    eval_start_ms = int(eval_start.timestamp() * 1000)
    eval_end_ms = int(end.timestamp() * 1000)

    closed_signals = build_track_signals(
        one_minute,
        track="closed",
        symbol=args.symbol,
        eval_start_ms=eval_start_ms,
        eval_end_ms=eval_end_ms,
        base=base,
        min_score=args.min_score,
        tolerance_bps=args.tolerance_bps,
        min_reward_pct=args.min_reward_pct,
    )
    intrabar_signals = build_track_signals(
        one_minute,
        track="intrabar",
        symbol=args.symbol,
        eval_start_ms=eval_start_ms,
        eval_end_ms=eval_end_ms,
        base=base,
        min_score=args.min_score,
        intrabar_offset_minutes=args.intrabar_offset_minutes,
        tolerance_bps=args.tolerance_bps,
        min_reward_pct=args.min_reward_pct,
    )
    closed_orders = simulate_track(closed_signals, one_minute, ttl_bars=args.ttl_bars, min_reward_pct=args.min_reward_pct)
    intrabar_orders = simulate_track(intrabar_signals, one_minute, ttl_bars=args.ttl_bars, min_reward_pct=args.min_reward_pct)
    payload = {
        "symbol": args.symbol,
        "window_utc": {
            "start": eval_start.isoformat(),
            "end": end.isoformat(),
        },
        "intrabar_offset_minutes": args.intrabar_offset_minutes,
        "tolerance_bps": args.tolerance_bps,
        "closed": summarize_orders("closed", closed_signals, closed_orders),
        "intrabar": summarize_orders("intrabar", intrabar_signals, intrabar_orders),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _print_summary(payload)
    return 0


def _bucket_open(open_time_ms: int) -> int:
    return open_time_ms - (open_time_ms % FIVE_MINUTES_MS)


def _ceil_bucket(time_ms: int) -> int:
    bucket = _bucket_open(time_ms)
    return bucket if bucket == time_ms else bucket + FIVE_MINUTES_MS


def _aggregate_bucket(open_time_ms: int, rows: list[Candle]) -> Candle:
    ordered = sorted(rows, key=lambda item: item.open_time_ms)
    return Candle(
        open_time_ms=open_time_ms,
        open=ordered[0].open,
        high=max(candle.high for candle in ordered),
        low=min(candle.low for candle in ordered),
        close=ordered[-1].close,
        volume=sum(candle.volume for candle in ordered),
        quote_volume=sum(candle.quote_volume for candle in ordered),
    )


def _entry_gap_bps(direction: str, market_price: float, entry: float) -> float:
    if entry <= 0:
        return 0.0
    if direction == "short":
        return (entry - market_price) / entry * 10_000
    return (market_price - entry) / entry * 10_000


def _is_stale(direction: str, market_price: float, stop: float, take_profit: float) -> bool:
    if direction == "short":
        return (stop > 0 and market_price >= stop) or (take_profit > 0 and market_price <= take_profit)
    return (stop > 0 and market_price <= stop) or (take_profit > 0 and market_price >= take_profit)


def _find_fill(signal: TrackSignal, candles: list[Candle], start_ms: int, end_ms: int) -> Candle | None:
    for candle in candles:
        if candle.open_time_ms < start_ms or candle.open_time_ms > end_ms:
            continue
        if signal.direction == "short" and candle.high >= signal.order_entry:
            return candle
        if signal.direction == "long" and candle.low <= signal.order_entry:
            return candle
    return None


def _find_exit(signal: TrackSignal, candles: list[Candle], fill_ms: int, max_holding_bars: int) -> tuple[int, str]:
    max_exit_ms = fill_ms + max_holding_bars * FIVE_MINUTES_MS
    last_seen_ms = fill_ms
    for candle in candles:
        if candle.open_time_ms < fill_ms:
            continue
        if candle.open_time_ms > max_exit_ms:
            break
        last_seen_ms = candle.open_time_ms
        if signal.direction == "short":
            if signal.stop > 0 and candle.high >= signal.stop:
                return candle.open_time_ms, "stop_loss"
            if signal.take_profit > 0 and candle.low <= signal.take_profit:
                return candle.open_time_ms, "take_profit"
        else:
            if signal.stop > 0 and candle.low <= signal.stop:
                return candle.open_time_ms, "stop_loss"
            if signal.take_profit > 0 and candle.high >= signal.take_profit:
                return candle.open_time_ms, "take_profit"
    return last_seen_ms, "max_hold"


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 3)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 3)


def _order_payload(order: SimulatedOrder) -> dict:
    signal = order.signal
    return {
        "signal_time_utc": datetime.fromtimestamp(signal.signal_time_ms / 1000, tz=timezone.utc).isoformat(),
        "status": order.status,
        "direction": signal.direction,
        "strategy": signal.strategy,
        "score": signal.score,
        "regime": signal.regime,
        "market_price": round(signal.market_price, 4),
        "entry": round(signal.order_entry, 4),
        "tp": round(signal.take_profit, 4),
        "gap_bps": signal.gap_bps,
        "stale": signal.stale,
        "exit": order.exit_reason,
    }


def _print_summary(payload: dict) -> None:
    print(f"symbol={payload['symbol']} window={payload['window_utc']['start']}..{payload['window_utc']['end']}")
    print(f"intrabar_offset_minutes={payload['intrabar_offset_minutes']} tolerance_bps={payload['tolerance_bps']}")
    for key in ("closed", "intrabar"):
        row = payload[key]
        print(
            f"{key}: raw_signals={row['raw_signals']} orders={row['orders_after_busy_filter']} "
            f"filled={row['filled']} expired={row['expired']} reward_blocked={row['reward_blocked']} "
            f"fill_rate={row['fill_rate_pct']}% stale={row['stale_raw_signals']} "
            f"avg_gap_bps={row['avg_gap_bps']} median_gap_bps={row['median_gap_bps']}"
        )
        print(f"  strategies={row['strategies']}")
        print(f"  regimes={row['regimes']}")
        print(f"  exits={row['exits']}")
        for order in row["last_orders"][-5:]:
            print(f"  last {order}")


if __name__ == "__main__":
    raise SystemExit(main())
