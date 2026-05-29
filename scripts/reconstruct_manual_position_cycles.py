from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import Settings
from scripts.analyze_manual_mainnet import (
    BINANCE_RECENT_ORDERS_MAX_LOOKBACK_MS,
    dt_to_ms,
    fetch_all_symbol_orders,
    fetch_all_symbol_trades,
    manual_trade_filter,
    ms_to_taipei,
)
from src.gridbot.binance.client import BinanceFuturesClient
from src.gridbot.binance.models import FuturesTrade


@dataclass
class Cycle:
    symbol: str
    side: str
    opened_at_ms: int
    closed_at_ms: int | None = None
    entry_qty: float = 0.0
    max_abs_qty: float = 0.0
    avg_entry_price: float = 0.0
    realized_pnl: float = 0.0
    commission: float = 0.0
    entry_fills: int = 0
    exit_fills: int = 0
    maker_entry_fills: int = 0
    maker_exit_fills: int = 0
    scale_in_count: int = 0
    partial_exit_count: int = 0
    entry_order_ids: set[int] = field(default_factory=set)
    exit_order_ids: set[int] = field(default_factory=set)

    @property
    def net_pnl(self) -> float:
        return self.realized_pnl - abs(self.commission)

    @property
    def notional_usdc(self) -> float:
        return abs(self.max_abs_qty) * self.avg_entry_price

    @property
    def maker_entry_ratio(self) -> float:
        return self.maker_entry_fills / self.entry_fills if self.entry_fills else 0.0

    @property
    def maker_exit_ratio(self) -> float:
        return self.maker_exit_fills / self.exit_fills if self.exit_fills else 0.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reconstruct manual one-way futures position cycles from Binance fills.")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--symbol", default="ETHUSDC")
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--include-open", action="store_true")
    parser.add_argument("--burst-gap-minutes", type=int, default=30)
    return parser.parse_args()


def _signed_qty(trade: FuturesTrade) -> float:
    return abs(trade.qty) if trade.side.upper() == "BUY" else -abs(trade.qty)


def _new_cycle(symbol: str, signed_qty: float, trade: FuturesTrade) -> Cycle:
    side = "LONG" if signed_qty > 0 else "SHORT"
    cycle = Cycle(
        symbol=symbol,
        side=side,
        opened_at_ms=trade.time_ms,
        entry_qty=abs(signed_qty),
        max_abs_qty=abs(signed_qty),
        avg_entry_price=trade.price,
        entry_fills=1,
        maker_entry_fills=1 if trade.is_maker else 0,
        entry_order_ids={trade.order_id},
    )
    return cycle


def _add_entry_fill(cycle: Cycle, signed_position: float, trade: FuturesTrade) -> None:
    add_qty = abs(trade.qty)
    old_qty = abs(signed_position)
    total_qty = old_qty + add_qty
    if total_qty > 0:
        cycle.avg_entry_price = ((cycle.avg_entry_price * old_qty) + (trade.price * add_qty)) / total_qty
    cycle.entry_qty += add_qty
    cycle.max_abs_qty = max(cycle.max_abs_qty, total_qty)
    cycle.entry_fills += 1
    cycle.maker_entry_fills += 1 if trade.is_maker else 0
    cycle.scale_in_count += 1
    cycle.entry_order_ids.add(trade.order_id)


def _add_exit_fill(cycle: Cycle, trade: FuturesTrade, close_fraction: float) -> None:
    commission = abs(trade.commission) * close_fraction
    realized = trade.realized_pnl * close_fraction
    cycle.realized_pnl += realized
    cycle.commission += commission
    cycle.exit_fills += 1
    cycle.maker_exit_fills += 1 if trade.is_maker else 0
    cycle.exit_order_ids.add(trade.order_id)


def reconstruct_cycles(trades: list[FuturesTrade], *, include_open: bool) -> list[Cycle]:
    cycles: list[Cycle] = []
    current: Cycle | None = None
    position_qty = 0.0
    eps = 1e-10

    for trade in sorted(trades, key=lambda item: (item.time_ms, item.trade_id)):
        delta = _signed_qty(trade)
        if abs(delta) <= eps:
            continue

        if abs(position_qty) <= eps:
            current = _new_cycle(trade.symbol, delta, trade)
            position_qty = delta
            continue

        same_direction = position_qty * delta > 0
        if same_direction:
            if current is not None:
                _add_entry_fill(current, position_qty, trade)
            position_qty += delta
            continue

        old_abs = abs(position_qty)
        delta_abs = abs(delta)
        close_qty = min(old_abs, delta_abs)
        close_fraction = close_qty / delta_abs if delta_abs > eps else 1.0
        if current is not None:
            if close_qty < old_abs - eps:
                current.partial_exit_count += 1
            _add_exit_fill(current, trade, close_fraction)

        new_position = position_qty + delta
        if abs(new_position) <= eps:
            if current is not None:
                current.closed_at_ms = trade.time_ms
                cycles.append(current)
            current = None
            position_qty = 0.0
            continue

        flipped = position_qty * new_position < 0
        if flipped:
            if current is not None:
                current.closed_at_ms = trade.time_ms
                cycles.append(current)
            open_qty = abs(new_position)
            current = _new_cycle(trade.symbol, new_position, trade)
            current.entry_qty = open_qty
            current.max_abs_qty = open_qty
            current.entry_fills = 1
            current.maker_entry_fills = 1 if trade.is_maker else 0
            open_fraction = open_qty / delta_abs if delta_abs > eps else 0.0
            current.commission += abs(trade.commission) * open_fraction
            position_qty = new_position
            continue

        position_qty = new_position

    if include_open and current is not None:
        cycles.append(current)
    return cycles


def _cycle_to_dict(cycle: Cycle) -> dict:
    row = asdict(cycle)
    row["opened_at"] = ms_to_taipei(cycle.opened_at_ms).isoformat()
    row["closed_at"] = ms_to_taipei(cycle.closed_at_ms).isoformat() if cycle.closed_at_ms else None
    row["duration_minutes"] = (
        (cycle.closed_at_ms - cycle.opened_at_ms) / 60_000 if cycle.closed_at_ms is not None else None
    )
    row["net_pnl_ex_funding"] = cycle.net_pnl
    row["notional_usdc"] = cycle.notional_usdc
    row["maker_entry_ratio"] = cycle.maker_entry_ratio
    row["maker_exit_ratio"] = cycle.maker_exit_ratio
    row["entry_order_ids"] = sorted(cycle.entry_order_ids)
    row["exit_order_ids"] = sorted(cycle.exit_order_ids)
    return row


def summarize(cycles: list[Cycle]) -> dict:
    wins = [cycle for cycle in cycles if cycle.net_pnl > 0]
    losses = [cycle for cycle in cycles if cycle.net_pnl < 0]
    gross_profit = sum(cycle.net_pnl for cycle in wins)
    gross_loss = abs(sum(cycle.net_pnl for cycle in losses))
    by_day: defaultdict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for cycle in cycles:
        key = ms_to_taipei(cycle.opened_at_ms).date().isoformat()
        by_day[key]["cycles"] += 1
        by_day[key]["net_pnl_ex_funding"] += cycle.net_pnl
        by_day[key]["realized_pnl"] += cycle.realized_pnl
        by_day[key]["commission"] += abs(cycle.commission)
    return {
        "cycles": len(cycles),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(cycles) if cycles else 0.0,
        "net_pnl_ex_funding": sum(cycle.net_pnl for cycle in cycles),
        "realized_pnl": sum(cycle.realized_pnl for cycle in cycles),
        "commission": sum(abs(cycle.commission) for cycle in cycles),
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "best_cycle_net": max((cycle.net_pnl for cycle in cycles), default=0.0),
        "worst_cycle_net": min((cycle.net_pnl for cycle in cycles), default=0.0),
        "maker_entry_ratio": (
            sum(cycle.maker_entry_fills for cycle in cycles) / sum(cycle.entry_fills for cycle in cycles)
            if sum(cycle.entry_fills for cycle in cycles)
            else 0.0
        ),
        "by_day": {
            key: {
                "cycles": int(bucket["cycles"]),
                "net_pnl_ex_funding": bucket["net_pnl_ex_funding"],
                "realized_pnl": bucket["realized_pnl"],
                "commission": bucket["commission"],
            }
            for key, bucket in sorted(by_day.items())
        },
    }


def _trade_to_dict(trade: FuturesTrade) -> dict:
    return {
        "trade_id": trade.trade_id,
        "order_id": trade.order_id,
        "symbol": trade.symbol,
        "side": trade.side,
        "price": trade.price,
        "qty": abs(trade.qty),
        "quote_qty": abs(trade.quote_qty) if trade.quote_qty else abs(trade.price * trade.qty),
        "realized_pnl": trade.realized_pnl,
        "commission": abs(trade.commission),
        "net_pnl_ex_funding": trade.realized_pnl - abs(trade.commission),
        "time_ms": trade.time_ms,
        "time": ms_to_taipei(trade.time_ms).isoformat(),
        "is_maker": trade.is_maker,
        "position_side": trade.position_side,
    }


def reconstruct_bursts(trades: list[FuturesTrade], gap_minutes: int) -> list[dict]:
    sorted_trades = sorted(trades, key=lambda item: (item.time_ms, item.trade_id))
    if not sorted_trades:
        return []
    bursts: list[list[FuturesTrade]] = []
    current: list[FuturesTrade] = [sorted_trades[0]]
    gap_ms = gap_minutes * 60_000
    for trade in sorted_trades[1:]:
        if trade.time_ms - current[-1].time_ms > gap_ms:
            bursts.append(current)
            current = [trade]
        else:
            current.append(trade)
    bursts.append(current)

    rows: list[dict] = []
    for idx, items in enumerate(bursts, start=1):
        buy_qty = sum(abs(item.qty) for item in items if item.side.upper() == "BUY")
        sell_qty = sum(abs(item.qty) for item in items if item.side.upper() == "SELL")
        maker_fills = sum(1 for item in items if item.is_maker)
        net = sum(item.realized_pnl - abs(item.commission) for item in items)
        quote = sum(abs(item.quote_qty) if item.quote_qty else abs(item.price * item.qty) for item in items)
        side = "BUY" if buy_qty > sell_qty else "SELL" if sell_qty > buy_qty else "MIXED"
        rows.append(
            {
                "burst_id": idx,
                "started_at_ms": items[0].time_ms,
                "ended_at_ms": items[-1].time_ms,
                "started_at": ms_to_taipei(items[0].time_ms).isoformat(),
                "ended_at": ms_to_taipei(items[-1].time_ms).isoformat(),
                "duration_minutes": (items[-1].time_ms - items[0].time_ms) / 60_000,
                "fills": len(items),
                "orders": len({item.order_id for item in items}),
                "dominant_side": side,
                "buy_qty": buy_qty,
                "sell_qty": sell_qty,
                "turnover_usdc": quote,
                "realized_pnl": sum(item.realized_pnl for item in items),
                "commission": sum(abs(item.commission) for item in items),
                "net_pnl_ex_funding": net,
                "maker_ratio": maker_fills / len(items) if items else 0.0,
                "first_price": items[0].price,
                "last_price": items[-1].price,
                "fills_detail": [_trade_to_dict(item) for item in items],
            }
        )
    return rows


def summarize_bursts(bursts: list[dict]) -> dict:
    wins = [item for item in bursts if item["net_pnl_ex_funding"] > 0]
    losses = [item for item in bursts if item["net_pnl_ex_funding"] < 0]
    gross_profit = sum(item["net_pnl_ex_funding"] for item in wins)
    gross_loss = abs(sum(item["net_pnl_ex_funding"] for item in losses))
    by_day: defaultdict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for item in bursts:
        key = ms_to_taipei(int(item["started_at_ms"])).date().isoformat()
        by_day[key]["bursts"] += 1
        by_day[key]["net_pnl_ex_funding"] += item["net_pnl_ex_funding"]
        by_day[key]["turnover_usdc"] += item["turnover_usdc"]
    return {
        "bursts": len(bursts),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(bursts) if bursts else 0.0,
        "net_pnl_ex_funding": sum(item["net_pnl_ex_funding"] for item in bursts),
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "best_burst_net": max((item["net_pnl_ex_funding"] for item in bursts), default=0.0),
        "worst_burst_net": min((item["net_pnl_ex_funding"] for item in bursts), default=0.0),
        "maker_ratio": (
            sum(item["maker_ratio"] * item["fills"] for item in bursts) / sum(item["fills"] for item in bursts)
            if sum(item["fills"] for item in bursts)
            else 0.0
        ),
        "by_day": {
            key: {
                "bursts": int(bucket["bursts"]),
                "net_pnl_ex_funding": bucket["net_pnl_ex_funding"],
                "turnover_usdc": bucket["turnover_usdc"],
            }
            for key, bucket in sorted(by_day.items())
        },
    }


async def main_async() -> int:
    args = parse_args()
    settings = Settings()
    client = BinanceFuturesClient(settings)
    await client.connect()
    try:
        now = datetime.now(tz=timezone.utc)
        since = now - timedelta(days=args.days)
        since_ms = dt_to_ms(since)
        end_ms = dt_to_ms(now)
        orders_since_ms = max(since_ms, end_ms - BINANCE_RECENT_ORDERS_MAX_LOOKBACK_MS)
        trades = await fetch_all_symbol_trades(client, args.symbol.upper(), since_ms, end_ms)
        orders = await fetch_all_symbol_orders(client, args.symbol.upper(), orders_since_ms, end_ms)
        manual_trades, grid_order_ids = manual_trade_filter(trades, orders)
        cycles = reconstruct_cycles(manual_trades, include_open=args.include_open)
        bursts = reconstruct_bursts(manual_trades, args.burst_gap_minutes)
        payload = {
            "generated_at": datetime.now(tz=timezone.utc).astimezone().isoformat(),
            "symbol": args.symbol.upper(),
            "days": args.days,
            "manual_fills": len(manual_trades),
            "grid_order_ids_excluded": len(grid_order_ids),
            "summary": summarize(cycles),
            "burst_gap_minutes": args.burst_gap_minutes,
            "burst_summary": summarize_bursts(bursts),
            "cycles": [_cycle_to_dict(cycle) for cycle in cycles],
            "bursts": bursts,
        }
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
        return 0
    finally:
        await client.close()


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
