import pytest

from config.settings import Settings
from src.gridbot.binance.models import PositionInfo
from src.gridbot.testnet.trader import TestnetTrader, _format_step_quantity


class FakeClient:
    def __init__(self):
        self.leverage_calls = []
        self.orders = []
        self.position = None

    async def get_mark_price(self, symbol):
        return {"markPrice": "2000"}

    async def get_exchange_info(self):
        return {
            "symbols": [
                {
                    "symbol": "ETHUSDC",
                    "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.001"}],
                }
            ]
        }

    async def set_leverage(self, symbol, leverage):
        self.leverage_calls.append((symbol, leverage))
        return {"leverage": leverage}

    async def create_market_order(self, **kwargs):
        self.orders.append(kwargs)
        return {"orderId": 123, "status": "NEW", **kwargs}

    async def get_position(self, symbol):
        return self.position


def _settings(**kwargs):
    data = {
        "binance_api_key": "key",
        "binance_api_secret": "secret",
        "binance_testnet": True,
        "trading_symbols": "ETHUSDC",
        "trading_mode": "testnet_live",
        "testnet_order_notional_usdc": 10,
        "testnet_order_leverage": 5,
        "testnet_max_order_notional_usdc": 25,
        "max_effective_leverage": 70,
    }
    data.update(kwargs)
    return Settings(**data)


def test_format_step_quantity_rounds_down():
    assert _format_step_quantity(0.0059, 0.001) == "0.005"
    assert _format_step_quantity(1.234, 0.01) == "1.23"


@pytest.mark.asyncio
async def test_open_position_requires_testnet_live():
    trader = TestnetTrader(_settings(trading_mode="signal_only"), FakeClient())

    with pytest.raises(RuntimeError, match="testnet_live"):
        await trader.open_position("ETHUSDC", "long")


@pytest.mark.asyncio
async def test_open_position_places_bounded_market_order():
    client = FakeClient()
    trader = TestnetTrader(_settings(testnet_max_order_notional_usdc=12), client)

    result = await trader.open_position("ETHUSDC", "long", 99)

    assert result.side == "BUY"
    assert result.quantity == "0.006"
    assert result.notional_usdc == 12
    assert client.leverage_calls == [("ETHUSDC", 5)]
    assert client.orders[0]["reduce_only"] is False


@pytest.mark.asyncio
async def test_close_position_uses_reduce_only_opposite_side():
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.0123,
        entry_price=2000,
        mark_price=2010,
        unrealized_pnl=0.1,
        liquidation_price=1500,
        leverage=5,
        margin_type="isolated",
    )
    trader = TestnetTrader(_settings(trading_mode="signal_only"), client)

    result = await trader.close_position("ETHUSDC")

    assert result.side == "SELL"
    assert result.quantity == "0.012"
    assert client.orders[0]["reduce_only"] is True


@pytest.mark.asyncio
async def test_close_position_returns_none_when_flat():
    trader = TestnetTrader(_settings(), FakeClient())

    assert await trader.close_position("ETHUSDC") is None
