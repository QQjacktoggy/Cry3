"""Inspect live v13_trend350 decision stages against recent ETHUSDC candles."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config.settings import Settings
from src.gridbot.binance.client import BinanceFuturesClient
from src.gridbot.strategy.long_orb import OrbConfig, build_orb_context
from src.gridbot.strategy.long_pullback import Candle, StrategyConfig, _daily_guard_reason, _risk_adjusted_config
from src.gridbot.strategy.market_state import build_market_state_context, classify_market_state
from src.gridbot.strategy.regime import build_regime_context, classify_regime
from src.gridbot.strategy.signal_journal import (
    _equity_base,
    _expected_action,
    _local_ai_risk_review,
    _local_nim_policy_review,
    _nim_review_rejected_by_market_state,
    _nim_scaled_base,
    _regime_allocator_adjusted_base,
    _regime_router_adjusted_base,
    _replace_base,
    _scaled_base,
    _select_journal_signal,
    run_orb_signal_journal,
    summarize_signal_journal,
)


def _live_base(settings: Settings, symbol: str) -> StrategyConfig:
    return StrategyConfig(
        symbol=symbol,
        equity_usdc=settings.testnet_equity_usdc,
        compounding_enabled=True,
        daily_target_min_pct=settings.testnet_daily_target_pct,
        daily_target_max_pct=settings.testnet_daily_target_pct,
        risk_per_trade_pct=100.0,
        min_score=60,
        max_effective_leverage=settings.max_effective_leverage,
        maker_fee_rate=settings.testnet_maker_fee_rate,
        taker_fee_rate=settings.testnet_taker_fee_rate,
        daily_soft_loss_pct=settings.daily_soft_loss_pct,
        daily_max_loss_pct=settings.max_daily_loss_pct,
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


def _trace_one(
    candles: list[Candle],
    index: int,
    base: StrategyConfig,
    context,
    regime_context,
    market_context,
) -> dict:
    config = OrbConfig(base=base, session_start_bar=0, opening_range_bars=9, min_volume_ratio=0.8, stop_atr=0.6)

    equity_base = _equity_base(base, base.equity_usdc)
    if _daily_guard_reason(equity_base, 0.0):
        return {"stage": "daily_guard"}

    runtime_base = _risk_adjusted_config(equity_base, 0.0)
    decision = classify_regime(candles, index, regime_context, runtime_base)
    runtime_config = config if runtime_base is base else _replace_base(config, runtime_base)
    market_decision = classify_market_state(candles, index, market_context, runtime_base)

    signal, strategy = _select_journal_signal(candles, index, runtime_config, context, decision, "router")
    row = _row(strategy, signal, decision, market_decision, candles[index].close)
    if signal.action != _expected_action(strategy):
        row["stage"] = "signal_action_mismatch"
        return row

    routed_base = _regime_router_adjusted_base(runtime_base, strategy, decision, market_decision, 0.70, 0.35)
    if routed_base is None:
        row["stage"] = "regime_router_block"
        return row
    if routed_base is not runtime_base:
        runtime_base = routed_base
        runtime_config = _replace_base(runtime_config, runtime_base)
        signal, strategy = _select_journal_signal(candles, index, runtime_config, context, decision, "router")
        row.update(_row(strategy, signal, decision, market_decision, candles[index].close))
        if signal.action != _expected_action(strategy):
            row["stage"] = "post_router_signal_mismatch"
            return row

    nim_review = _local_nim_policy_review("auto", strategy, signal, market_decision)
    if nim_review is not None:
        row["nim"] = f"{nim_review.playbook}/{nim_review.risk_mode}/{nim_review.confidence:.2f}"
        if _nim_review_rejected_by_market_state(strategy, decision, market_decision, nim_review):
            row["stage"] = "nim_market_reject"
            return row
        scaled_base = _nim_scaled_base(runtime_base, nim_review)
        if scaled_base is None:
            row["stage"] = "nim_scale_zero"
            return row
        if scaled_base is not runtime_base:
            runtime_base = scaled_base
            runtime_config = _replace_base(runtime_config, runtime_base)
            signal, strategy = _select_journal_signal(candles, index, runtime_config, context, decision, "router")
            row.update(_row(strategy, signal, decision, market_decision, candles[index].close))
            if signal.action != _expected_action(strategy):
                row["stage"] = "post_nim_signal_mismatch"
                return row

    allocated_base, allocation = _regime_allocator_adjusted_base(
        runtime_base,
        strategy,
        decision,
        market_decision,
        nim_review,
        0.0,
        2.0,
        1.5,
        0.45,
        1.00,
        3.50,
        1.00,
        0.35,
        0.35,
        0.55,
        0.25,
        0.05,
        0.30,
        None,
        1.25,
        0.45,
        0.05,
        0.30,
        0.20,
        0.05,
        0.0,
        100.0,
        signal_score=signal.score,
    )
    row["allocator"] = f"{allocation.get('state')}/{allocation.get('profile')}/{allocation.get('scale')}"
    if allocated_base is None:
        row["stage"] = "allocator_zero"
        return row
    if allocated_base is not runtime_base:
        runtime_base = allocated_base
        runtime_config = _replace_base(runtime_config, runtime_base)
        signal, strategy = _select_journal_signal(candles, index, runtime_config, context, decision, "router")
        row.update(_row(strategy, signal, decision, market_decision, candles[index].close))
        if signal.action != _expected_action(strategy):
            row["stage"] = "post_allocator_signal_mismatch"
            return row

    ai_review = _local_ai_risk_review(
        strategy,
        decision,
        market_decision,
        signal,
        allocation.get("state"),
        allocation.get("profile"),
        runtime_base,
    )
    if ai_review is not None:
        row["ai"] = f"{ai_review.decision}/{ai_review.risk_level}/{ai_review.risk_scale:.2f}/{ai_review.confidence:.2f}"
        if ai_review.decision == "reject" or ai_review.risk_scale <= 0:
            row["stage"] = "ai_reject"
            return row
        if ai_review.decision == "reduce" and ai_review.risk_scale < 1.0:
            runtime_base = _scaled_base(runtime_base, ai_review.risk_scale)
            runtime_config = _replace_base(runtime_config, runtime_base)
            signal, strategy = _select_journal_signal(candles, index, runtime_config, context, decision, "router")
            row.update(_row(strategy, signal, decision, market_decision, candles[index].close))
            if signal.action != _expected_action(strategy):
                row["stage"] = "post_ai_signal_mismatch"
                return row

    row["stage"] = "accepted"
    return row


def _row(strategy, signal, regime_decision, market_decision, price: float) -> dict:
    return {
        "stage": "accepted",
        "strategy": strategy,
        "action": signal.action,
        "expected": _expected_action(strategy),
        "score": signal.score,
        "regime": getattr(regime_decision, "regime", "none"),
        "risk_mode": getattr(regime_decision, "risk_mode", "none"),
        "playbook": getattr(market_decision, "playbook", "none"),
        "market_risk": getattr(market_decision, "risk_mode", "none"),
        "reason": "; ".join(signal.reasons[:3]),
        "price": price,
        "atr": signal.atr or 0.0,
        "entry": signal.entries[0] if signal.entries else signal.price,
        "stop": signal.stop_loss or 0.0,
        "tp": signal.take_profits[0] if signal.take_profits else 0.0,
    }


def _forward_move(candles: list[Candle], index: int, bars: int) -> tuple[float, float, float]:
    price = candles[index].close
    future = candles[index + 1 : min(len(candles), index + 1 + bars)]
    if not future or price <= 0:
        return 0.0, 0.0, 0.0
    high = max(candle.high for candle in future)
    low = min(candle.low for candle in future)
    close = future[-1].close
    return (high - price) / price * 100, (low - price) / price * 100, (close - price) / price * 100


def _live_entry_gate(row: dict) -> str:
    if row["stage"] != "accepted":
        return "not_core_accepted"
    action = row.get("action")
    price = float(row.get("price") or 0.0)
    stop = float(row.get("stop") or 0.0)
    take_profit = float(row.get("tp") or 0.0)
    if action == "PLAN_SHORT":
        if stop > 0 and price >= stop:
            return "skip_stop_breached"
        if take_profit > 0 and price <= take_profit:
            return "skip_take_profit_breached"
    if action == "PLAN_LONG":
        if stop > 0 and price <= stop:
            return "skip_stop_breached"
        if take_profit > 0 and price >= take_profit:
            return "skip_take_profit_breached"
    return "live_entry_allowed"


def _tp_overshoot_atr(row: dict) -> float | None:
    action = row.get("action")
    price = float(row.get("price") or 0.0)
    tp = float(row.get("tp") or 0.0)
    entry = float(row.get("entry") or 0.0)
    if action == "PLAN_SHORT":
        overshoot = tp - price
    elif action == "PLAN_LONG":
        overshoot = price - tp
    else:
        return None
    if overshoot <= 0:
        return 0.0
    atr = float(row.get("atr") or 0.0)
    if atr <= 0:
        atr = abs(entry - tp)
    if atr <= 0:
        return None
    return overshoot / atr


def _new_entry_gate(row: dict, min_score: int = 80, max_overshoot_atr: float = 1.0) -> str:
    old_gate = _live_entry_gate(row)
    if old_gate == "live_entry_allowed":
        return "v13_direct"
    if old_gate != "skip_take_profit_breached":
        return "v13_stale_skip"
    action = row.get("action")
    regime = row.get("regime")
    score = int(row.get("score") or 0)
    if action == "PLAN_SHORT" and regime not in {"trend_down", "high_volatility"}:
        return "v13_pending"
    if action == "PLAN_LONG" and regime not in {"trend_up", "high_volatility"}:
        return "v13_pending"
    overshoot_atr = _tp_overshoot_atr(row)
    if overshoot_atr is not None and score >= min_score and overshoot_atr <= max_overshoot_atr:
        return "v13_reanchor"
    return "v13_pending"


def _reanchored_levels(row: dict, executed_entry: float) -> tuple[float, float]:
    action = row.get("action")
    planned_entry = float(row.get("entry") or row.get("price") or 0.0)
    planned_stop = float(row.get("stop") or 0.0)
    planned_tp = float(row.get("tp") or 0.0)
    if action == "PLAN_SHORT":
        risk_distance = max(planned_stop - planned_entry, 0.0)
        reward_distance = max(planned_entry - planned_tp, 0.0)
        stop = executed_entry + risk_distance if risk_distance > 0 else executed_entry * 1.015
        tp = executed_entry - reward_distance if reward_distance > 0 else executed_entry * 0.99
    else:
        risk_distance = max(planned_entry - planned_stop, 0.0)
        reward_distance = max(planned_tp - planned_entry, 0.0)
        stop = executed_entry - risk_distance if risk_distance > 0 else executed_entry * 0.985
        tp = executed_entry + reward_distance if reward_distance > 0 else executed_entry * 1.01
    return round(stop, 4), round(tp, 4)


def _simulate_orders(candles: list[Candle], rows: list[dict], mode: str) -> dict:
    by_time = {row["open_time_ms"]: row for row in rows}
    opened = []
    pending_created = 0
    position = None
    for candle in candles:
        if position is not None:
            side = position["side"]
            stop = position["stop"]
            tp = position["tp"]
            exit_reason = None
            if side == "short":
                if candle.high >= stop:
                    exit_reason = "stop"
                elif candle.low <= tp:
                    exit_reason = "tp"
            else:
                if candle.low <= stop:
                    exit_reason = "stop"
                elif candle.high >= tp:
                    exit_reason = "tp"
            if exit_reason is None and candle.open_time_ms - position["opened_at_ms"] >= 48 * 300_000:
                exit_reason = "max_hold"
            if exit_reason is not None:
                position["exit_reason"] = exit_reason
                position["closed_at_ms"] = candle.open_time_ms
                position = None

        if position is not None:
            continue
        row = by_time.get(candle.open_time_ms)
        if row is None or row.get("stage") != "accepted":
            continue
        old_gate = row.get("live_gate")
        new_gate = row.get("new_gate")
        if mode == "old" and old_gate != "live_entry_allowed":
            continue
        if mode == "new" and new_gate == "v13_pending":
            pending_created += 1
            continue
        if mode == "new" and new_gate not in {"v13_direct", "v13_reanchor"}:
            continue

        side = "short" if row.get("action") == "PLAN_SHORT" else "long"
        entry = float(row.get("close") or row.get("price") or 0.0)
        stop, tp = _reanchored_levels(row, entry)
        position = {
            "time": row["time"],
            "opened_at_ms": candle.open_time_ms,
            "side": side,
            "strategy": row.get("strategy"),
            "entry_mode": new_gate if mode == "new" else "old_direct",
            "entry": entry,
            "stop": stop,
            "tp": tp,
        }
        opened.append(position)

    return {
        "orders": len(opened),
        "pending_created": pending_created,
        "modes": dict(Counter(order["entry_mode"] for order in opened)),
        "strategies": dict(Counter(order["strategy"] for order in opened)),
        "last_orders": opened[-10:],
    }


def _trend350_journal(candles: list[Candle], base: StrategyConfig, cutoff_ms: int) -> dict:
    config = OrbConfig(base=base, session_start_bar=0, opening_range_bars=9, min_volume_ratio=0.8, stop_atr=0.6)
    summary, rows = run_orb_signal_journal(
        candles,
        config,
        side="router",
        regime_router_enabled=True,
        regime_router_defensive_scale=0.70,
        regime_router_exploratory_scale=0.35,
        nim_reviewer=None,
        nim_query_policy="auto",
        ai_risk_judge_enabled=True,
        ai_risk_judge_query_policy="local",
        regime_allocator_enabled=True,
        allocator_protect_loss_pct=2.0,
        allocator_lock_profit_pct=1.5,
        allocator_protect_scale=0.45,
        allocator_lock_scale=1.00,
        allocator_trend_aggressive_scale=3.50,
        allocator_trend_normal_scale=1.00,
        allocator_trend_normal_low_quality_scale=0.35,
        allocator_trend_normal_weak_scale=0.35,
        allocator_short_scale=0.55,
        allocator_short_weak_low_atr_scale=0.25,
        allocator_short_fake_risk_scale=0.05,
        allocator_short_exhaustion_scale=0.30,
        allocator_short_exhaustion_strong_scale=None,
        allocator_short_breakdown_scale=1.25,
        allocator_volatility_short_breakdown_scale=0.45,
        allocator_reversion_scale=0.05,
        allocator_weak_pullback_scale=0.30,
        allocator_weak_pullback_normal_scale=0.20,
        allocator_aggressive_no_trade_scale=0.05,
        allocator_max_risk_pct=0.0,
        allocator_max_margin_pct=100.0,
        regime_exit_profile_enabled=True,
        defensive_exit_weights=(0.25, 0.35, 0.40),
        defensive_max_holding_bars=24,
        defensive_exit_scope="short_reversion",
    )
    filtered = [row for row in rows if row.signal_time_ms >= cutoff_ms]
    return {
        "orders": len(filtered),
        "total_pnl_usdc": round(sum(row.pnl_usdc for row in filtered), 4),
        "strategies": dict(Counter(row.strategy for row in filtered)),
        "exits": dict(Counter(row.exit_reason for row in filtered)),
        "journal_summary": summarize_signal_journal(filtered),
        "last_orders": [
            {
                "signal_time": row.signal_time_iso,
                "strategy": row.strategy,
                "score": row.score,
                "regime": row.regime,
                "playbook": row.market_playbook,
                "pnl_usdc": round(row.pnl_usdc, 4),
                "r": round(row.r_multiple, 4),
                "exit": row.exit_reason,
                "hold_bars": row.hold_bars,
            }
            for row in filtered[-10:]
        ],
        "full_window_trade_count": summary.total_trades,
    }


async def _load_candles(symbol: str, interval: str, limit: int) -> list[Candle]:
    client = BinanceFuturesClient(Settings())
    try:
        await client.connect()
        rows = await client.get_klines(symbol, interval=interval, limit=limit)
    finally:
        await client.close()
    return [Candle.from_binance_kline(row) for row in rows]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="ETHUSDC")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--limit", type=int, default=600)
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--forward-bars", type=int, default=12)
    parser.add_argument("--today-tz", default="")
    args = parser.parse_args()

    settings = Settings()
    candles = await _load_candles(args.symbol, args.interval, args.limit)
    base = _live_base(settings, args.symbol)
    config = OrbConfig(base=base, session_start_bar=0, opening_range_bars=9, min_volume_ratio=0.8, stop_atr=0.6)
    context = build_orb_context(candles, config)
    regime_context = build_regime_context(candles, config.base)
    market_context = build_market_state_context(candles, config.base)
    if args.today_tz:
        local_tz = ZoneInfo(args.today_tz)
        local_start = datetime.now(local_tz).replace(hour=0, minute=0, second=0, microsecond=0)
        cutoff_ms = int(local_start.astimezone(timezone.utc).timestamp() * 1000)
    else:
        cutoff_ms = int((datetime.now(timezone.utc) - timedelta(hours=args.hours)).timestamp() * 1000)
    start = next((idx for idx, candle in enumerate(candles) if candle.open_time_ms >= cutoff_ms), max(0, len(candles) - args.hours * 12))
    traces = []
    for index in range(max(300, start), len(candles) - args.forward_bars - 1):
        row = _trace_one(candles, index, base, context, regime_context, market_context)
        up, down, close = _forward_move(candles, index, args.forward_bars)
        row.update(
            time=datetime.fromtimestamp(candles[index].open_time_ms / 1000, timezone.utc).isoformat(),
            close=candles[index].close,
            fwd_up=up,
            fwd_down=down,
            fwd_close=close,
            open_time_ms=candles[index].open_time_ms,
        )
        row["live_gate"] = _live_entry_gate(row)
        row["new_gate"] = _new_entry_gate(row)
        traces.append(row)

    if not traces:
        print("NO_TRACES")
        return

    print("WINDOW", traces[0]["time"], traces[-1]["time"], "N", len(traces))
    print("STAGE_COUNTS", dict(Counter(row["stage"] for row in traces)))
    print("LIVE_GATE_COUNTS", dict(Counter(row["live_gate"] for row in traces)))
    print("NEW_GATE_COUNTS", dict(Counter(row["new_gate"] for row in traces)))
    print("ORDER_SIM_V13_STRICT", _simulate_orders(candles[start:], traces, "old"))
    print("ORDER_SIM_NEW", _simulate_orders(candles[start:], traces, "new"))
    print("TREND350_JOURNAL", _trend350_journal(candles, base, cutoff_ms))
    print("STAGE_STRATEGY_COUNTS")
    for key, value in Counter((row["stage"], row.get("strategy", "")) for row in traces).most_common(20):
        print(key, value)
    print("REGIME_COUNTS", dict(Counter(row.get("regime", "") for row in traces)))
    print("PLAYBOOK_COUNTS", dict(Counter(row.get("playbook", "") for row in traces)))
    print("TOP_BLOCKED_FORWARD_MOVES")
    blocked = [row for row in traces if row["stage"] != "accepted"]
    for row in sorted(blocked, key=lambda item: max(abs(item["fwd_up"]), abs(item["fwd_down"])), reverse=True)[:15]:
        print(
            row["time"],
            "stage=", row["stage"],
            "strategy=", row.get("strategy"),
            "action=", row.get("action"),
            "expected=", row.get("expected"),
            "score=", row.get("score"),
            "regime=", row.get("regime"),
            "playbook=", row.get("playbook"),
            "close=", round(row["close"], 2),
            "fwd_up=", round(row["fwd_up"], 2),
            "fwd_down=", round(row["fwd_down"], 2),
            "fwd_close=", round(row["fwd_close"], 2),
            "reason=", row.get("reason"),
        )
    print("ACCEPTED")
    for row in [item for item in traces if item["stage"] == "accepted"][-20:]:
        print(
            row["time"],
            row.get("strategy"),
            row.get("action"),
            row.get("score"),
            row.get("regime"),
            row.get("playbook"),
            "close=", round(row["close"], 2),
            "entry=", round(row.get("entry") or 0.0, 2),
            "stop=", round(row.get("stop") or 0.0, 2),
            "tp=", round(row.get("tp") or 0.0, 2),
            "live_gate=", row.get("live_gate"),
            "new_gate=", row.get("new_gate"),
            "fwd_close=", round(row["fwd_close"], 2),
            "reason=", row.get("reason"),
        )


if __name__ == "__main__":
    asyncio.run(main())
