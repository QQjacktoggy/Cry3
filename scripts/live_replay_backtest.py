"""Live-first replay backtest using 1m candles and execution planning."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_signal import fetch_klines
from src.gridbot.replay.live_replay import (
    ExecutionPlan,
    ReplayConfig,
    plan_execution,
    plan_micro_execution,
    reward_pct,
    should_preempt_pending,
)
from src.gridbot.strategy.long_breakout import BreakoutConfig, build_breakout_context, generate_breakout_signal_at
from src.gridbot.strategy.long_pullback import Candle, StrategyConfig, build_indicator_context, generate_signal_at
from src.gridbot.strategy.market_state import build_market_state_context, classify_market_state

ONE_MINUTE_MS = 60_000
FIVE_MINUTES_MS = 5 * ONE_MINUTE_MS
MARKETABLE_MODES = {
    "marketable_momentum",
    "marketable_reclaim",
    "marketable_retest",
    "marketable_pullback",
    "marketable_vwap",
}


@dataclass(frozen=True)
class ReplayTrade:
    mode: str
    strategy: str
    opened_at_ms: int
    closed_at_ms: int
    entry_price: float
    exit_price: float
    qty: float
    pnl_usdc: float
    fees_usdc: float
    reason: str


@dataclass
class PendingOrder:
    plan: ExecutionPlan
    created_at_ms: int

    @property
    def expires_at_ms(self) -> int:
        return self.created_at_ms + self.plan.ttl_minutes * ONE_MINUTE_MS


@dataclass
class ActivePosition:
    plan: ExecutionPlan
    opened_at_ms: int
    entry_price: float
    qty: float
    entry_fee_rate: float

    @property
    def max_hold_until_ms(self) -> int:
        return self.opened_at_ms + self.plan.max_hold_minutes * ONE_MINUTE_MS


def live_base(
    symbol: str,
    equity_usdc: float = 150.0,
    maker_fee_rate: float = 0.0002,
    taker_fee_rate: float = 0.0004,
) -> StrategyConfig:
    return StrategyConfig(
        symbol=symbol,
        equity_usdc=equity_usdc,
        compounding_enabled=True,
        daily_target_min_pct=3.0,
        daily_target_max_pct=3.0,
        risk_per_trade_pct=1.0,
        min_score=55,
        max_effective_leverage=20,
        maker_fee_rate=maker_fee_rate,
        taker_fee_rate=taker_fee_rate,
        daily_soft_loss_pct=16.0,
        daily_max_loss_pct=36.0,
        daily_loss_risk_scale=0.55,
        daily_target_stop_pct=10.0,
        max_open_positions=1,
        max_position_margin_pct=35.0,
        cooldown_bars=4,
        max_consecutive_losses_before_cooldown=3,
        consecutive_loss_cooldown_bars=18,
        max_holding_bars=48,
        take_profit_r=(0.55, 1.1, 2.2),
        exit_weights=(0.25, 0.35, 0.40),
    )


def run_live_replay(
    one_minute: list[Candle],
    *,
    start_time_ms: int | None = None,
    base: StrategyConfig,
    config: ReplayConfig,
) -> dict:
    pullback_config = base
    breakout_config = BreakoutConfig(base=base)
    warmup_bars = max(
        config.warmup_5m_bars,
        pullback_config.support_lookback,
        pullback_config.vwap_period,
        pullback_config.ema_slow_period,
        breakout_config.breakout_lookback,
        breakout_config.volume_lookback,
    ) + 2
    pending: PendingOrder | None = None
    position: ActivePosition | None = None
    trades: list[ReplayTrade] = []
    decisions = 0
    mode_counts: Counter[str] = Counter()
    decision_reasons: Counter[str] = Counter()
    micro_cooldown_until_ms = 0
    realized_daily_pnl: defaultdict[str, float] = defaultdict(float)

    for index in range(len(one_minute)):
        current = one_minute[index]
        closed_this_candle = False

        if position is not None:
            closed = _try_close_position(position, current, base)
            if closed is not None:
                trades.append(closed)
                realized_daily_pnl[_day_key(closed.closed_at_ms)] += closed.pnl_usdc
                position = None
                closed_this_candle = True
                micro_cooldown_until_ms = max(
                    micro_cooldown_until_ms,
                    _micro_cooldown_until(closed, current.open_time_ms, config),
                )

        if pending is not None and position is None:
            filled = _try_fill_pending(pending, current, config, base)
            if filled is not None:
                pending = None
                closed = _try_close_position(filled, current, base)
                if closed is not None:
                    trades.append(closed)
                    realized_daily_pnl[_day_key(closed.closed_at_ms)] += closed.pnl_usdc
                    closed_this_candle = True
                    micro_cooldown_until_ms = max(
                        micro_cooldown_until_ms,
                        _micro_cooldown_until(closed, current.open_time_ms, config),
                    )
                else:
                    position = filled
            elif current.open_time_ms >= pending.expires_at_ms:
                pending = None

        if start_time_ms is not None and current.open_time_ms < start_time_ms:
            continue
        if closed_this_candle:
            continue
        if position is not None:
            continue
        if _daily_guard_reason(base, realized_daily_pnl[_day_key(current.open_time_ms)]):
            pending = None
            continue

        micro_history_bars = max(
            config.micro_warmup_1m_bars,
            config.micro_lookback_bars + 1,
            config.micro_volume_lookback_bars + 1,
            config.micro_trend_slow_bars,
            config.micro_trend_fast_bars + config.micro_trend_slope_lookback_bars,
            config.micro_structure_5m_slow_bars * 5,
            (config.micro_structure_5m_fast_bars + config.micro_structure_5m_slope_lookback_bars) * 5,
            config.micro_vwap_lookback_bars,
            80,
        )
        micro_start = max(0, index + 1 - micro_history_bars)
        micro_plan = None
        if current.open_time_ms >= micro_cooldown_until_ms:
            micro_plan = plan_micro_execution(
                one_minute=one_minute[micro_start:index + 1],
                config=config,
                equity_usdc=base.equity_usdc,
            )
        if micro_plan is not None:
            if pending is not None:
                if not should_preempt_pending(pending.plan, micro_plan, config):
                    continue
                pending = None
            decisions += 1
            mode_counts[micro_plan.mode] += 1
            decision_reasons[micro_plan.reason] += 1
            if micro_plan.mode in MARKETABLE_MODES:
                position = _open_position(micro_plan, current.open_time_ms, current.close, base)
            else:
                pending = PendingOrder(plan=micro_plan, created_at_ms=current.open_time_ms)
            continue

        if not config.legacy_5m_enabled:
            continue

        snapshot = _aggregate_intrabar_5m(one_minute[: index + 1], current.open_time_ms + ONE_MINUTE_MS)
        if len(snapshot) < warmup_bars:
            continue

        market_context = build_market_state_context(snapshot, base)
        market_decision = classify_market_state(snapshot, len(snapshot) - 1, market_context, base)
        if market_decision is None:
            continue
        pullback_context = build_indicator_context(snapshot, pullback_config)
        breakout_context = build_breakout_context(snapshot, breakout_config)
        pullback_signal = generate_signal_at(snapshot, len(snapshot) - 1, pullback_config, pullback_context)
        breakout_signal = generate_breakout_signal_at(snapshot, len(snapshot) - 1, breakout_config, breakout_context)
        plan = plan_execution(
            current_candle=current,
            market_decision=market_decision,
            breakout_signal=breakout_signal,
            pullback_signal=pullback_signal,
            config=config,
        )
        if plan is None:
            continue
        if pending is not None:
            if not should_preempt_pending(pending.plan, plan, config):
                continue
            pending = None
        decisions += 1
        mode_counts[plan.mode] += 1
        decision_reasons[plan.reason] += 1
        if plan.mode in MARKETABLE_MODES:
            position = _open_position(plan, current.open_time_ms, current.close, base)
        else:
            pending = PendingOrder(plan=plan, created_at_ms=current.open_time_ms)

    if position is not None:
        last = one_minute[-1]
        trades.append(_force_close_position(position, last, base))

    daily_pnl = _seed_daily_pnl(one_minute, start_time_ms)
    for trade in trades:
        day = datetime.fromtimestamp(trade.closed_at_ms / 1000, tz=timezone.utc).date().isoformat()
        daily_pnl[day] += trade.pnl_usdc
    return {
        "decisions": decisions,
        "mode_counts": dict(mode_counts),
        "decision_reasons": dict(decision_reasons),
        "trades": [asdict(trade) for trade in trades],
        "summary": _summarize_trades(trades, base, daily_pnl),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay a live-first execution stack on 1m candles.")
    parser.add_argument("--symbol", default="ETHUSDC")
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--warmup-hours", type=float, default=30)
    parser.add_argument("--base-url", default="https://testnet.binancefuture.com")
    parser.add_argument("--maker-fee-rate", type=float, default=0.0002)
    parser.add_argument("--taker-fee-rate", type=float, default=0.0004)
    parser.add_argument(
        "--mainnet-usdc-fees",
        action="store_true",
        help="Use mainnet USDC-pair economics: maker 0, taker 0.0004.",
    )
    parser.add_argument("--micro-only", action="store_true")
    parser.add_argument("--micro-maker-first", action="store_true")
    parser.add_argument("--micro-fixed-ticket", action="store_true")
    parser.add_argument("--micro-target-net-profit-usdc", type=float, default=0.75)
    parser.add_argument("--micro-max-loss-usdc", type=float, default=1.25)
    parser.add_argument("--micro-maker-entry-atr", type=float, default=0.10)
    parser.add_argument("--micro-maker-ttl-minutes", type=int, default=3)
    parser.add_argument("--micro-maker-min-score", type=int, default=58)
    parser.add_argument(
        "--micro-maker-first-strategies",
        default="micro_breakout_retest,micro_ema_vwap_pullback,micro_reclaim,micro_vwap_reclaim",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    maker_fee_rate = 0.0 if args.mainnet_usdc_fees else args.maker_fee_rate
    taker_fee_rate = args.taker_fee_rate

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=args.hours)
    fetch_start = start - timedelta(hours=args.warmup_hours)
    one_minute = fetch_klines(
        args.base_url,
        args.symbol,
        "1m",
        int(fetch_start.timestamp() * 1000),
        int(end.timestamp() * 1000),
    )
    config = ReplayConfig(
        legacy_5m_enabled=not args.micro_only,
        micro_entry_taker_fee_rate=taker_fee_rate,
        micro_maker_entry_fee_rate=maker_fee_rate,
        micro_take_profit_fee_rate=maker_fee_rate,
        micro_stop_taker_fee_rate=taker_fee_rate,
        micro_maker_first_enabled=args.micro_maker_first,
        micro_fixed_ticket_enabled=args.micro_fixed_ticket,
        micro_target_net_profit_usdc=args.micro_target_net_profit_usdc,
        micro_max_loss_usdc=args.micro_max_loss_usdc,
        micro_maker_entry_atr=args.micro_maker_entry_atr,
        micro_maker_ttl_minutes=args.micro_maker_ttl_minutes,
        micro_maker_first_min_score=args.micro_maker_min_score,
        micro_maker_first_strategies=tuple(
            strategy.strip()
            for strategy in args.micro_maker_first_strategies.split(",")
            if strategy.strip()
        ),
    )
    payload = {
        "symbol": args.symbol,
        "window_utc": {"start": start.isoformat(), "end": end.isoformat()},
        "result": run_live_replay(
            one_minute,
            start_time_ms=int(start.timestamp() * 1000),
            base=live_base(args.symbol, maker_fee_rate=maker_fee_rate, taker_fee_rate=taker_fee_rate),
            config=config,
        ),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _print_payload(payload)
    return 0


def _aggregate_intrabar_5m(one_minute: list[Candle], signal_time_ms: int) -> list[Candle]:
    current_bucket = _bucket_open(signal_time_ms - 1)
    buckets: dict[int, list[Candle]] = {}
    for candle in one_minute:
        bucket = _bucket_open(candle.open_time_ms)
        if bucket < current_bucket:
            buckets.setdefault(bucket, []).append(candle)
        elif bucket == current_bucket and candle.open_time_ms < signal_time_ms:
            buckets.setdefault(bucket, []).append(candle)
    aggregated: list[Candle] = []
    for bucket, rows in sorted(buckets.items()):
        if bucket < current_bucket and len(rows) < 5:
            continue
        aggregated.append(_aggregate_bucket(bucket, rows))
    return aggregated


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


def _bucket_open(open_time_ms: int) -> int:
    return open_time_ms - (open_time_ms % FIVE_MINUTES_MS)


def _open_position(plan: ExecutionPlan, opened_at_ms: int, entry_price: float, base: StrategyConfig) -> ActivePosition:
    notional = max(plan.planned_notional_usdc, 1.0)
    qty = notional / max(entry_price, 0.0001)
    entry_fee_rate = base.taker_fee_rate if plan.mode in MARKETABLE_MODES else 0.0
    return ActivePosition(
        plan=plan,
        opened_at_ms=opened_at_ms,
        entry_price=entry_price,
        qty=qty,
        entry_fee_rate=entry_fee_rate,
    )


def _try_fill_pending(
    pending: PendingOrder,
    candle: Candle,
    config: ReplayConfig,
    base: StrategyConfig,
) -> ActivePosition | None:
    plan = pending.plan
    if plan.side != "long":
        return None
    fills: list[tuple[float, float]] = []
    total_weight = sum(plan.entry_weights) or 1.0
    for entry, weight in zip(plan.entry_levels, plan.entry_weights):
        if candle.low <= entry:
            fills.append((entry, weight))
    if not fills:
        return None
    filled_weight = sum(weight for _, weight in fills)
    if filled_weight <= 0:
        return None
    avg_entry = sum(price * weight for price, weight in fills) / filled_weight
    if plan.stop_loss >= avg_entry:
        return None
    if reward_pct(avg_entry, plan.take_profit, plan.side) < config.min_reward_pct:
        return None
    fill_ratio = min(filled_weight / total_weight, 1.0)
    notional = max(plan.planned_notional_usdc * fill_ratio, 1.0)
    qty = notional / max(avg_entry, 0.0001)
    adjusted_plan = replace(
        plan,
        entry_levels=(round(avg_entry, 4),),
        entry_weights=(1.0,),
    )
    return ActivePosition(
        plan=adjusted_plan,
        opened_at_ms=candle.open_time_ms,
        entry_price=avg_entry,
        qty=qty,
        entry_fee_rate=base.maker_fee_rate,
    )


def _try_close_position(position: ActivePosition, candle: Candle, base: StrategyConfig) -> ReplayTrade | None:
    plan = position.plan
    if plan.side != "long":
        return None
    if candle.low <= plan.stop_loss:
        return _close_trade(position, candle.open_time_ms, plan.stop_loss, "stop_loss", taker=True, base=base)
    if candle.high >= plan.take_profit:
        return _close_trade(position, candle.open_time_ms, plan.take_profit, "take_profit", taker=False, base=base)
    if candle.open_time_ms >= position.max_hold_until_ms:
        return _close_trade(position, candle.open_time_ms, candle.close, "max_hold", taker=True, base=base)
    return None


def _force_close_position(position: ActivePosition, candle: Candle, base: StrategyConfig) -> ReplayTrade:
    return _close_trade(position, candle.open_time_ms, candle.close, "forced_window_end", taker=True, base=base)


def _close_trade(
    position: ActivePosition,
    closed_at_ms: int,
    exit_price: float,
    reason: str,
    *,
    taker: bool,
    base: StrategyConfig,
) -> ReplayTrade:
    qty = position.qty
    gross = (exit_price - position.entry_price) * qty
    entry_fees = position.entry_price * qty * position.entry_fee_rate
    exit_fees = exit_price * qty * (base.taker_fee_rate if taker else base.maker_fee_rate)
    fees = entry_fees + exit_fees
    return ReplayTrade(
        mode=position.plan.mode,
        strategy=position.plan.strategy,
        opened_at_ms=position.opened_at_ms,
        closed_at_ms=closed_at_ms,
        entry_price=round(position.entry_price, 4),
        exit_price=round(exit_price, 4),
        qty=qty,
        pnl_usdc=gross - fees,
        fees_usdc=fees,
        reason=reason,
    )


def _seed_daily_pnl(candles: list[Candle], start_time_ms: int | None) -> defaultdict[str, float]:
    daily_pnl: defaultdict[str, float] = defaultdict(float)
    for candle in candles:
        if start_time_ms is not None and candle.open_time_ms < start_time_ms:
            continue
        day = datetime.fromtimestamp(candle.open_time_ms / 1000, tz=timezone.utc).date().isoformat()
        daily_pnl[day] += 0.0
    return daily_pnl


def _day_key(open_time_ms: int) -> str:
    return datetime.fromtimestamp(open_time_ms / 1000, tz=timezone.utc).date().isoformat()


def _daily_guard_reason(config: StrategyConfig, realized_day_pnl: float) -> str | None:
    if config.daily_max_loss_usdc > 0 and realized_day_pnl <= -config.daily_max_loss_usdc:
        return "daily max loss reached"
    if config.stop_trading_after_daily_target and realized_day_pnl >= config.daily_target_stop_usdc:
        return "daily target reached"
    return None


def _micro_cooldown_until(closed: ReplayTrade, current_open_time_ms: int, config: ReplayConfig) -> int:
    if not closed.strategy.startswith("micro_"):
        return 0
    if closed.reason == "stop_loss":
        minutes = config.micro_stop_cooldown_minutes
    elif closed.reason == "take_profit":
        minutes = config.micro_take_profit_cooldown_minutes
    elif closed.reason == "max_hold":
        minutes = config.micro_timeout_cooldown_minutes
    else:
        minutes = config.micro_trade_cooldown_minutes
    return current_open_time_ms + minutes * ONE_MINUTE_MS


def _summarize_trades(trades: list[ReplayTrade], config: StrategyConfig, daily_pnl: dict[str, float]) -> dict:
    wins = [trade for trade in trades if trade.pnl_usdc > 0]
    losses = [trade for trade in trades if trade.pnl_usdc < 0]
    gross_profit = sum(trade.pnl_usdc for trade in wins)
    gross_loss = abs(sum(trade.pnl_usdc for trade in losses))
    mode_pnl = defaultdict(float)
    for trade in trades:
        mode_pnl[trade.mode] += trade.pnl_usdc
    equity_usdc = config.equity_usdc
    daily_pct = {day: round(pnl / equity_usdc * 100, 4) for day, pnl in daily_pnl.items()}
    total_days = len(daily_pct)
    target_hit_days = sum(1 for pnl_pct in daily_pct.values() if pnl_pct >= config.daily_target_min_pct)
    max_loss_hit_days = sum(1 for pnl_pct in daily_pct.values() if pnl_pct <= -abs(config.daily_max_loss_pct))
    return {
        "total_trades": len(trades),
        "net_pnl_usdc": round(sum(trade.pnl_usdc for trade in trades), 4),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else None,
        "avg_daily_pct": round(sum(daily_pct.values()) / len(daily_pct), 4) if daily_pct else 0.0,
        "best_day_pct": max(daily_pct.values()) if daily_pct else 0.0,
        "worst_day_pct": min(daily_pct.values()) if daily_pct else 0.0,
        "target_hit_days": target_hit_days,
        "target_hit_rate_pct": round(target_hit_days / total_days * 100, 2) if total_days else 0.0,
        "max_loss_hit_days": max_loss_hit_days,
        "max_loss_hit_rate_pct": round(max_loss_hit_days / total_days * 100, 2) if total_days else 0.0,
        "mode_pnl_usdc": {mode: round(value, 4) for mode, value in mode_pnl.items()},
        "daily_pct": daily_pct,
    }


def _print_payload(payload: dict) -> None:
    result = payload["result"]
    summary = result["summary"]
    print(f"symbol={payload['symbol']} window={payload['window_utc']['start']}..{payload['window_utc']['end']}")
    print(f"decisions={result['decisions']} mode_counts={result['mode_counts']}")
    print(
        f"trades={summary['total_trades']} net_pnl={summary['net_pnl_usdc']} "
        f"win_rate={summary['win_rate_pct']}% avg_daily_pct={summary['avg_daily_pct']} "
        f"best_day_pct={summary['best_day_pct']} worst_day_pct={summary['worst_day_pct']}"
    )
    print(f"mode_pnl={summary['mode_pnl_usdc']}")
    for trade in result["trades"][-10:]:
        print(f"last_trade={trade}")


if __name__ == "__main__":
    raise SystemExit(main())
