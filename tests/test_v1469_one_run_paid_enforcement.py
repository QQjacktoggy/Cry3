from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.gridbot.binance.models import PositionInfo
from src.gridbot.mainnet.one_run import MainnetOneRunManager
from src.gridbot.mainnet.v1469_adaptive_identity import TakeProfitLevel
from src.gridbot.mainnet.v1469_execution_plan import V1469PaidExecutionPlan
from src.gridbot.mainnet.v1469_paid_entry_runtime import PaidEntryPreparation
from src.gridbot.mainnet.v1469_paid_execution_adapter import SubmissionResult
from src.gridbot.storage.v1469_paid_execution_claim_repository import (
    DurablePaidExecutionClaim,
)
from src.gridbot.strategy.long_pullback import SignalPlan
from src.gridbot.strategy.wildcat_live import WildcatLiveDecision
from tests.test_mainnet_one_run_maker import (
    FakeClient,
    FakeRepo,
    FakeTelegramApp,
    _run,
    _settings,
)


def _decision() -> WildcatLiveDecision:
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
        reasons=["unit"],
        risk_notes=[],
    )
    return WildcatLiveDecision(
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


def _claim() -> DurablePaidExecutionClaim:
    return DurablePaidExecutionClaim(
        claim_id="claim-v1469-paid-one-run",
        environment="MAINNET",
        symbol="ETHUSDC",
        opportunity_id="durable-opportunity",
        arm_key="arm-key",
        lease_id="lease-id",
        lease_generation=1,
        evidence_revision="evidence-revision",
        regime="RANGE",
        execution_profile_hash="profile-hash",
        risk_policy_hash="risk-policy-hash",
        approved_notional_usdc=25.0,
        reserved_loss_usdc=0.05,
        status="CLAIMED",
        generation=0,
        claimed_at_ms=1,
        terminal_at_ms=None,
        terminal_reason=None,
        result_payload=None,
        created_at_ms=1,
        updated_at_ms=1,
    )


def _preparation() -> PaidEntryPreparation:
    plan = V1469PaidExecutionPlan(
        arm_key="arm-key",
        lease_id="lease-id",
        lease_generation=1,
        evidence_revision="evidence-revision",
        lane_code="W6A",
        side="LONG",
        strategy="S1_BB_RSI",
        regime="RANGE",
        execution_profile_id="range-scalp",
        execution_profile_hash="profile-hash",
        risk_policy_hash="risk-policy-hash",
        notional_cap_usdc=25.0,
        entry_offset_bp=2.0,
        entry_ttl_s=37,
        maker_mode="POST_ONLY",
        take_profits=(
            TakeProfitLevel(level_id="TP1", target_bp=5.0, fraction=0.4),
            TakeProfitLevel(level_id="TP2", target_bp=12.0, fraction=0.6),
        ),
        sl_bp=8.0,
        max_hold_s=600,
    )
    risk = SimpleNamespace(
        reason="allowed",
        approved_notional_usdc=25.0,
        reserved_loss_usdc=0.05,
        snapshot=SimpleNamespace(
            risk_policy_hash="risk-policy-hash",
            active_day="2026-07-27",
            remaining_daily_risk_usdc=0.25,
        ),
    )
    return PaidEntryPreparation(
        authority=None,  # type: ignore[arg-type]
        risk=risk,  # type: ignore[arg-type]
        plan=plan,
        claim=_claim(),
    )


class _NeverPaidRuntime:
    async def prepare(self, request):
        raise AssertionError("paid runtime must not be called")


class _LeaseRepo:
    async def get_active_lease(self, **kwargs):
        return None


class _PaidRuntime:
    def __init__(self, preparation: PaidEntryPreparation):
        self.preparation = preparation
        self.requests = []

    async def prepare(self, request):
        self.requests.append(request)
        return self.preparation


class _PaidClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.lookup_calls = []
        self.submit_calls = []

    async def get_order_by_client_order_id(self, symbol, client_order_id):
        self.lookup_calls.append((symbol, client_order_id))
        return None

    async def create_limit_order_raw(self, **kwargs):
        self.submit_calls.append(dict(kwargs))
        return await super().create_limit_order_raw(**kwargs)


class _PaidAdapter:
    def __init__(self):
        self.calls = []

    async def submit_or_reconcile(
        self,
        *,
        claim,
        now_ms,
        find_by_client_order_id,
        submit,
        actor,
        before_submit=None,
    ):
        self.calls.append((claim.claim_id, now_ms, actor))
        assert await find_by_client_order_id("cry3_v1469_claim_cid") is None
        if before_submit is not None:
            await before_submit("cry3_v1469_claim_cid", claim)
        order = await submit("cry3_v1469_claim_cid")
        return SubmissionResult(
            claim=claim,
            client_order_id="cry3_v1469_claim_cid",
            exchange_order=order,
            submitted_now=True,
        )


class _PaidCloseRuntime:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls = []
        self.no_fill_calls = []

    async def record_close(self, **kwargs):
        self.calls.append(dict(kwargs))
        if self.fail:
            raise RuntimeError("close persistence failed")
        return SimpleNamespace()

    async def record_no_fill(self, **kwargs):
        self.no_fill_calls.append(dict(kwargs))
        if self.fail:
            raise RuntimeError("no-fill persistence failed")
        return SimpleNamespace(
            claim=SimpleNamespace(terminal_at_ms=kwargs["occurred_at_ms"])
        )


class _PaidClaimLookup:
    def __init__(self, claim):
        self.claim = claim

    async def get_claim_by_id(self, _claim_id):
        return self.claim


def _set_enforcement(settings, enabled: bool) -> None:
    object.__setattr__(
        settings,
        "mainnet_codex_v1469_live_enforcement_enabled",
        enabled,
    )


@pytest.mark.asyncio
async def test_enforcement_off_never_calls_paid_runtime(monkeypatch):
    settings = _settings(mainnet_entry_limit_offset=0.0004)
    client = FakeClient()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        settings,
        client,
        repo,
        FakeTelegramApp(),
        v1469_paid_entry_runtime=_NeverPaidRuntime(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(manager, "_codex_v1_execution_enabled", lambda: False)

    async def finish(*args, **kwargs):
        return "durable-off"

    monkeypatch.setattr(manager, "_v1469_finish_paid_observation", finish)
    await manager._place_entry(
        _run(run_id="cry3mn_v1469_off", status="ARMED"),
        _decision(),
    )

    assert len(client.open_orders) == 1
    assert repo.updated[-1][1]["signal_json"].get(
        "v1469_paid_execution"
    ) is None


@pytest.mark.asyncio
async def test_enforcement_on_missing_durability_makes_zero_order_calls(
    monkeypatch,
):
    settings = _settings()
    _set_enforcement(settings, True)
    client = _PaidClient()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        settings,
        client,
        repo,
        FakeTelegramApp(),
        v1469_paid_entry_runtime=_NeverPaidRuntime(),  # type: ignore[arg-type]
        v1469_paid_execution_adapter=_PaidAdapter(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(manager, "_codex_v1_execution_enabled", lambda: False)

    async def finish(*args, **kwargs):
        return None

    monkeypatch.setattr(manager, "_v1469_finish_paid_observation", finish)
    await manager._place_entry(
        _run(run_id="cry3mn_v1469_no_durable", status="ARMED"),
        _decision(),
    )

    assert client.lookup_calls == []
    assert client.submit_calls == []
    assert not hasattr(client, "leverage_set")
    assert repo.updated == []
    assert repo.events[-1][2]["reason"] == "durable_opportunity_id_missing"


@pytest.mark.asyncio
async def test_enforcement_on_submits_once_and_persists_exact_payload(
    monkeypatch,
):
    settings = _settings()
    _set_enforcement(settings, True)
    client = _PaidClient()
    repo = FakeRepo()
    preparation = _preparation()
    paid_runtime = _PaidRuntime(preparation)
    adapter = _PaidAdapter()
    manager = MainnetOneRunManager(
        settings,
        client,
        repo,
        FakeTelegramApp(),
        v1469_lease_repo=_LeaseRepo(),  # type: ignore[arg-type]
        v1469_paid_entry_runtime=paid_runtime,  # type: ignore[arg-type]
        v1469_paid_execution_adapter=adapter,  # type: ignore[arg-type]
    )
    monkeypatch.setattr(manager, "_codex_v1_execution_enabled", lambda: False)

    now_ms = 1_800_000_000_000
    state = SimpleNamespace(
        regime="RANGE",
        direction="NONE",
        since_ms=now_ms - 20_000,
        last_decision_time_ms=now_ms,
    )
    manager._v1469_observation_regime_runtimes["ETHUSDC"] = (
        SimpleNamespace(state=state)
    )

    async def finish(*args, **kwargs):
        return "durable-opportunity"

    monkeypatch.setattr(manager, "_v1469_finish_paid_observation", finish)
    await manager._place_entry(
        _run(run_id="cry3mn_v1469_paid", status="ARMED"),
        _decision(),
    )

    assert len(paid_runtime.requests) == 1
    assert paid_runtime.requests[0].authority_input.opportunity_id == (
        "durable-opportunity"
    )
    assert len(adapter.calls) == 1
    assert len(client.lookup_calls) == 1
    assert len(client.submit_calls) == 1
    prebind = repo.updated[0][1]
    assert prebind["status"] == "ENTRY_PENDING"
    assert prebind["entry_order_id"] == 0
    assert prebind["signal_json"]["v1469_paid_order_state"] == "SUBMITTING"
    submitted = client.submit_calls[0]
    assert submitted["time_in_force"] == "GTX"
    assert submitted["client_order_id"] == "cry3_v1469_claim_cid"
    assert submitted["price"] == pytest.approx(99.98)

    update = repo.updated[-1][1]
    payload = update["signal_json"]
    exact = payload["v1469_paid_execution"]
    assert exact == preparation.plan.to_payload()
    assert payload["v1469_paid_claim_id"] == preparation.claim.claim_id
    assert payload["v1469_paid_client_order_id"] == (
        "cry3_v1469_claim_cid"
    )
    assert payload["v1469_paid_risk"]["reserved_loss_usdc"] == 0.05
    assert payload["entry_ttl_seconds"] == 37
    assert payload["entry_ttl_source"] == "v1469_paid_execution_plan"
    assert payload["entry_deadline_ms"] - payload["entry_submitted_at_ms"] <= (
        37_000
    )
    assert payload["effective_recovery_enabled"] is False
    assert update["entry_client_order_id"] == "cry3_v1469_claim_cid"
    assert update["cumulative_notional_usdc"] == 25.0
@pytest.mark.asyncio
async def test_exact_plan_controls_tp_sl_hold_and_remaining_fraction():
    settings = _settings()
    client = FakeClient()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        settings, client, repo, FakeTelegramApp()
    )
    preparation = _preparation()
    signal = {
        "v1469_paid_execution": preparation.plan.to_payload(),
        "v1469_paid_initial_qty": 1.0,
        "wildcat": {"sl_pct": 0.5, "tp_pct": 0.5},
    }
    run = _run(
        run_id="cry3mn_v1469_exact_exits",
        status="RUNNING",
        side="LONG",
        signal_json=signal,
    )
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=1.0,
        entry_price=100.0,
        mark_price=100.0,
        unrealized_pnl=0.0,
        liquidation_price=90.0,
        leverage=75,
        margin_type="cross",
    )

    orders = await manager._desired_take_profit_orders(
        run, position, signal, "SELL"
    )
    assert manager._v1460_entry_safety_active(run)
    assert [float(item[1]) for item in orders] == [0.4, 0.6]
    assert [item[2] for item in orders] == pytest.approx([100.05, 100.12])
    assert manager._effective_sl_pct(signal) == pytest.approx(0.0008)
    assert manager._max_holding_bars_for_run(signal) == 10

    await repo.log_event(
        run["run_id"], "partial_exit", {"qty_filled": "0.4"}
    )
    remaining = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.6,
        entry_price=100.0,
        mark_price=100.1,
        unrealized_pnl=0.06,
        liquidation_price=90.0,
        leverage=75,
        margin_type="cross",
    )
    remaining_orders = await manager._desired_take_profit_orders(
        run, remaining, signal, "SELL"
    )
    assert len(remaining_orders) == 1
    assert float(remaining_orders[0][1]) == pytest.approx(0.6)
    assert remaining_orders[0][2] == pytest.approx(100.12)


@pytest.mark.asyncio
async def test_exact_tp_partial_fill_keeps_layer_targets_and_restarts_from_ledger():
    settings = _settings()
    client = FakeClient()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        settings, client, repo, FakeTelegramApp()
    )
    signal = {
        "v1469_paid_execution": _preparation().plan.to_payload(),
        "v1469_paid_initial_qty": 1.0,
    }
    run = _run(
        run_id="cry3mn_v1469_partial_tp",
        status="RUNNING",
        side="LONG",
        signal_json=signal,
        qty=0.8,
    )
    position = PositionInfo(
        symbol="ETHUSDC",
        position_amt=0.8,
        entry_price=100.0,
        mark_price=100.0,
        unrealized_pnl=0.0,
        liquidation_price=90.0,
        leverage=75,
        margin_type="cross",
    )
    client.open_orders = [
        {
            "orderId": 101,
            "clientOrderId": f"{run['run_id']}_tp1",
            "origQty": "0.4",
            "executedQty": "0.2",
            "price": "100.05",
            "side": "SELL",
        },
        {
            "orderId": 102,
            "clientOrderId": f"{run['run_id']}_tp3",
            "origQty": "0.6",
            "executedQty": "0",
            "price": "100.12",
            "side": "SELL",
        },
    ]

    desired = await manager._sync_take_profit_orders(run, position, signal)

    assert [float(item[1]) for item in desired] == pytest.approx([0.2, 0.6])
    assert client.cancelled == []
    progress = [
        details
        for _, event_type, details in repo.events
        if event_type == "v1469_tp_layer_progress"
    ]
    assert progress[-1]["level_index"] == 0
    assert progress[-1]["executed_qty"] == pytest.approx(0.2)

    restarted = MainnetOneRunManager(
        settings, FakeClient(), repo, FakeTelegramApp()
    )
    rebuilt = await restarted._desired_take_profit_orders(
        run, position, signal, "SELL"
    )
    assert [float(item[1]) for item in rebuilt] == pytest.approx([0.2, 0.6])
    assert [item[2] for item in rebuilt] == pytest.approx([100.05, 100.12])


@pytest.mark.asyncio
async def test_paid_close_uses_stable_exact_facts_and_marks_policy_breach():
    settings = _settings()
    runtime = _PaidCloseRuntime()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        settings,
        FakeClient(),
        repo,
        FakeTelegramApp(),
        v1469_paid_close_runtime=runtime,  # type: ignore[arg-type]
    )
    run = _run(
        run_id="cry3mn_v1469_close",
        status="COMPLETED",
        signal_json={
            "v1469_paid_execution": _preparation().plan.to_payload(),
            "v1469_paid_claim_id": _claim().claim_id,
        },
    )

    assert await manager._v1469_record_paid_close(
        run,
        net_pnl_usdc=-0.15,
        terminal_reason="SL",
        terminal_at_ms=1_800_000_123_456,
    )
    assert runtime.calls == [
        {
            "claim_id": _claim().claim_id,
            "fee_net_pnl_usdc": -0.15,
            "terminal_reason": "SL",
            "occurred_at_ms": 1_800_000_123_456,
            "source_run_id": "cry3mn_v1469_close",
            "hard_loss_marker": True,
            "actor": manager._v1465_owner_id,
        }
    ]
    assert repo.events[-1][1] == "v1469_paid_close_recorded"


@pytest.mark.asyncio
async def test_paid_close_failure_keeps_repair_required_and_returns_false():
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        _settings(),
        FakeClient(),
        repo,
        FakeTelegramApp(),
        v1469_paid_close_runtime=_PaidCloseRuntime(fail=True),  # type: ignore[arg-type]
    )
    run = _run(
        run_id="cry3mn_v1469_close_fail",
        status="COMPLETED",
        signal_json={
            "v1469_paid_execution": _preparation().plan.to_payload(),
            "v1469_paid_claim_id": _claim().claim_id,
        },
    )

    assert not await manager._v1469_record_paid_close(
        run,
        net_pnl_usdc=0.01,
        terminal_reason="TP",
        terminal_at_ms=1_800_000_123_456,
    )
    assert repo.events[-1][1] == "v1469_paid_close_repair_required"


@pytest.mark.asyncio
async def test_paid_entry_expiry_terminalizes_proven_no_fill(monkeypatch):
    settings = _settings()
    client = FakeClient()
    client.all_orders = [
        {
            "symbol": "ETHUSDC",
            "orderId": 123,
            "clientOrderId": "cry3_v1469_claim_cid",
            "status": "CANCELED",
            "executedQty": "0",
        }
    ]
    repo = FakeRepo()
    runtime = _PaidCloseRuntime()
    manager = MainnetOneRunManager(
        settings,
        client,
        repo,
        FakeTelegramApp(),
        v1469_paid_close_runtime=runtime,  # type: ignore[arg-type]
    )

    async def advance(*args, **kwargs):
        return None

    monkeypatch.setattr(manager, "_advance_loop_after_entry_failure", advance)
    run = _run(
        run_id="cry3mn_v1469_no_fill",
        status="ENTRY_PENDING",
        entry_order_id=123,
        entry_client_order_id="cry3_v1469_claim_cid",
        updated_at_ms=1,
        signal_json={
            "v1469_paid_execution": _preparation().plan.to_payload(),
            "v1469_paid_claim_id": _claim().claim_id,
            "v1469_paid_client_order_id": "cry3_v1469_claim_cid",
        },
    )

    await manager._run_entry_pending(run)

    assert len(runtime.no_fill_calls) == 1
    assert runtime.no_fill_calls[0]["terminal_reason"] == (
        "entry_not_open_no_position"
    )
    assert repo.completed == [
        (
            "cry3mn_v1469_no_fill",
            "ENTRY_EXPIRED",
            "entry_not_open_no_position",
            None,
        )
    ]
    assert any(event[1] == "v1469_paid_no_fill_recorded" for event in repo.events)


@pytest.mark.asyncio
async def test_paid_entry_ambiguous_cancel_never_releases_claim(monkeypatch):
    settings = _settings()
    client = FakeClient()
    repo = FakeRepo()
    runtime = _PaidCloseRuntime()
    manager = MainnetOneRunManager(
        settings,
        client,
        repo,
        FakeTelegramApp(),
        v1469_paid_close_runtime=runtime,  # type: ignore[arg-type]
    )

    async def ambiguous_cancel(*args, **kwargs):
        raise TimeoutError("cancel outcome unknown")

    monkeypatch.setattr(client, "cancel_order", ambiguous_cancel)
    run = _run(
        run_id="cry3mn_v1469_ambiguous_no_fill",
        status="ENTRY_PENDING",
        entry_order_id=124,
        entry_client_order_id="cry3_v1469_claim_cid",
        updated_at_ms=1,
        signal_json={
            "v1469_paid_execution": _preparation().plan.to_payload(),
            "v1469_paid_claim_id": _claim().claim_id,
            "v1469_paid_client_order_id": "cry3_v1469_claim_cid",
        },
    )

    await manager._run_entry_pending(run)

    assert runtime.no_fill_calls == []
    assert repo.completed == []
    assert any(
        event[1] == "v1460_entry_cancel_reconcile_pending"
        for event in repo.events
    )

@pytest.mark.asyncio
async def test_prebound_abandoned_claim_finishes_without_order_id():
    repo = FakeRepo()
    claim = SimpleNamespace(
        status="ABANDONED",
        terminal_reason="EXCHANGE_REJECTED",
        result_payload={"submitted": False},
    )
    manager = MainnetOneRunManager(
        _settings(),
        _PaidClient(),
        repo,
        FakeTelegramApp(),
        v1469_paid_claim_repo=_PaidClaimLookup(claim),
    )
    run = _run(
        run_id="cry3mn_v1469_prebound_rejected",
        status="ENTRY_PENDING",
        entry_order_id=0,
        signal_json={
            "v1469_paid_execution": _preparation().plan.to_payload(),
            "v1469_paid_claim_id": _claim().claim_id,
            "v1469_paid_client_order_id": "cry3_v1469_claim_cid",
        },
    )

    await manager._run_entry_pending(run)

    assert repo.completed[-1][1] == "ENTRY_REJECTED"
    assert repo.completed[-1][2] == "EXCHANGE_REJECTED"