import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.gridbot.mainnet.one_run as one_run_module
from src.gridbot.binance.models import PositionInfo
from src.gridbot.mainnet.one_run import MainnetOneRunManager
from src.gridbot.mainnet.v1459_adaptive_profiles import (
    EntryProfile,
    ExitPolicyInput,
    ExitProfile,
    select_exit_profile,
)
from src.gridbot.mainnet.v1459_profile_runtime import (
    build_profile_config,
)
from src.gridbot.strategy.codex_v1_live import CodexV1Decision
from src.gridbot.strategy.long_pullback import SignalPlan
from src.gridbot.strategy.wildcat_live import WildcatLiveDecision
from tests.test_mainnet_one_run_maker import (
    FakeClient,
    FakeRepo,
    FakeTelegramApp,
    _V1459FakeRuntime,
    _run,
    _settings,
)


class _ReadyProfileRuntime(_V1459FakeRuntime):
    """Minimal observation runtime with both enforcement hooks enabled."""

    def __init__(self) -> None:
        super().__init__()
        self.flags = SimpleNamespace(record_reconciliation=True)


class _AlgoQueryMustNotRunClient(FakeClient):
    async def get_open_algo_orders(self, symbol):
        raise AssertionError(f"unexpected get_open_algo_orders({symbol})")


class _FailingAlgoQueryClient(FakeClient):
    async def get_open_algo_orders(self, symbol):
        raise RuntimeError(f"open algo query failed for {symbol}")


def _adaptive_codex_run(run_id: str = "cry3mn_profile"):
    return _run(
        run_id=run_id,
        status="RUNNING",
        params={
            "mode": "adaptive_continuous",
            "adaptive": {
                "mode": "adaptive_continuous",
                "session_id": "adaptive_profile_test",
            },
        },
        signal_json=(
            '{"side":"LONG","codex_v1":'
            '{"enabled":true,"lane_code":"S1P-L"}}'
        ),
        avg_entry_price=100.0,
    )


def _enforcement_settings(**overrides):
    values = {
        "mainnet_codex_v1459_candidate_selector_enabled": True,
        "mainnet_codex_v1459_live_enforcement_enabled": True,
        "mainnet_v1459_observation_enabled": True,
        "mainnet_v1459_observation_persist_session_enabled": True,
        "mainnet_v1459_observation_record_opportunities_enabled": True,
        "mainnet_v1459_observation_record_reconciliation_enabled": True,
    }
    values.update(overrides)
    return _settings(**values)


def _exit_facts(**overrides):
    facts = {
        "position_open": True,
        "hard_sl_present": True,
        "early_window_open": False,
        "minimum_mfe_met": True,
        "adverse_markout": False,
        "direction_still_valid": True,
        "causal_mfe_covers_cost": True,
        "follow_through_valid": True,
        "runner_guards_present": True,
    }
    facts.update(overrides)
    return facts


def _exit_decision(settings, **fact_overrides):
    facts = _exit_facts(**fact_overrides)
    decision = select_exit_profile(
        ExitPolicyInput(**facts),
        build_profile_config(settings),
    )
    return decision, facts


def _position(
    position_amt: float = 0.12,
    *,
    mark_price: float = 99.97,
    unrealized_pnl: float = -0.004,
) -> PositionInfo:
    return PositionInfo(
        symbol="ETHUSDC",
        position_amt=position_amt,
        entry_price=100.0,
        mark_price=mark_price,
        unrealized_pnl=unrealized_pnl,
        liquidation_price=80.0,
        leverage=75,
        margin_type="cross",
    )


def _owned_hard_sl_order(**overrides):
    order = {
        "clientAlgoId": "cry3mn_profile_sl",
        "orderType": "STOP_MARKET",
        "reduceOnly": True,
        "algoStatus": "NEW",
        "symbol": "ETHUSDC",
        "side": "SELL",
        "quantity": "0.120",
    }
    order.update(overrides)
    return order


def _regime_decision(
    market_state: str = "STUP-S:clean_extension",
    *,
    lane_code: str = "STUP-S",
    side: str = "SHORT",
    metrics: dict | None = None,
) -> CodexV1Decision:
    baseline_metrics = {
        "market_state": market_state,
        "v1455_action": "S_E2_TP10_SL8_T90",
        "v1455_route": "THIN_SCALP",
        "tp1_bp": 11.0,
        "full_tp_bp": 13.0,
        "partial_exit_pct": 0.40,
        "sl_bp": 7.0,
        "be_bp": 1.0,
        "ttl_s": 123,
        "hold_s": 456,
        "maker_mode": "INCUMBENT",
        "exit_mode": "INCUMBENT",
        "incumbent": True,
    }
    baseline_metrics.update(metrics or {})
    return CodexV1Decision(
        accepted=True,
        version="_codex_v1.4.59_integration",
        baseline="unit",
        lane=f"unit_{lane_code.lower()}",
        lane_code=lane_code,
        strategy="S2_SuperTrend",
        side=side,
        entry_offset_bp=9.0,
        size_mult=0.40,
        notional_mult=0.40,
        requested_notional_usdc=20.0,
        reason="incumbent",
        regime=market_state,
        metrics=baseline_metrics,
        policy_tag="incumbent-policy",
    )


def _regime_run(
    decision: CodexV1Decision | None = None,
    *,
    run_id: str = "cry3mn_v1459_regime",
    status: str = "ENTRY_PENDING",
):
    decision = decision or _regime_decision()
    signal = {
        "side": decision.side,
        "take_profit": 99.87 if decision.side == "SHORT" else 100.13,
        "stop_loss": 100.07 if decision.side == "SHORT" else 99.93,
        "wildcat": {
            "tp_pct": 0.0013,
            "sl_pct": 0.0007,
            "partial_exit_pct": 0.40,
        },
        "codex_v1": {
            "enabled": True,
            "lane_code": decision.lane_code,
            "regime": decision.regime,
            "entry_offset_bp": decision.entry_offset_bp,
            "size_mult": decision.size_mult,
            "notional_mult": decision.notional_mult,
            "requested_notional_usdc": decision.requested_notional_usdc,
            "metrics": dict(decision.metrics),
        },
    }
    return _run(
        run_id=run_id,
        status=status,
        side=decision.side,
        params={
            "mode": "adaptive_continuous",
            "adaptive": {
                "mode": "adaptive_continuous",
                "session_id": "adaptive_regime_test",
            },
        },
        signal_json=json.dumps(signal),
        qty=0.5,
        avg_entry_price=100.0,
        cumulative_notional_usdc=decision.requested_notional_usdc,
    )


def _wildcat_decision() -> WildcatLiveDecision:
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
        take_profits=[99.9],
        planned_notional_usdc=20.0,
        planned_margin_usdc=20.0 / 75.0,
        planned_qty=0.2,
        reasons=["wildcat:S2_SuperTrend"],
        risk_notes=[],
    )
    return WildcatLiveDecision(
        signal=signal,
        strategy="S2_SuperTrend",
        side="SHORT",
        tp_pct=0.0013,
        sl_pct=0.0007,
        partial_exit_pct=0.40,
        partial_tp_pct=0.0011,
        recovery_steps=0,
        recovery_trigger_pct=0.0005,
        recovery_tp_shrink=0.45,
        adverse_exit_bars=10,
        adverse_exit_loss_pct=0.0007,
        max_holding_bars=24,
        params_label="v1459-regime-integration",
    )


def test_v1459_production_flags_use_observation_stage_defaults():
    settings = _settings()

    assert {
        "candidate_selector": settings.mainnet_codex_v1459_candidate_selector_enabled,
        "live_enforcement": settings.mainnet_codex_v1459_live_enforcement_enabled,
        "runner": settings.mainnet_codex_v1459_runner_enabled,
        "early_fail": settings.mainnet_codex_v1459_early_fail_enabled,
        "one_step_reprice": settings.mainnet_codex_v1459_one_step_reprice_enabled,
        "regime_switch": settings.mainnet_codex_v1459_regime_switch_enabled,
    } == {
        "candidate_selector": True,
        "live_enforcement": False,
        "runner": False,
        "early_fail": False,
        "one_step_reprice": False,
        "regime_switch": False,
    }


@pytest.mark.asyncio
async def test_regime_candidate_only_adds_audit_without_action(
    monkeypatch,
):
    settings = _settings(
        mainnet_codex_v1459_candidate_selector_enabled=True,
        mainnet_codex_v1459_regime_switch_enabled=True,
        mainnet_codex_v1459_live_enforcement_enabled=False,
    )
    client = FakeClient()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        settings,
        client,
        repo,
        FakeTelegramApp(),
    )
    decision = _regime_decision()
    run = _regime_run(decision, run_id="cry3mn_regime_candidate")
    clock = {"seconds": 0.0}
    monkeypatch.setattr(
        one_run_module.time,
        "time",
        lambda: clock["seconds"],
    )

    pending = await manager._v1459_apply_regime_profile(run, decision)
    clock["seconds"] = 15.0
    applied = await manager._v1459_apply_regime_profile(run, decision)

    assert replace(pending, metrics=decision.metrics) == decision
    assert pending.metrics["v1459_regime_state"] == "UNCERTAIN"
    assert replace(applied, metrics=decision.metrics) == decision
    assert applied.metrics["v1459_regime_mode"] == "candidate-only"
    assert applied.metrics["v1459_regime_state"] == "TREND_UP"
    assert applied.metrics["v1459_regime_profile"] == "TREND_RUNNER"
    _assert_no_order_mutation(client, [])


@pytest.mark.asyncio
async def test_regime_enforcement_after_confirmation_and_dwell_changes_actual_actions(
    monkeypatch,
):
    settings = _enforcement_settings(
        mainnet_codex_v1459_regime_switch_enabled=True,
        mainnet_codex_v1459_one_step_reprice_enabled=True,
        mainnet_codex_v1459_runner_enabled=True,
        mainnet_codex_v1459_early_fail_enabled=True,
    )
    trend_manager = MainnetOneRunManager(
        settings,
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
        observation_runtime=_ReadyProfileRuntime(),
    )
    range_manager = MainnetOneRunManager(
        settings,
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
        observation_runtime=_ReadyProfileRuntime(),
    )
    trend_decision = _regime_decision("STUP-S:clean_extension")
    range_decision = _regime_decision("STUP-S:weak_chop")
    trend_run = _regime_run(
        trend_decision,
        run_id="cry3mn_regime_trend",
    )
    range_run = _regime_run(
        range_decision,
        run_id="cry3mn_regime_range",
    )
    clock = {"seconds": 0.0}
    monkeypatch.setattr(
        one_run_module.time,
        "time",
        lambda: clock["seconds"],
    )

    trend_pending = await trend_manager._v1459_apply_regime_profile(
        trend_run,
        trend_decision,
    )
    clock["seconds"] = 15.0
    trend = await trend_manager._v1459_apply_regime_profile(
        trend_run,
        trend_decision,
    )
    clock["seconds"] = 0.0
    range_pending = await range_manager._v1459_apply_regime_profile(
        range_run,
        range_decision,
    )
    clock["seconds"] = 15.0
    range_ = await range_manager._v1459_apply_regime_profile(
        range_run,
        range_decision,
    )

    assert replace(
        trend_pending,
        metrics=trend_decision.metrics,
    ) == trend_decision
    assert trend_pending.metrics["v1459_regime_state"] == "UNCERTAIN"
    assert replace(
        range_pending,
        metrics=range_decision.metrics,
    ) == range_decision
    assert range_pending.metrics["v1459_regime_state"] == "UNCERTAIN"

    assert {
        "state": trend.metrics["v1459_regime_state"],
        "profile": trend.metrics["v1459_regime_profile"],
        "entry_bp": trend.metrics["entry_bp"],
        "tp1_bp": trend.metrics["tp1_bp"],
        "full_tp_bp": trend.metrics["full_tp_bp"],
        "partial_exit_pct": trend.metrics["partial_exit_pct"],
        "sl_bp": trend.metrics["sl_bp"],
        "ttl_s": trend.metrics["ttl_s"],
        "hold_s": trend.metrics["hold_s"],
        "notional_usdc": trend.requested_notional_usdc,
        "maker_mode": trend.metrics["maker_mode"],
        "exit_mode": trend.metrics["exit_mode"],
    } == {
        "state": "TREND_UP",
        "profile": "TREND_RUNNER",
        "entry_bp": 2.0,
        "tp1_bp": 6.0,
        "full_tp_bp": 16.0,
        "partial_exit_pct": 0.70,
        "sl_bp": 10.0,
        "ttl_s": 60,
        "hold_s": 720,
        "notional_usdc": 50.0,
        "maker_mode": "ONE_STEP_REPRICE",
        "exit_mode": "RUNNER",
    }
    assert {
        "state": range_.metrics["v1459_regime_state"],
        "profile": range_.metrics["v1459_regime_profile"],
        "entry_bp": range_.metrics["entry_bp"],
        "tp1_bp": range_.metrics["tp1_bp"],
        "full_tp_bp": range_.metrics["full_tp_bp"],
        "partial_exit_pct": range_.metrics["partial_exit_pct"],
        "sl_bp": range_.metrics["sl_bp"],
        "ttl_s": range_.metrics["ttl_s"],
        "hold_s": range_.metrics["hold_s"],
        "notional_usdc": range_.requested_notional_usdc,
        "maker_mode": range_.metrics["maker_mode"],
        "exit_mode": range_.metrics["exit_mode"],
    } == {
        "state": "RANGE",
        "profile": "RANGE_SCALP",
        "entry_bp": 1.0,
        "tp1_bp": 5.0,
        "full_tp_bp": 8.0,
        "partial_exit_pct": 1.0,
        "sl_bp": 8.0,
        "ttl_s": 90,
        "hold_s": 360,
        "notional_usdc": 37.5,
        "maker_mode": "PASSIVE",
        "exit_mode": "EARLY_FAIL",
    }

    trend_live = trend_manager._apply_codex_v1_decision(
        _wildcat_decision(),
        trend,
    )
    range_live = range_manager._apply_codex_v1_decision(
        _wildcat_decision(),
        range_,
    )
    assert trend_live.signal.entries[0] == pytest.approx(100.02)
    assert range_live.signal.entries[0] == pytest.approx(100.01)
    assert trend_live.partial_tp_pct == pytest.approx(0.0006)
    assert range_live.partial_tp_pct == pytest.approx(0.0005)
    assert trend_live.partial_exit_pct == pytest.approx(0.70)
    assert range_live.partial_exit_pct == pytest.approx(1.0)
    assert trend_live.tp_pct == pytest.approx(0.0016)
    assert range_live.tp_pct == pytest.approx(0.0008)
    assert trend_live.sl_pct == pytest.approx(0.0010)
    assert range_live.sl_pct == pytest.approx(0.0008)
    assert trend_live.signal.planned_notional_usdc == pytest.approx(50.0)
    assert range_live.signal.planned_notional_usdc == pytest.approx(37.5)
    assert trend_live.signal.take_profits[0] != pytest.approx(
        range_live.signal.take_profits[0]
    )
    assert trend_live.signal.stop_loss != pytest.approx(
        range_live.signal.stop_loss
    )

    enforced_trend_run = _regime_run(trend)
    enforced_range_run = _regime_run(range_)
    trend_signal = json.loads(enforced_trend_run["signal_json"])
    range_signal = json.loads(enforced_range_run["signal_json"])
    assert trend_manager._codex_v1_live_entry_ttl_policy(
        enforced_trend_run
    )["ttl_seconds"] == 60
    assert range_manager._codex_v1_live_entry_ttl_policy(
        enforced_range_run
    )["ttl_seconds"] == 90
    assert trend_manager._max_holding_bars_for_run(trend_signal) == 12
    assert range_manager._max_holding_bars_for_run(range_signal) == 6


@pytest.mark.asyncio
async def test_regime_enforcement_shock_blocks_and_uncertain_falls_back(
    monkeypatch,
):
    settings = _enforcement_settings(
        mainnet_codex_v1459_regime_switch_enabled=True,
    )
    shock_client = FakeClient()
    shock_repo = FakeRepo()
    shock_manager = MainnetOneRunManager(
        settings,
        shock_client,
        shock_repo,
        FakeTelegramApp(),
        observation_runtime=_ReadyProfileRuntime(),
    )
    uncertain_client = FakeClient()
    uncertain_manager = MainnetOneRunManager(
        settings,
        uncertain_client,
        FakeRepo(),
        FakeTelegramApp(),
        observation_runtime=_ReadyProfileRuntime(),
    )
    clock = {"seconds": 0.0}
    monkeypatch.setattr(
        one_run_module.time,
        "time",
        lambda: clock["seconds"],
    )
    shock_decision = _regime_decision("STUP-S:counter_recoil")
    uncertain_decision = _regime_decision("future:unrecognized_state")

    shock = await shock_manager._v1459_apply_regime_profile(
        _regime_run(shock_decision, run_id="cry3mn_regime_shock"),
        shock_decision,
    )
    uncertain = await uncertain_manager._v1459_apply_regime_profile(
        _regime_run(
            uncertain_decision,
            run_id="cry3mn_regime_uncertain",
        ),
        uncertain_decision,
    )

    assert shock.accepted is False
    assert shock.entry_offset_bp is None
    assert shock.size_mult == 0.0
    assert shock.notional_mult == 0.0
    assert shock.requested_notional_usdc == 0.0
    assert shock.reason == "v1.4.59_regime_overlay:shock_block"
    assert shock.metrics["v1459_regime_state"] == "SHOCK"
    shock_event = next(
        details
        for _, event_type, details in shock_repo.events
        if event_type == "v1459_regime_profile_transition"
    )
    assert shock_event["profile_actions"]["maker_mode"] == "BLOCK"
    assert shock_event["profile_actions"]["exit_mode"] == "RISK_OFF"
    _assert_no_order_mutation(shock_client, [])

    assert replace(
        uncertain,
        metrics=uncertain_decision.metrics,
    ) == uncertain_decision
    assert uncertain.metrics["v1459_regime_mode"] == "enforcement"
    assert uncertain.metrics["v1459_regime_state"] == "UNCERTAIN"
    assert (
        uncertain.metrics["v1459_regime_profile"]
        == "INCUMBENT_FALLBACK"
    )
    _assert_no_order_mutation(uncertain_client, [])


@pytest.mark.asyncio
async def test_candidate_flag_off_short_circuits_algo_io_and_profile_events():
    settings = _settings(
        mainnet_codex_v1459_candidate_selector_enabled=False,
        mainnet_codex_v1459_live_enforcement_enabled=False,
    )
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        settings,
        _AlgoQueryMustNotRunClient(),
        repo,
        FakeTelegramApp(),
    )
    run = _adaptive_codex_run()
    decision, facts = _exit_decision(settings)

    assert manager._v1459_profile_candidate_active(run) is False
    assert await manager._v1459_owned_hard_sl_present(
        run,
        _position(),
    ) is False
    assert await manager._v1459_apply_exit_profile(
        run,
        decision=decision,
        incumbent_reason="INCUMBENT_EXIT",
        facts=facts,
    ) == "INCUMBENT_EXIT"
    assert not any(
        event_type == "v1459_profile_transition"
        for _, event_type, _ in repo.events
    )


@pytest.mark.asyncio
async def test_live_enforcement_with_incomplete_evidence_does_not_create_run():
    settings = _settings(
        mainnet_codex_v1459_candidate_selector_enabled=True,
        mainnet_codex_v1459_live_enforcement_enabled=True,
        mainnet_codex_v1459_runner_enabled=True,
    )
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        settings,
        FakeClient(),
        repo,
        FakeTelegramApp(),
        observation_runtime=None,
    )

    readiness = manager._v1459_profile_enforcement_readiness()
    result = await manager.start_adaptive_session()

    assert readiness.enforcement_requested is True
    assert readiness.ready is False
    assert repo.created == []
    assert manager._adaptive_session is None
    assert "Adaptive continuous session" not in result


@pytest.mark.parametrize(
    ("algo_order", "expected"),
    (
        (_owned_hard_sl_order(), True),
        (
            _owned_hard_sl_order(
                clientAlgoId="cry3mn_profile_sl_extra",
            ),
            False,
        ),
        (_owned_hard_sl_order(reduceOnly=False), False),
        (_owned_hard_sl_order(algoStatus="CANCELED"), False),
        (_owned_hard_sl_order(orderType="STOP"), False),
        (_owned_hard_sl_order(side="BUY"), False),
        (_owned_hard_sl_order(quantity="0.119"), False),
        (_owned_hard_sl_order(algoStatus="UNKNOWN"), False),
    ),
    ids=(
        "fully-proven",
        "non-exact-id",
        "not-reduce-only",
        "closed-status",
        "wrong-order-type",
        "wrong-close-side",
        "insufficient-quantity",
        "unknown-status",
    ),
)
@pytest.mark.asyncio
async def test_owned_hard_sl_requires_complete_exchange_proof(
    algo_order,
    expected,
):
    client = FakeClient()
    client.algo_orders = [algo_order]
    manager = MainnetOneRunManager(
        _settings(mainnet_codex_v1459_candidate_selector_enabled=True),
        client,
        FakeRepo(),
        FakeTelegramApp(),
    )

    assert await manager._v1459_owned_hard_sl_present(
        _adaptive_codex_run(),
        _position(),
    ) is expected


@pytest.mark.asyncio
async def test_owned_hard_sl_query_failure_fails_closed():
    manager = MainnetOneRunManager(
        _settings(mainnet_codex_v1459_candidate_selector_enabled=True),
        _FailingAlgoQueryClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )

    assert await manager._v1459_owned_hard_sl_present(
        _adaptive_codex_run(),
        _position(),
    ) is False


@pytest.mark.asyncio
async def test_runner_soft_reason_gets_only_one_enforced_extension():
    settings = _enforcement_settings(
        mainnet_codex_v1459_runner_enabled=True,
        mainnet_codex_v1459_early_fail_enabled=False,
    )
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        settings,
        FakeClient(),
        repo,
        FakeTelegramApp(),
        observation_runtime=_ReadyProfileRuntime(),
    )
    run = _adaptive_codex_run()
    decision, facts = _exit_decision(settings)
    soft_reasons = getattr(
        one_run_module,
        "V1459_RUNNER_SOFT_EXIT_REASONS",
        (),
    )

    assert decision.profile is ExitProfile.RUNNER
    assert manager._v1459_profile_enforcement_active() is True
    assert soft_reasons, "production must define at least one RUNNER soft reason"
    incumbent_reason = sorted(soft_reasons)[0]

    first = await manager._v1459_apply_exit_profile(
        run,
        decision=decision,
        incumbent_reason=incumbent_reason,
        facts=facts,
    )
    second = await manager._v1459_apply_exit_profile(
        run,
        decision=decision,
        incumbent_reason=incumbent_reason,
        facts=facts,
    )

    assert first is None
    assert second == incumbent_reason


@pytest.mark.asyncio
async def test_runner_preserves_reason_and_extension_in_risk_reduction_only(
    monkeypatch,
):
    settings = _enforcement_settings(
        mainnet_codex_v1459_runner_enabled=True,
        mainnet_codex_v1459_early_fail_enabled=False,
    )
    manager = MainnetOneRunManager(
        settings,
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
        observation_runtime=_ReadyProfileRuntime(),
    )
    run = _adaptive_codex_run()
    decision, facts = _exit_decision(settings)
    incumbent_reason = sorted(
        one_run_module.V1459_RUNNER_SOFT_EXIT_REASONS
    )[0]
    monkeypatch.setattr(
        manager,
        "_v1459_cycle_mode",
        lambda active: "RISK_REDUCTION_ONLY",
    )
    extensions_before = set(manager._v1459_runner_extension_used)

    result = await manager._v1459_apply_exit_profile(
        run,
        decision=decision,
        incumbent_reason=incumbent_reason,
        facts=facts,
    )

    assert decision.profile is ExitProfile.RUNNER
    assert result == incumbent_reason
    assert manager._v1459_runner_extension_used == extensions_before


@pytest.mark.parametrize(
    "lane_code",
    ("STUP-S", "CNL-WPR-L", "W6A"),
)
@pytest.mark.asyncio
async def test_exit_mode_is_lane_agnostic_runner_only_and_early_fail_safe(
    lane_code,
):
    settings = _enforcement_settings(
        mainnet_codex_v1459_runner_enabled=True,
        mainnet_codex_v1459_early_fail_enabled=True,
    )
    manager = MainnetOneRunManager(
        settings,
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
        observation_runtime=_ReadyProfileRuntime(),
    )
    runner_decision, runner_facts = _exit_decision(settings)
    incumbent_reason = sorted(
        one_run_module.V1459_RUNNER_SOFT_EXIT_REASONS
    )[0]
    run_suffix = lane_code.lower().replace("-", "_")
    runner_run = _regime_run(
        _regime_decision(
            lane_code=lane_code,
            metrics={
                "v1459_regime_mode": "enforcement",
                "v1459_regime_state": "TREND_UP",
                "exit_mode": "RUNNER",
            },
        ),
        run_id=f"cry3mn_exit_runner_{run_suffix}",
        status="RUNNING",
    )
    non_runner_run = _regime_run(
        _regime_decision(
            "STUP-S:weak_chop",
            lane_code=lane_code,
            metrics={
                "v1459_regime_mode": "enforcement",
                "v1459_regime_state": "RANGE",
                "exit_mode": "EARLY_FAIL",
            },
        ),
        run_id=f"cry3mn_exit_range_{run_suffix}",
        status="RUNNING",
    )
    early_decision, early_facts = _exit_decision(
        settings,
        early_window_open=True,
        minimum_mfe_met=False,
        adverse_markout=True,
        direction_still_valid=False,
    )
    early_run = _regime_run(
        _regime_decision(
            "STUP-S:weak_chop",
            lane_code=lane_code,
            metrics={
                "v1459_regime_mode": "enforcement",
                "v1459_regime_state": "RANGE",
                "exit_mode": "EARLY_FAIL",
            },
        ),
        run_id=f"cry3mn_exit_early_{run_suffix}",
        status="RUNNING",
    )

    runner_result = await manager._v1459_apply_exit_profile(
        runner_run,
        decision=runner_decision,
        incumbent_reason=incumbent_reason,
        facts=runner_facts,
    )
    non_runner_result = await manager._v1459_apply_exit_profile(
        non_runner_run,
        decision=runner_decision,
        incumbent_reason=incumbent_reason,
        facts=runner_facts,
    )
    early_result = await manager._v1459_apply_exit_profile(
        early_run,
        decision=early_decision,
        incumbent_reason=incumbent_reason,
        facts=early_facts,
    )

    assert runner_decision.profile is ExitProfile.RUNNER
    assert runner_result is None
    assert non_runner_result == incumbent_reason
    assert early_decision.profile is ExitProfile.EARLY_FAIL
    assert early_result == "CODEX_EARLY_FAIL"


def _adaptive_running_exit_run():
    run = _adaptive_codex_run()
    signal = json.loads(run["signal_json"])
    signal.update(
        {
            "side": "LONG",
            "stop_loss": 99.0,
            "take_profit": 101.0,
            "wildcat": {"tp_pct": 0.001},
        }
    )
    signal["codex_v1"]["lane_code"] = "STUP-S"
    run.update(
        {
            "side": "LONG",
            "qty": 0.5,
            "avg_entry_price": 100.0,
            "cumulative_notional_usdc": 50.0,
            "signal_json": json.dumps(signal),
        }
    )
    return run


def _stub_running_manage_before_survival(
    monkeypatch,
    manager,
    *,
    age_bars,
    max_holding_bars,
):
    monkeypatch.setattr(
        manager,
        "_get_hold_start_ms",
        AsyncMock(
            return_value=(
                int(one_run_module.time.time() * 1000)
                - age_bars * 60_000
            )
        ),
    )
    monkeypatch.setattr(
        manager,
        "_refresh_partial_fill_state",
        AsyncMock(),
    )
    monkeypatch.setattr(
        manager,
        "_sync_take_profit_orders",
        AsyncMock(),
    )
    monkeypatch.setattr(
        manager,
        "_maybe_full_tp_touch_lock",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        manager,
        "_maybe_apply_breakeven_sl",
        AsyncMock(),
    )
    monkeypatch.setattr(
        manager,
        "_maybe_recovery",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        manager,
        "_start_trail_watch",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        manager,
        "_maybe_trailing_exit",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        manager,
        "_max_holding_bars_for_run",
        lambda signal: max_holding_bars,
    )


@pytest.mark.parametrize(
    (
        "expected_reason",
        "mark_price",
        "unrealized_pnl",
        "age_bars",
        "adverse_exit_bars",
        "max_holding_bars",
    ),
    (
        ("SL", 98.9, -0.05, 0, 99, 99),
        ("ADVERSE_EXIT", 99.5, -1.1, 2, 1, 99),
        ("MAX_HOLD_LOSS", 99.5, -0.1, 2, 99, 1),
    ),
    ids=("sl-pending", "adverse-pending", "max-hold-pending"),
)
@pytest.mark.asyncio
async def test_enforced_profile_skips_survival_for_pending_hard_exit(
    monkeypatch,
    expected_reason,
    mark_price,
    unrealized_pnl,
    age_bars,
    adverse_exit_bars,
    max_holding_bars,
):
    settings = _enforcement_settings(
        mainnet_codex_v1459_runner_enabled=True,
        mainnet_codex_v1459_early_fail_enabled=False,
        mainnet_adverse_exit_bars=adverse_exit_bars,
        mainnet_adverse_exit_loss_pct=0.02,
    )
    manager = MainnetOneRunManager(
        settings,
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
        observation_runtime=_ReadyProfileRuntime(),
    )
    run = _adaptive_running_exit_run()
    position = _position(
        0.5,
        mark_price=mark_price,
        unrealized_pnl=unrealized_pnl,
    )
    _stub_running_manage_before_survival(
        monkeypatch,
        manager,
        age_bars=age_bars,
        max_holding_bars=max_holding_bars,
    )
    survival = AsyncMock(
        side_effect=AssertionError("pending hard exit must bypass survival")
    )
    close_position = AsyncMock(return_value=True)
    monkeypatch.setattr(manager, "_maybe_codex_survival_exit", survival)
    monkeypatch.setattr(manager, "_close_position", close_position)

    assert manager._v1459_profile_enforcement_active() is True
    await manager._run_running_manage(
        run,
        position,
        "ETHUSDC",
        0.5,
        0.5,
    )

    survival.assert_not_awaited()
    close_position.assert_awaited_once()
    assert close_position.await_args.args[3] == expected_reason


@pytest.mark.asyncio
async def test_flags_off_preserve_incumbent_survival_before_hard_exit(
    monkeypatch,
):
    manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_v1459_candidate_selector_enabled=False,
            mainnet_codex_v1459_live_enforcement_enabled=False,
            mainnet_adverse_exit_bars=99,
        ),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    run = _adaptive_running_exit_run()
    position = _position(0.5, mark_price=98.9, unrealized_pnl=-0.05)
    _stub_running_manage_before_survival(
        monkeypatch,
        manager,
        age_bars=0,
        max_holding_bars=99,
    )
    survival = AsyncMock(return_value=True)
    close_position = AsyncMock(return_value=True)
    monkeypatch.setattr(manager, "_maybe_codex_survival_exit", survival)
    monkeypatch.setattr(manager, "_close_position", close_position)

    await manager._run_running_manage(
        run,
        position,
        "ETHUSDC",
        0.5,
        0.5,
    )

    survival.assert_awaited_once()
    close_position.assert_not_awaited()


@pytest.mark.asyncio
async def test_early_fail_enforcement_uses_codex_reason_and_maker_first():
    settings = _enforcement_settings(
        mainnet_codex_v1459_runner_enabled=False,
        mainnet_codex_v1459_early_fail_enabled=True,
        mainnet_recovery_enabled=False,
        mainnet_codex_survival_enabled=True,
        mainnet_codex_survival_exit_use_maker=True,
        mainnet_codex_survival_exit_maker_ttl_seconds=0,
    )
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
        settings,
        client,
        repo,
        FakeTelegramApp(),
        observation_runtime=_ReadyProfileRuntime(),
    )
    run = _adaptive_codex_run()
    decision, facts = _exit_decision(
        settings,
        early_window_open=True,
        minimum_mfe_met=False,
        adverse_markout=True,
        direction_still_valid=False,
    )

    reason = await manager._v1459_apply_exit_profile(
        run,
        decision=decision,
        incumbent_reason="INCUMBENT_EXIT",
        facts=facts,
    )

    assert decision.profile is ExitProfile.EARLY_FAIL
    assert reason == "CODEX_EARLY_FAIL"

    await manager._close_position("ETHUSDC", "SELL", 0.12, reason, run)

    assert any(order.get("timeInForce") == "GTX" for order in client.all_orders)
    assert any(
        event_type == "survival_maker_attempt"
        for _, event_type, _ in repo.events
    )


def test_one_step_enforcement_readiness_is_no_longer_an_executor_blocker():
    settings = _enforcement_settings(
        mainnet_codex_v1459_one_step_reprice_enabled=True,
    )
    manager = MainnetOneRunManager(
        settings,
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
        observation_runtime=_ReadyProfileRuntime(),
    )

    readiness = manager._v1459_profile_enforcement_readiness()

    assert readiness.enforcement_requested is True
    assert readiness.ready is True
    assert readiness.missing == ()
    assert manager._v1459_profile_enforcement_active() is True


def _adaptive_stup_entry_run(
    action_id: str,
    run_id: str = "cry3mn_profile_entry",
):
    return _run(
        run_id=run_id,
        status="ENTRY_PENDING",
        side="SHORT",
        params={
            "mode": "adaptive_continuous",
            "adaptive": {
                "mode": "adaptive_continuous",
                "session_id": "adaptive_profile_entry_test",
            },
        },
        signal_json=json.dumps(
            {
                "side": "SHORT",
                "codex_v1": {
                    "enabled": True,
                    "lane_code": "STUP-S",
                    "metrics": {"v1455_action": action_id},
                },
            }
        ),
    )


def _entry_order(run_id: str, order_id: int = 741):
    return {
        "orderId": order_id,
        "clientOrderId": f"{run_id}_entry",
        "symbol": "ETHUSDC",
        "side": "SELL",
        "price": "100.00",
        "origQty": "0.500",
        "status": "NEW",
    }


class _OrderTraceClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.order_operations = []

    async def cancel_order(self, symbol, order_id):
        self.order_operations.append(("cancel", order_id))
        return await super().cancel_order(symbol, order_id)

    async def create_limit_order_raw(self, *args, **kwargs):
        self.order_operations.append(
            (
                "place",
                kwargs.get("time_in_force", "GTC"),
                kwargs.get("client_order_id"),
            )
        )
        return await super().create_limit_order_raw(*args, **kwargs)


def _assert_no_order_mutation(client, open_orders_before):
    assert client.open_orders == open_orders_before
    assert client.cancelled == []
    assert client.cancelled_algo == []
    assert client.all_orders == []
    assert client.market_orders == []
    assert client.reduce_only_limit_orders == []
    assert client.stop_market_sl_orders == []
    assert client.stop_limit_sl_orders == []


@pytest.mark.asyncio
async def test_one_step_reprice_enforcement_cancels_then_gtx_once_without_gtc(
    monkeypatch,
):
    settings = _enforcement_settings(
        mainnet_codex_v1459_one_step_reprice_enabled=True,
        mainnet_entry_requote_min_age_seconds=30,
        mainnet_entry_reprice_interval_seconds=0,
        mainnet_entry_reprice_max_updates=5,
        mainnet_entry_max_deviation_bps=2.0,
        mainnet_entry_slippage_bps=8.0,
        mainnet_entry_fallback_to_gtc=True,
        mainnet_gtx_retry_attempts=1,
    )
    client = _OrderTraceClient()
    client.book = {"bidPrice": "99.96", "askPrice": "99.97"}
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        settings,
        client,
        repo,
        FakeTelegramApp(),
        observation_runtime=_ReadyProfileRuntime(),
    )
    decision = _regime_decision(
        metrics={
            "maker_mode": "ONE_STEP_REPRICE",
            "v1459_regime_mode": "enforcement",
            "ttl_s": 120,
        }
    )
    run = _regime_run(
        decision,
        run_id="cry3mn_one_step_enforced",
    )
    run["armed_at_ms"] = 40_000
    existing_order = _entry_order(run["run_id"])
    client.open_orders = [dict(existing_order)]
    clock = {"seconds": 100.0}
    monkeypatch.setattr(
        one_run_module.time,
        "time",
        lambda: clock["seconds"],
    )
    original_place = manager._place_post_only_with_retry
    place_calls = []

    async def traced_place(**kwargs):
        place_calls.append(dict(kwargs))
        return await original_place(**kwargs)

    monkeypatch.setattr(
        manager,
        "_place_post_only_with_retry",
        traced_place,
    )

    first = await manager._maybe_requote_entry(
        run,
        existing_order["orderId"],
        list(client.open_orders),
    )
    replacement = client.open_orders[0]
    clock["seconds"] = 101.0
    second = await manager._maybe_requote_entry(
        run,
        replacement["orderId"],
        list(client.open_orders),
    )

    assert first is True
    assert second is False
    assert client.order_operations == [
        ("cancel", existing_order["orderId"]),
        (
            "place",
            "GTX",
            f'{run["run_id"]}_entry_r1',
        ),
    ]
    assert client.cancelled == [
        ("ETHUSDC", existing_order["orderId"])
    ]
    assert place_calls[0]["fallback_to_gtc"] is False
    assert [order["timeInForce"] for order in client.all_orders] == [
        "GTX"
    ]
    assert manager._entry_requote_counts[run["run_id"]] == 1
    requote_event = next(
        details
        for _, event_type, details in repo.events
        if event_type == "entry_requoted"
    )
    assert requote_event["v1459_profile"] == "ONE_STEP_REPRICE"
    assert requote_event["maker_mode"] == "ONE_STEP_REPRICE"
    assert requote_event["enforcement"] is True


@pytest.mark.asyncio
async def test_passive_regime_enforcement_never_reprices(
    monkeypatch,
):
    settings = _enforcement_settings(
        mainnet_codex_v1459_one_step_reprice_enabled=True,
        mainnet_entry_requote_min_age_seconds=30,
        mainnet_entry_max_deviation_bps=2.0,
        mainnet_entry_slippage_bps=8.0,
    )
    client = _OrderTraceClient()
    client.book = {"bidPrice": "99.96", "askPrice": "99.97"}
    manager = MainnetOneRunManager(
        settings,
        client,
        FakeRepo(),
        FakeTelegramApp(),
        observation_runtime=_ReadyProfileRuntime(),
    )
    decision = _regime_decision(
        "STUP-S:weak_chop",
        metrics={
            "maker_mode": "PASSIVE",
            "v1459_regime_mode": "enforcement",
            "ttl_s": 120,
        },
    )
    run = _regime_run(
        decision,
        run_id="cry3mn_passive_no_reprice",
    )
    run["armed_at_ms"] = 40_000
    existing_order = _entry_order(run["run_id"])
    client.open_orders = [dict(existing_order)]
    monkeypatch.setattr(
        one_run_module.time,
        "time",
        lambda: 100.0,
    )
    open_orders_before = [dict(existing_order)]

    result = await manager._maybe_requote_entry(
        run,
        existing_order["orderId"],
        list(client.open_orders),
    )

    assert result is False
    assert client.order_operations == []
    _assert_no_order_mutation(client, open_orders_before)


@pytest.mark.asyncio
async def test_one_step_reprice_shadow_observation_is_deduped_and_orderless():
    settings = _settings(
        mainnet_codex_v1459_candidate_selector_enabled=True,
        mainnet_codex_v1459_one_step_reprice_enabled=True,
        mainnet_codex_v1459_live_enforcement_enabled=False,
        mainnet_entry_slippage_bps=8.0,
    )
    client = FakeClient()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        settings,
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _adaptive_stup_entry_run("S_E2_TP10_SL8_T90")
    existing_order = _entry_order(run["run_id"])
    client.open_orders = [dict(existing_order)]
    open_orders_before = [dict(existing_order)]
    observation = {
        "order_id": existing_order["orderId"],
        "existing_order": existing_order,
        "open_orders": list(client.open_orders),
        "age_ms": 60_000,
        "ttl_seconds": 120,
        "bid": 100.00,
        "ask": 100.01,
        "deviation_bps": 4.0,
    }

    await manager._v1459_observe_entry_profile(run, **observation)
    await manager._v1459_observe_entry_profile(run, **observation)

    transitions = [
        details
        for run_id, event_type, details in repo.events
        if run_id == run["run_id"]
        and event_type == "v1459_profile_transition"
    ]
    assert len(transitions) == 1
    assert transitions[0]["phase"] == "ENTRY"
    assert transitions[0]["profile"] == EntryProfile.ONE_STEP_REPRICE.value
    assert transitions[0]["enforcement"] is False
    assert transitions[0]["facts"]["ownership_clear"] is True
    assert transitions[0]["facts"]["reprice_window_open"] is True
    _assert_no_order_mutation(client, open_orders_before)


@pytest.mark.parametrize(
    ("action_id", "duplicate_owned_order", "unsafe_fact"),
    (
        ("S_E0_TP10_SL8_T90", False, "price_still_repriceable"),
        ("S_E2_TP10_SL8_T90", True, "ownership_clear"),
    ),
    ids=("e0-not-repriceable", "ownership-not-clear"),
)
@pytest.mark.asyncio
async def test_one_step_shadow_unsafe_entry_emits_expire_without_mutation(
    action_id,
    duplicate_owned_order,
    unsafe_fact,
):
    settings = _settings(
        mainnet_codex_v1459_candidate_selector_enabled=True,
        mainnet_codex_v1459_one_step_reprice_enabled=True,
        mainnet_codex_v1459_live_enforcement_enabled=False,
        mainnet_entry_slippage_bps=8.0,
    )
    client = FakeClient()
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        settings,
        client,
        repo,
        FakeTelegramApp(),
    )
    run = _adaptive_stup_entry_run(action_id)
    existing_order = _entry_order(run["run_id"])
    client.open_orders = [dict(existing_order)]
    if duplicate_owned_order:
        client.open_orders.append(
            {
                **existing_order,
                "orderId": existing_order["orderId"] + 1,
                "clientOrderId": f'{run["run_id"]}_entry_r1',
            }
        )
    open_orders_before = [dict(order) for order in client.open_orders]

    await manager._v1459_observe_entry_profile(
        run,
        order_id=existing_order["orderId"],
        existing_order=existing_order,
        open_orders=list(client.open_orders),
        age_ms=60_000,
        ttl_seconds=120,
        bid=100.00,
        ask=100.01,
        deviation_bps=4.0,
    )

    transitions = [
        details
        for run_id, event_type, details in repo.events
        if run_id == run["run_id"]
        and event_type == "v1459_profile_transition"
    ]
    assert len(transitions) == 1
    assert transitions[0]["profile"] == EntryProfile.EXPIRE.value
    assert transitions[0]["enforcement"] is False
    assert transitions[0]["facts"][unsafe_fact] is False
    _assert_no_order_mutation(client, open_orders_before)


class _FailingEventRepo(FakeRepo):
    def __init__(self, event_type_to_fail: str) -> None:
        super().__init__()
        self.event_type_to_fail = event_type_to_fail

    async def log_event(self, run_id, event_type, details=None):
        if event_type == self.event_type_to_fail:
            raise RuntimeError(f"forced {event_type} persistence failure")
        return await super().log_event(run_id, event_type, details)


@pytest.mark.asyncio
async def test_regime_transition_persistence_failure_enforcement_fails_closed(
    monkeypatch,
):
    settings = _enforcement_settings(
        mainnet_codex_v1459_regime_switch_enabled=True,
    )
    client = FakeClient()
    repo = _FailingEventRepo("v1459_regime_profile_transition")
    manager = MainnetOneRunManager(
        settings,
        client,
        repo,
        FakeTelegramApp(),
        observation_runtime=_ReadyProfileRuntime(),
    )
    manager._adaptive_session = {
        "stop_requested": False,
        "rearm_enabled": True,
    }
    decision = _regime_decision()
    run = _regime_run(
        decision,
        run_id="cry3mn_regime_persist_fail",
    )
    monkeypatch.setattr(one_run_module.time, "time", lambda: 0.0)

    result = await manager._v1459_apply_regime_profile(run, decision)

    assert result.accepted is False
    assert result.size_mult == 0.0
    assert result.notional_mult == 0.0
    assert result.requested_notional_usdc == 0.0
    assert result.reason == "v1459_regime_transition_persistence_failed"
    assert result.metrics["v1459_regime_fail_closed"] is True
    assert manager._adaptive_session["stop_requested"] is True
    assert manager._adaptive_session["rearm_enabled"] is False
    _assert_no_order_mutation(client, [])


@pytest.mark.asyncio
async def test_regime_transition_persistence_failure_candidate_only_is_exact_incumbent(
    monkeypatch,
):
    settings = _settings(
        mainnet_codex_v1459_candidate_selector_enabled=True,
        mainnet_codex_v1459_regime_switch_enabled=True,
        mainnet_codex_v1459_live_enforcement_enabled=False,
    )
    client = FakeClient()
    repo = _FailingEventRepo("v1459_regime_profile_transition")
    manager = MainnetOneRunManager(
        settings,
        client,
        repo,
        FakeTelegramApp(),
    )
    decision = _regime_decision()
    run = _regime_run(
        decision,
        run_id="cry3mn_regime_candidate_persist_fail",
    )
    monkeypatch.setattr(one_run_module.time, "time", lambda: 0.0)

    result = await manager._v1459_apply_regime_profile(run, decision)

    assert result == decision
    _assert_no_order_mutation(client, [])


@pytest.mark.asyncio
async def test_exit_profile_persistence_failure_preserves_incumbent_and_stops_rearm():
    settings = _enforcement_settings(
        mainnet_codex_v1459_runner_enabled=True,
    )
    repo = _FailingEventRepo("v1459_profile_transition")
    manager = MainnetOneRunManager(
        settings,
        FakeClient(),
        repo,
        FakeTelegramApp(),
        observation_runtime=_ReadyProfileRuntime(),
    )
    manager._adaptive_session = {
        "stop_requested": False,
        "rearm_enabled": True,
    }
    run = _adaptive_codex_run("cry3mn_exit_persist_fail")
    decision, facts = _exit_decision(settings)
    incumbent_reason = sorted(
        one_run_module.V1459_RUNNER_SOFT_EXIT_REASONS
    )[0]

    result = await manager._v1459_apply_exit_profile(
        run,
        decision=decision,
        incumbent_reason=incumbent_reason,
        facts=facts,
    )

    assert decision.profile is ExitProfile.RUNNER
    assert result == incumbent_reason
    assert manager._adaptive_session["stop_requested"] is True
    assert manager._adaptive_session["rearm_enabled"] is False
    assert run["run_id"] not in manager._v1459_runner_extension_used


def test_armed_adaptive_run_is_candidate_before_codex_signal_persistence():
    run = _adaptive_codex_run("cry3mn_armed_candidate")
    run["status"] = "ARMED"
    run["signal_json"] = None
    enabled_manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_v1459_candidate_selector_enabled=True,
            mainnet_codex_v1_enabled=True,
        ),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )
    disabled_manager = MainnetOneRunManager(
        _settings(
            mainnet_codex_v1459_candidate_selector_enabled=True,
            mainnet_codex_v1_enabled=False,
            mainnet_strategy_label="wildcat",
        ),
        FakeClient(),
        FakeRepo(),
        FakeTelegramApp(),
    )

    assert enabled_manager._v1459_profile_candidate_active(run) is True
    assert disabled_manager._v1459_profile_candidate_active(run) is False


@pytest.mark.asyncio
async def test_regime_transition_dedupe_is_scoped_to_run_id(monkeypatch):
    settings = _settings(
        mainnet_codex_v1459_candidate_selector_enabled=True,
        mainnet_codex_v1459_regime_switch_enabled=True,
        mainnet_codex_v1459_live_enforcement_enabled=False,
    )
    repo = FakeRepo()
    manager = MainnetOneRunManager(
        settings,
        FakeClient(),
        repo,
        FakeTelegramApp(),
    )
    decision = _regime_decision()
    clock = {"seconds": 0.0}
    monkeypatch.setattr(
        one_run_module.time,
        "time",
        lambda: clock["seconds"],
    )

    await manager._v1459_apply_regime_profile(
        _regime_run(decision, run_id="cry3mn_transition_run_a"),
        decision,
    )
    clock["seconds"] = 1.0
    await manager._v1459_apply_regime_profile(
        _regime_run(decision, run_id="cry3mn_transition_run_b"),
        decision,
    )

    transition_run_ids = [
        run_id
        for run_id, event_type, _ in repo.events
        if event_type == "v1459_regime_profile_transition"
    ]
    assert transition_run_ids == [
        "cry3mn_transition_run_a",
        "cry3mn_transition_run_b",
    ]
