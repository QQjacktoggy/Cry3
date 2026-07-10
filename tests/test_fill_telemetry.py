import json

import pytest

from src.gridbot.mainnet.fill_telemetry import emit_fill_v1_events


def trade(trade_id=10, order_id=20, *, time_ms=1234, qty=0.1, price=100.0):
    return type(
        "Trade",
        (),
        {
            "trade_id": trade_id,
            "order_id": order_id,
            "symbol": "ETHUSDC",
            "side": "SELL",
            "position_side": "BOTH",
            "price": price,
            "qty": qty,
            "quote_qty": price * qty,
            "realized_pnl": 0.25,
            "commission": 0.01,
            "commission_asset": "USDC",
            "time_ms": time_ms,
            "is_maker": True,
        },
    )()


class FakeRepo:
    def __init__(self, *, use_by_types=True, fail_reads=False):
        self.events = []
        self.use_by_types = use_by_types
        self.fail_reads = fail_reads

    async def log_event(self, run_id, event_type, details):
        self.events.append((run_id, event_type, details))

    async def get_events(self, run_id, limit=30):
        if self.fail_reads:
            raise RuntimeError("event read unavailable")
        rows = [
            {
                "run_id": rid,
                "event_type": event_type,
                "details_json": json.dumps(details),
            }
            for rid, event_type, details in reversed(self.events)
            if rid == run_id
        ]
        return rows[:limit]

    async def get_events_by_types(self, run_id, event_types, limit=30):
        if not self.use_by_types:
            raise AttributeError("disabled in this fake")
        if self.fail_reads:
            raise RuntimeError("event read unavailable")
        allowed = set(event_types)
        return [
            row
            for row in await self.get_events(run_id, limit=limit)
            if row["event_type"] in allowed
        ]


class GetEventsOnlyRepo(FakeRepo):
    get_events_by_types = None


class WriteOnlyRepo:
    def __init__(self):
        self.events = []

    async def log_event(self, run_id, event_type, details):
        self.events.append((run_id, event_type, details))


class FakeTrades:
    def __init__(self, trades=None, fail=False):
        self.trades = list(trades or [])
        self.fail = fail

    async def get_trades(self, symbol, since_ms, grid_only, limit):
        if self.fail:
            raise RuntimeError("db unavailable")
        return self.trades


class FakeClient:
    def __init__(self, trades=None, orders=None, *, fail_trades=False, fail_orders=False):
        self.trades = list(trades or [])
        self.orders = list(orders or [])
        self.fail_trades = fail_trades
        self.fail_orders = fail_orders

    async def get_user_trades(self, symbol, start_time, limit):
        if self.fail_trades:
            raise RuntimeError("api unavailable")
        return self.trades

    async def get_all_orders(self, symbol, start_time, limit):
        if self.fail_orders:
            raise RuntimeError("orders unavailable")
        return self.orders


RUN = {"run_id": "run_1", "symbol": "ETHUSDC", "armed_at_ms": 1000}


@pytest.mark.asyncio
async def test_emit_fill_v1_is_strategy_neutral_and_deduplicates_sources():
    repo = FakeRepo()
    db_trade = {
        "trade_id": 10,
        "order_id": 20,
        "symbol": "ETHUSDC",
        "side": "SELL",
        "price": 100.0,
        "qty": 0.1,
        "time_ms": 1234,
    }
    count = await emit_fill_v1_events(
        repo=repo,
        client=FakeClient([trade()], [{"orderId": 20, "clientOrderId": "run_tp1"}]),
        trade_repo=FakeTrades([db_trade]),
        run=RUN,
    )
    assert count == 1
    _, event_type, details = repo.events[0]
    assert event_type == "fill_v1"
    assert details["fill_key"] == "10:20"
    assert details["role"] == "partial_exit"
    assert details["liquidity"] == "maker"


@pytest.mark.asyncio
async def test_repeated_call_and_restart_rehydrate_are_idempotent():
    repo = FakeRepo()
    client = FakeClient([trade()], [{"orderId": 20, "clientOrderId": "run_tp1"}])
    assert await emit_fill_v1_events(repo=repo, client=client, trade_repo=None, run=RUN) == 1
    assert await emit_fill_v1_events(repo=repo, client=client, trade_repo=None, run=RUN) == 0

    restarted_repo = FakeRepo()
    restarted_repo.events = list(repo.events)
    assert await emit_fill_v1_events(
        repo=restarted_repo, client=client, trade_repo=None, run=RUN
    ) == 0
    assert len(restarted_repo.events) == 1


@pytest.mark.asyncio
async def test_incremental_call_emits_only_new_partial_fill():
    repo = FakeRepo()
    client = FakeClient([trade(10, 20, time_ms=1234, qty=0.1)])
    assert await emit_fill_v1_events(repo=repo, client=client, trade_repo=None, run=RUN) == 1
    client.trades.append(trade(11, 20, time_ms=1240, qty=0.2))
    assert await emit_fill_v1_events(repo=repo, client=client, trade_repo=None, run=RUN) == 1
    assert [event[2]["fill_key"] for event in repo.events] == ["10:20", "11:20"]


@pytest.mark.asyncio
async def test_identity_poor_partial_fills_get_stable_distinct_keys():
    repo = FakeRepo()
    client = FakeClient(
        [trade(0, 0, time_ms=1234, qty=0.1), trade(0, 0, time_ms=1235, qty=0.2)]
    )
    assert await emit_fill_v1_events(repo=repo, client=client, trade_repo=None, run=RUN) == 2
    keys = [event[2]["fill_key"] for event in repo.events]
    assert len(set(keys)) == 2
    assert all(key.startswith("fallback:") for key in keys)
    assert await emit_fill_v1_events(repo=repo, client=client, trade_repo=None, run=RUN) == 0


@pytest.mark.asyncio
async def test_unknown_or_algo_order_identity_still_emits_neutral_fill():
    repo = FakeRepo()
    count = await emit_fill_v1_events(
        repo=repo,
        client=FakeClient([trade()], fail_orders=True),
        trade_repo=None,
        run=RUN,
    )
    assert count == 1
    details = repo.events[0][2]
    assert details["client_order_id"] == ""
    assert details["role"] == "unknown_exchange_fill"


@pytest.mark.asyncio
async def test_get_events_fallback_reads_persisted_details_json():
    repo = GetEventsOnlyRepo()
    client = FakeClient([trade()])
    assert await emit_fill_v1_events(repo=repo, client=client, trade_repo=None, run=RUN) == 1
    assert await emit_fill_v1_events(repo=repo, client=client, trade_repo=None, run=RUN) == 0


@pytest.mark.asyncio
async def test_legacy_event_without_fill_key_is_deduplicated():
    repo = FakeRepo()
    details = {
        "trade_id": 10,
        "order_id": 20,
        "symbol": "ETHUSDC",
        "side": "SELL",
        "price": 100.0,
        "qty": 0.1,
        "time_ms": 1234,
    }
    repo.events.append(("run_1", "fill_v1", details))
    assert await emit_fill_v1_events(
        repo=repo, client=FakeClient([trade()]), trade_repo=None, run=RUN
    ) == 0


@pytest.mark.asyncio
async def test_missing_event_query_api_is_safe_and_dedupes_within_call():
    repo = WriteOnlyRepo()
    duplicate = trade()
    assert await emit_fill_v1_events(
        repo=repo,
        client=FakeClient([duplicate, duplicate]),
        trade_repo=None,
        run=RUN,
    ) == 1


@pytest.mark.asyncio
async def test_failed_event_query_is_safe_and_dedupes_within_call():
    repo = FakeRepo(fail_reads=True)
    duplicate = trade()
    assert await emit_fill_v1_events(
        repo=repo,
        client=FakeClient([duplicate, duplicate]),
        trade_repo=None,
        run=RUN,
    ) == 1


@pytest.mark.asyncio
async def test_api_trade_failure_uses_db_fallback_and_db_failure_uses_api():
    db_trade = {
        "trade_id": 10,
        "order_id": 20,
        "symbol": "ETHUSDC",
        "side": "SELL",
        "price": 100.0,
        "qty": 0.1,
        "time_ms": 1234,
    }
    repo = FakeRepo()
    assert await emit_fill_v1_events(
        repo=repo,
        client=FakeClient(fail_trades=True),
        trade_repo=FakeTrades([db_trade]),
        run=RUN,
    ) == 1

    other_run = dict(RUN, run_id="run_2")
    assert await emit_fill_v1_events(
        repo=repo,
        client=FakeClient([trade()]),
        trade_repo=FakeTrades(fail=True),
        run=other_run,
    ) == 1


@pytest.mark.asyncio
async def test_missing_armed_time_is_noop():
    repo = FakeRepo()
    assert await emit_fill_v1_events(
        repo=repo,
        client=FakeClient([trade()]),
        trade_repo=None,
        run={"run_id": "run_1", "symbol": "ETHUSDC"},
    ) == 0
    assert repo.events == []
