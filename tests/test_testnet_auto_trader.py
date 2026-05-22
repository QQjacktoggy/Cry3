import pytest
from datetime import datetime, timezone

from config.settings import Settings
from src.gridbot.binance.models import IncomeRecord, PositionInfo
from src.gridbot.strategy.long_pullback import SignalPlan
from src.gridbot.testnet.auto_trader import ActivePlan, TestnetAutoTrader
from src.gridbot.testnet.trader import TestnetOrderResult


class FakeClient:
    def __init__(self):
        self.orders = []
        self.leverage_calls = []
        self.position = None
        self.open_orders = []
        self.open_algo_orders = []
        self.all_orders = []
        self.income_history = []
        self.cancelled_orders = []
        self.conditional_orders = []
        self.limit_orders = []
        self.mark_price = 2100

    async def get_position(self, symbol):
        return self.position

    async def get_income_history(self, **kwargs):
        income_type = kwargs.get("income_type")
        return [item for item in self.income_history if item.income_type == income_type]

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
        return {"markPrice": str(self.mark_price)}

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

    async def get_open_orders(self, symbol):
        return list(self.open_orders)

    async def get_all_orders(self, symbol, start_time=None, limit=1000):
        return list(self.all_orders)

    async def cancel_order(self, symbol, order_id):
        self.cancelled_orders.append((symbol, order_id))
        self.open_orders = [o for o in self.open_orders if o.get("orderId") != order_id]
        return {"symbol": symbol, "orderId": order_id, "status": "CANCELED"}

    async def get_open_algo_orders(self, symbol):
        return list(self.open_algo_orders)

    async def cancel_algo_order(self, symbol, algo_id=None, client_algo_id=None):
        self.cancelled_orders.append((symbol, algo_id or client_algo_id))
        self.open_algo_orders = [
            o for o in self.open_algo_orders
            if o.get("algoId") != algo_id
            and o.get("orderId") != algo_id
            and o.get("clientAlgoId") != client_algo_id
            and o.get("clientOrderId") != client_algo_id
        ]
        return {"symbol": symbol, "algoId": algo_id, "clientAlgoId": client_algo_id, "status": "CANCELED"}

    async def create_conditional_close_order(self, **kwargs):
        self.conditional_orders.append(kwargs)
        order = {"algoId": 500 + len(self.conditional_orders), "status": "NEW", **kwargs}
        if "client_algo_id" in kwargs:
            order["clientAlgoId"] = kwargs["client_algo_id"]
            order["clientOrderId"] = kwargs["client_algo_id"]
        self.open_algo_orders.append(order)
        return order

    async def create_reduce_only_limit_order(self, **kwargs):
        self.limit_orders.append(kwargs)
        order = {"orderId": 700 + len(self.limit_orders), "status": "NEW", "type": "LIMIT", **kwargs}
        if "client_order_id" in kwargs:
            order["clientOrderId"] = kwargs["client_order_id"]
        self.open_orders.append(order)
        return order


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

    def fake_signal(candles, base, day_pnl=0.0):
        class FakeDecision:
            signal = SignalPlan(
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
            strategy = "orb_long"
            regime = "trend_up"
            risk_mode = "aggressive"
            market_playbook = "breakout"
            allocator_state = "active"
            allocator_profile = "trend_aggressive"
            allocator_scale = 3.5
            max_holding_bars = 24
        return FakeDecision()

    monkeypatch.setattr("src.gridbot.testnet.auto_trader.generate_router_allocator_v13_trend350_live_decision", fake_signal)

    await trader.run_cycle()

    assert client.leverage_calls == [("ETHUSDC", 10)]
    assert client.orders[0]["side"] == "BUY"
    assert client.orders[0]["reduce_only"] is False
    assert telegram.bot.messages
    assert "Testnet 自動開倉" in telegram.bot.messages[0]["text"]
    assert "策略：<b>orb_long</b>" in telegram.bot.messages[0]["text"]
    assert "方向：<b>買入 / 回補</b>" in telegram.bot.messages[0]["text"]
    assert "趨勢判定：<b>上升趨勢</b> | 風險模式：<b>積極</b>" in telegram.bot.messages[0]["text"]
    assert "資金配置：<b>趨勢強攻</b> (啟用) x3.50" in telegram.bot.messages[0]["text"]
    assert len(client.conditional_orders) == 1
    assert len(client.limit_orders) == 1


@pytest.mark.asyncio
async def test_auto_trader_skips_entry_when_mark_already_breached_exit_level(monkeypatch):
    client = FakeClient()
    client.mark_price = 2136.8
    trader = TestnetAutoTrader(_settings(), client, FakeTelegramApp())

    def fake_signal(candles, base, day_pnl=0.0):
        class FakeDecision:
            signal = SignalPlan(
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
                reasons=["stale breakout"],
            )
            strategy = "orb_long"
            regime = "trend_up"
            risk_mode = "aggressive"
            market_playbook = "breakout"
            allocator_state = "active"
            allocator_profile = "trend_aggressive"
            allocator_scale = 3.5
            max_holding_bars = 24
        return FakeDecision()

    monkeypatch.setattr("src.gridbot.testnet.auto_trader.generate_router_allocator_v13_trend350_live_decision", fake_signal)

    await trader.run_cycle()

    assert client.leverage_calls == []
    assert client.orders == []
    assert client.conditional_orders == []
    assert client.limit_orders == []
    assert trader._plans == {}


@pytest.mark.asyncio
async def test_auto_trader_reanchors_short_exit_levels_to_fill_price(monkeypatch):
    client = FakeClient()
    telegram = FakeTelegramApp()
    trader = TestnetAutoTrader(_settings(), client, telegram)

    async def fake_create_market_order(**kwargs):
        client.orders.append(kwargs)
        return {"orderId": 321, "status": "FILLED", "avgPrice": "2106.94", "updateTime": 1234567890, **kwargs}

    client.create_market_order = fake_create_market_order

    def fake_signal(candles, base, day_pnl=0.0):
        class FakeDecision:
            signal = SignalPlan(
                action="PLAN_SHORT",
                confidence=92,
                score=92,
                symbol="ETHUSDC",
                price=2108,
                rsi=42,
                atr=18,
                support=2120,
                vwap=2105,
                entries=[2113.95],
                stop_loss=2138.6117,
                take_profits=[2099.7058],
                planned_notional_usdc=120,
                leverage_cap=8,
                reasons=["router short"],
            )
            strategy = "orb_short"
            regime = "trend_down"
            risk_mode = "off"
            market_playbook = "no_trade"
            allocator_state = "normal"
            allocator_profile = "short"
            allocator_scale = 0.55
            max_holding_bars = 24
        return FakeDecision()

    monkeypatch.setattr("src.gridbot.testnet.auto_trader.generate_router_allocator_v13_trend350_live_decision", fake_signal)

    await trader.run_cycle()

    plan = trader._plans["ETHUSDC"]
    assert plan.entry_price == 2106.94
    assert plan.opened_at_ms == 1234567890
    assert plan.stop_loss == pytest.approx(2131.6017)
    assert plan.take_profit == pytest.approx(2092.6958)


def test_executed_entry_price_ignores_zero_avg_price():
    trader = TestnetAutoTrader(_settings(), FakeClient(), FakeTelegramApp())
    signal = SignalPlan(
        action="PLAN_SHORT",
        confidence=92,
        score=92,
        symbol="ETHUSDC",
        price=2108,
        rsi=42,
        atr=18,
        support=2120,
        vwap=2105,
        entries=[2113.95],
        stop_loss=2138.6117,
        take_profits=[2099.7058],
        planned_notional_usdc=120,
        leverage_cap=8,
        reasons=["router short"],
    )
    result = TestnetOrderResult(
        symbol="ETHUSDC",
        side="SELL",
        quantity="0.071",
        notional_usdc=150,
        leverage=8,
        reduce_only=False,
        order={"avgPrice": "0.00000", "executedQty": "0", "cumQuote": "0"},
    )

    assert trader._executed_entry_price(result, signal) == pytest.approx(2113.95)


@pytest.mark.asyncio
async def test_auto_trader_skips_tight_reward_signal(monkeypatch):
    client = FakeClient()
    trader = TestnetAutoTrader(_settings(testnet_min_reward_pct=0.12), client, FakeTelegramApp())

    def fake_signal(candles, base, day_pnl=0.0):
        class FakeDecision:
            signal = SignalPlan(
                action="PLAN_SHORT",
                confidence=92,
                score=92,
                symbol="ETHUSDC",
                price=2126.44,
                rsi=42,
                atr=18,
                support=2134,
                vwap=2128,
                entries=[2126.44],
                stop_loss=2133.14,
                take_profits=[2126.13],
                planned_notional_usdc=150,
                leverage_cap=8,
                reasons=["tight target"],
            )
            strategy = "orb_short"
            regime = "trend_down"
            risk_mode = "off"
            market_playbook = "no_trade"
            allocator_state = "normal"
            allocator_profile = "short"
            allocator_scale = 0.55
            max_holding_bars = 24
        return FakeDecision()

    monkeypatch.setattr("src.gridbot.testnet.auto_trader.generate_router_allocator_v13_trend350_live_decision", fake_signal)

    await trader.run_cycle()

    assert client.orders == []
    assert client.conditional_orders == []


@pytest.mark.asyncio
async def test_auto_trader_opens_short_from_router_signal(monkeypatch):
    client = FakeClient()
    telegram = FakeTelegramApp()
    trader = TestnetAutoTrader(_settings(), client, telegram)

    def fake_signal(candles, base, day_pnl=0.0):
        class FakeDecision:
            signal = SignalPlan(
                action="PLAN_SHORT",
                confidence=78,
                score=78,
                symbol="ETHUSDC",
                price=2100,
                rsi=45,
                atr=20,
                support=2120,
                vwap=2090,
                entries=[2098],
                stop_loss=2125,
                take_profits=[2060],
                planned_notional_usdc=120,
                leverage_cap=8,
                reasons=["router short"],
            )
            strategy = "orb_short"
            regime = "trend_down"
            risk_mode = "normal"
            market_playbook = "breakdown"
            allocator_state = "active"
            allocator_profile = "short_breakdown"
            allocator_scale = 1.25
            max_holding_bars = 24
        return FakeDecision()

    monkeypatch.setattr("src.gridbot.testnet.auto_trader.generate_router_allocator_v13_trend350_live_decision", fake_signal)

    await trader.run_cycle()

    assert client.leverage_calls == [("ETHUSDC", 8)]
    assert client.orders[0]["side"] == "SELL"
    assert client.orders[0]["reduce_only"] is False
    assert len(client.conditional_orders) == 1
    assert len(client.limit_orders) == 1


@pytest.mark.asyncio
async def test_auto_trader_closes_short_take_profit():
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=-0.05,
        entry_price=2100,
        mark_price=2050,
        unrealized_pnl=2.5,
        liquidation_price=2300,
        leverage=8,
        margin_type="isolated",
    )
    telegram = FakeTelegramApp()
    trader = TestnetAutoTrader(_settings(), client, telegram)
    trader._plans["ETHUSDC"] = ActivePlan(
        symbol="ETHUSDC",
        side="short",
        strategy="orb_short",
        regime="trend_down",
        risk_mode="normal",
        market_playbook="breakdown",
        allocator_state="active",
        allocator_profile="short_breakdown",
        allocator_scale=1.25,
        opened_at_ms=1,
        entry_price=2100,
        stop_loss=2125,
        take_profit=2060,
        max_holding_bars=24,
        score=78,
        reasons=["test"],
    )

    await trader.run_cycle()

    assert client.orders[0]["side"] == "BUY"
    assert client.orders[0]["reduce_only"] is True


@pytest.mark.asyncio
async def test_auto_trader_closes_short_stop_before_syncing_protection_orders():
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=-0.043,
        entry_price=2125.98,
        mark_price=2135.95,
        unrealized_pnl=-0.43,
        liquidation_price=117856,
        leverage=1,
        margin_type="isolated",
    )
    trader = TestnetAutoTrader(_settings(), client, FakeTelegramApp())
    trader._plans["ETHUSDC"] = ActivePlan(
        symbol="ETHUSDC",
        side="short",
        strategy="orb_short",
        regime="trend_down",
        risk_mode="normal",
        market_playbook="breakdown",
        allocator_state="active",
        allocator_profile="short_breakdown",
        allocator_scale=1.25,
        opened_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
        entry_price=2125.98,
        stop_loss=2131.0,
        take_profit=2118.0,
        max_holding_bars=24,
        score=78,
        reasons=["test"],
    )

    await trader.run_manage_cycle()

    assert client.orders[0]["side"] == "BUY"
    assert client.orders[0]["reduce_only"] is True
    assert client.conditional_orders == []
    assert "ETHUSDC" not in trader._plans


@pytest.mark.asyncio
async def test_manage_cycle_closes_unrecoverable_position():
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=-0.043,
        entry_price=2125.98,
        mark_price=2134.88,
        unrealized_pnl=-0.38,
        liquidation_price=117856,
        leverage=1,
        margin_type="isolated",
    )
    telegram = FakeTelegramApp()
    trader = TestnetAutoTrader(_settings(), client, telegram)

    await trader.run_manage_cycle()

    assert client.orders[0]["side"] == "BUY"
    assert client.orders[0]["reduce_only"] is True
    assert "Testnet 保護性平倉" in telegram.bot.messages[0]["text"]
    assert "無法復原策略 plan" in telegram.bot.messages[0]["text"]
    assert "ETHUSDC" not in trader._plans


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
        side="long",
        strategy="orb_long",
        regime="trend_up",
        risk_mode="aggressive",
        market_playbook="breakout",
        allocator_state="active",
        allocator_profile="trend_aggressive",
        allocator_scale=3.5,
        opened_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
        entry_price=2100,
        stop_loss=2070,
        take_profit=2130,
        max_holding_bars=24,
        score=80,
        reasons=["test"],
    )

    await trader.run_cycle()

    assert client.orders[0]["side"] == "SELL"
    assert client.orders[0]["reduce_only"] is True
    assert "Testnet 自動平倉" in telegram.bot.messages[0]["text"]
    assert "策略：<b>orb_long</b>" in telegram.bot.messages[0]["text"]
    assert "方向：<b>賣出 / 放空</b>" in telegram.bot.messages[0]["text"]
    assert "原因：觸發策略停利" in telegram.bot.messages[0]["text"]
    assert "資金配置：<b>趨勢強攻</b> (啟用) x3.50" in telegram.bot.messages[0]["text"]


@pytest.mark.asyncio
async def test_auto_trader_daily_loss_stop_closes_with_plan_notification():
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.05,
        entry_price=2100,
        mark_price=2020,
        unrealized_pnl=-4.0,
        liquidation_price=1800,
        leverage=10,
        margin_type="isolated",
    )
    client.income_history = [
        IncomeRecord(1, "ETHUSDC", "REALIZED_PNL", -5.0, "USDC", 1, "", ""),
    ]
    telegram = FakeTelegramApp()
    trader = TestnetAutoTrader(_settings(max_daily_loss_pct=3), client, telegram)
    trader._plans["ETHUSDC"] = ActivePlan(
        symbol="ETHUSDC",
        side="long",
        strategy="orb_long",
        regime="trend_up",
        risk_mode="aggressive",
        market_playbook="breakout",
        allocator_state="active",
        allocator_profile="trend_aggressive",
        allocator_scale=3.5,
        opened_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
        entry_price=2100,
        stop_loss=2070,
        take_profit=2130,
        max_holding_bars=24,
        score=80,
        reasons=["test"],
    )

    await trader.run_cycle()

    assert client.orders[0]["side"] == "SELL"
    assert client.orders[0]["reduce_only"] is True
    assert "原因：觸發單日虧損上限" in telegram.bot.messages[0]["text"]
    assert "策略：<b>orb_long</b>" in telegram.bot.messages[0]["text"]
    assert "ETHUSDC" not in trader._plans


@pytest.mark.asyncio
async def test_manage_cycle_cleans_stale_protection_orders_when_flat():
    client = FakeClient()
    client.open_algo_orders = [
        {"algoId": 901, "clientAlgoId": "cry3sl_1"},
    ]
    client.open_orders = [
        {"orderId": 902, "clientOrderId": "cry3tp_1"},
    ]
    trader = TestnetAutoTrader(_settings(), client, FakeTelegramApp())

    await trader.run_manage_cycle()

    assert client.cancelled_orders == [("ETHUSDC", 901), ("ETHUSDC", 902)]


@pytest.mark.asyncio
async def test_manage_cycle_replaces_mismatched_protection_order():
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.05,
        entry_price=2100,
        mark_price=2110,
        unrealized_pnl=0.5,
        liquidation_price=1800,
        leverage=10,
        margin_type="isolated",
    )
    client.open_algo_orders = [
        {
            "orderId": 801,
            "clientOrderId": "cry3sl_old",
            "side": "SELL",
            "orderType": "STOP_MARKET",
            "stopPrice": "2060",
        },
    ]
    client.open_orders = [
        {
            "orderId": 802,
            "clientOrderId": "cry3tp_ok",
            "side": "SELL",
            "orderType": "LIMIT",
            "price": "2130",
        },
    ]
    trader = TestnetAutoTrader(_settings(), client, FakeTelegramApp())
    trader._plans["ETHUSDC"] = ActivePlan(
        symbol="ETHUSDC",
        side="long",
        strategy="orb_long",
        regime="trend_up",
        risk_mode="aggressive",
        market_playbook="breakout",
        allocator_state="active",
        allocator_profile="trend_aggressive",
        allocator_scale=3.5,
        opened_at_ms=int(datetime.now(timezone.utc).timestamp() * 1000),
        entry_price=2100,
        stop_loss=2070,
        take_profit=2130,
        max_holding_bars=24,
        score=80,
        reasons=["test"],
    )

    await trader.run_manage_cycle()

    assert client.cancelled_orders == [("ETHUSDC", 801)]
    assert len(client.conditional_orders) == 1
    assert client.limit_orders == []
    assert client.conditional_orders[0]["order_type"] == "STOP_MARKET"
    assert client.conditional_orders[0]["trigger_price"] == 2070


@pytest.mark.asyncio
async def test_manage_cycle_recovers_plan_for_existing_position(monkeypatch):
    client = FakeClient()
    entry_time_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - 300_000
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=-0.071,
        entry_price=2106.94,
        mark_price=2105.50,
        unrealized_pnl=0.1,
        liquidation_price=2300,
        leverage=8,
        margin_type="isolated",
    )
    client.all_orders = [
        {
            "time": entry_time_ms,
            "side": "SELL",
            "status": "FILLED",
            "avgPrice": "2106.94000",
            "origQty": "0.071",
            "clientOrderId": "cry3tn_1779206810846",
            "orderId": 305010414,
        }
    ]
    telegram = FakeTelegramApp()
    trader = TestnetAutoTrader(_settings(), client, telegram)
    monkeypatch.setattr(trader, "_now_ms", lambda: entry_time_ms + 300_000)

    def fake_signal(candles, base, day_pnl=0.0):
        class FakeDecision:
            signal = SignalPlan(
                action="PLAN_SHORT",
                confidence=92,
                score=92,
                symbol="ETHUSDC",
                price=2108,
                rsi=42,
                atr=18,
                support=2120,
                vwap=2105,
                entries=[2113.95],
                stop_loss=2138.6117,
                take_profits=[2099.7058],
                planned_notional_usdc=120,
                leverage_cap=8,
                reasons=["router short"],
            )
            strategy = "orb_short"
            regime = "trend_down"
            risk_mode = "off"
            market_playbook = "no_trade"
            allocator_state = "normal"
            allocator_profile = "short"
            allocator_scale = 0.55
            max_holding_bars = 24
        return FakeDecision()

    monkeypatch.setattr("src.gridbot.testnet.auto_trader.generate_router_allocator_v13_trend350_live_decision", fake_signal)

    await trader.run_manage_cycle()

    plan = trader._plans["ETHUSDC"]
    assert plan.side == "short"
    assert plan.entry_price == 2106.94
    assert plan.opened_at_ms == entry_time_ms
    assert plan.strategy == "orb_short"
    assert "已重新接管既有持倉" in telegram.bot.messages[0]["text"]
    assert len(client.conditional_orders) == 1
    assert len(client.limit_orders) == 1
