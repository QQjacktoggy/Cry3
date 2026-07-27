import asyncio
from types import SimpleNamespace

import pytest

from src.gridbot.mainnet.adaptive_fill_shadow import (
    build_stup_fill_shadow_samples,
    first_stup_shadow_fill,
    no_fill_outcome,
)
from src.gridbot.strategy.codex_adaptive_controller import (
    AdaptiveControllerConfig,
    AdaptiveControllerInput,
    AdaptiveRoute,
    ExecutionQuality,
    ExecutorAction,
    decide_adaptive_route,
)


from src.gridbot.mainnet.adaptive_fill_shadow_runtime import AdaptiveStupFillShadowTracker


class _PagingClient:
    def __init__(self):
        self.calls = []

    async def get_agg_trades(self, symbol, **kwargs):
        self.calls.append((symbol, kwargs))
        if len(self.calls) == 1:
            return [{"a": i, "T": 999 + i, "p": "100.0"} for i in range(1, 1001)]
        return [
            {"a": 1001, "T": 2_400, "p": "100.0"},
            {"a": 1002, "T": 2_600, "p": "100.0"},
        ]


@pytest.mark.asyncio
async def test_shadow_pagination_uses_from_id_without_end_time_and_cuts_ttl():
    client = _PagingClient()
    tracker = AdaptiveStupFillShadowTracker(
        client=client,
        repo=SimpleNamespace(),
        settings=SimpleNamespace(mainnet_codex_v1458_stup_fill_shadow_max_pages=10),
        version="_codex_v1.4.58",
        variants={"STUP_E2": 2.0},
        count_event=lambda *_: None,
        samples={},
        started_groups=set(),
        unavailable_groups=set(),
        lock=asyncio.Lock(),
    )

    rows, complete, cursor_ms, next_from_id = await tracker._fetch_group(
        "group",
        {"symbol": "ETHUSDC", "eligible_after_ms": 1_000},
        2_500,
    )

    assert complete is True
    assert len(rows) == 1001
    assert max(int(row["T"]) for row in rows) == 2_400
    assert client.calls[0][1]["start_time"] == 1_000
    assert client.calls[0][1]["end_time"] == 2_500
    assert client.calls[1][1]["from_id"] == 1001
    assert client.calls[1][1]["end_time"] is None
    assert cursor_ms == 2_501
    assert next_from_id == 1001


def _samples(side="SHORT"):
    return build_stup_fill_shadow_samples(
        group_id="session:opportunity",
        run_id="run-1",
        session_id="session",
        symbol="ETHUSDC",
        side=side,
        signal_price=100.0,
        tick_size=0.01,
        start_ms=1_000,
        decision_latency_ms=250,
        ttl_seconds=90,
        notional_usdc=50.0,
        tp_pct=0.001,
        sl_pct=0.0008,
        partial_exit_pct=1.0,
        action_id="S_E2_TP10_SL8_T90_LOCK90_6_0",
        variants={"STUP_E2": 2.0, "STUP_E1": 1.0, "STUP_E0": 0.0},
    )


def test_stup_shadow_requires_one_tick_trade_through_after_latency():
    e2, e1, e0 = _samples()
    trades = [
        {"a": 1, "T": 1_200, "p": "100.50"},
        {"a": 2, "T": 1_300, "p": "100.02"},
        {"a": 3, "T": 1_400, "p": "100.03"},
    ]

    e2_fill = first_stup_shadow_fill(e2, trades)
    e1_fill = first_stup_shadow_fill(e1, trades)
    e0_fill = first_stup_shadow_fill(e0, trades)

    assert e2["entry_price"] == 100.02
    assert e2["trade_through_price"] == 100.03
    assert e2_fill["fill_trade_id"] == 3
    assert e1_fill["fill_trade_id"] == 2
    assert e0_fill["fill_trade_id"] == 2
    assert e2_fill["fill_age_ms"] == 400


def test_stup_shadow_no_fill_is_zero_pnl():
    sample = _samples()[0]

    outcome = no_fill_outcome(sample)

    assert outcome["outcome"] == "no_fill"
    assert outcome["filled"] is False
    assert outcome["no_fill_pnl_usdc"] == 0.0
    assert outcome["resolved_at_ms"] == 91_000


def _controller_request(state):
    return AdaptiveControllerInput(
        adaptive_session_id="session",
        symbol="ETHUSDC",
        lane_code="CNL-WPR-L",
        market_state=state,
        side="LONG",
        opportunity_bucket=123,
        execution_quality=ExecutionQuality.EXECUTABLE,
        incumbent_accepted=True,
        incumbent_action=ExecutorAction("L_E2_TP8_SL8_T180", 8.0, "wpr"),
        challenger_route=AdaptiveRoute.OBSERVE_ONLY,
    )


def test_v1458_enforces_only_allowlisted_cnl_deep_challenger():
    config = AdaptiveControllerConfig(
        challenger_enabled=True,
        live_enforcement_enabled=True,
    )

    deep = decide_adaptive_route(
        _controller_request("CNL-WPR-L:deep_discount_stable"),
        config,
    )
    mixed = decide_adaptive_route(
        _controller_request("CNL-WPR-L:discount_mixed"),
        config,
    )

    assert deep.enforcement_applied is True
    assert deep.live_effective_route is AdaptiveRoute.OBSERVE_ONLY
    assert deep.live_effective_action is None
    assert deep.live_gate_reason == "v1458_cnl_wpr_deep_no_lane_canary_gate"
    assert mixed.selected_route is AdaptiveRoute.OBSERVE_ONLY
    assert mixed.enforcement_applied is False
    assert mixed.live_effective_route is AdaptiveRoute.NORMAL
    assert mixed.live_effective_action is mixed.incumbent_action
