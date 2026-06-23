import json

import pytest

from config.settings import Settings
from src.gridbot.binance.models import PositionInfo
from src.gridbot.mainnet.one_run import GTXSlippageExceeded, MainnetOneRunManager


class FakeRepo:
    def __init__(self):
        self.updated = []
        self.events = []
        self.completed = []
        self.created = []
        self.first_event_time = {}
        self.recent_completed_runs = []

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

    async def get_events(self, run_id, limit=30):
        rows = []
        for idx, (rid, event_type, details) in enumerate(self.events):
            if rid != run_id:
                continue
            rows.append({
                "run_id": rid,
                "event_time_ms": idx,
                "event_type": event_type,
                "details_json": json.dumps(details),
            })
        return list(reversed(rows))[:limit]

    async def get_events_by_types(self, run_id, event_types, limit=30):
        allowed = set(event_types)
        rows = await self.get_events(run_id, limit=max(limit, len(self.events)))
        return [row for row in rows if row["event_type"] in allowed][:limit]

    async def count_events_since(self, event_type, since_ms, details_like=None):
        total = 0
        for _run_id, et, details in self.events:
            if et != event_type:
                continue
            if details_like and details_like not in json.dumps(details, ensure_ascii=False):
                continue
            total += 1
        return total

    async def get_completed_runs_since(self, since_ms, limit=200):
        return list(self.recent_completed_runs)[:limit]


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
            "reduceOnly": reduce_only,
        }
        self._next_order_id += 1
        self.open_orders.append(order)
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

    class FillAfterPostOnlyClient(FakeClient):
        async def create_limit_order_raw(self, *args, **kwargs):
            order = await super().create_limit_order_raw(*args, **kwargs)
            self.position = None
            self.open_orders = [o for o in self.open_orders if o.get("orderId") != order["orderId"]]
            return order

    client = FillAfterPostOnlyClient()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_recovery_enabled=False,
            mainnet_trail_exit_use_maker=True,
            mainnet_trail_exit_maker_ttl_seconds=1,
        ),
        client, repo, FakeTelegramApp(),
    )
    # The maker order fills before the first poll sees the position.
    client.position = PositionInfo(
        symbol="ETHUSDC", position_amt=-0.071, entry_price=100.3, mark_price=100.05,
        unrealized_pnl=0.0178, liquidation_price=120.0, leverage=75, margin_type="cross",
    )
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
async def test_codex_damage_control_uses_maker_first_even_below_profit_floor():
    """Survival damage-control is a timed exit, so it should try maker first
    even when the trade is already slightly underwater, then fall back to market
    after the short survival TTL."""
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=99.95,
        unrealized_pnl=-0.006,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    client.book = {"bidPrice": "99.95", "askPrice": "100.05"}
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_recovery_enabled=False,
            mainnet_codex_survival_exit_use_maker=True,
            mainnet_codex_survival_exit_maker_ttl_seconds=0,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _run(
        avg_entry_price=100.0,
        signal_json='{"side":"LONG","codex_v1":{"lane_code":"S1P-L","lane":"codex_v1_s1_bbrsi_ordinary_pullback_long_pre_vwap"}}',
    )

    await manager._close_position("ETHUSDC", "SELL", 0.12, "CODEX_DAMAGE_CONTROL", run)

    assert any(o.get("timeInForce") == "GTX" for o in client.all_orders)
    assert len(client.market_orders) == 1
    assert client.market_orders[0]["side"] == "SELL"
    assert any(
        et == "trail_maker_timeout" and details.get("reason") == "CODEX_DAMAGE_CONTROL"
        for _, et, details in repo.events
    )
    assert not any(et == "trail_maker_chase_floor" for _, et, _ in repo.events)


@pytest.mark.asyncio
async def test_early_fail_uses_maker_first_without_profit_floor():
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=99.97,
        unrealized_pnl=-0.004,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    client.book = {"bidPrice": "99.97", "askPrice": "100.05"}
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_recovery_enabled=False,
            mainnet_codex_survival_exit_use_maker=True,
            mainnet_codex_survival_exit_maker_ttl_seconds=0,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _run(avg_entry_price=100.0)

    await manager._close_position("ETHUSDC", "SELL", 0.12, "CODEX_EARLY_FAIL", run)

    assert any(o.get("timeInForce") == "GTX" for o in client.all_orders)
    assert len(client.market_orders) == 1
    assert any(et == "survival_maker_attempt" for _, et, _ in repo.events)
    assert any(
        et == "survival_maker_fallback_market" and details.get("fallback_reason") == "timeout"
        for _, et, details in repo.events
    )


@pytest.mark.asyncio
async def test_v139_w1b_survival_delays_loss_cut_until_900s(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=99.84,
        unrealized_pnl=-0.0192,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    client.position = position
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_recovery_enabled=False,
            mainnet_codex_survival_exit_use_maker=False,
            mainnet_codex_survival_watch_after_seconds=300,
            mainnet_codex_survival_exit_after_seconds=420,
            mainnet_codex_survival_force_after_seconds=420,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    signal = {
        "side": "LONG",
        "codex_v1": {"enabled": True, "lane_code": "W1B", "lane": "w1_lane_s1long_score65_80_rng35_55_e0"},
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.12)
    hold_start_ms = 1_700_000_000_000
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 600_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "LONG",
        99.84,
        100.0,
        0.12,
        "SELL",
        hold_start_ms,
    )

    assert fired is False
    assert not client.market_orders
    watch_event = next(details for _, event_type, details in repo.events if event_type == "codex_survival_watch")
    assert watch_event["survival_profile"] == "v139_w1b_delayed"
    assert watch_event["lane_code"] == "W1B"

    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 901_000) / 1000.0)
    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "LONG",
        99.84,
        100.0,
        0.12,
        "SELL",
        hold_start_ms,
    )

    assert fired is True
    assert len(client.market_orders) == 1
    exit_event = next(details for _, event_type, details in repo.events if event_type == "codex_survival_exit")
    assert exit_event["reason"] == "CODEX_EARLY_FAIL"
    assert exit_event["survival_profile"] == "v139_w1b_delayed"
    assert exit_event["lane_code"] == "W1B"

@pytest.mark.asyncio
async def test_v139b_wpr_survival_scratch_after_mfe_retrace(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=100.004,
        unrealized_pnl=0.00048,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    client.position = position
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(mainnet_recovery_enabled=False, mainnet_codex_survival_exit_use_maker=False),
        client,
        repo,
        FakeTelegramApp(),
    )
    signal = {
        "side": "LONG",
        "codex_v1": {"enabled": True, "lane_code": "CNL-WPR-L", "lane": "v139_canary_watch_pre_reprice_long_s1"},
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.12)
    manager._trail_peak[run["run_id"]] = 100.04
    hold_start_ms = 1_700_000_000_000
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 120_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "LONG",
        100.004,
        100.0,
        0.12,
        "SELL",
        hold_start_ms,
    )

    assert fired is True
    assert len(client.market_orders) == 1
    exit_event = next(details for _, event_type, details in repo.events if event_type == "codex_survival_exit")
    assert exit_event["reason"] == "CNL_WPR_SCRATCH"
    assert exit_event["survival_profile"] == "v139b_wpr_waiting_scratch"
    assert exit_event["mfe_bp"] == pytest.approx(4.0)
    assert exit_event["current_bp"] == pytest.approx(0.4)


@pytest.mark.asyncio
async def test_v139b_wpr_survival_damage_after_240s(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=99.94,
        unrealized_pnl=-0.0072,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    client.position = position
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(mainnet_recovery_enabled=False, mainnet_codex_survival_exit_use_maker=False),
        client,
        repo,
        FakeTelegramApp(),
    )
    signal = {
        "side": "LONG",
        "codex_v1": {"enabled": True, "lane_code": "CNL-WPR-L", "lane": "v139_canary_watch_pre_reprice_long_s1"},
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.12)
    hold_start_ms = 1_700_000_000_000
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 241_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "LONG",
        99.94,
        100.0,
        0.12,
        "SELL",
        hold_start_ms,
    )

    assert fired is True
    assert len(client.market_orders) == 1
    exit_event = next(details for _, event_type, details in repo.events if event_type == "codex_survival_exit")
    assert exit_event["reason"] == "CNL_WPR_DAMAGE_CONTROL"
    assert exit_event["survival_profile"] == "v139b_wpr_waiting_scratch"
    assert exit_event["current_bp"] == pytest.approx(-6.0)

@pytest.mark.asyncio
async def test_position_flat_no_close_order():
    client = FakeClient()
    client.position = None
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(mainnet_codex_survival_exit_use_maker=True),
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _run(avg_entry_price=100.0)

    await manager._close_position("ETHUSDC", "SELL", 0.12, "CODEX_DAMAGE_CONTROL", run)

    assert client.all_orders == []
    assert client.market_orders == []
    assert any(et == "close_skipped_position_flat" for _, et, _ in repo.events)
    assert any(et == "survival_exit_done" and d.get("mode") == "already_flat" for _, et, d in repo.events)


async def _assert_market_only_close(reason):
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=99.8,
        unrealized_pnl=-0.024,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_survival_exit_use_maker=True,
            mainnet_codex_survival_exit_maker_ttl_seconds=5,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    await manager._close_position("ETHUSDC", "SELL", 0.12, reason, _run(avg_entry_price=100.0))
    assert len(client.market_orders) == 1
    assert not any(o.get("timeInForce") == "GTX" for o in client.all_orders)
    assert not any(et == "survival_maker_attempt" for _, et, _ in repo.events)


@pytest.mark.asyncio
async def test_sl_skips_survival_maker():
    await _assert_market_only_close("SL")


@pytest.mark.asyncio
async def test_adverse_exit_skips_survival_maker():
    await _assert_market_only_close("ADVERSE_EXIT")


@pytest.mark.asyncio
async def test_max_hold_skips_survival_maker():
    await _assert_market_only_close("MAX_HOLD_LOSS")


@pytest.mark.asyncio
async def test_tp_cancelled_before_survival_maker_close():
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=99.95,
        unrealized_pnl=-0.006,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    run = _run(avg_entry_price=100.0)
    client.open_orders.append({"orderId": 901, "clientOrderId": f"{run['run_id']}_tp1", "status": "NEW"})
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_survival_exit_use_maker=True,
            mainnet_codex_survival_exit_maker_ttl_seconds=0,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )

    await manager._close_position("ETHUSDC", "SELL", 0.12, "CODEX_DAMAGE_CONTROL", run)

    assert client.cancelled[0] == ("ETHUSDC", 901)
    assert any(o.get("timeInForce") == "GTX" for o in client.all_orders)
    assert len(client.market_orders) == 1


@pytest.mark.asyncio
async def test_no_orphan_exit_order_after_timeout():
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=99.95,
        unrealized_pnl=-0.006,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_survival_exit_use_maker=True,
            mainnet_codex_survival_exit_maker_ttl_seconds=0,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _run(avg_entry_price=100.0)

    await manager._close_position("ETHUSDC", "SELL", 0.12, "CODEX_DAMAGE_CONTROL", run)

    trail_orders = [o for o in client.all_orders if o.get("clientOrderId") == f"{run['run_id']}_trail"]
    assert len(trail_orders) == 1
    assert not any(o.get("clientOrderId") == f"{run['run_id']}_trail" for o in client.open_orders)
    assert ("ETHUSDC", trail_orders[0]["orderId"]) in client.cancelled


@pytest.mark.asyncio
async def test_post_only_reject_fallback_market():
    class RejectPostOnlyClient(FakeClient):
        async def create_limit_order_raw(self, *args, **kwargs):
            raise GTXSlippageExceeded("post-only rejected")

    client = RejectPostOnlyClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=99.95,
        unrealized_pnl=-0.006,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(mainnet_codex_survival_exit_use_maker=True),
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _run(avg_entry_price=100.0)

    await manager._close_position("ETHUSDC", "SELL", 0.12, "CODEX_DAMAGE_CONTROL", run)

    assert len(client.market_orders) == 1
    assert any(
        et == "survival_maker_fallback_market" and details.get("fallback_reason") == "place_failed"
        for _, et, details in repo.events
    )


@pytest.mark.asyncio
async def test_survival_maker_adverse_break_fallback_market(monkeypatch):
    from src.gridbot.mainnet import one_run as _mod

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(_mod.asyncio, "sleep", _no_sleep)

    class WorseningAfterMakerClient(FakeClient):
        async def create_limit_order_raw(self, *args, **kwargs):
            order = await super().create_limit_order_raw(*args, **kwargs)
            self.position = PositionInfo(
                symbol="ETHUSDC",
                position_amt=0.12,
                entry_price=100.0,
                mark_price=99.90,
                unrealized_pnl=-0.012,
                liquidation_price=80.0,
                leverage=75,
                margin_type="cross",
            )
            return order

    client = WorseningAfterMakerClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=99.95,
        unrealized_pnl=-0.006,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_survival_exit_use_maker=True,
            mainnet_codex_survival_exit_maker_ttl_seconds=1,
            mainnet_codex_survival_exit_adverse_break_bp=1.5,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _run(avg_entry_price=100.0)

    await manager._close_position("ETHUSDC", "SELL", 0.12, "CODEX_DAMAGE_CONTROL", run)

    assert any(o.get("timeInForce") == "GTX" for o in client.all_orders)
    assert len(client.market_orders) == 1
    assert any(et == "survival_maker_adverse_break" for _, et, _ in repo.events)
    assert any(
        et == "survival_maker_fallback_market"
        and details.get("fallback_reason") == "adverse_break"
        and details.get("adverse_break_base_bp") is not None
        and details.get("adverse_break_threshold_bp") is not None
        for _, et, details in repo.events
    )


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


@pytest.mark.asyncio
async def test_w6a_pre_submit_filters_and_risk_cap(monkeypatch):
    """Test W6A pre-submit filters: bad payoff geometry, risk cap sizing, deep down extension, negative drift."""
    from dataclasses import replace
    from src.gridbot.strategy.wildcat_live import WildcatLiveDecision
    from src.gridbot.strategy.long_pullback import SignalPlan
    client = FakeClient()
    repo = FakeRepo()
    settings = _settings(
        mainnet_one_run_enabled=True,
        mainnet_codex_v1_enabled=True,
        mainnet_codex_v1_w6a_target_max_gross_loss_usdc=0.16,
        mainnet_codex_v1_w6a_guarded_200cap_enabled=False,
        mainnet_codex_v137_w6a_risk_shadow_enabled=False,
    )
    manager = MainnetOneRunManager(settings, client, repo, FakeTelegramApp())

    signal = SignalPlan(
        action="BUY",
        confidence=1,
        score=80,
        symbol="ETHUSDC",
        price=3000.0,
        rsi=45.0,
        atr=10.0,
        support=2980.0,
        vwap=3005.0,
        entries=[3000.0],
        entry_weights=[1.0],
        stop_loss=2988.0,
        take_profits=[3015.0],
        planned_notional_usdc=200.0,
        planned_margin_usdc=0.533,
        planned_qty=0.0667,
        risk_amount_usdc=0.8,
        reasons=["wildcat:S1_BB_RSI"],
        risk_notes=[]
    )
    decision = WildcatLiveDecision(
        signal=signal,
        strategy="S1_BB_RSI",
        side="LONG",
        tp_pct=0.005,
        sl_pct=0.004,
        partial_exit_pct=0.4,
        partial_tp_pct=0.0005,
        recovery_steps=1,
        recovery_trigger_pct=0.0005,
        recovery_tp_shrink=0.45,
        adverse_exit_bars=10,
        adverse_exit_loss_pct=0.0007,
        max_holding_bars=24,
        params_label="default"
    )

    features = {
        "symbol": "ETHUSDC",
        "strategy": "S1_BB_RSI",
        "side": "LONG",
        "score": 80,
        "rng15": 40.0,
        "d30": -35.0,
        "adv3": 1.0,
        "rsi": 45.0,
        "vwap_dist_bp": -10.0,
        "pullback_from_recent_high_bp": 15.0,
        "maker_fee_bp": 0.0,
        "kill_switch": "off",
        "open_position": "false",
        "open_entry_order": "0",
        "open_reduce_order": "",
    }

    from src.gridbot.strategy.codex_v1_live import CodexV1Decision as StrategyDecision
    def mock_select(feat):
        return StrategyDecision(
            accepted=True,
            version="_codex_v1.2.11",
            baseline="baseline",
            lane="w6_lane_s1long_rng38_86_range9_15_e0",
            lane_code="W6A",
            strategy="S1_BB_RSI",
            side="LONG",
            entry_offset_bp=0.0,
            size_mult=1.0,
            notional_mult=1.0,
            requested_notional_usdc=200.0,
            reason="accepted"
        )

    import src.gridbot.mainnet.one_run as or_mod
    monkeypatch.setattr(or_mod, "select_codex_v1_lane", mock_select)

    run = {
        "run_id": "test_run_w6a",
        "symbol": "ETHUSDC",
        "status": "ARMED",
        "strategy_label": "codex_v1"
    }

    async def _mock_load(symbol):
        return []
    monkeypatch.setattr(manager, "_load_candles", _mock_load)

    async def _mock_feat(*args, **kwargs):
        return features
    monkeypatch.setattr(manager, "_build_codex_v1_live_features_for_decision", _mock_feat)

    adjusted, raw_dec, codex_dec, final_feat = await manager._apply_codex_v1_gate(
        run, decision, [], rng15=40.0, drift_bp=-10.0
    )

    assert adjusted is not None
    assert codex_dec.accepted
    assert codex_dec.notional_mult == pytest.approx(0.20, abs=0.01)
    assert "w6a_risk_capped" in codex_dec.risk_tags
    assert codex_dec.metrics["planned_rr"] == pytest.approx(1.25, abs=0.01)

    decision = replace(decision, signal=replace(decision.signal, stop_loss=2980.0))
    adjusted, raw_dec, codex_dec, final_feat = await manager._apply_codex_v1_gate(
        run, decision, [], rng15=40.0, drift_bp=-10.0
    )
    assert adjusted is None
    assert not codex_dec.accepted
    assert codex_dec.reason == "w6a_bad_payoff_geometry_blocked"

    decision = replace(decision, signal=replace(decision.signal, stop_loss=2988.0))
    features["d30"] = -35.0
    features["vwap_dist_bp"] = -50.0
    features["pullback_from_recent_high_bp"] = 35.0
    adjusted, raw_dec, codex_dec, final_feat = await manager._apply_codex_v1_gate(
        run, decision, [], rng15=40.0, drift_bp=-35.0
    )
    assert adjusted is None
    assert codex_dec.reason == "w6a_deep_down_extension_long_blocked"

    features["rsi"] = 35.0
    features["vwap_dist_bp"] = -110.0
    features["pullback_from_recent_high_bp"] = 45.0
    features["adv3"] = 3.0
    features["rng15"] = 75.0
    adjusted, raw_dec, codex_dec, final_feat = await manager._apply_codex_v1_gate(
        run, decision, [], rng15=75.0, drift_bp=-35.0
    )
    assert adjusted is not None
    assert codex_dec.accepted
    assert "w6a_capitulation_bounce" in codex_dec.risk_tags


@pytest.mark.asyncio
async def test_v137_w6a_risk_shadow_sizing_tree(monkeypatch):
    from dataclasses import replace

    from src.gridbot.strategy.codex_v1_live import CodexV1Decision as StrategyDecision
    from src.gridbot.strategy.long_pullback import SignalPlan
    from src.gridbot.strategy.wildcat_live import WildcatLiveDecision

    client = FakeClient()
    repo = FakeRepo()
    settings = _settings(
        mainnet_one_run_enabled=True,
        mainnet_codex_v1_enabled=True,
        mainnet_codex_v1_max_notional_usdc=800.0,
    )
    manager = MainnetOneRunManager(settings, client, repo, FakeTelegramApp())

    signal = SignalPlan(
        action="BUY",
        confidence=1,
        score=75,
        symbol="ETHUSDC",
        price=3000.0,
        rsi=45.0,
        atr=10.0,
        support=2980.0,
        vwap=3005.0,
        entries=[3000.0],
        entry_weights=[1.0],
        stop_loss=2993.94,
        take_profits=[3003.0],
        planned_notional_usdc=200.0,
        planned_margin_usdc=2.6667,
        planned_qty=0.0667,
        risk_amount_usdc=0.4,
        reasons=["wildcat:S1_BB_RSI"],
        risk_notes=[],
    )
    decision = WildcatLiveDecision(
        signal=signal,
        strategy="S1_BB_RSI",
        side="LONG",
        tp_pct=0.001,
        sl_pct=0.00202,
        partial_exit_pct=0.4,
        partial_tp_pct=0.0005,
        recovery_steps=1,
        recovery_trigger_pct=0.0005,
        recovery_tp_shrink=0.45,
        adverse_exit_bars=10,
        adverse_exit_loss_pct=0.0007,
        max_holding_bars=24,
        params_label="default",
    )
    features = {
        "symbol": "ETHUSDC",
        "strategy": "S1_BB_RSI",
        "side": "LONG",
        "score": 75.0,
        "rng15": 50.0,
        "d30": -20.0,
        "adv3": -3.0,
        "rsi": 45.0,
        "vwap_dist_bp": -20.0,
        "bb_lower_dist_bp": 14.0,
        "pullback_from_recent_high_bp": 20.0,
        "price_above_or_reclaimed_vwap": 1.0,
        "setup_age_sec": 80.0,
        "reprice_wait_elapsed_seconds": 10.0,
        "maker_fee_bp": 0.0,
        "kill_switch": "off",
        "open_position": "false",
        "open_entry_order": "0",
        "open_reduce_order": "",
    }
    current_codex = StrategyDecision(
        accepted=True,
        version="_codex_v1.3.7E_w6a_risk_shadow",
        baseline="baseline",
        lane="w6_lane_s1long_rng38_86_range9_15_e0",
        lane_code="W6A",
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=200.0,
        reason="accepted",
    )

    def mock_select(_feat):
        return current_codex

    import src.gridbot.mainnet.one_run as or_mod

    monkeypatch.setattr(or_mod, "select_codex_v1_lane", mock_select)

    async def _mock_feat(*args, **kwargs):
        return dict(features)

    monkeypatch.setattr(manager, "_build_codex_v1_live_features_for_decision", _mock_feat)
    run = {"run_id": "test_v137_w6a", "symbol": "ETHUSDC", "status": "ARMED", "strategy_label": "codex_v1"}

    adjusted, _raw_dec, codex_dec, _final_feat = await manager._apply_codex_v1_gate(
        run, decision, [], rng15=50.0, drift_bp=-20.0
    )
    assert adjusted is not None
    assert adjusted.signal.planned_notional_usdc == pytest.approx(200.0)
    assert codex_dec.policy_tag == "v137_w6a_200_promo_keep"
    assert codex_dec.metrics["risk_score"] == 0
    assert codex_dec.metrics["eligible_200"] is True

    features["setup_age_sec"] = 181.0
    adjusted, _raw_dec, codex_dec, _final_feat = await manager._apply_codex_v1_gate(
        run, decision, [], rng15=50.0, drift_bp=-20.0
    )
    assert adjusted is not None
    assert adjusted.signal.planned_notional_usdc == pytest.approx(50.0)
    assert codex_dec.policy_tag == "v137_w6a_200_promo_downgrade50"
    assert codex_dec.metrics["live_action"] == "DOWNGRADE50"

    features.update({
        "setup_age_sec": 80.0,
        "price_above_or_reclaimed_vwap": 0.0,
        "pullback_from_recent_high_bp": 25.0,
        "d30": -30.0,
        "vwap_dist_bp": -20.0,
    })
    adjusted, _raw_dec, codex_dec, _final_feat = await manager._apply_codex_v1_gate(
        run, decision, [], rng15=50.0, drift_bp=-30.0
    )
    assert adjusted is not None
    assert adjusted.signal.planned_notional_usdc == pytest.approx(50.0)
    assert codex_dec.policy_tag == "v137_w6a_risk_score_force50"
    assert codex_dec.metrics["risk_score"] == 3

    features.update({
        "setup_age_sec": 600.0,
        "reprice_wait_elapsed_seconds": 30.0,
        "price_above_or_reclaimed_vwap": 0.0,
        "pullback_from_recent_high_bp": 20.0,
        "d30": -20.0,
        "vwap_dist_bp": -30.0,
    })
    adjusted, _raw_dec, codex_dec, _final_feat = await manager._apply_codex_v1_gate(
        run, decision, [], rng15=50.0, drift_bp=-20.0
    )
    assert adjusted is not None
    assert adjusted.signal.planned_notional_usdc == pytest.approx(50.0)
    assert codex_dec.policy_tag == "v137_w6a_stale_hard_cap50"
    assert codex_dec.metrics["stale_hard"] is True
    assert codex_dec.metrics["setup_age_sec"] == pytest.approx(600.0)

    features.update({
        "setup_age_sec": 600.0,
        "price_above_or_reclaimed_vwap": 0.0,
        "pullback_from_recent_high_bp": 25.0,
        "d30": -30.0,
        "vwap_dist_bp": -45.0,
    })
    adjusted, _raw_dec, codex_dec, _final_feat = await manager._apply_codex_v1_gate(
        run, decision, [], rng15=50.0, drift_bp=-30.0
    )
    assert adjusted is None
    assert codex_dec.reason == "v137_w6a_risk_score_block"
    assert codex_dec.metrics["risk_score"] >= 4


@pytest.mark.asyncio
async def test_v130_w2a_is_shadow_only(monkeypatch):
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision as StrategyDecision
    from src.gridbot.strategy.long_pullback import SignalPlan
    from src.gridbot.strategy.wildcat_live import WildcatLiveDecision

    manager = MainnetOneRunManager(
        _settings(mainnet_one_run_enabled=True, mainnet_codex_v1_enabled=True),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    signal = SignalPlan(
        action="BUY",
        confidence=1,
        score=67,
        symbol="ETHUSDC",
        price=3000.0,
        rsi=44.0,
        atr=10.0,
        support=2980.0,
        vwap=3005.0,
        entries=[3000.0],
        entry_weights=[1.0],
        stop_loss=2992.0,
        take_profits=[3003.0],
        planned_notional_usdc=200.0,
        planned_margin_usdc=2.6667,
        planned_qty=0.0667,
        risk_amount_usdc=0.4,
        reasons=["wildcat:S1_BB_RSI"],
        risk_notes=[],
    )
    decision = WildcatLiveDecision(
        signal=signal,
        strategy="S1_BB_RSI",
        side="LONG",
        tp_pct=0.001,
        sl_pct=0.0026,
        partial_exit_pct=0.4,
        partial_tp_pct=0.0005,
        recovery_steps=1,
        recovery_trigger_pct=0.0005,
        recovery_tp_shrink=0.45,
        adverse_exit_bars=10,
        adverse_exit_loss_pct=0.0007,
        max_holding_bars=24,
        params_label="default",
    )
    codex = StrategyDecision(
        accepted=True,
        version="_codex_v1.3.0_w6a_guarded_200cap",
        baseline="baseline",
        lane="w2_lane_s1long_score64_74_rng35_55_e0_block",
        lane_code="W2A",
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=200.0,
        reason="accepted",
    )

    import src.gridbot.mainnet.one_run as or_mod

    monkeypatch.setattr(or_mod, "select_codex_v1_lane", lambda _feat: codex)

    async def _mock_feat(*args, **kwargs):
        return {
            "symbol": "ETHUSDC",
            "strategy": "S1_BB_RSI",
            "side": "LONG",
            "score": 67.0,
            "rng15": 48.0,
            "d30": -10.0,
            "adv3": 4.0,
            "bb_lower_dist_bp": 13.0,
            "maker_fee_bp": 0.0,
            "kill_switch": "off",
            "open_position": "false",
            "open_entry_order": "0",
            "open_reduce_order": "",
        }

    monkeypatch.setattr(manager, "_build_codex_v1_live_features_for_decision", _mock_feat)
    run = {"run_id": "test_v130_w2a", "symbol": "ETHUSDC", "status": "ARMED", "strategy_label": "codex_v1"}

    adjusted, raw_dec, codex_dec, _final_feat = await manager._apply_codex_v1_gate(
        run, decision, [], rng15=48.0, drift_bp=-10.0
    )

    assert raw_dec.accepted
    assert raw_dec.lane_code == "W2A"
    assert adjusted is None
    assert not codex_dec.accepted
    assert codex_dec.reason == "v130_w2a_shadow_only"
    assert codex_dec.policy_tag == "v130_w2a_shadow_only"
    assert codex_dec.shadow_lane == "SH_W2A_SHADOW_ONLY"


@pytest.mark.asyncio
async def test_v130_w2a_shadow_outcome_logger_tp_first(monkeypatch):
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision as StrategyDecision
    from src.gridbot.strategy.long_pullback import Candle, SignalPlan
    from src.gridbot.strategy.wildcat_live import WildcatLiveDecision

    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(mainnet_one_run_enabled=True, mainnet_codex_v1_enabled=True),
        FakeClient(),
        repo,
        FakeTelegramApp(),
    )
    signal = SignalPlan(
        action="BUY",
        confidence=1,
        score=67,
        symbol="ETHUSDC",
        price=3000.0,
        rsi=44.0,
        atr=10.0,
        support=2980.0,
        vwap=3005.0,
        entries=[3000.0],
        entry_weights=[1.0],
        stop_loss=2992.0,
        take_profits=[3003.0],
        planned_notional_usdc=200.0,
        planned_margin_usdc=2.6667,
        planned_qty=0.0667,
        risk_amount_usdc=0.4,
        reasons=["wildcat:S1_BB_RSI"],
        risk_notes=[],
    )
    decision = WildcatLiveDecision(
        signal=signal,
        strategy="S1_BB_RSI",
        side="LONG",
        tp_pct=0.001,
        sl_pct=0.0026,
        partial_exit_pct=0.4,
        partial_tp_pct=0.0005,
        recovery_steps=1,
        recovery_trigger_pct=0.0005,
        recovery_tp_shrink=0.45,
        adverse_exit_bars=10,
        adverse_exit_loss_pct=0.0007,
        max_holding_bars=24,
        params_label="default",
    )
    codex = StrategyDecision(
        accepted=True,
        version="_codex_v1.3.0_w6a_guarded_200cap",
        baseline="baseline",
        lane="w2_lane_s1long_score64_74_rng35_55_e0_block",
        lane_code="W2A",
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=200.0,
        reason="accepted",
    )

    import src.gridbot.mainnet.one_run as or_mod

    monkeypatch.setattr(or_mod, "select_codex_v1_lane", lambda _feat: codex)

    async def _mock_feat(*args, **kwargs):
        return {
            "symbol": "ETHUSDC",
            "strategy": "S1_BB_RSI",
            "side": "LONG",
            "score": 67.0,
            "rng15": 48.0,
            "d30": -10.0,
            "adv3": 4.0,
            "bb_lower_dist_bp": 13.0,
            "maker_fee_bp": 0.0,
            "kill_switch": "off",
            "open_position": "false",
            "open_entry_order": "0",
            "open_reduce_order": "",
        }

    monkeypatch.setattr(manager, "_build_codex_v1_live_features_for_decision", _mock_feat)
    run = {"run_id": "test_v130_w2a_shadow_tp", "symbol": "ETHUSDC", "status": "ARMED", "strategy_label": "codex_v1"}

    adjusted, _raw_dec, codex_dec, _final_feat = await manager._apply_codex_v1_gate(
        run, decision, [], rng15=48.0, drift_bp=-10.0
    )

    assert adjusted is None
    assert codex_dec.shadow_lane == "SH_W2A_SHADOW_ONLY"
    assert any(event[1] == "entry_codex_v1_shadow_sample_started" for event in repo.events)
    sample = next(iter(manager._codex_v1_shadow_samples.values()))
    start_ms = int(sample["start_ms"])
    await manager._update_codex_v1_shadow_outcomes(
        run,
        [
            Candle(start_ms, 3000.0, 3001.0, 2999.0, 3000.5, 1.0),
            Candle(start_ms + 60_000, 3000.5, 3003.5, 3000.2, 3003.2, 1.0),
        ],
    )

    outcome_event = [event for event in repo.events if event[1] == "entry_codex_v1_shadow_outcome"][-1]
    assert outcome_event[2]["shadow_lane"] == "SH_W2A_SHADOW_ONLY"
    assert outcome_event[2]["shadow_outcome"] == "tp1_first"
    assert not manager._codex_v1_shadow_samples


@pytest.mark.asyncio
async def test_v137_w6a_risk_block_shadow_outcome_logger_sl_first(monkeypatch):
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision as StrategyDecision
    from src.gridbot.strategy.long_pullback import Candle, SignalPlan
    from src.gridbot.strategy.wildcat_live import WildcatLiveDecision

    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_one_run_enabled=True,
            mainnet_codex_v1_enabled=True,
            mainnet_codex_v1_max_notional_usdc=800.0,
        ),
        FakeClient(),
        repo,
        FakeTelegramApp(),
    )
    signal = SignalPlan(
        action="BUY",
        confidence=1,
        score=75,
        symbol="ETHUSDC",
        price=3000.0,
        rsi=45.0,
        atr=10.0,
        support=2980.0,
        vwap=3005.0,
        entries=[3000.0],
        entry_weights=[1.0],
        stop_loss=2993.94,
        take_profits=[3003.0],
        planned_notional_usdc=200.0,
        planned_margin_usdc=2.6667,
        planned_qty=0.0667,
        risk_amount_usdc=0.4,
        reasons=["wildcat:S1_BB_RSI"],
        risk_notes=[],
    )
    decision = WildcatLiveDecision(
        signal=signal,
        strategy="S1_BB_RSI",
        side="LONG",
        tp_pct=0.001,
        sl_pct=0.00202,
        partial_exit_pct=0.4,
        partial_tp_pct=0.0005,
        recovery_steps=1,
        recovery_trigger_pct=0.0005,
        recovery_tp_shrink=0.45,
        adverse_exit_bars=10,
        adverse_exit_loss_pct=0.0007,
        max_holding_bars=24,
        params_label="default",
    )
    codex = StrategyDecision(
        accepted=True,
        version="_codex_v1.3.7E_w6a_risk_shadow",
        baseline="baseline",
        lane="w6_lane_s1long_rng38_86_range9_15_e0",
        lane_code="W6A",
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=200.0,
        reason="accepted",
    )

    import src.gridbot.mainnet.one_run as or_mod

    monkeypatch.setattr(or_mod, "select_codex_v1_lane", lambda _feat: codex)

    async def _mock_feat(*args, **kwargs):
        return {
            "symbol": "ETHUSDC",
            "strategy": "S1_BB_RSI",
            "side": "LONG",
            "score": 75.0,
            "rng15": 72.0,
            "d30": -30.0,
            "adv3": -3.0,
            "rsi": 45.0,
            "vwap_dist_bp": -45.0,
            "pullback_from_recent_high_bp": 25.0,
            "price_above_or_reclaimed_vwap": 0.0,
            "setup_age_sec": 600.0,
            "reprice_wait_elapsed_seconds": 30.0,
            "maker_fee_bp": 0.0,
            "kill_switch": "off",
            "open_position": "false",
            "open_entry_order": "0",
            "open_reduce_order": "",
        }

    monkeypatch.setattr(manager, "_build_codex_v1_live_features_for_decision", _mock_feat)
    run = {"run_id": "test_v137_w6a_shadow_sl", "symbol": "ETHUSDC", "status": "ARMED", "strategy_label": "codex_v1"}

    adjusted, _raw_dec, codex_dec, _final_feat = await manager._apply_codex_v1_gate(
        run, decision, [], rng15=72.0, drift_bp=-30.0
    )

    assert adjusted is None
    assert codex_dec.reason == "v137_w6a_risk_score_block"
    assert codex_dec.shadow_lane == "SH_W6A_RISK_SCORE_V1"
    assert any(event[1] == "entry_codex_v1_shadow_sample_started" for event in repo.events)
    sample = next(iter(manager._codex_v1_shadow_samples.values()))
    start_ms = int(sample["start_ms"])
    await manager._update_codex_v1_shadow_outcomes(
        run,
        [
            Candle(start_ms, 3000.0, 3000.5, 2999.5, 3000.0, 1.0),
            Candle(start_ms + 60_000, 3000.0, 3000.2, 2990.0, 2991.0, 1.0),
        ],
    )

    outcome_event = [event for event in repo.events if event[1] == "entry_codex_v1_shadow_outcome"][-1]
    assert outcome_event[2]["shadow_lane"] == "SH_W6A_RISK_SCORE_V1"
    assert outcome_event[2]["shadow_outcome"] == "sl_first"
    assert not manager._codex_v1_shadow_samples


def test_v130_no_lane_shadow_lane_bucket_mapping():
    from types import SimpleNamespace

    decision = SimpleNamespace(side="LONG", strategy="S1_BB_RSI")
    raw = SimpleNamespace(lane_code=None, side="LONG", strategy="S1_BB_RSI")
    assert (
        MainnetOneRunManager._codex_v1_no_lane_shadow_lane(
            "no_codex_v1_lane_match",
            decision,
            raw,
            {"reprice_wait_elapsed_seconds": 60.0},
        )
        == "NL-WATCH_PRE_REPRICE"
    )
    assert (
        MainnetOneRunManager._codex_v1_no_lane_shadow_lane(
            "no_codex_v1_lane_match",
            decision,
            raw,
            {
                "reprice_wait_elapsed_seconds": 310.0,
                "reprice_favorable_bp": 7.0002,
                "reprice_adverse_bp": 5.451,
                "vwap_dist_bp": -25.338893,
                "pullback_from_recent_high_bp": 25.292735,
            },
        )
        == "NL-L1_ADVERSE_REPRICE_MR_LONG"
    )
    assert (
        MainnetOneRunManager._codex_v1_no_lane_shadow_lane(
            "no_codex_v1_lane_match",
            decision,
            raw,
            {"reprice_wait_elapsed_seconds": 310.0, "reprice_favorable_bp": 2.0, "reprice_adverse_bp": 1.0},
        )
        == "NL-UNCLASSIFIED"
    )


@pytest.mark.asyncio
async def test_v130_no_lane_shadow_collector_logs_outcome(monkeypatch):
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision as StrategyDecision
    from src.gridbot.strategy.long_pullback import Candle, SignalPlan
    from src.gridbot.strategy.wildcat_live import WildcatLiveDecision

    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(mainnet_one_run_enabled=True, mainnet_codex_v1_enabled=True),
        FakeClient(),
        repo,
        FakeTelegramApp(),
    )
    signal = SignalPlan(
        action="BUY",
        confidence=1,
        score=82,
        symbol="ETHUSDC",
        price=3000.0,
        rsi=49.0,
        atr=10.0,
        support=2980.0,
        vwap=3005.0,
        entries=[3000.0],
        entry_weights=[1.0],
        stop_loss=2992.0,
        take_profits=[3003.0],
        planned_notional_usdc=200.0,
        planned_margin_usdc=2.6667,
        planned_qty=0.0667,
        risk_amount_usdc=0.4,
        reasons=["wildcat:S1_BB_RSI"],
        risk_notes=[],
    )
    decision = WildcatLiveDecision(
        signal=signal,
        strategy="S1_BB_RSI",
        side="LONG",
        tp_pct=0.001,
        sl_pct=0.0026,
        partial_exit_pct=0.4,
        partial_tp_pct=0.0005,
        recovery_steps=1,
        recovery_trigger_pct=0.0005,
        recovery_tp_shrink=0.45,
        adverse_exit_bars=10,
        adverse_exit_loss_pct=0.0007,
        max_holding_bars=24,
        params_label="default",
    )
    codex = StrategyDecision(
        accepted=False,
        version="_codex_v1.3.0_w6a_guarded_200cap",
        baseline="baseline",
        lane=None,
        lane_code=None,
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=None,
        size_mult=0.0,
        notional_mult=0.0,
        requested_notional_usdc=0.0,
        reason="no_codex_v1_lane_match",
    )

    import src.gridbot.mainnet.one_run as or_mod

    monkeypatch.setattr(or_mod, "select_codex_v1_lane", lambda _feat: codex)

    async def _mock_feat(*args, **kwargs):
        return {
            "symbol": "ETHUSDC",
            "strategy": "S1_BB_RSI",
            "side": "LONG",
            "score": 82.0,
            "rng15": 30.0,
            "d30": 40.0,
            "adv3": 2.0,
            "range_bp": 3.0,
            "bb_lower_dist_bp": 12.0,
            "vwap_dist_bp": -20.0,
            "maker_fee_bp": 0.0,
            "kill_switch": "off",
            "open_position": "false",
            "open_entry_order": "0",
            "open_reduce_order": "",
        }

    monkeypatch.setattr(manager, "_build_codex_v1_live_features_for_decision", _mock_feat)
    run = {"run_id": "test_v130_nolane_shadow", "symbol": "ETHUSDC", "status": "ARMED", "strategy_label": "codex_v1"}

    adjusted, _raw_dec, codex_dec, _final_feat = await manager._apply_codex_v1_gate(
        run, decision, [], rng15=30.0, drift_bp=18.0
    )

    assert adjusted is None
    assert codex_dec.reason == "no_codex_v1_lane_match"
    start_event = [event for event in repo.events if event[1] == "entry_codex_v1_shadow_sample_started"][-1]
    assert start_event[2]["shadow_lane"] == "SH_UNC_L_S1"
    assert start_event[2]["shadow_lane_family"] == "NL"
    assert start_event[2]["candidate_lane"] == "NL-UNCLASSIFIED"
    sample = next(iter(manager._codex_v1_shadow_samples.values()))
    start_ms = int(sample["start_ms"])
    await manager._update_codex_v1_shadow_outcomes(
        run,
        [
            Candle(start_ms, 3000.0, 3001.0, 2999.0, 3000.5, 1.0),
            Candle(start_ms + 60_000, 3000.5, 3003.5, 3000.2, 3003.2, 1.0),
        ],
    )

    outcome_event = [event for event in repo.events if event[1] == "entry_codex_v1_shadow_outcome"][-1]
    assert outcome_event[2]["shadow_lane"] == "SH_UNC_L_S1"
    assert outcome_event[2]["candidate_lane"] == "NL-UNCLASSIFIED"
    assert outcome_event[2]["shadow_outcome"] == "tp1_first"


@pytest.mark.asyncio
async def test_v139_reprice_shadow_canary_promotes_wpr_to_live50(monkeypatch):
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision as StrategyDecision
    from src.gridbot.strategy.long_pullback import SignalPlan
    from src.gridbot.strategy.wildcat_live import WildcatLiveDecision

    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(mainnet_one_run_enabled=True, mainnet_codex_v1_enabled=True, mainnet_codex_v1_max_notional_usdc=800.0),
        FakeClient(),
        repo,
        FakeTelegramApp(),
    )

    async def _no_v136(raw_codex_decision, features):
        return raw_codex_decision

    monkeypatch.setattr(manager, "_codex_v136_maybe_promote_nl_near_w1d_live200", _no_v136)
    signal = SignalPlan(
        action="BUY",
        confidence=1,
        score=82,
        symbol="ETHUSDC",
        price=3000.0,
        rsi=49.0,
        atr=10.0,
        support=2980.0,
        vwap=3005.0,
        entries=[3000.0],
        entry_weights=[1.0],
        stop_loss=2992.0,
        take_profits=[3003.0],
        planned_notional_usdc=200.0,
        planned_margin_usdc=2.6667,
        planned_qty=0.0667,
        risk_amount_usdc=0.4,
        reasons=["wildcat:S1_BB_RSI"],
        risk_notes=[],
    )
    decision = WildcatLiveDecision(
        signal=signal,
        strategy="S1_BB_RSI",
        side="LONG",
        tp_pct=0.001,
        sl_pct=0.0026,
        partial_exit_pct=0.4,
        partial_tp_pct=0.0005,
        recovery_steps=1,
        recovery_trigger_pct=0.0005,
        recovery_tp_shrink=0.45,
        adverse_exit_bars=10,
        adverse_exit_loss_pct=0.0007,
        max_holding_bars=24,
        params_label="default",
    )
    codex = StrategyDecision(
        accepted=False,
        version="_codex_v1.3.9C_live_admission_guard",
        baseline="baseline",
        lane=None,
        lane_code=None,
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=None,
        size_mult=0.0,
        notional_mult=0.0,
        requested_notional_usdc=0.0,
        reason="no_codex_v1_lane_match",
    )

    import src.gridbot.mainnet.one_run as or_mod

    monkeypatch.setattr(or_mod, "select_codex_v1_lane", lambda _feat: codex)

    async def _mock_feat(*args, **kwargs):
        return {
            "symbol": "ETHUSDC",
            "strategy": "S1_BB_RSI",
            "side": "LONG",
            "score": 82.0,
            "rng15": 30.0,
            "d30": 40.0,
            "adv3": 2.0,
            "range_bp": 3.0,
            "bb_lower_dist_bp": 12.0,
            "vwap_dist_bp": -20.0,
            "reprice_wait_elapsed_seconds": 30.0,
            "maker_fee_bp": 0.0,
            "kill_switch": "off",
            "open_position": "false",
            "open_entry_order": "0",
            "open_reduce_order": "",
        }

    monkeypatch.setattr(manager, "_build_codex_v1_live_features_for_decision", _mock_feat)
    run = {"run_id": "test_v139_wpr_canary", "symbol": "ETHUSDC", "status": "ARMED", "strategy_label": "codex_v1"}

    adjusted, raw_dec, codex_dec, _final_feat = await manager._apply_codex_v1_gate(
        run, decision, [], rng15=30.0, drift_bp=40.0
    )

    assert raw_dec.reason == "no_codex_v1_lane_match"
    assert adjusted is not None
    assert codex_dec.accepted
    assert codex_dec.lane_code == "CNL-WPR-L"
    assert codex_dec.shadow_lane == "SH_WPR_L_S1"
    assert codex_dec.reason == "v139_reprice_canary_promoted"
    assert codex_dec.policy_tag == "v139_reprice_tiny_canary"
    assert codex_dec.requested_notional_usdc == pytest.approx(50.0)
    assert codex_dec.entry_offset_bp == pytest.approx(3.0)
    assert adjusted.signal.planned_notional_usdc == pytest.approx(50.0)
    assert adjusted.signal.entries == [pytest.approx(2999.1)]
    assert adjusted.signal.stop_loss == pytest.approx(2999.1 * (1 - 8.0 / 10_000.0))
    assert adjusted.sl_pct == pytest.approx(8.0 / 10_000.0)
    assert adjusted.partial_tp_pct == pytest.approx(0.00030)
    assert adjusted.partial_exit_pct == pytest.approx(0.60)
    assert "no_dca" in codex_dec.risk_tags
    assert "v139b_wpr_waiting_scratch" in codex_dec.risk_tags
    assert codex_dec.metrics["canary_daily_count_24h"] is None
    assert codex_dec.metrics["canary_daily_cap"] is None
    assert codex_dec.metrics["wpr_profile"] == "v139b_wpr_waiting_scratch"
    accepted_event = next(details for _, event_type, details in repo.events if event_type == "entry_codex_v1_accepted")
    assert accepted_event["effective_execution"]["lane_code"] == "CNL-WPR-L"
    assert accepted_event["decision"]["metrics"]["canary_policy"] == "v139_reprice_tiny_canary"


@pytest.mark.asyncio
async def test_v139c_wpr_down_tape_blocks_live_promotion():
    from types import SimpleNamespace
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision

    manager = MainnetOneRunManager(
        _settings(mainnet_one_run_enabled=True, mainnet_codex_v1_enabled=True),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    raw = CodexV1Decision(
        accepted=False,
        version="test",
        baseline="test",
        lane=None,
        lane_code=None,
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=None,
        size_mult=0.0,
        notional_mult=0.0,
        requested_notional_usdc=0.0,
        reason="no_codex_v1_lane_match",
    )
    features = {
        "symbol": "ETHUSDC",
        "strategy": "S1_BB_RSI",
        "side": "LONG",
        "score": 72.0,
        "rng15": 31.6,
        "d30": -23.6,
        "adv3": 2.4,
        "vwap_dist_bp": -7.4,
        "price_above_or_reclaimed_vwap": 0.0,
        "rsi": 37.5,
        "reprice_wait_elapsed_seconds": 30.0,
        "maker_fee_bp": 0.0,
    }

    codex_dec = await manager._codex_v139_maybe_promote_reprice_canary(
        SimpleNamespace(side="LONG", strategy="S1_BB_RSI"),
        raw,
        raw,
        features,
    )

    assert not codex_dec.accepted
    assert codex_dec.lane_code == "CNL-WPR-L"
    assert codex_dec.shadow_lane == "SH_WPR_L_S1"
    assert codex_dec.reason == "wpr_down_tape_block"
    assert codex_dec.policy_tag == "wpr_down_tape_block"
    assert codex_dec.metrics["wpr_guard_reason"] == "wpr_down_tape_block"


@pytest.mark.asyncio
async def test_v139c_disabled_lanes_match_lane_code(monkeypatch):
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision
    from src.gridbot.strategy.long_pullback import SignalPlan
    from src.gridbot.strategy.wildcat_live import WildcatLiveDecision

    manager = MainnetOneRunManager(
        _settings(
            mainnet_one_run_enabled=True,
            mainnet_codex_v1_enabled=True,
            mainnet_codex_v1_disabled_lanes="W1B",
        ),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    signal = SignalPlan(
        action="SELL",
        confidence=1,
        score=72,
        symbol="ETHUSDC",
        price=3000.0,
        rsi=53.0,
        atr=10.0,
        support=2980.0,
        vwap=2998.0,
        entries=[3000.0],
        entry_weights=[1.0],
        stop_loss=3006.0,
        take_profits=[2997.0],
        planned_notional_usdc=200.0,
        planned_margin_usdc=2.6667,
        planned_qty=0.0667,
        risk_amount_usdc=0.4,
        reasons=["wildcat:S1_BB_RSI"],
        risk_notes=[],
    )
    decision = WildcatLiveDecision(
        signal=signal,
        strategy="S1_BB_RSI",
        side="SHORT",
        tp_pct=0.001,
        sl_pct=0.0026,
        partial_exit_pct=0.4,
        partial_tp_pct=0.0005,
        recovery_steps=1,
        recovery_trigger_pct=0.0005,
        recovery_tp_shrink=0.45,
        adverse_exit_bars=10,
        adverse_exit_loss_pct=0.0007,
        max_holding_bars=24,
        params_label="default",
    )
    codex = CodexV1Decision(
        accepted=True,
        version="test",
        baseline="test",
        lane="w1_lane_s1short_score71_76_range3_9_e0_advopen",
        lane_code="W1B",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=200.0,
        reason="accepted",
    )

    import src.gridbot.mainnet.one_run as or_mod

    monkeypatch.setattr(or_mod, "select_codex_v1_lane", lambda _feat: codex)

    async def _mock_feat(*args, **kwargs):
        return {
            "symbol": "ETHUSDC",
            "strategy": "S1_BB_RSI",
            "side": "SHORT",
            "score": 72.0,
            "rng15": 22.0,
            "d30": 2.0,
            "adv3": -2.0,
            "vwap_dist_bp": 4.0,
            "maker_fee_bp": 0.0,
            "kill_switch": "off",
            "open_position": "false",
            "open_entry_order": "0",
            "open_reduce_order": "",
        }

    monkeypatch.setattr(manager, "_build_codex_v1_live_features_for_decision", _mock_feat)
    run = {"run_id": "test_v139c_w1b_disabled", "symbol": "ETHUSDC", "status": "ARMED", "strategy_label": "codex_v1"}

    adjusted, _raw_dec, codex_dec, _final_feat = await manager._apply_codex_v1_gate(
        run, decision, [], rng15=22.0, drift_bp=2.0
    )

    assert adjusted is None
    assert not codex_dec.accepted
    assert codex_dec.lane_code == "W1B"
    assert codex_dec.reason == "codex_v1_lane_disabled"
    assert "live_lane_disabled" in codex_dec.risk_tags


@pytest.mark.asyncio
async def test_v136_nl_near_w1d_promotes_to_live200(monkeypatch):
    from src.gridbot.strategy.long_pullback import SignalPlan
    from src.gridbot.strategy.wildcat_live import WildcatLiveDecision

    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(mainnet_one_run_enabled=True, mainnet_codex_v1_enabled=True, mainnet_codex_v1_max_notional_usdc=800.0),
        FakeClient(),
        repo,
        FakeTelegramApp(),
    )
    signal = SignalPlan(
        action="BUY",
        confidence=1,
        score=66,
        symbol="ETHUSDC",
        price=3000.0,
        rsi=55.0,
        atr=10.0,
        support=2980.0,
        vwap=2990.0,
        entries=[3000.0],
        entry_weights=[1.0],
        stop_loss=2992.0,
        take_profits=[3003.0],
        planned_notional_usdc=200.0,
        planned_margin_usdc=2.6667,
        planned_qty=0.0667,
        risk_amount_usdc=0.4,
        reasons=["wildcat:S1_BB_RSI"],
        risk_notes=[],
    )
    decision = WildcatLiveDecision(
        signal=signal,
        strategy="S1_BB_RSI",
        side="LONG",
        tp_pct=0.001,
        sl_pct=0.0026,
        partial_exit_pct=0.4,
        partial_tp_pct=0.0005,
        recovery_steps=1,
        recovery_trigger_pct=0.0005,
        recovery_tp_shrink=0.45,
        adverse_exit_bars=10,
        adverse_exit_loss_pct=0.0007,
        max_holding_bars=24,
        params_label="default",
    )

    async def _mock_feat(*args, **kwargs):
        return {
            "symbol": "ETHUSDC",
            "strategy": "S1_BB_RSI",
            "side": "LONG",
            "score": 66.0,
            "rng15": 90.0,
            "d30": 0.0,
            "adv3": 0.0,
            "range_bp": 4.0,
            "d3": 0.0,
            "d5": 0.0,
            "rsi": 55.0,
            "bb_lower_dist_bp": 20.0,
            "vwap_dist_bp": 4.0,
            "pullback_from_recent_high_bp": 4.0,
            "price_above_or_reclaimed_vwap": 1.0,
            "maker_fee_bp": 0.0,
            "kill_switch": "off",
            "open_position": "false",
            "open_entry_order": "0",
            "open_reduce_order": "",
        }

    monkeypatch.setattr(manager, "_build_codex_v1_live_features_for_decision", _mock_feat)
    run = {"run_id": "test_v136_w1d_live200", "symbol": "ETHUSDC", "status": "ARMED", "strategy_label": "codex_v1"}

    adjusted, raw_dec, codex_dec, _final_feat = await manager._apply_codex_v1_gate(
        run, decision, [], rng15=90.0, drift_bp=0.0
    )

    assert raw_dec.reason == "no_codex_v1_lane_match"
    assert adjusted is not None
    assert codex_dec.accepted
    assert codex_dec.lane_code == "SH_NL_NEAR_W1D_LONG_LIVE200"
    assert codex_dec.reason == "nl_near_w1d_live200_promoted"
    assert codex_dec.requested_notional_usdc == pytest.approx(200.0)
    assert adjusted.signal.planned_notional_usdc == pytest.approx(200.0)
    assert adjusted.signal.entries == [pytest.approx(3000.0)]
    assert "no_dca" in codex_dec.risk_tags
    assert "no_taker_fallback" in codex_dec.risk_tags
    accepted_event = next(details for _, event_type, details in repo.events if event_type == "entry_codex_v1_accepted")
    assert accepted_event["raw_classifier"]["reason"] == "no_codex_v1_lane_match"
    assert accepted_event["effective_execution"]["lane_code"] == "SH_NL_NEAR_W1D_LONG_LIVE200"


@pytest.mark.asyncio
async def test_v139c_nl_near_w1d_without_vwap_reclaim_stays_shadow():
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision

    manager = MainnetOneRunManager(
        _settings(mainnet_one_run_enabled=True, mainnet_codex_v1_enabled=True),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    raw = CodexV1Decision(
        accepted=False,
        version="test",
        baseline="test",
        lane=None,
        lane_code=None,
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=None,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=0.0,
        reason="no_codex_v1_lane_match",
    )
    features = {
        "symbol": "ETHUSDC",
        "strategy": "S1_BB_RSI",
        "side": "LONG",
        "score": 70.0,
        "rng15": 23.2,
        "d30": -9.7,
        "adv3": 12.0,
        "range_bp": 25.6,
        "rsi": 42.5,
        "bb_lower_dist_bp": 1.6,
        "vwap_dist_bp": -5.1,
        "pullback_from_recent_high_bp": 15.4,
        "price_above_or_reclaimed_vwap": 0.0,
    }

    codex_dec = await manager._codex_v136_maybe_promote_nl_near_w1d_live200(raw, features)

    assert not codex_dec.accepted
    assert codex_dec.lane_code == "SH_NL_NEAR_W1D_LONG_LIVE200"
    assert codex_dec.reason == "nl_near_w1d_reclaim_guard_shadow"
    assert codex_dec.policy_tag == "nl_near_w1d_reclaim_guard_shadow"
    assert "reclaim_guard_shadow" in codex_dec.risk_tags


@pytest.mark.asyncio
async def test_v139c_nl_near_w1d_reclaim_but_weak_d30_caps_to_50():
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision

    manager = MainnetOneRunManager(
        _settings(mainnet_one_run_enabled=True, mainnet_codex_v1_enabled=True),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    raw = CodexV1Decision(
        accepted=False,
        version="test",
        baseline="test",
        lane=None,
        lane_code=None,
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=None,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=0.0,
        reason="no_codex_v1_lane_match",
    )
    features = {
        "symbol": "ETHUSDC",
        "strategy": "S1_BB_RSI",
        "side": "LONG",
        "score": 66.0,
        "rng15": 90.0,
        "d30": -12.0,
        "adv3": 0.0,
        "range_bp": 4.0,
        "d3": 0.0,
        "d5": 0.0,
        "rsi": 55.0,
        "bb_lower_dist_bp": 20.0,
        "vwap_dist_bp": 2.0,
        "pullback_from_recent_high_bp": 4.0,
        "price_above_or_reclaimed_vwap": 1.0,
    }

    codex_dec = await manager._codex_v136_maybe_promote_nl_near_w1d_live200(raw, features)

    assert codex_dec.accepted
    assert codex_dec.lane_code == "SH_NL_NEAR_W1D_LONG_LIVE200"
    assert codex_dec.reason == "nl_near_w1d_cap50_reclaim_guard_promoted"
    assert codex_dec.requested_notional_usdc == pytest.approx(50.0)
    assert codex_dec.metrics["policy_tag"] == "nl_near_w1d_cap50_reclaim_guard"
    assert "fixed_50_usdc" in codex_dec.risk_tags


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_reason", "realized", "commission", "expected_reason", "expected_risk_tag", "expected_sl_count"),
    [
        ("SL", -0.20, 0.05, "nl_near_w1d_live200_sl_guard_block", "sl_guard_block", 1),
        ("TP_RETRACE", -0.36, 0.10, "nl_near_w1d_live200_net_loss_guard_block", "net_loss_guard_block", 0),
    ],
)
async def test_v136_nl_near_w1d_live200_loss_guard_blocks_promotion(
    exit_reason,
    realized,
    commission,
    expected_reason,
    expected_risk_tag,
    expected_sl_count,
):
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision

    repo = FakeRepo()
    repo.recent_completed_runs = [
        {
            "run_id": "prev_live200",
            "signal_json": json.dumps({"codex_v1": {"lane_code": "SH_NL_NEAR_W1D_LONG_LIVE200"}}),
            "realized_pnl_usdc": realized,
            "commission_usdc": commission,
            "exit_reason": exit_reason,
        }
    ]
    manager = MainnetOneRunManager(
        _settings(mainnet_one_run_enabled=True, mainnet_codex_v1_enabled=True),
        FakeClient(),
        repo,
        FakeTelegramApp(),
    )
    raw = CodexV1Decision(
        accepted=False,
        version="test",
        baseline="test",
        lane=None,
        lane_code=None,
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=None,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=0.0,
        reason="no_codex_v1_lane_match",
    )
    features = {
        "symbol": "ETHUSDC",
        "strategy": "S1_BB_RSI",
        "side": "LONG",
        "score": 66.0,
        "rng15": 90.0,
        "d30": 0.0,
        "adv3": 0.0,
        "range_bp": 4.0,
        "d3": 0.0,
        "d5": 0.0,
        "rsi": 55.0,
        "bb_lower_dist_bp": 20.0,
        "vwap_dist_bp": 4.0,
        "pullback_from_recent_high_bp": 4.0,
        "price_above_or_reclaimed_vwap": 1.0,
    }

    codex_dec = await manager._codex_v136_maybe_promote_nl_near_w1d_live200(raw, features)

    assert not codex_dec.accepted
    assert codex_dec.lane_code == "SH_NL_NEAR_W1D_LONG_LIVE200"
    assert codex_dec.reason == expected_reason
    assert codex_dec.policy_tag == expected_reason
    assert expected_risk_tag in codex_dec.risk_tags
    assert codex_dec.metrics["loss_guard_completed_count_24h"] == 1
    assert codex_dec.metrics["loss_guard_sl_count_24h"] == expected_sl_count
    assert codex_dec.metrics["loss_guard_net_pnl_24h_usdc"] == pytest.approx(realized - commission)


def test_v131_shadow_identity_excludes_run_id_from_opportunity():
    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    opp_a = manager._codex_v1_shadow_opportunity_id(
        "ETHUSDC", "SH_WPR_L_S1", "NL-WATCH_PRE_REPRICE", "LONG", "S1_BB_RSI", "waiting", 0, 123
    )
    opp_b = manager._codex_v1_shadow_opportunity_id(
        "ETHUSDC", "SH_WPR_L_S1", "NL-WATCH_PRE_REPRICE", "LONG", "S1_BB_RSI", "waiting", 0, 123
    )
    assert opp_a == opp_b
    assert "run" not in opp_a
    opp_tp_changed = manager._codex_v1_shadow_opportunity_id(
        "ETHUSDC", "SH_WPR_L_S1", "NL-WATCH_PRE_REPRICE", "LONG", "S1_BB_RSI", "waiting", 0, 123, tp_price_bucket=10
    )
    opp_version_changed = manager._codex_v1_shadow_opportunity_id(
        "ETHUSDC", "SH_WPR_L_S1", "NL-WATCH_PRE_REPRICE", "LONG", "S1_BB_RSI", "waiting", 0, 123, version_family="codex_v1.4"
    )
    assert opp_tp_changed != opp_a
    assert opp_version_changed != opp_a
    sample_a = manager._codex_v1_shadow_sample_id("run_a", 1000, opp_a, 3000.0, 3003.0, 2992.0)
    sample_b = manager._codex_v1_shadow_sample_id("run_b", 1000, opp_a, 3000.0, 3003.0, 2992.0)
    assert sample_a != sample_b


def test_v131_shadow_sample_cooldown_and_5bp_override():
    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    scope = "ETHUSDC:SH_WPR_L_S1:LONG"
    base = {
        "run_id": "run_1",
        "sample_scope_key": scope,
        "opportunity_id": "opp_a",
        "start_ms": 30_000,
        "entry_price": 3000.0,
        "entry_price_bucket": 0,
        "sampling_family": "NO_LANE",
    }
    assert manager._codex_v1_should_start_shadow_sample(base) == (True, None)
    manager._codex_v1_shadow_last_sample_by_scope[scope] = {
        "start_ms": 0,
        "entry_price": 3000.0,
        "entry_price_bucket": 0,
        "sampling_family": "NO_LANE",
    }
    assert manager._codex_v1_should_start_shadow_sample(base) == (False, "cooldown")
    moved = dict(base, opportunity_id="opp_b", entry_price=3002.0, entry_price_bucket=5)
    assert manager._codex_v1_should_start_shadow_sample(moved) == (True, None)


def test_v133_shadow_sample_5bp_override_respects_family_active_cap():
    manager = MainnetOneRunManager(
        _settings(mainnet_codex_v133_shadow_family_active_cap=1), FakeClient(), FakeRepo(), FakeTelegramApp()
    )
    scope = "ETHUSDC:SH_WPR_L_S1:LONG"
    manager._codex_v1_shadow_last_sample_by_scope[scope] = {
        "start_ms": 0,
        "entry_price": 3000.0,
        "entry_price_bucket": 0,
        "sampling_family": "NO_LANE",
    }
    manager._codex_v1_shadow_samples["active_1"] = {
        "run_id": "run_1",
        "opportunity_id": "opp_active",
        "sampling_family": "NO_LANE",
        "diagnostic_only": False,
    }
    sample = {
        "run_id": "run_1",
        "sample_scope_key": scope,
        "opportunity_id": "opp_new",
        "start_ms": 30_000,
        "entry_price": 3002.0,
        "entry_price_bucket": 5,
        "sampling_family": "NO_LANE",
    }
    assert manager._codex_v1_should_start_shadow_sample(sample) == (False, "family_active_cap")


def test_v131_shadow_first_touch_no_fill_limit_model():
    from src.gridbot.strategy.long_pullback import Candle

    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    sample = {
        "run_id": "run_nf",
        "sample_id": "sample_nf",
        "opportunity_id": "opp_nf",
        "side": "LONG",
        "entry_price": 3000.0,
        "tp_price": 3003.0,
        "sl_price": 2992.0,
        "fill_model": "limit_touch",
        "start_ms": 100_000,
        "entry_ttl_s": 60,
        "outcome_ttl_s": 300,
    }
    outcome = manager._codex_v1_shadow_first_touch(
        sample,
        [Candle(100_000, 3002.0, 3002.5, 3001.0, 3002.0, 1.0)],
    )
    assert outcome is not None
    assert outcome["shadow_outcome"] == "no_fill"
    assert outcome["filled"] is False



def test_v134_shadow_default_entry_ttl_is_180_seconds():
    from src.gridbot.strategy.long_pullback import Candle

    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    sample = {
        "run_id": "run_default_ttl",
        "sample_id": "sample_default_ttl",
        "opportunity_id": "opp_default_ttl",
        "side": "LONG",
        "entry_price": 3000.0,
        "tp_price": 3003.0,
        "sl_price": 2992.0,
        "fill_model": "limit_touch",
        "start_ms": 0,
        "outcome_ttl_s": 300,
    }
    candles_120s = [
        Candle(0, 3002.0, 3002.5, 3001.0, 3002.0, 1.0),
        Candle(60_000, 3002.0, 3002.5, 3001.0, 3002.0, 1.0),
    ]
    assert manager._codex_v1_shadow_first_touch(sample, candles_120s) is None

    outcome = manager._codex_v1_shadow_first_touch(
        sample,
        [*candles_120s, Candle(120_000, 3002.0, 3002.5, 3001.0, 3002.0, 1.0)],
    )
    assert outcome is not None
    assert outcome["shadow_outcome"] == "no_fill"
    assert outcome["elapsed_s"] == 180.0
@pytest.mark.asyncio
async def test_v133_diagnostic_shadow_outcome_is_not_promotion_eligible():
    repo = FakeRepo()
    manager = MainnetOneRunManager(_settings(), FakeClient(), repo, FakeTelegramApp())
    sample = {
        "run_id": "run_diag",
        "sample_id": "diag_1",
        "opportunity_id": "opp_diag",
        "shadow_lane": "SH_WPR_L_S1",
        "candidate_bucket": "NL_NEAR_RP1_LONG",
        "side": "LONG",
        "entry_price": 3000.0,
        "tp_price": 3003.0,
        "sl_price": 2992.0,
        "fill_model": "immediate_shadow",
        "diagnostic_only": True,
        "promotion_eligible": False,
        "features": {"maker_fee_bp": 0.0},
    }
    await manager._log_codex_v1_shadow_outcome(
        "diag_1",
        sample,
        {"shadow_outcome": "tp1_first", "exit_reference_price": 3003.0},
    )
    assert repo.events[-1][1] == "entry_codex_v1_shadow_outcome"
    assert repo.events[-1][2]["promotion_counts_as"] == "diagnostic_only"


def test_v131_shadow_first_touch_ambiguous_both():
    from src.gridbot.strategy.long_pullback import Candle

    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    sample = {
        "run_id": "run_ab",
        "sample_id": "sample_ab",
        "opportunity_id": "opp_ab",
        "side": "LONG",
        "entry_price": 3000.0,
        "tp_price": 3003.0,
        "sl_price": 2992.0,
        "fill_model": "immediate_shadow",
        "start_ms": 100_000,
        "entry_ttl_s": 60,
        "outcome_ttl_s": 300,
    }
    outcome = manager._codex_v1_shadow_first_touch(
        sample,
        [Candle(100_000, 3000.0, 3004.0, 2990.0, 3001.0, 1.0)],
    )
    assert outcome is not None
    assert outcome["shadow_outcome"] == "ambiguous_both"
    assert outcome["ambiguity_flag"] is True


def test_v131_shadow_mapping_disabled_and_short_veto():
    from types import SimpleNamespace

    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    decision = SimpleNamespace(side="LONG", strategy="S1_BB_RSI")
    raw = SimpleNamespace(lane_code="W1D", lane="w1_lane", side="LONG", strategy="S1_BB_RSI")
    codex = SimpleNamespace(
        metrics={}, shadow_lane="", lane_code="W1D", lane="w1_lane", side="LONG", strategy="S1_BB_RSI"
    )
    disabled = manager._codex_v1_map_block_to_shadow_lane(
        "codex_v1_lane_disabled", decision, raw, codex, {"side": "LONG", "strategy": "S1_BB_RSI"}
    )
    assert disabled["shadow_lane"] == "SH_DISABLED_LONG_S1"

    anchor_raw = SimpleNamespace(
        lane_code="ANCHOR-S",
        lane="anchor_s1_preblock_broad_su6_exitA",
        side="SHORT",
        strategy="S1_BB_RSI",
    )
    anchor_codex = SimpleNamespace(
        metrics={},
        shadow_lane="",
        lane_code="ANCHOR-S",
        lane="anchor_s1_preblock_broad_su6_exitA",
        side="SHORT",
        strategy="S1_BB_RSI",
    )
    anchor = manager._codex_v1_map_block_to_shadow_lane(
        "codex_v1_lane_disabled",
        SimpleNamespace(side="SHORT", strategy="S1_BB_RSI"),
        anchor_raw,
        anchor_codex,
        {"side": "SHORT", "strategy": "S1_BB_RSI"},
    )
    assert anchor["shadow_lane"] == "SH_ANCHOR_S_SAFE"
    assert anchor["candidate_lane"] == "ANCHOR-S"
    assert anchor["shadow_lane_family"] == "ANCHOR_S"
    assert "disabled_anchor_s_specialized_shadow" in anchor["secondary_reasons"]
    assert manager._codex_v1_shadow_priority(anchor["shadow_lane"]) == 4

    generic_short_raw = SimpleNamespace(lane_code="W5A", lane="w5_lane", side="SHORT", strategy="S1_BB_RSI")
    generic_short_codex = SimpleNamespace(
        metrics={}, shadow_lane="", lane_code="W5A", lane="w5_lane", side="SHORT", strategy="S1_BB_RSI"
    )
    generic_short = manager._codex_v1_map_block_to_shadow_lane(
        "codex_v1_lane_disabled",
        SimpleNamespace(side="SHORT", strategy="S1_BB_RSI"),
        generic_short_raw,
        generic_short_codex,
        {"side": "SHORT", "strategy": "S1_BB_RSI"},
    )
    assert generic_short["shadow_lane"] == "SH_DISABLED_SHORT_S1"

    short_decision = SimpleNamespace(side="SHORT", strategy="S1_BB_RSI")
    short_raw = SimpleNamespace(lane_code=None, lane=None, side="SHORT", strategy="S1_BB_RSI")
    short_codex = SimpleNamespace(metrics={}, shadow_lane="", lane_code=None, lane=None, side="SHORT", strategy="S1_BB_RSI")
    hot = manager._codex_v1_map_block_to_shadow_lane(
        "hot_up_extension_short_blocked",
        short_decision,
        short_raw,
        short_codex,
        {
            "side": "SHORT",
            "strategy": "S1_BB_RSI",
            "d30": 30.0,
            "rsi": 60.0,
            "vwap_dist_bp": 25.0,
            "bb_lower_dist_bp": 40.0,
        },
    )
    assert hot["shadow_lane"] == "SH_SHORT_HOT_UP_EXTENSION_S1"
    assert hot["candidate_lane"] == "hot_up_extension_short_blocked"

    mid_over_stale = manager._codex_v1_map_block_to_shadow_lane(
        "stale_short_after_upmove_blocked",
        short_decision,
        short_raw,
        short_codex,
        {
            "side": "SHORT",
            "strategy": "S1_BB_RSI",
            "reprice_wait_elapsed_seconds": 120.0,
            "d30": 16.0,
            "adv3": 8.0,
            "rsi": 60.0,
            "vwap_dist_bp": 10.0,
            "bb_lower_dist_bp": 25.0,
        },
    )
    assert mid_over_stale["shadow_lane"] == "SH_SHORT_MID_UP_EXTENSION_S1"
    assert mid_over_stale["candidate_lane"] == "mid_up_extension_short_blocked"
    assert "stale_short_after_upmove_blocked" in mid_over_stale["secondary_reasons"]

    s1p_raw = SimpleNamespace(lane_code="S1P-L", lane="codex_v1_s1_bbrsi_ordinary_pullback_long_pre_vwap", side="LONG", strategy="S1_BB_RSI")
    s1p_codex = SimpleNamespace(
        metrics={"shadow_lane": "SH_S1P_L_WAIT_GT180"},
        shadow_lane="SH_S1P_L_WAIT_GT180",
        lane_code="S1P-L",
        lane="codex_v1_s1_bbrsi_ordinary_pullback_long_pre_vwap",
        side="LONG",
        strategy="S1_BB_RSI",
    )
    s1p = manager._codex_v1_map_block_to_shadow_lane(
        "s1p_l_wait_gt180_block",
        SimpleNamespace(side="LONG", strategy="S1_BB_RSI"),
        s1p_raw,
        s1p_codex,
        {"side": "LONG", "strategy": "S1_BB_RSI", "reprice_wait_elapsed_seconds": 400.0},
    )
    assert s1p["shadow_lane"] == "SH_S1P_L_WAIT_GT180"
    assert s1p["candidate_lane"] == "S1P-L"
    assert s1p["shadow_lane_family"] == "S1P-L"
    assert manager._codex_v1_shadow_priority(s1p["shadow_lane"]) == 2


@pytest.mark.asyncio
async def test_v131_shadow_cap_replaces_lower_priority_sample():
    repo = FakeRepo()
    manager = MainnetOneRunManager(_settings(), FakeClient(), repo, FakeTelegramApp())
    run_id = "run_cap"
    for idx in range(manager.CODEX_V1_SHADOW_MAX_SAMPLES_PER_RUN):
        manager._codex_v1_shadow_samples[f"low_{idx}"] = {
            "run_id": run_id,
            "sample_id": f"low_{idx}",
            "opportunity_id": f"opp_low_{idx}",
            "shadow_lane": "SH_UNC_L_S1",
            "candidate_lane": "NL-UNCLASSIFIED",
            "sample_priority": 7,
            "start_ms": idx,
        }
    manager._codex_v1_shadow_sample_counts_by_run[run_id] = manager.CODEX_V1_SHADOW_MAX_SAMPLES_PER_RUN
    replacement = {
        "run_id": run_id,
        "sample_id": "hi_1",
        "opportunity_id": "opp_hi",
        "shadow_lane": "SH_WPR_L_S1",
        "candidate_lane": "NL-WATCH_PRE_REPRICE",
        "sample_priority": 1,
    }

    assert await manager._codex_v1_try_replace_lower_priority_shadow_sample(replacement) is True

    assert manager._codex_v1_shadow_sample_counts_by_run[run_id] == manager.CODEX_V1_SHADOW_MAX_SAMPLES_PER_RUN - 1
    assert len(manager._codex_v1_shadow_samples) == manager.CODEX_V1_SHADOW_MAX_SAMPLES_PER_RUN - 1
    assert repo.events[-1][1] == "entry_codex_v1_shadow_sample_dropped"
    assert repo.events[-1][2]["drop_reason"] == "replaced_by_higher_priority"


def test_v131_shadow_first_touch_skips_partial_start_candle():
    from src.gridbot.strategy.long_pullback import Candle

    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    sample = {
        "run_id": "run_partial",
        "sample_id": "sample_partial",
        "opportunity_id": "opp_partial",
        "side": "LONG",
        "entry_price": 3000.0,
        "tp_price": 3003.0,
        "sl_price": 2992.0,
        "fill_model": "immediate_shadow",
        "start_ms": 130_000,
        "entry_ttl_s": 60,
        "outcome_ttl_s": 300,
    }
    outcome = manager._codex_v1_shadow_first_touch(
        sample,
        [
            Candle(120_000, 3000.0, 3005.0, 2990.0, 3004.0, 1.0),
            Candle(180_000, 3004.0, 3004.5, 3003.5, 3004.0, 1.0),
        ],
    )

    assert outcome is not None
    assert outcome["shadow_outcome"] == "tp1_first"
    assert outcome["hit_candle_open_ms"] == 180_000

def test_v136_promoted_w1d_live200_entry_ttl_override_is_120_seconds():
    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    policy = manager._codex_v1_live_entry_ttl_policy(
        {"signal_json": {"codex_v1": {"enabled": True, "lane_code": "SH_NL_NEAR_W1D_LONG_LIVE200"}}}
    )

    assert policy["ttl_seconds"] == 120
    assert policy["ttl_source"] == "codex_v135_lane_override"
    assert policy["lane_code"] == "SH_NL_NEAR_W1D_LONG_LIVE200"


@pytest.mark.asyncio
async def test_v135_codex_entry_pending_uses_lane_ttl_override():
    import time

    now_ms = int(time.time() * 1000)
    client = FakeClient()
    client.open_orders = [
        {"orderId": 321, "symbol": "ETHUSDC", "clientOrderId": "cry3mn_test_entry", "price": "100.0"}
    ]
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_entry_order_ttl_seconds=45,
            mainnet_codex_v135_entry_ttl_by_lane_enabled=True,
            mainnet_codex_v135_entry_ttl_seconds_by_lane="S1P-L:180",
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _run(
        status="ENTRY_PENDING",
        entry_order_id=321,
        updated_at_ms=now_ms - 100_000,
        signal_json=json.dumps({
            "side": "LONG",
            "take_profit": 101.0,
            "stop_loss": 99.0,
            "codex_v1": {"enabled": True, "lane_code": "S1P-L"},
        }),
    )

    await manager._run_entry_pending(run)

    assert repo.completed == []
    assert client.cancelled == []
    assert not any(event_type == "entry_ttl_expired" for _, event_type, _ in repo.events)


@pytest.mark.asyncio
async def test_v135_codex_entry_pending_expires_at_lane_ttl():
    import time

    now_ms = int(time.time() * 1000)
    client = FakeClient()
    client.open_orders = [
        {"orderId": 321, "symbol": "ETHUSDC", "clientOrderId": "cry3mn_test_entry", "price": "100.0"}
    ]
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_entry_order_ttl_seconds=45,
            mainnet_codex_v135_entry_ttl_by_lane_enabled=True,
            mainnet_codex_v135_entry_ttl_seconds_by_lane="S1P-L:180",
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _run(
        status="ENTRY_PENDING",
        entry_order_id=321,
        updated_at_ms=now_ms - 181_000,
        signal_json=json.dumps({
            "side": "LONG",
            "take_profit": 101.0,
            "stop_loss": 99.0,
            "codex_v1": {"enabled": True, "lane_code": "S1P-L"},
        }),
    )

    await manager._run_entry_pending(run)

    assert client.cancelled == [("ETHUSDC", 321)]
    assert repo.completed[-1][0:3] == ("cry3mn_test", "ENTRY_EXPIRED", "entry_ttl_expired")
    ttl_event = next(details for _, event_type, details in repo.events if event_type == "entry_ttl_expired")
    assert ttl_event["entry_ttl_s"] == 180
    assert ttl_event["entry_ttl_source"] == "codex_v135_lane_override"
    assert ttl_event["lane_code"] == "S1P-L"
    assert ttl_event["entry_age_s"] >= 180


def test_v135_shadow_first_touch_records_entry_fill_age_bucket():
    from src.gridbot.strategy.long_pullback import Candle

    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    sample = {
        "run_id": "run_fill_age",
        "sample_id": "sample_fill_age",
        "opportunity_id": "opp_fill_age",
        "side": "LONG",
        "entry_price": 100.0,
        "tp_price": 101.0,
        "sl_price": 98.0,
        "fill_model": "limit_touch",
        "start_ms": 0,
        "entry_ttl_s": 180,
        "outcome_ttl_s": 300,
    }

    outcome = manager._codex_v1_shadow_first_touch(
        sample,
        [
            Candle(0, 100.4, 100.8, 99.8, 100.2, 1.0),
            Candle(60_000, 100.2, 101.2, 100.1, 101.0, 1.0),
        ],
    )

    assert outcome is not None
    assert outcome["shadow_outcome"] == "tp1_first"
    assert outcome["entry_fill_age_s"] == 60.0
    assert outcome["entry_fill_age_bucket"] == "45-90s"

@pytest.mark.asyncio
async def test_w6a_running_dynamic_exit(monkeypatch):
    """Test W6A running management: 25s weak-no-bounce exit and stop tightening."""
    client = FakeClient()
    repo = FakeRepo()
    settings = _settings(
        mainnet_codex_v1_enabled=True,
        mainnet_codex_v1_w6a_no_tp1_exit_shadow=True,
        mainnet_codex_v1_w6a_no_tp1_early_exit_live=True,
        mainnet_codex_v1_w6a_no_tp1_stop_tighten_live=True
    )
    manager = MainnetOneRunManager(settings, client, repo, FakeTelegramApp())

    import json
    run = {
        "run_id": "test_running_w6a",
        "symbol": "ETHUSDC",
        "status": "RUNNING",
        "side": "LONG",
        "qty": 0.05,
        "avg_entry_price": 3000.0,
        "signal_json": json.dumps({
            "stop_loss": 2900.0,
            "take_profits": [3150.0],
            "codex_v1": {
                "enabled": True,
                "lane_code": "W6A",
                "policy_note": "w6a_risk_capped"
            }
        })
    }

    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.05,
        entry_price=3000.0,
        mark_price=2960.0,
        unrealized_pnl=-2.0,
        liquidation_price=2500.0,
        leverage=75,
        margin_type="cross",
    )

    import time as _time
    now_ms = int(_time.time() * 1000)
    hold_start_ms = now_ms - 26000
    async def _mock_hold_start(*args, **kwargs):
        return hold_start_ms
    monkeypatch.setattr(manager, "_get_hold_start_ms", _mock_hold_start)

    manager._trail_peak[run["run_id"]] = 3005.0
    manager._w6a_price_history[run["run_id"]] = [
        (_time.time() - 10, 2970.0),
        (_time.time(), 2960.0)
    ]

    closed = []
    async def _mock_close(symbol, close_side, qty, reason, r):
        closed.append((qty, reason))
        return True
    monkeypatch.setattr(manager, "_close_position", _mock_close)

    await manager._run_running_manage(run, position, "ETHUSDC", 0.05, 0.05)

    assert len(closed) == 1
    assert closed[0][1] == "w6a_no_tp1_weak_no_bounce_early_exit"

    closed.clear()
    hold_start_ms = now_ms - 61000
    position.mark_price = 2975.0
    manager._trail_peak[run["run_id"]] = 3010.0
    manager._w6a_shadow_recorded.clear()

    cancelled = []
    placed = []
    async def _mock_cancel(symbol, r_id):
        cancelled.append(r_id)
    async def _mock_place(**kwargs):
        placed.append(kwargs)
    monkeypatch.setattr(manager, "_cancel_stop_loss_order", _mock_cancel)
    monkeypatch.setattr(manager, "_place_stop_loss_maker", _mock_place)

    await manager._run_running_manage(run, position, "ETHUSDC", 0.05, 0.05)

    assert len(placed) == 1
    assert placed[0]["sl_price"] == 2945.0
    assert manager._w6a_stop_tightened_runs[run["run_id"]] == -0.55






def test_v137_forces_tp_policy_live_override_off():
    settings = _settings(mainnet_codex_tp_policy_live_override_enabled=True)

    manager = MainnetOneRunManager(settings, FakeClient(), FakeRepo(), FakeTelegramApp())

    assert manager._settings.mainnet_codex_tp_policy_live_override_enabled is False


@pytest.mark.asyncio
async def test_v137_w6a_no_bounce_live_soft_exit_is_one_shot(monkeypatch):
    import time as _time

    client = FakeClient()
    repo = FakeRepo()
    settings = _settings(
        mainnet_codex_v1_enabled=True,
        mainnet_codex_v1_w6a_no_tp1_early_exit_live=False,
        mainnet_codex_v137_w6a_no_bounce_exit_live=True,
        mainnet_codex_v137_w6a_no_bounce_exit_shadow=True,
        mainnet_codex_v137_w6a_no_bounce_after_seconds=60.0,
    )
    manager = MainnetOneRunManager(settings, client, repo, FakeTelegramApp())
    run = {
        "run_id": "test_v137_no_bounce",
        "symbol": "ETHUSDC",
        "status": "RUNNING",
        "side": "LONG",
        "qty": 0.05,
        "avg_entry_price": 3000.0,
        "signal_json": json.dumps({
            "side": "LONG",
            "stop_loss": 2900.0,
            "take_profits": [3150.0],
            "codex_v1": {"enabled": True, "lane_code": "W6A", "policy_note": "v137_w6a_200_promo_keep"},
        }),
    }
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.05,
        entry_price=3000.0,
        mark_price=2950.0,
        unrealized_pnl=-2.5,
        liquidation_price=2500.0,
        leverage=75,
        margin_type="cross",
    )
    now_ms = int(_time.time() * 1000)

    async def _mock_hold_start(*args, **kwargs):
        return now_ms - 61_000

    monkeypatch.setattr(manager, "_get_hold_start_ms", _mock_hold_start)
    manager._trail_peak[run["run_id"]] = 3002.0
    manager._w6a_price_history[run["run_id"]] = [(_time.time() - 30, 2960.0)]
    closed = []

    async def _mock_close(symbol, close_side, qty, reason, r):
        closed.append((qty, reason))
        return True

    monkeypatch.setattr(manager, "_close_position", _mock_close)

    await manager._run_running_manage(run, position, "ETHUSDC", 0.05, 0.05)
    await manager._run_running_manage(run, position, "ETHUSDC", 0.05, 0.05)

    assert closed == [(0.05, "w6a_no_bounce_soft_exit_v2")]
    assert run["run_id"] in manager._w6a_no_bounce_exiting
    assert any(event_type == "no_bounce_exit_signal" for _, event_type, _ in repo.events)
    assert any(
        event_type == "w6a_exit_policy_shadow" and details.get("shadow_policy") == "SH_W6A_NO_BOUNCE_EXIT_V2"
        for _, event_type, details in repo.events
    )


@pytest.mark.asyncio
async def test_tp1_executable_touch_no_fill_audit_uses_book_bid():
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=100.05,
        unrealized_pnl=0.006,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    client.book = {"bidPrice": "100.06", "askPrice": "100.07"}
    client.open_orders = [
        {"orderId": 7001, "clientOrderId": "cry3mn_test_tp1", "origQty": "0.048", "executedQty": "0", "price": "100.05", "status": "NEW"},
    ]
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(mainnet_partial_tp_pct=0.0005, mainnet_partial_exit_pct=0.40),
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _run(
        signal_json=json.dumps({"side": "LONG", "take_profit": 101.0, "stop_loss": 99.0}),
        avg_entry_price=100.0,
    )

    await manager._sync_take_profit_orders(run, client.position, json.loads(run["signal_json"]))

    audit_events = [event for event in repo.events if event[1] in {"tp1_touch_no_fill_audit", "tp1_cross_no_fill_audit"}]
    assert audit_events
    event_type, details = audit_events[-1][1], audit_events[-1][2]
    assert event_type == "tp1_cross_no_fill_audit"
    assert details["best_bid"] == pytest.approx(100.06)
    assert details["tp1_price"] == pytest.approx(100.05)
    assert details["order_status"] == "NEW"
    assert details["partial_exit_seen"] is False


@pytest.mark.asyncio
async def test_v137_w6a_post_tp_probe_shadow_logs_after_tp1():
    import time as _time

    client = FakeClient()
    repo = FakeRepo()
    settings = _settings(
        mainnet_trail_enabled=False,
        mainnet_codex_survival_enabled=False,
        mainnet_partial_tp_pct=0.0005,
        mainnet_codex_v137_w6a_post_tp_probe_shadow=True,
        mainnet_codex_v137_w6a_post_tp_probe_giveback_bp="1.5,2.0,2.5",
    )
    manager = MainnetOneRunManager(settings, client, repo, FakeTelegramApp())
    run_id = "test_v137_post_tp_probe"
    run = _run(
        run_id=run_id,
        signal_json=json.dumps({
            "side": "LONG",
            "take_profit": 101.0,
            "stop_loss": 99.0,
            "wildcat": {"tp_pct": 0.004},
            "codex_v1": {"enabled": True, "lane_code": "W6A", "policy_note": "v137_w6a_200_promo_keep"},
        }),
        avg_entry_price=100.0,
        armed_at_ms=int(_time.time() * 1000) - 120_000,
    )
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.06,
        entry_price=100.0,
        mark_price=100.038,
        unrealized_pnl=0.00228,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    manager._partial_exits.add(run_id)
    manager._trail_peak[run_id] = 100.06

    await manager._run_running_manage(run, position, "ETHUSDC", 0.06, 0.06)

    event_types = [event_type for _, event_type, _ in repo.events]
    assert "post_tp_probe_eval" in event_types
    reduce_events = [details for _, event_type, details in repo.events if event_type == "post_tp_probe_reduce"]
    hold_events = [details for _, event_type, details in repo.events if event_type == "post_tp_probe_hold"]
    assert {details["threshold_bp"] for details in reduce_events} == {1.5, 2.0}
    assert {details["threshold_bp"] for details in hold_events} == {2.5}
    assert all(details["shadow_policy"] == "SH_W6A_POST_TP_PROBE_V1" for details in reduce_events + hold_events)
    assert all(details["runner_qty"] == pytest.approx(0.06) for details in reduce_events + hold_events)
    assert client.market_orders == []

@pytest.mark.asyncio
async def test_v132_rehydrates_pending_tp_policy_sample_from_event_log():
    run_id = "cry3mn_restore"
    repo = FakeRepo()
    manager = MainnetOneRunManager(_settings(), FakeClient(), repo, FakeTelegramApp())
    sample = {
        "sample_id": "sample_restore",
        "run_id": run_id,
        "opportunity_id": "opp_restore",
        "symbol": "ETHUSDC",
        "shadow_lane_family": "W1D",
        "candidate_lane": "W1D",
        "shadow_lane": None,
        "side": "LONG",
        "strategy": "S1_BB_RSI",
        "start_ms": 1_000,
        "entry_price": 100.0,
        "tp_price": 101.0,
        "sl_price": 99.0,
        "fill_model": "immediate_shadow",
        "entry_ttl_s": 0,
        "requested_notional_usdc": 200.0,
        "features": {"maker_fee_bp": 0.0, "taker_fee_bp": 0.0},
    }

    await manager._start_codex_v132_tp_policy_sample(sample, source_type="shadow_sample")
    assert "sample_restore" in manager._codex_v132_tp_policy_samples

    restored = MainnetOneRunManager(_settings(), FakeClient(), repo, FakeTelegramApp())
    await restored._rehydrate_codex_v132_tp_policy_samples(_run(run_id=run_id))

    assert "sample_restore" in restored._codex_v132_tp_policy_samples
    assert restored._codex_v132_tp_policy_samples["sample_restore"]["rehydrated_from_event_log"] is True
    assert any(event_type == "entry_codex_v1_tp_policy_shadow_rehydrated" for _, event_type, _ in repo.events)


@pytest.mark.asyncio
async def test_v132_rehydrate_skips_terminal_tp_policy_sample():
    run_id = "cry3mn_restore_done"
    repo = FakeRepo()
    manager = MainnetOneRunManager(_settings(), FakeClient(), repo, FakeTelegramApp())
    sample = {
        "sample_id": "sample_restore_done",
        "run_id": run_id,
        "opportunity_id": "opp_restore_done",
        "symbol": "ETHUSDC",
        "shadow_lane_family": "W1D",
        "candidate_lane": "W1D",
        "shadow_lane": None,
        "side": "LONG",
        "strategy": "S1_BB_RSI",
        "start_ms": 1_000,
        "entry_price": 100.0,
        "tp_price": 101.0,
        "sl_price": 99.0,
        "fill_model": "immediate_shadow",
        "entry_ttl_s": 0,
        "requested_notional_usdc": 200.0,
        "features": {"maker_fee_bp": 0.0, "taker_fee_bp": 0.0},
    }

    await manager._start_codex_v132_tp_policy_sample(sample, source_type="shadow_sample")
    repo.events.append((
        run_id,
        "entry_codex_v1_tp_policy_shadow_outcome",
        {"paired_sample_id": "sample_restore_done", "tp_policy_id": "baseline"},
    ))

    restored = MainnetOneRunManager(_settings(), FakeClient(), repo, FakeTelegramApp())
    await restored._rehydrate_codex_v132_tp_policy_samples(_run(run_id=run_id))

    assert restored._codex_v132_tp_policy_samples == {}
    assert not any(event_type == "entry_codex_v1_tp_policy_shadow_rehydrated" for _, event_type, _ in repo.events)


@pytest.mark.asyncio
async def test_v132_rehydrate_retries_after_transient_event_read_failure():
    class FlakyRepo(FakeRepo):
        def __init__(self):
            super().__init__()
            self.fail_next_read = True

        async def get_events_by_types(self, run_id, event_types, limit=30):
            if self.fail_next_read:
                self.fail_next_read = False
                raise RuntimeError("database is locked")
            return await super().get_events_by_types(run_id, event_types, limit=limit)

    run_id = "cry3mn_restore_retry"
    repo = FlakyRepo()
    manager = MainnetOneRunManager(_settings(), FakeClient(), repo, FakeTelegramApp())
    sample = {
        "sample_id": "sample_restore_retry",
        "run_id": run_id,
        "opportunity_id": "opp_restore_retry",
        "symbol": "ETHUSDC",
        "shadow_lane_family": "W1D",
        "candidate_lane": "W1D",
        "shadow_lane": "W1D",
        "side": "LONG",
        "strategy": "S1_BB_RSI",
        "entry_price": 100.0,
        "tp_price": 101.0,
        "sl_price": 99.0,
        "fill_model": "immediate_shadow",
        "entry_ttl_s": 0,
        "requested_notional_usdc": 200.0,
        "features": {"maker_fee_bp": 0.0, "taker_fee_bp": 0.0},
    }
    await manager._start_codex_v132_tp_policy_sample(sample, source_type="shadow_sample")

    restored = MainnetOneRunManager(_settings(), FakeClient(), repo, FakeTelegramApp())
    await restored._rehydrate_codex_v132_tp_policy_samples(_run(run_id=run_id))
    assert run_id not in restored._codex_v132_rehydrated_runs
    assert restored._codex_v132_tp_policy_samples == {}

    await restored._rehydrate_codex_v132_tp_policy_samples(_run(run_id=run_id))
    assert "sample_restore_retry" in restored._codex_v132_tp_policy_samples
    assert run_id in restored._codex_v132_rehydrated_runs


@pytest.mark.asyncio
async def test_v132_rehydrate_filters_tp_policy_events_past_noise_limit():
    run_id = "cry3mn_restore_noise"
    repo = FakeRepo()
    manager = MainnetOneRunManager(_settings(), FakeClient(), repo, FakeTelegramApp())
    sample = {
        "sample_id": "sample_restore_noise",
        "run_id": run_id,
        "opportunity_id": "opp_restore_noise",
        "symbol": "ETHUSDC",
        "shadow_lane_family": "W1D",
        "candidate_lane": "W1D",
        "shadow_lane": "W1D",
        "side": "LONG",
        "strategy": "S1_BB_RSI",
        "entry_price": 100.0,
        "tp_price": 101.0,
        "sl_price": 99.0,
        "fill_model": "immediate_shadow",
        "entry_ttl_s": 0,
        "requested_notional_usdc": 200.0,
        "features": {"maker_fee_bp": 0.0, "taker_fee_bp": 0.0},
    }
    await manager._start_codex_v132_tp_policy_sample(sample, source_type="shadow_sample")
    for idx in range(1200):
        repo.events.append((run_id, "entry_codex_v1_skipped", {"idx": idx}))

    restored = MainnetOneRunManager(_settings(), FakeClient(), repo, FakeTelegramApp())
    await restored._rehydrate_codex_v132_tp_policy_samples(_run(run_id=run_id))

    assert "sample_restore_noise" in restored._codex_v132_tp_policy_samples


@pytest.mark.asyncio
async def test_v132_rehydrate_log_failure_does_not_block_run_cycle(monkeypatch):
    class ActiveRepo(FakeRepo):
        def __init__(self, run):
            super().__init__()
            self.run = run

        async def get_active_run(self):
            return self.run

        async def log_event(self, run_id, event_type, details):
            if event_type == "entry_codex_v1_tp_policy_shadow_rehydrated":
                raise RuntimeError("log write failed")
            await super().log_event(run_id, event_type, details)

    run_id = "cry3mn_restore_log_fail"
    run = _run(run_id=run_id, status="ARMED")
    repo = ActiveRepo(run)
    manager = MainnetOneRunManager(_settings(), FakeClient(), repo, FakeTelegramApp())
    sample = {
        "sample_id": "sample_restore_log_fail",
        "run_id": run_id,
        "opportunity_id": "opp_restore_log_fail",
        "symbol": "ETHUSDC",
        "shadow_lane_family": "W1D",
        "candidate_lane": "W1D",
        "shadow_lane": "W1D",
        "side": "LONG",
        "strategy": "S1_BB_RSI",
        "entry_price": 100.0,
        "tp_price": 101.0,
        "sl_price": 99.0,
        "fill_model": "immediate_shadow",
        "entry_ttl_s": 0,
        "requested_notional_usdc": 200.0,
        "features": {"maker_fee_bp": 0.0, "taker_fee_bp": 0.0},
    }
    starter = MainnetOneRunManager(_settings(), FakeClient(), repo, FakeTelegramApp())
    await starter._start_codex_v132_tp_policy_sample(sample, source_type="shadow_sample")
    called = {"armed": False}

    async def fake_run_armed(active):
        called["armed"] = True

    monkeypatch.setattr(manager, "_run_armed", fake_run_armed)
    await manager.run_cycle()

    assert called["armed"] is True
    assert "sample_restore_log_fail" in manager._codex_v132_tp_policy_samples
    assert repo.completed == []

def test_v137_w6a_risk_shadow_bypasses_legacy_research_block():
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision as StrategyDecision

    decision = StrategyDecision(
        accepted=True,
        version="_codex_v1.3.7E_w6a_risk_shadow",
        baseline="baseline",
        lane="w6_lane_s1long_rng38_86_range9_15_e0",
        lane_code="W6A",
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=200.0,
        reason="accepted",
    )
    weak_features = {
        "d30": -20.0,
        "adv3": 0.0,
        "rsi": 45.0,
        "vwap_dist_bp": -20.0,
        "pullback_from_recent_high_bp": 15.0,
    }

    v137_manager = MainnetOneRunManager(
        _settings(mainnet_codex_v137_w6a_risk_shadow_enabled=True),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    assert v137_manager._codex_v1_live_research_block_reason(decision, weak_features) is None

    legacy_manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_v137_w6a_risk_shadow_enabled=False,
            mainnet_codex_v134_w6a_weak_drift_50_canary_enabled=False,
        ),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    assert legacy_manager._codex_v1_live_research_block_reason(decision, weak_features) == "codex_v1_w6_weak_drift_block"

    deep_pullback_features = {
        "d30": -35.0,
        "adv3": 7.0,
        "rsi": 38.0,
        "vwap_dist_bp": -31.0,
        "pullback_from_recent_high_bp": 35.0,
    }
    assert (
        legacy_manager._codex_v1_live_research_block_reason(decision, deep_pullback_features)
        == "codex_v1_w6_deep_pullback_block"
    )

    no_reclaim_features = {
        "d30": -35.0,
        "adv3": 7.0,
        "rsi": 38.0,
        "vwap_dist_bp": -20.0,
        "pullback_from_recent_high_bp": 35.0,
        "price_above_or_reclaimed_vwap": 0.0,
    }
    assert (
        legacy_manager._codex_v1_live_research_block_reason(decision, no_reclaim_features)
        == "codex_v1_w6_deep_pullback_block"
    )


def test_v138_w6a_apply_codex_decision_uses_tp6_partial_exit():
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision
    from src.gridbot.strategy.long_pullback import SignalPlan
    from src.gridbot.strategy.wildcat_live import WildcatLiveDecision

    manager = MainnetOneRunManager(
        _settings(mainnet_codex_v138_w6a_partial_tp_pct=0.0006),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    signal = SignalPlan(
        action="BUY",
        confidence=80,
        score=80,
        symbol="ETHUSDC",
        price=100.0,
        rsi=45.0,
        atr=1.0,
        support=99.0,
        vwap=100.0,
        entries=[100.0],
        entry_weights=[1.0],
        stop_loss=99.0,
        take_profits=[100.1],
        planned_notional_usdc=200.0,
        planned_margin_usdc=2.6667,
        planned_qty=2.0,
        reasons=["wildcat:S1_BB_RSI"],
        risk_notes=[],
    )
    decision = WildcatLiveDecision(
        signal=signal,
        strategy="S1_BB_RSI",
        side="LONG",
        tp_pct=0.001,
        sl_pct=0.001,
        partial_exit_pct=0.4,
        partial_tp_pct=0.0005,
        recovery_steps=1,
        recovery_trigger_pct=0.0005,
        recovery_tp_shrink=0.45,
        adverse_exit_bars=10,
        adverse_exit_loss_pct=0.0007,
        max_holding_bars=24,
        params_label="default",
    )
    codex_decision = CodexV1Decision(
        accepted=True,
        version="test",
        baseline="test",
        lane="w6_lane_s1long_rng38_86_range9_15_e0",
        lane_code="W6A",
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=200.0,
        reason="accepted",
    )

    adjusted = manager._apply_codex_v1_decision(decision, codex_decision)

    assert adjusted.partial_tp_pct == pytest.approx(0.0006)
    assert adjusted.signal.take_profits[0] == pytest.approx(100.1)
    assert "codex_v138_w6a_partial_tp_pct:0.0006" in adjusted.signal.reasons


@pytest.mark.asyncio
async def test_v138_w6a_take_profit_orders_use_signal_partial_tp_pct():
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=100.02,
        unrealized_pnl=0.0024,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    manager = MainnetOneRunManager(
        _settings(mainnet_partial_tp_pct=0.0005, mainnet_partial_exit_pct=0.40),
        client,
        FakeRepo(),
        FakeTelegramApp(),
    )
    run = _run(
        run_id="cry3mn_v138_tp6",
        signal_json=json.dumps({
            "side": "LONG",
            "take_profit": 100.1,
            "stop_loss": 99.0,
            "wildcat": {"tp_pct": 0.001, "partial_tp_pct": 0.0006},
            "codex_v1": {"enabled": True, "lane_code": "W6A"},
        }),
        avg_entry_price=100.0,
    )

    orders = await manager._desired_take_profit_orders(
        run,
        client.position,
        signal=json.loads(run["signal_json"]),
        close_side="SELL",
    )
    order_dict = {client_order_id: (qty, price) for client_order_id, qty, price in orders}

    assert order_dict["cry3mn_v138_tp6_tp1"][0] == "0.048"
    assert order_dict["cry3mn_v138_tp6_tp1"][1] == pytest.approx(100.06)
    assert order_dict["cry3mn_v138_tp6_tp3"][1] == pytest.approx(100.1)


@pytest.mark.asyncio
async def test_v139b_wpr_take_profit_orders_use_signal_partial_exit_pct():
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=100.02,
        unrealized_pnl=0.0024,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    manager = MainnetOneRunManager(
        _settings(mainnet_partial_tp_pct=0.0005, mainnet_partial_exit_pct=0.40),
        client,
        FakeRepo(),
        FakeTelegramApp(),
    )
    run = _run(
        run_id="cry3mn_v139b_wpr_tp60",
        signal_json=json.dumps({
            "side": "LONG",
            "take_profit": 100.1,
            "stop_loss": 99.92,
            "wildcat": {"tp_pct": 0.001, "partial_tp_pct": 0.0003, "partial_exit_pct": 0.60},
            "codex_v1": {"enabled": True, "lane_code": "CNL-WPR-L"},
        }),
        avg_entry_price=100.0,
    )

    orders = await manager._desired_take_profit_orders(
        run,
        client.position,
        signal=json.loads(run["signal_json"]),
        close_side="SELL",
    )
    order_dict = {client_order_id: (qty, price) for client_order_id, qty, price in orders}

    assert order_dict["cry3mn_v139b_wpr_tp60_tp1"][0] == "0.072"
    assert order_dict["cry3mn_v139b_wpr_tp60_tp1"][1] == pytest.approx(100.03)
    assert order_dict["cry3mn_v139b_wpr_tp60_tp3"][1] == pytest.approx(100.1)


def test_v138_w6a_fast_trail_can_be_enabled_for_1s_watch():
    manager = MainnetOneRunManager(
        _settings(
            mainnet_trail_arm_frac=0.7,
            mainnet_trail_watch_interval_seconds=2,
            mainnet_codex_v138_w6a_fast_trail_enabled=True,
            mainnet_codex_v138_w6a_trail_arm_cap_bp=3.5,
            mainnet_codex_v138_w6a_trail_watch_interval_seconds=1,
        ),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    w6a_run = _run(
        signal_json=json.dumps({
            "side": "LONG",
            "take_profit": 101.0,
            "stop_loss": 99.0,
            "wildcat": {"tp_pct": 0.001},
            "codex_v1": {"enabled": True, "lane_code": "W6A"},
        })
    )
    other_run = _run(
        signal_json=json.dumps({
            "side": "LONG",
            "take_profit": 101.0,
            "stop_loss": 99.0,
            "wildcat": {"tp_pct": 0.001},
            "codex_v1": {"enabled": True, "lane_code": "W1D"},
        })
    )

    assert manager._trail_arm_mfe(w6a_run, 0.001) == pytest.approx(0.00035)
    assert manager._trail_watch_interval_seconds(w6a_run) == 1
    assert manager._trail_arm_mfe(other_run, 0.001) == pytest.approx(0.0007)
    assert manager._trail_watch_interval_seconds(other_run) == 2


def test_v138_w6a_uses_conservative_trail_by_default():
    manager = MainnetOneRunManager(
        _settings(
            mainnet_trail_arm_frac=0.7,
            mainnet_trail_watch_interval_seconds=2,
            mainnet_codex_v138_w6a_fast_trail_enabled=False,
            mainnet_codex_v138_w6a_trail_arm_cap_bp=3.5,
            mainnet_codex_v138_w6a_trail_watch_interval_seconds=1,
        ),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    w6a_run = _run(
        signal_json=json.dumps({
            "side": "LONG",
            "take_profit": 101.0,
            "stop_loss": 99.0,
            "wildcat": {"tp_pct": 0.001},
            "codex_v1": {"enabled": True, "lane_code": "W6A"},
        })
    )

    assert manager._trail_arm_mfe(w6a_run, 0.001) == pytest.approx(0.0007)
    assert manager._trail_watch_interval_seconds(w6a_run) == 2