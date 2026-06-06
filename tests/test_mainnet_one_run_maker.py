import pytest

from config.settings import Settings
from src.gridbot.binance.models import PositionInfo
from src.gridbot.mainnet.one_run import MainnetOneRunManager


class FakeRepo:
    def __init__(self):
        self.updated = []
        self.events = []
        self.completed = []
        self.first_event_time = {}

    async def get_latest_run(self):
        return None

    async def get_active_run(self):
        return None

    async def create_run(self, run):
        return None

    async def update_run(self, run_id, **fields):
        self.updated.append((run_id, fields))

    async def log_event(self, run_id, event_type, details):
        self.events.append((run_id, event_type, details))

    async def complete_run(self, run_id, status, exit_reason=None, error=None):
        self.completed.append((run_id, status, exit_reason, error))

    async def get_first_event_time(self, run_id, event_type):
        return self.first_event_time.get((run_id, event_type))


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)


class FakeTelegramApp:
    def __init__(self):
        self.bot = FakeBot()


class FakeTradeRepo:
    def __init__(self, trades=None):
        self.trades = trades or []

    async def get_trades(self, symbol, since_ms=0, grid_only=False, limit=1000):
        return list(self.trades)


class FakeClient:
    def __init__(self):
        self.position = None
        self.open_orders = []
        self.all_orders = []
        self.user_trades = []
        self.market_orders = []
        self.reduce_only_limit_orders = []
        self.cancelled = []
        self._next_order_id = 1000

    async def get_position(self, symbol):
        return self.position

    async def get_open_orders(self, symbol):
        return list(self.open_orders)

    async def get_all_orders(self, symbol, start_time=None, limit=1000):
        return list(self.all_orders)

    async def get_user_trades(self, symbol, start_time=None, limit=1000):
        return list(self.user_trades)

    async def cancel_order(self, symbol, order_id):
        self.cancelled.append((symbol, order_id))
        self.open_orders = [o for o in self.open_orders if o.get("orderId") != order_id]
        return {"symbol": symbol, "orderId": order_id, "status": "CANCELED"}

    async def create_reduce_only_limit_order(
        self,
        symbol,
        side,
        quantity,
        price,
        client_order_id=None,
        post_only=False,
        position_side=None,
    ):
        order = {
            "orderId": self._next_order_id,
            "symbol": symbol,
            "side": side,
            "origQty": str(quantity),
            "price": str(price),
            "clientOrderId": client_order_id,
            "postOnly": post_only,
        }
        self._next_order_id += 1
        self.reduce_only_limit_orders.append(order)
        self.open_orders.append(order)
        self.all_orders.append({**order, "status": "NEW", "updateTime": 1})
        return order

    async def create_market_order(
        self,
        symbol,
        side,
        quantity,
        reduce_only=False,
        client_order_id=None,
        position_side=None,
    ):
        order = {
            "orderId": self._next_order_id,
            "symbol": symbol,
            "side": side,
            "origQty": str(quantity),
            "reduceOnly": reduce_only,
            "clientOrderId": client_order_id,
        }
        self._next_order_id += 1
        self.market_orders.append(order)
        self.all_orders.append({**order, "status": "NEW", "updateTime": 1})
        return order

    async def format_quantity(self, symbol, qty):
        return f"{qty:.3f}".rstrip("0").rstrip(".")

    async def get_order_book_ticker(self, symbol):
        return {"bidPrice": "100.00", "askPrice": "100.10"}

    async def price_tick_size(self, symbol):
        from decimal import Decimal
        return Decimal("0.01")


def _settings(**overrides):
    data = {
        "binance_api_key": "key",
        "binance_api_secret": "secret",
        "telegram_chat_id": "123",
        "mainnet_one_run_enabled": True,
        "mainnet_api_key": "main-key",
        "mainnet_api_secret": "main-secret",
        "mainnet_symbol": "ETHUSDC",
        "mainnet_require_zero_maker_fee": False,
    }
    data.update(overrides)
    return Settings(**data)


def _run(**overrides):
    data = {
        "run_id": "cry3mn_test",
        "symbol": "ETHUSDC",
        "status": "RUNNING",
        "side": "LONG",
        "signal_json": '{"side":"LONG","take_profit":101.0,"stop_loss":99.0}',
        "qty": 0.12,
        "cumulative_notional_usdc": 200.0,
        "armed_at_ms": 1,
    }
    data.update(overrides)
    return data


@pytest.mark.asyncio
async def test_entry_fill_syncs_maker_take_profit_orders():
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=100.0,
        unrealized_pnl=0.0,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    repo = FakeRepo()
    telegram = FakeTelegramApp()
    manager = MainnetOneRunManager(_settings(), client, repo, telegram)

    run = _run(status="ENTRY_PENDING", entry_order_id=321, signal_json='{"side":"LONG","take_profit":101.0,"stop_loss":99.0}')

    await manager._run_entry_pending(run)

    assert len(client.reduce_only_limit_orders) == 2
    assert all(order["side"] == "SELL" for order in client.reduce_only_limit_orders)
    assert {order["clientOrderId"] for order in client.reduce_only_limit_orders} == {"cry3mn_test_tp1", "cry3mn_test_tp2"}
    assert telegram.bot.messages
    assert "Mainnet one-run 已成交" in telegram.bot.messages[0]["text"]


@pytest.mark.asyncio
async def test_run_running_take_profit_does_not_market_close():
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=101.2,
        unrealized_pnl=0.1,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    repo = FakeRepo()
    manager = MainnetOneRunManager(_settings(), client, repo, FakeTelegramApp())
    run = _run(signal_json='{"side":"LONG","take_profit":101.0,"stop_loss":99.0}', avg_entry_price=100.0)

    await manager._run_running(run)

    assert client.market_orders == []
    assert len(client.reduce_only_limit_orders) == 2


@pytest.mark.asyncio
async def test_run_running_stop_loss_closes_with_market_and_cancels_tp_orders():
    client = FakeClient()
    client.open_orders = [
        {"orderId": 111, "symbol": "ETHUSDC", "clientOrderId": "cry3mn_test_tp1", "origQty": "0.048", "price": "100.05"},
        {"orderId": 112, "symbol": "ETHUSDC", "clientOrderId": "cry3mn_test_tp2", "origQty": "0.072", "price": "101.00"},
    ]
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=98.8,
        unrealized_pnl=-0.2,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    repo = FakeRepo()
    telegram = FakeTelegramApp()
    manager = MainnetOneRunManager(_settings(), client, repo, telegram)
    run = _run(signal_json='{"side":"LONG","take_profit":101.0,"stop_loss":99.0}', avg_entry_price=100.0)

    await manager._run_running(run)

    assert client.cancelled == [("ETHUSDC", 111), ("ETHUSDC", 112)]
    assert len(client.market_orders) == 1
    assert client.market_orders[0]["side"] == "SELL"
    assert telegram.bot.messages
    assert "reason=<b>SL</b>" in telegram.bot.messages[-1]["text"]


@pytest.mark.asyncio
async def test_run_running_max_hold_counts_from_entry_fill_not_arm_time():
    import time

    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=100.2,
        unrealized_pnl=0.02,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    repo = FakeRepo()
    run_id = "cry3mn_test"
    repo.first_event_time[(run_id, "entry_filled")] = int(time.time() * 1000) - 5 * 60_000
    manager = MainnetOneRunManager(_settings(), client, repo, FakeTelegramApp())
    run = _run(run_id=run_id, armed_at_ms=int(time.time() * 1000) - 25 * 60_000, signal_json='{"side":"LONG","take_profit":101.0,"stop_loss":99.0}', avg_entry_price=100.0)

    await manager._run_running(run)

    assert client.market_orders == []
    assert not repo.completed


@pytest.mark.asyncio
async def test_finish_flat_run_updates_summary_from_orders_and_trades():
    client = FakeClient()
    client.all_orders = [
        {"orderId": 1001, "clientOrderId": "cry3mn_test_entry", "origQty": "0.120", "status": "FILLED", "updateTime": 10},
        {"orderId": 1002, "clientOrderId": "cry3mn_test_tp1", "origQty": "0.048", "status": "FILLED", "updateTime": 20},
        {"orderId": 1003, "clientOrderId": "cry3mn_test_dca1", "origQty": "0.120", "status": "FILLED", "updateTime": 30},
        {"orderId": 1004, "clientOrderId": "cry3mn_test_close", "origQty": "0.192", "status": "FILLED", "updateTime": 40},
    ]
    from src.gridbot.binance.models import FuturesTrade

    client.user_trades = [
        FuturesTrade(1, 1002, "ETHUSDC", "SELL", 100.5, 0.048, 4.824, 0.05, 0.01, "USDC", 20, "BOTH", False, False),
        FuturesTrade(2, 1004, "ETHUSDC", "SELL", 99.0, 0.192, 19.008, -0.30, 0.04, "USDC", 40, "BOTH", False, False),
    ]
    repo = FakeRepo()
    telegram = FakeTelegramApp()
    manager = MainnetOneRunManager(_settings(), client, repo, telegram)
    run = _run(exit_reason="SL", armed_at_ms=1)

    await manager._finish_flat_run(run, "flat_detected")

    assert repo.updated
    run_id, fields = repo.updated[-1]
    assert run_id == "cry3mn_test"
    assert fields["qty"] == pytest.approx(0.192)
    assert fields["realized_pnl_usdc"] == pytest.approx(-0.25)
    assert fields["commission_usdc"] == pytest.approx(0.05)
    assert telegram.bot.messages
    assert "已實現損益：<b>$-0.2500</b>" in telegram.bot.messages[-1]["text"]


@pytest.mark.asyncio
async def test_finish_flat_run_merges_db_trade_commission_fallback():
    client = FakeClient()
    client.all_orders = [
        {"orderId": 2001, "clientOrderId": "cry3mn_test_entry", "origQty": "0.126", "status": "FILLED", "updateTime": 10},
        {"orderId": 2002, "clientOrderId": "cry3mn_test_tp1", "origQty": "0.05", "status": "FILLED", "updateTime": 20},
        {"orderId": 2003, "clientOrderId": "cry3mn_test_tp2", "origQty": "0.076", "status": "FILLED", "updateTime": 30},
    ]
    from src.gridbot.binance.models import FuturesTrade

    client.user_trades = [
        FuturesTrade(11, 2002, "ETHUSDC", "SELL", 101.0, 0.05, 5.05, 0.05, 0.0, "USDC", 20, "BOTH", True, False),
        FuturesTrade(12, 2003, "ETHUSDC", "SELL", 101.2, 0.076, 7.6912, 0.08602, 0.0, "USDC", 30, "BOTH", True, False),
    ]
    trade_repo = FakeTradeRepo(
        trades=[
            {
                "trade_id": 11,
                "order_id": 2002,
                "symbol": "ETHUSDC",
                "qty": 0.05,
                "realized_pnl": 0.05,
                "commission": 0.002,
                "commission_asset": "USDC",
                "time_ms": 20,
            },
            {
                "trade_id": 12,
                "order_id": 2003,
                "symbol": "ETHUSDC",
                "qty": 0.076,
                "realized_pnl": 0.08602,
                "commission": 0.003,
                "commission_asset": "USDC",
                "time_ms": 30,
            },
        ]
    )
    repo = FakeRepo()
    telegram = FakeTelegramApp()
    manager = MainnetOneRunManager(_settings(), client, repo, trade_repo=trade_repo, telegram_app=telegram)
    run = _run(exit_reason="TP", armed_at_ms=1, qty=0.126)

    await manager._finish_flat_run(run, "flat_detected")

    _, fields = repo.updated[-1]
    assert fields["realized_pnl_usdc"] == pytest.approx(0.13602)
    assert fields["commission_usdc"] == pytest.approx(0.005)
    assert "手續費：<b>$0.0050</b>" in telegram.bot.messages[-1]["text"]
