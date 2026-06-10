import pytest

from config.settings import Settings
from src.gridbot.binance.models import PositionInfo
from src.gridbot.mainnet.one_run import MainnetOneRunManager


class FakeRepo:
    def __init__(self):
        self.updated = []
        self.events = []
        self.completed = []
        self.created = []
        self.first_event_time = {}

    async def get_latest_run(self):
        return None

    async def get_active_run(self):
        return None

    async def create_run(self, run):
        self.created.append(run)
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
        self.stop_market_sl_orders = []
        self.stop_limit_sl_orders = []
        self.algo_orders = []
        self.cancelled = []
        self.cancelled_algo = []
        self._next_order_id = 1000
        # Configurable top-of-book so tests can keep the book consistent with
        # the position's mark (the E3 anchor gate compares book vs cost basis).
        self.book = {"bidPrice": "100.00", "askPrice": "100.10"}

    async def get_position(self, symbol):
        return self.position

    async def get_klines(self, symbol, interval="1m", limit=300):
        # Minimal synthetic klines; tests that need real indicators monkeypatch
        # evaluate_dca_guard, so the values here only need to parse cleanly.
        base = 1_700_000_000_000
        return [
            [base + i * 60_000, "100", "101", "99", "100", "1", 0, "100"]
            for i in range(max(2, min(limit, 5)))
        ]

    async def get_open_orders(self, symbol):
        return list(self.open_orders)

    async def get_open_algo_orders(self, symbol):
        return list(self.algo_orders)

    async def get_all_orders(self, symbol, start_time=None, limit=1000):
        return list(self.all_orders)

    async def get_user_trades(self, symbol, start_time=None, limit=1000):
        return list(self.user_trades)

    async def cancel_order(self, symbol, order_id):
        self.cancelled.append((symbol, order_id))
        self.open_orders = [o for o in self.open_orders if o.get("orderId") != order_id]
        return {"symbol": symbol, "orderId": order_id, "status": "CANCELED"}

    async def cancel_algo_order(self, symbol, algo_id=None, client_algo_id=None):
        self.cancelled_algo.append((symbol, algo_id, client_algo_id))
        self.algo_orders = [o for o in self.algo_orders if o.get("algoId") != algo_id]
        return {"symbol": symbol, "algoId": algo_id, "algoStatus": "CANCELED"}

    async def create_stop_market_sl_order(
        self,
        symbol,
        side,
        stop_price,
        quantity,
        client_order_id=None,
        working_type="MARK_PRICE",
    ):
        # Mirror the real SDK: STOP_MARKET is routed to the algoOrder endpoint,
        # gets a random clientAlgoId (our client_order_id is discarded), and
        # lives in openAlgoOrders — NOT openOrders.
        algo_id = self._next_order_id
        self._next_order_id += 1
        order = {
            "algoId": algo_id,
            "clientAlgoId": f"x-FAKE{algo_id}",
            "algoType": "CONDITIONAL",
            "orderType": "STOP_MARKET",
            "symbol": symbol,
            "side": side,
            "triggerPrice": str(stop_price),
            "quantity": str(quantity),
            "reduceOnly": True,
            "algoStatus": "NEW",
        }
        self.stop_market_sl_orders.append(order)
        self.algo_orders.append(order)
        return order

    async def create_stop_limit_sl_order(
        self,
        symbol,
        side,
        stop_price,
        limit_price,
        quantity,
        client_order_id=None,
        working_type="MARK_PRICE",
    ):
        order = {
            "orderId": self._next_order_id,
            "clientOrderId": client_order_id,
            "type": "STOP",
            "symbol": symbol,
            "side": side,
            "stopPrice": str(stop_price),
            "price": str(limit_price),
            "quantity": str(quantity),
            "reduceOnly": True,
            "status": "NEW",
        }
        self._next_order_id += 1
        self.stop_limit_sl_orders.append(order)
        return order

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
        return dict(self.book)

    async def get_book_ticker(self, symbol):
        return dict(self.book)

    async def get_commission_rate(self, symbol):
        return {"makerCommissionRate": "0", "takerCommissionRate": "0.0004"}

    async def price_tick_size(self, symbol):
        from decimal import Decimal
        return Decimal("0.01")

    async def create_limit_order_raw(self, symbol, side, quantity, price,
                                     time_in_force="GTC", reduce_only=False,
                                     client_order_id=None):
        order = {
            "orderId": self._next_order_id,
            "symbol": symbol, "side": side,
            "origQty": str(quantity), "price": str(price),
            "timeInForce": time_in_force,
            "clientOrderId": client_order_id,
        }
        self._next_order_id += 1
        self.all_orders.append({**order, "status": "NEW", "updateTime": 1})
        return order


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
        # Tests use unrealistic ~100 prices; disable dust cleanup by default so
        # the small notionals don't trip it. The dust test overrides this.
        "mainnet_residual_cleanup_notional_usdc": 0.0,
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

    tp_orders = [o for o in client.reduce_only_limit_orders if "tp" in (o.get("clientOrderId") or "")]
    # TP2 (mid fixed) disabled: only TP1 (40%) + TP3 (signal, 30%) placed; remaining 30% left for TRAIL
    assert len(tp_orders) == 2
    assert all(order["side"] == "SELL" for order in tp_orders)
    assert {o["clientOrderId"] for o in tp_orders} == {"cry3mn_test_tp1", "cry3mn_test_tp3"}
    # SL is a STOP_MARKET algo order, lives in algo_orders (not reduce_only_limit_orders)
    assert len(client.stop_market_sl_orders) == 1
    sl = client.stop_market_sl_orders[0]
    assert float(sl["triggerPrice"]) == pytest.approx(99.0)
    assert sl["side"] == "SELL"
    assert telegram.bot.messages
    assert "Mainnet one-run 已成交" in telegram.bot.messages[-1]["text"]


@pytest.mark.asyncio
async def test_run_running_take_profit_does_not_market_close():
    import time as _time
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
    # armed just now so run_age_bars=0, no MAX_HOLD trigger
    run = _run(
        signal_json='{"side":"LONG","take_profit":101.0,"stop_loss":99.0}',
        avg_entry_price=100.0,
        armed_at_ms=int(_time.time() * 1000),
    )

    await manager._run_running(run)

    assert client.market_orders == []
    # TP2 (mid fixed) disabled: TP1 + TP3 only (2 orders)
    assert len(client.reduce_only_limit_orders) == 2


@pytest.mark.asyncio
async def test_run_running_stop_loss_closes_with_market_and_cancels_tp_orders():
    import time as _time
    client = FakeClient()
    client.open_orders = [
        {"orderId": 111, "symbol": "ETHUSDC", "clientOrderId": "cry3mn_test_tp1", "origQty": "0.048", "price": "100.05"},
        {"orderId": 112, "symbol": "ETHUSDC", "clientOrderId": "cry3mn_test_tp2", "origQty": "0.036", "price": "100.30"},
        {"orderId": 114, "symbol": "ETHUSDC", "clientOrderId": "cry3mn_test_tp3", "origQty": "0.036", "price": "101.00"},
    ]
    # SL is a STOP_MARKET algo order on the separate openAlgoOrders endpoint
    client.algo_orders = [
        {"algoId": 113, "clientAlgoId": "x-FAKE113", "algoType": "CONDITIONAL", "orderType": "STOP_MARKET",
         "symbol": "ETHUSDC", "side": "SELL", "triggerPrice": "99.0", "quantity": "0.12", "reduceOnly": True},
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
    # Disable recovery so _hit_stop path is reached without DCA interference
    manager = MainnetOneRunManager(
        _settings(mainnet_recovery_enabled=False, mainnet_mid_tp_pct=0.0030), client, repo, telegram
    )
    run = _run(
        signal_json='{"side":"LONG","take_profit":101.0,"stop_loss":99.0}',
        avg_entry_price=100.0,
        armed_at_ms=int(_time.time() * 1000),
    )

    await manager._run_running(run)

    # TP limit orders cancelled via cancel_order
    assert set(client.cancelled) == {("ETHUSDC", 111), ("ETHUSDC", 112), ("ETHUSDC", 114)}
    # STOP_MARKET SL algo order cancelled via cancel_algo_order
    assert len(client.cancelled_algo) == 1
    assert client.cancelled_algo[0][1] == 113  # algoId
    # Then a single market close
    assert len(client.market_orders) == 1
    assert client.market_orders[0]["side"] == "SELL"


@pytest.mark.asyncio
async def test_finish_flat_run_cancels_residual_sl_algo():
    """STOP_MARKET SL algo order must be cancelled when position closes via TP fill."""
    client = FakeClient()
    # SL lives in algo_orders (openAlgoOrders), not open_orders
    client.algo_orders = [
        {"algoId": 201, "clientAlgoId": "x-FAKE201", "algoType": "CONDITIONAL", "orderType": "STOP_MARKET",
         "symbol": "ETHUSDC", "side": "SELL", "triggerPrice": "99.0", "quantity": "0.120", "reduceOnly": True},
    ]
    client.all_orders = [
        {"orderId": 1001, "clientOrderId": "cry3mn_test_entry", "origQty": "0.120", "status": "FILLED", "updateTime": 10},
        {"orderId": 1002, "clientOrderId": "cry3mn_test_tp1", "origQty": "0.120", "status": "FILLED", "updateTime": 20},
    ]
    from src.gridbot.binance.models import FuturesTrade
    client.user_trades = [
        FuturesTrade(1, 1002, "ETHUSDC", "SELL", 101.0, 0.120, 12.12, 0.12, 0.0, "USDC", 20, "BOTH", True, False),
    ]
    repo = FakeRepo()
    telegram = FakeTelegramApp()
    manager = MainnetOneRunManager(_settings(), client, repo, telegram)
    run = _run(exit_reason="TP", armed_at_ms=1)

    await manager._finish_flat_run(run, "flat_detected")

    # STOP_MARKET SL algo order cancelled via cancel_algo_order (not cancel_order)
    assert len(client.cancelled_algo) == 1
    assert client.cancelled_algo[0][1] == 201  # algoId
    assert ("ETHUSDC", 201) not in client.cancelled


@pytest.mark.asyncio
async def test_sl_places_stop_market():
    """_place_stop_loss_maker places a STOP_MARKET algo order at sl_price.
    The order lives in openAlgoOrders (not openOrders)."""
    client = FakeClient()
    repo = FakeRepo()
    manager = MainnetOneRunManager(_settings(), client, repo, FakeTelegramApp())
    run = _run()

    await manager._place_stop_loss_maker(
        symbol="ETHUSDC", side="SELL", qty_str="0.119", sl_price=1688.49,
        run_id="cry3mn_test", reason="SL", run=run,
    )

    assert len(client.stop_market_sl_orders) == 1
    order = client.stop_market_sl_orders[0]
    assert float(order["triggerPrice"]) == pytest.approx(1688.49)
    assert order["side"] == "SELL"
    assert order["reduceOnly"] is True
    assert len(client.algo_orders) == 1
    assert client.reduce_only_limit_orders == []


@pytest.mark.asyncio
async def test_finish_flat_run_captures_algo_sl_fill_pnl():
    """A2 fix: the STOP_MARKET SL fill carries an 'x-...' clientOrderId, so it is
    NOT matched by the '<run_id>' prefix. Its realized PnL/commission must still
    be captured from the in-window trades (was reported as $0 before)."""
    client = FakeClient()
    # Only the entry order has the run_id prefix; the SL algo fill order does not.
    client.all_orders = [
        {"orderId": 5001, "clientOrderId": "cry3mn_test_entry", "origQty": "0.119", "status": "FILLED", "updateTime": 10},
    ]
    from src.gridbot.binance.models import FuturesTrade
    client.user_trades = [
        # entry fill (matched order 5001, opening trade, no realized pnl)
        FuturesTrade(1, 5001, "ETHUSDC", "BUY", 1677.33, 0.119, 199.6, 0.0, 0.0, "USDC", 20, "BOTH", True, True),
        # SL STOP_MARKET close: order 9999 (the 'x-...' algo fill) is NOT matched
        FuturesTrade(2, 9999, "ETHUSDC", "SELL", 1674.32, 0.119, 199.25, -0.3582, 0.0797, "USDC", 40, "BOTH", False, False),
    ]
    repo = FakeRepo()
    telegram = FakeTelegramApp()
    manager = MainnetOneRunManager(_settings(), client, repo, telegram)
    run = _run(exit_reason="SL", armed_at_ms=1)

    await manager._finish_flat_run(run, "flat_detected")

    _, fields = repo.updated[-1]
    assert fields["realized_pnl_usdc"] == pytest.approx(-0.3582)
    assert fields["commission_usdc"] == pytest.approx(0.0797)
    assert fields["qty"] == pytest.approx(0.119)
    assert "已實現損益：<b>$-0.3582</b>" in telegram.bot.messages[-1]["text"]


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


@pytest.mark.asyncio
async def test_finish_flat_run_cancels_all_run_orders():
    """_finish_flat_run must cancel all TP limit orders AND sweep the STOP_MARKET
    SL algo order when position closes."""
    client = FakeClient()
    client.open_orders = [
        {"orderId": 301, "symbol": "ETHUSDC", "clientOrderId": "cry3mn_test_tp1", "origQty": "0.048", "price": "101.0"},
        {"orderId": 302, "symbol": "ETHUSDC", "clientOrderId": "cry3mn_test_tp2", "origQty": "0.072", "price": "101.5"},
        # unrelated limit order from another run — prefix mismatch, must NOT be cancelled
        {"orderId": 999, "symbol": "ETHUSDC", "clientOrderId": "cry3mn_OTHER_tp1"},
    ]
    # SL lives in algo_orders (openAlgoOrders), not open_orders
    client.algo_orders = [
        {"algoId": 303, "clientAlgoId": "x-FAKE303", "algoType": "CONDITIONAL", "orderType": "STOP_MARKET",
         "symbol": "ETHUSDC", "side": "SELL", "triggerPrice": "99.0", "quantity": "0.120", "reduceOnly": True},
    ]
    client.all_orders = [
        {"orderId": 1001, "clientOrderId": "cry3mn_test_entry", "origQty": "0.120", "status": "FILLED", "updateTime": 10},
        {"orderId": 1002, "clientOrderId": "cry3mn_test_tp2", "origQty": "0.072", "status": "FILLED", "updateTime": 20},
    ]
    from src.gridbot.binance.models import FuturesTrade
    client.user_trades = [
        FuturesTrade(1, 1002, "ETHUSDC", "SELL", 101.5, 0.072, 7.308, 0.07, 0.0, "USDC", 20, "BOTH", True, False),
    ]
    repo = FakeRepo()
    telegram = FakeTelegramApp()
    manager = MainnetOneRunManager(_settings(), client, repo, telegram)
    run = _run(exit_reason="TP", armed_at_ms=1)

    await manager._finish_flat_run(run, "flat_detected")

    # TP limit orders cancelled by prefix match
    assert ("ETHUSDC", 301) in client.cancelled
    assert ("ETHUSDC", 302) in client.cancelled
    # STOP_MARKET SL algo order cancelled via cancel_algo_order
    assert len(client.cancelled_algo) == 1
    assert client.cancelled_algo[0][1] == 303  # algoId
    # Unrelated limit order (prefix mismatch) must NOT be cancelled
    assert ("ETHUSDC", 999) not in client.cancelled


@pytest.mark.asyncio
async def test_run_running_tp_partial_fill_does_not_rearm_sl():
    """Bug 3: when qty shrinks (TP partial fill), STOP_MARKET SL must not be
    cancelled-and-rearmed — only the qty tracking should be updated."""
    import time as _time
    client = FakeClient()
    # STOP_MARKET SL (algoId 301) is already live on the exchange as an algo order
    client.algo_orders = [
        {"algoId": 301, "clientAlgoId": "x-FAKE301", "algoType": "CONDITIONAL", "orderType": "STOP_MARKET",
         "symbol": "ETHUSDC", "side": "SELL", "triggerPrice": "99.0", "quantity": "0.120", "reduceOnly": True},
    ]
    # Position qty shrank from 0.12 → 0.072 (partial TP filled 0.048)
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.072,
        entry_price=100.0,
        mark_price=100.5,
        unrealized_pnl=0.036,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(mainnet_sl_use_maker=True, mainnet_recovery_enabled=False),
        client, repo, FakeTelegramApp(),
    )
    run = _run(
        qty=0.12,  # previous qty before partial TP
        signal_json='{"side":"LONG","take_profit":101.0,"stop_loss":99.0,"wildcat":{"sl_pct":0.01}}',
        avg_entry_price=100.0,
        armed_at_ms=int(_time.time() * 1000),
    )

    await manager._run_running(run)

    # STOP_MARKET SL must NOT have been cancelled (algo order stays)
    assert client.cancelled_algo == []
    # No new STOP_MARKET SL placed (only new TP orders are acceptable)
    assert client.stop_market_sl_orders == []
    # qty tracking updated in the repo
    qty_updates = [f for _, f in repo.updated if "qty" in f]
    assert qty_updates and qty_updates[-1]["qty"] == pytest.approx(0.072)


@pytest.mark.asyncio
async def test_place_post_only_with_retry_raises_gtx_slippage_when_retries_exhausted():
    """Bug 5: when all GTX retries are rejected with -5022 and fallback_to_gtc=False,
    _place_post_only_with_retry must raise GTXSlippageExceeded (not raw BinanceAPIException)
    so _place_entry marks the run ENTRY_REJECTED instead of FAILED."""
    from binance.exceptions import BinanceAPIException
    from src.gridbot.mainnet.one_run import GTXSlippageExceeded

    client = FakeClient()

    # Override create_limit_order_raw to always raise -5022
    async def always_reject(*args, **kwargs):
        raise BinanceAPIException(
            response=None,
            status_code=400,
            text='{"code":-5022,"msg":"Post Only order rejected"}',
        )
    client.create_limit_order_raw = always_reject

    manager = MainnetOneRunManager(
        _settings(mainnet_gtx_retry_attempts=3, mainnet_entry_fallback_to_gtc=False),
        client, FakeRepo(), FakeTelegramApp(),
    )

    # signal_price matches book so slippage check never fires — only -5022 blocks us
    with pytest.raises(GTXSlippageExceeded, match="GTX entry retries exhausted"):
        await manager._place_post_only_with_retry(
            symbol="ETHUSDC",
            side="BUY",
            quantity="0.124",
            signal_price=100.05,   # near ask (100.10), slippage stays tiny
            client_order_id="cry3mn_test_entry",
            slippage_bps=12.0,
            fallback_to_gtc=False,
        )


@pytest.mark.asyncio
async def test_residual_dust_placed_as_postonly_maker_not_market():
    """Dust below the threshold must be cleared with a reduce-only POST-ONLY
    (maker, 0 fee) order at the top of book — NOT a taker market order."""
    import time as _time
    client = FakeClient()
    # 0.001 ETH * 1623 ≈ 1.6 USDC, well below the 20 USDC threshold
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.001,
        entry_price=1620.0,
        mark_price=1623.0,
        unrealized_pnl=-0.003,
        liquidation_price=2000.0,
        leverage=75,
        margin_type="cross",
    )
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(mainnet_recovery_enabled=False, mainnet_residual_cleanup_notional_usdc=20.0),
        client, repo, FakeTelegramApp(),
    )
    run = _run(
        qty=0.001,
        side="LONG",
        signal_json='{"side":"LONG","take_profit":1630.0,"stop_loss":1610.0}',
        avg_entry_price=1620.0,
        armed_at_ms=int(_time.time() * 1000),
    )

    await manager._run_running(run)

    # NO taker market order
    assert client.market_orders == []
    # A single reduce-only POST-ONLY maker dust order, sitting at best ask (SELL)
    assert len(client.reduce_only_limit_orders) == 1
    dust = client.reduce_only_limit_orders[0]
    assert dust["postOnly"] is True
    assert dust["side"] == "SELL"
    assert dust["clientOrderId"] == "cry3mn_test_dust"
    assert dust["price"] == "100.1"  # FakeClient best ask


@pytest.mark.asyncio
async def test_trailing_exit_locks_runner_gain_after_arm_then_retrace():
    """Trailing TP: once peak MFE crosses arm_frac*tp_pct the lock arms; a
    subsequent retrace past the giveback fraction market-closes the runner."""
    import time as _time
    client = FakeClient()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_recovery_enabled=False,
            mainnet_trail_enabled=True,
            mainnet_trail_arm_frac=0.7,
            mainnet_trail_giveback_frac=0.25,
            mainnet_trail_exit_use_maker=False,  # test the pure market lock-exit
        ),
        client, repo, FakeTelegramApp(),
    )
    # tp_pct=0.01 → arm threshold = 0.007 (peak must reach 100.7).
    run = _run(
        signal_json='{"side":"LONG","take_profit":101.0,"stop_loss":99.0,"wildcat":{"tp_pct":0.01,"sl_pct":0.01}}',
        avg_entry_price=100.0,
        armed_at_ms=int(_time.time() * 1000),
    )

    def _pos(mark):
        return PositionInfo(
            symbol="ETHUSDC", position_amt=0.12, entry_price=100.0, mark_price=mark,
            unrealized_pnl=(mark - 100.0) * 0.12, liquidation_price=80.0,
            leverage=75, margin_type="cross",
        )

    # Cycle 1: peak spikes to 100.8 (MFE 0.8% >= 0.7%) → arms, no exit yet.
    client.position = _pos(100.8)
    await manager._run_running(run)
    assert client.market_orders == []
    assert run["run_id"] in manager._trail_armed

    # Cycle 2: retrace to 100.5. trail_stop = 100 + (100.8-100)*0.75 = 100.6,
    # mark 100.5 <= 100.6 → lock-exit via market close.  Keep the fake book
    # consistent with the mark so the E3 anchor gate (bid vs cost basis +
    # profit-floor epsilon) lets the fire proceed.
    client.position = _pos(100.5)
    client.book = {"bidPrice": "100.50", "askPrice": "100.60"}
    await manager._run_running(run)
    assert len(client.market_orders) == 1
    assert client.market_orders[0]["side"] == "SELL"
    assert any(c[1] == "TRAIL" for c in repo.completed) or any(
        f.get("exit_reason") == "TRAIL" for _, f in repo.updated
    )


@pytest.mark.asyncio
async def test_trailing_exit_does_not_fire_before_arm_threshold():
    """A small favorable move below arm_frac*tp_pct must not arm or exit."""
    import time as _time
    client = FakeClient()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_recovery_enabled=False,
            mainnet_trail_enabled=True,
            mainnet_trail_arm_frac=0.7,
            mainnet_trail_giveback_frac=0.25,
            mainnet_trail_exit_use_maker=False,  # test the pure market lock-exit
        ),
        client, repo, FakeTelegramApp(),
    )
    run = _run(
        signal_json='{"side":"LONG","take_profit":101.0,"stop_loss":99.0,"wildcat":{"tp_pct":0.01,"sl_pct":0.01}}',
        avg_entry_price=100.0,
        armed_at_ms=int(_time.time() * 1000),
    )
    # MFE 0.3% < 0.7% arm threshold.
    client.position = PositionInfo(
        symbol="ETHUSDC", position_amt=0.12, entry_price=100.0, mark_price=100.3,
        unrealized_pnl=0.036, liquidation_price=80.0, leverage=75, margin_type="cross",
    )

    await manager._run_running(run)

    assert client.market_orders == []
    assert run["run_id"] not in manager._trail_armed


@pytest.mark.asyncio
async def test_trail_maker_exit_fills_as_maker_no_taker(monkeypatch):
    """TRAIL lock-exit: a reduce-only POST_ONLY (GTX) order is placed first;
    when it fills (position goes flat within the TTL) no market taker order is
    sent and the run is marked CLOSING with exit_reason TRAIL."""
    from src.gridbot.mainnet import one_run as _mod

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(_mod.asyncio, "sleep", _no_sleep)

    client = FakeClient()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_recovery_enabled=False,
            mainnet_trail_exit_use_maker=True,
            mainnet_trail_exit_maker_ttl_seconds=1,
        ),
        client, repo, FakeTelegramApp(),
    )
    # Position is already flat → the first poll sees the maker fill.
    client.position = None
    run = _run(side="SHORT")

    await manager._close_position("ETHUSDC", "BUY", 0.071, "TRAIL", run)

    # A GTX (post-only) reduce-only order was placed, not a market order.
    assert client.market_orders == []
    assert any(o.get("timeInForce") == "GTX" for o in client.all_orders)
    assert any(et == "trail_maker_filled" for _, et, _ in repo.events)
    assert any(f.get("exit_reason") == "TRAIL" for _, f in repo.updated)


@pytest.mark.asyncio
async def test_trail_maker_exit_times_out_falls_back_to_market():
    """If the maker exit does not fill within the TTL, it is cancelled and the
    remainder is market-closed (guaranteed exit)."""
    client = FakeClient()
    # SHORT in profit: entry 100.3, ask 100.10 — the buy-back anchor clears the
    # cost basis (E3 gates require a TRAIL exit to still be in the money).
    client.position = PositionInfo(
        symbol="ETHUSDC", position_amt=-0.071, entry_price=100.3, mark_price=100.05,
        unrealized_pnl=0.0178, liquidation_price=120.0, leverage=75, margin_type="cross",
    )
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_recovery_enabled=False,
            mainnet_trail_exit_use_maker=True,
            mainnet_trail_exit_maker_ttl_seconds=0,  # immediate timeout, no polling
        ),
        client, repo, FakeTelegramApp(),
    )
    run = _run(side="SHORT")

    await manager._close_position("ETHUSDC", "BUY", 0.071, "TRAIL", run)

    # Maker placed (GTX), timed out, then market-closed.
    assert any(o.get("timeInForce") == "GTX" for o in client.all_orders)
    assert len(client.market_orders) == 1
    assert client.market_orders[0]["side"] == "BUY"
    assert any(et == "trail_maker_timeout" for _, et, _ in repo.events)


@pytest.mark.asyncio
async def test_loop_defers_arm_during_cooldown_then_resumes_after_expiry():
    """Cooldown must not stall the loop: the arm is deferred while the cooldown
    is active, then resumed by _maybe_resume_pending_loop once it expires."""
    import time as _time
    client = FakeClient()
    client.position = None
    repo = FakeRepo()
    manager = MainnetOneRunManager(_settings(), client, repo, FakeTelegramApp())
    manager._loop_total = 3
    manager._loop_completed = 2
    key = ("SHORT", "wildcat_v2_adverse_guard")

    # Cooldown still active → arm is deferred, pending resume recorded.
    future = int(_time.time() * 1000) + 60_000
    manager._loop_cooldowns[key] = future
    await manager._try_arm_next_loop_run("SHORT", "wildcat_v2_adverse_guard", "cry3mn_prev")
    assert repo.created == []
    assert manager._loop_resume is not None
    assert manager._loop_resume["resume_at_ms"] == future

    # Before expiry, resume does nothing.
    await manager._maybe_resume_pending_loop()
    assert repo.created == []

    # Cooldown expired → resume arms the next run.
    manager._loop_cooldowns[key] = int(_time.time() * 1000) - 1000
    manager._loop_resume["resume_at_ms"] = int(_time.time() * 1000) - 1000
    await manager._maybe_resume_pending_loop()
    assert len(repo.created) == 1
    assert repo.created[0]["status"] == "ARMED"
    assert manager._loop_resume is None


@pytest.mark.asyncio
async def test_dca_blocked_by_guard_places_no_order(monkeypatch):
    """When the DCA risk gate blocks, _maybe_recovery must not place any order."""
    import src.gridbot.mainnet.one_run as orm
    client = FakeClient()
    # SHORT position, mark moved against us enough to trigger the recovery hit.
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=-0.119,
        entry_price=1675.32,
        mark_price=1677.0,  # >= 1675.32 * (1 + 0.0005) → hit
        unrealized_pnl=-0.2,
        liquidation_price=2000.0,
        leverage=75,
        margin_type="cross",
    )
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(mainnet_recovery_enabled=True, mainnet_recovery_steps=1), client, repo, FakeTelegramApp()
    )
    run = _run(side="SHORT", qty=0.119)
    monkeypatch.setattr(orm, "evaluate_dca_guard", lambda c, s: (False, "trend=up（趨勢逆行）"))

    result = await manager._maybe_recovery(run, {}, client.position)

    assert result is False
    assert all("_dca" not in str(o.get("clientOrderId") or "") for o in client.all_orders)


@pytest.mark.asyncio
async def test_dca_allowed_by_guard_places_order(monkeypatch):
    """When the gate allows, _maybe_recovery places a DCA maker order."""
    import src.gridbot.mainnet.one_run as orm
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=-0.119,
        entry_price=1675.32,
        mark_price=1677.0,
        unrealized_pnl=-0.2,
        liquidation_price=2000.0,
        leverage=75,
        margin_type="cross",
    )
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(mainnet_recovery_enabled=True, mainnet_recovery_steps=1), client, repo, FakeTelegramApp()
    )
    run = _run(side="SHORT", qty=0.119)
    monkeypatch.setattr(orm, "evaluate_dca_guard", lambda c, s: (True, "range ok"))

    result = await manager._maybe_recovery(run, {}, client.position)

    assert result is True
    assert any("_dca" in str(o.get("clientOrderId") or "") for o in client.all_orders)


def _dca_short_position():
    return PositionInfo(
        symbol="ETHUSDC", position_amt=-0.119, entry_price=1675.32, mark_price=1677.0,
        unrealized_pnl=-0.2, liquidation_price=2000.0, leverage=75, margin_type="cross",
    )


@pytest.mark.asyncio
async def test_dca_guard_block_starts_cooldown_blocking_regime_flicker(monkeypatch):
    """After the guard blocks once, a brief regime flip to 'range' must not let
    a second DCA through within the cooldown window (the 12:00 UTC incident)."""
    import src.gridbot.mainnet.one_run as orm
    client = FakeClient()
    client.position = _dca_short_position()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(mainnet_recovery_enabled=True, mainnet_recovery_steps=1, mainnet_dca_guard_cooldown_seconds=300),
        client, repo, FakeTelegramApp(),
    )
    run = _run(side="SHORT", qty=0.119)

    # 1st call: guard blocks (trend) → records the cooldown timestamp.
    monkeypatch.setattr(orm, "evaluate_dca_guard", lambda c, s: (False, "trend=down"))
    assert await manager._maybe_recovery(run, {}, client.position) is False
    assert run["run_id"] in manager._dca_block_times

    # 2nd call moments later: regime flickers back to range, guard would now
    # allow — but the cooldown must keep it blocked, so no DCA order is placed.
    monkeypatch.setattr(orm, "evaluate_dca_guard", lambda c, s: (True, "range ok"))
    assert await manager._maybe_recovery(run, {}, client.position) is False
    assert all("_dca" not in str(o.get("clientOrderId") or "") for o in client.all_orders)
    assert any(et == "dca_blocked_guard_cooldown" for _, et, _ in repo.events) or \
        all("_dca" not in str(o.get("clientOrderId") or "") for o in client.all_orders)


@pytest.mark.asyncio
async def test_dca_blocked_after_partial_exit(monkeypatch):
    """Once TP1 has partially closed the runner, DCA must never re-average into
    the remaining position (the 12:29 UTC incident)."""
    import src.gridbot.mainnet.one_run as orm
    client = FakeClient()
    client.position = _dca_short_position()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(mainnet_recovery_enabled=True, mainnet_recovery_steps=1), client, repo, FakeTelegramApp()
    )
    run = _run(side="SHORT", qty=0.119)
    # Guard would allow, but the run already booked a partial exit.
    monkeypatch.setattr(orm, "evaluate_dca_guard", lambda c, s: (True, "range ok"))
    manager._partial_exits.add(run["run_id"])

    result = await manager._maybe_recovery(run, {}, client.position)

    assert result is False
    assert all("_dca" not in str(o.get("clientOrderId") or "") for o in client.all_orders)


@pytest.mark.asyncio
async def test_entry_failure_advances_loop_and_arms_next():
    """Bug 9: an entry-stage failure mid-loop must consume one slot and arm the
    next run (no cooldown, since there was no position/PnL)."""
    client = FakeClient()
    client.position = None
    repo = FakeRepo()
    manager = MainnetOneRunManager(_settings(), client, repo, FakeTelegramApp())
    manager._loop_total = 3
    manager._loop_completed = 1
    run = _run(side="SHORT")

    await manager._advance_loop_after_entry_failure(run, "signal_timeout")

    assert manager._loop_completed == 2
    # next run armed
    assert len(repo.created) == 1
    assert repo.created[0]["status"] == "ARMED"


@pytest.mark.asyncio
async def test_entry_failure_on_last_loop_run_ends_loop():
    """Bug 9: entry failure on the final loop slot ends the loop cleanly."""
    client = FakeClient()
    repo = FakeRepo()
    manager = MainnetOneRunManager(_settings(), client, repo, FakeTelegramApp())
    manager._loop_total = 3
    manager._loop_completed = 2
    run = _run(side="SHORT")

    await manager._advance_loop_after_entry_failure(run, "entry_ttl_expired")

    # consumed the last slot → loop reset, nothing armed
    assert manager._loop_total == 0
    assert manager._loop_completed == 0
    assert repo.created == []


@pytest.mark.asyncio
async def test_entry_failure_on_single_run_is_noop():
    """A non-loop (single) run entry failure must not try to chain anything."""
    client = FakeClient()
    repo = FakeRepo()
    manager = MainnetOneRunManager(_settings(), client, repo, FakeTelegramApp())
    manager._loop_total = 0
    run = _run(side="SHORT")

    await manager._advance_loop_after_entry_failure(run, "signal_timeout")

    assert repo.created == []


@pytest.mark.asyncio
async def test_loop_arms_immediately_when_no_cooldown():
    """When there is no active cooldown, the next loop run is armed right away."""
    client = FakeClient()
    client.position = None
    repo = FakeRepo()
    manager = MainnetOneRunManager(_settings(), client, repo, FakeTelegramApp())
    manager._loop_total = 3
    manager._loop_completed = 1

    await manager._try_arm_next_loop_run("LONG", "wildcat_v2_adverse_guard", "cry3mn_prev")

    assert len(repo.created) == 1
    assert repo.created[0]["status"] == "ARMED"
    assert manager._loop_resume is None


@pytest.mark.asyncio
async def test_run_running_dca_shrinks_and_caps_take_profits():
    """Verify that when dca_count > 0, all TP orders are shrunk and capped at full_tp_price."""
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=-0.238,  # SHORT position
        entry_price=1686.71,
        mark_price=1686.50,
        unrealized_pnl=0.05,
        liquidation_price=2000.0,
        leverage=75,
        margin_type="cross",
    )
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_partial_tp_pct=0.0005,  # 0.05%
            mainnet_mid_tp_pct=0.0012,     # 0.12%
            mainnet_recovery_tp_shrink=0.45,
            mainnet_partial_exit_pct=0.40,
            mainnet_mid_exit_pct=0.50,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    # Mark the run as having 1 DCA step completed
    manager._recovery_counts["cry3mn_test"] = 1
    
    run = _run(
        run_id="cry3mn_test",
        side="SHORT",
        signal_json='{"side":"SHORT","take_profit":1685.43,"stop_loss":1689.59,"wildcat":{"tp_pct":0.0008,"recovery_tp_shrink":0.45}}',
        entry_price=1686.15,
        avg_entry_price=1686.71,
    )
    
    import json
    signal = json.loads(run["signal_json"])
    orders = await manager._desired_take_profit_orders(run, client.position, signal=signal, close_side="BUY")
    
    assert len(orders) == 2
    order_dict = {o[0]: (o[1], o[2]) for o in orders}
    
    # Check quantities:
    assert order_dict["cry3mn_test_tp1"][0] == "0.095"
    assert order_dict["cry3mn_test_tp3"][0] == "0.143"
    
    # Check prices (SHORT direction):
    # tp1 = 1686.71 * (1 - 0.000225) = 1686.33
    # tp3 = 1686.71 * (1 - 0.00036) = 1686.10
    assert float(order_dict["cry3mn_test_tp1"][1]) == pytest.approx(1686.3305, abs=0.01)
    assert float(order_dict["cry3mn_test_tp3"][1]) == pytest.approx(1686.1027, abs=0.01)


@pytest.mark.asyncio
async def test_dca_fill_resets_trail_peak_and_disarms():
    """E1: a DCA fill moves the cost basis TOWARD the market, so a pre-DCA peak
    measured against the new basis would instantly re-satisfy arm_mfe and fire
    on the first noise tick (06-10 08:32 loss run: stale peak 1632.58 vs new
    avg 1633.72 armed AT the fill).  The manage cycle must restart peak
    tracking from the current mark and disarm."""
    import time as _time
    client = FakeClient()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_recovery_enabled=False,  # fill already happened; skip preplace
            mainnet_trail_enabled=True,
        ),
        client, repo, FakeTelegramApp(),
    )
    run = _run(
        signal_json='{"side":"LONG","take_profit":101.0,"stop_loss":99.0,"wildcat":{"tp_pct":0.01,"sl_pct":0.01}}',
        qty=0.12,  # DB still holds the pre-DCA qty
        avg_entry_price=100.0,
        armed_at_ms=int(_time.time() * 1000),
    )
    # Stale pre-DCA trail state: peaked at 100.9 and armed.
    manager._trail_peak["cry3mn_test"] = 100.9
    manager._trail_armed.add("cry3mn_test")
    # Exchange filled the DCA: qty doubled, avg moved up toward the market.
    client.position = PositionInfo(
        symbol="ETHUSDC", position_amt=0.24, entry_price=100.5, mark_price=100.55,
        unrealized_pnl=0.012, liquidation_price=80.0, leverage=75, margin_type="cross",
    )

    await manager._run_running(run)

    assert any(et == "recovery_entry_filled" for _, et, _ in repo.events)
    # Trail baseline restarted from the current mark, no longer armed.
    assert manager._trail_peak["cry3mn_test"] == pytest.approx(100.55)
    assert "cry3mn_test" not in manager._trail_armed
    # And nothing fired: mark is barely above the new avg (MFE << arm threshold).
    assert client.market_orders == []


@pytest.mark.asyncio
async def test_trail_fire_aborted_when_book_anchor_below_floor():
    """E3: the trigger passes on MARK, but the exit executes against the BOOK
    (a LONG exit SELL rests at the bid).  If the bid has already dumped through
    cost basis + floor, the fire must abort BEFORE tearing anything down — no
    market order, no cancellations, run handed back to the SL/DCA path."""
    import time as _time
    client = FakeClient()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_recovery_enabled=False,
            mainnet_trail_enabled=True,
            mainnet_trail_arm_frac=0.7,
            mainnet_trail_giveback_frac=0.25,
            mainnet_trail_exit_use_maker=False,  # without the gate → market close
        ),
        client, repo, FakeTelegramApp(),
    )
    run = _run(
        signal_json='{"side":"LONG","take_profit":101.0,"stop_loss":99.0,"wildcat":{"tp_pct":0.01,"sl_pct":0.01}}',
        avg_entry_price=100.0,
        armed_at_ms=int(_time.time() * 1000),
    )

    def _pos(mark):
        return PositionInfo(
            symbol="ETHUSDC", position_amt=0.12, entry_price=100.0, mark_price=mark,
            unrealized_pnl=(mark - 100.0) * 0.12, liquidation_price=80.0,
            leverage=75, margin_type="cross",
        )

    # Cycle 1: peak 100.8 (MFE 0.8% >= 0.7%) arms the trail.
    client.position = _pos(100.8)
    await manager._run_running(run)
    assert run["run_id"] in manager._trail_armed

    # Cycle 2: mark 100.5 passes the trigger (<= trail_stop 100.6, above the
    # mark floor) — but the book already dumped through the cost basis:
    # bid 99.95 < floor 100.0×(1+1.5bp) = 100.015.  The fire must abort.
    client.position = _pos(100.5)
    client.book = {"bidPrice": "99.95", "askPrice": "99.99"}
    await manager._run_running(run)

    assert client.market_orders == []          # nothing closed
    assert client.cancelled_algo == []         # SL untouched
    assert any(et == "trail_fire_aborted_anchor_floor" for _, et, _ in repo.events)
    assert run["run_id"] not in manager._trail_exiting  # back to managed state
    assert repo.completed == []                # run still alive
    assert not any(f.get("exit_reason") == "TRAIL" for _, f in repo.updated)
    # Still armed: when the book recovers above the floor it may fire again.
    assert run["run_id"] in manager._trail_armed


@pytest.mark.asyncio
async def test_loop_loss_protection_breaks_chain_at_cap():
    """Loop loss protection: once the chain's cumulative NET PnL (realized −
    commission) reaches −cap, the loop must stop — no next run armed, loop
    state cleared, protection event logged."""
    from src.gridbot.binance.models import FuturesTrade
    client = FakeClient()
    client.all_orders = [
        {"orderId": 1001, "clientOrderId": "cry3mn_test_entry", "origQty": "0.120", "status": "FILLED", "updateTime": 10},
        {"orderId": 1004, "clientOrderId": "cry3mn_test_close", "origQty": "0.120", "status": "FILLED", "updateTime": 40},
    ]
    # Net for this run: realized −2.40, commission 0.10 → −2.50.
    client.user_trades = [
        FuturesTrade(2, 1004, "ETHUSDC", "SELL", 98.0, 0.120, 11.76, -2.40, 0.10, "USDC", 40, "BOTH", False, False),
    ]
    repo = FakeRepo()
    telegram = FakeTelegramApp()
    manager = MainnetOneRunManager(_settings(), client, repo, telegram)
    # Mid-loop (run 1 of 3) with a 2 USDC cap; prior runs net 0.
    manager._loop_total = 3
    manager._loop_completed = 0
    manager._loop_run_ids = ["cry3mn_test"]
    manager._loop_loss_cap = 2.0
    manager._loop_net_pnl = 0.0
    run = _run(exit_reason="SL", armed_at_ms=1)

    await manager._finish_flat_run(run, "flat_detected")

    # −2.50 <= −2.0 → tripped: event logged, NO next run armed, state cleared.
    assert any(et == "loop_loss_protection_tripped" for _, et, _ in repo.events)
    assert repo.created == []
    assert manager._loop_total == 0
    assert manager._loop_completed == 0
    assert manager._loop_net_pnl == 0.0
    assert "Loop 虧損保護觸發" in telegram.bot.messages[-1]["text"]


@pytest.mark.asyncio
async def test_loop_loss_within_cap_still_chains_next_run():
    """A net loss that does NOT reach the cap must keep the chain going (the
    protection only breaks the loop at the threshold)."""
    from src.gridbot.binance.models import FuturesTrade
    client = FakeClient()
    client.position = None
    client.all_orders = [
        {"orderId": 1001, "clientOrderId": "cry3mn_test_entry", "origQty": "0.120", "status": "FILLED", "updateTime": 10},
        {"orderId": 1004, "clientOrderId": "cry3mn_test_close", "origQty": "0.120", "status": "FILLED", "updateTime": 40},
    ]
    # Net −0.50: under the 2 USDC cap.
    client.user_trades = [
        FuturesTrade(2, 1004, "ETHUSDC", "SELL", 99.6, 0.120, 11.952, -0.50, 0.0, "USDC", 40, "BOTH", False, False),
    ]
    repo = FakeRepo()
    manager = MainnetOneRunManager(_settings(), client, repo, FakeTelegramApp())
    manager._loop_total = 3
    manager._loop_completed = 0
    manager._loop_run_ids = ["cry3mn_test"]
    manager._loop_loss_cap = 2.0
    manager._loop_net_pnl = 0.0
    run = _run(exit_reason="SL", armed_at_ms=1)

    await manager._finish_flat_run(run, "flat_detected")

    assert not any(et == "loop_loss_protection_tripped" for _, et, _ in repo.events)
    assert manager._loop_net_pnl == pytest.approx(-0.50)
    # Chain continues: next run armed (no strategy_label → no cooldown set).
    assert len(repo.created) == 1
    assert repo.created[0]["status"] == "ARMED"
