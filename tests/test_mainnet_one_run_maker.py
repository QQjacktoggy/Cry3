import json
from types import SimpleNamespace

import pytest

from config.settings import Settings
from src.gridbot.binance.models import PositionInfo
from src.gridbot.mainnet.one_run import GTXSlippageExceeded, MainnetOneRunManager, WPR_V143_PROFILES
from src.gridbot.strategy.codex_v1_live import CodexV1Decision


class FakeRepo:
    def __init__(self):
        self.updated = []
        self.events = []
        self.completed = []
        self.created = []
        self.first_event_time = {}
        self.recent_completed_runs = []
        self.recent_runs = []
        self.latest_run = None

    async def get_latest_run(self):
        return self.latest_run

    async def get_recent_runs(self, limit=200):
        rows = sorted(
            list(self.recent_runs),
            key=lambda row: (int(row.get("armed_at_ms") or 0), str(row.get("run_id") or "")),
            reverse=True,
        )
        return rows[:limit]

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


class FakeConfigRepo:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.set_calls = []

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value):
        self.values[key] = value
        self.set_calls.append((key, value))


class FakeClient:
    def __init__(self):
        self.position = None
        self.open_orders = []
        self.all_orders = []
        self.all_orders_calls = []
        self.user_trades = []
        self.user_trades_calls = []
        self.income_history = []
        self.income_history_calls = []
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
        self.asset_balances = {
            "USDC": {"asset": "USDC", "availableBalance": "1000"},
            "USDT": {"asset": "USDT", "availableBalance": "1000"},
        }

    async def get_position(self, symbol):
        return self.position

    async def get_asset_balance(self, asset):
        return self.asset_balances.get(asset)

    async def set_leverage(self, symbol, leverage):
        self.leverage_set = (symbol, leverage)
        return {"symbol": symbol, "leverage": leverage}

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
        self.all_orders_calls.append((symbol, start_time, limit))
        return list(self.all_orders)

    async def get_user_trades(self, symbol, start_time=None, limit=1000):
        self.user_trades_calls.append((symbol, start_time, limit))
        return list(self.user_trades)

    async def get_income_history(
        self,
        income_type=None,
        symbol=None,
        start_time=None,
        end_time=None,
        limit=100,
    ):
        self.income_history_calls.append(
            (income_type, symbol, start_time, end_time, limit)
        )
        return list(self.income_history)

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
        # Conditional futures orders live in openAlgoOrders. v1.4.59 passes
        # clientAlgoId explicitly so terminal fills retain run ownership.
        algo_id = self._next_order_id
        self._next_order_id += 1
        order = {
            "algoId": algo_id,
            "clientAlgoId": client_order_id or f"x-FAKE{algo_id}",
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
        "_env_file": None,
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


def _codex_recovery_signal(lane_code="CNL-WPR-L", side="SHORT"):
    return json.dumps(
        {
            "side": side,
            "take_profit": 99.5 if side == "SHORT" else 100.5,
            "stop_loss": 101.5 if side == "SHORT" else 99.5,
            "wildcat": {"tp_pct": 0.001, "sl_pct": 0.001, "partial_exit_pct": 1.0},
            "codex_v1": {
                "enabled": True,
                "lane_code": lane_code,
                "metrics": {"market_state": f"{lane_code}:unit"},
            },
        }
    )


def _codex_recovery_run(lane_code="CNL-WPR-L", side="SHORT", run_id="cry3mn_codex_dca"):
    return _run(
        run_id=run_id,
        side=side,
        signal_json=_codex_recovery_signal(lane_code=lane_code, side=side),
        avg_entry_price=1675.32,
        qty=0.119,
    )


def _codex_dca_short_position(unrealized_pnl=-0.2, mark_price=1685.0):
    return PositionInfo(
        symbol="ETHUSDC",
        position_amt=-0.119,
        entry_price=1675.32,
        mark_price=mark_price,
        unrealized_pnl=unrealized_pnl,
        liquidation_price=2000.0,
        leverage=75,
        margin_type="cross",
    )


def _recovery_skip_events(repo):
    return [details for _, event_type, details in repo.events if event_type == "recovery_skipped"]


def _recovery_skip_reasons(repo):
    return [details.get("reason") for details in _recovery_skip_events(repo)]


@pytest.mark.asyncio
async def test_v149_loop_state_rehydrates_from_active_run_params():
    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    active = _run(
        run_id="cry3mn_loop50",
        status="ARMED",
        params={"actor": "telegram", "loop_count": 50},
    )

    await manager._maybe_rehydrate_loop_state_from_active_run(active)

    assert manager._loop_total == 50
    assert manager._loop_completed == 0
    assert manager._loop_run_ids == ["cry3mn_loop50"]


@pytest.mark.asyncio
async def test_v149_loop_state_rehydrates_from_params_json():
    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    active = _run(
        run_id="cry3mn_loop50_json",
        status="ARMED",
        params_json=json.dumps({"actor": "telegram", "loop_count": 50}),
    )

    await manager._maybe_rehydrate_loop_state_from_active_run(active)

    assert manager._loop_total == 50
    assert manager._loop_completed == 0
    assert manager._loop_run_ids == ["cry3mn_loop50_json"]

@pytest.mark.asyncio
async def test_v149_loop_state_rehydrates_mid_loop_index():
    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    active = _run(
        run_id="cry3mn_loop50_17",
        status="ARMED",
        params={"actor": "telegram_loop", "loop_count": 50, "loop_index": 17},
    )

    await manager._maybe_rehydrate_loop_state_from_active_run(active)

    assert manager._loop_total == 50
    assert manager._loop_completed == 16
    assert manager._loop_run_ids == ["cry3mn_loop50_17"]

@pytest.mark.asyncio
async def test_v149_loop_rehydrate_restores_completed_net_pnl_and_run_ids():
    repo = FakeRepo()
    first = _run(
        run_id="cry3mn_loop_1",
        status="COMPLETED",
        params={"actor": "telegram", "loop_count": 50, "loop_index": 1, "side": "LONG"},
        strategy_label="S1",
        side="LONG",
        realized_pnl_usdc=-0.20,
        commission_usdc=0.02,
        armed_at_ms=100,
        completed_at_ms=200,
        updated_at_ms=200,
    )
    second = _run(
        run_id="cry3mn_loop_2",
        status="COMPLETED",
        params={"actor": "telegram_loop", "loop_count": 50, "loop_index": 2, "side": "LONG"},
        strategy_label="S1",
        side="LONG",
        realized_pnl_usdc=0.10,
        commission_usdc=0.01,
        armed_at_ms=300,
        completed_at_ms=400,
        updated_at_ms=400,
    )
    active = _run(
        run_id="cry3mn_loop_3",
        status="RUNNING",
        params={"actor": "telegram_loop", "loop_count": 50, "loop_index": 3, "side": "LONG"},
        strategy_label="S1",
        side="LONG",
        armed_at_ms=500,
    )
    repo.recent_runs = [active, second, first]
    manager = MainnetOneRunManager(_settings(), FakeClient(), repo, FakeTelegramApp())

    await manager._maybe_rehydrate_loop_state_from_active_run(active)

    assert manager._loop_total == 50
    assert manager._loop_completed == 2
    assert manager._loop_run_ids == ["cry3mn_loop_1", "cry3mn_loop_2", "cry3mn_loop_3"]
    assert manager._loop_net_pnl == pytest.approx(-0.13)
    assert manager._loss_streak == 0


@pytest.mark.asyncio
async def test_v149_loop_pending_cooldown_rehydrates_without_active_run():
    import time as _time

    now_ms = int(_time.time() * 1000)
    repo = FakeRepo()
    first = _run(
        run_id="cry3mn_loop_p1",
        status="COMPLETED",
        params={"actor": "telegram", "loop_count": 50, "loop_index": 1, "side": "SHORT"},
        strategy_label="wildcat_v2_adverse_guard",
        side="SHORT",
        realized_pnl_usdc=0.08,
        commission_usdc=0.01,
        armed_at_ms=100,
        completed_at_ms=200,
        updated_at_ms=200,
    )
    latest = _run(
        run_id="cry3mn_loop_p2",
        status="COMPLETED",
        params={"actor": "telegram_loop", "loop_count": 50, "loop_index": 2, "side": "SHORT"},
        strategy_label="wildcat_v2_adverse_guard",
        side="SHORT",
        realized_pnl_usdc=-0.20,
        commission_usdc=0.02,
        armed_at_ms=300,
        completed_at_ms=now_ms,
        updated_at_ms=now_ms,
    )
    repo.latest_run = latest
    repo.recent_runs = [latest, first]
    repo.events.append((
        latest["run_id"],
        "loop_cooldown_pending",
        {
            "side": "SHORT",
            "strategy": "wildcat_v2_adverse_guard",
            "prev_run_id": latest["run_id"],
            "resume_at_ms": now_ms + 60_000,
            "completed": 2,
            "total": 50,
            "loop_net_pnl": -0.15,
        },
    ))
    manager = MainnetOneRunManager(_settings(), FakeClient(), repo, FakeTelegramApp())

    await manager._maybe_rehydrate_pending_loop_from_latest_run()

    assert manager._loop_total == 50
    assert manager._loop_completed == 2
    assert manager._loop_run_ids == ["cry3mn_loop_p1", "cry3mn_loop_p2"]
    assert manager._loop_net_pnl == pytest.approx(-0.15)
    assert manager._loop_resume == {
        "side": "SHORT",
        "strategy": "wildcat_v2_adverse_guard",
        "prev_run_id": "cry3mn_loop_p2",
        "resume_at_ms": now_ms + 60_000,
    }

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
    # Hermes live mode: TP2/TP3 disabled, only TP1 rests and the runner is left for TRAIL.
    assert len(tp_orders) == 1
    assert all(order["side"] == "SELL" for order in tp_orders)
    assert {o["clientOrderId"] for o in tp_orders} == {"cry3mn_test_tp1"}
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
    manager = MainnetOneRunManager(_settings(mainnet_trail_disable_final_tp=False), client, repo, FakeTelegramApp())

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
        {"orderId": 111, "symbol": "ETHUSDC", "clientOrderId": "cry3mn_test_tp1", "origQty": "0.048", "price": "100.04"},
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
        _settings(
            mainnet_recovery_enabled=False,
            mainnet_mid_tp_pct=0.0030,
            mainnet_hard_sl_pct_override=0.0,
            mainnet_trail_disable_final_tp=False,
            mainnet_mid_exit_pct=0.5,
        ),
        client,
        repo,
        telegram,
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
    assert order["clientAlgoId"] == "cry3mn_test_sl"
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
async def test_v1421_hold_s_extends_live_max_hold_window():
    import time

    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=-0.032,
        entry_price=100.0,
        mark_price=99.9,
        unrealized_pnl=0.0032,
        liquidation_price=120.0,
        leverage=75,
        margin_type="cross",
    )
    repo = FakeRepo()
    manager = MainnetOneRunManager(_settings(mainnet_max_holding_bars=24), client, repo, FakeTelegramApp())
    run_id = "cry3mn_v1421_hold"
    now_ms = int(time.time() * 1000)
    repo.first_event_time[(run_id, "entry_filled")] = now_ms - 25 * 60_000
    signal = {
        "side": "SHORT",
        "take_profit": 99.0,
        "stop_loss": 101.0,
        "codex_v1": {
            "enabled": True,
            "lane_code": "STUP-S",
            "metrics": {"hold_s": 2100},
        },
    }
    run = _run(
        run_id=run_id,
        side="SHORT",
        signal_json=json.dumps(signal),
        avg_entry_price=100.0,
        armed_at_ms=now_ms - 25 * 60_000,
        cumulative_notional_usdc=50.0,
    )

    await manager._run_running(run)

    assert client.market_orders == []
    assert not repo.completed


@pytest.mark.asyncio
async def test_v1421_hold_s_closes_when_profile_window_elapsed():
    import time

    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=-0.032,
        entry_price=100.0,
        mark_price=99.9,
        unrealized_pnl=0.0032,
        liquidation_price=120.0,
        leverage=75,
        margin_type="cross",
    )
    repo = FakeRepo()
    manager = MainnetOneRunManager(_settings(mainnet_max_holding_bars=24), client, repo, FakeTelegramApp())
    run_id = "cry3mn_v1421_hold_done"
    now_ms = int(time.time() * 1000)
    repo.first_event_time[(run_id, "entry_filled")] = now_ms - 36 * 60_000
    signal = {
        "side": "SHORT",
        "take_profit": 99.0,
        "stop_loss": 101.0,
        "codex_v1": {
            "enabled": True,
            "lane_code": "STUP-S",
            "metrics": {"hold_s": 2100},
        },
    }
    run = _run(
        run_id=run_id,
        side="SHORT",
        signal_json=json.dumps(signal),
        avg_entry_price=100.0,
        armed_at_ms=now_ms - 36 * 60_000,
        cumulative_notional_usdc=50.0,
    )

    await manager._run_running(run)

    assert len(client.market_orders) == 1
    assert client.market_orders[0]["side"] == "BUY"
    assert any(fields.get("exit_reason") == "MAX_HOLD_WIN" for _, fields in repo.updated)

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
async def test_v1415_breakeven_sl_uses_tp1_be_reason_and_short_sign():
    client = FakeClient()
    repo = FakeRepo()
    telegram = FakeTelegramApp()
    manager = MainnetOneRunManager(
        _settings(mainnet_codex_be_maker_first_enabled=False),
        client,
        repo,
        telegram,
    )
    run_id = "cry3mn_be_short"
    signal = {
        "side": "SHORT",
        "take_profit": 99.5,
        "stop_loss": 101.5,
        "codex_v1": {
            "enabled": True,
            "lane_code": "STUP-S",
            "metrics": {"be_bp": 4.0},
        },
    }
    run = _run(
        run_id=run_id,
        side="SHORT",
        signal_json=json.dumps(signal),
        avg_entry_price=100.0,
        qty=0.019,
    )
    repo.first_event_time[(run_id, "partial_exit")] = 1
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=-0.019,
        entry_price=100.0,
        mark_price=99.90,
        unrealized_pnl=0.0019,
        liquidation_price=120.0,
        leverage=75,
        margin_type="cross",
    )

    await manager._maybe_apply_breakeven_sl(
        run=run,
        position=position,
        signal=json.loads(run["signal_json"]),
        side="SHORT",
        entry=100.0,
        qty=0.019,
        close_side="BUY",
    )

    assert len(client.stop_market_sl_orders) == 1
    stop_order = client.stop_market_sl_orders[0]
    assert stop_order["side"] == "BUY"
    assert float(stop_order["triggerPrice"]) == pytest.approx(99.96)
    saved_signal = json.loads(run["signal_json"])
    assert saved_signal["stop_loss_reason"] == "TP1_BE_SL"
    assert saved_signal["stop_loss_kind"] == "breakeven"
    assert saved_signal["breakeven_sl"]["offset_bp"] == pytest.approx(4.0)
    sl_event = next(details for _, event_type, details in repo.events if event_type == "sl_placed")
    assert sl_event["reason"] == "TP1_BE_SL"
    be_event = next(details for _, event_type, details in repo.events if event_type == "breakeven_sl_applied")
    assert be_event["reason"] == "TP1_BE_SL"
    assert "Entry: $100.0000 - 4bp" in telegram.bot.messages[-1]["text"]


@pytest.mark.asyncio
async def test_v1415_breakeven_maker_first_skips_stop_when_maker_fills(monkeypatch):
    client = FakeClient()
    repo = FakeRepo()
    manager = MainnetOneRunManager(_settings(), client, repo, FakeTelegramApp())
    run_id = "cry3mn_be_maker"
    signal = {
        "side": "LONG",
        "take_profit": 100.5,
        "stop_loss": 99.0,
        "codex_v1": {
            "enabled": True,
            "lane_code": "STUP-S",
            "metrics": {"be_bp": 4.0},
        },
    }
    run = _run(
        run_id=run_id,
        side="LONG",
        signal_json=json.dumps(signal),
        avg_entry_price=100.0,
        qty=0.019,
    )
    repo.first_event_time[(run_id, "partial_exit")] = 1
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.019,
        entry_price=100.0,
        mark_price=100.08,
        unrealized_pnl=0.00152,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    calls = {}

    async def _maker_fill(symbol, side, qty_str, run_arg, **kwargs):
        calls.update({"symbol": symbol, "side": side, "qty_str": qty_str, **kwargs})
        return True

    monkeypatch.setattr(manager, "_try_trail_maker_exit", _maker_fill)

    await manager._maybe_apply_breakeven_sl(
        run=run,
        position=position,
        signal=json.loads(run["signal_json"]),
        side="LONG",
        entry=100.0,
        qty=0.019,
        close_side="SELL",
    )

    assert calls["reason"] == "TP1_BE_SL"
    assert calls["ttl_seconds"] == 2
    assert calls["profit_floor_bp"] == pytest.approx(4.0)
    assert calls["enforce_profit_floor"] is True
    assert client.stop_market_sl_orders == []
    assert any(event_type == "breakeven_maker_exit_done" for _, event_type, _ in repo.events)


@pytest.mark.asyncio
async def test_v1415_close_position_be_uses_be_maker_settings(monkeypatch):
    client = FakeClient()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_be_maker_first_enabled=True,
            mainnet_codex_be_maker_ttl_seconds=2,
            mainnet_codex_be_maker_adverse_break_bp=1.0,
            mainnet_codex_survival_exit_maker_ttl_seconds=9,
            mainnet_codex_survival_exit_adverse_break_bp=3.0,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
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
    client.book = {"bidPrice": "100.05", "askPrice": "100.06"}
    run = _run(
        run_id="cry3mn_be_close",
        side="LONG",
        signal_json=json.dumps({
            "side": "LONG",
            "breakeven_sl": {"offset_bp": 4.0},
            "codex_v1": {"enabled": True, "lane_code": "STUP-S", "metrics": {"be_bp": 4.0}},
        }),
        avg_entry_price=100.0,
    )
    calls = {}

    async def _maker_fill(symbol, side, qty_str, run_arg, **kwargs):
        calls.update({"symbol": symbol, "side": side, "qty_str": qty_str, **kwargs})
        return True

    monkeypatch.setattr(manager, "_try_trail_maker_exit", _maker_fill)

    closed = await manager._close_position("ETHUSDC", "SELL", 0.12, "TP1_BE_SL", run)

    assert closed is True
    assert calls["reason"] == "TP1_BE_SL"
    assert calls["ttl_seconds"] == 2
    assert calls["adverse_break_bp"] == pytest.approx(1.0)
    assert calls["profit_floor_bp"] == pytest.approx(4.0)
    assert calls["enforce_profit_floor"] is True
    assert client.market_orders == []

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
            mainnet_trail_require_partial_fill=False,
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
            mainnet_trail_require_partial_fill=False,
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
            mainnet_codex_survival_enabled=True,
            mainnet_codex_survival_watch_after_seconds=300,
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
            mainnet_codex_survival_enabled=True,
            mainnet_codex_survival_watch_after_seconds=300,
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
async def test_v147_wpr_discount_mixed_delays_early_scratch(monkeypatch):
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
        "codex_v1": {
            "enabled": True,
            "lane_code": "CNL-WPR-L",
            "lane": "v139_canary_watch_pre_reprice_long_s1",
            "metrics": {"wpr_profile": "CNL-WPR-L:discount_mixed"},
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.12)
    manager._trail_peak[run["run_id"]] = 100.04
    hold_start_ms = 1_700_000_000_000
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 61_000) / 1000.0)

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

    assert fired is False
    assert not client.market_orders
    watch_event = next(details for _, event_type, details in repo.events if event_type == "codex_survival_watch")
    assert watch_event["survival_profile"] == "v139b_wpr_waiting_scratch"
    assert watch_event["market_state"] == "CNL-WPR-L:discount_mixed"

    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 121_000) / 1000.0)
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
    exit_event = next(details for _, event_type, details in repo.events if event_type == "codex_survival_exit")
    assert exit_event["reason"] == "CNL_WPR_SCRATCH"
    assert exit_event["market_state"] == "CNL-WPR-L:discount_mixed"


@pytest.mark.asyncio
async def test_v145_wpr_survival_tracks_peak_without_trail_watcher(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=100.06,
        unrealized_pnl=0.0084,
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
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 60_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "LONG",
        100.06,
        100.0,
        0.12,
        "SELL",
        hold_start_ms,
    )

    assert fired is False
    assert manager._trail_peak[run["run_id"]] == pytest.approx(100.06)
    watch_event = next(details for _, event_type, details in repo.events if event_type == "codex_survival_watch")
    assert watch_event["mfe_bp"] == pytest.approx(6.0)
    assert watch_event["current_bp"] == pytest.approx(6.0)


@pytest.mark.asyncio
async def test_v145_wpr_profit_lock_preempts_damage_after_mfe(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=99.93,
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
    manager._trail_peak[run["run_id"]] = 100.08
    hold_start_ms = 1_700_000_000_000
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 241_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "LONG",
        99.93,
        100.0,
        0.12,
        "SELL",
        hold_start_ms,
    )

    assert fired is True
    assert len(client.market_orders) == 1
    exit_event = next(details for _, event_type, details in repo.events if event_type == "codex_survival_exit")
    assert exit_event["reason"] == "CNL_WPR_PROFIT_LOCK"
    assert exit_event["survival_profile"] == "v139b_wpr_waiting_scratch"
    assert exit_event["mfe_bp"] == pytest.approx(8.0)
    assert exit_event["current_bp"] == pytest.approx(-7.0)



@pytest.mark.asyncio
async def test_v1411_wpr_falling_profit_lock_ignores_fee_negative_floor(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.032,
        entry_price=100.0,
        mark_price=100.018,
        unrealized_pnl=0.000576,
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
        "codex_v1": {
            "enabled": True,
            "lane_code": "CNL-WPR-L",
            "lane": "v139_canary_watch_pre_reprice_long_s1",
            "metrics": {
                "market_state": "CNL-WPR-L:falling_discount_trap",
                "profit_lock_mfe_bp": 8.0,
                "profit_lock_floor_bp": 6.0,
                "profit_lock_giveback_bp": 3.0,
            },
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.032)
    manager._trail_peak[run["run_id"]] = 100.11
    hold_start_ms = 1_700_000_000_000
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 260_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "LONG",
        100.018,
        100.0,
        0.032,
        "SELL",
        hold_start_ms,
    )

    assert fired is False
    assert not client.market_orders
    assert not any(event_type == "codex_survival_exit" for _, event_type, _ in repo.events)


@pytest.mark.asyncio
async def test_v1411_wpr_falling_profit_lock_captures_fee_safe_giveback(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.032,
        entry_price=100.0,
        mark_price=100.07,
        unrealized_pnl=0.00224,
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
        "codex_v1": {
            "enabled": True,
            "lane_code": "CNL-WPR-L",
            "lane": "v139_canary_watch_pre_reprice_long_s1",
            "metrics": {
                "market_state": "CNL-WPR-L:falling_discount_trap",
                "profit_lock_mfe_bp": 8.0,
                "profit_lock_floor_bp": 6.0,
                "profit_lock_giveback_bp": 3.0,
            },
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.032)
    manager._trail_peak[run["run_id"]] = 100.11
    hold_start_ms = 1_700_000_000_000
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 181_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "LONG",
        100.07,
        100.0,
        0.032,
        "SELL",
        hold_start_ms,
    )

    assert fired is True
    assert len(client.market_orders) == 1
    exit_event = next(details for _, event_type, details in repo.events if event_type == "codex_survival_exit")
    assert exit_event["reason"] == "CNL_WPR_PROFIT_LOCK"
    assert exit_event["market_state"] == "CNL-WPR-L:falling_discount_trap"
    assert exit_event["mfe_bp"] == pytest.approx(11.0)
    assert exit_event["current_bp"] == pytest.approx(7.0)
    assert exit_event["giveback_bp"] == pytest.approx(4.0)
    assert exit_event["profit_lock_fee_floor_enabled"] is True
    assert exit_event["profit_lock_fee_floor_bp"] == pytest.approx(6.0)


@pytest.mark.asyncio
async def test_v1412_wpr_profit_lock_aborts_before_cancel_when_book_loses_fee_floor(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.032,
        entry_price=100.0,
        mark_price=100.07,
        unrealized_pnl=0.00224,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    client.position = position
    client.book = {"bidPrice": "100.02", "askPrice": "100.03"}
    client.open_orders.append({"orderId": 901, "clientOrderId": "cry3mn_test_tp1", "status": "NEW"})
    client.algo_orders.append({"algoId": 902, "clientAlgoId": "sl", "algoStatus": "NEW"})
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(mainnet_recovery_enabled=False, mainnet_codex_survival_exit_use_maker=True),
        client,
        repo,
        FakeTelegramApp(),
    )
    signal = {
        "side": "LONG",
        "codex_v1": {
            "enabled": True,
            "lane_code": "CNL-WPR-L",
            "lane": "v139_canary_watch_pre_reprice_long_s1",
            "metrics": {
                "market_state": "CNL-WPR-L:falling_discount_trap",
                "profit_lock_mfe_bp": 8.0,
                "profit_lock_floor_bp": 6.0,
                "profit_lock_giveback_bp": 3.0,
            },
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.032)
    manager._trail_peak[run["run_id"]] = 100.11
    hold_start_ms = 1_700_000_000_000
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 181_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "LONG",
        100.07,
        100.0,
        0.032,
        "SELL",
        hold_start_ms,
    )

    assert fired is False
    assert not client.market_orders
    assert not any(o.get("clientOrderId") == f"{run['run_id']}_trail" for o in client.all_orders)
    assert client.cancelled == []
    assert client.cancelled_algo == []
    abort_event = next(details for _, event_type, details in repo.events if event_type == "survival_profit_lock_aborted_anchor_floor")
    assert abort_event["reason"] == "CNL_WPR_PROFIT_LOCK"
    assert abort_event["floor_bp"] == pytest.approx(6.0)


@pytest.mark.asyncio
async def test_v149_stups_weak_chop_time_profit_lock_ignores_fee_negative_thin_profit(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=-0.032,
        entry_price=100.0,
        mark_price=99.97,
        unrealized_pnl=0.00096,
        liquidation_price=120.0,
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
        "side": "SHORT",
        "codex_v1": {
            "enabled": True,
            "lane_code": "STUP-S",
            "lane": "codex_v1_stale_upmove_short_rng20_canary",
            "metrics": {"market_state": "STUP-S:weak_chop"},
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.032, side="SHORT")
    manager._trail_peak[run["run_id"]] = 99.89
    hold_start_ms = 1_700_000_000_000
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 181_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "SHORT",
        99.97,
        100.0,
        0.032,
        "BUY",
        hold_start_ms,
    )

    assert fired is False
    assert len(client.market_orders) == 0
    assert not any(event_type == "codex_survival_exit" for _, event_type, _ in repo.events)

@pytest.mark.asyncio
async def test_v1427_profile_time_lock_takes_priority_over_wpr_scratch(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.032,
        entry_price=100.0,
        mark_price=100.07,
        unrealized_pnl=0.00224,
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
        "codex_v1": {
            "enabled": True,
            "lane_code": "CNL-WPR-L",
            "lane": "v139_canary_watch_pre_reprice_long_s1",
            "metrics": {
                "market_state": "CNL-WPR-L:discount_mixed",
                "time_profit_lock_enabled": True,
                "time_lock_s": 60,
                "time_lock_min_bp": 6.0,
                "time_lock_slope_max_bp": 0.0,
                "time_lock_lookback_s": 30,
            },
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.032, side="LONG")
    hold_start_ms = 1_700_000_000_000
    manager._codex_time_lock_price_history[run["run_id"]] = [((hold_start_ms + 31_000) / 1000.0, 100.08)]
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 61_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "LONG",
        100.07,
        100.0,
        0.032,
        "SELL",
        hold_start_ms,
    )

    assert fired is True
    assert len(client.market_orders) == 1
    exit_event = next(details for _, event_type, details in repo.events if event_type == "codex_survival_exit")
    assert exit_event["reason"] == "CODEX_V1427_TIME_LOCK"
    assert exit_event["survival_profile"] == "v1427_profile_time_lock"
    assert exit_event["profile_time_lock_s"] == pytest.approx(60.0)
    assert exit_event["profile_time_lock_min_bp"] == pytest.approx(6.0)
    assert exit_event["profile_time_lock_slope_bp"] <= 0.0



@pytest.mark.asyncio
async def test_v1429_side_override_fast_lock_takes_profit_before_profile_time_lock(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.032,
        entry_price=100.0,
        mark_price=100.04,
        unrealized_pnl=0.00128,
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
        "codex_v1": {
            "enabled": True,
            "lane_code": "STUP-S",
            "lane": "codex_v1_stale_upmove_short_rng20_canary",
            "metrics": {
                "market_state": "STUP-S:counter_recoil",
                "time_profit_lock_enabled": True,
                "time_lock_s": 60,
                "time_lock_min_bp": 6.0,
                "time_lock_slope_max_bp": 0.0,
                "time_lock_lookback_s": 30,
                "target_side": "LONG",
                "v1427_previous_side": "SHORT",
            },
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.032, side="LONG")
    hold_start_ms = 1_700_000_000_000
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 10_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "LONG",
        100.04,
        100.0,
        0.032,
        "SELL",
        hold_start_ms,
    )

    assert fired is True
    exit_event = next(details for _, event_type, details in repo.events if event_type == "codex_survival_exit")
    assert exit_event["reason"] == "CODEX_V1429_SIDE_OVERRIDE_FAST_LOCK"
    assert exit_event["v1429_previous_side"] == "SHORT"
    assert exit_event["v1429_target_side"] == "LONG"
    assert exit_event["v1429_fast_lock_floor_bp"] == pytest.approx(3.0)


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_v1432_full_tp_touch_lock_uses_maker_only_and_defers_on_timeout():
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=100.12,
        unrealized_pnl=0.0144,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    client.book = {"bidPrice": "100.11", "askPrice": "100.12"}
    client.open_orders.append({"orderId": 901, "clientOrderId": "cry3mn_test_tp1", "status": "NEW"})
    client.algo_orders.append({"algoId": 902, "clientAlgoId": "x-FAKE902", "reduceOnly": True, "algoStatus": "NEW"})
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_recovery_enabled=False,
            mainnet_codex_survival_enabled=True,
            mainnet_codex_survival_watch_after_seconds=300,
            mainnet_codex_survival_exit_use_maker=True,
            mainnet_codex_full_tp_touch_maker_only_enabled=True,
            mainnet_codex_full_tp_touch_maker_ttl_seconds=0,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    signal = {
        "side": "LONG",
        "codex_v1": {
            "enabled": True,
            "lane_code": "STUP-S",
            "metrics": {
                "market_state": "STUP-S:clean_extension",
                "tp1_bp": 10.0,
                "partial_exit_pct": 1.0,
            },
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.12, side="LONG")

    closed = await manager._close_position("ETHUSDC", "SELL", 0.12, "CODEX_FULL_TP_TOUCH_LOCK", run)

    assert closed is False
    assert any(o.get("timeInForce") == "GTX" for o in client.all_orders)
    assert client.market_orders == []
    assert ("ETHUSDC", 901) not in client.cancelled
    assert client.cancelled_algo == []
    assert any(
        event_type == "survival_maker_deferred" and details.get("reason") == "CODEX_FULL_TP_TOUCH_LOCK"
        for _, event_type, details in repo.events
    )
    assert any(event_type == "full_tp_touch_maker_only_deferred" for _, event_type, _ in repo.events)


@pytest.mark.asyncio
async def test_v1432_stups_full_tp_touch_triggers_profit_lock(monkeypatch):
    client = FakeClient()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_recovery_enabled=False,
            mainnet_codex_full_tp_touch_maker_only_enabled=True,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    signal = {
        "side": "LONG",
        "take_profit": 100.1,
        "stop_loss": 99.94,
        "wildcat": {"tp_pct": 0.001, "sl_pct": 0.0006, "partial_exit_pct": 1.0},
        "codex_v1": {
            "enabled": True,
            "lane_code": "STUP-S",
            "metrics": {
                "market_state": "STUP-S:clean_extension",
                "tp1_bp": 10.0,
                "partial_exit_pct": 1.0,
            },
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.12, side="LONG")
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=100.12,
        unrealized_pnl=0.0144,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    calls = []

    async def _mock_close(symbol, side, qty, reason, run_arg):
        calls.append((symbol, side, qty, reason, run_arg["run_id"]))
        return True

    monkeypatch.setattr(manager, "_close_position", _mock_close)

    fired = await manager._maybe_full_tp_touch_lock(
        run,
        signal,
        position,
        "LONG",
        100.12,
        100.0,
        0.12,
        "SELL",
    )

    assert fired is True
    assert calls == [("ETHUSDC", "SELL", 0.12, "CODEX_FULL_TP_TOUCH_LOCK", run["run_id"])]
    assert any(event_type == "full_tp_touch_lock_signal" for _, event_type, _ in repo.events)


async def test_v1427_profile_time_lock_uses_maker_only_and_defers_on_timeout():
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=100.07,
        unrealized_pnl=0.0084,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    client.book = {"bidPrice": "100.07", "askPrice": "100.08"}
    client.open_orders.append({"orderId": 901, "clientOrderId": "cry3mn_test_tp1", "status": "NEW"})
    client.algo_orders.append({"algoId": 902, "clientAlgoId": "x-FAKE902", "reduceOnly": True, "algoStatus": "NEW"})
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_recovery_enabled=False,
            mainnet_codex_survival_enabled=True,
            mainnet_codex_survival_watch_after_seconds=300,
            mainnet_codex_survival_exit_use_maker=True,
            mainnet_codex_v1427_time_lock_maker_only_enabled=True,
            mainnet_codex_v1427_time_lock_maker_ttl_seconds=0,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    signal = {
        "side": "LONG",
        "codex_v1": {
            "enabled": True,
            "lane_code": "STUP-S",
            "metrics": {"time_lock_min_bp": 6.0},
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.12, side="LONG")

    closed = await manager._close_position("ETHUSDC", "SELL", 0.12, "CODEX_V1427_TIME_LOCK", run)

    assert closed is False
    assert any(o.get("timeInForce") == "GTX" for o in client.all_orders)
    assert client.market_orders == []
    assert ("ETHUSDC", 901) not in client.cancelled
    assert client.cancelled_algo == []
    assert any(
        event_type == "survival_maker_attempt" and details.get("reason") == "CODEX_V1427_TIME_LOCK"
        for _, event_type, details in repo.events
    )
    assert any(
        event_type == "trail_maker_timeout" and details.get("reason") == "CODEX_V1427_TIME_LOCK"
        for _, event_type, details in repo.events
    )
    assert any(
        event_type == "survival_maker_deferred" and details.get("fallback_reason") == "timeout"
        for _, event_type, details in repo.events
    )
    assert any(event_type == "time_lock_maker_only_deferred" for _, event_type, _ in repo.events)
    assert not any(event_type == "survival_maker_fallback_market" for _, event_type, _ in repo.events)
@pytest.mark.asyncio
async def test_v1427_profile_time_lock_waits_when_slope_still_favorable(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.032,
        entry_price=100.0,
        mark_price=100.07,
        unrealized_pnl=0.00224,
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
        "codex_v1": {
            "enabled": True,
            "lane_code": "CNL-WPR-L",
            "lane": "v139_canary_watch_pre_reprice_long_s1",
            "metrics": {
                "market_state": "CNL-WPR-L:discount_mixed",
                "time_profit_lock_enabled": True,
                "time_lock_s": 60,
                "time_lock_min_bp": 6.0,
                "time_lock_slope_max_bp": 0.0,
                "time_lock_lookback_s": 30,
            },
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.032, side="LONG")
    hold_start_ms = 1_700_000_000_000
    manager._codex_time_lock_price_history[run["run_id"]] = [((hold_start_ms + 31_000) / 1000.0, 100.02)]
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 61_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "LONG",
        100.07,
        100.0,
        0.032,
        "SELL",
        hold_start_ms,
    )

    assert fired is False
    assert len(client.market_orders) == 0
    assert not any(event_type == "codex_survival_exit" for _, event_type, _ in repo.events)


@pytest.mark.asyncio
async def test_v146_stups_weak_chop_time_profit_lock_captures_fee_safe_giveback(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=-0.032,
        entry_price=100.0,
        mark_price=99.93,
        unrealized_pnl=0.00192,
        liquidation_price=120.0,
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
        "side": "SHORT",
        "codex_v1": {
            "enabled": True,
            "lane_code": "STUP-S",
            "lane": "codex_v1_stale_upmove_short_rng20_canary",
            "metrics": {"market_state": "STUP-S:weak_chop"},
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.032, side="SHORT")
    manager._trail_peak[run["run_id"]] = 99.89
    hold_start_ms = 1_700_000_000_000
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 181_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "SHORT",
        99.93,
        100.0,
        0.032,
        "BUY",
        hold_start_ms,
    )

    assert fired is True
    assert len(client.market_orders) == 1
    exit_event = next(details for _, event_type, details in repo.events if event_type == "codex_survival_exit")
    assert exit_event["reason"] == "STUPS_TIME_PROFIT_LOCK"
    assert exit_event["survival_profile"] == "v146_stups_time_profit_lock"
    assert exit_event["market_state"] == "STUP-S:weak_chop"
    assert exit_event["mfe_bp"] == pytest.approx(11.0)
    assert exit_event["current_bp"] == pytest.approx(7.0)
    assert exit_event["giveback_bp"] == pytest.approx(4.0)
    assert exit_event["profit_lock_fee_floor_enabled"] is True
    assert exit_event["profit_lock_fee_floor_bp"] == pytest.approx(6.0)


@pytest.mark.asyncio
async def test_v147_stups_weak_chop_stall_profit_lock_captures_medium_profit(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=-0.032,
        entry_price=100.0,
        mark_price=99.93,
        unrealized_pnl=0.00192,
        liquidation_price=120.0,
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
        "side": "SHORT",
        "codex_v1": {
            "enabled": True,
            "lane_code": "STUP-S",
            "lane": "codex_v1_stale_upmove_short_rng20_canary",
            "metrics": {"market_state": "STUP-S:weak_chop"},
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.032, side="SHORT")
    hold_start_ms = 1_700_000_000_000
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 301_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "SHORT",
        99.93,
        100.0,
        0.032,
        "BUY",
        hold_start_ms,
    )

    assert fired is True
    assert len(client.market_orders) == 1
    exit_event = next(details for _, event_type, details in repo.events if event_type == "codex_survival_exit")
    assert exit_event["reason"] == "STUPS_STALL_PROFIT_LOCK"
    assert exit_event["survival_profile"] == "v146_stups_time_profit_lock"
    assert exit_event["market_state"] == "STUP-S:weak_chop"
    assert exit_event["current_bp"] == pytest.approx(7.0)


@pytest.mark.asyncio
async def test_v1414_stups_mixed_profit_lock_captures_fee_safe_medium_profit(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=-0.032,
        entry_price=100.0,
        mark_price=99.93,
        unrealized_pnl=0.00192,
        liquidation_price=120.0,
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
        "side": "SHORT",
        "codex_v1": {
            "enabled": True,
            "lane_code": "STUP-S",
            "lane": "codex_v1_stale_upmove_short_rng20_canary",
            "metrics": {"market_state": "STUP-S:mixed"},
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.032, side="SHORT")
    manager._trail_peak[run["run_id"]] = 99.89
    hold_start_ms = 1_700_000_000_000
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 46_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "SHORT",
        99.93,
        100.0,
        0.032,
        "BUY",
        hold_start_ms,
    )

    assert fired is True
    assert len(client.market_orders) == 1
    exit_event = next(details for _, event_type, details in repo.events if event_type == "codex_survival_exit")
    assert exit_event["reason"] == "STUPS_TIME_PROFIT_LOCK"
    assert exit_event["survival_profile"] == "v146_stups_time_profit_lock"
    assert exit_event["market_state"] == "STUP-S:mixed"
    assert exit_event["mfe_bp"] == pytest.approx(11.0)
    assert exit_event["current_bp"] == pytest.approx(7.0)
    assert exit_event["giveback_bp"] == pytest.approx(4.0)
    assert exit_event["profit_lock_fee_floor_enabled"] is True
    assert exit_event["profit_lock_fee_floor_bp"] == pytest.approx(6.0)

@pytest.mark.asyncio
async def test_v139b_wpr_survival_damage_after_240s(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=99.93,
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
        99.93,
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
    assert exit_event["current_bp"] == pytest.approx(-7.0)

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
async def test_v1443_max_hold_loss_near_flat_tries_maker_scratch():
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=99.99,
        unrealized_pnl=-0.0012,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    client.book = {"bidPrice": "99.99", "askPrice": "100.00"}
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_v1443_max_hold_loss_maker_ttl_seconds=0,
            mainnet_codex_v1443_max_hold_loss_scratch_min_bp=-2.5,
            mainnet_codex_v1443_max_hold_loss_scratch_max_bp=0.75,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _run(
        avg_entry_price=100.0,
        signal_json=json.dumps(
            {
                "side": "LONG",
                "codex_v1": {
                    "enabled": True,
                    "lane_code": "STUP-S",
                    "metrics": {"market_state": "STUP-S:mixed"},
                },
            }
        ),
    )

    await manager._close_position("ETHUSDC", "SELL", 0.12, "MAX_HOLD_LOSS", run)

    assert any(o.get("timeInForce") == "GTX" and o.get("reduceOnly") is True for o in client.all_orders)
    assert len(client.market_orders) == 1
    trigger = next(details for _, event_type, details in repo.events if event_type == "codex_v1443_near_flat_scratch_triggered")
    assert trigger["original_reason"] == "MAX_HOLD_LOSS"
    assert trigger["scratch_reason"] == "CODEX_V1443_MAX_HOLD_LOSS_SCRATCH"
    assert trigger["current_bp"] == pytest.approx(-1.0)
    assert any(
        event_type == "survival_maker_attempt"
        and details.get("reason") == "CODEX_V1443_MAX_HOLD_LOSS_SCRATCH"
        for _, event_type, details in repo.events
    )
    assert any(
        event_type == "close_submitted" and details.get("reason") == "MAX_HOLD_LOSS"
        for _, event_type, details in repo.events
    )

def test_v1436_max_hold_win_defers_fee_negative_codex_flat_close():
    manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_v1436_max_hold_win_fee_floor_defer_enabled=True,
            mainnet_codex_v1436_max_hold_win_fee_floor_defer_extra_bars=2,
            mainnet_codex_max_hold_profit_min_floor_bp=5.0,
        ),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    signal = {
        "side": "LONG",
        "codex_v1": {
            "enabled": True,
            "metrics": {"market_state": "CNL-WPR-L:falling_discount_trap"},
        },
    }

    assert manager._should_defer_codex_max_hold_win_fee_floor(signal, "LONG", 100.0, 100.01, 1, 1) is True
    assert manager._should_codex_max_hold_profit_lock(signal, "LONG", 100.0, 100.01) is False
    assert manager._should_defer_codex_max_hold_win_fee_floor(signal, "LONG", 100.0, 100.01, 3, 1) is False
    assert manager._should_defer_codex_max_hold_win_fee_floor(signal, "LONG", 100.0, 100.07, 1, 1) is False
    assert manager._should_codex_max_hold_profit_lock(signal, "LONG", 100.0, 100.07) is True

@pytest.mark.asyncio
async def test_v1433_max_hold_win_profit_lock_is_maker_only(monkeypatch):
    from src.gridbot.mainnet import one_run as _mod

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(_mod.asyncio, "sleep", _no_sleep)
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=100.06,
        unrealized_pnl=0.0084,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    client.book = {"bidPrice": "100.06", "askPrice": "100.07"}
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_survival_exit_use_maker=True,
            mainnet_codex_max_hold_profit_maker_ttl_seconds=0,
            mainnet_codex_max_hold_profit_min_floor_bp=5.0,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _run(
        avg_entry_price=100.0,
        signal_json=json.dumps(
            {
                "side": "LONG",
                "codex_v1": {
                    "enabled": True,
                    "lane_code": "STUP-S",
                    "metrics": {"market_state": "STUP-S:mixed"},
                },
            }
        ),
    )

    submitted = await manager._close_position("ETHUSDC", "SELL", 0.12, "CODEX_MAX_HOLD_PROFIT_LOCK", run)

    assert submitted is False
    assert any(o.get("timeInForce") == "GTX" and o.get("reduceOnly") is True for o in client.all_orders)
    assert client.market_orders == []
    assert any(et == "max_hold_profit_maker_only_deferred" for _, et, _ in repo.events)


def test_v1434_stups_fast_floor_only_targets_full_exit_v1430_profiles():
    manager = MainnetOneRunManager(
        _settings(mainnet_codex_v1434_stups_fast_floor_maker_only_enabled=True),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    full_exit_run = _run(
        signal_json=json.dumps(
            {
                "side": "SHORT",
                "wildcat": {"partial_exit_pct": 1.0},
                "codex_v1": {
                    "enabled": True,
                    "lane_code": "STUP-S",
                    "metrics": {
                        "policy_tag": "v1430_loss_prune_exec",
                        "market_state": "STUP-S:mixed",
                        "partial_exit_pct": 1.0,
                        "trail_floor_bp": 5.0,
                    },
                },
            }
        )
    )
    staged_tp_run = _run(
        signal_json=json.dumps(
            {
                "side": "SHORT",
                "wildcat": {"partial_exit_pct": 0.70, "partial_tp_pct": 0.0006},
                "codex_v1": {
                    "enabled": True,
                    "lane_code": "STUP-S",
                    "metrics": {
                        "policy_tag": "v1430_loss_prune_exec",
                        "market_state": "STUP-S:mixed",
                        "partial_exit_pct": 0.70,
                        "tp1_bp": 6.0,
                        "trail_floor_bp": 5.0,
                    },
                },
            }
        )
    )

    assert manager._stups_fast_floor_lock_trigger_bp(full_exit_run) == pytest.approx(5.0)
    assert manager._stups_fast_floor_lock_trigger_bp(staged_tp_run) is None
    staged_profile = manager._stups_staged_tp1_profile(staged_tp_run, require_floor_enabled=True)
    assert staged_profile is not None
    assert staged_profile["partial_exit_pct"] == pytest.approx(0.70)
    assert staged_profile["trigger_bp"] == pytest.approx(6.0)
    assert staged_profile["floor_bp"] == pytest.approx(5.0)
    assert manager._stups_staged_runner_pre_tp1_watch_enabled(staged_tp_run) is True


@pytest.mark.asyncio
async def test_v1435_stups_staged_tp1_floor_replaces_tp1_with_maker_order():
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=100.055,
        unrealized_pnl=0.0066,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    client.book = {"bidPrice": "100.055", "askPrice": "100.065"}
    client.open_orders.append(
        {"orderId": 901, "clientOrderId": "cry3mn_test_tp1", "status": "NEW", "origQty": "0.084"}
    )
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_v1435_stups_tp1_floor_enabled=True,
            mainnet_codex_v1435_stups_tp1_floor_floor_bp=5.0,
            mainnet_codex_v1435_stups_tp1_floor_trigger_bp=6.0,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _run(
        avg_entry_price=100.0,
        signal_json=json.dumps(
            {
                "side": "LONG",
                "wildcat": {"partial_exit_pct": 0.70, "partial_tp_pct": 0.0006},
                "codex_v1": {
                    "enabled": True,
                    "lane_code": "STUP-S",
                    "metrics": {
                        "policy_tag": "v1430_loss_prune_exec",
                        "market_state": "STUP-S:mixed",
                        "partial_exit_pct": 0.70,
                        "tp1_bp": 6.0,
                    },
                },
            }
        ),
    )

    placed = await manager._maybe_fire_stups_staged_tp1_floor_lock(
        run,
        "LONG",
        "SELL",
        client.position,
        100.0,
        100.055,
        100.06,
    )

    assert placed is True
    assert ("ETHUSDC", 901) in client.cancelled
    floor_orders = [o for o in client.open_orders if o.get("clientOrderId") == "cry3mn_test_tp1_floor"]
    assert len(floor_orders) == 1
    assert floor_orders[0]["timeInForce"] == "GTX"
    assert floor_orders[0]["reduceOnly"] is True
    assert floor_orders[0]["side"] == "SELL"
    assert manager._tp_layer_qty["cry3mn_test"]["tp1"] == pytest.approx(0.084)
    assert "cry3mn_test" in manager._partial_order_armed
    assert client.market_orders == []
    assert any(et == "stups_tp1_floor_lock_signal" for _, et, _ in repo.events)
    assert any(et == "stups_tp1_floor_lock_placed" for _, et, _ in repo.events)


@pytest.mark.asyncio
async def test_v1434_stups_fast_floor_lock_is_maker_only(monkeypatch):
    from src.gridbot.mainnet import one_run as _mod

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(_mod.asyncio, "sleep", _no_sleep)
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=100.06,
        unrealized_pnl=0.0084,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    client.book = {"bidPrice": "100.06", "askPrice": "100.07"}
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_survival_exit_use_maker=True,
            mainnet_codex_v1434_stups_fast_floor_maker_ttl_seconds=0,
            mainnet_codex_v1434_stups_fast_floor_floor_bp=5.0,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _run(
        avg_entry_price=100.0,
        signal_json=json.dumps(
            {
                "side": "LONG",
                "codex_v1": {
                    "enabled": True,
                    "lane_code": "STUP-S",
                    "metrics": {
                        "policy_tag": "v1430_loss_prune_exec",
                        "market_state": "STUP-S:mixed",
                        "trail_floor_bp": 5.0,
                    },
                },
            }
        ),
    )

    submitted = await manager._close_position("ETHUSDC", "SELL", 0.12, "CODEX_STUPS_FAST_FLOOR_LOCK", run)

    assert submitted is False
    assert any(o.get("timeInForce") == "GTX" and o.get("reduceOnly") is True for o in client.all_orders)
    assert client.market_orders == []
    assert any(et == "stups_fast_floor_maker_only_deferred" for _, et, _ in repo.events)

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
    pending_events = [event for event in repo.events if event[1] == "loop_cooldown_pending"]
    assert len(pending_events) == 1
    assert pending_events[0][2]["resume_at_ms"] == future
    assert pending_events[0][2]["completed"] == 2

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
        _settings(mainnet_recovery_enabled=True, mainnet_codex_recovery_enabled=True, mainnet_recovery_steps=1), client, repo, FakeTelegramApp()
    )
    run = _run(side="SHORT", qty=0.119)
    monkeypatch.setattr(orm, "evaluate_dca_guard", lambda c, s: (False, "trend=up（趨勢逆行）"))

    result = await manager._maybe_recovery(run, {}, client.position)

    assert result is False
    assert all("_dca" not in str(o.get("clientOrderId") or "") for o in client.all_orders)
    assert "dca_guard_blocked" in _recovery_skip_reasons(repo)


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
        _settings(mainnet_recovery_enabled=True, mainnet_codex_recovery_enabled=True, mainnet_recovery_steps=1), client, repo, FakeTelegramApp()
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
        _settings(mainnet_recovery_enabled=True, mainnet_codex_recovery_enabled=True, mainnet_recovery_steps=1), client, repo, FakeTelegramApp()
    )
    run = _run(side="SHORT", qty=0.119)
    # Guard would allow, but the run already booked a partial exit.
    monkeypatch.setattr(orm, "evaluate_dca_guard", lambda c, s: (True, "range ok"))
    manager._partial_exits.add(run["run_id"])

    result = await manager._maybe_recovery(run, {}, client.position)

    assert result is False
    assert all("_dca" not in str(o.get("clientOrderId") or "") for o in client.all_orders)
    assert "partial_exit" in _recovery_skip_reasons(repo)


@pytest.mark.asyncio
async def test_codex_recovery_cnl_wpr_l_whitelist_allows_canary(monkeypatch):
    import src.gridbot.mainnet.one_run as orm

    client = FakeClient()
    client.position = _codex_dca_short_position()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(mainnet_recovery_enabled=True, mainnet_codex_recovery_enabled=True, mainnet_recovery_steps=1),
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _codex_recovery_run("CNL-WPR-L")
    monkeypatch.setattr(orm, "evaluate_dca_guard", lambda c, s: (True, "range ok"))

    result = await manager._maybe_recovery(run, json.loads(run["signal_json"]), client.position)

    assert result is True
    assert any(event_type == "recovery_entry_placed" for _, event_type, _ in repo.events)
    assert _recovery_skip_reasons(repo) == []


@pytest.mark.asyncio
async def test_codex_recovery_stups_rejected_by_default_whitelist(monkeypatch):
    import src.gridbot.mainnet.one_run as orm

    client = FakeClient()
    client.position = _codex_dca_short_position()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(mainnet_recovery_enabled=True, mainnet_codex_recovery_enabled=True, mainnet_recovery_steps=1),
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _codex_recovery_run("STUP-S")
    monkeypatch.setattr(orm, "evaluate_dca_guard", lambda c, s: (True, "range ok"))

    result = await manager._maybe_recovery(run, json.loads(run["signal_json"]), client.position)

    assert result is False
    assert all("_dca" not in str(o.get("clientOrderId") or "") for o in client.all_orders)
    skips = _recovery_skip_events(repo)
    assert skips[-1]["reason"] == "codex_recovery_lane_not_whitelisted"
    assert skips[-1]["codex_recovery_lane_code"] == "STUP-S"


@pytest.mark.asyncio
async def test_codex_recovery_basket_cap_skip_reason(monkeypatch):
    import src.gridbot.mainnet.one_run as orm

    client = FakeClient()
    client.position = _codex_dca_short_position(unrealized_pnl=-0.6)
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(mainnet_recovery_enabled=True, mainnet_codex_recovery_enabled=True, mainnet_recovery_steps=1, mainnet_codex_recovery_max_basket_loss_usdc=0.50),
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _codex_recovery_run("CNL-WPR-L")
    monkeypatch.setattr(orm, "evaluate_dca_guard", lambda c, s: (True, "range ok"))

    result = await manager._maybe_recovery(run, json.loads(run["signal_json"]), client.position)

    assert result is False
    skip = _recovery_skip_events(repo)[-1]
    assert skip["reason"] == "basket_loss_cap"
    assert skip["recovery_block_reason"] is None


@pytest.mark.asyncio
async def test_codex_recovery_drift_gate_skip_reason(monkeypatch):
    client = FakeClient()
    client.position = _codex_dca_short_position()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(mainnet_recovery_enabled=True, mainnet_codex_recovery_enabled=True, mainnet_recovery_steps=1, mainnet_dca_guard_enabled=False, mainnet_dca_drift_gate_bp=1.0),
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _codex_recovery_run("CNL-WPR-L")

    async def _load_candles(_symbol):
        return []

    monkeypatch.setattr(manager, "_load_candles", _load_candles)
    monkeypatch.setattr(manager, "_dca_drift_blocked", lambda candles: 2.5)

    result = await manager._maybe_recovery(run, json.loads(run["signal_json"]), client.position)

    assert result is False
    assert any(event_type == "dca_drift_blocked" for _, event_type, _ in repo.events)
    assert "drift_gate" in _recovery_skip_reasons(repo)


@pytest.mark.asyncio
async def test_codex_recovery_max_layers_skip_reason(monkeypatch):
    import src.gridbot.mainnet.one_run as orm

    client = FakeClient()
    client.position = _codex_dca_short_position()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(mainnet_recovery_enabled=True, mainnet_codex_recovery_enabled=True, mainnet_recovery_steps=1),
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _codex_recovery_run("CNL-WPR-L")
    manager._recovery_counts[run["run_id"]] = 1
    monkeypatch.setattr(orm, "evaluate_dca_guard", lambda c, s: (True, "range ok"))

    result = await manager._maybe_recovery(run, json.loads(run["signal_json"]), client.position)

    assert result is False
    assert "max_layers_reached" in _recovery_skip_reasons(repo)


@pytest.mark.asyncio
async def test_entry_payload_includes_runtime_dca_config():
    from src.gridbot.strategy.long_pullback import SignalPlan
    from src.gridbot.strategy.wildcat_live import WildcatLiveDecision

    client = FakeClient()
    repo = FakeRepo()
    config_repo = FakeConfigRepo({"mainnet_dca_enabled": "false"})
    manager = MainnetOneRunManager(
        _settings(mainnet_recovery_enabled=True, mainnet_codex_recovery_enabled=True, mainnet_recovery_steps=1),
        client,
        repo,
        FakeTelegramApp(),
        config_repo=config_repo,
    )
    run = _run(run_id="cry3mn_payload_dca", status="ARMED", side="LONG", cumulative_notional_usdc=0.0)
    signal = SignalPlan(
        action="BUY",
        confidence=80,
        score=80,
        symbol="ETHUSDC",
        price=100.0,
        rsi=50.0,
        atr=1.0,
        support=99.0,
        vwap=100.0,
        entries=[100.0],
        entry_weights=[1.0],
        stop_loss=99.0,
        take_profits=[100.1],
        planned_notional_usdc=50.0,
        planned_margin_usdc=50.0 / 75.0,
        planned_qty=0.5,
        reasons=["wildcat:S1_BB_RSI"],
        risk_notes=[],
    )
    decision = WildcatLiveDecision(
        signal=signal,
        strategy="S1_BB_RSI",
        side="LONG",
        tp_pct=0.001,
        sl_pct=0.001,
        partial_exit_pct=1.0,
        partial_tp_pct=0.001,
        recovery_steps=1,
        recovery_trigger_pct=0.0005,
        recovery_tp_shrink=0.45,
        adverse_exit_bars=10,
        adverse_exit_loss_pct=0.0007,
        max_holding_bars=24,
        params_label="unit",
    )
    codex_decision = CodexV1Decision(
        accepted=True,
        version="test",
        baseline="test",
        lane="codex_v1_wpr_unit",
        lane_code="CNL-WPR-L",
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="unit",
        metrics={"market_state": "CNL-WPR-L:unit"},
        policy_tag="unit",
    )

    await manager._place_entry(run, decision, codex_decision=codex_decision)

    payload = repo.updated[-1][1]["signal_json"]
    assert payload["runtime_dca_enabled"] is False
    assert payload["codex_recovery_allowed"] is True
    assert payload["effective_recovery_enabled"] is False
    assert payload["recovery_block_reason"] == "runtime_dca_disabled"
    assert payload["wildcat"]["runtime_dca_enabled"] is False
    assert payload["codex_v1"]["runtime_dca_enabled"] is False
    assert payload["codex_v1"]["codex_recovery_allowed"] is True


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
            mainnet_trail_disable_final_tp=False,
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
async def test_trail_waits_for_tp1_before_starting_runner_mode():
    import asyncio
    import json
    import time as _time
    client = FakeClient()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_recovery_enabled=False,
            mainnet_trail_enabled=True,
            mainnet_trail_require_partial_fill=True,
            mainnet_trail_arm_frac=0.5,
            mainnet_trail_giveback_frac=0.5,
            mainnet_trail_exit_use_maker=False,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _run(
        signal_json='{"side":"LONG","take_profit":101.0,"stop_loss":97.5,"wildcat":{"tp_pct":0.01,"sl_pct":0.025}}',
        avg_entry_price=100.0,
        armed_at_ms=int(_time.time() * 1000),
    )
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=101.0,
        unrealized_pnl=0.12,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )

    manager._start_trail_watch(run, "LONG", "SELL", 0.01)
    assert run["run_id"] not in manager._trail_watch_tasks
    fired = await manager._maybe_trailing_exit(
        run, json.loads(run["signal_json"]), client.position, "LONG", 101.0, 100.0, 0.12, "SELL"
    )
    assert fired is False
    assert run["run_id"] not in manager._trail_armed

    manager._partial_exits.add(run["run_id"])
    manager._start_trail_watch(run, "LONG", "SELL", 0.01)
    assert run["run_id"] in manager._trail_watch_tasks
    manager._trail_watch_tasks[run["run_id"]].cancel()
    with pytest.raises(asyncio.CancelledError):
        await manager._trail_watch_tasks[run["run_id"]]


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
            mainnet_trail_require_partial_fill=False,
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
            mainnet_trail_require_partial_fill=False,
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
        mainnet_codex_v143_w6a_shadow_only_enabled=False,
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
    assert codex_dec.notional_mult == pytest.approx(0.10, abs=0.01)
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
        mainnet_codex_v143_w6a_shadow_only_enabled=False,
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
    assert adjusted.signal.planned_notional_usdc == pytest.approx(400.0)
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
async def test_v143_w6a_shadow_only_has_explicit_shadow_metadata(monkeypatch):
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision as StrategyDecision
    from src.gridbot.strategy.long_pullback import SignalPlan
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
        version="_codex_v1.4.7",
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
            "rng15": 50.0,
            "d30": -20.0,
            "adv3": -3.0,
            "rsi": 45.0,
            "vwap_dist_bp": -20.0,
            "pullback_from_recent_high_bp": 20.0,
            "price_above_or_reclaimed_vwap": 1.0,
            "reprice_wait_elapsed_seconds": 10.0,
            "maker_fee_bp": 0.0,
            "kill_switch": "off",
            "open_position": "false",
            "open_entry_order": "0",
            "open_reduce_order": "",
        }

    monkeypatch.setattr(manager, "_build_codex_v1_live_features_for_decision", _mock_feat)
    run = {"run_id": "test_v143_w6a_shadow", "symbol": "ETHUSDC", "status": "ARMED", "strategy_label": "codex_v1"}

    adjusted, _raw_dec, codex_dec, _final_feat = await manager._apply_codex_v1_gate(
        run, decision, [], rng15=50.0, drift_bp=-20.0
    )

    assert adjusted is None
    assert codex_dec.reason == "v143_w6a_shadow_only"
    assert codex_dec.shadow_lane == "SH_W6A_V143_SHADOW_ONLY"
    assert codex_dec.metrics["policy_tag"] == "v143_w6a_shadow_only"
    assert codex_dec.metrics["live_action"] == "shadow_only"
    skipped_event = next(details for _, event_type, details in repo.events if event_type == "entry_codex_v1_skipped")
    assert skipped_event["effective_execution"]["shadow_lane"] == "SH_W6A_V143_SHADOW_ONLY"


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
        _settings(
            mainnet_one_run_enabled=True,
            mainnet_codex_v1_enabled=True,
            # The conservative 1m-bar evaluator only accepts a fill from a
            # candle fully contained inside the entry window.
            mainnet_entry_order_ttl_seconds=120,
        ),
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
    assert any(
        event[1] == "entry_codex_v1_shadow_sample_started"
        for event in repo.events
    ), repo.events
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
            mainnet_codex_v143_w6a_shadow_only_enabled=False,
            # Keep both synthetic 1m candles inside the entry window.
            mainnet_entry_order_ttl_seconds=120,
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
    assert any(
        event[1] == "entry_codex_v1_shadow_sample_started"
        for event in repo.events
    ), repo.events
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


def test_v1424_l1mr_strong_fall_guard_blocks_live_promotion():
    reason, metrics = MainnetOneRunManager._codex_v1424_l1mr_guard(
        {
            "d30": -86.25776,
            "slope60": -11.210547,
            "slope120": -30.752706,
            "vwap_dist_bp": -32.158085,
            "rng15": 40.531527,
            "range_pos_15": 0.174051,
            "rsi": 39.295436,
        }
    )

    assert reason == "v1424_l1mr_strong_fall_long_block"
    assert metrics["l1mr_guard_reason"] == "v1424_l1mr_strong_fall_long_block"
    assert metrics["l1mr_guard_d30"] == -86.25776


def test_v1424_l1mr_guard_allows_non_strong_fall_sample():
    reason, metrics = MainnetOneRunManager._codex_v1424_l1mr_guard(
        {
            "d30": -25.0,
            "slope60": -2.0,
            "slope120": -8.0,
            "vwap_dist_bp": -12.0,
        }
    )

    assert reason is None
    assert metrics["l1mr_guard_reason"] is None


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
            "rsi": 55.0,
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
    assert codex_dec.reason == "v1420_wpr_deep_discount_stable_tight"
    assert codex_dec.policy_tag == "v1420_wpr_deep_discount_stable_tight"
    assert codex_dec.regime == "CNL-WPR-L:deep_discount_stable"
    assert codex_dec.requested_notional_usdc == pytest.approx(50.0)
    assert codex_dec.entry_offset_bp == pytest.approx(2.0)
    assert adjusted.signal.planned_notional_usdc == pytest.approx(50.0)
    assert adjusted.signal.entries == [pytest.approx(2999.4)]
    assert adjusted.signal.stop_loss == pytest.approx(2999.4 * (1 - 8.0 / 10_000.0))
    assert adjusted.sl_pct == pytest.approx(8.0 / 10_000.0)
    assert adjusted.partial_tp_pct == pytest.approx(0.0006)
    assert adjusted.partial_exit_pct == pytest.approx(1.0)
    assert "no_dca" in codex_dec.risk_tags
    assert "v139b_wpr_waiting_scratch" in codex_dec.risk_tags
    assert codex_dec.metrics["canary_daily_count_24h"] is None
    assert codex_dec.metrics["canary_daily_cap"] is None
    assert codex_dec.metrics["wpr_profile"] == "CNL-WPR-L:deep_discount_stable"
    assert codex_dec.metrics["ttl_s"] == 60
    assert codex_dec.metrics["be_bp"] == 0.0
    assert codex_dec.metrics["staged_entry_reprice_enabled"] is True
    assert tuple(codex_dec.metrics["staged_entry_bps"]) == pytest.approx((2.0, 1.0, 0.0))
    assert codex_dec.metrics["profile_patch"] == "v1420_wpr_deep_discount_stable_tight"
    accepted_event = next(details for _, event_type, details in repo.events if event_type == "entry_codex_v1_accepted")
    assert accepted_event["effective_execution"]["lane_code"] == "CNL-WPR-L"
    assert accepted_event["decision"]["metrics"]["canary_policy"] == "v1420_wpr_deep_discount_stable_tight"



def test_v1420_wpr_discount_mixed_is_selective_runner_bucket():
    manager = MainnetOneRunManager(_settings(mainnet_codex_v1_enabled=True), FakeClient(), FakeRepo(), FakeTelegramApp())
    profile = WPR_V143_PROFILES["CNL-WPR-L:discount_mixed"]

    assert profile["adaptive_tp_engine"] == "v1420_wpr_discount_mixed_runner"
    assert profile["tp1_bp"] == pytest.approx(5.0)
    assert profile["full_tp_bp"] == pytest.approx(16.0)
    assert profile["partial_exit_pct"] == pytest.approx(0.45)
    assert profile["pre_tp_profit_lock_enabled"] is False
    assert manager._codex_v1419_wpr_block_reason(
        "CNL-WPR-L:discount_mixed",
        {"rng15": 24.0, "d30": -8.0, "rsi": 44.0, "vwap_dist_bp": -8.0, "range_pos_15": 0.33, "pullback_from_recent_high_bp": 25.0},
    ) is None
    assert manager._codex_v1419_wpr_block_reason(
        "CNL-WPR-L:discount_mixed",
        {"rng15": 24.0, "d30": -8.0, "rsi": 44.0, "vwap_dist_bp": -8.0, "range_pos_15": 0.60, "pullback_from_recent_high_bp": 25.0},
    ) == "v1420_wpr_discount_mixed_bad_block"
    adjusted = manager._codex_v1419_wpr_profile_for_state(
        "CNL-WPR-L:discount_mixed",
        profile,
        {"rng15": 24.0, "d30": -8.0, "rsi": 44.0, "vwap_dist_bp": -8.0, "range_pos_15": 0.33, "pullback_from_recent_high_bp": 25.0},
    )
    assert adjusted["profile_patch"] == "v1420_wpr_discount_mixed_runner"
    assert adjusted["tp1_bp"] == pytest.approx(5.0)
    assert adjusted["full_tp_bp"] == pytest.approx(16.0)
    assert adjusted["partial_exit_pct"] == pytest.approx(0.45)



def test_v1414_wpr_state_classifier_splits_falling_and_delayed_reclaim():
    manager = MainnetOneRunManager(_settings(mainnet_codex_v1_enabled=True), FakeClient(), FakeRepo(), FakeTelegramApp())

    assert manager._codex_v143_wpr_market_state(
        {"rng15": 96.0, "d30": -42.0, "rsi": 45.0, "vwap_dist_bp": -24.0}
    ) == "CNL-WPR-L:falling_continuation_probe"
    assert manager._codex_v143_wpr_market_state(
        {"rng15": 52.0, "d30": 27.0, "rsi": 39.0, "vwap_dist_bp": -22.0}
    ) == "CNL-WPR-L:discount_delayed_reclaim"
    assert manager._codex_v143_wpr_market_state(
        {"rng15": 78.0, "d30": -62.0, "rsi": 44.0, "vwap_dist_bp": -52.0}
    ) == "CNL-WPR-L:falling_discount_trap"

    continuation = WPR_V143_PROFILES["CNL-WPR-L:falling_continuation_probe"]
    assert continuation["entry_bp"] == pytest.approx(3.0)
    assert continuation["profile_patch"] == "v1420_wpr_falling_continuation_probe_filtered"
    assert continuation["adaptive_tp_engine"] == "v1420_wpr_falling_continuation_probe_filtered"
    assert WPR_V143_PROFILES["CNL-WPR-L:discount_delayed_reclaim"]["full_tp_bp"] == pytest.approx(8.0)
    assert WPR_V143_PROFILES["CNL-WPR-L:discount_delayed_reclaim"]["sl_bp"] == pytest.approx(8.0)
    assert WPR_V143_PROFILES["CNL-WPR-L:discount_delayed_reclaim"]["partial_exit_pct"] == pytest.approx(0.70)
    falling_trap = WPR_V143_PROFILES["CNL-WPR-L:falling_discount_trap"]
    assert falling_trap["tp1_bp"] == pytest.approx(4.0)
    assert falling_trap["full_tp_bp"] == pytest.approx(10.0)
    assert falling_trap["partial_exit_pct"] == pytest.approx(0.60)
    assert falling_trap["be_bp"] == pytest.approx(4.0)
    assert falling_trap["profit_lock_mfe_bp"] == pytest.approx(6.0)
    assert falling_trap["profit_lock_giveback_bp"] == pytest.approx(2.0)
    assert falling_trap["adaptive_tp_engine"] == "v1416_wpr_scalp_runner"


def test_v1415_wpr_strong_fall_trap_uses_deep_entry_only_for_knife_slice():
    manager = MainnetOneRunManager(_settings(mainnet_codex_v1_enabled=True), FakeClient(), FakeRepo(), FakeTelegramApp())
    base_profile = WPR_V143_PROFILES["CNL-WPR-L:falling_discount_trap"]

    strong_fall_features = {
        "rng15": 57.8,
        "d30": -89.8,
        "rsi": 14.4,
        "vwap_dist_bp": -24.4,
    }
    assert manager._codex_v143_wpr_market_state(strong_fall_features) == "CNL-WPR-L:falling_discount_trap"

    adjusted = manager._codex_v1415_wpr_profile_for_state(
        "CNL-WPR-L:falling_discount_trap",
        base_profile,
        strong_fall_features,
    )

    assert adjusted["entry_bp"] == pytest.approx(8.0)
    assert adjusted["tp1_bp"] == pytest.approx(4.0)
    assert adjusted["full_tp_bp"] == pytest.approx(10.0)
    assert adjusted["partial_exit_pct"] == pytest.approx(0.60)
    assert adjusted["sl_bp"] == pytest.approx(15.0)
    assert adjusted["profile_patch"] == "v1415_wpr_strong_fall_deep_entry"
    assert adjusted["strong_fall_deep_entry"] is True

    runner = manager._codex_v1419_wpr_profile_for_state(
        "CNL-WPR-L:falling_discount_trap",
        adjusted,
        strong_fall_features,
    )
    assert runner["entry_bp"] == pytest.approx(2.0)
    assert runner["tp1_bp"] == pytest.approx(6.0)
    assert runner["full_tp_bp"] == pytest.approx(20.0)
    assert runner["partial_exit_pct"] == pytest.approx(0.70)
    assert runner["sl_bp"] == pytest.approx(8.0)
    assert runner["profile_patch"] == "v1420_wpr_falling_discount_runner"

    ordinary_falling = manager._codex_v1415_wpr_profile_for_state(
        "CNL-WPR-L:falling_discount_trap",
        base_profile,
        {"rng15": 78.0, "d30": -35.0, "rsi": 44.0, "vwap_dist_bp": -30.0},
    )
    assert ordinary_falling["entry_bp"] == pytest.approx(0.0)
    assert "profile_patch" not in ordinary_falling
    assert manager._codex_v1419_wpr_block_reason(
        "CNL-WPR-L:falling_discount_trap",
        {"rng15": 78.0, "d30": -35.0, "rsi": 52.0, "vwap_dist_bp": -30.0},
    ) == "v1420_wpr_falling_bad_slice_block"
    assert manager._codex_v1419_wpr_block_reason(
        "CNL-WPR-L:falling_discount_trap",
        {"rng15": 101.0, "d30": -120.0, "rsi": 38.0, "vwap_dist_bp": -35.0},
    ) == "v1420_wpr_falling_bad_slice_block"
    assert manager._codex_v1419_wpr_block_reason(
        "CNL-WPR-L:falling_discount_trap",
        {"rng15": 78.0, "d30": -35.0, "rsi": 44.0, "vwap_dist_bp": -30.0},
    ) is None
    assert manager._codex_v1419_wpr_block_reason(
        "CNL-WPR-L:falling_continuation_probe",
        {"rng15": 96.0, "d30": -52.0, "rsi": 39.0, "vwap_dist_bp": -28.0, "range_bp": 23.2},
    ) == "v1420_wpr_continuation_hirange_block"
    assert manager._codex_v1419_wpr_block_reason(
        "CNL-WPR-L:falling_continuation_probe",
        {"rng15": 96.0, "d30": -35.0, "rsi": 45.0, "vwap_dist_bp": -24.0, "range_bp": 20.0},
    ) is None
    continuation_profile = manager._codex_v1419_wpr_profile_for_state(
        "CNL-WPR-L:falling_continuation_probe",
        WPR_V143_PROFILES["CNL-WPR-L:falling_continuation_probe"],
        {"rng15": 96.0, "d30": -35.0, "rsi": 45.0, "vwap_dist_bp": -24.0, "range_bp": 20.0},
    )
    assert continuation_profile["profile_patch"] == "v1420_wpr_falling_continuation_probe_filtered"

@pytest.mark.asyncio
async def test_v1413_codex_state_throttle_rehydrates_falling_trap_only():
    from dataclasses import replace
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision

    now_ms = 1_782_700_000_000

    def _completed_loss(run_id: str, age_s: int, market_state: str) -> dict:
        completed_at = now_ms - age_s * 1000
        return {
            "run_id": run_id,
            "status": "COMPLETED",
            "armed_at_ms": completed_at,
            "updated_at_ms": completed_at,
            "completed_at_ms": completed_at,
            "realized_pnl_usdc": -0.08,
            "commission_usdc": 0.02,
            "signal_json": json.dumps({
                "codex_v1": {
                    "enabled": True,
                    "lane_code": "CNL-WPR-L",
                    "metrics": {"market_state": market_state},
                }
            }),
        }

    repo = FakeRepo()
    repo.recent_runs = [
        _completed_loss("loss_a", 1500, "CNL-WPR-L:falling_discount_trap"),
        _completed_loss("loss_b", 300, "CNL-WPR-L:falling_discount_trap"),
        _completed_loss("mixed_loss", 120, "CNL-WPR-L:discount_mixed"),
    ]
    manager = MainnetOneRunManager(_settings(mainnet_codex_v1_enabled=True), FakeClient(), repo, FakeTelegramApp())
    falling = CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.13",
        baseline="test",
        lane="v139_canary_watch_pre_reprice_long_s1",
        lane_code="CNL-WPR-L",
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v145_wpr_profit_lock_exec",
        metrics={"market_state": "CNL-WPR-L:falling_discount_trap"},
    )

    block = await manager._codex_state_throttle_block(falling, now_ms)
    mixed = replace(falling, metrics={"market_state": "CNL-WPR-L:discount_mixed"})

    assert block is not None
    assert block["source"] == "db"
    assert block["market_state"] == "CNL-WPR-L:falling_discount_trap"
    assert block["required_loss_count"] == 2
    assert block["remaining_ms"] == pytest.approx(3_300_000)
    assert await manager._codex_state_throttle_block(mixed, now_ms) is None


@pytest.mark.asyncio
async def test_v1413_codex_gate_state_throttle_skips_without_accept_event(monkeypatch):
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision as StrategyDecision
    from src.gridbot.strategy.long_pullback import SignalPlan
    from src.gridbot.strategy.wildcat_live import WildcatLiveDecision
    import src.gridbot.mainnet.one_run as or_mod

    now_ms = 1_782_700_000_000

    def _completed_loss(run_id: str, age_s: int) -> dict:
        completed_at = now_ms - age_s * 1000
        return {
            "run_id": run_id,
            "status": "COMPLETED",
            "armed_at_ms": completed_at,
            "updated_at_ms": completed_at,
            "completed_at_ms": completed_at,
            "realized_pnl_usdc": -0.08,
            "commission_usdc": 0.02,
            "signal_json": json.dumps({
                "codex_v1": {
                    "enabled": True,
                    "lane_code": "CNL-WPR-L",
                    "metrics": {"market_state": "CNL-WPR-L:falling_discount_trap"},
                }
            }),
        }

    repo = FakeRepo()
    repo.recent_runs = [_completed_loss("loss_a", 1500), _completed_loss("loss_b", 300)]
    telegram = FakeTelegramApp()
    manager = MainnetOneRunManager(
        _settings(mainnet_one_run_enabled=True, mainnet_codex_v1_enabled=True),
        FakeClient(),
        repo,
        telegram,
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
        planned_notional_usdc=50.0,
        planned_margin_usdc=0.6667,
        planned_qty=0.0167,
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
        version="_codex_v1.4.13",
        baseline="test",
        lane="v139_canary_watch_pre_reprice_long_s1",
        lane_code="CNL-WPR-L",
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v145_wpr_profit_lock_exec",
        metrics={
            "market_state": "CNL-WPR-L:falling_discount_trap",
            "policy_tag": "v145_wpr_profit_lock_exec",
            "tp1_bp": 8.0,
            "sl_bp": 15.0,
            "partial_exit_pct": 0.40,
            "ttl_s": 90,
        },
        policy_tag="v145_wpr_profit_lock_exec",
    )
    monkeypatch.setattr(or_mod.time, "time", lambda: now_ms / 1000.0)
    monkeypatch.setattr(or_mod, "select_codex_v1_lane", lambda _feat: codex)

    async def _mock_feat(*args, **kwargs):
        return {
            "symbol": "ETHUSDC",
            "strategy": "S1_BB_RSI",
            "side": "LONG",
            "score": 82.0,
            "rng15": 72.0,
            "d30": -65.0,
            "adv3": 2.0,
            "range_bp": 3.0,
            "bb_lower_dist_bp": 12.0,
            "vwap_dist_bp": -49.0,
            "rsi": 34.0,
            "reprice_wait_elapsed_seconds": 30.0,
            "maker_fee_bp": 0.0,
            "kill_switch": "off",
            "open_position": "false",
            "open_entry_order": "0",
            "open_reduce_order": "",
        }

    monkeypatch.setattr(manager, "_build_codex_v1_live_features_for_decision", _mock_feat)
    run = {"run_id": "test_v1413_state_throttle", "symbol": "ETHUSDC", "status": "ARMED", "strategy_label": "codex_v1"}

    adjusted, _raw_dec, codex_dec, _final_feat = await manager._apply_codex_v1_gate(
        run, decision, [], rng15=72.0, drift_bp=-65.0
    )

    assert adjusted is None
    assert codex_dec.accepted
    assert not any(event_type == "entry_codex_v1_accepted" for _, event_type, _ in repo.events)
    event = next(details for _, event_type, details in repo.events if event_type == "entry_codex_state_throttled")
    assert event["market_state"] == "CNL-WPR-L:falling_discount_trap"
    assert event["source"] == "db"
    assert event["effective_execution"]["status"] == "state_throttled"
    assert telegram.bot.messages


@pytest.mark.asyncio
async def test_v139c_wpr_down_tape_blocks_live_promotion():
    from types import SimpleNamespace
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision

    manager = MainnetOneRunManager(
        _settings(mainnet_one_run_enabled=True, mainnet_codex_v1_enabled=True, mainnet_codex_v143_adaptive_exec_enabled=False),
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
async def test_v142_nl_near_w1d_stays_ambiguous_shadow_only(monkeypatch):
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
    assert adjusted is None
    assert not codex_dec.accepted
    assert codex_dec.lane_code == "SH_NL_NEAR_W1D_LONG_LIVE200"
    assert codex_dec.reason == "v142_ambiguous_shadow_only"
    assert codex_dec.requested_notional_usdc == pytest.approx(0.0)
    assert codex_dec.metrics["fixed_notional_usdc"] == pytest.approx(50.0)
    assert codex_dec.metrics["applied_notional_cap_usdc"] == pytest.approx(0.0)
    assert "no_live_promotion" in codex_dec.risk_tags
    assert "fixed_50_usdc" in codex_dec.risk_tags
    skipped_event = next(details for _, event_type, details in repo.events if event_type == "entry_codex_v1_skipped")
    assert skipped_event["raw_classifier"]["reason"] == "no_codex_v1_lane_match"
    assert skipped_event["effective_execution"]["lane_code"] == "SH_NL_NEAR_W1D_LONG_LIVE200"


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
    assert codex_dec.reason == "v142_ambiguous_shadow_only"
    assert codex_dec.policy_tag == "v142_ambiguous_shadow_only"
    assert codex_dec.requested_notional_usdc == pytest.approx(0.0)
    assert codex_dec.metrics["fixed_notional_usdc"] == pytest.approx(50.0)
    assert codex_dec.metrics["applied_notional_cap_usdc"] == pytest.approx(0.0)
    assert "no_live_promotion" in codex_dec.risk_tags


@pytest.mark.asyncio
async def test_v142_nl_near_w1d_reclaim_still_stays_shadow_only():
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

    assert not codex_dec.accepted
    assert codex_dec.lane_code == "SH_NL_NEAR_W1D_LONG_LIVE200"
    assert codex_dec.reason == "v142_ambiguous_shadow_only"
    assert codex_dec.requested_notional_usdc == pytest.approx(0.0)
    assert codex_dec.metrics["policy_tag"] == "v142_ambiguous_shadow_only"
    assert codex_dec.metrics["fixed_notional_usdc"] == pytest.approx(50.0)
    assert codex_dec.metrics["applied_notional_cap_usdc"] == pytest.approx(0.0)
    assert "fixed_50_usdc" in codex_dec.risk_tags
    assert "no_live_promotion" in codex_dec.risk_tags


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
    assert codex_dec.reason == "v142_ambiguous_shadow_only"
    assert codex_dec.policy_tag == "v142_ambiguous_shadow_only"
    assert codex_dec.requested_notional_usdc == pytest.approx(0.0)
    assert codex_dec.metrics["fixed_notional_usdc"] == pytest.approx(50.0)
    assert codex_dec.metrics["applied_notional_cap_usdc"] == pytest.approx(0.0)
    assert "no_live_promotion" in codex_dec.risk_tags


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


def test_v143_profile_ttl_overrides_lane_ttl():
    manager = MainnetOneRunManager(
        _settings(mainnet_codex_v135_entry_ttl_seconds_by_lane="STUP-S:180"),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    policy = manager._codex_v1_live_entry_ttl_policy(
        {"signal_json": {"codex_v1": {"enabled": True, "lane_code": "STUP-S", "metrics": {"ttl_s": 60}}}}
    )

    assert policy["ttl_seconds"] == 60
    assert policy["ttl_source"] == "codex_v143_profile"
    assert policy["lane_code"] == "STUP-S"

    disabled_manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_v143_adaptive_exec_enabled=False,
            mainnet_codex_v135_entry_ttl_seconds_by_lane="STUP-S:180",
        ),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    disabled_policy = disabled_manager._codex_v1_live_entry_ttl_policy(
        {"signal_json": {"codex_v1": {"enabled": True, "lane_code": "STUP-S", "metrics": {"ttl_s": 60}}}}
    )

    assert disabled_policy["ttl_seconds"] == 180
    assert disabled_policy["ttl_source"] == "codex_v135_lane_override"


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

    assert ("ETHUSDC", 321) in client.cancelled
    assert repo.completed[-1][0:3] == ("cry3mn_test", "ENTRY_EXPIRED", "entry_ttl_expired")
    ttl_event = next(details for _, event_type, details in repo.events if event_type == "entry_ttl_expired")
    assert ttl_event["entry_ttl_s"] == 180
    assert ttl_event["entry_ttl_source"] == "codex_v135_lane_override"
    assert ttl_event["lane_code"] == "S1P-L"
    assert ttl_event["entry_age_s"] >= 180


@pytest.mark.asyncio
async def test_v1438_entry_late_fill_after_ttl_closes_without_entry_setup(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    hold_start_ms = 1_700_000_000_000
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
    client.open_orders = [
        {"orderId": 321, "symbol": "ETHUSDC", "clientOrderId": "cry3mn_test_entry", "price": "100.0"}
    ]
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_entry_order_ttl_seconds=180,
            mainnet_codex_v135_entry_ttl_by_lane_enabled=True,
            mainnet_codex_v135_entry_ttl_seconds_by_lane="STUP-S:60",
            mainnet_codex_v1438_strict_entry_ttl_enabled=True,
            mainnet_codex_v1443_entry_late_fill_maker_ttl_seconds=0,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _run(
        status="ENTRY_PENDING",
        side="LONG",
        entry_order_id=321,
        updated_at_ms=hold_start_ms,
        armed_at_ms=hold_start_ms,
        signal_json=json.dumps({
            "side": "LONG",
            "take_profit": 101.0,
            "stop_loss": 99.0,
            "codex_v1": {"enabled": True, "lane_code": "STUP-S"},
        }),
    )
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 61_000) / 1000.0)

    await manager._run_entry_pending(run)

    event = next(details for _, event_type, details in repo.events if event_type == "entry_late_fill_after_ttl")
    assert event["entry_ttl_s"] == 60
    assert event["entry_ttl_source"] == "codex_v135_lane_override"
    assert event["lane_code"] == "STUP-S"
    assert event["actual_entry_fill_age_s"] is None
    assert any(fields.get("exit_reason") == "ENTRY_LATE_FILL_TTL" for _, fields in repo.updated)
    assert any(fields.get("status") == "CLOSING" for _, fields in repo.updated)
    assert ("ETHUSDC", 321) in client.cancelled
    assert len(client.market_orders) == 1
    assert client.market_orders[0]["side"] == "SELL"
    assert not client.stop_market_sl_orders
    assert any(o.get("timeInForce") == "GTX" and o.get("reduceOnly") is True for o in client.all_orders)
    assert not any(event_type == "entry_filled" for _, event_type, _ in repo.events)
    assert any(
        event_type == "close_submitted" and details.get("reason") == "ENTRY_LATE_FILL_TTL"
        for _, event_type, details in repo.events
    )
    assert any(
        event_type == "codex_v1443_near_flat_scratch_triggered"
        and details.get("original_reason") == "ENTRY_LATE_FILL_TTL"
        for _, event_type, details in repo.events
    )
    assert any(
        event_type == "survival_maker_attempt"
        and details.get("reason") == "CODEX_V1443_ENTRY_LATE_FILL_SCRATCH"
        for _, event_type, details in repo.events
    )



@pytest.mark.asyncio
async def test_v1449_cnl_wpr_late_fill_uses_wider_maker_first_exit(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    hold_start_ms = 1_700_000_000_000
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
    client.open_orders = [
        {"orderId": 654, "symbol": "ETHUSDC", "clientOrderId": "cry3mn_test_entry", "price": "100.0"}
    ]
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_entry_order_ttl_seconds=180,
            mainnet_codex_v135_entry_ttl_by_lane_enabled=True,
            mainnet_codex_v135_entry_ttl_seconds_by_lane="CNL-WPR-L:20",
            mainnet_codex_v1438_strict_entry_ttl_enabled=True,
            mainnet_codex_v1449_cnl_wpr_late_fill_maker_ttl_seconds=0,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _run(
        status="ENTRY_PENDING",
        side="LONG",
        entry_order_id=654,
        updated_at_ms=hold_start_ms,
        armed_at_ms=hold_start_ms,
        signal_json=json.dumps({
            "side": "LONG",
            "take_profit": 101.0,
            "stop_loss": 99.0,
            "codex_v1": {
                "enabled": True,
                "lane_code": "CNL-WPR-L",
                "metrics": {"market_state": "CNL-WPR-L:deep_discount_stable"},
            },
        }),
    )
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 21_000) / 1000.0)

    await manager._run_entry_pending(run)

    assert any(fields.get("exit_reason") == "ENTRY_LATE_FILL_TTL" for _, fields in repo.updated)
    assert any(
        event_type == "codex_v1449_cnl_wpr_late_fill_maker_exit_triggered"
        and details.get("scratch_reason") == "CODEX_V1449_CNL_WPR_LATE_FILL_MAKER_EXIT"
        and details.get("current_bp") == pytest.approx(-5.0)
        and details.get("lane_code") == "CNL-WPR-L"
        for _, event_type, details in repo.events
    )
    assert any(
        event_type == "survival_maker_attempt"
        and details.get("reason") == "CODEX_V1449_CNL_WPR_LATE_FILL_MAKER_EXIT"
        for _, event_type, details in repo.events
    )
    assert any(
        event_type == "codex_v1449_late_fill_maker_exit_fallback_market"
        and details.get("scratch_reason") == "CODEX_V1449_CNL_WPR_LATE_FILL_MAKER_EXIT"
        for _, event_type, details in repo.events
    )
    assert len(client.market_orders) == 1
    assert client.market_orders[0]["side"] == "SELL"

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

    v143_manager = MainnetOneRunManager(
        _settings(mainnet_codex_v137_w6a_risk_shadow_enabled=True),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    assert v143_manager._codex_v1_live_research_block_reason(decision, weak_features) == "v143_w6a_shadow_only"

    v137_manager = MainnetOneRunManager(
        _settings(mainnet_codex_v137_w6a_risk_shadow_enabled=True, mainnet_codex_v143_w6a_shadow_only_enabled=False),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    assert v137_manager._codex_v1_live_research_block_reason(decision, weak_features) is None

    legacy_manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_v137_w6a_risk_shadow_enabled=False,
            mainnet_codex_v134_w6a_weak_drift_50_canary_enabled=False,
            mainnet_codex_v143_w6a_shadow_only_enabled=False,
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


def test_v142_w6a_apply_codex_decision_uses_tp8_full_exit():
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision
    from src.gridbot.strategy.long_pullback import SignalPlan
    from src.gridbot.strategy.wildcat_live import WildcatLiveDecision

    manager = MainnetOneRunManager(
        _settings(mainnet_codex_v138_w6a_partial_tp_pct=0.0008, mainnet_codex_w6a_partial_exit_pct=1.0, mainnet_codex_w6a_entry_offset_bp=0.0),
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
        take_profits=[100.04428],
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
        tp_pct=0.0004428,
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

    assert adjusted.partial_tp_pct == pytest.approx(0.0008)
    assert adjusted.partial_exit_pct == pytest.approx(1.0)
    assert adjusted.tp_pct == pytest.approx(0.0008)
    assert adjusted.signal.take_profits[0] == pytest.approx(100.08)
    assert "codex_v138_w6a_partial_tp_pct:0.0008" in adjusted.signal.reasons
    assert "codex_v142_w6a_partial_exit_pct:1" in adjusted.signal.reasons


def test_v143_stups_apply_codex_decision_uses_state_profile_metrics():
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision
    from src.gridbot.strategy.long_pullback import SignalPlan
    from src.gridbot.strategy.wildcat_live import WildcatLiveDecision

    manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_stups_entry_offset_bp=3.0,
            mainnet_codex_stups_partial_tp_pct=0.0011,
            mainnet_codex_stups_partial_exit_pct=0.70,
            mainnet_codex_stups_max_sl_bp=25.0,
        ),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    signal = SignalPlan(
        action="SELL",
        confidence=80,
        score=80,
        symbol="ETHUSDC",
        price=100.0,
        rsi=55.0,
        atr=1.0,
        support=99.0,
        vwap=100.0,
        entries=[100.0],
        entry_weights=[1.0],
        stop_loss=101.0,
        take_profits=[99.95],
        planned_notional_usdc=50.0,
        planned_margin_usdc=0.6667,
        planned_qty=0.5,
        reasons=["wildcat:S1_BB_RSI"],
        risk_notes=[],
    )
    decision = WildcatLiveDecision(
        signal=signal,
        strategy="S1_BB_RSI",
        side="SHORT",
        tp_pct=0.00054,
        sl_pct=0.0025,
        partial_exit_pct=0.4,
        partial_tp_pct=0.0004,
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
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=2.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1419_stups_runner_exec",
        metrics={
            "policy_note": "v1419_stups_runner_exec",
            "market_state": "STUP-S:clean_extension",
            "entry_bp": 2.0,
            "tp1_bp": 6.0,
            "full_tp_bp": 80.0,
            "partial_exit_pct": 0.7,
            "sl_bp": 8.0,
            "be_bp": 2.0,
            "ttl_s": 60,
        },
    )

    adjusted = manager._apply_codex_v1_decision(decision, codex_decision)

    entry_ref = 100.0 * (1 + 2.0 / 10_000.0)
    assert adjusted.signal.entries[0] == pytest.approx(entry_ref)
    assert adjusted.partial_tp_pct == pytest.approx(0.0006)
    assert adjusted.partial_exit_pct == pytest.approx(0.7)
    assert adjusted.tp_pct == pytest.approx(0.008)
    assert adjusted.signal.take_profits[0] == pytest.approx(entry_ref * (1 - 0.008))
    assert adjusted.signal.stop_loss == pytest.approx(entry_ref * (1 + 8.0 / 10_000.0))
    assert "codex_v1_entry_bp:2" in adjusted.signal.reasons
    assert "codex_v143_profile_state:STUP-S:clean_extension" in adjusted.signal.reasons
    assert "codex_v143_partial_tp_pct:0.0006" in adjusted.signal.reasons
    assert "codex_v143_partial_exit_pct:0.7" in adjusted.signal.reasons
    assert "codex_v143_max_sl_bp:8" in adjusted.signal.reasons
    assert "codex_v143_be_bp:2" in adjusted.signal.reasons
    assert "codex_v145_full_tp_bp:80" in adjusted.signal.reasons
    assert "codex_v143_ttl_s:60" in adjusted.signal.reasons




def test_v1421_apply_codex_decision_honors_side_override_profile():
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision
    from src.gridbot.strategy.long_pullback import SignalPlan
    from src.gridbot.strategy.wildcat_live import WildcatLiveDecision

    manager = MainnetOneRunManager(
        _settings(mainnet_initial_notional_usdc=50.0, mainnet_max_cumulative_notional_usdc=50.0),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    signal = SignalPlan(
        action="BUY",
        confidence=72,
        score=68,
        symbol="ETHUSDC",
        price=100.0,
        rsi=46.0,
        atr=1.0,
        support=99.0,
        vwap=100.0,
        entries=[100.0],
        entry_weights=[1.0],
        stop_loss=99.0,
        take_profits=[100.05],
        planned_notional_usdc=50.0,
        planned_margin_usdc=50.0 / 75.0,
        planned_qty=0.5,
        reasons=["wildcat:S1_BB_RSI"],
        risk_notes=[],
    )
    decision = WildcatLiveDecision(
        signal=signal,
        strategy="S1_BB_RSI",
        side="LONG",
        tp_pct=0.0005,
        sl_pct=0.0010,
        partial_exit_pct=0.4,
        partial_tp_pct=0.0004,
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
        version="_codex_v1.4.21",
        baseline="test",
        lane="v139_canary_watch_pre_reprice_long_s1",
        lane_code="CNL-WPR-L",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=8.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1421_decision_tree_adaptive_exec",
        metrics={
            "policy_note": "v1421_decision_tree_adaptive_exec",
            "market_state": "CNL-WPR-L:discount_mixed",
            "entry_bp": 8.0,
            "tp1_bp": 60.0,
            "full_tp_bp": 120.0,
            "partial_exit_pct": 0.2,
            "sl_bp": 15.0,
            "be_bp": 2.0,
            "ttl_s": 60,
            "v1421_action": "SHORT_WIDE_E8",
        },
        policy_tag="v1421_decision_tree_adaptive_exec",
    )

    adjusted = manager._apply_codex_v1_decision(decision, codex_decision)

    entry_ref = 100.0 * (1 + 8.0 / 10_000.0)
    assert adjusted.side == "SHORT"
    assert adjusted.signal.action == "SELL"
    assert adjusted.signal.entries[0] == pytest.approx(entry_ref)
    assert adjusted.signal.stop_loss == pytest.approx(entry_ref * (1 + 15.0 / 10_000.0))
    assert adjusted.signal.take_profits[0] == pytest.approx(entry_ref * (1 - 120.0 / 10_000.0))
    assert adjusted.partial_tp_pct == pytest.approx(0.006)
    assert adjusted.partial_exit_pct == pytest.approx(0.2)
    assert adjusted.tp_pct == pytest.approx(0.012)
    assert "codex_v1421_side_override:LONG->SHORT" in adjusted.signal.reasons
    assert "codex_v1_side_overridden" in adjusted.signal.risk_notes


def test_v1433_stups_clean_high_side_override_guard_keeps_raw_short():
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision
    from src.gridbot.strategy.long_pullback import SignalPlan
    from src.gridbot.strategy.wildcat_live import WildcatLiveDecision

    manager = MainnetOneRunManager(
        _settings(mainnet_initial_notional_usdc=50.0, mainnet_max_cumulative_notional_usdc=50.0),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    signal = SignalPlan(
        action="SELL",
        confidence=72,
        score=68,
        symbol="ETHUSDC",
        price=100.0,
        rsi=61.0,
        atr=1.0,
        support=99.0,
        vwap=100.0,
        entries=[100.0],
        entry_weights=[1.0],
        stop_loss=101.0,
        take_profits=[99.95],
        planned_notional_usdc=50.0,
        planned_margin_usdc=50.0 / 75.0,
        planned_qty=0.5,
        reasons=["wildcat:S1_BB_RSI"],
        risk_notes=[],
    )
    decision = WildcatLiveDecision(
        signal=signal,
        strategy="S1_BB_RSI",
        side="SHORT",
        tp_pct=0.0005,
        sl_pct=0.0010,
        partial_exit_pct=0.4,
        partial_tp_pct=0.0004,
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
        version="_codex_v1.4.33",
        baseline="test",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1427_five_window_tp14_adaptive_exec",
        risk_tags=("v1427_side_override_long",),
        metrics={
            "market_state": "STUP-S:clean_extension",
            "entry_bp": 0.0,
            "tp1_bp": 14.0,
            "full_tp_bp": 14.0,
            "partial_exit_pct": 1.0,
            "sl_bp": 8.0,
            "be_bp": 0.0,
            "ttl_s": 90,
            "v1427_features": {"range_pos_15": 0.9807, "vwap_dist_bp": 74.596},
        },
        policy_tag="v1427_five_window_tp14_adaptive_exec",
    )

    guarded = manager._codex_v1433_guard_clean_high_side_override(
        {"side": "SHORT", "range_pos_15": 0.9807, "vwap_dist_bp": 74.596},
        codex_decision,
        decision.side,
    )
    adjusted = manager._apply_codex_v1_decision(decision, guarded)

    assert guarded.side == "SHORT"
    assert guarded.metrics["v1433_side_override_guarded"] is True
    assert guarded.metrics["target_side"] == "SHORT"
    assert guarded.metrics["v1433_original_target_side"] == "LONG"
    assert guarded.metrics["v1433_target_side"] == "SHORT"
    assert guarded.metrics["v1427_side_override_suppressed"] is True
    assert "v1427_side_override_long" not in guarded.risk_tags
    assert adjusted.side == "SHORT"
    assert adjusted.signal.action == "SELL"
    assert "codex_v1433_clean_high_side_override_guard:SHORT_kept" in adjusted.signal.reasons
    assert not any(reason.startswith("codex_v1421_side_override") for reason in adjusted.signal.reasons)
    assert "codex_v1433_side_override_guarded" in adjusted.signal.risk_notes


def test_v1433_stups_clean_high_side_override_ignores_raw_vwap_price_without_distance():
    manager = MainnetOneRunManager(
        _settings(mainnet_initial_notional_usdc=50.0, mainnet_max_cumulative_notional_usdc=50.0),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    codex_decision = CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.40",
        baseline="test",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1427_five_window_tp14_adaptive_exec",
        regime="STUP-S:clean_extension",
        risk_tags=("v1427_side_override_long",),
        metrics={"market_state": "STUP-S:clean_extension"},
        policy_tag="v1427_five_window_tp14_adaptive_exec",
    )

    guarded = manager._codex_v1433_guard_clean_high_side_override(
        {"side": "SHORT", "range_pos_15": 0.9807, "vwap": 1698.0},
        codex_decision,
        "SHORT",
    )

    assert guarded.side == "LONG"
    assert guarded.metrics == {"market_state": "STUP-S:clean_extension"}
    assert "v1433_side_override_guarded" not in guarded.risk_tags


def test_v1411_stups_mixed_uses_tp5_partial_and_runner8_profile():
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision
    from src.gridbot.strategy.long_pullback import SignalPlan
    from src.gridbot.strategy.wildcat_live import WildcatLiveDecision

    manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_stups_entry_offset_bp=3.0,
            mainnet_codex_stups_partial_tp_pct=0.0011,
            mainnet_codex_stups_partial_exit_pct=0.70,
            mainnet_codex_stups_max_sl_bp=25.0,
        ),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    signal = SignalPlan(
        action="SELL",
        confidence=80,
        score=80,
        symbol="ETHUSDC",
        price=100.0,
        rsi=55.0,
        atr=1.0,
        support=99.0,
        vwap=100.0,
        entries=[100.0],
        entry_weights=[1.0],
        stop_loss=101.0,
        take_profits=[99.95],
        planned_notional_usdc=50.0,
        planned_margin_usdc=0.6667,
        planned_qty=0.5,
        reasons=["wildcat:S1_BB_RSI"],
        risk_notes=[],
    )
    decision = WildcatLiveDecision(
        signal=signal,
        strategy="S1_BB_RSI",
        side="SHORT",
        tp_pct=0.00054,
        sl_pct=0.0025,
        partial_exit_pct=0.4,
        partial_tp_pct=0.0004,
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
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1419_stups_runner_exec",
        metrics={
            "policy_note": "v1419_stups_runner_exec",
            "market_state": "STUP-S:mixed",
            "entry_bp": 2.0,
            "tp1_bp": 6.0,
            "full_tp_bp": 80.0,
            "partial_exit_pct": 0.7,
            "sl_bp": 8.0,
            "be_bp": 2.0,
            "ttl_s": 60,
        },
    )

    adjusted = manager._apply_codex_v1_decision(decision, codex_decision)

    entry_ref = 100.0 * (1 + 2.0 / 10_000.0)
    assert adjusted.signal.entries[0] == pytest.approx(entry_ref)
    assert adjusted.partial_tp_pct == pytest.approx(0.0006)
    assert adjusted.partial_exit_pct == pytest.approx(0.7)
    assert adjusted.tp_pct == pytest.approx(0.008)
    assert adjusted.signal.take_profits[0] == pytest.approx(entry_ref * (1 - 0.008))
    assert adjusted.signal.stop_loss == pytest.approx(entry_ref * (1 + 8.0 / 10_000.0))
    assert "codex_v143_profile_state:STUP-S:mixed" in adjusted.signal.reasons
    assert "codex_v143_partial_tp_pct:0.0006" in adjusted.signal.reasons
    assert "codex_v145_full_tp_bp:80" in adjusted.signal.reasons
def test_v143_adaptive_disabled_stups_uses_lane_level_profile():
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision
    from src.gridbot.strategy.long_pullback import SignalPlan
    from src.gridbot.strategy.wildcat_live import WildcatLiveDecision

    manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_v143_adaptive_exec_enabled=False,
            mainnet_codex_stups_entry_offset_bp=3.0,
            mainnet_codex_stups_partial_tp_pct=0.0011,
            mainnet_codex_stups_partial_exit_pct=0.70,
            mainnet_codex_stups_max_sl_bp=25.0,
        ),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    signal = SignalPlan(
        action="SELL",
        confidence=80,
        score=80,
        symbol="ETHUSDC",
        price=100.0,
        rsi=55.0,
        atr=1.0,
        support=99.0,
        vwap=100.0,
        entries=[100.0],
        entry_weights=[1.0],
        stop_loss=101.0,
        take_profits=[99.95],
        planned_notional_usdc=50.0,
        planned_margin_usdc=0.6667,
        planned_qty=0.5,
        reasons=["wildcat:S1_BB_RSI"],
        risk_notes=[],
    )
    decision = WildcatLiveDecision(
        signal=signal,
        strategy="S1_BB_RSI",
        side="SHORT",
        tp_pct=0.00054,
        sl_pct=0.0025,
        partial_exit_pct=0.4,
        partial_tp_pct=0.0004,
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
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1419_stups_runner_exec",
        metrics={
            "policy_note": "v1419_stups_runner_exec",
            "market_state": "STUP-S:clean_extension",
            "entry_bp": 0.0,
            "tp1_bp": 4.0,
            "partial_exit_pct": 0.4,
            "sl_bp": 8.0,
            "be_bp": 2.0,
            "ttl_s": 120,
        },
    )

    adjusted = manager._apply_codex_v1_decision(decision, codex_decision)

    assert adjusted.signal.entries[0] == pytest.approx(100.03)
    assert adjusted.partial_tp_pct == pytest.approx(0.0011)
    assert adjusted.partial_exit_pct == pytest.approx(0.70)
    assert adjusted.tp_pct == pytest.approx(0.0011)
    assert adjusted.signal.stop_loss == pytest.approx(100.03 * (1 + 25.0 / 10_000.0))
    assert "codex_v1_entry_bp:3" in adjusted.signal.reasons
    assert "codex_v143_profile_state:STUP-S:clean_extension" not in adjusted.signal.reasons
    assert "codex_v142_stups_partial_tp_pct:0.0011" in adjusted.signal.reasons


def test_v149_s1pl_profile_caps_notional_and_uses_full_exit_profile():
    from dataclasses import replace
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision
    from src.gridbot.strategy.long_pullback import SignalPlan
    from src.gridbot.strategy.wildcat_live import WildcatLiveDecision

    manager = MainnetOneRunManager(
        _settings(
            mainnet_initial_notional_usdc=200.0,
            mainnet_max_cumulative_notional_usdc=200.0,
            mainnet_codex_v1_max_notional_usdc=50.0,
            mainnet_partial_tp_pct=0.0005,
            mainnet_partial_exit_pct=0.40,
        ),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    signal = SignalPlan(
        action="BUY",
        confidence=72,
        score=68,
        symbol="ETHUSDC",
        price=100.0,
        rsi=46.0,
        atr=1.0,
        support=99.0,
        vwap=100.0,
        entries=[100.0],
        entry_weights=[1.0],
        stop_loss=99.0,
        take_profits=[100.05],
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
        tp_pct=0.00054,
        sl_pct=0.0025,
        partial_exit_pct=0.4,
        partial_tp_pct=0.0004,
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
        version="_codex_v1.4.13",
        baseline="test",
        lane="codex_v1_s1_bbrsi_ordinary_pullback_long_pre_vwap",
        lane_code="S1P-L",
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=0.0,
        size_mult=0.2,
        notional_mult=0.2,
        requested_notional_usdc=25.0,
        reason="s1p_l_match",
        metrics={
            "policy_note": "v149_s1pl_tiny_profile_fix",
            "market_state": "S1P-L:ordinary_pullback_pre_vwap",
            "applied_notional_cap_usdc": 25.0,
            "entry_bp": 0.0,
            "tp1_bp": 6.0,
            "partial_exit_pct": 1.0,
            "sl_bp": 15.0,
            "be_bp": 0.0,
            "ttl_s": 180,
        },
    )

    adjusted = manager._apply_codex_v1_decision(decision, codex_decision)
    metrics_without_cap = dict(codex_decision.metrics)
    metrics_without_cap.pop("applied_notional_cap_usdc")
    adjusted_without_metric_cap = manager._apply_codex_v1_decision(
        decision,
        replace(codex_decision, metrics=metrics_without_cap),
    )

    legacy_low_notional = manager._apply_codex_v1_decision(
        decision,
        replace(
            codex_decision,
            requested_notional_usdc=10.0,
            metrics={**metrics_without_cap, "applied_notional_cap_usdc": 10.0},
        ),
    )

    assert adjusted.signal.planned_notional_usdc == pytest.approx(25.0)
    assert adjusted_without_metric_cap.signal.planned_notional_usdc == pytest.approx(25.0)
    assert legacy_low_notional.signal.planned_notional_usdc == pytest.approx(25.0)
    assert "codex_v1_min_entry_notional_floor" in legacy_low_notional.signal.risk_notes
    assert "codex_v1_min_entry_notional_usdc:25" in legacy_low_notional.signal.reasons
    assert adjusted.signal.planned_margin_usdc == pytest.approx(25.0 / 75.0)
    assert adjusted.signal.planned_qty == pytest.approx(0.25)
    assert adjusted.partial_tp_pct == pytest.approx(0.0006)
    assert adjusted.partial_exit_pct == pytest.approx(1.0)
    assert adjusted.tp_pct == pytest.approx(0.0006)
    assert adjusted.signal.take_profits[0] == pytest.approx(100.06)
    assert adjusted.signal.stop_loss == pytest.approx(99.85)
    assert "codex_v1_notional_capped" in adjusted.signal.risk_notes
    assert "codex_v1_policy_note:v149_s1pl_tiny_profile_fix" in adjusted.signal.reasons
    assert "codex_v143_profile_state:S1P-L:ordinary_pullback_pre_vwap" in adjusted.signal.reasons
    assert "codex_v143_partial_tp_pct:0.0006" in adjusted.signal.reasons
    assert "codex_v143_partial_exit_pct:1" in adjusted.signal.reasons
    assert "codex_v143_max_sl_bp:15" in adjusted.signal.reasons
    assert "codex_v143_ttl_s:180" in adjusted.signal.reasons

def test_v147_codex_entry_sl_pct_prefers_profile_over_hard_override():
    from types import SimpleNamespace
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision

    manager = MainnetOneRunManager(
        _settings(mainnet_hard_sl_pct_override=0.0025),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )

    assert manager._effective_sl_pct({"wildcat": {"sl_pct": 0.0015}}) == pytest.approx(0.0025)

    decision = SimpleNamespace(sl_pct=0.0015)
    codex_decision = CodexV1Decision(
        accepted=True,
        version="test",
        baseline="test",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=1.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1419_stups_runner_exec",
        metrics={"market_state": "STUP-S:mixed", "sl_bp": 15.0},
    )

    sl_pct = manager._entry_sl_pct_for_decision(decision, codex_decision)

    assert sl_pct == pytest.approx(0.0015)
    assert manager._sl_price_from_pct(1578.0, "SHORT", sl_pct) == pytest.approx(1580.367)

@pytest.mark.asyncio
async def test_v142_w6a_take_profit_orders_full_exit_at_tp1():
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
        _settings(mainnet_partial_tp_pct=0.0005, mainnet_partial_exit_pct=0.40, mainnet_trail_disable_final_tp=True),
        client,
        FakeRepo(),
        FakeTelegramApp(),
    )
    run = _run(
        run_id="cry3mn_v142_tp8_full",
        signal_json=json.dumps({
            "side": "LONG",
            "take_profit": 100.08,
            "stop_loss": 99.0,
            "wildcat": {"tp_pct": 0.0008, "partial_tp_pct": 0.0008, "partial_exit_pct": 1.0},
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

    assert order_dict == {"cry3mn_v142_tp8_full_tp1": ("0.12", pytest.approx(100.08))}


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
        _settings(mainnet_partial_tp_pct=0.0005, mainnet_partial_exit_pct=0.40, mainnet_trail_disable_final_tp=False),
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
            "wildcat": {"tp_pct": 0.001, "partial_tp_pct": 0.0002, "partial_exit_pct": 0.40},
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

    assert order_dict["cry3mn_v139b_wpr_tp60_tp1"][0] == "0.048"
    assert order_dict["cry3mn_v139b_wpr_tp60_tp1"][1] == pytest.approx(100.02)
    assert order_dict["cry3mn_v139b_wpr_tp60_tp3"][1] == pytest.approx(100.1)


def test_v143_profile_shadow_marks_adaptive_states_and_w6a_shadow_only():
    manager = MainnetOneRunManager(
        _settings(mainnet_codex_v142_no_fill_watch_hours_tpe="05,08,10,13,14,15"),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )

    cnl = manager._codex_v142_profile_shadow("CNL-WPR-L")
    stups = manager._codex_v142_profile_shadow("STUP-S")
    s1p = manager._codex_v142_profile_shadow("S1P-L")
    w6a = manager._codex_v142_profile_shadow("W6A")

    assert cnl is not None
    assert cnl["profile"]["status"] == "v1420_fixed_bucket_candidate_live"
    assert cnl["profile"]["policy_tag"] == "v1420_current_market_fixed_buckets_candidate"
    assert cnl["profile"]["live_blocks"]["discount_mixed_bad_slice"] == "v1420_wpr_discount_mixed_bad_block"
    assert cnl["profile"]["live_blocks"]["falling_discount_trap_bad_slice"] == "v1420_wpr_falling_bad_slice_block"
    assert cnl["profile"]["live_blocks"]["falling_continuation_hirange"] == "v1420_wpr_continuation_hirange_block"
    assert cnl["profile"]["live_overrides"]["CNL-WPR-L:deep_discount_stable"]["entry_bp"] == pytest.approx(2.0)
    assert cnl["profile"]["live_overrides"]["CNL-WPR-L:deep_discount_stable"]["tp1_bp"] == pytest.approx(6.0)
    assert cnl["profile"]["live_overrides"]["CNL-WPR-L:deep_discount_stable"]["partial_exit_pct"] == pytest.approx(1.0)
    assert cnl["profile"]["live_overrides"]["CNL-WPR-L:deep_discount_stable"]["staged_entry_reprice_enabled"] is True
    assert cnl["profile"]["live_overrides"]["CNL-WPR-L:falling_discount_trap"]["entry_bp"] == pytest.approx(2.0)
    assert cnl["profile"]["live_overrides"]["CNL-WPR-L:falling_discount_trap"]["full_tp_bp"] == pytest.approx(20.0)
    assert cnl["profile"]["live_overrides"]["CNL-WPR-L:falling_discount_trap"]["partial_exit_pct"] == pytest.approx(0.70)
    assert cnl["profile"]["live_overrides"]["CNL-WPR-L:falling_continuation_probe"]["entry_bp"] == pytest.approx(3.0)
    assert cnl["profile"]["live_overrides"]["CNL-WPR-L:falling_continuation_probe"]["adaptive_tp_engine"] == "v1420_wpr_falling_continuation_probe_filtered"
    assert cnl["no_fill_watch_hours_tpe"] == ["05", "08", "10", "13", "14", "15"]
    assert stups is not None
    assert stups["profile"]["status"] == "v1420_fixed_regime_exec"
    assert stups["profile"]["states"]["clean_extension"]["entry_bp"] == pytest.approx(2.0)
    assert stups["profile"]["states"]["clean_extension"]["partial_exit_pct"] == pytest.approx(0.7)
    assert stups["profile"]["states"]["clean_extension"]["adaptive_tp_engine"] == "v1420_stups_runner_after_clean_gate"
    assert stups["profile"]["states"]["clean_extension"]["full_tp_bp"] == pytest.approx(80.0)
    assert stups["profile"]["states"]["mixed"]["tp1_bp"] == pytest.approx(6.0)
    assert stups["profile"]["states"]["mixed"]["adaptive_tp_engine"] == "v1420_stups_runner_after_bad_weakzone_block"
    assert stups["profile"]["states"]["mixed"]["full_tp_bp"] == pytest.approx(80.0)
    assert stups["profile"]["states"]["mixed"]["sl_bp"] == pytest.approx(8.0)
    assert stups["profile"]["states"]["weak_chop"]["tp1_bp"] == pytest.approx(5.0)
    assert stups["profile"]["states"]["weak_chop"]["full_tp_bp"] == pytest.approx(12.0)
    assert stups["profile"]["states"]["weak_chop"]["partial_exit_pct"] == pytest.approx(0.60)
    assert stups["profile"]["states"]["weak_chop"]["be_bp"] == pytest.approx(4.0)
    assert stups["profile"]["live_adjustments"]["weak_chop_low_rng_weak_adv"]["status"] == "cautious_live_entry"
    assert stups["profile"]["live_adjustments"]["weak_chop_low_rng_weak_adv"]["entry_bp"] == pytest.approx(2.0)
    assert stups["profile"]["live_adjustments"]["weak_chop_low_rng_weak_adv"]["policy_tag"] == "v1417_stups_low_rng_weak_adv_cautious_live"
    assert stups["profile"]["live_adjustments"]["clean_extension_hot_entry_band"]["status"] == "legacy_disabled_by_v1420_runner"
    assert stups["profile"]["live_adjustments"]["clean_extension_hot_entry_band"]["entry_bp"] == pytest.approx(6.0)
    assert stups["profile"]["live_adjustments"]["clean_extension_hot_entry_band"]["ttl_s"] == 75
    assert stups["profile"]["live_adjustments"]["clean_extension_hot_entry_band"]["disabled_when"] == "adaptive_tp_engine in {v1419_stups_runner,v1420_stups_runner_after_clean_gate}"
    assert stups["profile"]["live_blocks"]["mixed_weakzone"] == "v1420_stups_mixed_weakzone_block"
    assert "stale_squeeze_top" in stups["profile"]["shadow_only_states"]
    assert s1p is not None
    assert s1p["profile"]["fixed_notional_usdc"] == pytest.approx(25.0)
    assert s1p["profile"]["policy_tag"] == "v149_s1pl_tiny_profile_fix"
    assert w6a is not None
    assert w6a["profile"]["status"] == "v143_shadow_only"
    assert "high_range" in w6a["profile"]["observe_states"]

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


def test_v1430_trail_metrics_override_global_trail_settings():
    manager = MainnetOneRunManager(
        _settings(
            mainnet_trail_arm_frac=0.7,
            mainnet_trail_profit_floor_bp=1.5,
            mainnet_trail_giveback_frac=0.25,
        ),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    run = _run(
        signal_json=json.dumps({
            "side": "LONG",
            "take_profit": 101.0,
            "stop_loss": 99.0,
            "wildcat": {"tp_pct": 0.0011},
            "codex_v1": {
                "enabled": True,
                "lane_code": "CNL-WPR-L",
                "metrics": {
                    "policy_tag": "v1430_loss_prune_exec",
                    "trail_arm_bp": 11.0,
                    "trail_giveback_bp": 6.0,
                    "trail_floor_bp": 5.0,
                },
            },
        })
    )

    assert manager._trail_arm_mfe(run, 0.0011) == pytest.approx(0.0011)
    assert manager._trail_giveback_bp(run) == pytest.approx(6.0)
    assert manager._trail_profit_floor_bp(run) == pytest.approx(5.0)
    assert manager._survival_maker_profit_floor_bp("TRAIL", run) == pytest.approx(5.0)
    assert manager._trail_stop_from_bp("LONG", 100.0, 12.0, 6.0) == pytest.approx(100.06)
    assert manager._trail_stop_from_bp("SHORT", 100.0, 12.0, 6.0) == pytest.approx(99.94)



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


@pytest.mark.asyncio
async def test_v1436_late_stups_short_blocks_after_veto_edge_spent():
    from dataclasses import replace
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision
    from src.gridbot.strategy.long_pullback import SignalPlan
    from src.gridbot.strategy.wildcat_live import WildcatLiveDecision

    repo = FakeRepo()
    run = _run(run_id="cry3mn_late_stups", side="SHORT")
    await repo.log_event(
        run["run_id"],
        "entry_codex_v1_shadow_sample_started",
        {
            "candidate_lane": "hot_up_extension_short_blocked",
            "mapping_reason": "hot_up_extension_short_blocked",
            "side": "SHORT",
            "entry_price": 1700.0,
        },
    )
    manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_v1436_late_stups_after_veto_enabled=True,
            mainnet_codex_v1436_late_stups_after_veto_edge_spent_bp=10.0,
        ),
        FakeClient(),
        repo,
        FakeTelegramApp(),
    )
    signal = SignalPlan(
        action="SELL",
        confidence=80,
        score=80,
        symbol="ETHUSDC",
        price=1698.2,
        rsi=55.0,
        atr=1.0,
        support=1690.0,
        vwap=1698.0,
        entries=[1696.52],
        entry_weights=[1.0],
        stop_loss=1698.22,
        take_profits=[1695.50],
        planned_notional_usdc=50.0,
        planned_margin_usdc=0.6667,
        planned_qty=0.03,
        reasons=["wildcat:S1_BB_RSI"],
        risk_notes=[],
    )
    live_decision = WildcatLiveDecision(
        signal=signal,
        strategy="S1_BB_RSI",
        side="SHORT",
        tp_pct=0.0006,
        sl_pct=0.001,
        partial_exit_pct=0.7,
        partial_tp_pct=0.0006,
        recovery_steps=0,
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
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1430_loss_prune_exec",
        regime="STUP-S:mixed",
        metrics={"market_state": "STUP-S:mixed", "policy_tag": "v1430_loss_prune_exec"},
        policy_tag="v1430_loss_prune_exec",
    )
    raw_codex = replace(codex, entry_offset_bp=3.0)

    blocked = await manager._codex_v1436_guard_late_stups_after_veto_edge(
        run,
        live_decision,
        raw_codex,
        codex,
        {},
    )

    assert blocked.accepted is False
    assert blocked.reason == "v1436_late_stups_after_veto_edge_block"
    assert blocked.requested_notional_usdc == 0.0
    assert blocked.shadow_lane == "SH_V1436_LATE_STUPS_AFTER_VETO"
    assert blocked.metrics["v1436_proposed_entry"] == pytest.approx(1698.2)
    assert blocked.metrics["v1436_edge_spent_bp"] == pytest.approx(10.59, abs=0.1)
    assert not any(event_type == "entry_codex_v1_skipped" for _, event_type, _ in repo.events)


@pytest.mark.asyncio
async def test_v1437_late_stups_clean_extension_blocks_after_veto_edge_spent():
    from dataclasses import replace
    from src.gridbot.strategy.codex_v1_live import CodexV1Decision
    from src.gridbot.strategy.long_pullback import SignalPlan
    from src.gridbot.strategy.wildcat_live import WildcatLiveDecision

    repo = FakeRepo()
    run = _run(run_id="cry3mn_late_stups_clean", side="SHORT")
    await repo.log_event(
        run["run_id"],
        "entry_codex_v1_shadow_sample_started",
        {
            "candidate_lane": "hot_up_extension_short_blocked",
            "mapping_reason": "hot_up_extension_short_blocked",
            "side": "SHORT",
            "entry_price": 1700.0,
        },
    )
    manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_v1436_late_stups_after_veto_enabled=True,
            mainnet_codex_v1436_late_stups_after_veto_edge_spent_bp=10.0,
        ),
        FakeClient(),
        repo,
        FakeTelegramApp(),
    )
    signal = SignalPlan(
        action="SELL",
        confidence=80,
        score=80,
        symbol="ETHUSDC",
        price=1698.2,
        rsi=55.0,
        atr=1.0,
        support=1690.0,
        vwap=1698.0,
        entries=[1698.2],
        entry_weights=[1.0],
        stop_loss=1699.22,
        take_profits=[1695.50],
        planned_notional_usdc=50.0,
        planned_margin_usdc=0.6667,
        planned_qty=0.03,
        reasons=["wildcat:S1_BB_RSI"],
        risk_notes=[],
    )
    live_decision = WildcatLiveDecision(
        signal=signal,
        strategy="S1_BB_RSI",
        side="SHORT",
        tp_pct=0.0006,
        sl_pct=0.001,
        partial_exit_pct=0.7,
        partial_tp_pct=0.0006,
        recovery_steps=0,
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
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1430_loss_prune_exec",
        regime="STUP-S:clean_extension",
        metrics={"market_state": "STUP-S:clean_extension", "policy_tag": "v1430_loss_prune_exec"},
        policy_tag="v1430_loss_prune_exec",
    )
    raw_codex = replace(codex, entry_offset_bp=3.0)

    blocked = await manager._codex_v1436_guard_late_stups_after_veto_edge(
        run,
        live_decision,
        raw_codex,
        codex,
        {},
    )

    assert blocked.accepted is False
    assert blocked.reason == "v1436_late_stups_after_veto_edge_block"
    assert blocked.requested_notional_usdc == 0.0
    assert blocked.shadow_lane == "SH_V1436_LATE_STUPS_AFTER_VETO"
    assert blocked.metrics["v1437_late_veto_state_expanded"] is True
    assert blocked.metrics["v1436_edge_spent_bp"] == pytest.approx(10.59, abs=0.1)
    assert not any(event_type == "entry_codex_v1_skipped" for _, event_type, _ in repo.events)


@pytest.mark.asyncio
async def test_v1437_stups_clean_extension_thin_lock_uses_maker_only(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=-0.12,
        entry_price=100.0,
        mark_price=99.964,
        unrealized_pnl=0.00432,
        liquidation_price=130.0,
        leverage=75,
        margin_type="cross",
    )
    client.position = position
    client.book = {"bidPrice": "99.950", "askPrice": "99.964"}
    client.open_orders.append({"orderId": 901, "clientOrderId": "cry3mn_test_tp1", "status": "NEW"})
    client.algo_orders.append({"algoId": 902, "clientAlgoId": "x-FAKE902", "reduceOnly": True, "algoStatus": "NEW"})
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_recovery_enabled=False,
            mainnet_codex_survival_enabled=True,
            mainnet_codex_survival_watch_after_seconds=300,
            mainnet_codex_survival_exit_use_maker=True,
            mainnet_codex_v1437_stups_clean_extension_thin_lock_enabled=True,
            mainnet_codex_v1437_stups_clean_extension_thin_lock_maker_ttl_seconds=0,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    signal = {
        "side": "SHORT",
        "codex_v1": {
            "enabled": True,
            "lane_code": "STUP-S",
            "lane": "codex_v1_stale_upmove_short_rng20_canary",
            "metrics": {
                "market_state": "STUP-S:clean_extension",
                "time_profit_lock_enabled": True,
                "time_lock_s": 60,
                "time_lock_min_bp": 5.0,
                "time_lock_slope_max_bp": 0.0,
                "time_lock_lookback_s": 30,
            },
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.12, side="SHORT")
    hold_start_ms = 1_700_000_000_000
    manager._trail_peak[run["run_id"]] = 99.95
    manager._codex_time_lock_price_history[run["run_id"]] = [((hold_start_ms + 25_000) / 1000.0, 99.955)]
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 55_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "SHORT",
        99.964,
        100.0,
        0.12,
        "BUY",
        hold_start_ms,
    )

    assert fired is False
    exit_event = next(details for _, event_type, details in repo.events if event_type == "codex_survival_exit")
    assert exit_event["reason"] == "CODEX_V1437_STUPS_THIN_LOCK"
    assert exit_event["v1437_stups_clean_extension_thin_lock"] is True
    assert exit_event["v1437_thin_lock_after_seconds"] == pytest.approx(50.0)
    assert exit_event["mfe_bp"] == pytest.approx(5.0)
    assert exit_event["current_bp"] == pytest.approx(3.6)
    assert any(o.get("timeInForce") == "GTX" for o in client.all_orders)
    assert client.market_orders == []
    assert ("ETHUSDC", 901) not in client.cancelled
    assert client.cancelled_algo == []
    assert any(
        event_type == "survival_maker_attempt" and details.get("reason") == "CODEX_V1437_STUPS_THIN_LOCK"
        for _, event_type, details in repo.events
    )
    assert any(event_type == "stups_thin_lock_maker_only_deferred" for _, event_type, _ in repo.events)
    assert not any(event_type == "survival_maker_fallback_market" for _, event_type, _ in repo.events)

@pytest.mark.asyncio
async def test_v1438_stups_counter_recoil_thin_lock_uses_maker_only(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=-0.12,
        entry_price=100.0,
        mark_price=99.935,
        unrealized_pnl=0.0066,
        liquidation_price=130.0,
        leverage=75,
        margin_type="cross",
    )
    client.position = position
    client.book = {"bidPrice": "99.940", "askPrice": "99.945"}
    client.open_orders.append({"orderId": 901, "clientOrderId": "cry3mn_test_tp1", "status": "NEW"})
    client.algo_orders.append({"algoId": 902, "clientAlgoId": "x-FAKE902", "reduceOnly": True, "algoStatus": "NEW"})
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_recovery_enabled=False,
            mainnet_codex_survival_enabled=True,
            mainnet_codex_survival_watch_after_seconds=300,
            mainnet_codex_survival_exit_use_maker=True,
            mainnet_codex_v1438_stups_thin_lock_enabled=True,
            mainnet_codex_v1438_stups_thin_lock_maker_ttl_seconds=0,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    signal = {
        "side": "SHORT",
        "codex_v1": {
            "enabled": True,
            "lane_code": "STUP-S",
            "lane": "codex_v1_stale_upmove_short_rng20_canary",
            "metrics": {
                "market_state": "STUP-S:counter_recoil",
                "time_profit_lock_enabled": True,
                "time_lock_s": 60,
                "time_lock_min_bp": 6.0,
                "time_lock_slope_max_bp": 0.0,
                "time_lock_lookback_s": 30,
            },
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.12, side="SHORT")
    hold_start_ms = 1_700_000_000_000
    manager._trail_peak[run["run_id"]] = 99.944
    manager._codex_time_lock_price_history[run["run_id"]] = [((hold_start_ms + 35_000) / 1000.0, 99.945)]
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 65_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "SHORT",
        99.945,
        100.0,
        0.12,
        "BUY",
        hold_start_ms,
    )

    assert fired is False
    exit_event = next(details for _, event_type, details in repo.events if event_type == "codex_survival_exit")
    assert exit_event["reason"] == "CODEX_V1438_STUPS_THIN_LOCK"
    assert exit_event["v1438_stups_thin_lock"] is True
    assert exit_event["v1438_thin_lock_after_seconds"] == pytest.approx(60.0)
    assert exit_event["v1438_thin_lock_mfe_bp"] == pytest.approx(5.5)
    assert exit_event["v1438_thin_lock_floor_bp"] == pytest.approx(5.0)
    assert exit_event["mfe_bp"] == pytest.approx(5.6)
    assert exit_event["current_bp"] == pytest.approx(5.5)
    assert any(o.get("timeInForce") == "GTX" for o in client.all_orders)
    assert client.market_orders == []
    assert ("ETHUSDC", 901) not in client.cancelled
    assert client.cancelled_algo == []
    assert any(
        event_type == "survival_maker_attempt" and details.get("reason") == "CODEX_V1438_STUPS_THIN_LOCK"
        for _, event_type, details in repo.events
    )
    assert any(
        event_type == "trail_maker_timeout" and details.get("reason") == "CODEX_V1438_STUPS_THIN_LOCK"
        for _, event_type, details in repo.events
    )
    assert any(event_type == "stups_v1438_thin_lock_maker_only_deferred" for _, event_type, _ in repo.events)
    assert not any(event_type == "survival_maker_fallback_market" for _, event_type, _ in repo.events)


@pytest.mark.asyncio
async def test_v1438_stups_counter_recoil_thin_lock_waits_below_floor(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=-0.12,
        entry_price=100.0,
        mark_price=99.951,
        unrealized_pnl=0.00588,
        liquidation_price=130.0,
        leverage=75,
        margin_type="cross",
    )
    client.position = position
    client.book = {"bidPrice": "99.946", "askPrice": "99.951"}
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_recovery_enabled=False,
            mainnet_codex_survival_enabled=True,
            mainnet_codex_survival_watch_after_seconds=300,
            mainnet_codex_survival_exit_use_maker=True,
            mainnet_codex_v1438_stups_thin_lock_enabled=True,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    signal = {
        "side": "SHORT",
        "codex_v1": {
            "enabled": True,
            "lane_code": "STUP-S",
            "metrics": {
                "market_state": "STUP-S:counter_recoil",
                "time_profit_lock_enabled": True,
                "time_lock_s": 60,
                "time_lock_min_bp": 6.0,
                "time_lock_slope_max_bp": 0.0,
                "time_lock_lookback_s": 30,
            },
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.12, side="SHORT")
    hold_start_ms = 1_700_000_000_000
    manager._trail_peak[run["run_id"]] = 99.944
    manager._codex_time_lock_price_history[run["run_id"]] = [((hold_start_ms + 35_000) / 1000.0, 99.951)]
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 65_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "SHORT",
        99.951,
        100.0,
        0.12,
        "BUY",
        hold_start_ms,
    )

    assert fired is False
    assert client.market_orders == []
    assert not any(event_type == "codex_survival_exit" for _, event_type, _ in repo.events)

def test_v1439_shadow_score_marks_block_candidate_without_blocking():
    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    raw = CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.39",
        baseline="unit",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="accepted",
        regime="STUP-S:clean_extension",
    )
    decision = CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.39",
        baseline="unit",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1427_five_window_tp14_adaptive_exec",
        regime="STUP-S:clean_extension",
        metrics={
            "market_state": "STUP-S:clean_extension",
            "policy_tag": "v1427_five_window_tp14_adaptive_exec",
        },
        policy_tag="v1427_five_window_tp14_adaptive_exec",
    )

    scored = manager._codex_v1439_apply_shadow_score(
        {
            "side": "SHORT",
            "rng15": 30.2,
            "range_pos_15": 0.92,
            "vwap_dist_bp": 55.0,
        },
        raw,
        decision,
    )

    assert scored.accepted is True
    assert scored.policy_tag == "v1427_five_window_tp14_adaptive_exec"
    assert scored.requested_notional_usdc == pytest.approx(50.0)
    metrics = scored.metrics or {}
    assert metrics["v1439_shadow_live_effect"] == "telemetry_only"
    assert metrics["v1439_shadow_action"] == "SHADOW_BLOCK_CANDIDATE"
    assert metrics["v1439_shadow_would_block"] is True
    assert "selector_clean_extension_short_rng15_gte_28_91" in metrics["v1439_shadow_reasons"]
    assert "v1439_shadow_score" in scored.risk_tags


def test_v1439_shadow_score_ignores_raw_vwap_price_without_distance():
    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    raw = CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.40",
        baseline="unit",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="accepted",
        regime="STUP-S:mixed",
    )
    decision = CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.40",
        baseline="unit",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1427_five_window_tp14_adaptive_exec",
        regime="STUP-S:mixed",
        metrics={"market_state": "STUP-S:mixed"},
        policy_tag="v1427_five_window_tp14_adaptive_exec",
    )

    scored = manager._codex_v1439_apply_shadow_score(
        {"side": "SHORT", "rng15": 24.0, "range_pos_15": 0.50, "vwap": 1698.0},
        raw,
        decision,
    )

    metrics = scored.metrics or {}
    assert metrics["v1439_shadow_features"]["vwap_dist_bp"] is None
    assert "vwap_extension_review" not in metrics["v1439_shadow_reasons"]
    assert metrics["v1439_shadow_action"] == "SHADOW_THIN_LOCK_CANDIDATE"


@pytest.mark.asyncio
async def test_v1439_stups_mixed_shadow_thin_lock_uses_maker_only(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=-0.12,
        entry_price=100.0,
        mark_price=99.935,
        unrealized_pnl=0.0066,
        liquidation_price=130.0,
        leverage=75,
        margin_type="cross",
    )
    client.position = position
    client.book = {"bidPrice": "99.940", "askPrice": "99.945"}
    client.open_orders.append({"orderId": 901, "clientOrderId": "cry3mn_test_tp1", "status": "NEW"})
    client.algo_orders.append({"algoId": 902, "clientAlgoId": "x-FAKE902", "reduceOnly": True, "algoStatus": "NEW"})
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_recovery_enabled=False,
            mainnet_codex_survival_enabled=True,
            mainnet_codex_survival_watch_after_seconds=300,
            mainnet_codex_survival_exit_use_maker=True,
            mainnet_codex_v1439_stups_shadow_thin_lock_enabled=True,
            mainnet_codex_v1439_stups_shadow_thin_lock_maker_ttl_seconds=0,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    signal = {
        "side": "SHORT",
        "codex_v1": {
            "enabled": True,
            "lane_code": "STUP-S",
            "lane": "codex_v1_stale_upmove_short_rng20_canary",
            "metrics": {
                "market_state": "STUP-S:mixed",
                "v1439_shadow_action": "SHADOW_THIN_LOCK_CANDIDATE",
                "time_profit_lock_enabled": True,
                "time_lock_s": 90,
                "time_lock_min_bp": 6.0,
                "time_lock_slope_max_bp": 0.0,
                "time_lock_lookback_s": 30,
            },
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.12, side="SHORT")
    hold_start_ms = 1_700_000_000_000
    manager._trail_peak[run["run_id"]] = 99.944
    manager._codex_time_lock_price_history[run["run_id"]] = [((hold_start_ms + 35_000) / 1000.0, 99.949)]
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 65_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "SHORT",
        99.945,
        100.0,
        0.12,
        "BUY",
        hold_start_ms,
    )

    assert fired is False
    exit_event = next(details for _, event_type, details in repo.events if event_type == "codex_survival_exit")
    assert exit_event["reason"] == "CODEX_V1439_SHADOW_THIN_LOCK"
    assert exit_event["v1439_stups_shadow_thin_lock"] is True
    assert exit_event["v1439_shadow_action"] == "SHADOW_THIN_LOCK_CANDIDATE"
    assert exit_event["v1439_thin_lock_after_seconds"] == pytest.approx(60.0)
    assert exit_event["v1439_thin_lock_mfe_bp"] == pytest.approx(5.5)
    assert exit_event["v1439_thin_lock_floor_bp"] == pytest.approx(5.0)
    assert exit_event["mfe_bp"] == pytest.approx(5.6)
    assert exit_event["current_bp"] == pytest.approx(5.5)
    assert any(o.get("timeInForce") == "GTX" for o in client.all_orders)
    assert client.market_orders == []
    assert ("ETHUSDC", 901) not in client.cancelled
    assert client.cancelled_algo == []
    assert any(
        event_type == "survival_maker_attempt" and details.get("reason") == "CODEX_V1439_SHADOW_THIN_LOCK"
        for _, event_type, details in repo.events
    )
    assert any(event_type == "stups_v1439_shadow_thin_lock_maker_only_deferred" for _, event_type, _ in repo.events)
    assert not any(event_type == "survival_maker_fallback_market" for _, event_type, _ in repo.events)


def test_v1441_selector_marks_stups_mixed_for_thin_lock_canary():
    manager = MainnetOneRunManager(
        _settings(mainnet_codex_v1443_stups_mixed_live_block_enabled=False),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    raw = CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.41",
        baseline="unit",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="accepted",
        regime="STUP-S:mixed",
    )
    decision = CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.41",
        baseline="unit",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1427_five_window_tp14_adaptive_exec",
        regime="STUP-S:mixed",
        metrics={"market_state": "STUP-S:mixed"},
        policy_tag="v1427_five_window_tp14_adaptive_exec",
    )

    scored = manager._codex_v1439_apply_shadow_score(
        {
            "side": "SHORT",
            "rng15": 21.3,
            "d30": -9.76,
            "adv3": 5.43,
            "rsi": 48.82,
            "vwap_dist_bp": 4.78,
            "pullback_from_recent_high_bp": 15.07,
        },
        raw,
        decision,
    )

    metrics = scored.metrics or {}
    assert metrics["v1441_research_selector_action"] == "ALLOW_THIN_LOCK_PROFILE"
    assert metrics["v1441_live_effect"] == "mixed_maker_thin_lock_canary"
    assert metrics["v1441_mixed_thin_lock_trigger"]["mfe_bp"] == pytest.approx(3.0)
    assert metrics["v1441_mixed_thin_lock_trigger"]["floor_bp"] == pytest.approx(2.5)
    assert "v1441_allow_thin_lock_profile" in scored.risk_tags



def test_v1441_selector_still_runs_when_v1439_shadow_score_disabled():
    manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_v1439_shadow_score_enabled=False,
            mainnet_codex_v1443_stups_mixed_live_block_enabled=False,
        ),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    raw = CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.41",
        baseline="unit",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="accepted",
        regime="STUP-S:mixed",
    )
    decision = CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.41",
        baseline="unit",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1427_five_window_tp14_adaptive_exec",
        regime="STUP-S:mixed",
        metrics={"market_state": "STUP-S:mixed"},
        policy_tag="v1427_five_window_tp14_adaptive_exec",
    )

    scored = manager._codex_v1439_apply_shadow_score(
        {"side": "SHORT", "rng15": 21.3, "vwap_dist_bp": 4.78},
        raw,
        decision,
    )

    metrics = scored.metrics or {}
    assert metrics["v1439_shadow_score_enabled"] is False
    assert metrics["v1441_research_selector_action"] == "ALLOW_THIN_LOCK_PROFILE"
    assert "v1441_allow_thin_lock_profile" in scored.risk_tags
    assert "v1439_shadow_score" not in scored.risk_tags

def test_v1443_selector_blocks_stups_mixed_live_by_default():
    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    raw = CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.43",
        baseline="unit",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="accepted",
        regime="STUP-S:mixed",
    )
    decision = CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.43",
        baseline="unit",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1427_five_window_tp14_adaptive_exec",
        regime="STUP-S:mixed",
        metrics={"market_state": "STUP-S:mixed"},
        policy_tag="v1427_five_window_tp14_adaptive_exec",
    )

    scored = manager._codex_v1439_apply_shadow_score(
        {"side": "SHORT", "rng15": 21.3, "vwap_dist_bp": 4.78},
        raw,
        decision,
    )

    assert scored.accepted is False
    assert scored.reason == "v1443_stups_mixed_live_block"
    assert scored.policy_tag == "v1443_stups_mixed_live_block"
    assert scored.shadow_lane == "SH_STUPS_MIXED_LIVE_BLOCK"
    metrics = scored.metrics or {}
    assert metrics["v1441_research_selector_action"] == "BLOCK_ENTRY_QUALITY"
    assert metrics["v1441_live_effect"] == "hard_block"
    assert metrics["v1443_live_block_reason"] == "v1443_stups_mixed_live_block"
    assert metrics["v1442_live_block_reason"] is None
    assert "v1443_live_block" in scored.risk_tags

def test_v1442_selector_blocks_stups_clean_extension_long_chase():
    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    raw = CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.42",
        baseline="unit",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="accepted",
        regime="STUP-S:clean_extension",
    )
    decision = CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.42",
        baseline="unit",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1427_five_window_tp14_adaptive_exec",
        regime="STUP-S:clean_extension",
        metrics={"market_state": "STUP-S:clean_extension", "v1427_previous_side": "SHORT"},
        policy_tag="v1427_five_window_tp14_adaptive_exec",
    )

    scored = manager._codex_v1439_apply_shadow_score(
        {
            "side": "SHORT",
            "rng15": 55.51,
            "d30": 37.29,
            "adv3": 16.8,
            "rsi": 58.16,
            "vwap_dist_bp": 10.03,
            "pullback_from_recent_high_bp": 35.57,
        },
        raw,
        decision,
    )

    assert scored.accepted is False
    assert scored.reason == "v1442_stups_clean_extension_chase_block"
    metrics = scored.metrics or {}
    assert metrics["v1441_research_selector_action"] == "BLOCK_ENTRY_QUALITY"
    assert metrics["v1441_live_effect"] == "hard_block"
    assert metrics["v1442_live_block_reason"] == "v1442_stups_clean_extension_chase_block"
    assert scored.policy_tag == "v1442_stups_clean_extension_chase_block"
    assert "v1442_live_block" in scored.risk_tags



def test_v1447_selector_blocks_stups_long_chase_when_premium_high_and_slope_weak():
    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    raw = CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.47",
        baseline="unit",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="accepted",
        regime="STUP-S:clean_extension",
    )
    decision = CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.47",
        baseline="unit",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1427_five_window_tp14_adaptive_exec",
        regime="STUP-S:clean_extension",
        metrics={"market_state": "STUP-S:clean_extension", "v1427_previous_side": "SHORT"},
        policy_tag="v1427_five_window_tp14_adaptive_exec",
    )

    scored = manager._codex_v1439_apply_shadow_score(
        {
            "side": "SHORT",
            "rng15": 37.1175,
            "d30": 24.4045,
            "adv3": 8.1794,
            "rsi": 64.198,
            "slope30": -2.1841,
            "slope60": -4.3682,
            "vwap_dist_bp": 36.6561,
            "pullback_from_recent_high_bp": 24.1775,
            "reprice_wait_elapsed_seconds": 440.0,
        },
        raw,
        decision,
    )

    assert scored.accepted is False
    assert scored.reason == "v1447_stups_clean_extension_long_chase_quality_block"
    assert scored.policy_tag == "v1447_stups_clean_extension_long_chase_quality_block"
    assert scored.shadow_lane == "SH_STUPS_CLEAN_EXTENSION_LONG_CHASE_QUALITY_BLOCK"
    metrics = scored.metrics or {}
    assert metrics["v1441_research_selector_action"] == "BLOCK_ENTRY_QUALITY"
    assert metrics["v1441_live_effect"] == "hard_block"
    assert metrics["v1447_live_block_reason"] == "v1447_stups_clean_extension_long_chase_quality_block"
    assert metrics["v1447_stups_long_chase_quality_matched"] is True
    assert "high_vwap_premium_with_rng_or_d30_extension" in metrics["v1447_stups_long_chase_quality_reasons"]
    assert "d30_extended_but_slope30_turned_down" in metrics["v1447_stups_long_chase_quality_reasons"]
    assert "stale_wait_with_slope30_nonpositive" in metrics["v1447_stups_long_chase_quality_reasons"]
    assert "v1447_live_block" in scored.risk_tags


def test_v1447_selector_allows_stups_long_chase_when_wait_is_stale_but_slope_strong():
    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    raw = CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.47",
        baseline="unit",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="accepted",
        regime="STUP-S:clean_extension",
    )
    decision = CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.47",
        baseline="unit",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1427_five_window_tp14_adaptive_exec",
        regime="STUP-S:clean_extension",
        metrics={"market_state": "STUP-S:clean_extension", "v1427_previous_side": "SHORT"},
        policy_tag="v1427_five_window_tp14_adaptive_exec",
    )

    scored = manager._codex_v1439_apply_shadow_score(
        {
            "side": "SHORT",
            "rng15": 23.2628,
            "d30": 7.4509,
            "adv3": 11.0464,
            "rsi": 58.1206,
            "slope30": 2.4754,
            "slope60": 4.9508,
            "vwap_dist_bp": 19.79,
            "pullback_from_recent_high_bp": 15.0725,
            "reprice_wait_elapsed_seconds": 1880.0,
        },
        raw,
        decision,
    )

    assert scored.accepted is True
    assert scored.reason == "v1427_five_window_tp14_adaptive_exec"
    metrics = scored.metrics or {}
    assert metrics["v1441_research_selector_action"] == "SHADOW_REVIEW"
    assert metrics["v1447_live_block_reason"] is None
    assert metrics["v1447_stups_long_chase_quality_matched"] is False
    assert "v1447_live_block" not in scored.risk_tags

@pytest.mark.asyncio
async def test_codex_preflight_ignores_foreign_mainnet_open_orders():
    from src.gridbot.strategy.long_pullback import SignalPlan
    from src.gridbot.strategy.wildcat_live import WildcatLiveDecision

    client = FakeClient()
    client.open_orders = [
        {
            "orderId": 1,
            "clientOrderId": "aos_foreign_entry",
            "reduceOnly": False,
            "status": "NEW",
        },
        {
            "orderId": 2,
            "clientOrderId": "cry3mn_unit_tp1",
            "reduceOnly": True,
            "status": "NEW",
        },
    ]
    manager = MainnetOneRunManager(_settings(), client, FakeRepo(), FakeTelegramApp())
    signal = SignalPlan(
        action="SELL",
        confidence=1,
        score=82,
        symbol="ETHUSDC",
        price=100.0,
        rsi=62.0,
        atr=1.0,
        support=99.0,
        vwap=100.0,
        entries=[100.0],
        entry_weights=[1.0],
        stop_loss=101.0,
        take_profits=[99.0],
        planned_notional_usdc=50.0,
        planned_margin_usdc=1.0,
        planned_qty=0.5,
        risk_amount_usdc=0.5,
        reasons=["wildcat:S1_BB_RSI"],
        risk_notes=[],
    )
    decision = WildcatLiveDecision(
        signal=signal,
        strategy="S1_BB_RSI",
        side="SHORT",
        tp_pct=0.001,
        sl_pct=0.001,
        partial_exit_pct=1.0,
        partial_tp_pct=0.001,
        recovery_steps=1,
        recovery_trigger_pct=0.001,
        recovery_tp_shrink=0.45,
        adverse_exit_bars=10,
        adverse_exit_loss_pct=0.001,
        max_holding_bars=10,
        params_label="unit",
    )

    features = await manager._build_codex_v1_live_features_for_decision(
        _run(symbol="ETHUSDC"),
        decision,
        [],
        rng15=20.0,
        drift_bp=0.0,
    )

    assert features["open_order_count"] == 2
    assert features["open_run_order_count"] == 1
    assert features["ignored_foreign_open_order_count"] == 1
    assert features["open_entry_order"] is False
    assert features["open_reduce_order"] is True


def test_v1445_selector_blocks_stups_clean_extension_short_quality_slice():
    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    raw = CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.45",
        baseline="unit",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="accepted",
        regime="STUP-S:clean_extension",
    )
    decision = CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.45",
        baseline="unit",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1427_five_window_tp14_adaptive_exec",
        regime="STUP-S:clean_extension",
        metrics={"market_state": "STUP-S:clean_extension"},
        policy_tag="v1427_five_window_tp14_adaptive_exec",
    )

    scored = manager._codex_v1439_apply_shadow_score(
        {
            "side": "SHORT",
            "rng15": 28.7554,
            "d30": 8.8259,
            "adv3": 10.7735,
            "rsi": 60.6355,
            "slope30": 2.2502,
            "vwap_dist_bp": 30.6442,
            "pullback_from_recent_high_bp": 21.8655,
        },
        raw,
        decision,
    )

    assert scored.accepted is False
    assert scored.reason == "v1445_stups_clean_extension_short_quality_block"
    assert scored.policy_tag == "v1445_stups_clean_extension_short_quality_block"
    assert scored.shadow_lane == "SH_STUPS_CLEAN_EXTENSION_SHORT_QUALITY_BLOCK"
    metrics = scored.metrics or {}
    assert metrics["v1441_research_selector_action"] == "BLOCK_ENTRY_QUALITY"
    assert metrics["v1441_live_effect"] == "hard_block"
    assert metrics["v1445_live_block_reason"] == "v1445_stups_clean_extension_short_quality_block"
    assert metrics["v1445_stups_clean_short_quality_matched"] is True
    assert metrics["v1445_stups_clean_short_quality_thresholds"]["rsi_max"] == pytest.approx(60.8432)
    assert metrics["v1445_stups_clean_short_quality_thresholds"]["slope30_min_bp"] == pytest.approx(1.26926)
    assert metrics["v1441_features"]["slope30"] == pytest.approx(2.2502)
    assert "v1445_live_block" in scored.risk_tags


def test_v1445_selector_allows_clean_extension_short_when_slope_not_in_bad_slice():
    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    raw = CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.45",
        baseline="unit",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="accepted",
        regime="STUP-S:clean_extension",
    )
    decision = CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.45",
        baseline="unit",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1427_five_window_tp14_adaptive_exec",
        regime="STUP-S:clean_extension",
        metrics={"market_state": "STUP-S:clean_extension"},
        policy_tag="v1427_five_window_tp14_adaptive_exec",
    )

    scored = manager._codex_v1439_apply_shadow_score(
        {
            "side": "SHORT",
            "rng15": 24.0,
            "d30": 2.0,
            "adv3": 3.0,
            "rsi": 60.0,
            "slope30": 0.75,
            "vwap_dist_bp": 30.0,
            "pullback_from_recent_high_bp": 20.0,
        },
        raw,
        decision,
    )

    assert scored.accepted is True
    assert scored.reason == "v1427_five_window_tp14_adaptive_exec"
    metrics = scored.metrics or {}
    assert metrics["v1441_research_selector_action"] == "OBSERVE"
    assert metrics["v1445_live_block_reason"] is None
    assert metrics["v1445_stups_clean_short_quality_matched"] is False
    assert "v1445_live_block" not in scored.risk_tags


def test_v1442_cnl_wpr_strict_selector_caps_entry_ttl_to_twenty_seconds():
    manager = MainnetOneRunManager(
        _settings(mainnet_codex_v1442_cnl_wpr_strict_entry_ttl_seconds=20),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )

    policy = manager._codex_v1_live_entry_ttl_policy(
        {
            "signal_json": {
                "codex_v1": {
                    "enabled": True,
                    "lane_code": "CNL-WPR-L",
                    "metrics": {
                        "ttl_s": 90,
                        "v1441_research_selector_action": "STRICT_TTL_OR_FILL_POLICY",
                    },
                }
            }
        }
    )

    assert policy["ttl_seconds"] == 20
    assert policy["ttl_source"] == "codex_v1442_selector_strict_ttl"


def test_v1442_cnl_wpr_max_hold_profit_lock_uses_thin_maker_floor():
    manager = MainnetOneRunManager(
        _settings(mainnet_codex_v1442_cnl_wpr_max_hold_floor_bp=4.0),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    signal = {
        "side": "LONG",
        "codex_v1": {
            "enabled": True,
            "lane_code": "CNL-WPR-L",
            "metrics": {
                "market_state": "CNL-WPR-L:falling_discount_trap",
                "v1441_research_selector_action": "STRICT_TTL_OR_FILL_POLICY",
            },
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0)

    assert manager._should_codex_max_hold_profit_lock(signal, "LONG", 100.0, 100.045) is True
    assert manager._should_defer_codex_max_hold_win_fee_floor(signal, "LONG", 100.0, 100.045, 1, 1) is False
    assert manager._survival_maker_profit_floor_bp("CODEX_MAX_HOLD_PROFIT_LOCK", run) == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_v1442_cnl_wpr_max_hold_profit_lock_does_not_abort_at_thin_floor(monkeypatch):
    from src.gridbot.mainnet import one_run as _mod

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(_mod.asyncio, "sleep", _no_sleep)
    client = FakeClient()
    client.position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=100.06,
        unrealized_pnl=0.0084,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    client.book = {"bidPrice": "100.045", "askPrice": "100.055"}
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_survival_exit_use_maker=True,
            mainnet_codex_max_hold_profit_maker_ttl_seconds=0,
            mainnet_codex_v1442_cnl_wpr_max_hold_floor_bp=4.0,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _run(
        avg_entry_price=100.0,
        signal_json=json.dumps(
            {
                "side": "LONG",
                "codex_v1": {
                    "enabled": True,
                    "lane_code": "CNL-WPR-L",
                    "metrics": {
                        "market_state": "CNL-WPR-L:falling_discount_trap",
                        "v1441_research_selector_action": "STRICT_TTL_OR_FILL_POLICY",
                    },
                },
            }
        ),
    )

    submitted = await manager._close_position("ETHUSDC", "SELL", 0.12, "CODEX_MAX_HOLD_PROFIT_LOCK", run)

    assert submitted is False
    assert any(o.get("timeInForce") == "GTX" and o.get("reduceOnly") is True for o in client.all_orders)
    assert not any(event_type == "survival_profit_lock_aborted_anchor_floor" for _, event_type, _ in repo.events)
    assert any(event_type == "max_hold_profit_maker_only_deferred" for _, event_type, _ in repo.events)

@pytest.mark.asyncio
async def test_v1443_stups_clean_extension_reversal_scratch_fee_floor_blocks_thin_close(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=-0.12,
        entry_price=100.0,
        mark_price=99.99,
        unrealized_pnl=0.0012,
        liquidation_price=130.0,
        leverage=75,
        margin_type="cross",
    )
    client.position = position
    client.book = {"bidPrice": "99.98", "askPrice": "99.99"}
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_recovery_enabled=False,
            mainnet_codex_survival_enabled=True,
            mainnet_codex_survival_watch_after_seconds=300,
            mainnet_codex_survival_exit_use_maker=True,
            mainnet_codex_v1443_stups_clean_extension_reversal_maker_ttl_seconds=0,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    signal = {
        "side": "SHORT",
        "codex_v1": {
            "enabled": True,
            "lane_code": "STUP-S",
            "lane": "codex_v1_stale_upmove_short_rng20_canary",
            "metrics": {
                "market_state": "STUP-S:clean_extension",
                "time_profit_lock_enabled": True,
                "time_lock_s": 90,
                "time_lock_min_bp": 6.0,
                "time_lock_slope_max_bp": 0.0,
                "time_lock_lookback_s": 30,
            },
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.12, side="SHORT")
    hold_start_ms = 1_700_000_000_000
    manager._trail_peak[run["run_id"]] = 99.94
    manager._codex_time_lock_price_history[run["run_id"]] = [((hold_start_ms + 20_000) / 1000.0, 99.96)]
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 50_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "SHORT",
        99.99,
        100.0,
        0.12,
        "BUY",
        hold_start_ms,
    )

    assert fired is False
    assert not any(event_type == "codex_survival_exit" for _, event_type, _ in repo.events)
    assert client.all_orders == []
    assert client.market_orders == []

@pytest.mark.asyncio
async def test_v1448_stups_clean_extension_fast_scalp_lock_is_fee_safe_maker_only(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=-0.12,
        entry_price=100.0,
        mark_price=99.93,
        unrealized_pnl=0.0084,
        liquidation_price=130.0,
        leverage=75,
        margin_type="cross",
    )
    client.position = position
    client.book = {"bidPrice": "99.925", "askPrice": "99.930"}
    client.open_orders.append({"orderId": 901, "clientOrderId": "cry3mn_test_tp1", "status": "NEW"})
    client.algo_orders.append({"algoId": 902, "clientAlgoId": "x-FAKE902", "reduceOnly": True, "algoStatus": "NEW"})
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_recovery_enabled=False,
            mainnet_codex_survival_enabled=True,
            mainnet_codex_survival_watch_after_seconds=300,
            mainnet_codex_survival_exit_use_maker=True,
            mainnet_codex_v1448_stups_clean_extension_fast_scalp_enabled=True,
            mainnet_codex_v1448_stups_clean_extension_fast_scalp_after_seconds=10,
            mainnet_codex_v1448_stups_clean_extension_fast_scalp_mfe_bp=6.0,
            mainnet_codex_v1448_stups_clean_extension_fast_scalp_floor_bp=5.0,
            mainnet_codex_v1448_stups_clean_extension_fast_scalp_maker_ttl_seconds=0,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    signal = {
        "side": "SHORT",
        "codex_v1": {
            "enabled": True,
            "lane_code": "STUP-S",
            "lane": "codex_v1_stale_upmove_short_rng20_canary",
            "metrics": {
                "market_state": "STUP-S:clean_extension",
                "time_profit_lock_enabled": True,
                "time_lock_s": 90,
                "time_lock_min_bp": 6.0,
                "time_lock_slope_max_bp": 0.0,
                "time_lock_lookback_s": 30,
            },
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.12, side="SHORT")
    hold_start_ms = 1_700_000_000_000
    manager._trail_peak[run["run_id"]] = 99.92
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 15_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "SHORT",
        99.93,
        100.0,
        0.12,
        "BUY",
        hold_start_ms,
    )

    assert fired is False
    exit_event = next(details for _, event_type, details in repo.events if event_type == "codex_survival_exit")
    assert exit_event["reason"] == "CODEX_V1448_STUPS_FAST_SCALP_LOCK"
    assert exit_event["v1448_stups_fast_scalp_lock"] is True
    assert exit_event["v1448_fast_scalp_after_seconds"] == pytest.approx(10.0)
    assert exit_event["v1448_fast_scalp_mfe_bp"] == pytest.approx(6.0)
    assert exit_event["v1448_fast_scalp_floor_bp"] == pytest.approx(5.9)
    assert exit_event["mfe_bp"] == pytest.approx(8.0)
    assert exit_event["current_bp"] == pytest.approx(7.0)
    assert any(o.get("timeInForce") == "GTX" and o.get("reduceOnly") is True for o in client.all_orders)
    assert client.market_orders == []
    assert ("ETHUSDC", 901) not in client.cancelled
    assert client.cancelled_algo == []
    assert any(event_type == "stups_v1448_fast_scalp_maker_only_deferred" for _, event_type, _ in repo.events)

@pytest.mark.asyncio
async def test_v1441_stups_mixed_thin_lock_uses_low_floor_maker_only(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=-0.12,
        entry_price=100.0,
        mark_price=99.972,
        unrealized_pnl=0.00336,
        liquidation_price=130.0,
        leverage=75,
        margin_type="cross",
    )
    client.position = position
    client.book = {"bidPrice": "99.970", "askPrice": "99.972"}
    client.open_orders.append({"orderId": 901, "clientOrderId": "cry3mn_test_tp1", "status": "NEW"})
    client.algo_orders.append({"algoId": 902, "clientAlgoId": "x-FAKE902", "reduceOnly": True, "algoStatus": "NEW"})
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_recovery_enabled=False,
            mainnet_codex_survival_enabled=True,
            mainnet_codex_survival_watch_after_seconds=300,
            mainnet_codex_survival_exit_use_maker=True,
            mainnet_codex_v1441_mixed_thin_lock_enabled=True,
            mainnet_codex_v1441_mixed_thin_lock_maker_ttl_seconds=0,
            mainnet_codex_v1441_mixed_thin_lock_after_seconds=45,
            mainnet_codex_v1441_mixed_thin_lock_mfe_bp=3.0,
            mainnet_codex_v1441_mixed_thin_lock_floor_bp=2.5,
            mainnet_codex_v1441_mixed_thin_lock_slope_max_bp=0.75,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    signal = {
        "side": "SHORT",
        "codex_v1": {
            "enabled": True,
            "lane_code": "STUP-S",
            "lane": "codex_v1_stale_upmove_short_rng20_canary",
            "metrics": {
                "market_state": "STUP-S:mixed",
                "v1441_research_selector_action": "ALLOW_THIN_LOCK_PROFILE",
                "time_profit_lock_enabled": True,
                "time_lock_s": 90,
                "time_lock_min_bp": 6.0,
                "time_lock_slope_max_bp": 0.0,
                "time_lock_lookback_s": 30,
            },
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.12, side="SHORT")
    hold_start_ms = 1_700_000_000_000
    manager._trail_peak[run["run_id"]] = 99.969
    manager._codex_time_lock_price_history[run["run_id"]] = [((hold_start_ms + 20_000) / 1000.0, 99.973)]
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 50_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "SHORT",
        99.972,
        100.0,
        0.12,
        "BUY",
        hold_start_ms,
    )

    assert fired is False
    exit_event = next(details for _, event_type, details in repo.events if event_type == "codex_survival_exit")
    assert exit_event["reason"] == "CODEX_V1441_MIXED_THIN_LOCK"
    assert exit_event["v1441_mixed_thin_lock"] is True
    assert exit_event["v1441_selector_action"] == "ALLOW_THIN_LOCK_PROFILE"
    assert exit_event["v1441_thin_lock_after_seconds"] == pytest.approx(45.0)
    assert exit_event["v1441_thin_lock_mfe_bp"] == pytest.approx(3.0)
    assert exit_event["v1441_thin_lock_floor_bp"] == pytest.approx(2.5)
    assert exit_event["mfe_bp"] == pytest.approx(3.1)
    assert exit_event["current_bp"] == pytest.approx(2.8)
    assert any(o.get("timeInForce") == "GTX" for o in client.all_orders)
    assert client.market_orders == []
    assert ("ETHUSDC", 901) not in client.cancelled
    assert client.cancelled_algo == []
    assert any(
        event_type == "survival_maker_attempt" and details.get("reason") == "CODEX_V1441_MIXED_THIN_LOCK"
        for _, event_type, details in repo.events
    )
    assert any(event_type == "stups_v1441_mixed_thin_lock_maker_only_deferred" for _, event_type, _ in repo.events)
    assert not any(event_type == "survival_maker_fallback_market" for _, event_type, _ in repo.events)

@pytest.mark.asyncio
async def test_v1444_cnl_wpr_deep_trail_lock_uses_maker_only_relaxed_floor(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=100.052,
        unrealized_pnl=0.00624,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    client.position = position
    client.book = {"bidPrice": "100.047", "askPrice": "100.052"}
    client.open_orders.append({"orderId": 901, "clientOrderId": "cry3mn_test_tp1", "status": "NEW"})
    client.algo_orders.append({"algoId": 902, "clientAlgoId": "x-FAKE902", "reduceOnly": True, "algoStatus": "NEW"})
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_recovery_enabled=False,
            mainnet_codex_survival_enabled=True,
            mainnet_codex_survival_watch_after_seconds=300,
            mainnet_codex_survival_exit_use_maker=True,
            mainnet_codex_v1444_cnl_wpr_deep_trail_lock_enabled=True,
            mainnet_codex_v1444_cnl_wpr_deep_trail_maker_ttl_seconds=0,
            mainnet_codex_v1444_cnl_wpr_deep_trail_floor_bp=4.5,
            mainnet_codex_v1444_cnl_wpr_deep_time_lock_floor_bp=4.5,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    signal = {
        "side": "LONG",
        "codex_v1": {
            "enabled": True,
            "lane_code": "CNL-WPR-L",
            "lane": "v139_canary_watch_pre_reprice_long_s1",
            "metrics": {
                "market_state": "CNL-WPR-L:deep_discount_stable",
                "time_profit_lock_enabled": True,
                "time_lock_s": 60,
                "time_lock_min_bp": 5.0,
                "time_lock_slope_max_bp": 0.0,
                "time_lock_lookback_s": 30,
            },
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.12, side="LONG")
    hold_start_ms = 1_700_000_000_000
    manager._trail_peak[run["run_id"]] = 100.068
    manager._codex_time_lock_price_history[run["run_id"]] = [((hold_start_ms + 31_000) / 1000.0, 100.060)]
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 61_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "LONG",
        100.052,
        100.0,
        0.12,
        "SELL",
        hold_start_ms,
    )

    assert fired is False
    assert manager._survival_maker_profit_floor_bp("CODEX_V1427_TIME_LOCK", run) == pytest.approx(4.5)
    assert manager._survival_maker_profit_floor_bp("CODEX_V1444_CNL_WPR_DEEP_TRAIL_LOCK", run) == pytest.approx(4.5)
    exit_event = next(details for _, event_type, details in repo.events if event_type == "codex_survival_exit")
    assert exit_event["reason"] == "CODEX_V1444_CNL_WPR_DEEP_TRAIL_LOCK"
    assert exit_event["v1444_cnl_wpr_deep_trail_lock"] is True
    assert exit_event["mfe_bp"] == pytest.approx(6.8)
    assert exit_event["current_bp"] == pytest.approx(5.2)
    assert exit_event["v1444_cnl_wpr_deep_trail_slope_bp"] < 0
    assert any(o.get("timeInForce") == "GTX" and o.get("reduceOnly") is True for o in client.all_orders)
    assert client.market_orders == []
    assert ("ETHUSDC", 901) not in client.cancelled
    assert client.cancelled_algo == []
    assert not any(event_type == "survival_profit_lock_aborted_anchor_floor" for _, event_type, _ in repo.events)
    assert any(
        event_type == "survival_maker_attempt"
        and details.get("reason") == "CODEX_V1444_CNL_WPR_DEEP_TRAIL_LOCK"
        and details.get("profit_floor_bp") == pytest.approx(4.5)
        for _, event_type, details in repo.events
    )
    assert any(event_type == "cnl_wpr_deep_trail_maker_only_deferred" for _, event_type, _ in repo.events)
    assert not any(event_type == "survival_maker_fallback_market" for _, event_type, _ in repo.events)


@pytest.mark.asyncio
async def test_v1444_cnl_wpr_deep_trail_lock_waits_when_slope_still_favorable(monkeypatch):
    import src.gridbot.mainnet.one_run as or_mod

    client = FakeClient()
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.12,
        entry_price=100.0,
        mark_price=100.060,
        unrealized_pnl=0.0084,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )
    client.position = position
    client.book = {"bidPrice": "100.055", "askPrice": "100.060"}
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_recovery_enabled=False,
            mainnet_codex_survival_enabled=True,
            mainnet_codex_survival_watch_after_seconds=300,
            mainnet_codex_survival_exit_use_maker=True,
            mainnet_codex_v1444_cnl_wpr_deep_trail_lock_enabled=True,
        ),
        client,
        repo,
        FakeTelegramApp(),
    )
    signal = {
        "side": "LONG",
        "codex_v1": {
            "enabled": True,
            "lane_code": "CNL-WPR-L",
            "lane": "v139_canary_watch_pre_reprice_long_s1",
            "metrics": {
                "market_state": "CNL-WPR-L:deep_discount_stable",
                "time_profit_lock_enabled": True,
                "time_lock_s": 60,
                "time_lock_min_bp": 5.0,
                "time_lock_slope_max_bp": 0.0,
                "time_lock_lookback_s": 30,
            },
        },
    }
    run = _run(signal_json=json.dumps(signal), avg_entry_price=100.0, qty=0.12, side="LONG")
    hold_start_ms = 1_700_000_000_000
    manager._trail_peak[run["run_id"]] = 100.068
    manager._codex_time_lock_price_history[run["run_id"]] = [((hold_start_ms + 31_000) / 1000.0, 100.020)]
    monkeypatch.setattr(or_mod.time, "time", lambda: (hold_start_ms + 61_000) / 1000.0)

    fired = await manager._maybe_codex_survival_exit(
        run,
        signal,
        position,
        "LONG",
        100.060,
        100.0,
        0.12,
        "SELL",
        hold_start_ms,
    )

    assert fired is False
    assert client.market_orders == []
    assert not any(event_type == "codex_survival_exit" for _, event_type, _ in repo.events)

@pytest.mark.asyncio
async def test_v1457_adaptive_start_locks_runtime_and_stop_restores_it():
    settings = _settings(
        mainnet_equity_cap_usdc=200.0,
        mainnet_initial_notional_usdc=200.0,
        mainnet_max_cumulative_notional_usdc=800.0,
        mainnet_codex_recovery_enabled=True,
    )
    repo = FakeRepo()
    manager = MainnetOneRunManager(settings, FakeClient(), repo, FakeTelegramApp())
    manager._dca_enabled = True
    manager._loop_loss_cap = 5.0

    result = await manager.start_adaptive_session(actor="telegram")

    assert "Adaptive continuous session" in result
    assert manager._adaptive_session is not None
    assert len(manager._adaptive_session["config_sha"]) == 64
    assert settings.mainnet_effective_entry_notional_usdc == pytest.approx(50.0)
    assert settings.mainnet_effective_max_cumulative_notional_usdc == pytest.approx(50.0)
    assert manager._loop_loss_cap == pytest.approx(1.0)
    assert manager._dca_enabled is False
    assert settings.mainnet_recovery_enabled is False
    assert repo.created[-1]["params"]["adaptive"]["dca_enabled"] is False
    assert repo.created[-1]["params"]["adaptive"]["target_paid_closed_fills"] == 20
    assert "max_terminal_runs" not in repo.created[-1]["params"]["adaptive"]
    assert "locked at 50" in await manager.set_notional(200)
    assert "locked at -1" in await manager.set_loop_loss_cap(5)
    assert "locked off" in await manager.set_dca_enabled(True)

    stopped = await manager.stop_loop()

    assert "Adaptive loop" in stopped
    assert manager._adaptive_session is None
    assert settings.mainnet_effective_entry_notional_usdc == pytest.approx(200.0)
    assert settings.mainnet_effective_max_cumulative_notional_usdc == pytest.approx(800.0)
    assert manager._loop_loss_cap == pytest.approx(5.0)
    assert manager._dca_enabled is True
    assert settings.mainnet_recovery_enabled is True


@pytest.mark.asyncio
async def test_v1457_adaptive_loss_and_positive_high_water_stops():
    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    await manager.start_adaptive_session()
    run = _run(params={"mode": "adaptive_continuous", "adaptive": manager._adaptive_metadata(manager._adaptive_session)})
    armed = []

    async def fake_arm(initial=False):
        armed.append(initial)
        return "armed"

    manager._arm_adaptive_run = fake_arm
    assert await manager._adaptive_after_terminal(run, 1.20, "TP") is True
    assert manager._adaptive_session is not None
    assert armed == [False]
    second_run = _run(run_id="cry3mn_adaptive_second", params=run["params"])
    assert await manager._adaptive_after_terminal(second_run, -1.00, "SL") is True
    assert manager._adaptive_session is None
    assert manager._adaptive_last_review["stop_reason"] == "high_water_giveback"

    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    await manager.start_adaptive_session()
    run = _run(params={"mode": "adaptive_continuous", "adaptive": manager._adaptive_metadata(manager._adaptive_session)})
    manager._arm_adaptive_run = fake_arm
    await manager._adaptive_after_terminal(run, -1.01, "SL")
    assert manager._adaptive_session is None
    assert manager._adaptive_last_review["stop_reason"] == "net_loss_cap"


@pytest.mark.asyncio
async def test_v1459_adaptive_stops_at_twenty_paid_closed_fills():
    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    await manager.start_adaptive_session()
    manager._adaptive_session["terminal_runs"] = 27
    manager._adaptive_session["counters"]["paid_closed_fills"] = 19
    run = _run(
        run_id="cry3mn_adaptive_twentieth",
        params={"mode": "adaptive_continuous", "adaptive": manager._adaptive_metadata(manager._adaptive_session)},
    )

    assert await manager._adaptive_after_terminal(run, 0.05, "TP") is True
    assert manager._adaptive_session is None
    assert manager._adaptive_last_review["terminal_runs"] == 28
    assert manager._adaptive_last_review["counters"]["paid_closed_fills"] == 20
    assert manager._adaptive_last_review["stop_reason"] == "paid_closed_fill_target"


@pytest.mark.asyncio
async def test_v1459_many_no_fill_attempts_do_not_consume_paid_fill_target_or_performance():
    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    await manager.start_adaptive_session()
    session = manager._adaptive_session
    assert session is not None
    session["net_pnl_usdc"] = 0.42
    session["high_water_net_pnl_usdc"] = 0.42
    session["route_loss_streaks"] = {"seed-route": 1}
    session["last_loss_route"] = "seed-route"
    session["state_net_pnl_usdc"] = {"RANGE": -0.2}
    session["state_throttle_count"] = {"RANGE": 1}
    session["state_throttle_deadlines"] = {"RANGE": 123.0}
    counters = session["counters"]
    counters["gross_pnl_usdc"] = 0.50
    counters["commission_usdc"] = 0.08
    counters["net_pnl_usdc"] = 0.42
    counters["route_state_action_pnl"] = {
        "seed-slice": {
            "runs": 1,
            "gross_pnl_usdc": 0.50,
            "commission_usdc": 0.08,
            "funding_usdc": 0.0,
            "net_pnl_usdc": 0.42,
        }
    }
    initial_route_buckets = json.loads(json.dumps(counters["route_state_action_pnl"]))
    armed = []

    async def fake_arm(initial=False):
        armed.append(initial)
        return "armed"

    manager._arm_adaptive_run = fake_arm
    for index in range(25):
        run = _run(
            run_id=f"cry3mn_adaptive_no_fill_{index}",
            params={"mode": "adaptive_continuous", "adaptive": manager._adaptive_metadata(session)},
        )
        await manager._advance_loop_after_entry_failure(run, "signal_timeout")

    assert manager._adaptive_session is session
    assert session["terminal_runs"] == 25
    assert counters["paid_closed_fills"] == 0
    assert session["net_pnl_usdc"] == pytest.approx(0.42)
    assert counters["gross_pnl_usdc"] == pytest.approx(0.50)
    assert counters["commission_usdc"] == pytest.approx(0.08)
    assert counters["net_pnl_usdc"] == pytest.approx(0.42)
    assert counters["route_state_action_pnl"] == initial_route_buckets
    assert session["route_loss_streaks"] == {"seed-route": 1}
    assert session["last_loss_route"] == "seed-route"
    assert session["state_net_pnl_usdc"] == {"RANGE": pytest.approx(-0.2)}
    assert session["state_throttle_count"] == {"RANGE": 1}
    assert session["state_throttle_deadlines"] == {"RANGE": pytest.approx(123.0)}
    assert len(armed) == 25


@pytest.mark.asyncio
async def test_v1459_adaptive_expired_deadline_does_not_arm_another_run():
    repo = FakeRepo()
    manager = MainnetOneRunManager(_settings(), FakeClient(), repo, FakeTelegramApp())
    await manager.start_adaptive_session()
    created_before = len(repo.created)
    manager._adaptive_session["deadline_at_ms"] = int(manager._adaptive_session["started_at_ms"]) - 1

    result = await manager._arm_adaptive_run()

    assert "72 小時上限" in result
    assert len(repo.created) == created_before
    assert manager._adaptive_session is None
    assert manager._adaptive_last_review["stop_reason"] == "wall_clock_cap"


def _v1458_adaptive_signal(lane: str, state: str, action: str, route: str = "NORMAL") -> str:
    return json.dumps(
        {
            "codex_v1": {
                "lane_code": lane,
                "metrics": {"market_state": state},
            },
            "adaptive": {
                "decision": {
                    "lane_code": lane,
                    "market_state": state,
                    "live_effective_route": route,
                    "live_effective_action": {"action_id": action},
                }
            },
        }
    )


@pytest.mark.asyncio
async def test_v1458_exact_cnl_deep_no_lane_gate_stays_armed_without_order_call():
    repo = FakeRepo()
    manager = MainnetOneRunManager(_settings(), FakeClient(), repo, FakeTelegramApp())
    await manager.start_adaptive_session()
    run = _run(
        status="ARMED",
        params={"mode": "adaptive_continuous", "adaptive": manager._adaptive_metadata(manager._adaptive_session)},
    )
    raw = CodexV1Decision(
        accepted=False,
        version="_codex_v1.4.58",
        baseline="unit",
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
    promoted = CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.58",
        baseline="unit",
        lane="codex_v1_wpr_long",
        lane_code="CNL-WPR-L",
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=2.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="accepted",
        regime="CNL-WPR-L:deep_discount_stable",
        metrics={
            "market_state": "CNL-WPR-L:deep_discount_stable",
            "promotion_source": "no_lane_shadow_reprice_canary",
            "v1455_action": "L_E2_TP8_SL8_T180",
        },
    )

    payload = manager._adaptive_decision_payload(
        run,
        promoted,
        raw_decision=raw,
        features={"setup_started_at_ms": 123_000},
    )

    assert payload["enforcement_applied"] is True
    assert payload["live_effective_route"] == "OBSERVE_ONLY"
    assert await manager._adaptive_gate_before_submit(run, payload) is True
    assert run["status"] == "ARMED"
    gate = [event for event in repo.events if event[1] == "adaptive_live_gate_skipped"]
    assert len(gate) == 1
    assert gate[0][2]["order_api_calls"] == 0
    assert manager._adaptive_session["counters"]["gate_skips"] == 1


def test_v1458_other_routes_are_not_live_enforced():
    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    manager._adaptive_session = {
        "session_id": "session",
        "config_sha": "a" * 64,
        "counters": manager._new_adaptive_counters(),
    }
    run = _run(
        params={
            "mode": "adaptive_continuous",
            "adaptive": {"session_id": "session", "config_sha": "a" * 64},
        }
    )
    raw = CodexV1Decision(
        accepted=False,
        version="_codex_v1.4.58",
        baseline="unit",
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
    for lane, state, action, side in (
        ("CNL-WPR-L", "CNL-WPR-L:discount_mixed", "L_E2_TP8_SL8_T180", "LONG"),
        ("STUP-S", "STUP-S:clean_extension", "S_E2_TP10_SL8_T90", "SHORT"),
    ):
        decision = CodexV1Decision(
            accepted=True,
            version="_codex_v1.4.58",
            baseline="unit",
            lane="unit",
            lane_code=lane,
            strategy="S1_BB_RSI",
            side=side,
            entry_offset_bp=2.0,
            size_mult=1.0,
            notional_mult=1.0,
            requested_notional_usdc=50.0,
            reason="accepted",
            regime=state,
            metrics={
                "market_state": state,
                "promotion_source": "no_lane_shadow_reprice_canary",
                "v1455_action": action,
                "v1455_route": "THIN_SCALP" if lane == "STUP-S" else "NORMAL",
            },
        )
        payload = manager._adaptive_decision_payload(run, decision, raw_decision=raw)
        assert payload["enforcement_applied"] is False
        assert payload["live_effective_route"] == ("THIN_SCALP" if lane == "STUP-S" else "NORMAL")


@pytest.mark.asyncio
async def test_v1458_adaptive_first_late_fill_stops_and_accounts_terminal():
    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    await manager.start_adaptive_session()
    run = _run(
        signal_json=_v1458_adaptive_signal(
            "CNL-WPR-L",
            "CNL-WPR-L:falling_continuation_probe",
            "TP8",
        ),
        params={"mode": "adaptive_continuous", "adaptive": manager._adaptive_metadata(manager._adaptive_session)},
    )

    assert await manager._adaptive_after_terminal(
        run,
        -0.03,
        "ENTRY_LATE_FILL_TTL",
        gross_pnl=-0.02,
        commission=0.01,
    ) is True

    review = manager._adaptive_last_review
    assert manager._adaptive_session is None
    assert review["stop_reason"] == "entry_late_fill_ttl"
    counters = review["counters"]
    assert counters["gross_pnl_usdc"] == pytest.approx(-0.02)
    assert counters["commission_usdc"] == pytest.approx(0.01)
    assert counters["net_pnl_usdc"] == pytest.approx(-0.03)
    bucket = counters["route_state_action_pnl"][
        "CNL-WPR-L|CNL-WPR-L:falling_continuation_probe|TP8"
    ]
    assert bucket["runs"] == 1
    assert bucket["net_pnl_usdc"] == pytest.approx(-0.03)


@pytest.mark.asyncio
async def test_v1458_adaptive_two_consecutive_losses_on_same_live_route_stop():
    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    await manager.start_adaptive_session()
    params = {"mode": "adaptive_continuous", "adaptive": manager._adaptive_metadata(manager._adaptive_session)}
    manager._arm_adaptive_run = lambda initial=False: _async_value("armed")
    signal = _v1458_adaptive_signal("STUP-S", "STUP-S:clean_extension", "E2_TP10", "THIN_SCALP")

    first = _run(run_id="cry3mn_route_loss_1", signal_json=signal, params=params)
    second = _run(run_id="cry3mn_route_loss_2", signal_json=signal, params=params)
    await manager._adaptive_after_terminal(first, -0.05, "SL", gross_pnl=-0.04, commission=0.01)
    assert manager._adaptive_session is not None
    await manager._adaptive_after_terminal(second, -0.06, "SL", gross_pnl=-0.05, commission=0.01)

    assert manager._adaptive_session is None
    assert manager._adaptive_last_review["stop_reason"] == "same_live_route_two_net_losses"


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_v1458_adaptive_different_route_breaks_loss_streak():
    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    await manager.start_adaptive_session()
    params = {"mode": "adaptive_continuous", "adaptive": manager._adaptive_metadata(manager._adaptive_session)}
    manager._arm_adaptive_run = lambda initial=False: _async_value("armed")
    runs = [
        _run(run_id="cry3mn_route_a1", signal_json=_v1458_adaptive_signal("STUP-S", "STUP-S:clean_extension", "E2_TP10", "THIN_SCALP"), params=params),
        _run(run_id="cry3mn_route_b1", signal_json=_v1458_adaptive_signal("CNL-WPR-L", "CNL-WPR-L:falling_continuation_probe", "TP8"), params=params),
        _run(run_id="cry3mn_route_a2", signal_json=_v1458_adaptive_signal("STUP-S", "STUP-S:clean_extension", "E2_TP10", "THIN_SCALP"), params=params),
    ]
    for run in runs:
        await manager._adaptive_after_terminal(run, -0.05, "SL")

    assert manager._adaptive_session is not None
    assert max(manager._adaptive_session["route_loss_streaks"].values()) == 1


@pytest.mark.asyncio
async def test_v1457_adaptive_restart_recovers_protection_without_rearm():
    settings = _settings(mainnet_codex_recovery_enabled=True)
    manager = MainnetOneRunManager(settings, FakeClient(), FakeRepo(), FakeTelegramApp())
    active = _run(
        status="RUNNING",
        params={
            "mode": "adaptive_continuous",
            "adaptive": {
                "mode": "adaptive_continuous",
                "session_id": "adaptive_restart",
                "config_sha": "a" * 64,
                "prior_runtime": {},
            },
        },
    )

    await manager._maybe_rehydrate_adaptive_session(active)

    assert manager._adaptive_session["rearm_enabled"] is False
    assert manager._adaptive_session["stop_requested"] is True
    assert manager._dca_enabled is False
    assert settings.mainnet_effective_entry_notional_usdc == pytest.approx(50.0)
    assert settings.mainnet_effective_max_cumulative_notional_usdc == pytest.approx(50.0)
    await manager._adaptive_after_terminal(active, 0.1, "TP")
    assert manager._adaptive_session is None
    assert manager._adaptive_last_review["stop_reason"] == "restart_recovered_terminal"


@pytest.mark.asyncio
async def test_v1466_adaptive_restart_preserves_durable_rearm_authority():
    manager = MainnetOneRunManager(
        _settings(), FakeClient(), FakeRepo(), FakeTelegramApp()
    )
    now_ms = __import__("time").time_ns() // 1_000_000
    counters = manager._new_adaptive_counters()

    class DurableGuard:
        identity_unsafe = False

        def restored_session(self):
            return {
                "session_id": "adaptive_restart_authorized",
                "started_at_ms": now_ms - 60_000,
                "last_checkpoint_at_ms": now_ms - 1_000,
                "terminal_runs": 3,
                "net_pnl_usdc": 0.05,
                "high_water_net_pnl_usdc": 0.05,
                "counters": counters,
                "disabled_states": set(),
                "route_stats": {},
                "rearm_enabled": True,
                "stop_requested": False,
                "restart_recovered": True,
            }

        async def checkpoint(self, session, *, checkpoint_at_ms):
            return SimpleNamespace(
                continue_live=True, status="ACTIVE", reason=None
            )

    manager._v1459_guard = DurableGuard()
    active = _run(
        status="ARMED",
        params={
            "mode": "adaptive_continuous",
            "adaptive": {
                "mode": "adaptive_continuous",
                "session_id": "adaptive_restart_authorized",
                "started_at_ms": now_ms - 60_000,
                "deadline_at_ms": now_ms + 60_000,
                "config_sha": "a" * 64,
                "prior_runtime": {},
            },
        },
    )
    arm_calls = []

    async def fake_arm(initial=False):
        arm_calls.append(initial)
        return "armed"

    manager._arm_adaptive_run = fake_arm

    assert await manager._maybe_rehydrate_adaptive_session(active) is True
    assert manager._adaptive_session["rearm_enabled"] is True
    assert manager._adaptive_session["stop_requested"] is False

    await manager._adaptive_after_terminal(
        active,
        0.0,
        "signal_timeout",
        paid_closed_fill=False,
    )

    assert arm_calls == [False]
    assert manager._adaptive_session is not None


@pytest.mark.asyncio
async def test_v1466_idle_adaptive_recovery_arms_once_and_respects_db_active():
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(), FakeClient(), repo, FakeTelegramApp()
    )
    await manager.start_adaptive_session()
    active_holder = {"run": None}
    arm_calls = []

    async def get_active_run():
        return active_holder["run"]

    async def fake_arm(initial=False):
        arm_calls.append(initial)
        active_holder["run"] = {
            "run_id": "cry3mn_idle_recovered",
            "status": "ARMED",
        }
        return "armed"

    repo.get_active_run = get_active_run
    manager._arm_adaptive_run = fake_arm

    assert await manager._maybe_resume_idle_adaptive_session() is True
    assert await manager._maybe_resume_idle_adaptive_session() is False
    assert arm_calls == [False]


@pytest.mark.asyncio
async def test_v1466_signal_timeout_advances_before_notification():
    manager = MainnetOneRunManager(
        _settings(mainnet_one_run_signal_timeout_minutes=1),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    order = []
    run = _run(armed_at_ms=0)

    async def fake_expire(current, reason):
        order.append("expire")

    async def fake_advance(current, reason):
        order.append("advance")

    async def fake_notify(message):
        order.append("notify")

    manager._expire_codex_v1_shadow_samples = fake_expire
    manager._advance_loop_after_entry_failure = fake_advance
    manager._notify = fake_notify

    await manager._run_armed(run)

    assert order == ["expire", "advance", "notify"]
    assert manager._repo.completed[0][1:3] == (
        "ENTRY_EXPIRED",
        "signal_timeout",
    )


@pytest.mark.asyncio
async def test_v1457_adaptive_unexpected_stop_keeps_safe_runtime_locked():
    settings = _settings(
        mainnet_equity_cap_usdc=200.0,
        mainnet_initial_notional_usdc=200.0,
        mainnet_max_cumulative_notional_usdc=800.0,
        mainnet_recovery_enabled=True,
        mainnet_codex_recovery_enabled=True,
        mainnet_recovery_steps=3,
    )
    manager = MainnetOneRunManager(settings, FakeClient(), FakeRepo(), FakeTelegramApp())
    manager._dca_enabled = True
    await manager.start_adaptive_session()

    await manager._stop_adaptive_session(None, "unexpected_test", unexpected=True)

    assert manager._adaptive_session is None
    assert settings.mainnet_effective_entry_notional_usdc == pytest.approx(50.0)
    assert settings.mainnet_effective_max_cumulative_notional_usdc == pytest.approx(50.0)
    assert settings.mainnet_recovery_steps == 0
    assert settings.mainnet_recovery_enabled is False
    assert manager._dca_enabled is False
    assert manager._adaptive_last_review["stop_reason"] == "unexpected_test"
@pytest.mark.asyncio
async def test_v1457_adaptive_terminal_hook_is_concurrently_idempotent():
    import asyncio

    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    await manager.start_adaptive_session()
    run = _run(params={"mode": "adaptive_continuous", "adaptive": manager._adaptive_metadata(manager._adaptive_session)})
    arm_calls = []

    async def fake_arm(initial=False):
        await asyncio.sleep(0)
        arm_calls.append(initial)
        return "armed"

    manager._arm_adaptive_run = fake_arm
    results = await asyncio.gather(
        manager._adaptive_after_terminal(run, 0.25, "TP"),
        manager._adaptive_after_terminal(run, 0.25, "TP"),
    )

    assert results == [True, True]
    assert manager._adaptive_session["terminal_runs"] == 1
    assert manager._adaptive_session["net_pnl_usdc"] == pytest.approx(0.25)
    assert arm_calls == [False]


@pytest.mark.asyncio
async def test_v1457_adaptive_create_failure_restores_prior_runtime():
    class CreateFailRepo(FakeRepo):
        async def create_run(self, run):
            raise RuntimeError("create failed")

    settings = _settings(
        mainnet_equity_cap_usdc=200.0,
        mainnet_initial_notional_usdc=200.0,
        mainnet_max_cumulative_notional_usdc=800.0,
        mainnet_recovery_steps=3,
    )
    manager = MainnetOneRunManager(settings, FakeClient(), CreateFailRepo(), FakeTelegramApp())
    manager._dca_enabled = True

    result = await manager.start_adaptive_session()

    assert "建立失敗" in result
    assert manager._adaptive_session is None
    assert settings.mainnet_effective_entry_notional_usdc == pytest.approx(200.0)
    assert settings.mainnet_effective_max_cumulative_notional_usdc == pytest.approx(800.0)
    assert settings.mainnet_recovery_steps == 3
    assert manager._dca_enabled is True


@pytest.mark.asyncio
async def test_v1457_adaptive_event_log_failure_terminalizes_and_keeps_safe_runtime():
    class LogFailRepo(FakeRepo):
        async def log_event(self, run_id, event_type, details):
            raise RuntimeError("log failed")

    settings = _settings(
        mainnet_equity_cap_usdc=200.0,
        mainnet_initial_notional_usdc=200.0,
        mainnet_max_cumulative_notional_usdc=800.0,
        mainnet_recovery_steps=3,
    )
    repo = LogFailRepo()
    manager = MainnetOneRunManager(settings, FakeClient(), repo, FakeTelegramApp())
    manager._dca_enabled = True

    result = await manager.start_adaptive_session()

    assert "建立失敗" in result
    assert manager._adaptive_session is None
    assert repo.completed[-1][1:3] == ("FAILED", "adaptive_arm_persistence_failed")
    assert settings.mainnet_effective_entry_notional_usdc == pytest.approx(50.0)
    assert settings.mainnet_effective_max_cumulative_notional_usdc == pytest.approx(50.0)
    assert settings.mainnet_recovery_steps == 0
    assert manager._dca_enabled is False

class _V1459TerminalRuntime:
    permits_order_mutation = False

    def __init__(self):
        self.flags = SimpleNamespace(record_reconciliation=True)
        self.reconciliation_calls = []

    async def record_reconciliation(self, **kwargs):
        from src.gridbot.mainnet.run_reconciler import reconcile_run

        self.reconciliation_calls.append(kwargs)
        result = reconcile_run(
            kwargs["trades"],
            kwargs["incomes"],
            require_closed_run=True,
        )
        return result, SimpleNamespace(
            attempted=True,
            inserted=True,
            status=result.reconciliation_status,
            reason=result.completeness_reason,
        )


@pytest.mark.asyncio
async def test_v1459_terminal_reconciliation_flags_off_adds_zero_exchange_io():
    client = FakeClient()
    manager = MainnetOneRunManager(
        _settings(), client, FakeRepo(), FakeTelegramApp()
    )
    run = _run(params={"mode": "adaptive_continuous"}, armed_at_ms=1)

    result = await manager._v1459_reconcile_terminal_run(run)

    assert result is None
    assert client.all_orders_calls == []
    assert client.user_trades_calls == []
    assert client.income_history_calls == []


@pytest.mark.asyncio
async def test_v1459_finish_flat_run_counts_only_exact_funding_adjusted_net():
    from src.gridbot.binance.models import FuturesTrade, IncomeRecord

    client = FakeClient()
    client.all_orders = [
        {
            "orderId": 501, "clientOrderId": "cry3mn_test_entry",
            "origQty": "0.500", "status": "FILLED", "updateTime": 10,
        },
        {
            "orderId": 502, "clientOrderId": "cry3mn_test_tp1",
            "origQty": "0.500", "status": "FILLED", "updateTime": 20,
        },
    ]
    client.user_trades = [
        FuturesTrade(
            1, 501, "ETHUSDC", "BUY", 100.0, 0.5, 50.0,
            0.0, 0.01, "USDC", 10, "BOTH", True, True,
        ),
        FuturesTrade(
            2, 502, "ETHUSDC", "SELL", 100.2, 0.5, 50.1,
            0.10, 0.02, "USDC", 20, "BOTH", False, False,
        ),
    ]
    client.income_history = [
        IncomeRecord(
            701, "ETHUSDC", "FUNDING_FEE", -0.005, "USDC", 15,
            "FUNDING_FEE", "",
        )
    ]
    runtime = _V1459TerminalRuntime()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(), client, repo, FakeTelegramApp(), observation_runtime=runtime
    )
    terminal_calls = []

    async def capture_terminal(run, net_pnl, reason, **kwargs):
        terminal_calls.append((run, net_pnl, reason, kwargs))
        return True

    manager._adaptive_after_terminal = capture_terminal
    run = _run(
        exit_reason="TP",
        armed_at_ms=1,
        params={"mode": "adaptive_continuous"},
        cumulative_notional_usdc=50.0,
    )

    await manager._finish_flat_run(run, "flat_detected")

    assert len(runtime.reconciliation_calls) == 1
    assert client.income_history_calls == [
        ("FUNDING_FEE", "ETHUSDC", 10, 20, 1000)
    ]
    assert len(terminal_calls) == 1
    _, exact_net, _, kwargs = terminal_calls[0]
    assert exact_net == pytest.approx(0.065)
    assert kwargs["gross_pnl"] == pytest.approx(0.10)
    assert kwargs["commission"] == pytest.approx(0.03)
    assert kwargs["funding"] == pytest.approx(0.005)
    completed = [
        details for _, event_type, details in repo.events
        if event_type == "completed"
    ][-1]
    assert completed["reconciliation_status"] == "COMPLETE"
    assert completed["eligible_for_wr_ev"] is True
    assert completed["funding_usdc"] == pytest.approx(0.005)
    assert completed["net_pnl"] == pytest.approx(0.065)


@pytest.mark.asyncio
async def test_v1459_incomplete_terminal_reconciliation_pauses_without_counting():
    from src.gridbot.binance.models import FuturesTrade

    client = FakeClient()
    client.all_orders = [
        {
            "orderId": 601, "clientOrderId": "cry3mn_test_entry",
            "origQty": "0.500", "status": "FILLED", "updateTime": 10,
        }
    ]
    client.user_trades = [
        FuturesTrade(
            1, 601, "ETHUSDC", "BUY", 100.0, 0.5, 50.0,
            0.0, 0.01, "USDC", 10, "BOTH", True, True,
        )
    ]
    runtime = _V1459TerminalRuntime()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(), client, repo, FakeTelegramApp(), observation_runtime=runtime
    )
    terminal_calls = []

    async def capture_terminal(*args, **kwargs):
        terminal_calls.append((args, kwargs))
        return True

    manager._adaptive_after_terminal = capture_terminal
    run = _run(
        exit_reason="SL",
        armed_at_ms=1,
        params={"mode": "adaptive_continuous"},
    )

    await manager._finish_flat_run(run, "flat_detected")

    assert len(runtime.reconciliation_calls) == 1
    assert manager._v1459_reconciliation_hook.entry_paused is True
    assert terminal_calls == []
    assert client.income_history_calls == []
    assert repo.completed[-1][0:3] == ("cry3mn_test", "COMPLETED", "SL")
    assert any(
        event_type == "v1459_reconciliation_paused"
        for _, event_type, _ in repo.events
    )
    completed = [
        details for _, event_type, details in repo.events
        if event_type == "completed"
    ][-1]
    assert completed["reconciliation_status"] == "DATA_INCOMPLETE"
    assert completed["eligible_for_wr_ev"] is False


class _V1459FakeRuntime:
    permits_order_mutation = False
    durable_session = None

    def __init__(
        self,
        *,
        checkpoint_status="ACTIVE",
        checkpoint_reason=None,
        checkpoint_error=None,
        opportunity_error=None,
        durable_session=None,
        retire_error=None,
    ):
        self.checkpoint_status = checkpoint_status
        self.checkpoint_reason = checkpoint_reason
        self.checkpoint_error = checkpoint_error
        self.opportunity_error = opportunity_error
        self.durable_session = durable_session
        self.retire_error = retire_error
        self.checkpoints = []
        self.opportunities = []
        self.retirements = []

    async def checkpoint_session(self, session, *, checkpoint_at_ms):
        self.checkpoints.append((dict(session), checkpoint_at_ms))
        if self.checkpoint_error is not None:
            raise self.checkpoint_error
        return SimpleNamespace(
            attempted=True,
            inserted=True,
            status=self.checkpoint_status,
            reason=self.checkpoint_reason,
        )

    async def record_opportunity(self, **kwargs):
        self.opportunities.append(kwargs)
        if self.opportunity_error is not None:
            raise self.opportunity_error
        return SimpleNamespace(
            attempted=True,
            inserted=True,
            status="ACCEPTED_OBSERVED",
            reason=None,
        )

    async def retire_durable_session(self, *, checkpoint_at_ms, stop_reason):
        self.retirements.append((checkpoint_at_ms, stop_reason))
        if self.retire_error is not None:
            raise self.retire_error
        return SimpleNamespace(
            attempted=bool(self.durable_session),
            inserted=bool(self.durable_session),
            status="STOPPED" if self.durable_session else "NO_DURABLE_SESSION",
            reason=None,
        )


def _v1459_stup_decision():
    return CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.58",
        baseline="unit",
        lane="codex_v1_stup_short",
        lane_code="STUP-S",
        strategy="S2_SuperTrend",
        side="SHORT",
        entry_offset_bp=2.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="accepted",
        regime="STUP-S:clean_extension",
        metrics={
            "market_state": "STUP-S:clean_extension",
            "v1455_action": "S_E2_TP10_SL8_T90",
            "v1455_route": "THIN_SCALP",
        },
    )


@pytest.mark.asyncio
async def test_v1459_initial_checkpoint_failure_creates_no_run_or_entry_path():
    runtime = _V1459FakeRuntime(checkpoint_error=RuntimeError("db unavailable"))
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(), FakeClient(), repo, FakeTelegramApp(), observation_runtime=runtime
    )

    result = await manager.start_adaptive_session()

    assert "identity/evidence" in result
    assert repo.created == []
    assert manager._adaptive_session is None
    assert manager._v1459_guard.entry_paused is True


@pytest.mark.asyncio
async def test_v1459_start_retires_orphaned_durable_session_before_arming() -> None:
    runtime = _V1459FakeRuntime(durable_session={"session_id": "old-session"})
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(), FakeClient(), repo, FakeTelegramApp(), observation_runtime=runtime
    )

    result = await manager.start_adaptive_session()

    assert "Adaptive continuous session" in result
    assert len(runtime.retirements) == 1
    assert runtime.retirements[0][1] == "restart_orphaned_no_active_run"
    assert len(repo.created) == 1


@pytest.mark.asyncio
async def test_v1459_opportunity_contract_is_persisted_before_entry_continues():
    runtime = _V1459FakeRuntime()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(), FakeClient(), repo, FakeTelegramApp(), observation_runtime=runtime
    )
    await manager.start_adaptive_session()
    run = repo.created[-1]
    decision_at_floor = int(__import__("time").time() * 1000)
    payload = manager._adaptive_decision_payload(
        run,
        _v1459_stup_decision(),
        features={
            "rng15": 24.0,
            "setup_started_at_ms": decision_at_floor - 60_000,
            "feature_age_seconds": 2.0,
            "future_pnl_usdc": 99.0,
        },
    )

    assert await manager._adaptive_gate_before_submit(run, payload) is False
    write = runtime.opportunities[-1]
    assert write["source_run_id"] == run["run_id"]
    assert write["opportunity_bucket"] == (decision_at_floor - 60_000) // 60_000
    assert write["decision_at_ms"] <= write["observed_at_ms"]
    assert write["features"]["rng15"] == 24.0
    assert "future_pnl_usdc" not in write["features"]
    assert write["feature_timestamps"]["setup_started_at_ms"] == decision_at_floor - 60_000
    assert manager._adaptive_session["counters"]["opportunities"] == 1


@pytest.mark.asyncio
async def test_v1459_opportunity_write_failure_pauses_before_submit():
    runtime = _V1459FakeRuntime(opportunity_error=RuntimeError("db unavailable"))
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(), FakeClient(), repo, FakeTelegramApp(), observation_runtime=runtime
    )
    await manager.start_adaptive_session()
    run = repo.created[-1]
    payload = manager._adaptive_decision_payload(
        run, _v1459_stup_decision(), features={"rng15": 24.0}
    )

    assert await manager._adaptive_gate_before_submit(run, payload) is True
    assert manager._adaptive_session["stop_requested"] is True
    assert manager._adaptive_session["rearm_enabled"] is False
    assert manager._adaptive_session["counters"]["opportunities"] == 0


@pytest.mark.asyncio
async def test_v1459_cycle_modes_split_identity_from_owned_risk_reduction():
    persistence = _V1459FakeRuntime(checkpoint_error=RuntimeError("db unavailable"))
    manager = MainnetOneRunManager(
        _settings(), FakeClient(), FakeRepo(), FakeTelegramApp(), observation_runtime=persistence
    )
    await manager._v1459_guard.checkpoint({"session_id": "s"}, checkpoint_at_ms=1)
    adaptive = {"mode": "adaptive_continuous", "session_id": "s"}
    owned = _run(run_id="cry3mn_123", status="RUNNING", params={"mode": "adaptive_continuous", "adaptive": adaptive})
    armed = dict(owned, status="ARMED")
    foreign = dict(owned, run_id="foreign_123")
    assert manager._v1459_cycle_mode(owned) == "RISK_REDUCTION_ONLY"
    assert manager._v1459_cycle_mode(armed) == "ENTRY_PAUSED"
    assert manager._v1459_cycle_mode(foreign) == "ENTRY_PAUSED"

    mismatch = _V1459FakeRuntime(
        checkpoint_status="PAUSED_REQUIRES_ACK",
        checkpoint_reason="account_fingerprint_mismatch",
    )
    unsafe = MainnetOneRunManager(
        _settings(), FakeClient(), FakeRepo(), FakeTelegramApp(), observation_runtime=mismatch
    )
    await unsafe._v1459_guard.checkpoint({"session_id": "s"}, checkpoint_at_ms=1)
    assert unsafe._v1459_cycle_mode(owned) == "IDENTITY_UNSAFE"


@pytest.mark.asyncio
async def test_v1459_run_cycle_manages_owned_position_on_db_failure_but_not_identity_mismatch():
    adaptive = {
        "mode": "adaptive_continuous",
        "session_id": "adaptive_restart",
        "config_sha": "a" * 64,
        "prior_runtime": {},
    }
    active = _run(
        run_id="cry3mn_123",
        status="RUNNING",
        params={"mode": "adaptive_continuous", "adaptive": adaptive},
    )

    class ActiveRepo(FakeRepo):
        async def get_active_run(self):
            return active

    async def noop(*args, **kwargs):
        return None

    persistence = _V1459FakeRuntime(checkpoint_error=RuntimeError("db unavailable"))
    manager = MainnetOneRunManager(
        _settings(),
        FakeClient(),
        ActiveRepo(),
        FakeTelegramApp(),
        observation_runtime=persistence,
    )
    managed = []

    async def manage(run):
        managed.append(run["run_id"])

    manager._ensure_runtime_config_loaded = noop
    manager._adaptive_stup_fill_shadow_tracker.update = noop
    manager._maybe_rehydrate_loop_state_from_active_run = noop
    manager._rehydrate_codex_v132_tp_policy_samples = noop
    manager._run_running = manage
    await manager._v1459_guard.checkpoint({"session_id": "s"}, checkpoint_at_ms=1)

    await manager.run_cycle()

    assert managed == ["cry3mn_123"]
    assert manager._v1459_cycle_mode(active) == "RISK_REDUCTION_ONLY"

    mismatch = _V1459FakeRuntime(
        checkpoint_status="PAUSED_REQUIRES_ACK",
        checkpoint_reason="account_fingerprint_mismatch",
    )
    unsafe = MainnetOneRunManager(
        _settings(),
        FakeClient(),
        ActiveRepo(),
        FakeTelegramApp(),
        observation_runtime=mismatch,
    )
    unsafe_managed = []
    unsafe._ensure_runtime_config_loaded = noop
    unsafe._adaptive_stup_fill_shadow_tracker.update = noop
    unsafe._maybe_rehydrate_loop_state_from_active_run = noop
    unsafe._rehydrate_codex_v132_tp_policy_samples = noop

    async def unsafe_manage(run):
        unsafe_managed.append(run["run_id"])

    unsafe._run_running = unsafe_manage
    await unsafe._v1459_guard.checkpoint({"session_id": "s"}, checkpoint_at_ms=1)

    await unsafe.run_cycle()

    assert unsafe_managed == []
    assert unsafe._v1459_cycle_mode(active) == "IDENTITY_UNSAFE"


@pytest.mark.asyncio
async def test_v1457_adaptive_double_start_and_finite_arm_are_serialized():
    import asyncio

    repo = FakeRepo()
    manager = MainnetOneRunManager(_settings(), FakeClient(), repo, FakeTelegramApp())

    first, second = await asyncio.gather(
        manager.start_adaptive_session(),
        manager.start_adaptive_session(),
    )

    assert len(repo.created) == 1
    assert sorted(("已在進行中" in first, "已在進行中" in second)) == [False, True]
    finite = await manager.arm(loop_count=3)
    assert "Adaptive session" in finite
    assert len(repo.created) == 1


@pytest.mark.asyncio
async def test_v1467_idle_evidence_maintenance_does_not_block_cycle():
    import asyncio

    manager = MainnetOneRunManager(_settings(), FakeClient(), FakeRepo(), FakeTelegramApp())
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_maintenance():
        started.set()
        await release.wait()

    manager._run_idle_maintenance = slow_maintenance
    await manager.run_cycle()
    await asyncio.wait_for(started.wait(), timeout=0.2)
    assert manager._idle_maintenance_task is not None
    assert not manager._idle_maintenance_task.done()

    release.set()
    await manager._idle_maintenance_task


@pytest.mark.asyncio
async def test_v1467_db_capacity_guard_alerts_once_without_mutating_evidence(tmp_path):
    db_path = tmp_path / "gridbot_testnet.db"
    db_path.write_bytes(b"evidence")
    repo = FakeRepo()
    repo._db = SimpleNamespace(_db_path=str(db_path))
    telegram = FakeTelegramApp()
    manager = MainnetOneRunManager(
        _settings(
            mainnet_db_capacity_warn_free_mb=10**9,
            mainnet_db_capacity_critical_free_mb=1,
        ),
        FakeClient(),
        repo,
        telegram,
    )

    await manager._observe_db_capacity()
    assert len(telegram.bot.messages) == 1
    assert "Adaptive DB 容量 WARN" in telegram.bot.messages[0]["text"]
    assert "未刪除證據" in telegram.bot.messages[0]["text"]

    manager._db_capacity_next_check_ms = 0
    await manager._observe_db_capacity()
    assert len(telegram.bot.messages) == 1
