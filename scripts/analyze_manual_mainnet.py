"""Analyze manual Binance USD-M Futures trades from mainnet API.

This script:
1. Pulls recent income history to discover symbols traded.
2. Fetches user trades and order history per symbol.
3. Excludes bot-originated orders using the grid clientOrderId prefix.
4. Aggregates manual-trade performance by order, day, month, and symbol.

Usage:
    python scripts/analyze_manual_mainnet.py --days 90
    python scripts/analyze_manual_mainnet.py --days 30 --symbols ETHUSDC,BTCUSDC
"""

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import Settings
from src.gridbot.binance.client import BinanceFuturesClient, is_grid_order
from src.gridbot.binance.models import FuturesTrade, IncomeRecord

TAIPEI = ZoneInfo("Asia/Taipei")
PAGE_LIMIT_TRADES = 500
PAGE_LIMIT_ORDERS = 1000
TRADES_WINDOW_MS = 7 * 24 * 60 * 60 * 1000
ORDERS_WINDOW_MS = 7 * 24 * 60 * 60 * 1000
BINANCE_RECENT_ORDERS_MAX_LOOKBACK_MS = 90 * 24 * 60 * 60 * 1000 - 60_000


@dataclass
class OrderSummary:
    order_id: int
    symbol: str
    side: str
    first_time_ms: int
    last_time_ms: int
    qty: float = 0.0
    quote_qty: float = 0.0
    fill_count: int = 0
    maker_fills: int = 0
    realized_pnl: float = 0.0
    commission: float = 0.0

    @property
    def avg_price(self) -> float:
        if self.qty <= 0:
            return 0.0
        return self.quote_qty / self.qty

    @property
    def net_pnl(self) -> float:
        return self.realized_pnl - self.commission

    @property
    def maker_ratio(self) -> float:
        if self.fill_count <= 0:
            return 0.0
        return self.maker_fills / self.fill_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze manual Binance USD-M Futures trades from mainnet")
    parser.add_argument("--days", type=int, default=90, help="Lookback window in days (default: 90)")
    parser.add_argument(
        "--start-time",
        type=str,
        default="",
        help="Start time (Taipei Time, e.g. '2026-05-29 00:00:00')",
    )
    parser.add_argument(
        "--end-time",
        type=str,
        default="",
        help="End time (Taipei Time, e.g. '2026-05-29 12:00:00')",
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default="",
        help="Comma-separated symbol allowlist. Leave empty to auto-discover from income history.",
    )
    parser.add_argument(
        "--output-json",
        type=str,
        default="",
        help="Optional path to save the raw analysis result as JSON.",
    )
    parser.add_argument(
        "--top-orders",
        type=int,
        default=10,
        help="How many best/worst manual orders to print (default: 10)",
    )
    return parser.parse_args()


def dt_to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def ms_to_taipei(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(TAIPEI)


def month_key(ms: int) -> str:
    return ms_to_taipei(ms).strftime("%Y-%m")


def day_key(ms: int) -> str:
    return ms_to_taipei(ms).strftime("%Y-%m-%d")


def fmt_num(value: float) -> str:
    return f"{value:,.4f}"


def render_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))
    line = " | ".join(header.ljust(widths[idx]) for idx, header in enumerate(headers))
    sep = "-+-".join("-" * widths[idx] for idx in range(len(headers)))
    body = [" | ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row)) for row in rows]
    return "\n".join([line, sep, *body])


async def fetch_all_symbol_trades(client: BinanceFuturesClient, symbol: str, since_ms: int, end_ms: int) -> list[FuturesTrade]:
    trades_by_id: dict[int, FuturesTrade] = {}
    window_start = since_ms
    while window_start <= end_ms:
        window_end = min(window_start + TRADES_WINDOW_MS - 1, end_ms)
        raw_batch = await client.client.futures_account_trades(
            symbol=symbol,
            startTime=window_start,
            endTime=window_end,
            limit=1000,
        )
        batch = [FuturesTrade.from_api(item) for item in raw_batch]
        for trade in batch:
            trades_by_id[trade.trade_id] = trade
        window_start = window_end + 1
    return sorted(trades_by_id.values(), key=lambda item: item.time_ms)


async def fetch_all_symbol_orders(client: BinanceFuturesClient, symbol: str, since_ms: int, end_ms: int) -> list[dict]:
    orders_by_id: dict[int, dict] = {}
    window_start = since_ms
    while window_start <= end_ms:
        window_end = min(window_start + ORDERS_WINDOW_MS - 1, end_ms)
        raw_batch = await client.client.futures_get_all_orders(
            symbol=symbol,
            startTime=window_start,
            endTime=window_end,
            limit=PAGE_LIMIT_ORDERS,
        )
        for order in raw_batch:
            order_id = order.get("orderId")
            if order_id is not None:
                orders_by_id[int(order_id)] = order
        window_start = window_end + 1
    return sorted(
        orders_by_id.values(),
        key=lambda item: int(item.get("time", 0) or item.get("updateTime", 0) or 0),
    )


def manual_trade_filter(trades: list[FuturesTrade], orders: list[dict]) -> tuple[list[FuturesTrade], set[int]]:
    order_id_to_client_id = {
        int(order["orderId"]): str(order.get("clientOrderId", ""))
        for order in orders
        if "orderId" in order
    }
    grid_order_ids = {
        order_id
        for order_id, client_id in order_id_to_client_id.items()
        if is_grid_order(client_id)
    }
    manual_trades = [trade for trade in trades if trade.order_id not in grid_order_ids]
    return manual_trades, grid_order_ids


def aggregate_order_summaries(
    manual_trades: list[FuturesTrade],
    manual_realized_by_trade_id: dict[str, float],
    manual_commission_by_trade_id: dict[str, float],
) -> list[OrderSummary]:
    by_order: dict[int, OrderSummary] = {}
    for trade in manual_trades:
        summary = by_order.get(trade.order_id)
        if summary is None:
            summary = OrderSummary(
                order_id=trade.order_id,
                symbol=trade.symbol,
                side=trade.side,
                first_time_ms=trade.time_ms,
                last_time_ms=trade.time_ms,
            )
            by_order[trade.order_id] = summary
        summary.first_time_ms = min(summary.first_time_ms, trade.time_ms)
        summary.last_time_ms = max(summary.last_time_ms, trade.time_ms)
        summary.qty += abs(trade.qty)
        summary.quote_qty += abs(trade.quote_qty) if trade.quote_qty else abs(trade.price * trade.qty)
        summary.fill_count += 1
        summary.maker_fills += 1 if trade.is_maker else 0
        trade_id_key = str(trade.trade_id)
        summary.realized_pnl += manual_realized_by_trade_id.get(trade_id_key, 0.0)
        summary.commission += abs(manual_commission_by_trade_id.get(trade_id_key, 0.0))
    return sorted(by_order.values(), key=lambda item: item.first_time_ms)


def aggregate_income(records: Iterable[IncomeRecord]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    for record in records:
        totals[record.income_type] += float(record.income)
    return dict(totals)


async def analyze_manual_mainnet(
    days: int,
    symbols_filter: list[str],
    start_time_str: str = "",
    end_time_str: str = "",
) -> dict:
    settings = Settings()
    client = BinanceFuturesClient(settings)
    await client.connect()
    try:
        now = datetime.now(tz=timezone.utc)
        if start_time_str:
            since_dt = datetime.strptime(start_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TAIPEI)
            since_ms = dt_to_ms(since_dt)
        else:
            since_dt = now - timedelta(days=days)
            since_ms = dt_to_ms(since_dt)

        if end_time_str:
            end_dt = datetime.strptime(end_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TAIPEI)
            end_ms = dt_to_ms(end_dt)
        else:
            end_dt = now
            end_ms = dt_to_ms(end_dt)

        orders_since_ms = max(since_ms, end_ms - BINANCE_RECENT_ORDERS_MAX_LOOKBACK_MS)

        realized_income = await client.get_all_income_since(since_ms, income_type="REALIZED_PNL")
        commission_income = await client.get_all_income_since(since_ms, income_type="COMMISSION")
        funding_income = await client.get_all_income_since(since_ms, income_type="FUNDING_FEE")

        discovered_symbols = {
            record.symbol
            for record in [*realized_income, *commission_income, *funding_income]
            if record.symbol
        }
        symbols = sorted({symbol.upper() for symbol in symbols_filter if symbol.strip()} or discovered_symbols)

        manual_trades_all: list[FuturesTrade] = []
        manual_trade_ids: set[str] = set()
        symbol_trade_counts: dict[str, int] = {}

        for symbol in symbols:
            trades = await fetch_all_symbol_trades(client, symbol, since_ms, end_ms)
            if not trades:
                continue
            orders = await fetch_all_symbol_orders(client, symbol, orders_since_ms, end_ms)
            manual_trades, _grid_order_ids = manual_trade_filter(trades, orders)
            manual_trades_all.extend(manual_trades)
            symbol_trade_counts[symbol] = len(manual_trades)
            manual_trade_ids.update(str(trade.trade_id) for trade in manual_trades)

        manual_realized = [record for record in realized_income if record.trade_id and record.trade_id in manual_trade_ids]
        manual_commission = [record for record in commission_income if record.trade_id and record.trade_id in manual_trade_ids]

        manual_realized_by_trade_id: dict[str, float] = defaultdict(float)
        for record in manual_realized:
            manual_realized_by_trade_id[record.trade_id] += float(record.income)

        manual_commission_by_trade_id: dict[str, float] = defaultdict(float)
        for record in manual_commission:
            manual_commission_by_trade_id[record.trade_id] += float(record.income)

        order_summaries = aggregate_order_summaries(
            manual_trades_all,
            manual_realized_by_trade_id,
            manual_commission_by_trade_id,
        )

        manual_trades_all.sort(key=lambda trade: trade.time_ms)

        by_symbol: dict[str, dict[str, float | int]] = defaultdict(lambda: defaultdict(float))
        by_day: dict[str, dict[str, float | int]] = defaultdict(lambda: defaultdict(float))
        by_month: dict[str, dict[str, float | int]] = defaultdict(lambda: defaultdict(float))

        for summary in order_summaries:
            symbol_bucket = by_symbol[summary.symbol]
            symbol_bucket["orders"] += 1
            symbol_bucket["fills"] += summary.fill_count
            symbol_bucket["realized_pnl"] += summary.realized_pnl
            symbol_bucket["commission"] += summary.commission
            symbol_bucket["net_pnl"] += summary.net_pnl
            symbol_bucket["maker_fills"] += summary.maker_fills

            d_key = day_key(summary.first_time_ms)
            day_bucket = by_day[d_key]
            day_bucket["orders"] += 1
            day_bucket["realized_pnl"] += summary.realized_pnl
            day_bucket["commission"] += summary.commission
            day_bucket["net_pnl"] += summary.net_pnl

            m_key = month_key(summary.first_time_ms)
            month_bucket = by_month[m_key]
            month_bucket["orders"] += 1
            month_bucket["realized_pnl"] += summary.realized_pnl
            month_bucket["commission"] += summary.commission
            month_bucket["net_pnl"] += summary.net_pnl

        wins = [item for item in order_summaries if item.net_pnl > 0]
        losses = [item for item in order_summaries if item.net_pnl < 0]

        total_realized = sum(item.realized_pnl for item in order_summaries)
        total_commission = sum(item.commission for item in order_summaries)
        total_net = sum(item.net_pnl for item in order_summaries)
        total_fills = sum(item.fill_count for item in order_summaries)
        total_maker_fills = sum(item.maker_fills for item in order_summaries)
        maker_ratio = (total_maker_fills / total_fills) if total_fills else 0.0
        win_rate = (len(wins) / len(order_summaries)) if order_summaries else 0.0
        gross_profit = sum(item.net_pnl for item in wins)
        gross_loss = abs(sum(item.net_pnl for item in losses))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

        funding_by_symbol: dict[str, float] = defaultdict(float)
        for record in funding_income:
            if record.symbol:
                funding_by_symbol[record.symbol] += float(record.income)

        return {
            "generated_at": datetime.now(tz=TAIPEI).isoformat(),
            "window_days": days,
            "window_start": since_dt.astimezone(TAIPEI).isoformat(),
            "symbols_considered": symbols,
            "symbol_trade_counts": symbol_trade_counts,
            "summary": {
                "manual_orders": len(order_summaries),
                "manual_fills": len(manual_trades_all),
                "symbols_traded": sorted(by_symbol.keys()),
                "realized_pnl": total_realized,
                "commission": total_commission,
                "net_pnl_ex_funding": total_net,
                "maker_ratio": maker_ratio,
                "win_rate": win_rate,
                "profit_factor": profit_factor,
                "avg_order_net": (total_net / len(order_summaries)) if order_summaries else 0.0,
                "best_order_net": max((item.net_pnl for item in order_summaries), default=0.0),
                "worst_order_net": min((item.net_pnl for item in order_summaries), default=0.0),
            },
            "by_symbol": {
                symbol: {
                    "orders": int(bucket["orders"]),
                    "fills": int(bucket["fills"]),
                    "realized_pnl": bucket["realized_pnl"],
                    "commission": bucket["commission"],
                    "net_pnl_ex_funding": bucket["net_pnl"],
                    "maker_ratio": (bucket["maker_fills"] / bucket["fills"]) if bucket["fills"] else 0.0,
                    "funding_context": funding_by_symbol.get(symbol, 0.0),
                }
                for symbol, bucket in sorted(by_symbol.items())
            },
            "by_day": {
                key: {
                    "orders": int(bucket["orders"]),
                    "realized_pnl": bucket["realized_pnl"],
                    "commission": bucket["commission"],
                    "net_pnl_ex_funding": bucket["net_pnl"],
                }
                for key, bucket in sorted(by_day.items())
            },
            "by_month": {
                key: {
                    "orders": int(bucket["orders"]),
                    "realized_pnl": bucket["realized_pnl"],
                    "commission": bucket["commission"],
                    "net_pnl_ex_funding": bucket["net_pnl"],
                }
                for key, bucket in sorted(by_month.items())
            },
            "top_winners": [
                {
                    "order_id": item.order_id,
                    "symbol": item.symbol,
                    "side": item.side,
                    "started_at": ms_to_taipei(item.first_time_ms).isoformat(),
                    "fills": item.fill_count,
                    "avg_price": item.avg_price,
                    "qty": item.qty,
                    "realized_pnl": item.realized_pnl,
                    "commission": item.commission,
                    "net_pnl_ex_funding": item.net_pnl,
                }
                for item in sorted(order_summaries, key=lambda row: row.net_pnl, reverse=True)
            ],
            "top_losers": [
                {
                    "order_id": item.order_id,
                    "symbol": item.symbol,
                    "side": item.side,
                    "started_at": ms_to_taipei(item.first_time_ms).isoformat(),
                    "fills": item.fill_count,
                    "avg_price": item.avg_price,
                    "qty": item.qty,
                    "realized_pnl": item.realized_pnl,
                    "commission": item.commission,
                    "net_pnl_ex_funding": item.net_pnl,
                }
                for item in sorted(order_summaries, key=lambda row: row.net_pnl)
            ],
            "order_summaries": [
                {
                    "order_id": item.order_id,
                    "symbol": item.symbol,
                    "side": item.side,
                    "time_first_ms": item.first_time_ms,
                    "time_last_ms": item.last_time_ms,
                    "started_at": ms_to_taipei(item.first_time_ms).isoformat(),
                    "ended_at": ms_to_taipei(item.last_time_ms).isoformat(),
                    "fills": item.fill_count,
                    "qty": item.qty,
                    "quote_qty": item.quote_qty,
                    "avg_price": item.avg_price,
                    "maker_fills": item.maker_fills,
                    "maker_ratio": item.maker_ratio,
                    "realized_pnl": item.realized_pnl,
                    "commission": item.commission,
                    "net_pnl_ex_funding": item.net_pnl,
                }
                for item in order_summaries
            ],
        }
    finally:
        await client.close()


def print_analysis(result: dict, top_orders: int) -> None:
    summary = result["summary"]
    print("=== Manual Mainnet Trade Analysis ===")
    print(f"Generated At (Taipei): {result['generated_at']}")
    print(f"Window: last {result['window_days']} days since {result['window_start']}")
    print(f"Symbols considered: {', '.join(result['symbols_considered']) or '(none)'}")
    print()
    print("Summary")
    print(
        render_table(
            ["orders", "fills", "realized", "commission", "net ex funding", "maker ratio", "win rate", "profit factor"],
            [[
                str(summary["manual_orders"]),
                str(summary["manual_fills"]),
                fmt_num(summary["realized_pnl"]),
                fmt_num(summary["commission"]),
                fmt_num(summary["net_pnl_ex_funding"]),
                f"{summary['maker_ratio'] * 100:.1f}%",
                f"{summary['win_rate'] * 100:.1f}%",
                "n/a" if summary["profit_factor"] is None else f"{summary['profit_factor']:.2f}",
            ]],
        )
    )
    print()

    if result["by_symbol"]:
        symbol_rows = []
        for symbol, bucket in result["by_symbol"].items():
            symbol_rows.append([
                symbol,
                str(bucket["orders"]),
                str(bucket["fills"]),
                fmt_num(bucket["realized_pnl"]),
                fmt_num(bucket["commission"]),
                fmt_num(bucket["net_pnl_ex_funding"]),
                f"{bucket['maker_ratio'] * 100:.1f}%",
                fmt_num(bucket["funding_context"]),
            ])
        print("By Symbol")
        print(render_table(
            ["symbol", "orders", "fills", "realized", "commission", "net ex funding", "maker ratio", "funding ctx"],
            symbol_rows,
        ))
        print()

    if result["by_month"]:
        month_rows = []
        for key, bucket in result["by_month"].items():
            month_rows.append([
                key,
                str(bucket["orders"]),
                fmt_num(bucket["realized_pnl"]),
                fmt_num(bucket["commission"]),
                fmt_num(bucket["net_pnl_ex_funding"]),
            ])
        print("By Month")
        print(render_table(["month", "orders", "realized", "commission", "net ex funding"], month_rows))
        print()

    print(f"Top {top_orders} Winners")
    winner_rows = []
    for item in result["top_winners"][:top_orders]:
        winner_rows.append([
            item["started_at"][:16],
            item["symbol"],
            item["side"],
            str(item["fills"]),
            fmt_num(item["avg_price"]),
            fmt_num(item["qty"]),
            fmt_num(item["net_pnl_ex_funding"]),
        ])
    if winner_rows:
        print(render_table(["time", "symbol", "side", "fills", "avg price", "qty", "net ex funding"], winner_rows))
    else:
        print("(none)")
    print()

    print(f"Top {top_orders} Losers")
    loser_rows = []
    for item in result["top_losers"][:top_orders]:
        loser_rows.append([
            item["started_at"][:16],
            item["symbol"],
            item["side"],
            str(item["fills"]),
            fmt_num(item["avg_price"]),
            fmt_num(item["qty"]),
            fmt_num(item["net_pnl_ex_funding"]),
        ])
    if loser_rows:
        print(render_table(["time", "symbol", "side", "fills", "avg price", "qty", "net ex funding"], loser_rows))
    else:
        print("(none)")


async def _main() -> int:
    args = parse_args()
    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    result = await analyze_manual_mainnet(
        days=args.days,
        symbols_filter=symbols,
        start_time_str=args.start_time,
        end_time_str=args.end_time,
    )
    print_analysis(result, top_orders=args.top_orders)
    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print()
        print(f"Saved JSON to {output_path}")
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
