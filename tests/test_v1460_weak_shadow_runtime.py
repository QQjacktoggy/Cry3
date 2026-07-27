from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.gridbot.mainnet import v1460_weak_shadow_runtime as runtime
from src.gridbot.mainnet.v1460_weak_shadow_runtime import (
    OUTCOME_EVENT,
    STARTED_EVENT,
    V1460WeakStupShadowTracker,
    find_maker_fill,
    normalize_agg_trades,
)


class _Repo:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def log_event(self, run_id: str, event_type: str, details: dict) -> None:
        self.events.append((run_id, event_type, details))


class _AggTradeClient:
    def __init__(self, rows: list[dict] | None = None, *, fail_times: int = 0) -> None:
        self.rows = list(rows or [])
        self.fail_times = fail_times
        self.calls: list[tuple[str, dict]] = []
        self.order_calls: list[str] = []

    async def get_agg_trades(self, symbol: str, **kwargs):
        self.calls.append((symbol, dict(kwargs)))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("temporary public aggTrade failure")
        from_id = kwargs.get("from_id")
        start_time = kwargs.get("start_time")
        end_time = kwargs.get("end_time")
        limit = int(kwargs["limit"])
        rows = list(self.rows)
        if from_id is not None:
            rows = [row for row in rows if int(row["a"]) >= int(from_id)]
        else:
            if start_time is not None:
                rows = [row for row in rows if int(row["T"]) >= int(start_time)]
            if end_time is not None:
                rows = [row for row in rows if int(row["T"]) <= int(end_time)]
        rows.sort(key=lambda row: int(row["a"]))
        return rows[:limit]

    async def create_order(self, *args, **kwargs):
        self.order_calls.append("create_order")
        raise AssertionError("read-only tracker called create_order")

    async def cancel_order(self, *args, **kwargs):
        self.order_calls.append("cancel_order")
        raise AssertionError("read-only tracker called cancel_order")

    async def close_position_market(self, *args, **kwargs):
        self.order_calls.append("close_position_market")
        raise AssertionError("read-only tracker called close_position_market")


def _settings(**overrides) -> SimpleNamespace:
    values = {
        "mainnet_codex_v1460_weak_shadow_maker_fee_rate": 0.0002,
        "mainnet_codex_v1460_weak_shadow_taker_fee_rate": 0.0004,
        "mainnet_codex_v1460_weak_shadow_max_pages": 10,
        "mainnet_codex_v1460_weak_shadow_page_limit": 1_000,
        "mainnet_codex_v1460_weak_shadow_max_fetch_failures": 2,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _tracker(client: _AggTradeClient, *, settings=None):
    repo = _Repo()
    outcomes: list[dict] = []
    samples: dict[str, dict] = {}
    groups: set[str] = set()
    tracker = V1460WeakStupShadowTracker(
        client=client,
        repo=repo,
        settings=settings or _settings(),
        on_outcome=outcomes.append,
        samples=samples,
        started_groups=groups,
        lock=asyncio.Lock(),
    )
    return tracker, repo, outcomes, samples, groups


async def _start(tracker: V1460WeakStupShadowTracker, **overrides) -> bool:
    values = {
        "run_id": "run-1",
        "session_id": "session-1",
        "opportunity_id": "opportunity-1",
        "symbol": "ETHUSDC",
        "side": "BUY",
        "market_state": "near_vwap_flat",
        "entry_limit_price": 100.0,
        "notional_usdc": 50.0,
        "tp_price": 101.0,
        "sl_price": 99.0,
        "entry_submitted_at_ms": 1_000,
        "entry_deadline_ms": 2_000,
        "outcome_deadline_ms": 3_000,
    }
    values.update(overrides)
    return await tracker.start(**values)


def _outcome(repo: _Repo) -> dict:
    matches = [details for _run, event, details in repo.events if event == OUTCOME_EVENT]
    assert len(matches) == 1
    return matches[0]


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(runtime, "_now_ms", lambda: 4_000)


def test_buy_and_sell_maker_fill_semantics_are_inclusive_and_side_correct() -> None:
    trades = normalize_agg_trades(
        [
            {"a": 4, "T": 1_400, "p": "100.20"},
            {"a": 2, "T": 1_200, "p": "99.90"},
            {"a": 1, "T": 900, "p": "99.00"},
            {"a": 3, "T": 1_300, "p": "100.00"},
        ]
    )

    buy = find_maker_fill(
        trades,
        side="BUY",
        entry_limit_price=100.0,
        entry_submitted_at_ms=1_000,
        entry_deadline_ms=2_000,
    )
    sell = find_maker_fill(
        trades,
        side="SELL",
        entry_limit_price=100.1,
        entry_submitted_at_ms=1_000,
        entry_deadline_ms=2_000,
    )

    assert buy is not None and buy.agg_trade_id == 2
    assert sell is not None and sell.agg_trade_id == 4


def test_aggtrades_are_sorted_and_identical_ids_are_deduplicated() -> None:
    rows = [
        {"a": 2, "T": 1_200, "p": "100.2"},
        {"a": 1, "T": 1_100, "p": "100.1"},
        {"a": 2, "T": 1_200, "p": "100.2"},
    ]

    trades = normalize_agg_trades(rows)

    assert [trade.agg_trade_id for trade in trades] == [1, 2]
    with pytest.raises(ValueError, match="conflicting aggTrade id"):
        normalize_agg_trades(rows + [{"a": 2, "T": 1_201, "p": "100.2"}])


@pytest.mark.asyncio
async def test_tp_first_persists_complete_cost_aware_event_and_callback() -> None:
    client = _AggTradeClient(
        [
            {"a": 1, "T": 1_050, "p": "100.20"},
            {"a": 2, "T": 1_100, "p": "99.90"},
            {"a": 3, "T": 2_100, "p": "100.50"},
            {"a": 4, "T": 2_200, "p": "101.00"},
        ]
    )
    tracker, repo, outcomes, samples, groups = _tracker(client)

    assert await _start(tracker, market_state="STUP-S:near_vwap_flat") is True
    await tracker.update()

    assert len(repo.events) == 2
    started = repo.events[0]
    assert started[1] == STARTED_EVENT
    assert started[2]["version"] == "v1.4.60B"
    assert started[2]["lane_code"] == "STUP-S"
    assert started[2]["market_state"] == "STUP-S:near_vwap_flat"
    assert started[2]["shadow_only"] is True
    assert started[2]["places_real_order"] is False
    assert started[2]["opportunity_key"] == "session-1:opportunity-1"

    result = _outcome(repo)
    assert result["first_touch_result"] == "TP_FIRST"
    assert result["evaluable"] is True
    assert result["filled"] is True
    assert result["fill_trade_id"] == 2
    assert result["touch_trade_id"] == 4
    assert result["exit_liquidity"] == "MAKER"
    assert result["gross_pnl_usdc"] == pytest.approx(0.5)
    assert result["entry_fee_usdc"] == pytest.approx(0.01)
    assert result["exit_fee_usdc"] == pytest.approx(0.0101)
    assert result["net_pnl_usdc"] == pytest.approx(0.4799)
    assert result["ev_contribution_usdc"] == pytest.approx(0.4799)
    assert result["mfe_bp"] == pytest.approx(100.0)
    assert result["mae_bp"] == 0.0
    assert result["duration_ms"] == 1_100
    assert result["data_quality"]["complete"] is True
    assert result["data_quality"]["coverage_through_ms"] == 3_000
    assert outcomes == [result]
    assert samples == {}
    assert groups == {"session-1:opportunity-1"}


@pytest.mark.asyncio
async def test_sl_first_uses_taker_exit_fee_and_side_correct_net() -> None:
    client = _AggTradeClient(
        [
            {"a": 1, "T": 1_100, "p": "100.00"},
            {"a": 2, "T": 2_100, "p": "99.50"},
            {"a": 3, "T": 2_200, "p": "99.00"},
        ]
    )
    tracker, repo, outcomes, _samples, _groups = _tracker(client)

    await _start(tracker)
    await tracker.update()

    result = _outcome(repo)
    assert result["first_touch_result"] == "SL_FIRST"
    assert result["exit_liquidity"] == "TAKER"
    assert result["gross_pnl_usdc"] == pytest.approx(-0.5)
    assert result["entry_fee_usdc"] == pytest.approx(0.01)
    assert result["exit_fee_usdc"] == pytest.approx(0.0198)
    assert result["net_pnl_usdc"] == pytest.approx(-0.5298)
    assert result["mae_bp"] == pytest.approx(100.0)
    assert len(outcomes) == 1


@pytest.mark.asyncio
async def test_tp_and_sl_at_same_timestamp_are_ambiguous_and_not_evaluable() -> None:
    client = _AggTradeClient(
        [
            {"a": 1, "T": 1_100, "p": "100.00"},
            {"a": 2, "T": 2_200, "p": "101.10"},
            {"a": 3, "T": 2_200, "p": "98.90"},
        ]
    )
    tracker, repo, outcomes, _samples, _groups = _tracker(client)

    await _start(tracker)
    await tracker.update()

    result = _outcome(repo)
    assert result["first_touch_result"] == "AMBIGUOUS"
    assert result["evaluable"] is False
    assert result["gross_pnl_usdc"] is None
    assert result["net_pnl_usdc"] is None
    assert result["ev_contribution_usdc"] is None
    assert result["data_quality"]["complete"] is True
    assert len(outcomes) == 1


@pytest.mark.asyncio
async def test_complete_entry_window_without_fill_is_evaluable_zero_ev() -> None:
    client = _AggTradeClient(
        [
            {"a": 1, "T": 1_100, "p": "100.10"},
            {"a": 2, "T": 1_900, "p": "100.20"},
        ]
    )
    tracker, repo, outcomes, _samples, _groups = _tracker(client)

    await _start(tracker)
    await tracker.update()

    result = _outcome(repo)
    assert result["first_touch_result"] == "NO_FILL"
    assert result["evaluable"] is True
    assert result["filled"] is False
    assert result["gross_pnl_usdc"] == 0.0
    assert result["total_fee_usdc"] == 0.0
    assert result["net_pnl_usdc"] == 0.0
    assert result["ev_contribution_usdc"] == 0.0
    assert result["duration_ms"] == 1_000
    assert result["data_quality"]["required_deadline_ms"] == 2_000
    assert len(outcomes) == 1


@pytest.mark.asyncio
async def test_max_hold_uses_last_trade_and_maker_exit_fee() -> None:
    client = _AggTradeClient(
        [
            {"a": 1, "T": 1_100, "p": "100.00"},
            {"a": 2, "T": 2_500, "p": "100.40"},
        ]
    )
    tracker, repo, _outcomes, _samples, _groups = _tracker(client)

    await _start(tracker)
    await tracker.update()

    result = _outcome(repo)
    assert result["first_touch_result"] == "MAX_HOLD"
    assert result["exit_price"] == pytest.approx(100.4)
    assert result["exit_liquidity"] == "TAKER"
    assert result["gross_pnl_usdc"] == pytest.approx(0.2)
    assert result["entry_fee_usdc"] == pytest.approx(0.01)
    assert result["exit_fee_usdc"] == pytest.approx(0.02008)
    assert result["net_pnl_usdc"] == pytest.approx(0.16992)
    assert result["mfe_bp"] == pytest.approx(40.0)
    assert result["mae_bp"] == 0.0
    assert result["duration_ms"] == 1_900


@pytest.mark.asyncio
async def test_pagination_cap_terminalizes_data_incomplete_not_no_fill() -> None:
    client = _AggTradeClient(
        [
            {"a": 1, "T": 1_100, "p": "100.10"},
            {"a": 2, "T": 1_200, "p": "100.20"},
            {"a": 3, "T": 1_300, "p": "100.30"},
        ]
    )
    tracker, repo, outcomes, samples, _groups = _tracker(
        client,
        settings=_settings(
            mainnet_codex_v1460_weak_shadow_page_limit=2,
            mainnet_codex_v1460_weak_shadow_max_pages=1,
        ),
    )

    await _start(tracker)
    await tracker.update()

    result = _outcome(repo)
    assert result["first_touch_result"] == "DATA_INCOMPLETE"
    assert result["evaluable"] is False
    assert result["net_pnl_usdc"] is None
    assert result["data_quality"]["status"] == "DATA_INCOMPLETE"
    assert result["data_quality"]["complete"] is False
    assert result["data_quality"]["pagination_capped"] is True
    assert result["data_quality"]["coverage_through_ms"] < 2_000
    assert result["data_quality"]["reason"] == "pagination_cap"
    assert len(outcomes) == 1
    assert samples == {}


@pytest.mark.asyncio
async def test_fetch_failure_is_retained_for_retry_without_inventing_outcome() -> None:
    client = _AggTradeClient(
        [
            {"a": 1, "T": 1_100, "p": "100.00"},
            {"a": 2, "T": 2_200, "p": "101.00"},
        ],
        fail_times=1,
    )
    tracker, repo, outcomes, samples, _groups = _tracker(client)

    await _start(tracker)
    await tracker.update()

    assert len(repo.events) == 1
    assert outcomes == []
    assert len(samples) == 1
    sample = next(iter(samples.values()))
    assert sample["_fetch_failures"] == 1
    assert sample["_coverage_through_ms"] < sample["entry_deadline_ms"]

    await tracker.update()

    assert _outcome(repo)["first_touch_result"] == "TP_FIRST"
    assert len(outcomes) == 1
    assert samples == {}


@pytest.mark.asyncio
async def test_duplicate_update_is_idempotent_and_callback_runs_once() -> None:
    client = _AggTradeClient(
        [
            {"a": 1, "T": 1_100, "p": "100.00"},
            {"a": 2, "T": 2_200, "p": "101.00"},
        ]
    )
    tracker, repo, outcomes, _samples, _groups = _tracker(client)

    await _start(tracker)
    await tracker.update()
    calls_after_terminal = len(client.calls)
    await tracker.update()

    assert len([event for event in repo.events if event[1] == OUTCOME_EVENT]) == 1
    assert len(outcomes) == 1
    assert len(client.calls) == calls_after_terminal


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    ["near_vwap_flat", "no_momentum_edge", "weak_chop", "mixed"],
)
async def test_only_closed_weak_stup_states_are_accepted(state: str) -> None:
    tracker, repo, _outcomes, _samples, _groups = _tracker(_AggTradeClient())

    assert await _start(tracker, opportunity_id=state, market_state=state) is True
    assert repo.events[-1][2]["market_state"] == f"STUP-S:{state}"


@pytest.mark.asyncio
async def test_nonweak_or_wrong_lane_state_is_rejected_and_dedup_is_stable() -> None:
    tracker, repo, _outcomes, samples, groups = _tracker(_AggTradeClient())

    with pytest.raises(ValueError, match="weak STUP state"):
        await _start(tracker, market_state="clean_extension")
    with pytest.raises(ValueError, match="lane prefix"):
        await _start(tracker, market_state="S1P-L:mixed")
    assert await _start(tracker, market_state="mixed") is True
    assert await _start(tracker, market_state="mixed") is False

    assert len(repo.events) == 1
    assert len(samples) == 1
    assert groups == {"session-1:opportunity-1"}


@pytest.mark.asyncio
async def test_runtime_calls_public_aggtrades_only_and_never_order_methods() -> None:
    client = _AggTradeClient([])
    tracker, repo, _outcomes, _samples, _groups = _tracker(client)

    await _start(tracker)
    await tracker.update()

    assert _outcome(repo)["first_touch_result"] == "NO_FILL"
    assert len(client.calls) == 1
    assert client.order_calls == []
