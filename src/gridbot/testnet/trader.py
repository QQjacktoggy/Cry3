"""Small guarded Binance Futures testnet order executor."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from config.settings import Settings
from src.gridbot.binance.client import BinanceFuturesClient
from src.gridbot.binance.models import PositionInfo


@dataclass(frozen=True)
class TestnetOrderResult:
    symbol: str
    side: str
    quantity: str
    notional_usdc: float
    leverage: int
    reduce_only: bool
    order: dict


class TestnetTrader:
    """Small guarded Binance Futures testnet order executor."""

    def __init__(self, settings: Settings, client: BinanceFuturesClient) -> None:
        self._settings = settings
        self._client = client

    async def open_position(
        self,
        symbol: str,
        direction: str,
        notional_usdc: float | None = None,
        leverage: int | None = None,
    ) -> TestnetOrderResult:
        self._assert_can_open()
        symbol = symbol.upper()
        side = _side_for_direction(direction)
        notional = self._bounded_notional(notional_usdc)
        leverage = self._bounded_leverage(leverage)
        qty = await self._quantity_for_notional(symbol, notional)

        await self._client.set_leverage(symbol, leverage)
        order = await self._client.create_market_order(
            symbol=symbol,
            side=side,
            quantity=qty,
            reduce_only=False,
            client_order_id=_client_order_id("cry3tn"),
        )
        return TestnetOrderResult(symbol, side, qty, notional, leverage, False, order)

    async def place_entry_limit(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        notional_usdc: float | None = None,
        leverage: int | None = None,
    ) -> TestnetOrderResult:
        self._assert_can_open()
        symbol = symbol.upper()
        side = _side_for_direction(direction)
        notional = self._bounded_notional(notional_usdc)
        leverage = self._bounded_leverage(leverage)
        qty = await self._quantity_for_notional_at_price(symbol, notional, entry_price)

        await self._client.set_leverage(symbol, leverage)
        order = await self._client.create_limit_order(
            symbol=symbol,
            side=side,
            quantity=qty,
            price=entry_price,
            reduce_only=False,
            client_order_id=_client_order_id("cry3en"),
        )
        return TestnetOrderResult(symbol, side, qty, notional, leverage, False, order)

    async def close_position(self, symbol: str) -> TestnetOrderResult | None:
        self._assert_testnet()
        symbol = symbol.upper()
        position = await self._client.get_position(symbol)
        if not position or position.position_amt == 0:
            return None

        side = "SELL" if position.position_amt > 0 else "BUY"
        qty = await self._quantity_for_position(symbol, position)
        order = await self._client.create_market_order(
            symbol=symbol,
            side=side,
            quantity=qty,
            reduce_only=True,
            client_order_id=_client_order_id("cry3close"),
        )
        return TestnetOrderResult(
            symbol=symbol,
            side=side,
            quantity=qty,
            notional_usdc=abs(position.position_amt) * position.mark_price,
            leverage=position.leverage,
            reduce_only=True,
            order=order,
        )

    def _assert_can_open(self) -> None:
        self._assert_testnet()
        if self._settings.trading_mode != "testnet_live":
            raise RuntimeError("TRADING_MODE must be testnet_live to open testnet orders.")

    def _assert_testnet(self) -> None:
        if not self._settings.binance_testnet:
            raise RuntimeError("Refusing to place orders unless BINANCE_TESTNET=true.")

    def _bounded_notional(self, requested: float | None) -> float:
        notional = self._settings.testnet_order_notional_usdc if requested is None else float(requested)
        if notional <= 0:
            raise ValueError("Order notional must be positive.")
        return min(notional, self._settings.testnet_max_order_notional_usdc)

    def _bounded_leverage(self, requested: int | None = None) -> int:
        leverage = self._settings.testnet_order_leverage if requested is None else requested
        leverage = max(1, int(leverage))
        return min(leverage, int(self._settings.max_effective_leverage))

    async def _quantity_for_notional(self, symbol: str, notional: float) -> str:
        mark = await self._client.get_mark_price(symbol)
        price = float(mark.get("markPrice") or 0)
        if price <= 0:
            raise RuntimeError(f"Invalid mark price for {symbol}.")
        return await self._quantity_for_notional_at_price(symbol, notional, price)

    async def _quantity_for_notional_at_price(self, symbol: str, notional: float, price: float) -> str:
        if price <= 0:
            raise ValueError("Entry price must be positive.")
        step_size = await self._quantity_step(symbol)
        return _format_step_quantity(notional / price, step_size)

    async def _quantity_for_position(self, symbol: str, position: PositionInfo) -> str:
        step_size = await self._quantity_step(symbol)
        qty = Decimal(str(abs(position.position_amt)))
        step = Decimal(str(step_size))
        if qty <= 0 or step <= 0:
            raise ValueError("position quantity and step_size must be positive.")
        rounded = qty.quantize(step, rounding=ROUND_HALF_UP)
        if rounded <= 0:
            raise ValueError("Position quantity is below symbol minimum step size.")
        decimals = max(0, min(12, _decimal_places(step_size)))
        return f"{rounded:.{decimals}f}".rstrip("0").rstrip(".")

    async def _quantity_step(self, symbol: str) -> float:
        info = await self._client.get_exchange_info()
        for item in info.get("symbols", []):
            if item.get("symbol") != symbol:
                continue
            for flt in item.get("filters", []):
                if flt.get("filterType") == "LOT_SIZE":
                    step_size = float(flt.get("stepSize", 0))
                    if step_size > 0:
                        return step_size
        raise RuntimeError(f"LOT_SIZE stepSize not found for {symbol}.")


def _side_for_direction(direction: str) -> str:
    normalized = direction.strip().lower()
    if normalized in {"long", "buy"}:
        return "BUY"
    if normalized in {"short", "sell"}:
        return "SELL"
    raise ValueError("direction must be long/buy or short/sell.")


def _format_step_quantity(quantity: float, step_size: float) -> str:
    if quantity <= 0 or step_size <= 0:
        raise ValueError("quantity and step_size must be positive.")
    steps = math.floor(quantity / step_size)
    rounded = steps * step_size
    if rounded <= 0:
        raise ValueError("Quantity is below symbol minimum step size.")
    decimals = max(0, min(12, _decimal_places(step_size)))
    return f"{rounded:.{decimals}f}".rstrip("0").rstrip(".")


def _decimal_places(value: float) -> int:
    text = f"{value:.12f}".rstrip("0")
    return len(text.split(".", 1)[1]) if "." in text else 0


def _client_order_id(prefix: str) -> str:
    return f"{prefix}_{int(time.time() * 1000)}"
