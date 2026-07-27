from __future__ import annotations

from dataclasses import replace
import json

import pytest

from src.gridbot.mainnet.one_run import MAINNET_MIN_ENTRY_NOTIONAL_USDC
from src.gridbot.mainnet.v1462_admission import V1462_POLICY_HASH
from src.gridbot.mainnet.v1462_lane_registry import (
    REGISTRY_HASH,
    REGISTRY_VERSION,
    lane_definition_hash,
    lane_for,
)
from tests.test_mainnet_one_run_maker import FakeClient, FakeRepo
from tests.test_v1460_one_run_integration import (
    _adaptive_run,
    _codex,
    _manager,
    _v1460_settings,
    _wildcat,
)


def _settings(**overrides):
    values = {
        "mainnet_codex_v1461_candidate_selector_enabled": True,
        "mainnet_codex_v1461_regime_gate_enabled": True,
        "mainnet_codex_v1461_live_enforcement_enabled": True,
        "mainnet_codex_v1461_shadow_all_strategy_rejects_enabled": True,
        "mainnet_codex_v1462_strict_live_allowlist_enabled": True,
        "mainnet_codex_v1462_shadow_all_enabled": True,
        "mainnet_codex_v1462_promotion_enforcement_enabled": False,
    }
    values.update(overrides)
    return _v1460_settings(**values)


def _ordinary_codex_run(run_id: str = "cry3mn_v1462_general") -> dict:
    return {
        "run_id": run_id,
        "symbol": "ETHUSDC",
        "status": "ARMED",
        "side": "SHORT",
        "armed_at_ms": 1_000,
        "updated_at_ms": 1_000,
        "params": {"mode": "one_run"},
        "signal_json": json.dumps(
            {"codex_v1": {"enabled": True, "lane_code": "RP1"}}
        ),
    }


def _rp1_control():
    return replace(
        _codex(market_state="RP1:pullback", lane_code="RP1"),
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=1.0,
    )


def test_general_codex_run_cannot_bypass_v1462_and_v1461_cannot_enforce() -> None:
    manager = _manager(settings=_settings())
    manager._dca_enabled = False
    run = _ordinary_codex_run()
    assert manager._is_adaptive_run(run) is False
    assert manager._v1462_strict_live_allowlist_active(run) is True
    assert manager._v1462_shadow_all_active(run) is True
    assert manager._v1460_candidate_active(run) is True
    assert manager._v1461_enforcement_active(_adaptive_run()) is False


@pytest.mark.parametrize(
    "setting_name",
    [
        "mainnet_codex_v1459_runner_enabled",
        "mainnet_codex_v1459_one_step_reprice_enabled",
        "mainnet_codex_v1460_runner_enabled",
        "mainnet_codex_v1460_one_step_reprice_enabled",
        "mainnet_codex_v1461_runner_enabled",
        "mainnet_codex_v1461_one_step_reprice_enabled",
    ],
)
def test_runner_and_reprice_controls_fail_closed(setting_name: str) -> None:
    manager = _manager(settings=_settings(**{setting_name: True}))
    manager._dca_enabled = False
    assert manager._v1462_execution_controls_safe() is False


def test_dca_control_fails_closed() -> None:
    manager = _manager(settings=_settings())
    manager._dca_enabled = True
    assert manager._v1462_execution_controls_safe() is False


def test_opportunity_id_dedupes_across_runs_within_two_minute_bucket() -> None:
    manager = _manager(settings=_settings())
    raw = _rp1_control()
    ticket = manager._v1462_candidate_ticket(raw, raw)
    observed_at_ms = 120_001
    first = manager._v1462_opportunity_id(
        _ordinary_codex_run("cry3mn_v1462_first"), ticket, observed_at_ms
    )
    second = manager._v1462_opportunity_id(
        _ordinary_codex_run("cry3mn_v1462_second"), ticket, observed_at_ms
    )
    assert first == second


def test_resolved_profile_hash_is_deterministic_and_action_sensitive() -> None:
    manager = _manager(settings=_settings())
    raw = _rp1_control()
    ticket = manager._v1462_candidate_ticket(raw, raw)

    first = manager._v1462_resolved_profile_hash(ticket)
    second = manager._v1462_resolved_profile_hash(ticket)
    changed = manager._v1462_resolved_profile_hash(
        replace(ticket, entry_offset_bp=ticket.entry_offset_bp + 1.0)
    )

    assert first == second
    assert len(first) == 64
    assert changed != first


def test_resolved_profile_hash_ignores_absolute_prices_but_keeps_risk_geometry() -> None:
    manager = _manager(settings=_settings())
    raw = _rp1_control()
    ticket = manager._v1462_candidate_ticket(raw, raw)
    plan = manager._v1463_frozen_effective_plan(_wildcat(), raw, ticket)
    assert plan is not None

    repriced = {
        **plan,
        "entry_price": 2_000.0,
        "tp1_price": 2_001.0,
        "sl_price": 1_998.0,
        "full_tp_price": 2_002.0,
        "observed_at_ms": 999_999,
    }
    assert manager._v1462_resolved_profile_hash(
        ticket, plan
    ) == manager._v1462_resolved_profile_hash(ticket, repriced)
    assert manager._v1462_resolved_profile_hash(
        ticket, {**repriced, "sl_bp": float(plan["sl_bp"]) + 1.0}
    ) != manager._v1462_resolved_profile_hash(ticket, plan)


def test_true_shadow_drop_attribution_distinguishes_registry_scope() -> None:
    legacy = {
        "lane_code": "W2A",
        "registry_version": REGISTRY_VERSION,
        "registry_hash": REGISTRY_HASH,
        "v1462_policy_hash": V1462_POLICY_HASH,
        "resolved_profile_hash": "stable-profile",
        "market_state": "W2A:control",
        "effective_side": "LONG",
    }
    legacy_attribution = (
        _manager(settings=_settings())._v1464_shadow_drop_attribution(legacy)
    )
    assert legacy_attribution["attribution_scope"] == "LEGACY_REGISTRY"
    assert legacy_attribution["legacy_lane_code"] == "W2A"
    assert legacy_attribution["resolved_profile_hash"] == "stable-profile"
    assert legacy_attribution["lane_definition_hash"] == lane_definition_hash(
        lane_for("W2A")
    )

    outside = {
        **legacy,
        "lane_code": "NL-UNCLASSIFIED",
        "candidate_lane": "NL-UNCLASSIFIED",
        "shadow_lane": "SH_UNC_L_S1",
        "lane_definition_hash": None,
    }
    outside_attribution = (
        _manager(settings=_settings())._v1464_shadow_drop_attribution(outside)
    )
    assert outside_attribution["attribution_scope"] == "OUT_OF_REGISTRY"
    assert outside_attribution["attribution_lane_code"] == "NL-UNCLASSIFIED"
    assert outside_attribution["out_of_registry_lane_code"] == (
        "NL-UNCLASSIFIED"
    )
    assert outside_attribution["lane_code"] == "SH_UNC_L_S1"
    assert outside_attribution["lane_definition_hash"] is None
    assert outside_attribution["legacy_lane_code"] is None


@pytest.mark.asyncio
async def test_registered_incomplete_drop_recovers_canonical_ticket_identity() -> None:
    repo = FakeRepo()
    manager = _manager(settings=_settings(), repo=repo)
    ticket = manager._v1462_candidate_ticket(_rp1_control(), _rp1_control())
    resolved_profile_hash = manager._v1462_resolved_profile_hash(ticket)

    await manager._log_v1463_shadow_data_incomplete(
        "cry3mn_v1462_drop_identity",
        "v1462_opp_registered_drop",
        reason="collector_interrupted",
        source={
            "candidate_ticket": ticket.to_payload(),
            "resolved_profile_hash": resolved_profile_hash,
            "environment": "MAINNET",
            "symbol": "ETHUSDC",
            "effective_side": "LONG",
            "strategy": "S1_BB_RSI",
            "market_state": ticket.market_state,
            "final_route": "SHADOW",
        },
    )

    drop = next(
        details
        for _, event_type, details in repo.events
        if event_type == "entry_codex_v1_shadow_sample_dropped"
    )
    assert drop["v1462_opportunity_id"] == "v1462_opp_registered_drop"
    assert drop["lane_code"] == "RP1"
    assert drop["registry_version"] == REGISTRY_VERSION
    assert drop["registry_hash"] == REGISTRY_HASH
    assert drop["lane_definition_hash"] == lane_definition_hash(
        lane_for("RP1")
    )
    assert drop["v1462_policy_hash"] == V1462_POLICY_HASH
    assert drop["admission_policy_hash"] == V1462_POLICY_HASH
    assert drop["profile_identity_schema"] == "v1464.stable-profile.1"
    assert drop["resolved_profile_hash"] == resolved_profile_hash


def test_frozen_effective_plan_uses_effective_side_profile_and_ttls() -> None:
    manager = _manager(settings=_settings())
    manager._dca_enabled = False
    effective = replace(
        _codex(market_state="STUP-S:hot_continuation", lane_code="STUP-S"),
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=0.0,
        metrics={
            "market_state": "STUP-S:hot_continuation",
            "entry_bp": 0.0,
            "tp1_bp": 4.0,
            "sl_bp": 4.0,
            "be_bp": 0.0,
            "partial_exit_pct": 0.40,
            "ttl_s": 45,
            "hold_s": 120,
        },
    )
    ticket = manager._v1462_candidate_ticket(effective, effective)
    plan = manager._v1463_frozen_effective_plan(_wildcat(), effective, ticket)
    assert plan is not None
    assert plan["side"] == "LONG"
    assert plan["strategy"] == "S1_BB_RSI"
    assert plan["entry_price"] == pytest.approx(100.0)
    assert plan["tp1_price"] == pytest.approx(100.04)
    assert plan["sl_price"] == pytest.approx(99.96)
    assert plan["entry_ttl_s"] == 45
    assert plan["outcome_ttl_s"] == 120
    # The source Wildcat was SHORT with stop above entry; no raw geometry may
    # leak into the LONG counterfactual.
    assert plan["sl_price"] != pytest.approx(_wildcat().signal.stop_loss)


@pytest.mark.parametrize(
    ("base_notional", "max_notional", "expected"),
    [
        (200.0, 80.0, 80.0),
        (1.0, 500.0, MAINNET_MIN_ENTRY_NOTIONAL_USDC),
    ],
)
def test_frozen_effective_plan_uses_applied_cap_or_minimum_lift(
    base_notional: float,
    max_notional: float,
    expected: float,
) -> None:
    manager = _manager(
        settings=_settings(
            mainnet_initial_notional_usdc=base_notional,
            mainnet_equity_cap_usdc=(200.0 if expected == 80.0 else 500.0),
            mainnet_max_cumulative_notional_usdc=max_notional,
            mainnet_recovery_steps=0,
            mainnet_codex_v1_max_notional_usdc=(80.0 if expected == 80.0 else 0.0),
        )
    )
    control = _rp1_control()
    if expected != 80.0:
        control = replace(
            control,
            size_mult=1.0,
            notional_mult=1.0,
            requested_notional_usdc=1.0,
        )
    ticket = manager._v1462_candidate_ticket(control, control)
    plan = manager._v1463_frozen_effective_plan(_wildcat(), control, ticket)
    assert plan is not None
    assert plan["planned_notional_usdc"] == pytest.approx(expected)
    assert plan["requested_notional_usdc"] == pytest.approx(expected)
    assert plan["ticket_requested_notional_usdc"] == pytest.approx(
        ticket.requested_notional_usdc
    )


@pytest.mark.asyncio
async def test_same_opportunity_keeps_a_durable_admission_row_per_run(monkeypatch) -> None:
    monkeypatch.setattr("src.gridbot.mainnet.one_run.time.time", lambda: 120.001)
    repo = FakeRepo()
    manager = _manager(settings=_settings(), repo=repo)
    manager._dca_enabled = False
    incumbent = _rp1_control()

    for run_id in ("cry3mn_v1462_a", "cry3mn_v1462_b"):
        run = _ordinary_codex_run(run_id)
        v1460 = await manager._v1460_apply_lane_policy(run, incumbent)
        admitted = await manager._v1462_apply_strict_admission(
            run,
            incumbent,
            True,
            incumbent,
            v1460,
            wildcat_decision=_wildcat(),
        )
        assert admitted.accepted is True

    admissions = [
        (run_id, details)
        for run_id, event_type, details in repo.events
        if event_type == "entry_codex_v1462_admission"
    ]
    assert [run_id for run_id, _ in admissions] == [
        "cry3mn_v1462_a",
        "cry3mn_v1462_b",
    ]
    assert len({details["opportunity_id"] for _, details in admissions}) == 1


@pytest.mark.asyncio
async def test_general_run_only_control_rule_is_live_and_unknown_is_shadow() -> None:
    repo = FakeRepo()
    manager = _manager(settings=_settings(), repo=repo)
    manager._dca_enabled = False
    run = _ordinary_codex_run()

    rp1 = _rp1_control()
    rp1_v1460 = await manager._v1460_apply_lane_policy(run, rp1)
    allowed = await manager._v1462_apply_strict_admission(
        run,
        rp1,
        True,
        rp1,
        rp1_v1460,
        wildcat_decision=_wildcat(),
    )
    assert allowed.accepted is True
    live_identity = allowed.metrics["v1462_admission"]
    assert live_identity["matrix_rule_id"] == "v1460.rp1.control"
    assert live_identity["mode"] == "LIVE"
    assert live_identity["final_route"] == "LIVE"
    assert live_identity["raw_accepted"] is True
    assert live_identity["pre_gate_accepted"] is True
    assert live_identity["final_incumbent_accepted"] is True
    assert live_identity["reject_reopen_detected"] is False

    unknown = _codex(market_state="STUP-S:brand_new_state", lane_code="STUP-S")
    unknown_v1460 = await manager._v1460_apply_lane_policy(run, unknown)
    blocked = await manager._v1462_apply_strict_admission(
        run,
        unknown,
        True,
        unknown,
        unknown_v1460,
        wildcat_decision=_wildcat(),
    )
    assert blocked.accepted is False
    assert blocked.requested_notional_usdc == 0.0
    assert blocked.reason == "v1462.shadow.rule_not_allowlisted"


@pytest.mark.asyncio
async def test_v1428_legacy_reopen_is_shadow_even_when_stup_control_matches() -> None:
    repo = FakeRepo()
    manager = _manager(settings=_settings(), repo=repo)
    manager._dca_enabled = False
    run = _ordinary_codex_run("cry3mn_v1462_v1428_reopen")
    raw = _codex(market_state="STUP-S:clean_extension", lane_code="STUP-S")
    reopened = replace(
        raw,
        risk_tags=(
            *raw.risk_tags,
            "v1428_legacy_stups_reopen_for_tree_profile",
        ),
        policy_tag="v1427_five_window_tp14_adaptive_exec",
        metrics={
            **(raw.metrics or {}),
            "v1428_legacy_reopen": True,
            "v1428_legacy_reopen_reason": "v1420_stups_clean_extension_gate_block",
        },
    )
    annotated = await manager._v1460_apply_lane_policy(run, reopened)
    assert annotated.metrics["v1460_lane_adaptive"]["matrix_rule_id"] == (
        "v1460.stup_clean.control"
    )
    lineage = manager._v1462_reject_reopen_lineage(raw, reopened, annotated)
    assert any("v1428_legacy_stups_reopen" in item for item in lineage)

    blocked = await manager._v1462_apply_strict_admission(
        run,
        raw,
        True,
        reopened,
        annotated,
        wildcat_decision=_wildcat(),
        reject_lineage=lineage,
    )
    assert blocked.accepted is False
    assert blocked.reason == "v1462.shadow.reject_reopen_lineage"
    assert blocked.metrics["v1462_admission"]["reject_lineage"]
    identity = blocked.metrics["v1462_admission"]
    assert identity["reject_reopen_flag"] is True
    assert identity["final_route"] == "SHADOW"
    assert identity["mode"] == "SHADOW"
    assert identity["raw_accepted"] is True
    assert identity["pre_gate_accepted"] is True
    assert identity["final_incumbent_accepted"] is True
    assert identity["reject_reopen_detected"] is True


@pytest.mark.asyncio
async def test_reject_shadow_opportunity_is_durable_deduped_and_positive() -> None:
    repo = FakeRepo()
    manager = _manager(settings=_settings(), repo=repo)
    manager._dca_enabled = False
    run = _ordinary_codex_run("cry3mn_v1462_shadow")
    raw = replace(
        _codex(market_state="UNKNOWN", lane_code="STUP-S"),
        accepted=False,
        lane=None,
        lane_code=None,
        entry_offset_bp=None,
        size_mult=0.0,
        notional_mult=0.0,
        requested_notional_usdc=0.0,
        reason="no_codex_v1_lane_match",
        regime=None,
        metrics={},
        policy_tag=None,
    )
    ticket = manager._v1462_candidate_ticket(raw, raw)

    for _ in range(2):
        await manager._start_codex_v1_shadow_sample(
            run,
            _wildcat(),
            raw,
            raw,
            {"symbol": "ETHUSDC", "mid_price": 100.0},
            reason="no_codex_v1_lane_match",
            effective_status="blocked",
            candidate_ticket=ticket,
        )

    durable = [
        details
        for _, event_type, details in repo.events
        if event_type == "entry_codex_v1462_shadow_opportunity"
    ]
    assert len(durable) == 1
    assert durable[0]["lane_code"] == "UNKNOWN"
    assert durable[0]["classifier_lane"] == "UNKNOWN"
    assert durable[0]["side"] == "SHORT"
    assert durable[0]["market_state"] == "UNKNOWN"
    assert durable[0]["lane_definition_hash"] is None
    assert durable[0]["registry_version"] == REGISTRY_VERSION
    assert durable[0]["registry_hash"] == REGISTRY_HASH
    assert durable[0]["v1462_policy_hash"] == V1462_POLICY_HASH
    assert durable[0]["final_route"] == "SHADOW"
    assert durable[0]["candidate_ticket"]["requested_notional_usdc"] > 0.0
    started = [
        details
        for _, event_type, details in repo.events
        if event_type == "entry_codex_v1_shadow_sample_started"
        and not details.get("diagnostic_only")
    ]
    assert started
    assert started[0]["requested_notional_usdc"] > 0.0
    assert started[0]["promotion_eligible"] is False
    assert started[0]["evidence_evaluator_eligible"] is False
    assert len(started) == 1
    assert not any(
        event_type == "entry_codex_v1_shadow_sample_dropped"
        for _, event_type, _ in repo.events
    )


@pytest.mark.asyncio
async def test_admission_and_shadow_events_share_opportunity_id() -> None:
    repo = FakeRepo()
    manager = _manager(settings=_settings(), repo=repo)
    manager._dca_enabled = False
    run = _ordinary_codex_run("cry3mn_v1462_join")
    unknown = _codex(market_state="STUP-S:brand_new_state", lane_code="STUP-S")
    v1460 = await manager._v1460_apply_lane_policy(run, unknown)
    ticket = manager._v1462_candidate_ticket(unknown, unknown)
    blocked = await manager._v1462_apply_strict_admission(
        run,
        unknown,
        True,
        unknown,
        v1460,
        wildcat_decision=_wildcat(),
        candidate_ticket=ticket,
    )
    await manager._start_codex_v1_shadow_sample(
        run,
        _wildcat(),
        unknown,
        blocked,
        {"symbol": "ETHUSDC", "mid_price": 100.0},
        reason=blocked.reason,
        effective_status="blocked_v1462_strict_allowlist",
        candidate_ticket=ticket,
    )
    admission = next(
        details
        for _, event_type, details in repo.events
        if event_type == "entry_codex_v1462_admission"
    )
    shadow = next(
        details
        for _, event_type, details in repo.events
        if event_type == "entry_codex_v1462_shadow_opportunity"
    )
    assert admission["opportunity_id"] == shadow["opportunity_id"]
    sample = next(
        details
        for _, event_type, details in repo.events
        if event_type == "entry_codex_v1_shadow_sample_started"
        and not details.get("diagnostic_only")
    )
    assert sample["v1462_opportunity_id"] == admission["opportunity_id"]
    identity_fields = (
        "registry_version",
        "registry_hash",
        "lane_definition_hash",
        "v1462_policy_hash",
        "resolved_profile_hash",
        "raw_accepted",
        "pre_gate_accepted",
        "final_incumbent_accepted",
        "reject_lineage",
        "reject_reopen_flag",
        "reject_reopen_detected",
        "classifier_side",
        "effective_side",
        "market_state",
        "mode",
        "final_route",
    )
    assert admission["lane_definition_hash"] == lane_definition_hash(
        lane_for("STUP-S")
    )
    assert admission["registry_version"] == REGISTRY_VERSION
    assert admission["registry_hash"] == REGISTRY_HASH
    assert admission["v1462_policy_hash"] == V1462_POLICY_HASH
    assert admission["final_route"] == "SHADOW"
    assert admission["mode"] == "SHADOW"
    for field in identity_fields:
        assert shadow[field] == admission[field]
        assert sample[field] == admission[field]
    await manager._log_codex_v1_shadow_outcome(
        str(sample["sample_id"]),
        sample,
        {
            "shadow_outcome": "no_fill",
            "filled": False,
            "resolved_at_ms": int(sample["start_ms"]) + 180_000,
        },
    )
    outcome = next(
        details
        for _, event_type, details in repo.events
        if event_type == "entry_codex_v1_shadow_outcome"
    )
    assert outcome["v1462_opportunity_id"] == admission["opportunity_id"]
    assert outcome["evidence_evaluator_eligible"] == sample[
        "evidence_evaluator_eligible"
    ]
    for field in identity_fields:
        assert outcome[field] == admission[field]


@pytest.mark.asyncio
async def test_v1462_shadow_evidence_uses_aggtrade_without_promotion_or_live_order() -> None:
    repo = FakeRepo()
    client = FakeClient()
    manager = _manager(
        settings=_settings(
            mainnet_codex_v1461_candidate_selector_enabled=False,
            mainnet_codex_v1461_regime_gate_enabled=False,
        ),
        repo=repo,
        client=client,
    )
    manager._dca_enabled = False
    run = _ordinary_codex_run("cry3mn_v1462_promotion_evidence")
    raw = _codex(market_state="STUP-S:brand_new_state", lane_code="STUP-S")
    v1460 = await manager._v1460_apply_lane_policy(run, raw)
    ticket = manager._v1462_candidate_ticket(raw, raw)
    blocked = await manager._v1462_apply_strict_admission(
        run,
        raw,
        True,
        raw,
        v1460,
        wildcat_decision=_wildcat(),
        candidate_ticket=ticket,
    )
    assert blocked.accepted is False
    assert blocked.metrics["v1462_admission"]["final_route"] == "SHADOW"

    manager._codex_v1_map_block_to_shadow_lane = lambda *args: {
        "shadow_lane": "SH_V1462_STUP_S",
        "candidate_lane": "STUP-S",
        "shadow_lane_family": "STUP-S",
        "promotion_eligible": True,
        "fill_model": "limit_touch",
        "mapping_reason": "v1462_test_promotion_evidence",
    }
    await manager._start_codex_v1_shadow_sample(
        run,
        _wildcat(),
        raw,
        blocked,
        {"symbol": "ETHUSDC", "mid_price": 100.0},
        reason=blocked.reason,
        effective_status="blocked_v1462_strict_allowlist",
        candidate_ticket=ticket,
    )

    sample = next(
        details
        for _, event_type, details in repo.events
        if event_type == "entry_codex_v1_shadow_sample_started"
        and not details.get("diagnostic_only")
    )
    assert manager._v1461_config_selected() is False
    assert sample["promotion_eligible"] is False
    assert sample["evidence_evaluator_eligible"] is True
    assert sample["fill_model"] == "limit_touch"
    assert sample["final_route"] == "SHADOW"
    assert sample["v1462_opportunity_id"]

    evaluated_sample_ids = []

    async def capture_aggtrade_cache(run_id, samples, target_ms):
        assert run_id == run["run_id"]
        evaluated_sample_ids.extend(item["sample_id"] for item in samples)
        start_ms = int(samples[0]["start_ms"])
        return {
            "coverage_start_ms": start_ms,
            "coverage_end_ms": start_ms,
            "rows": [],
            "fetch_failures": 0,
            "invalid_reason": None,
            "last_error": None,
        }

    manager._v1461_advance_shadow_aggtrade_cache = capture_aggtrade_cache
    await manager._update_codex_v1_shadow_outcomes(run, [])
    assert evaluated_sample_ids == [sample["sample_id"]]
    assert client.market_orders == []
    assert client.reduce_only_limit_orders == []
    assert client.stop_market_sl_orders == []
    assert client.stop_limit_sl_orders == []
    assert client.algo_orders == []


@pytest.mark.asyncio
async def test_v1464_aggtrade_page_budget_limits_each_update_cycle() -> None:
    class PagedAggTradeClient:
        def __init__(self):
            self.calls = []

        async def get_agg_trades(self, symbol, **kwargs):
            self.calls.append((symbol, kwargs))
            first_id = int(kwargs.get("from_id") or 10)
            return [
                {"a": first_id, "T": 1_000 + first_id, "p": "100.0"},
                {"a": first_id + 1, "T": 1_001 + first_id, "p": "100.1"},
            ]

    client = PagedAggTradeClient()
    manager = _manager(
        settings=_settings(
            mainnet_codex_v1460_weak_shadow_max_pages=10,
            mainnet_codex_v1460_weak_shadow_page_limit=2,
            mainnet_codex_v1464_shadow_aggtrade_pages_per_cycle=1,
        ),
        client=client,
    )
    samples = [{
        "sample_id": "budgeted",
        "symbol": "ETHUSDC",
        "start_ms": 1_000,
    }]

    await manager._v1461_advance_shadow_aggtrade_cache(
        "cry3mn_v1464_budget",
        samples,
        10_000,
    )
    assert len(client.calls) == 1
    await manager._v1461_advance_shadow_aggtrade_cache(
        "cry3mn_v1464_budget",
        samples,
        10_000,
    )
    assert len(client.calls) == 2


def test_lane_monitor_is_available_in_idle_and_active_loop_keyboards() -> None:
    manager = _manager(settings=_settings())

    def callbacks(markup) -> set[str]:
        return {
            str(button.callback_data)
            for row in markup.inline_keyboard
            for button in row
            if button.callback_data
        }

    manager._loop_total = 0
    assert "mainnet:lanes" in callbacks(manager._buttons(active=False))
    manager._loop_total = 20
    assert "mainnet:lanes" in callbacks(manager._buttons(active=True))


class _DurableLedgerRepo(FakeRepo):
    def __init__(self):
        super().__init__()
        self.fail_events: dict[str, int] = {}
        self.fail_typed_reads = 0
        self.terminal_shadow_rows: list[dict] = []
        self.terminal_tp_rows: list[dict] = []

    async def log_event(self, run_id, event_type, details):
        remaining = self.fail_events.get(event_type, 0)
        if remaining > 0:
            self.fail_events[event_type] = remaining - 1
            raise RuntimeError(f"forced {event_type} failure")
        await super().log_event(run_id, event_type, details)

    async def get_terminal_runs_with_unresolved_v1463_shadow_samples(self):
        return list(self.terminal_shadow_rows)

    async def get_events_by_types(self, run_id, event_types, limit=30):
        if self.fail_typed_reads > 0:
            self.fail_typed_reads -= 1
            raise RuntimeError("forced typed event read failure")
        return await super().get_events_by_types(run_id, event_types, limit=limit)

    async def get_terminal_runs_with_unresolved_v132_tp_policy_samples(self):
        return list(self.terminal_tp_rows)

    async def get_unresolved_v1462_shadow_opportunity_events(self):
        return []


async def _start_unknown_formal_sample(manager, run):
    raw = replace(
        _codex(market_state="UNKNOWN", lane_code="STUP-S"),
        accepted=False,
        lane=None,
        lane_code=None,
        entry_offset_bp=None,
        size_mult=0.0,
        notional_mult=0.0,
        requested_notional_usdc=0.0,
        reason="no_codex_v1_lane_match",
        metrics={},
    )
    await manager._start_codex_v1_shadow_sample(
        run,
        _wildcat(),
        raw,
        raw,
        {"symbol": "ETHUSDC", "mid_price": 100.0},
        reason=raw.reason,
        effective_status="blocked",
        candidate_ticket=manager._v1462_candidate_ticket(raw, raw),
    )


@pytest.mark.asyncio
async def test_v1463_formal_collector_bypasses_legacy_cap_and_cooldown(monkeypatch) -> None:
    repo = FakeRepo()
    manager = _manager(settings=_settings(), repo=repo)
    manager._dca_enabled = False
    run = _ordinary_codex_run("cry3mn_v1463_complete_collector")
    now = [119.999]
    monkeypatch.setattr("src.gridbot.mainnet.one_run.time.time", lambda: now[0])
    for index in range(13):
        now[0] = 119.999 + index * 120.0
        await _start_unknown_formal_sample(manager, run)
    started = [
        details
        for _, event_type, details in repo.events
        if event_type == "entry_codex_v1_shadow_sample_started"
        and not details.get("diagnostic_only")
    ]
    assert len(started) == 13

    # Adjacent two-minute buckets can be only milliseconds apart; durable ID,
    # not the legacy 90-second cooldown, controls formal cohort dedupe.
    repo2 = FakeRepo()
    manager2 = _manager(settings=_settings(), repo=repo2)
    manager2._dca_enabled = False
    now[0] = 119.999
    await _start_unknown_formal_sample(manager2, run)
    now[0] = 120.001
    await _start_unknown_formal_sample(manager2, run)
    assert sum(
        et == "entry_codex_v1_shadow_sample_started"
        and not details.get("diagnostic_only")
        for _, et, details in repo2.events
    ) == 2


@pytest.mark.asyncio
async def test_v1463_outcome_failure_retries_and_terminal_restart_rehydrates() -> None:
    repo = _DurableLedgerRepo()
    manager = _manager(settings=_settings(), repo=repo)
    manager._dca_enabled = False
    run = _ordinary_codex_run("cry3mn_v1463_retry")
    await _start_unknown_formal_sample(manager, run)
    sample_id, sample = next(iter(manager._codex_v1_shadow_samples.items()))
    repo.fail_events["entry_codex_v1_shadow_outcome"] = 1
    await manager._expire_codex_v1_shadow_samples(run, "signal_timeout")
    assert sample_id in manager._codex_v1_shadow_samples

    terminal = {**run, "status": "ENTRY_EXPIRED", "exit_reason": "signal_timeout"}
    repo.terminal_shadow_rows = [terminal]
    restarted = _manager(settings=_settings(), repo=repo)
    restarted._dca_enabled = False
    await restarted._reconcile_recent_terminal_v1463_shadow_samples()
    outcomes = [
        details for _, et, details in repo.events
        if et == "entry_codex_v1_shadow_outcome" and details.get("sample_id") == sample_id
    ]
    assert len(outcomes) == 1
    assert sample_id not in restarted._codex_v1_shadow_samples


@pytest.mark.asyncio
async def test_v1463_invalid_started_is_data_incomplete_and_db_failure_retries() -> None:
    repo = _DurableLedgerRepo()
    run = _ordinary_codex_run("cry3mn_v1463_invalid_started")
    invalid = {
        "sample_id": "sh_invalid",
        "run_id": run["run_id"],
        "registry_version": REGISTRY_VERSION,
        "frozen_execution_plan": {"schema": "broken"},
    }
    await repo.log_event(run["run_id"], "entry_codex_v1_shadow_sample_started", invalid)
    repo.fail_events["entry_codex_v1_shadow_sample_dropped"] = 1
    manager = _manager(settings=_settings(), repo=repo)
    await manager._rehydrate_v1463_shadow_samples(run)
    assert run["run_id"] not in manager._v1463_shadow_rehydrated_runs
    await manager._rehydrate_v1463_shadow_samples(run)
    dropped = [d for _, et, d in repo.events if et == "entry_codex_v1_shadow_sample_dropped"]
    assert len(dropped) == 1
    assert dropped[0]["data_quality_status"] == "DATA_INCOMPLETE"


@pytest.mark.asyncio
async def test_v132_started_and_terminal_drop_are_durable_across_restart() -> None:
    repo = _DurableLedgerRepo()
    manager = _manager(settings=_settings(), repo=repo)
    sample = {
        "sample_id": "tp_pair_1",
        "run_id": "cry3mn_v132_restart",
        "symbol": "ETHUSDC",
        "side": "LONG",
        "strategy": "S1_BB_RSI",
        "shadow_lane_family": "RP1",
        "entry_price": 100.0,
        "tp_price": 101.0,
        "sl_price": 99.0,
        "start_ms": 1_000,
        "requested_notional_usdc": 50.0,
        "features": {},
    }
    await manager._start_codex_v132_tp_policy_sample(sample, source_type="shadow")
    assert "tp_pair_1" in manager._codex_v132_tp_policy_samples
    repo.terminal_tp_rows = [{
        "run_id": sample["run_id"],
        "status": "CANCELLED",
        "armed_at_ms": 1,
    }]
    restarted = _manager(settings=_settings(), repo=repo)
    await restarted._reconcile_terminal_v132_tp_policy_samples()
    assert "tp_pair_1" not in restarted._codex_v132_tp_policy_samples
    assert any(et == "entry_codex_v1_tp_policy_shadow_dropped" for _, et, _ in repo.events)


@pytest.mark.asyncio
async def test_terminal_scanners_retry_typed_read_failure() -> None:
    repo = _DurableLedgerRepo()
    shadow_run = _ordinary_codex_run("cry3mn_v1463_read_retry")
    first = _manager(settings=_settings(), repo=repo)
    first._dca_enabled = False
    await _start_unknown_formal_sample(first, shadow_run)
    repo.terminal_shadow_rows = [{
        **shadow_run,
        "status": "ENTRY_EXPIRED",
        "exit_reason": "signal_timeout",
    }]
    restarted = _manager(settings=_settings(), repo=repo)
    repo.fail_typed_reads = 1
    with pytest.raises(RuntimeError, match="rehydrate incomplete"):
        await restarted._reconcile_recent_terminal_v1463_shadow_samples()
    assert restarted._v1463_terminal_startup_reconciled is False
    await restarted._reconcile_recent_terminal_v1463_shadow_samples()
    assert restarted._v1463_terminal_startup_reconciled is True

    tp_sample = {
        "sample_id": "tp_read_retry",
        "run_id": "cry3mn_v132_read_retry",
        "symbol": "ETHUSDC",
        "side": "LONG",
        "strategy": "S1_BB_RSI",
        "shadow_lane_family": "RP1",
        "entry_price": 100.0,
        "tp_price": 101.0,
        "sl_price": 99.0,
        "start_ms": 1_000,
        "requested_notional_usdc": 50.0,
        "features": {},
    }
    await first._start_codex_v132_tp_policy_sample(tp_sample, source_type="shadow")
    repo.terminal_tp_rows = [{"run_id": tp_sample["run_id"], "status": "CANCELLED"}]
    restarted_tp = _manager(settings=_settings(), repo=repo)
    repo.fail_typed_reads = 1
    with pytest.raises(RuntimeError, match="rehydrate incomplete"):
        await restarted_tp._reconcile_terminal_v132_tp_policy_samples()
    assert restarted_tp._v132_terminal_startup_reconciled is False
    await restarted_tp._reconcile_terminal_v132_tp_policy_samples()
    assert restarted_tp._v132_terminal_startup_reconciled is True


@pytest.mark.asyncio
async def test_modern_loop_authority_read_failure_fails_closed_but_legacy_rehydrates() -> None:
    class BrokenAuthorityRepo(FakeRepo):
        async def get_events_by_types(self, run_id, event_types, limit=30):
            raise RuntimeError("db unavailable")

    manager = _manager(settings=_settings(), repo=BrokenAuthorityRepo())
    modern = {"run_id": "modern", "params": {"loop_authority_id": "a", "loop_rearm_authorized": True}}
    legacy = {"run_id": "legacy", "params": {"loop_count": 3}}
    assert await manager._loop_rearm_allowed_for_row(modern) is False
    assert await manager._loop_rearm_allowed_for_row(legacy) is True
