import pytest
from binance.exceptions import BinanceAPIException

from config.settings import Settings
from src.gridbot.binance.client import BinanceFuturesClient


class FakeAsyncBinanceClient:
    def __init__(self):
        self.order_params = None
        self.cancel_params = None

    async def futures_exchange_info(self):
        return {
            "symbols": [
                {
                    "symbol": "ETHUSDC",
                    "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01"}],
                }
            ]
        }

    async def futures_create_order(self, **params):
        self.order_params = params
        return {"algoId": 123, **params}

    async def futures_cancel_algo_order(self, **params):
        self.cancel_params = params
        return {"status": "CANCELED", **params}


@pytest.mark.asyncio
async def test_conditional_close_order_uses_quantity_reduce_only_and_tick_price():
    client = BinanceFuturesClient(Settings(binance_api_key="key", binance_api_secret="secret"))
    fake = FakeAsyncBinanceClient()
    client._client = fake

    result = await client.create_conditional_close_order(
        symbol="ETHUSDC",
        side="SELL",
        order_type="STOP_MARKET",
        trigger_price=2070.12345,
        quantity="0.069",
        client_algo_id="cry3sl_test",
    )

    assert result["quantity"] == "0.069"
    assert result["reduceOnly"] == "true"
    assert result["triggerPrice"] == "2070.12"
    assert result["clientAlgoId"] == "cry3sl_test"
    assert "newClientOrderId" not in fake.order_params
    assert "closePosition" not in fake.order_params
    assert "stopPrice" not in fake.order_params


@pytest.mark.asyncio
async def test_cancel_algo_order_treats_unknown_order_as_already_closed():
    class UnknownOrderClient(FakeAsyncBinanceClient):
        async def futures_cancel_algo_order(self, **params):
            self.cancel_params = params
            response = type("Response", (), {"text": "", "request": None})()
            raise BinanceAPIException(response, 400, '{"code": -2011, "msg": "Unknown order sent."}')

    client = BinanceFuturesClient(Settings(binance_api_key="key", binance_api_secret="secret"))
    fake = UnknownOrderClient()
    client._client = fake

    result = await client.cancel_algo_order("ETHUSDC", algo_id=123)

    assert fake.cancel_params == {"symbol": "ETHUSDC", "algoId": 123}
    assert result["status"] == "ALREADY_CLOSED"


@pytest.mark.asyncio
async def test_reduce_only_limit_order_uses_tick_price_and_client_id():
    client = BinanceFuturesClient(Settings(binance_api_key="key", binance_api_secret="secret"))
    fake = FakeAsyncBinanceClient()
    client._client = fake

    result = await client.create_reduce_only_limit_order(
        symbol="ETHUSDC",
        side="SELL",
        quantity="0.069",
        price=2135.116,
        client_order_id="cry3tp_test",
    )

    assert result["type"] == "LIMIT"
    assert result["timeInForce"] == "GTC"
    assert result["quantity"] == "0.069"
    assert result["price"] == "2135.12"
    assert result["reduceOnly"] == "true"
    assert result["newClientOrderId"] == "cry3tp_test"
