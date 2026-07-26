from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.gridbot.binance.models import PositionInfo
from src.gridbot.mainnet import one_run as one_run_module
from src.gridbot.mainnet.one_run import (
    GTXSlippageExceeded,
    MainnetOneRunManager,
    V1459_ADAPTIVE_CANARY_CONTRACT,
    V1460_ADAPTIVE_CANARY_CONTRACT,
)
from src.gridbot.strategy.codex_v1_live import CodexV1Decision
from src.gridbot.strategy.long_pullback import SignalPlan
from src.gridbot.strategy.wildcat_live import WildcatLiveDecision
from tests.test_mainnet_one_run_maker import (
    FakeClient,
    FakeRepo,
    FakeTelegramApp,
    _V1459FakeRuntime,
    _settings,
)


class ReadyObservationRuntime(_V1459FakeRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.flags = SimpleNamespace(record_reconciliation=True)


def _v1460_settings(**overrides):
    values = {
        "mainnet_codex_v1_enabled": True,
        "mainnet_codex_v1459_candidate_selector_enabled": True,
        "mainnet_codex_v1459_live_enforcement_enabled": True,
        "mainnet_v1459_observation_enabled": True,
        "mainnet_v1459_observation_persist_session_enabled": True,
        "mainnet_v1459_observation_record_opportunities_enabled": True,
        "mainnet_v1459_observation_record_reconciliation_enabled": True,
        "mainnet_codex_v1459_regime_switch_enabled": False,
        "mainnet_codex_v1459_runner_enabled": False,
        "mainnet_codex_v1459_one_step_reprice_enabled": False,
        "mainnet_codex_v1460_candidate_selector_enabled": True,
        "mainnet_codex_v1460_lane_matrix_enabled": True,
        "mainnet_codex_v1460_live_enforcement_enabled": True,
        "mainnet_codex_v1460_shadow_evidence_enabled": True,
        "mainnet_codex_v1460_runner_enabled": False,
        "mainnet_codex_v1460_one_step_reprice_enabled": False,
    }
    values.update(overrides)
    return _settings(**values)


def _manager(*, settings=None, client=None, repo=None, runtime=None):
    return MainnetOneRunManager(
        settings or _v1460_settings(),
        client or FakeClient(),
        repo or FakeRepo(),
        FakeTelegramApp(),
        observation_runtime=runtime or ReadyObservationRuntime(),
    )


def _adaptive_run(run_id: str = "cry3mn_v1460", **overrides):
    row = {
        "run_id": run_id,
        "symbol": "ETHUSDC",
        "status": "ARMED",
        "side": "SHORT",
        "armed_at_ms": 1_000,
        "updated_at_ms": 1_000,
        "params": {
            "mode": "adaptive_continuous",
            "adaptive": {
                "mode": "adaptive_continuous",
                "session_id": "adaptive-v1460-test",
            },
        },
    }
    row.update(overrides)
    return row


def _session(manager: MainnetOneRunManager, **overrides):
    now_ms = int(one_run_module.time.time() * 1000)
    values = {
        "session_id": "adaptive-v1460-test",
        "actor": "test",
        "started_at_ms": now_ms,
        "deadline_at_ms": now_ms + 72 * 60 * 60 * 1000,
        "last_checkpoint_at_ms": now_ms,
        "config_sha": manager._adaptive_config_sha(),
        "prior_runtime": manager._adaptive_runtime_snapshot(),
        "terminal_runs": 0,
        "net_pnl_usdc": 0.0,
        "high_water_net_pnl_usdc": 0.0,
        "run_ids": [],
        "disabled_states": set(),
        "state_net_pnl_usdc": {},
        "state_throttle_count": {},
        "state_throttle_deadlines": {},
        "route_loss_streaks": {},
        "v1460_lane_state_loss_streaks": {},
        "v1460_lane_state_net_pnl_usdc": {},
        "v1460_isolated_keys": set(),
        "v1460_weak_evidence": {},
        "counters": manager._new_adaptive_counters(),
        "rearm_enabled": True,
        "stop_requested": False,
        "route_stats": {},
    }
    values.update(overrides)
    manager._adaptive_session = values
    return values


def _codex(
    market_state: str = "STUP-S:weak_chop",
    *,
    lane_code: str = "STUP-S",
    requested_notional_usdc: float = 50.0,
    notional_mult: float = 1.0,
) -> CodexV1Decision:
    return CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.60-test",
        baseline="incumbent",
        lane=lane_code,
        lane_code=lane_code,
        strategy="S2_SuperTrend",
        side="SHORT",
        entry_offset_bp=2.0,
        size_mult=1.0,
        notional_mult=notional_mult,
        requested_notional_usdc=requested_notional_usdc,
        reason="incumbent_accept",
        regime=market_state,
        metrics={
            "market_state": market_state,
            "entry_bp": 2.0,
            "tp1_bp": 10.0,
            "sl_bp": 8.0,
            "ttl_s": 90,
            "hold_s": 360,
            "v1455_action": "S_E2_TP10_SL8_T90",
        },
        policy_tag="incumbent",
    )


def _wildcat(notional_usdc: float = 50.0) -> WildcatLiveDecision:
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
        stop_loss=100.08,
        take_profits=[99.90],
        planned_notional_usdc=notional_usdc,
        planned_margin_usdc=notional_usdc / 75.0,
        planned_qty=notional_usdc / 100.0,
        reasons=["unit"],
        risk_notes=[],
    )
    return WildcatLiveDecision(
        signal=signal,
        strategy="S2_SuperTrend",
        side="SHORT",
        tp_pct=0.001,
        sl_pct=0.0008,
        partial_exit_pct=1.0,
        partial_tp_pct=0.001,
        recovery_steps=0,
        recovery_trigger_pct=0.0005,
        recovery_tp_shrink=0.45,
        adverse_exit_bars=10,
        adverse_exit_loss_pct=0.0008,
        max_holding_bars=6,
        params_label="v1460-unit",
    )


def _position(amount: float = -0.5) -> PositionInfo:
    return PositionInfo(
        symbol="ETHUSDC",
        position_amt=amount,
        entry_price=100.0,
        mark_price=100.0,
        unrealized_pnl=0.0,
        liquidation_price=120.0,
        leverage=75,
        margin_type="cross",
    )


def _terminal_run(
    run_id: str,
    *,
    lane_code: str = "S1P-L",
    market_state: str = "S1P-L:ordinary_pullback",
) -> dict:
    run = _adaptive_run(run_id, status="COMPLETED")
    run["signal_json"] = json.dumps(
        {
            "side": "SHORT",
            "take_profit": 99.90,
            "stop_loss": 100.08,
            "codex_v1": {
                "enabled": True,
                "lane_code": lane_code,
                "metrics": {"market_state": market_state},
            },
            "adaptive": {
                "decision": {
                    "live_effective_route": "NORMAL",
                    "live_effective_action": {"action_id": "CONTROL"},
                }
            },
        }
    )
    return run


def _entry_pending_run(run_id: str = "cry3mn_v1460") -> dict:
    return _adaptive_run(
        run_id,
        status="ENTRY_PENDING",
        entry_client_order_id=f"{run_id}_entry",
        signal_json=json.dumps(
            {
                "side": "SHORT",
                "take_profit": 99.90,
                "stop_loss": 100.08,
                "codex_v1": {
                    "enabled": True,
                    "lane_code": "STUP-S",
                    "metrics": {"market_state": "STUP-S:clean_extension"},
                },
            }
        ),
    )


def test_v1460_defaults_are_off_and_fallback_contract_is_v1459() -> None:
    manager = _manager(settings=_settings())

    assert manager._v1460_config_selected() is False
    assert manager._adaptive_canary_contract() == V1459_ADAPTIVE_CANARY_CONTRACT
    assert _settings().mainnet_codex_v1460_live_enforcement_enabled is False


def test_v1460_reviewed_enforcement_is_ready_and_uses_new_contract() -> None:
    manager = _manager()

    requested, missing = manager._v1460_enforcement_readiness()

    assert requested is True
    assert missing == ()
    assert manager._adaptive_canary_contract() == V1460_ADAPTIVE_CANARY_CONTRACT


@pytest.mark.asyncio
async def test_v1460_weak_state_is_blocked_and_records_policy_transition() -> None:
    repo = FakeRepo()
    manager = _manager(repo=repo)
    _session(manager)

    applied = await manager._v1460_apply_lane_policy(
        _adaptive_run(),
        _codex(),
    )

    assert applied.accepted is False
    audit = applied.metrics["v1460_lane_adaptive"]
    assert audit["action_mode"] == "SHADOW_BLOCK"
    assert audit["matrix_rule_id"] == "v1460.stup_weak.shadow_block"
    assert audit["policy_hash"] == manager._v1460_policy_hash()
    assert any(event == "v1460_lane_policy_transition" for _, event, _ in repo.events)


@pytest.mark.asyncio
async def test_v1460_qualified_weak_state_is_half_risk_and_hard_capped_at_25() -> None:
    manager = _manager()
    _session(
        manager,
        v1460_weak_evidence={
            "opportunities": 8,
            "evaluable": 8,
            "tp_first": 6,
            "sl_first": 2,
            "net_pnl_usdc": 0.08,
            "data_complete": True,
            "ambiguous": 0,
            "incomplete": 0,
        },
    )

    applied = await manager._v1460_apply_lane_policy(
        _adaptive_run(),
        _codex(),
    )
    live = manager._apply_codex_v1_decision(_wildcat(), applied)

    assert applied.accepted is True
    assert applied.metrics["v1460_lane_adaptive"]["action_mode"] == "PROBATION_0_5"
    assert applied.metrics["applied_notional_cap_usdc"] == pytest.approx(25.0)
    assert live.signal.planned_notional_usdc == pytest.approx(25.0)


@pytest.mark.asyncio
async def test_v1460_unknown_state_is_generic_half_risk_not_lane_specific() -> None:
    manager = _manager()
    _session(manager)

    applied = await manager._v1460_apply_lane_policy(
        _adaptive_run(),
        _codex(market_state="UNKNOWN", lane_code="CNL-L1MR-L"),
    )
    live = manager._apply_codex_v1_decision(_wildcat(), applied)

    audit = applied.metrics["v1460_lane_adaptive"]
    assert applied.accepted is True
    assert audit["action_mode"] == "PROBATION_0_5"
    assert audit["matrix_rule_id"] == "v1460.state.unknown_probation_0_5"
    assert audit["risk_scale"] == pytest.approx(0.5)
    assert applied.metrics["applied_notional_cap_usdc"] == pytest.approx(25.0)
    assert live.signal.planned_notional_usdc == pytest.approx(25.0)


@pytest.mark.parametrize(
    "override",
    [
        {"mainnet_codex_stups_max_sl_bp": 24.0},
        {"mainnet_s2_max_sl_bp": 16.0},
        {"mainnet_codex_v1460_weak_shadow_taker_fee_rate": 0.0005},
    ],
)
def test_v1460_policy_hash_covers_exit_and_shadow_cost_contract(
    override: dict,
) -> None:
    baseline = _manager()
    changed = _manager(settings=_v1460_settings(**override))

    assert baseline._v1460_policy_hash() != changed._v1460_policy_hash()


def test_v1460_policy_hash_covers_cancel_reconcile_timeout(monkeypatch) -> None:
    baseline = _manager()._v1460_policy_hash()

    monkeypatch.setattr(
        one_run_module,
        "V1460_CANCEL_RECONCILE_TIMEOUT_MS",
        31_000,
    )

    assert _manager()._v1460_policy_hash() != baseline


@pytest.mark.asyncio
async def test_v1460_weak_outcome_updates_durable_promotion_evidence() -> None:
    runtime = ReadyObservationRuntime()
    manager = _manager(runtime=runtime)
    session = _session(manager)

    await manager._v1460_on_weak_shadow_outcome(
        {
            "session_id": session["session_id"],
            "opportunity_key": "adaptive-v1460-test:opp-1",
            "first_touch_result": "TP_FIRST",
            "evaluable": True,
            "ev_contribution_usdc": 0.02,
            "data_quality": {"complete": True},
        }
    )

    evidence = session["v1460_weak_evidence"]
    assert evidence["opportunities"] == 1
    assert evidence["evaluable"] == 1
    assert evidence["tp_first"] == 1
    assert evidence["net_pnl_usdc"] == pytest.approx(0.02)
    assert evidence["data_complete"] is True
    assert len(runtime.checkpoints) == 1


@pytest.mark.asyncio
async def test_v1460_ambiguous_shadow_can_never_mark_data_complete() -> None:
    manager = _manager()
    session = _session(manager)

    await manager._v1460_on_weak_shadow_outcome(
        {
            "session_id": session["session_id"],
            "opportunity_key": "adaptive-v1460-test:opp-ambiguous",
            "first_touch_result": "AMBIGUOUS",
            "evaluable": False,
            "ev_contribution_usdc": None,
            "data_quality": {"complete": True},
        }
    )

    evidence = session["v1460_weak_evidence"]
    assert evidence["ambiguous"] == 1
    assert evidence["data_complete"] is False


@pytest.mark.asyncio
async def test_v1460_weak_evidence_keeps_complete_dedup_history() -> None:
    manager = _manager()
    session = _session(manager)

    for index in range(105):
        await manager._v1460_on_weak_shadow_outcome(
            {
                "session_id": session["session_id"],
                "opportunity_key": f"adaptive-v1460-test:opp-{index}",
                "first_touch_result": "NO_FILL",
                "evaluable": True,
                "ev_contribution_usdc": 0.0,
                "data_quality": {"complete": True},
            }
        )
    await manager._v1460_on_weak_shadow_outcome(
        {
            "session_id": session["session_id"],
            "opportunity_key": "adaptive-v1460-test:opp-0",
            "first_touch_result": "TP_FIRST",
            "evaluable": True,
            "ev_contribution_usdc": 1.0,
            "data_quality": {"complete": True},
        }
    )

    evidence = session["v1460_weak_evidence"]
    assert evidence["opportunities"] == 105
    assert len(evidence["opportunity_keys"]) == 105
    assert evidence.get("tp_first", 0) == 0
    assert evidence["net_pnl_usdc"] == pytest.approx(0.0)


def test_v1460_canary_payload_reports_pass_and_wilson_separately() -> None:
    manager = _manager()
    session = _session(manager)
    session["counters"].update(
        {
            "paid_closed_fills": 20,
            "wins": 15,
            "losses": 5,
            "flats": 0,
            "opportunities": 24,
            "net_pnl_usdc": 0.20,
        }
    )

    result = manager._v1460_canary_evaluation_payload(session)

    assert result["status"] == "PASS"
    assert result["raw_win_rate"] == "0.75"
    assert float(result["wilson_95_lower_bound_report_only"]) < 0.75
    assert result["criteria"]["net_pnl_usdc"] is True


def test_v1460_timing_uses_persisted_deadline_and_forces_zero_grace() -> None:
    manager = _manager(
        settings=_v1460_settings(
            mainnet_codex_v1438_entry_late_fill_grace_seconds=30.0
        )
    )
    run = _adaptive_run(
        status="ENTRY_PENDING",
        signal_json=json.dumps(
            {
                "entry_submitted_at_ms": 10_000,
                "entry_deadline_ms": 20_000,
                "codex_v1": {
                    "enabled": True,
                    "lane_code": "STUP-S",
                    "metrics": {"market_state": "STUP-S:clean_extension"},
                },
            }
        ),
    )

    first = manager._entry_timing_snapshot(run, now_ms=30_000)
    run["updated_at_ms"] = 99_000
    second = manager._entry_timing_snapshot(run, now_ms=100_000)

    assert first["entry_deadline_ms"] == second["entry_deadline_ms"] == 20_000
    assert first["submitted_at_ms"] == second["submitted_at_ms"] == 10_000
    assert first["grace_s"] == second["grace_s"] == 0.0


@pytest.mark.asyncio
async def test_v1460_cancel_confirmation_requires_zero_fill_trade_and_position() -> None:
    client = FakeClient()
    manager = _manager(client=client)
    _session(manager)
    run = _adaptive_run(entry_client_order_id="cry3mn_v1460_entry")

    result = await manager._v1460_cancel_confirm_entry(
        run,
        7,
        purpose="unit",
        halt_on_unsafe=True,
    )

    assert result["status"] == "NO_FILL"
    assert manager._adaptive_session["stop_requested"] is False


@pytest.mark.asyncio
async def test_v1460_lagging_new_status_is_durable_pending_then_zero_fill_after_restart() -> None:
    repo = FakeRepo()
    client = FakeClient()
    client.all_orders = [
        {
            "orderId": 7,
            "clientOrderId": "cry3mn_v1460_entry",
            "status": "NEW",
            "executedQty": "0",
        }
    ]
    manager = _manager(client=client, repo=repo)
    first_session = _session(manager)
    run = _entry_pending_run()
    signal = json.loads(run["signal_json"])
    signal["entry_submitted_at_ms"] = 10_000
    signal["entry_deadline_ms"] = 20_000
    run["signal_json"] = json.dumps(signal)

    first = await manager._v1460_cancel_confirm_entry(
        run,
        7,
        purpose="entry_not_open",
        halt_on_unsafe=True,
    )

    assert first["status"] == "PENDING_CONFIRMATION"
    assert first_session["stop_requested"] is False
    persisted = json.loads(run["signal_json"])
    assert persisted["entry_deadline_ms"] == 20_000
    marker = persisted[one_run_module.V1460_CANCEL_RECONCILE_PENDING_KEY]
    assert marker["order_id"] == 7
    assert marker["attempts"] == 1
    assert marker["deadline_ms"] - marker["started_at_ms"] == 30_000
    assert any(
        event == "v1460_entry_cancel_reconcile_pending"
        for _, event, _ in repo.events
    )
    assert not any(event == "v1460_paid_path_halted" for _, event, _ in repo.events)

    # Simulate exchange-history convergence after a process restart.  The
    # pending marker in signal_json, not in-memory state, carries the window.
    client.all_orders[0]["status"] = "CANCELED"
    restarted = _manager(client=client, repo=repo)
    restarted_session = _session(restarted)
    second = await restarted._v1460_cancel_confirm_entry(
        run,
        7,
        purpose="entry_not_open",
        halt_on_unsafe=True,
    )

    assert second["status"] == "NO_FILL"
    assert restarted_session["stop_requested"] is False
    assert client.cancelled == [("ETHUSDC", 7)]
    assert any(event == "entry_cancel_reconciled_no_fill" for _, event, _ in repo.events)
    assert not any(event == "v1460_paid_path_halted" for _, event, _ in repo.events)


@pytest.mark.asyncio
async def test_v1460_unproven_zero_fill_halts_only_after_fixed_pending_window() -> None:
    repo = FakeRepo()
    client = FakeClient()
    client.all_orders = [
        {
            "orderId": 7,
            "clientOrderId": "cry3mn_v1460_entry",
            "status": "NEW",
            "executedQty": "0",
        }
    ]
    manager = _manager(client=client, repo=repo)
    session = _session(manager)
    run = _entry_pending_run()

    first = await manager._v1460_cancel_confirm_entry(
        run,
        7,
        purpose="entry_not_open",
        halt_on_unsafe=True,
    )
    assert first["status"] == "PENDING_CONFIRMATION"
    assert session["stop_requested"] is False

    signal = json.loads(run["signal_json"])
    marker = signal[one_run_module.V1460_CANCEL_RECONCILE_PENDING_KEY]
    marker["started_at_ms"] -= (
        one_run_module.V1460_CANCEL_RECONCILE_TIMEOUT_MS + 1
    )
    marker["deadline_ms"] = (
        marker["started_at_ms"]
        + one_run_module.V1460_CANCEL_RECONCILE_TIMEOUT_MS
    )
    run["signal_json"] = json.dumps(signal)

    second = await manager._v1460_cancel_confirm_entry(
        run,
        7,
        purpose="entry_ttl_expiry",
        halt_on_unsafe=True,
    )

    assert second["status"] == "UNSAFE"
    assert second["reason"] == "zero_fill_terminal_unproven"
    assert second["pending_elapsed_ms"] >= 30_000
    assert session["stop_requested"] is True
    assert session["safety_halt_reason"] == "entry_zero_fill_terminal_unproven"


@pytest.mark.asyncio
async def test_v1460_cancel_confirmation_detects_partial_fill_and_forbids_replace() -> None:
    client = FakeClient()
    client.position = _position()
    client.user_trades = [SimpleNamespace(order_id=7, time_ms=15_000)]
    manager = _manager(client=client)
    _session(manager)

    result = await manager._v1460_cancel_confirm_entry(
        _entry_pending_run(),
        7,
        purpose="unit",
        halt_on_unsafe=True,
    )

    assert result["status"] == "FILLED"
    assert result["actual_fill_ms"] == 15_000
    assert result["identity_unsafe"] is False
    assert result["cancel_race_hard_sl_armed"] is True
    assert len(client.stop_market_sl_orders) == 1
    assert client.stop_market_sl_orders[0]["clientAlgoId"] == "cry3mn_v1460_sl"


@pytest.mark.asyncio
async def test_v1460_orphan_position_halts_but_returns_position_for_protection() -> None:
    client = FakeClient()
    client.position = _position()
    manager = _manager(client=client)
    session = _session(manager)

    result = await manager._v1460_cancel_confirm_entry(
        _entry_pending_run(),
        7,
        purpose="unit",
        halt_on_unsafe=True,
    )

    assert result["status"] == "FILLED"
    assert result["identity_unsafe"] is True
    assert session["stop_requested"] is True
    assert session["safety_halt_reason"] == "orphan_position_after_entry_cancel"
    assert result["cancel_race_hard_sl_armed"] is True
    assert len(client.stop_market_sl_orders) == 1


@pytest.mark.asyncio
async def test_v1460_cancel_race_hard_sl_failure_halts_and_closes(
    monkeypatch,
) -> None:
    client = FakeClient()
    client.position = _position()
    client.user_trades = [SimpleNamespace(order_id=7, time_ms=15_000)]
    manager = _manager(client=client)
    session = _session(manager)
    calls: list[str] = []

    async def fail_sl(**kwargs):
        calls.append("hard_sl")
        raise RuntimeError("exchange rejected stop")

    async def close(*args, **kwargs):
        calls.append("close")
        return True

    monkeypatch.setattr(manager, "_place_stop_loss_maker", fail_sl)
    monkeypatch.setattr(manager, "_close_position", close)

    result = await manager._v1460_cancel_confirm_entry(
        _entry_pending_run(),
        7,
        purpose="unit",
        halt_on_unsafe=True,
    )

    assert result["status"] == "UNSAFE"
    assert result["reason"] == "cancel_race_hard_sl_failed"
    assert calls == ["hard_sl", "close"]
    assert session["stop_requested"] is True
    assert session["safety_halt_reason"] == "entry_cancel_race_hard_sl_failed"


@pytest.mark.asyncio
async def test_v1460_cancel_race_hard_sl_evidence_failure_halts_and_closes(
    monkeypatch,
) -> None:
    client = FakeClient()
    client.position = _position()
    client.user_trades = [SimpleNamespace(order_id=7, time_ms=15_000)]
    repo = FakeRepo()
    manager = _manager(client=client, repo=repo)
    session = _session(manager)
    calls: list[str] = []
    original_log_event = repo.log_event

    async def fail_armed_event(run_id, event, details):
        if event == "v1460_entry_cancel_race_hard_sl_armed":
            raise RuntimeError("evidence store unavailable")
        await original_log_event(run_id, event, details)

    async def close(*args, **kwargs):
        calls.append("close")
        return True

    monkeypatch.setattr(repo, "log_event", fail_armed_event)
    monkeypatch.setattr(manager, "_close_position", close)

    result = await manager._v1460_cancel_confirm_entry(
        _entry_pending_run(),
        7,
        purpose="unit",
        halt_on_unsafe=True,
    )

    assert result["status"] == "UNSAFE"
    assert calls == ["close"]
    assert len(client.stop_market_sl_orders) == 1
    assert session["stop_requested"] is True
    assert session["safety_halt_reason"] == "entry_cancel_race_hard_sl_failed"


@pytest.mark.asyncio
async def test_v1460_late_fill_arms_hard_sl_before_close(monkeypatch) -> None:
    manager = _manager()
    session = _session(manager)
    order: list[str] = []

    async def place_sl(**kwargs):
        order.append("hard_sl")

    async def close(*args, **kwargs):
        order.append("close")

    monkeypatch.setattr(manager, "_place_stop_loss_maker", place_sl)
    monkeypatch.setattr(manager, "_close_position", close)
    run = _adaptive_run(
        status="ENTRY_PENDING",
        signal_json=json.dumps(
            {
                "stop_loss": 100.08,
                "side": "SHORT",
                "codex_v1": {
                    "enabled": True,
                    "lane_code": "STUP-S",
                    "metrics": {"market_state": "STUP-S:clean_extension"},
                },
            }
        ),
    )

    await manager._handle_entry_fill_after_deadline(
        run,
        _position(),
        7,
        timing={
            "submitted_at_ms": 10_000,
            "entry_deadline_ms": 20_000,
            "effective_deadline_ms": 20_000,
            "ttl_seconds": 10,
            "ttl_source": "unit",
            "lane_code": "STUP-S",
            "grace_s": 0.0,
        },
        fill_detected_ms=21_000,
        actual_fill_ms=21_000,
        detection_source="unit",
    )

    assert order == ["hard_sl", "close"]
    assert session["stop_requested"] is True
    assert session["safety_halt_reason"] == "entry_late_fill_ttl"


def test_v1460_restart_restores_lane_risk_and_weak_evidence(monkeypatch) -> None:
    manager = _manager()
    restored_snapshot = {
        "session_id": "adaptive-v1460-test",
        "route_stats": {
            "v1460_lane_state_loss_streaks": {
                "S1P-L|S1P-L:ordinary_pullback": 2
            },
            "v1460_lane_state_net_pnl_usdc": {
                "S1P-L|S1P-L:ordinary_pullback": -0.08
            },
            "v1460_isolated_keys": ["S1P-L|S1P-L:ordinary_pullback"],
            "v1460_weak_evidence": {
                "opportunities": 8,
                "evaluable": 8,
                "tp_first": 6,
                "net_pnl_usdc": 0.08,
                "data_complete": True,
                "opportunity_keys": [f"opp-{index}" for index in range(8)],
            },
        },
    }
    monkeypatch.setattr(
        manager._v1459_guard,
        "restored_session",
        lambda: restored_snapshot,
    )

    restored = manager._v1459_restored_adaptive_session(
        _adaptive_run(),
        {
            "session_id": "adaptive-v1460-test",
            "deadline_at_ms": 999_000,
            "config_sha": manager._adaptive_config_sha(),
        },
    )

    assert restored is not None
    key = "S1P-L|S1P-L:ordinary_pullback"
    assert restored["v1460_lane_state_loss_streaks"][key] == 2
    assert restored["v1460_lane_state_net_pnl_usdc"][key] == pytest.approx(-0.08)
    assert restored["v1460_isolated_keys"] == {key}
    assert restored["v1460_weak_evidence"]["tp_first"] == 6
    assert restored["restart_recovered"] is True
    assert restored["rearm_enabled"] is False


@pytest.mark.asyncio
async def test_v1460_two_complete_losses_isolate_only_that_lane_state(
    monkeypatch,
) -> None:
    repo = FakeRepo()
    manager = _manager(repo=repo)
    session = _session(manager)

    async def no_rearm() -> None:
        return None

    monkeypatch.setattr(manager, "_arm_adaptive_run", no_rearm)
    first = _terminal_run("cry3mn_v1460_loss_1")
    second = _terminal_run("cry3mn_v1460_loss_2")

    await manager._adaptive_after_terminal(first, -0.04, "SL")
    await manager._adaptive_after_terminal(second, -0.04, "SL")

    key = "S1P-L|S1P-L:ordinary_pullback"
    assert session["v1460_lane_state_loss_streaks"][key] == 2
    assert session["v1460_lane_state_net_pnl_usdc"][key] == pytest.approx(-0.08)
    assert session["v1460_isolated_keys"] == {key}
    assert session["stop_requested"] is False
    assert any(
        event == "v1460_lane_state_isolated"
        and details["reason"] == "consecutive_net_losses"
        for _, event, details in repo.events
    )


@pytest.mark.asyncio
async def test_v1460_lane_net_cap_isolates_before_two_losses(monkeypatch) -> None:
    repo = FakeRepo()
    manager = _manager(repo=repo)
    session = _session(manager)

    async def no_rearm() -> None:
        return None

    monkeypatch.setattr(manager, "_arm_adaptive_run", no_rearm)
    await manager._adaptive_after_terminal(
        _terminal_run("cry3mn_v1460_lane_cap"),
        -0.12,
        "SL",
    )

    key = "S1P-L|S1P-L:ordinary_pullback"
    assert session["v1460_isolated_keys"] == {key}
    assert session["stop_requested"] is False
    assert any(
        event == "v1460_lane_state_isolated"
        and details["reason"] == "lane_net_loss_cap"
        for _, event, details in repo.events
    )


@pytest.mark.asyncio
async def test_v1460_global_net_cap_stops_entire_cohort(monkeypatch) -> None:
    manager = _manager()
    session = _session(manager)
    stopped: list[str] = []

    async def capture_stop(run, reason, *, unexpected):
        stopped.append(reason)
        session["stop_requested"] = True
        session["rearm_enabled"] = False

    monkeypatch.setattr(manager, "_stop_adaptive_session", capture_stop)

    await manager._adaptive_after_terminal(
        _terminal_run("cry3mn_v1460_global_cap"),
        -0.30,
        "SL",
    )

    assert stopped == ["net_loss_cap"]
    assert session["net_pnl_usdc"] == pytest.approx(-0.30)
    assert session["stop_requested"] is True
    assert session["rearm_enabled"] is False


@pytest.mark.parametrize(
    "flag",
    [
        "mainnet_codex_v1460_runner_enabled",
        "mainnet_codex_v1460_one_step_reprice_enabled",
    ],
)
def test_v1460_reviewed_readiness_keeps_runner_and_reprice_closed(flag: str) -> None:
    manager = _manager(settings=_v1460_settings(**{flag: True}))

    requested, missing = manager._v1460_enforcement_readiness()

    assert requested is True
    assert f"{flag}=false" in missing


@pytest.mark.parametrize(
    ("setting_name", "invalid_value", "expected_value"),
    [
        ("mainnet_codex_v1460_target_paid_closed_fills", 19, 20),
        ("mainnet_codex_v1460_max_duration_seconds", 72 * 60 * 60 + 1, 72 * 60 * 60),
        ("mainnet_codex_v1460_checkpoint_fills", 10, 5),
    ],
)
def test_v1460_reviewed_readiness_freezes_live_cohort_protocol(
    setting_name: str,
    invalid_value: int,
    expected_value: int,
) -> None:
    manager = _manager(
        settings=_v1460_settings(**{setting_name: invalid_value})
    )

    requested, missing = manager._v1460_enforcement_readiness()

    assert requested is True
    assert f"{setting_name}={expected_value}" in missing


def test_v1460_checkpoint_schedule_is_part_of_cohort_identity() -> None:
    baseline = _manager()
    changed = _manager(
        settings=_v1460_settings(mainnet_codex_v1460_checkpoint_fills=10)
    )

    assert baseline._adaptive_config_sha() != changed._adaptive_config_sha()


@pytest.mark.asyncio
async def test_v1460_replace_failure_leaves_no_order_and_never_moves_deadline(
    monkeypatch,
) -> None:
    settings = _v1460_settings(
        mainnet_codex_v1460_one_step_reprice_enabled=True,
        mainnet_entry_requote_min_age_seconds=1,
        mainnet_entry_reprice_interval_seconds=0,
        mainnet_entry_max_deviation_bps=2.0,
        mainnet_entry_slippage_bps=8.0,
    )
    client = FakeClient()
    client.book = {"bidPrice": "99.00", "askPrice": "99.01"}
    repo = FakeRepo()
    manager = _manager(settings=settings, client=client, repo=repo)
    _session(manager)
    run = _adaptive_run(
        side="SELL",
        armed_at_ms=40_000,
        cumulative_notional_usdc=50.0,
        entry_client_order_id="cry3mn_v1460_entry",
        signal_json=json.dumps(
            {
                "entry_submitted_at_ms": 50_000,
                "entry_deadline_ms": 140_000,
                "codex_v1": {
                    "enabled": True,
                    "lane_code": "STUP-S",
                    "metrics": {"market_state": "STUP-S:clean_extension"},
                },
            }
        ),
    )
    existing = {
        "orderId": 7,
        "clientOrderId": run["entry_client_order_id"],
        "price": "100.00",
        "status": "NEW",
    }
    client.open_orders = [dict(existing)]
    monkeypatch.setattr(one_run_module.time, "time", lambda: 100.0)
    monkeypatch.setattr(manager, "_v1460_entry_safety_active", lambda _run: True)

    async def no_profile(*args, **kwargs):
        return None

    async def fail_replace(**kwargs):
        raise GTXSlippageExceeded("replacement rejected")

    monkeypatch.setattr(manager, "_v1459_observe_entry_profile", no_profile)
    monkeypatch.setattr(manager, "_place_post_only_with_retry", fail_replace)
    before = manager._entry_timing_snapshot(run, now_ms=100_000)

    result = await manager._maybe_requote_entry(run, 7, list(client.open_orders))
    after = manager._entry_timing_snapshot(run, now_ms=130_000)

    assert result is False
    assert client.cancelled == [("ETHUSDC", 7)]
    assert client.open_orders == []
    assert manager._entry_requote_counts.get(run["run_id"], 0) == 0
    assert before["entry_deadline_ms"] == after["entry_deadline_ms"] == 140_000
    assert not repo.updated
    assert any(
        event == "entry_requote_skipped" and details["reason"] == "gtx_slippage"
        for _, event, details in repo.events
    )
