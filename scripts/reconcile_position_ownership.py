"""Classify an open mainnet position without exposing account credentials."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from config.settings import Settings
from src.gridbot.binance.client import BinanceFuturesClient


def classify_ownership(
    *,
    has_position: bool,
    active_run: dict[str, Any] | None,
    bot_open_orders: list[dict[str, Any]],
    bot_recent_orders: list[dict[str, Any]],
    matched_bot_order_ids: list[int],
    non_bot_open_orders: list[dict[str, Any]],
) -> str:
    if not has_position:
        return "FLAT"
    bot_evidence = bool(bot_open_orders or matched_bot_order_ids)
    external_evidence = bool(non_bot_open_orders)
    if bot_evidence and external_evidence:
        return "AMBIGUOUS"
    if bot_evidence:
        return "BOT_MATCHED"
    if active_run or bot_recent_orders:
        return "AMBIGUOUS"
    return "MANUAL_OR_EXTERNAL"


def _safe_order(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_id": int(order.get("orderId") or 0),
        "client_order_id": str(order.get("clientOrderId") or ""),
        "side": str(order.get("side") or ""),
        "type": str(order.get("type") or ""),
        "status": str(order.get("status") or ""),
        "price": str(order.get("price") or ""),
        "orig_qty": str(order.get("origQty") or ""),
        "executed_qty": str(order.get("executedQty") or ""),
        "reduce_only": bool(order.get("reduceOnly", False)),
        "update_time_ms": int(order.get("updateTime") or order.get("time") or 0),
    }


def _active_run(db_path: Path) -> dict[str, Any] | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT run_id, symbol, strategy_label, status, armed_at_ms, updated_at_ms "
            "FROM mainnet_runs WHERE status IN ('ARMED','ENTRY_PENDING','RUNNING','CLOSING') "
            "ORDER BY armed_at_ms DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


async def reconcile(args: argparse.Namespace) -> dict[str, Any]:
    settings = Settings()
    client_settings = settings
    if args.environment == "mainnet":
        client_settings = settings.model_copy(
            update={
                "binance_api_key": settings.mainnet_api_key,
                "binance_api_secret": settings.mainnet_api_secret,
                "binance_testnet": False,
            }
        )
    client = BinanceFuturesClient(client_settings)
    await client.connect()
    try:
        position = await client.get_position(args.symbol)
        open_orders = await client.get_open_orders(args.symbol)
        since_ms = int(time.time() * 1000) - args.lookback_hours * 3_600_000
        recent_orders = await client.get_all_orders(args.symbol, start_time=since_ms, limit=1000)
        trades = await client.get_user_trades(args.symbol, start_time=since_ms, limit=1000)
    finally:
        await client.close()

    prefix = args.client_order_prefix
    bot_open = [_safe_order(order) for order in open_orders if str(order.get("clientOrderId") or "").startswith(prefix)]
    external_open = [_safe_order(order) for order in open_orders if not str(order.get("clientOrderId") or "").startswith(prefix)]
    bot_recent = [_safe_order(order) for order in recent_orders if str(order.get("clientOrderId") or "").startswith(prefix)]
    active_run = _active_run(args.db)
    trade_order_ids = {int(trade.order_id) for trade in trades}
    matched_bot_order_ids = sorted(
        {order["order_id"] for order in bot_recent if order["order_id"] in trade_order_ids}
    )
    ownership = classify_ownership(
        has_position=position is not None,
        active_run=active_run,
        bot_open_orders=bot_open,
        bot_recent_orders=bot_recent,
        matched_bot_order_ids=matched_bot_order_ids,
        non_bot_open_orders=external_open,
    )
    return {
        "schema": "position_ownership_v1",
        "account_environment": args.environment,
        "symbol": args.symbol,
        "ownership": ownership,
        "active_run": active_run,
        "position": None
        if position is None
        else {
            "side": position.position_direction,
            "qty": abs(position.position_amt),
            "entry_price": position.entry_price,
            "mark_price": position.mark_price,
            "unrealized_pnl": position.unrealized_pnl,
            "leverage": position.leverage,
            "margin_type": position.margin_type,
        },
        "bot_open_orders": bot_open,
        "non_bot_open_orders": external_open,
        "bot_recent_order_count": len(bot_recent),
        "recent_trade_count": len(trades),
        "matched_bot_order_ids": matched_bot_order_ids,
        "evidence_complete": ownership in {"FLAT", "BOT_MATCHED"},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="ETHUSDC")
    parser.add_argument("--environment", choices=("mainnet", "testnet"), default="mainnet")
    parser.add_argument("--db", type=Path, default=Path("testnet/data/gridbot_testnet.db"))
    parser.add_argument("--client-order-prefix", default="cry3mn")
    parser.add_argument("--lookback-hours", type=int, default=168)
    return parser.parse_args()


def main() -> None:
    print(json.dumps(asyncio.run(reconcile(parse_args())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
