"""Execute an isolated testnet round trip and emit in-memory fill_v1 evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Any

from config.settings import Settings
from src.gridbot.binance.client import BinanceFuturesClient
from src.gridbot.mainnet.fill_telemetry import emit_fill_v1_events


class MemoryRepo:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def log_event(self, run_id: str, event_type: str, details: dict[str, Any]) -> None:
        self.events.append({"run_id": run_id, "event_type": event_type, "details": details})

    async def get_events_by_types(self, run_id: str, event_types, limit: int = 5000):
        allowed = set(event_types)
        return [
            event for event in self.events
            if event["run_id"] == run_id and event["event_type"] in allowed
        ][-limit:]


async def _wait_position(client: BinanceFuturesClient, symbol: str, *, present: bool):
    for _ in range(30):
        position = await client.get_position(symbol)
        if (position is not None) == present:
            return position
        await asyncio.sleep(0.5)
    raise RuntimeError(f"position state did not become present={present}")


async def run_probe(args: argparse.Namespace) -> dict[str, Any]:
    if not args.execute:
        raise RuntimeError("refusing to trade without --execute")
    settings = Settings()
    if not settings.binance_testnet:
        raise RuntimeError("refusing to run unless BINANCE_TESTNET=true")
    client = BinanceFuturesClient(settings)
    await client.connect()
    run_id = f"tprobe_{int(time.time())}"
    armed_at_ms = int(time.time() * 1000) - 1000
    entry_id = f"{run_id}_entry"
    close_id = f"{run_id}_close"
    repo = MemoryRepo()
    opened_by_probe = False
    try:
        if await client.get_position(args.symbol) is not None:
            raise RuntimeError(f"refusing to touch non-flat testnet symbol {args.symbol}")
        if await client.get_open_orders(args.symbol):
            raise RuntimeError(f"refusing to touch symbol with open orders {args.symbol}")
        quantity = await client.format_quantity(args.symbol, args.quantity)
        if float(quantity) <= 0:
            raise RuntimeError("formatted quantity is zero")
        await client.create_market_order(
            args.symbol, "BUY", quantity, client_order_id=entry_id
        )
        position = await _wait_position(client, args.symbol, present=True)
        close_qty = await client.format_quantity(args.symbol, abs(position.position_amt))
        await client.create_market_order(
            args.symbol,
            "SELL",
            close_qty,
            reduce_only=True,
            client_order_id=close_id,
        )
        await _wait_position(client, args.symbol, present=False)
        await asyncio.sleep(1.0)
        emitted_first = await emit_fill_v1_events(
            repo=repo,
            client=client,
            trade_repo=None,
            run={"run_id": run_id, "symbol": args.symbol, "armed_at_ms": armed_at_ms},
        )
        emitted_second = await emit_fill_v1_events(
            repo=repo,
            client=client,
            trade_repo=None,
            run={"run_id": run_id, "symbol": args.symbol, "armed_at_ms": armed_at_ms},
        )
        fills = [event["details"] for event in repo.events if event["event_type"] == "fill_v1"]
        roles = sorted({str(fill.get("role")) for fill in fills})
        return {
            "schema": "fill_v1_testnet_probe_v1",
            "environment": "testnet",
            "run_id": run_id,
            "symbol": args.symbol,
            "quantity": quantity,
            "emitted_first": emitted_first,
            "emitted_second": emitted_second,
            "idempotent": emitted_second == 0,
            "roles": roles,
            "fill_count": len(fills),
            "fills": fills,
            "final_position": "FLAT",
        }
    finally:
        residual = await client.get_position(args.symbol)
        if opened_by_probe and residual is not None:
            close_side = "SELL" if residual.position_amt > 0 else "BUY"
            close_qty = await client.format_quantity(args.symbol, abs(residual.position_amt))
            await client.create_market_order(
                args.symbol, close_side, close_qty, reduce_only=True,
                client_order_id=f"{run_id}_cleanup",
            )
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="BTCUSDC")
    parser.add_argument("--quantity", type=float, default=0.001)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run_probe(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
