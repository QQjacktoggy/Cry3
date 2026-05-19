import pytest

from config.settings import Settings
from src.gridbot.binance.models import PositionInfo
from src.gridbot.strategy.long_pullback import SignalPlan
from src.gridbot.testnet.auto_trader import ActivePlan, TestnetAutoTrader


class FakeClient:
    def __init__(self):
        self.orders = []
        self.leverage_calls = []
        self.position = None

    async def get_position(self, symbol):
        return self.position

    async def get_income_history(self, **kwargs):
        return []

    async def get_klines(self, symbol, interval, limit):
        rows = []
        for i in range(80):
            price = 2000 + i
            rows.append([
                i * 300000,
                str(price),
                str(price + 10),
                str(price - 10),
                str(price + 4),
                "100",
                i * 300000 + 299999,
                "200000",
            ])
        return rows

    async def get_mark_price(self, symbol):
        return {"markPrice": "2100"}

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
        return {"orderId": 321, "status": "NEW", **kwargs}


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)


class FakeTelegramApp:
    def __init__(self):
        self.bot = FakeBot()


def _settings(**kwargs):
    data = {
        "binance_api_key": "key",
        "binance_api_secret": "secret",
        "binance_testnet": True,
        "trading_symbols": "ETHUSDC",
        "trading_mode": "testnet_live",
        "telegram_chat_id": "123",
        "testnet_equity_usdc": 150,
        "testnet_max_order_notional_usdc": 150,
        "max_effective_leverage": 20,
    }
    data.update(kwargs)
    return Settings(**data)


@pytest.mark.asyncio
async def test_auto_trader_opens_and_notifies(monkeypatch):
    client = FakeClient()
    telegram = FakeTelegramApp()
    trader = TestnetAutoTrader(_settings(), client, telegram)

    def fake_signal(candles, config):
        return SignalPlan(
            action="PLAN_LONG",
            confidence=82,
            score=82,
            symbol="ETHUSDC",
            price=2100,
            rsi=55,
            atr=20,
            support=2050,
            vwap=2080,
            entries=[2100],
            stop_loss=2070,
            take_profits=[2130],
            planned_notional_usdc=300,
            leverage_cap=10,
            reasons=["test signal"],
        )

    monkeypatch.setattr("src.gridbot.testnet.auto_trader.generate_ntrend_signal", fake_signal)

    await trader.run_cycle()

    assert client.leverage_calls == [("ETHUSDC", 10)]
    assert client.orders[0]["side"] == "BUY"
    assert client.orders[0]["reduce_only"] is False
    assert telegram.bot.messages
    assert "Testnet auto entry" in telegram.bot.messages[0]["text"]


@pytest.mark.asyncio
async def test_auto_trader_closes_and_notifies_on_take_profit():
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.05,
        entry_price=2100,
        mark_price=2140,
        unrealized_pnl=2.0,
        liquidation_price=1800,
        leverage=10,
        margin_type="isolated",
    )
    telegram = FakeTelegramApp()
    trader = TestnetAutoTrader(_settings(), client, telegram)
    trader._plans["ETHUSDC"] = ActivePlan(
        symbol="ETHUSDC",
        opened_at_ms=1,
        entry_price=2100,
        stop_loss=2070,
        take_profit=2130,
        score=80,
        reasons=["test"],
    )

    await trader.run_cycle()

    assert client.orders[0]["side"] == "SELL"
    assert client.orders[0]["reduce_only"] is True
    assert "Testnet auto exit" in telegram.bot.messages[0]["text"]
