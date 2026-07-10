"""Exchange-fill telemetry for mainnet one-run evidence.

This module is intentionally strategy-neutral. It records what Binance filled;
it does not alter order placement, sizing, exits, recovery, or risk decisions.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _role(client_order_id: str) -> str:
    cid = client_order_id.lower()
    if not cid:
        return "unknown_exchange_fill"
    if "tp1" in cid:
        return "partial_exit"
    if "tp2" in cid:
        return "mid_exit"
    if "tp3" in cid or cid.endswith("_tp"):
        return "final_exit"
    if "sl" in cid:
        return "stop_loss_exit"
    if "dca" in cid or "recovery" in cid:
        return "recovery_entry"
    if "entry" in cid or cid.endswith("_open"):
        return "entry"
    if "close" in cid or "trail" in cid or "be_" in cid:
        return "exit"
    return "unknown_exchange_fill"


def _api_record(trade: Any) -> dict[str, Any]:
    return {
        "trade_id": int(trade.trade_id),
        "order_id": int(trade.order_id),
        "symbol": str(trade.symbol),
        "side": str(trade.side),
        "position_side": str(trade.position_side),
        "price": float(trade.price),
        "qty": float(trade.qty),
        "quote_qty": float(trade.quote_qty),
        "realized_pnl": float(trade.realized_pnl),
        "commission": float(trade.commission),
        "commission_asset": str(trade.commission_asset),
        "time_ms": int(trade.time_ms),
        "is_maker": bool(trade.is_maker),
    }


def _db_record(trade: dict[str, Any]) -> dict[str, Any]:
    return {
        "trade_id": int(trade.get("trade_id") or 0),
        "order_id": int(trade.get("order_id") or 0),
        "symbol": str(trade.get("symbol") or ""),
        "side": str(trade.get("side") or ""),
        "position_side": str(trade.get("position_side") or ""),
        "price": float(trade.get("price") or 0.0),
        "qty": float(trade.get("qty") or 0.0),
        "quote_qty": float(trade.get("quote_qty") or 0.0),
        "realized_pnl": float(trade.get("realized_pnl") or 0.0),
        "commission": float(trade.get("commission") or 0.0),
        "commission_asset": str(trade.get("commission_asset") or ""),
        "time_ms": int(trade.get("time_ms") or 0),
        "is_maker": bool(trade.get("is_maker", False)),
    }


def _record_key(record: dict[str, Any]) -> str:
    """Build a stable key while preserving the deployed trade/order format."""
    trade_id = int(record.get("trade_id") or 0)
    order_id = int(record.get("order_id") or 0)
    if trade_id:
        return f"{trade_id}:{order_id}"

    # Some persisted/algo fills lack trade or order identity. Keep every partial
    # fill distinct using immutable execution fields, not inferred strategy state.
    identity = {
        "order_id": order_id,
        "symbol": str(record.get("symbol") or ""),
        "side": str(record.get("side") or ""),
        "position_side": str(record.get("position_side") or ""),
        "price": float(record.get("price") or 0.0),
        "qty": float(record.get("qty") or 0.0),
        "quote_qty": float(record.get("quote_qty") or 0.0),
        "commission": float(record.get("commission") or 0.0),
        "commission_asset": str(record.get("commission_asset") or ""),
        "time_ms": int(record.get("time_ms") or 0),
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return f"fallback:{digest}"


def _merge(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for record in records:
        key = _record_key(record)
        current = merged.setdefault(key, dict(record))
        current["qty"] = max(current["qty"], record["qty"])
        current["quote_qty"] = max(current["quote_qty"], record["quote_qty"])
        current["time_ms"] = max(current["time_ms"], record["time_ms"])
        current["is_maker"] = current["is_maker"] or record["is_maker"]
        for key_name in ("symbol", "side", "position_side", "commission_asset"):
            if not current[key_name] and record[key_name]:
                current[key_name] = record[key_name]
        if not current["price"] and record["price"]:
            current["price"] = record["price"]
        if not current["realized_pnl"] and record["realized_pnl"]:
            current["realized_pnl"] = record["realized_pnl"]
        if not current["commission"] and record["commission"]:
            current["commission"] = record["commission"]
    return sorted(merged.values(), key=lambda item: (item["time_ms"], item["trade_id"]))


def _event_details(event: Any) -> dict[str, Any]:
    if not isinstance(event, dict):
        return {}
    details = event.get("details")
    if isinstance(details, dict):
        return details
    raw = event.get("details_json")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


async def _existing_fill_keys(repo: Any, run_id: str) -> set[str]:
    """Best-effort rehydration makes repeated calls and restarts idempotent."""
    get_by_types = getattr(repo, "get_events_by_types", None)
    get_events = getattr(repo, "get_events", None)
    try:
        if callable(get_by_types):
            events = await get_by_types(run_id, ("fill_v1",), limit=5000)
        elif callable(get_events):
            events = await get_events(run_id, limit=5000)
            events = [event for event in events if event.get("event_type") == "fill_v1"]
        else:
            return set()
    except Exception:  # noqa: BLE001 - telemetry must not affect trading behavior
        return set()

    keys: set[str] = set()
    for event in events or []:
        details = _event_details(event)
        fill_key = str(details.get("fill_key") or "")
        if fill_key:
            keys.add(fill_key)
        elif details:
            keys.add(_record_key(details))
    return keys


async def _order_client_ids(client: Any, symbol: str, start_time: int) -> dict[int, str]:
    try:
        orders = await client.get_all_orders(symbol, start_time=start_time, limit=1000)
    except Exception:  # noqa: BLE001 - fills remain useful without order metadata
        return {}
    return {
        int(order.get("orderId") or 0): str(order.get("clientOrderId") or "")
        for order in orders or []
        if isinstance(order, dict) and order.get("orderId") is not None
    }


async def emit_fill_v1_events(*, repo, client, trade_repo, run: dict) -> int:
    """Incrementally emit one immutable ``fill_v1`` event per exchange trade."""
    start_time = int(run.get("armed_at_ms") or 0)
    if not start_time:
        return 0

    try:
        api_trades = await client.get_user_trades(
            run["symbol"], start_time=start_time, limit=1000
        )
    except Exception:  # noqa: BLE001 - persisted trade fallback may still be available
        api_trades = []
    records = [_api_record(trade) for trade in api_trades or []]
    if trade_repo:
        try:
            db_trades = await trade_repo.get_trades(
                run["symbol"], since_ms=start_time, grid_only=False, limit=1000
            )
        except Exception:  # noqa: BLE001 - API records are independently sufficient
            db_trades = []
        records.extend(_db_record(trade) for trade in db_trades or [])

    fills = _merge(records)
    if not fills:
        return 0
    existing_keys = await _existing_fill_keys(repo, str(run["run_id"]))
    client_ids = await _order_client_ids(client, run["symbol"], start_time)

    emitted = 0
    for fill in fills:
        fill_key = _record_key(fill)
        if fill_key in existing_keys:
            continue
        order_id = fill["order_id"]
        client_order_id = client_ids.get(order_id, "")
        await repo.log_event(
            run["run_id"],
            "fill_v1",
            {
                "schema": "fill_v1",
                "run_id": run["run_id"],
                "fill_key": fill_key,
                "trade_id": fill["trade_id"],
                "order_id": order_id,
                "client_order_id": client_order_id,
                "symbol": fill["symbol"] or run["symbol"],
                "side": fill["side"],
                "position_side": fill["position_side"],
                "price": fill["price"],
                "qty": fill["qty"],
                "quote_qty": fill["quote_qty"],
                "realized_pnl": fill["realized_pnl"],
                "commission": fill["commission"],
                "commission_asset": fill["commission_asset"],
                "time_ms": fill["time_ms"],
                "liquidity": "maker" if fill["is_maker"] else "taker",
                "role": _role(client_order_id),
                "source": "binance_user_trades",
            },
        )
        existing_keys.add(fill_key)
        emitted += 1
    return emitted
