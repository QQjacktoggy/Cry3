import json
import asyncio

from config.settings import Settings
from src.gridbot.binance.client import BinanceFuturesClient


def _client_order_id(order: dict) -> str:
    return str(order.get("newClientOrderId") or order.get("clientOrderId") or "")


def _position_field(position, name: str, default: float = 0.0) -> float:
    if position is None:
        return default
    if hasattr(position, name):
        return float(getattr(position, name))
    if isinstance(position, dict):
        return float(position.get(name, default))
    return default


async def _main() -> None:
    settings = Settings()
    symbol = settings.trading_symbols.split(",")[0].strip()
    client = BinanceFuturesClient(settings)
    await client.connect()
    try:
        position = await client.get_position(symbol)
        open_orders = await client.get_open_orders(symbol)
    finally:
        await client.close()

    position_amt = _position_field(position, "position_amt", _position_field(position, "positionAmt", 0.0))
    entry_price = _position_field(position, "entry_price", _position_field(position, "entryPrice", 0.0))
    mark_price = _position_field(position, "mark_price", _position_field(position, "markPrice", 0.0))
    unrealized_pnl = _position_field(
        position,
        "unrealized_pnl",
        _position_field(position, "unRealizedProfit", 0.0),
    )
    leverage = _position_field(position, "leverage", 0.0)
    notional_est = abs(position_amt) * entry_price
    margin_est = notional_est / leverage if leverage else 0.0
    summary = {
        "symbol": symbol,
        "has_position": bool(position and abs(position_amt) > 0),
        "position_amt": position_amt,
        "entry_price": entry_price,
        "mark_price": mark_price,
        "leverage": leverage,
        "notional_est": round(notional_est, 6),
        "margin_est": round(margin_est, 6),
        "unrealized_pnl": unrealized_pnl,
        "open_orders": len(open_orders),
        "entry_orders": len([order for order in open_orders if _client_order_id(order).startswith("cry3en_")]),
        "tp_orders": len([order for order in open_orders if _client_order_id(order).startswith("cry3tp_")]),
        "sl_orders": len([order for order in open_orders if _client_order_id(order).startswith("cry3sl_")]),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
