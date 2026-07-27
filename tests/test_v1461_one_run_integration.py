from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.gridbot.mainnet import one_run as one_run_module
from src.gridbot.mainnet.one_run import V1461_ADAPTIVE_CANARY_CONTRACT
from src.gridbot.mainnet.v1461_adaptive_gate import promotion_key
from src.gridbot.strategy.codex_v1_live import LANES, lane_code_from_name
from tests.test_v1460_one_run_integration import (
    _adaptive_run,
    _codex,
    _manager,
    _position,
    _session,
    _v1460_settings,
    _wildcat,
)


def _settings(**overrides):
    values = {
        "mainnet_codex_v1461_candidate_selector_enabled": True,
        "mainnet_codex_v1461_regime_gate_enabled": True,
        "mainnet_codex_v1461_live_enforcement_enabled": True,
        "mainnet_codex_v1461_shadow_all_strategy_rejects_enabled": True,
    }
    values.update(overrides)
    return _v1460_settings(**values)


def _seed_fast_evidence(manager, *, family: str, lane: str, state: str):
    session = _session(manager)
    now_ms = 100_000_000
    key = promotion_key(family, lane, state)
    session["v1461_gate_evidence"] = {
        key: {
            "opportunity_ids": [f"o{i}" for i in range(4)],
            "episode_ids": ["prior-ep"],
            "opportunities": 4,
            "evaluable": 4,
            "tp_first": 3,
            "sl_first": 1,
            "no_fill": 0,
            "ambiguous": 0,
            "incomplete": 0,
            "net_pnl_usdc": 0.04,
            "last_outcome": "tp1_first",
            "last_outcome_at_ms": now_ms - 1_000,
            "policy_hash": manager._v1461_gate_config().policy_hash,
            "records": [
                {
                    "opportunity_id": f"o{i}",
                    "episode_id": "prior-ep",
                    # Keep the single loss as the oldest observation.  The
                    # FAST gate intentionally refuses a route whose latest
                    # first-touch outcome is SL-first.
                    "outcome": "sl_first" if i == 0 else "tp1_first",
                    "net_pnl_usdc": -0.02 if i == 0 else 0.02,
                    "resolved_at_ms": now_ms - (4 - i) * 1_000,
                    "processed_at_ms": now_ms - (4 - i) * 1_000,
                    "data_complete": True,
                    "policy_hash": manager._v1461_gate_config().policy_hash,
                }
                for i in range(4)
            ],
        }
    }
    session["v1461_episode_states"] = {}
    session["v1461_consumed_tokens"] = set()
    session["v1461_gate_loss_streaks"] = {}
    session["v1461_gate_net_pnl_usdc"] = {}
    session["v1461_quarantined_keys"] = set()
    session["v1461_paid_results"] = []
    return session, key, now_ms


def test_v1461_defaults_off_preserve_v1460_path() -> None:
    manager = _manager(settings=_v1460_settings())
    run = _adaptive_run()
    decision = _codex()
    assert manager._v1461_candidate_active(run) is False
    assert manager._v1461_config_selected() is False


def test_v1461_readiness_requires_complete_reviewed_switch_set() -> None:
    manager = _manager(settings=_settings())
    requested, missing = manager._v1461_enforcement_readiness()
    assert requested is True
    assert missing == ()
    assert manager._adaptive_canary_contract() == V1461_ADAPTIVE_CANARY_CONTRACT


def test_v1461_readiness_requires_v1460_execution_safety() -> None:
    manager = _manager(
        settings=_settings(mainnet_codex_v1460_live_enforcement_enabled=False)
    )
    requested, missing = manager._v1461_enforcement_readiness()
    assert requested is True
    assert "mainnet_codex_v1460_live_enforcement_enabled" in missing
    assert manager._v1461_enforcement_active(_adaptive_run()) is False


@pytest.mark.parametrize(
    ("setting_name", "unsafe_value", "expected_missing"),
    [
        (
            "mainnet_codex_v1459_regime_switch_enabled",
            True,
            "mainnet_codex_v1459_regime_switch_enabled=false",
        ),
        (
            "mainnet_codex_v1460_target_paid_closed_fills",
            19,
            "mainnet_codex_v1460_target_paid_closed_fills=20",
        ),
        (
            "mainnet_codex_v1460_max_duration_seconds",
            60,
            "mainnet_codex_v1460_max_duration_seconds=259200",
        ),
        (
            "mainnet_codex_v1460_checkpoint_fills",
            4,
            "mainnet_codex_v1460_checkpoint_fills=5",
        ),
    ],
)
def test_v1461_readiness_inherits_full_v1460_contract(
    setting_name: str,
    unsafe_value: object,
    expected_missing: str,
) -> None:
    manager = _manager(settings=_settings(**{setting_name: unsafe_value}))
    requested, missing = manager._v1461_enforcement_readiness()
    assert requested is True
    assert expected_missing in missing
    assert manager._v1460_entry_safety_active(_adaptive_run()) is False
    assert manager._v1461_enforcement_active(_adaptive_run()) is False


@pytest.mark.parametrize(
    ("lane", "trend_up", "trend_down", "side"),
    [
        ("W6A", "SUPPORTIVE", "ADVERSE", "LONG"),
        ("W6B", "SUPPORTIVE", "ADVERSE", "LONG"),
        ("W6C", "ADVERSE", "SUPPORTIVE", "SHORT"),
    ],
)
def test_w6_lane_direction_matches_regime_compatibility(
    lane: str,
    trend_up: str,
    trend_down: str,
    side: str,
) -> None:
    manager = _manager(settings=_settings())
    assert manager._v1461_compatibility(lane, "TREND_UP", "").value == trend_up
    assert manager._v1461_compatibility(lane, "TREND_DOWN", "").value == trend_down
    assert manager._v1461_compatibility(lane, "RANGE", "").value == "NEUTRAL"
    assert manager._v1461_compatibility(lane, "SHOCK", "").value == "HARD_BLOCK"
    mapped_sides = {
        lane_code_from_name(spec.name, spec.side): spec.side for spec in LANES
    }
    assert mapped_sides[lane] == side


@pytest.mark.parametrize(
    "reason",
    [
        "v134_w6a_weak_drift_daily_limit_block",
        "w6a_bad_payoff_geometry_blocked",
        "w6a_risk_cap_too_small",
    ],
)
def test_governance_payoff_and_sizing_rejects_are_never_promotable(reason) -> None:
    manager = _manager(settings=_settings())
    _family, _gate_class, eligible = manager._v1461_gate_taxonomy(reason, "W6A")
    assert eligible is False


@pytest.mark.asyncio
async def test_adverse_regime_blocks_incumbent_accept() -> None:
    manager = _manager(settings=_settings())
    run = _adaptive_run()
    decision = _codex(market_state="RP1:pullback", lane_code="RP1")
    decision.metrics["v1459_regime_state"] = "TREND_DOWN"
    result = await manager._v1461_apply_gate_policy(
        run,
        decision,
        incumbent_accepted=True,
        gate_family_id="INCUMBENT_RP1",
        promotion_eligible=False,
    )
    assert result.accepted is False
    assert result.metrics["v1461_adaptive_gate"]["action_mode"] == "SHADOW_BLOCK"


def test_current_v1459_regime_wins_over_prior_v1461_annotation() -> None:
    manager = _manager(settings=_settings())
    decision = _codex(market_state="W6A:clean", lane_code="W6A")
    decision.metrics["v1459_regime_state"] = "TREND_DOWN"
    decision.metrics["v1461_adaptive_gate"] = {"market_state": "TREND_UP"}
    assert manager._v1461_coarse_regime(_adaptive_run(), decision) == "TREND_DOWN"


def test_episode_rolls_after_confirmed_observation_gap() -> None:
    manager = _manager(settings=_settings())
    _session(manager)
    first = manager._v1461_episode_id(
        "W6A_ENTRY_RISK", "W6A", "TREND_UP", 1_000_000
    )
    same = manager._v1461_episode_id(
        "W6A_ENTRY_RISK", "W6A", "TREND_UP", 1_060_000
    )
    after_gap = manager._v1461_episode_id(
        "W6A_ENTRY_RISK", "W6A", "TREND_UP", 1_360_000
    )
    assert same == first
    assert after_gap != first


@pytest.mark.asyncio
async def test_supportive_old_gate_release_is_25_usdc_fast_probe(monkeypatch) -> None:
    manager = _manager(settings=_settings())
    run = _adaptive_run()
    decision = _codex(market_state="W6A:clean", lane_code="W6A")
    decision.metrics["v1459_regime_state"] = "TREND_UP"
    _, _, now_ms = _seed_fast_evidence(
        manager, family="W6A_ENTRY_RISK", lane="W6A", state="TREND_UP"
    )
    monkeypatch.setattr("src.gridbot.mainnet.one_run.time.time", lambda: now_ms / 1000)
    result = await manager._v1461_apply_gate_policy(
        run,
        decision,
        incumbent_accepted=False,
        gate_family_id="W6A_ENTRY_RISK",
        promotion_eligible=True,
    )
    policy = result.metrics["v1461_adaptive_gate"]
    assert result.accepted is True
    assert result.requested_notional_usdc == 25.0
    assert policy["action_mode"] == "FAST_PROBE_0_5"
    assert policy["token_id"]


@pytest.mark.asyncio
async def test_fast_probe_token_is_durably_single_use_before_submit(monkeypatch) -> None:
    manager = _manager(settings=_settings())
    run = _adaptive_run()
    decision = _codex(market_state="W6A:clean", lane_code="W6A")
    decision.metrics["v1459_regime_state"] = "TREND_UP"
    session, _, now_ms = _seed_fast_evidence(
        manager, family="W6A_ENTRY_RISK", lane="W6A", state="TREND_UP"
    )
    monkeypatch.setattr("src.gridbot.mainnet.one_run.time.time", lambda: now_ms / 1000)
    applied = await manager._v1461_apply_gate_policy(
        run,
        decision,
        incumbent_accepted=False,
        gate_family_id="W6A_ENTRY_RISK",
        promotion_eligible=True,
    )
    payload = manager._adaptive_decision_payload(run, applied, raw_decision=decision)

    async def record(_payload):
        return True

    async def checkpoint(_session, *, checkpoint_at_ms):
        return SimpleNamespace(continue_live=True, status="OK", reason=None)

    monkeypatch.setattr(manager, "_adaptive_record_opportunity", record)
    monkeypatch.setattr(manager._v1459_guard, "checkpoint", checkpoint)
    assert await manager._adaptive_gate_before_submit(run, payload) is False
    token = payload["v1461_adaptive_gate"]["token_id"]
    assert token in session["v1461_consumed_tokens"]
    assert await manager._adaptive_gate_before_submit(run, payload) is True


@pytest.mark.asyncio
async def test_fast_probe_loses_cross_process_checkpoint_race_fail_closed(monkeypatch) -> None:
    manager = _manager(settings=_settings())
    run = _adaptive_run()
    decision = _codex(market_state="W6A:clean", lane_code="W6A")
    decision.metrics["v1459_regime_state"] = "TREND_UP"
    session, _, now_ms = _seed_fast_evidence(
        manager, family="W6A_ENTRY_RISK", lane="W6A", state="TREND_UP"
    )
    monkeypatch.setattr("src.gridbot.mainnet.one_run.time.time", lambda: now_ms / 1000)
    applied = await manager._v1461_apply_gate_policy(
        run,
        decision,
        incumbent_accepted=False,
        gate_family_id="W6A_ENTRY_RISK",
        promotion_eligible=True,
    )
    payload = manager._adaptive_decision_payload(run, applied, raw_decision=decision)

    async def record(_payload):
        return True

    async def checkpoint(_session, *, checkpoint_at_ms):
        return SimpleNamespace(
            continue_live=True,
            status="ACTIVE",
            reason="IDEMPOTENT_RETRY",
        )

    monkeypatch.setattr(manager, "_adaptive_record_opportunity", record)
    monkeypatch.setattr(manager._v1459_guard, "checkpoint", checkpoint)
    assert await manager._adaptive_gate_before_submit(run, payload) is True
    assert session["stop_requested"] is True
    assert session["rearm_enabled"] is False
    assert session["safety_halt_reason"] == "promotion_token_claim_conflict"


def test_route_snapshot_round_trips_v1461_state() -> None:
    manager = _manager(settings=_settings())
    session = _session(manager)
    session.update(
        {
            "v1461_gate_evidence": {"A": {"opportunities": 4}},
            "v1461_episode_states": {"A": {"current_state": "TREND_UP"}},
            "v1461_consumed_tokens": {"token-1"},
            "v1461_gate_loss_streaks": {"A": 1},
            "v1461_gate_net_pnl_usdc": {"A": -0.01},
            "v1461_quarantined_keys": {"B"},
            "v1461_paid_results": [{"run_id": "r1"}],
        }
    )
    snapshot = manager._adaptive_route_stats_snapshot(session)
    assert snapshot["v1461_consumed_tokens"] == ["token-1"]
    assert snapshot["v1461_quarantined_keys"] == ["B"]
    assert snapshot["v1461_gate_evidence"]["A"]["opportunities"] == 4


def test_status_line_identifies_v1461_enforcement_and_durable_state() -> None:
    manager = _manager(settings=_settings())
    session = _session(manager)
    session["v1461_gate_evidence"] = {"A": {}, "B": {}}
    session["v1461_consumed_tokens"] = {"token-1"}
    session["v1461_quarantined_keys"] = {"W6A_ENTRY_RISK|W6A|TREND_UP"}
    line = manager._v1461_status_line(session)
    assert line is not None
    assert "candidate=ON" in line
    assert "enforcement=ON" in line
    assert "shadow-all=ON" in line
    assert "evidence keys=2" in line
    assert "used tokens=1" in line


def test_freshness_reaggregates_records_instead_of_refreshing_old_wins(monkeypatch) -> None:
    manager = _manager(settings=_settings())
    session = _session(manager)
    now_ms = 100_000_000
    monkeypatch.setattr("src.gridbot.mainnet.one_run.time.time", lambda: now_ms / 1000)
    key = promotion_key("W6A_ENTRY_RISK", "W6A", "TREND_UP")
    policy_hash = manager._v1461_gate_config().policy_hash
    old_ms = now_ms - (6 * 60 * 60 + 10) * 1000
    session["v1461_gate_evidence"] = {
        key: {
            "opportunities": 4,
            "evaluable": 4,
            "tp_first": 4,
            "net_pnl_usdc": 0.08,
            "last_outcome_at_ms": now_ms - 1_000,
            "records": [
                {
                    "opportunity_id": f"old-{i}",
                    "episode_id": "old",
                    "outcome": "tp1_first",
                    "net_pnl_usdc": 0.02,
                    "resolved_at_ms": old_ms,
                    "data_complete": True,
                    "policy_hash": policy_hash,
                }
                for i in range(3)
            ]
            + [
                {
                    "opportunity_id": "fresh",
                    "episode_id": "fresh",
                    "outcome": "tp1_first",
                    "net_pnl_usdc": 0.02,
                    "resolved_at_ms": now_ms - 1_000,
                    "data_complete": True,
                    "policy_hash": policy_hash,
                }
            ],
        }
    }
    evidence = manager._v1461_evidence_input(key)
    assert evidence.opportunities == 1
    assert evidence.tp_first == 1
    assert evidence.net_pnl_usdc == pytest.approx(0.02)


@pytest.mark.parametrize("invalid_outcome", ["ambiguous_both", "incomplete"])
@pytest.mark.asyncio
async def test_recent_invalid_shadow_record_blocks_fast_probe(
    monkeypatch, invalid_outcome: str
) -> None:
    manager = _manager(settings=_settings())
    decision = _codex(market_state="W6A:clean", lane_code="W6A")
    decision.metrics["v1459_regime_state"] = "TREND_UP"
    session, key, now_ms = _seed_fast_evidence(
        manager, family="W6A_ENTRY_RISK", lane="W6A", state="TREND_UP"
    )
    session["v1461_gate_evidence"][key]["records"].append(
        {
            "opportunity_id": f"invalid-{invalid_outcome}",
            "episode_id": "prior-ep",
            "outcome": invalid_outcome,
            "net_pnl_usdc": 0.0,
            "resolved_at_ms": 0,
            "processed_at_ms": now_ms - 500,
            "data_complete": False,
            "policy_hash": manager._v1461_gate_config().policy_hash,
        }
    )
    monkeypatch.setattr("src.gridbot.mainnet.one_run.time.time", lambda: now_ms / 1000)
    result = await manager._v1461_apply_gate_policy(
        _adaptive_run(),
        decision,
        incumbent_accepted=False,
        gate_family_id="W6A_ENTRY_RISK",
        promotion_eligible=True,
    )
    policy = result.metrics["v1461_adaptive_gate"]
    assert result.accepted is False
    assert policy["action_mode"] == "SHADOW_BLOCK"
    assert policy["evidence_gate"]["data_complete"] is False


@pytest.mark.asyncio
async def test_legacy_reprobation_evaluates_shared_regime_once(monkeypatch) -> None:
    manager = _manager(
        settings=_settings(mainnet_codex_v1459_regime_switch_enabled=True)
    )
    _session(manager)
    run = _adaptive_run("cry3mn_v1461_single_regime_eval")
    wildcat = _wildcat()
    raw = _codex(market_state="W6B:ordinary", lane_code="W6B")
    features = {
        "kill_switch": "off",
        "open_position": "false",
        "open_entry_order": "0",
        "open_reduce_order": "",
        "feature_age_seconds": 0.0,
    }

    async def build_features(*_args, **_kwargs):
        return features

    async def keep_v136(codex, _features):
        return codex

    async def keep_v139(_decision, _raw, codex, _features):
        return codex

    async def keep_v1436(_run, _decision, _raw, codex, _features):
        return codex

    async def no_shadow(*_args, **_kwargs):
        return None

    async def blocked_gate(*_args, **_kwargs):
        return True

    monkeypatch.setattr(manager, "_build_codex_v1_live_features_for_decision", build_features)
    monkeypatch.setattr(manager, "_codex_v136_maybe_promote_nl_near_w1d_live200", keep_v136)
    monkeypatch.setattr(manager, "_codex_v139_maybe_promote_reprice_canary", keep_v139)
    monkeypatch.setattr(manager, "_codex_v1436_guard_late_stups_after_veto_edge", keep_v1436)
    monkeypatch.setattr(manager, "_codex_v1433_guard_clean_high_side_override", lambda _features, codex, _side: codex)
    monkeypatch.setattr(manager, "_codex_v1439_apply_shadow_score", lambda _features, _raw, codex: codex)
    monkeypatch.setattr(manager, "_codex_v1_live_research_block_reason", lambda *_args: "codex_v1_w6_weak_drift_block")
    monkeypatch.setattr(manager, "_start_codex_v1_shadow_sample", no_shadow)
    monkeypatch.setattr(manager, "_adaptive_gate_before_submit", blocked_gate)
    monkeypatch.setattr(one_run_module, "select_codex_v1_lane", lambda _features: raw)
    monkeypatch.setattr(one_run_module, "apply_v1427_five_window_decision", lambda _features, codex: codex)
    monkeypatch.setattr(one_run_module, "apply_v1430_loss_prune_decision", lambda _features, codex: codex)
    monkeypatch.setattr(one_run_module, "apply_v1436_live_hotfix_decision", lambda _features, codex: codex)
    monkeypatch.setattr(one_run_module, "codex_v1_feature_gaps", lambda _features: ())
    monkeypatch.setattr(one_run_module, "live_preflight_rejections", lambda _features: ())

    original_evaluate = one_run_module.V1459RegimeRuntime.evaluate
    calls = []

    def spy_evaluate(runtime, *args, **kwargs):
        calls.append((args, kwargs))
        return original_evaluate(runtime, *args, **kwargs)

    monkeypatch.setattr(one_run_module.V1459RegimeRuntime, "evaluate", spy_evaluate)
    await manager._apply_codex_v1_gate(
        run,
        wildcat,
        [],
        rng15=40.0,
        drift_bp=0.0,
    )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_terminal_reconciliation_updates_promoted_paid_evidence(monkeypatch) -> None:
    manager = _manager(settings=_settings())
    session = _session(manager)
    policy_hash = manager._v1461_gate_config().policy_hash
    run = _adaptive_run("cry3mn_v1461_terminal", status="COMPLETED")
    run["signal_json"] = json.dumps(
        {
            "codex_v1": {"lane_code": "W6A", "metrics": {"market_state": "W6A:clean"}},
            "adaptive": {
                "decision": {
                    "live_effective_route": "NORMAL",
                    "live_effective_action": {"action_id": "FAST_PROBE_0_5"},
                    "v1461_adaptive_gate": {
                        "action_mode": "FAST_PROBE_0_5",
                        "incumbent_accepted": False,
                        "gate_family_id": "W6A_ENTRY_RISK",
                        "lane": "W6A",
                        "market_state": "TREND_UP",
                        "policy_hash": policy_hash,
                    },
                }
            },
        }
    )

    async def checkpoint(_session, *, checkpoint_at_ms):
        return SimpleNamespace(continue_live=True, status="OK", reason=None)

    async def arm(*, initial=False):
        return "armed"

    monkeypatch.setattr(manager._v1459_guard, "checkpoint", checkpoint)
    monkeypatch.setattr(manager, "_arm_adaptive_run", arm)
    assert await manager._adaptive_after_terminal_unlocked(
        run, 0.02, "TP", paid_closed_fill=True
    ) is True
    key = promotion_key("W6A_ENTRY_RISK", "W6A", "TREND_UP")
    evidence = session["v1461_gate_evidence"][key]
    assert evidence["paid_complete"] == 1
    assert evidence["paid_wins"] == 1
    assert evidence["first_probe_net_pnl_usdc"] == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_new_episode_winning_probe_replaces_failed_attempt_stats(monkeypatch) -> None:
    manager = _manager(settings=_settings())
    session = _session(manager)
    policy_hash = manager._v1461_gate_config().policy_hash
    key = promotion_key("W6A_ENTRY_RISK", "W6A", "TREND_UP")
    session["v1461_gate_evidence"] = {
        key: {
            "policy_hash": policy_hash,
            "records": [],
            "first_probe_net_pnl_usdc": -0.02,
            "first_probe_episode_id": "ep-1",
            "paid_complete": 1,
            "paid_wins": 0,
            "paid_net_pnl_usdc": -0.02,
            "paid_integrity_complete": True,
        }
    }
    session["v1461_paid_results"] = [{"run_id": "old-loss", "net_pnl_usdc": -0.02}]
    session["v1461_gate_net_pnl_usdc"] = {key: -0.02}
    run = _adaptive_run("cry3mn_v1461_retry", status="COMPLETED")
    run["signal_json"] = json.dumps(
        {
            "codex_v1": {"lane_code": "W6A", "metrics": {"market_state": "W6A:clean"}},
            "adaptive": {
                "decision": {
                    "live_effective_route": "NORMAL",
                    "live_effective_action": {"action_id": "FAST_PROBE_0_5"},
                    "v1461_adaptive_gate": {
                        "action_mode": "FAST_PROBE_0_5",
                        "incumbent_accepted": False,
                        "gate_family_id": "W6A_ENTRY_RISK",
                        "lane": "W6A",
                        "market_state": "TREND_UP",
                        "episode_id": "ep-2",
                        "policy_hash": policy_hash,
                    },
                }
            },
        }
    )

    async def checkpoint(_session, *, checkpoint_at_ms):
        return SimpleNamespace(continue_live=True, status="OK", reason=None)

    async def arm(*, initial=False):
        return "armed"

    monkeypatch.setattr(manager._v1459_guard, "checkpoint", checkpoint)
    monkeypatch.setattr(manager, "_arm_adaptive_run", arm)
    assert await manager._adaptive_after_terminal_unlocked(
        run, 0.02, "TP", paid_closed_fill=True
    ) is True
    evidence = session["v1461_gate_evidence"][key]
    assert evidence["paid_complete"] == 1
    assert evidence["paid_wins"] == 1
    assert evidence["paid_net_pnl_usdc"] == pytest.approx(0.02)
    assert evidence["first_probe_net_pnl_usdc"] == pytest.approx(0.02)
    assert evidence["first_probe_episode_id"] == "ep-2"
    assert session["v1461_paid_results"][0]["run_id"] == "old-loss"
    assert session["v1461_gate_net_pnl_usdc"][key] == pytest.approx(0.0)


@pytest.mark.parametrize("invalid_pnl", [None, "not-a-number", float("nan"), float("inf")])
@pytest.mark.asyncio
async def test_invalid_shadow_pnl_is_incomplete_and_not_promotable(
    monkeypatch, invalid_pnl
) -> None:
    manager = _manager(settings=_settings())
    session = _session(manager)
    now_ms = 100_000_000
    monkeypatch.setattr("src.gridbot.mainnet.one_run.time.time", lambda: now_ms / 1000)

    async def checkpoint(_session, *, checkpoint_at_ms):
        return SimpleNamespace(continue_live=True, status="OK", reason=None)

    monkeypatch.setattr(manager._v1459_guard, "checkpoint", checkpoint)
    policy_hash = manager._v1461_gate_config().policy_hash
    await manager._v1461_record_shadow_evidence(
        {
            "promotion_eligible": True,
            "reject_stage": "strategy",
            "fill_model": "limit_touch",
            "diagnostic_only": False,
            "v1461_policy_hash": policy_hash,
            "gate_family_id": "W6A_ENTRY_RISK",
            "legacy_lane_code": "W6A",
            "coarse_market_state": "TREND_UP",
            "opportunity_id": f"bad-{invalid_pnl!r}",
            "episode_id": "ep-1",
            "shadow_outcome": "tp1_first",
            "paper_pnl_usdc_after_fee": invalid_pnl,
            "resolved_ts": now_ms - 1,
        }
    )
    key = promotion_key("W6A_ENTRY_RISK", "W6A", "TREND_UP")
    record = session["v1461_gate_evidence"][key]["records"][0]
    assert record["data_complete"] is False
    assert record["pnl_valid"] is False
    facts = manager._v1461_evidence_input(key)
    assert facts.evaluable == 0
    assert facts.tp_first == 0
    assert facts.incomplete == 1


@pytest.mark.asyncio
async def test_promotion_shadow_uses_aggtrades_and_outcome_aware_costs(
    monkeypatch,
) -> None:
    class AggTradeClient:
        def __init__(self):
            from tests.test_mainnet_one_run_maker import FakeClient

            self._base = FakeClient()
            self.calls = []

        def __getattr__(self, name):
            return getattr(self._base, name)

        async def get_agg_trades(self, symbol, **kwargs):
            self.calls.append((symbol, kwargs))
            return [
                {"a": 10, "T": 2_000, "p": "100.0"},
                {"a": 11, "T": 3_000, "p": "99.0"},
            ]

    client = AggTradeClient()
    manager = _manager(settings=_settings(), client=client)
    run = _adaptive_run("cry3mn_v1461_aggtrade")
    sample = {
        "sample_id": "sample-aggtrade",
        "opportunity_id": "opp-aggtrade",
        "run_id": run["run_id"],
        "symbol": "ETHUSDC",
        "side": "LONG",
        "start_ms": 1_000,
        "entry_price": 100.0,
        "tp_price": 101.0,
        "sl_price": 99.0,
        "entry_ttl_s": 10,
        "outcome_ttl_s": 20,
        "requested_notional_usdc": 25.0,
        "fill_model": "limit_touch",
        "promotion_eligible": True,
        "diagnostic_only": False,
    }
    manager._codex_v1_shadow_samples[sample["sample_id"]] = sample
    captured = []

    async def capture(key, active_sample, outcome, *, terminal_reason=None):
        captured.append((key, dict(outcome)))
        manager._codex_v1_shadow_samples.pop(key, None)

    monkeypatch.setattr(manager, "_log_codex_v1_shadow_outcome", capture)
    monkeypatch.setattr("src.gridbot.mainnet.one_run.time.time", lambda: 30.0)
    await manager._update_codex_v1_shadow_outcomes(run, [])
    assert client.calls
    assert captured[0][1]["shadow_outcome"] == "sl_first"
    assert captured[0][1]["exit_liquidity"] == "TAKER"
    assert captured[0][1]["estimated_fee_bp"] > 0.0
    assert captured[0][1]["conservative_slippage_buffer_bp"] > 0.0
    assert captured[0][1]["data_complete"] is True


@pytest.mark.asyncio
async def test_same_ms_capped_page_does_not_hide_later_sl(monkeypatch) -> None:
    class PagedAggTradeClient:
        def __init__(self):
            from tests.test_mainnet_one_run_maker import FakeClient

            self._base = FakeClient()
            self.calls = 0

        def __getattr__(self, name):
            return getattr(self._base, name)

        async def get_agg_trades(self, symbol, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return [
                    {"a": 10, "T": 1_000, "p": "100.0"},
                    {"a": 11, "T": 1_000, "p": "101.0"},
                ]
            return [{"a": 12, "T": 1_000, "p": "99.0"}]

    client = PagedAggTradeClient()
    manager = _manager(
        settings=_settings(
            mainnet_codex_v1460_weak_shadow_max_pages=1,
            mainnet_codex_v1460_weak_shadow_page_limit=2,
        ),
        client=client,
    )
    run = _adaptive_run("cry3mn_v1461_same_ms")
    sample = {
        "sample_id": "sample-same-ms",
        "opportunity_id": "opp-same-ms",
        "run_id": run["run_id"],
        "symbol": "ETHUSDC",
        "side": "LONG",
        "start_ms": 1_000,
        "entry_price": 100.0,
        "tp_price": 101.0,
        "sl_price": 99.0,
        "entry_ttl_s": 10,
        "outcome_ttl_s": 20,
        "requested_notional_usdc": 25.0,
        "fill_model": "limit_touch",
        "promotion_eligible": True,
        "diagnostic_only": False,
    }
    manager._codex_v1_shadow_samples[sample["sample_id"]] = sample
    captured = []

    async def capture(key, active_sample, outcome, *, terminal_reason=None):
        captured.append(dict(outcome))
        manager._codex_v1_shadow_samples.pop(key, None)

    monkeypatch.setattr(manager, "_log_codex_v1_shadow_outcome", capture)
    monkeypatch.setattr("src.gridbot.mainnet.one_run.time.time", lambda: 30.0)
    await manager._update_codex_v1_shadow_outcomes(run, [])
    assert captured == []
    await manager._update_codex_v1_shadow_outcomes(run, [])
    assert captured[0]["shadow_outcome"] == "sl_first"
    assert captured[0]["ambiguity_flag"] is True


@pytest.mark.asyncio
async def test_aggtrade_cost_payload_is_not_overwritten_by_legacy_pnl() -> None:
    from tests.test_mainnet_one_run_maker import FakeRepo

    repo = FakeRepo()
    manager = _manager(settings=_settings(), repo=repo)
    sample = {
        "sample_id": "sample-cost",
        "opportunity_id": "opp-cost",
        "run_id": "cry3mn_v1461_cost",
        "symbol": "ETHUSDC",
        "side": "LONG",
        "start_ms": 1_000,
        "entry_price": 100.0,
        "tp_price": 101.0,
        "sl_price": 99.0,
        "entry_ttl_s": 10,
        "outcome_ttl_s": 20,
        "requested_notional_usdc": 25.0,
        "fill_model": "limit_touch",
        "promotion_eligible": True,
        "diagnostic_only": False,
    }
    manager._codex_v1_shadow_samples[sample["sample_id"]] = sample
    await manager._log_codex_v1_shadow_outcome(
        sample["sample_id"],
        sample,
        {
            "shadow_outcome": "sl_first",
            "filled": True,
            "filled_ts": 2_000,
            "resolved_ts": 3_000,
            "exit_reference_price": 99.0,
            "paper_pnl_bp_before_fee": -100.0,
            "paper_pnl_bp_after_fee": -104.4,
            "paper_pnl_usdc_after_fee": -0.261,
            "estimated_fee_bp": 4.0,
            "conservative_slippage_buffer_bp": 0.4,
            "data_complete": True,
        },
    )
    details = repo.events[-1][2]
    assert details["paper_pnl_usdc_after_fee"] == pytest.approx(-0.261)
    assert details["estimated_fee_bp"] == pytest.approx(4.0)
    assert details["conservative_slippage_buffer_bp"] == pytest.approx(0.4)


def test_legacy_shadow_sl_path_uses_taker_fee_and_slippage() -> None:
    manager = _manager(settings=_settings())
    costs = manager._codex_v1_shadow_paper_pnl(
        {
            "side": "LONG",
            "entry_price": 100.0,
            "sl_price": 99.9,
            "requested_notional_usdc": 25.0,
            "features": {"maker_fee_bp": 0.0},
        },
        {"shadow_outcome": "sl_first", "exit_reference_price": 99.9},
    )
    assert costs["exit_liquidity"] == "TAKER"
    assert costs["estimated_fee_bp"] == pytest.approx(4.0)
    assert costs["conservative_slippage_buffer_bp"] == pytest.approx(0.4)
    assert costs["paper_pnl_bp_after_fee"] == pytest.approx(-14.4)


@pytest.mark.asyncio
async def test_complete_max_hold_counts_as_evaluable_not_incomplete(monkeypatch) -> None:
    manager = _manager(settings=_settings())
    _session(manager)
    now_ms = 100_000_000
    monkeypatch.setattr("src.gridbot.mainnet.one_run.time.time", lambda: now_ms / 1000)

    async def checkpoint(_session, *, checkpoint_at_ms):
        return SimpleNamespace(continue_live=True, status="OK", reason=None)

    monkeypatch.setattr(manager._v1459_guard, "checkpoint", checkpoint)
    policy_hash = manager._v1461_gate_config().policy_hash
    await manager._v1461_record_shadow_evidence(
        {
            "promotion_eligible": True,
            "reject_stage": "strategy",
            "fill_model": "limit_touch",
            "diagnostic_only": False,
            "v1461_policy_hash": policy_hash,
            "gate_family_id": "W6A_ENTRY_RISK",
            "legacy_lane_code": "W6A",
            "coarse_market_state": "TREND_UP",
            "opportunity_id": "max-hold-complete",
            "episode_id": "ep-1",
            "shadow_outcome": "max_hold",
            "paper_pnl_usdc_after_fee": -0.005,
            "evidence_source": "binance_aggTrade",
            "data_complete": True,
            "data_quality": {"complete": True},
            "start_ms": now_ms - 6_000,
            "entry_ttl_s": 2,
            "outcome_ttl_s": 5,
            "resolved_ts": now_ms - 1,
        }
    )
    key = promotion_key("W6A_ENTRY_RISK", "W6A", "TREND_UP")
    facts = manager._v1461_evidence_input(key)
    assert facts.max_hold == 1
    assert facts.evaluable == 1
    assert facts.incomplete == 0
    assert facts.net_pnl_usdc == pytest.approx(-0.005)


@pytest.mark.parametrize(
    ("outcome", "resolved_offset"),
    [("max_hold", -2_000), ("tp1_first", -0.5)],
)
@pytest.mark.asyncio
async def test_incomplete_or_invalid_timing_cannot_enter_promotion_evidence(
    monkeypatch, outcome: str, resolved_offset: float
) -> None:
    manager = _manager(settings=_settings())
    _session(manager)
    now_ms = 100_000_000
    monkeypatch.setattr("src.gridbot.mainnet.one_run.time.time", lambda: now_ms / 1000)

    async def checkpoint(_session, *, checkpoint_at_ms):
        return SimpleNamespace(continue_live=True, status="OK", reason=None)

    monkeypatch.setattr(manager._v1459_guard, "checkpoint", checkpoint)
    policy_hash = manager._v1461_gate_config().policy_hash
    await manager._v1461_record_shadow_evidence(
        {
            "promotion_eligible": True,
            "reject_stage": "strategy",
            "fill_model": "limit_touch",
            "diagnostic_only": False,
            "v1461_policy_hash": policy_hash,
            "gate_family_id": "W6A_ENTRY_RISK",
            "legacy_lane_code": "W6A",
            "coarse_market_state": "TREND_UP",
            "opportunity_id": f"bad-timing-{outcome}",
            "episode_id": "ep-1",
            "shadow_outcome": outcome,
            "paper_pnl_usdc_after_fee": 0.01,
            "evidence_source": "binance_aggTrade",
            "data_complete": True,
            "data_quality": {"complete": True},
            "start_ms": now_ms - 5_000,
            "entry_ttl_s": 2,
            "outcome_ttl_s": 5,
            "resolved_ts": now_ms + resolved_offset,
        }
    )
    key = promotion_key("W6A_ENTRY_RISK", "W6A", "TREND_UP")
    facts = manager._v1461_evidence_input(key)
    assert facts.evaluable == 0
    assert facts.incomplete == 1


@pytest.mark.asyncio
async def test_pending_adverse_cancel_confirms_zero_fill_and_never_reprices(monkeypatch) -> None:
    manager = _manager(settings=_settings())
    run = _adaptive_run(
        "cry3mn_v1461_pending",
        status="ENTRY_PENDING",
        entry_order_id=123,
        signal_json=json.dumps({"adaptive": {"decision": {}}}),
    )
    completed = []
    advanced = []

    async def open_orders(_symbol):
        return [{"orderId": 123}]

    async def no_position(_symbol):
        return None

    async def block(_run):
        return {"reason": "v1461_pending_regime_invalidated"}

    async def cancel_confirm(*_args, **_kwargs):
        return {"status": "NO_FILL", "position": None}

    async def complete(*args):
        completed.append(args)

    async def advance(*args):
        advanced.append(args)

    async def forbidden_reprice(*_args, **_kwargs):
        raise AssertionError("regime cancellation must not replace/reprice")

    monkeypatch.setattr(manager._client, "get_open_orders", open_orders)
    monkeypatch.setattr(manager._client, "get_position", no_position)
    monkeypatch.setattr(manager, "_v1461_pending_entry_regime_block", block)
    monkeypatch.setattr(manager, "_v1460_cancel_confirm_entry", cancel_confirm)
    monkeypatch.setattr(manager._repo, "complete_run", complete)
    monkeypatch.setattr(manager, "_advance_loop_after_entry_failure", advance)
    monkeypatch.setattr(manager, "_maybe_requote_entry", forbidden_reprice)
    await manager._run_entry_pending(run)
    assert completed and completed[0][1] == "ENTRY_EXPIRED"
    assert advanced


@pytest.mark.asyncio
async def test_pending_cancel_race_fill_activates_protection_and_does_not_replace(monkeypatch) -> None:
    manager = _manager(settings=_settings())
    run = _adaptive_run(
        "cry3mn_v1461_pending_fill",
        status="ENTRY_PENDING",
        entry_order_id=321,
        signal_json=json.dumps({"adaptive": {"decision": {}}}),
    )
    activated = []

    async def open_orders(_symbol):
        return [{"orderId": 321}]

    async def no_position(_symbol):
        return None

    async def block(_run):
        return {"reason": "v1461_pending_regime_invalidated"}

    async def cancel_confirm(*_args, **_kwargs):
        return {
            "status": "FILLED",
            "position": _position(),
            "cancel_race_hard_sl_armed": True,
        }

    async def activate(*args, **kwargs):
        activated.append((args, kwargs))

    async def forbidden_complete(*_args, **_kwargs):
        raise AssertionError("a cancel-race fill must not be expired")

    monkeypatch.setattr(manager._client, "get_open_orders", open_orders)
    monkeypatch.setattr(manager._client, "get_position", no_position)
    monkeypatch.setattr(manager, "_v1461_pending_entry_regime_block", block)
    monkeypatch.setattr(manager, "_v1460_cancel_confirm_entry", cancel_confirm)
    monkeypatch.setattr(manager, "_activate_entry_fill", activate)
    monkeypatch.setattr(manager._repo, "complete_run", forbidden_complete)
    await manager._run_entry_pending(run)
    assert activated
    assert activated[0][1]["protection_prearmed"] is True
