from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import math

import pytest

from src.gridbot.mainnet.v1460_lane_adaptive import (
    AdaptiveActionMode,
    CNL_CONTROL_STATES,
    GLOBAL_SHADOW_BLOCK_STATES,
    LaneAdaptiveConfig,
    LaneAdaptiveDecision,
    LaneAdaptiveRiskInput,
    S1P_PULLBACK_STATES,
    STUP_CLEAN_STATES,
    STUP_WEAK_STATES,
    WeakStateShadowEvidence,
    apply_lane_adaptive_decision,
    policy_hash,
    select_lane_adaptive_decision,
)
from src.gridbot.strategy.codex_v1_live import CodexV1Decision


def _select(
    lane: str | None = "RP1",
    state: str | None = "fast_reclaim",
    *,
    accepted: bool = True,
    evidence: WeakStateShadowEvidence | None = None,
    risk: LaneAdaptiveRiskInput | None = None,
    config: LaneAdaptiveConfig | None = None,
) -> LaneAdaptiveDecision:
    return select_lane_adaptive_decision(
        lane=lane,
        market_state=state,
        incumbent_accepted=accepted,
        weak_evidence=evidence,
        risk=risk,
        config=config,
    )


def _codex_decision(**overrides: object) -> CodexV1Decision:
    values: dict[str, object] = {
        "accepted": True,
        "version": "v1.4.59",
        "baseline": "incumbent",
        "lane": "STUP-S",
        "lane_code": "STUP-S",
        "strategy": "S6_TrendPull",
        "side": "SHORT",
        "entry_offset_bp": 3.0,
        "size_mult": 1.2,
        "notional_mult": 1.5,
        "requested_notional_usdc": 50.0,
        "reason": "incumbent_accept",
        "regime": "TREND_UP",
        "missing_features": (),
        "risk_tags": ("incumbent",),
        "metrics": {"tp_bp": 6.0, "sl_bp": 15.0, "ttl_s": 60, "hold_s": 720},
        "policy_tag": "incumbent_policy",
        "shadow_lane": None,
    }
    values.update(overrides)
    return CodexV1Decision(**values)


def _qualified_evidence(**overrides: object) -> WeakStateShadowEvidence:
    values: dict[str, object] = {
        "evaluable": 8,
        "tp_first": 6,
        "cost_adjusted_ev_per_opportunity": 0.000001,
        "data_complete": True,
        "ambiguous": 0,
        "incomplete": 0,
    }
    values.update(overrides)
    return WeakStateShadowEvidence(**values)


def test_dataclasses_are_immutable_and_decision_gate_is_read_only() -> None:
    config = LaneAdaptiveConfig()
    evidence = _qualified_evidence()
    risk = LaneAdaptiveRiskInput()
    decision = _select("STUP-S", "mixed", evidence=evidence)

    with pytest.raises(FrozenInstanceError):
        config.weak_min_evaluable = 9  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        evidence.evaluable = 9  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        risk.global_halted = True  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        decision.risk_scale = 1.0  # type: ignore[misc]
    with pytest.raises(TypeError):
        decision.evidence_gate["qualified"] = False  # type: ignore[index]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"weak_min_evaluable": 7},
        {"weak_min_tp_first": 5},
        {"weak_min_evaluable": 8, "weak_min_tp_first": 9},
        {"weak_min_cost_adjusted_ev_per_opportunity": -0.01},
        {"weak_max_ambiguous": 1},
        {"weak_max_incomplete": 1},
        {"control_risk_scale": 0.99},
        {"probation_risk_scale": 0.49},
        {"control_max_notional_usdc": 50.01},
        {"probation_max_notional_usdc": 25.01},
        {"lane_loss_streak_limit": 0},
        {"lane_loss_streak_limit": 3},
        {"lane_loss_limit_usdc": 0.0},
        {"lane_loss_limit_usdc": 0.120001},
        {"cohort_loss_limit_usdc": 0.0},
        {"cohort_loss_limit_usdc": 0.300001},
        {"cohort_loss_limit_usdc": math.inf},
    ],
)
def test_config_rejects_open_or_nonfinite_thresholds(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        LaneAdaptiveConfig(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"evaluable": -1},
        {"tp_first": -1},
        {"evaluable": 2, "tp_first": 3},
        {"ambiguous": -1},
        {"incomplete": -1},
        {"cost_adjusted_ev_per_opportunity": math.nan},
        {"data_complete": 1},
    ],
)
def test_weak_evidence_validates_counts_completeness_and_finite_ev(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        WeakStateShadowEvidence(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"consecutive_complete_losses": -1},
        {"lane_net_pnl_usdc": math.nan},
        {"cohort_net_pnl_usdc": math.inf},
        {"integrity_safe": 1},
    ],
)
def test_risk_input_validates_finite_values_and_nonnegative_count(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        LaneAdaptiveRiskInput(**kwargs)


@pytest.mark.parametrize("state", ["fast_reclaim", "RP1:mixed", "RP1:unchanged", None])
def test_rp1_accepted_is_control_with_zero_bp_shadow_label(
    state: str | None,
) -> None:
    decision = _select("rp1", state)

    assert decision.action_mode is AdaptiveActionMode.CONTROL
    assert decision.risk_scale == 1.0
    assert decision.max_notional_usdc == 50.0
    assert decision.permits_order is True
    assert decision.matrix_rule_id == "v1460.rp1.control"
    assert decision.shadow_candidates == ("rp1_entry_0bp_first_touch",)


@pytest.mark.parametrize("state", sorted(S1P_PULLBACK_STATES))
def test_s1p_pullback_states_are_control_with_tick_shadow(state: str) -> None:
    decision = _select("S1P-L", state)

    assert decision.action_mode is AdaptiveActionMode.CONTROL
    assert decision.risk_scale == 1.0
    assert decision.shadow_candidates == ("s1p_entry_plus_minus_1_tick",)


@pytest.mark.parametrize("state", sorted(STUP_CLEAN_STATES))
def test_stup_clean_states_are_control_with_time_lock_shadow(state: str) -> None:
    decision = _select("STUP-S", f"STUP-S:{state}")

    assert decision.action_mode is AdaptiveActionMode.CONTROL
    assert decision.risk_scale == 1.0
    assert decision.shadow_candidates == ("stup_clean_time_lock_4_5bp",)


@pytest.mark.parametrize("state", sorted(CNL_CONTROL_STATES))
def test_cnl_reclaim_states_are_control(state: str) -> None:
    decision = _select("CNL_WPR_L", state)

    assert decision.action_mode is AdaptiveActionMode.CONTROL
    assert decision.permits_order is True
    assert decision.matrix_rule_id == "v1460.cnl_reclaim.control"


@pytest.mark.parametrize(
    "state", ["deep_discount_stable", "falling_discount_trap", "ambiguous"]
)
def test_cnl_deep_or_ambiguous_states_are_shadow_blocked(state: str) -> None:
    decision = _select("CNL-WPR-L", state)

    assert decision.action_mode is AdaptiveActionMode.SHADOW_BLOCK
    assert decision.permits_order is False
    assert decision.risk_scale == 0.0
    assert decision.matrix_rule_id == "v1460.cnl_risk.shadow_block"
    assert decision.shadow_candidates == ("cnl_blocked_state_outcome",)


@pytest.mark.parametrize("state", sorted(GLOBAL_SHADOW_BLOCK_STATES))
def test_global_risk_states_are_hard_shadow_blocks_not_halts(state: str) -> None:
    decision = _select("RP1", state)

    assert decision.action_mode is AdaptiveActionMode.SHADOW_BLOCK
    assert decision.action_mode is not AdaptiveActionMode.HALT
    assert decision.permits_order is False
    assert decision.matrix_rule_id == "v1460.state.global_shadow_block"


@pytest.mark.parametrize("state", sorted(STUP_WEAK_STATES))
def test_stup_weak_states_start_shadow_blocked(state: str) -> None:
    decision = _select("STUP-S", state)

    assert decision.action_mode is AdaptiveActionMode.SHADOW_BLOCK
    assert decision.evidence_gate["qualified"] is False
    assert decision.shadow_candidates == ("stup_weak_state_first_touch_outcome",)


@pytest.mark.parametrize(
    "evidence,failed_gate",
    [
        (_qualified_evidence(evaluable=7, tp_first=6), "evaluable_pass"),
        (_qualified_evidence(tp_first=5), "tp_first_pass"),
        (
            _qualified_evidence(cost_adjusted_ev_per_opportunity=0.0),
            "ev_pass",
        ),
        (_qualified_evidence(data_complete=False), "data_complete"),
        (_qualified_evidence(ambiguous=1), "ambiguous_pass"),
        (_qualified_evidence(incomplete=1), "incomplete_pass"),
    ],
)
def test_weak_evidence_gate_boundaries_are_closed(
    evidence: WeakStateShadowEvidence,
    failed_gate: str,
) -> None:
    decision = _select("STUP-S", "near_vwap_flat", evidence=evidence)

    assert decision.action_mode is AdaptiveActionMode.SHADOW_BLOCK
    assert decision.evidence_gate[failed_gate] is False
    assert decision.evidence_gate["qualified"] is False


def test_weak_evidence_exact_count_boundary_and_positive_ev_enters_half_probation() -> None:
    decision = _select(
        "STUP-S",
        "no_momentum_edge",
        evidence=_qualified_evidence(),
    )

    assert decision.action_mode is AdaptiveActionMode.PROBATION_0_5
    assert decision.risk_scale == 0.5
    assert decision.max_notional_usdc == 25.0
    assert decision.permits_order is True
    assert decision.evidence_gate["qualified"] is True
    assert decision.matrix_rule_id == "v1460.stup_weak.probation_0_5"


@pytest.mark.parametrize("state", [None, "", "unknown", "future_unmapped_state"])
def test_unknown_or_missing_state_uses_generic_half_risk_probation(
    state: str | None,
) -> None:
    decision = _select("STUP-S", state)

    assert decision.action_mode is AdaptiveActionMode.PROBATION_0_5
    assert decision.risk_scale == 0.5
    assert decision.max_notional_usdc == 25.0
    assert decision.permits_order is True
    assert decision.matrix_rule_id == "v1460.state.unknown_probation_0_5"
    assert decision.shadow_candidates == ()


def test_other_accepted_lane_uses_control_fallback() -> None:
    decision = _select("FUTURE-LANE", "clean_extension")

    assert decision.action_mode is AdaptiveActionMode.CONTROL
    assert decision.permits_order is True
    assert decision.matrix_rule_id == "v1460.other.incumbent_fallback"


@pytest.mark.parametrize(
    "risk,rule_id",
    [
        (LaneAdaptiveRiskInput(integrity_safe=False), "v1460.integrity.halt"),
        (LaneAdaptiveRiskInput(global_halted=True), "v1460.risk.global_halt"),
        (
            LaneAdaptiveRiskInput(cohort_net_pnl_usdc=-0.30),
            "v1460.risk.global_halt",
        ),
    ],
)
def test_integrity_or_global_risk_halts(risk: LaneAdaptiveRiskInput, rule_id: str) -> None:
    decision = _select(risk=risk)

    assert decision.action_mode is AdaptiveActionMode.HALT
    assert decision.permits_order is False
    assert decision.risk_scale == 0.0
    assert decision.matrix_rule_id == rule_id


@pytest.mark.parametrize(
    "risk",
    [
        LaneAdaptiveRiskInput(lane_state_isolated=True),
        LaneAdaptiveRiskInput(consecutive_complete_losses=2),
        LaneAdaptiveRiskInput(lane_net_pnl_usdc=-0.12),
    ],
)
def test_lane_isolation_and_closed_loss_boundaries_shadow_block(
    risk: LaneAdaptiveRiskInput,
) -> None:
    decision = _select(risk=risk)

    assert decision.action_mode is AdaptiveActionMode.SHADOW_BLOCK
    assert decision.permits_order is False
    assert decision.matrix_rule_id == "v1460.risk.lane_state_isolated"


def test_risk_values_inside_boundaries_do_not_isolate() -> None:
    decision = _select(
        risk=LaneAdaptiveRiskInput(
            consecutive_complete_losses=1,
            lane_net_pnl_usdc=-0.119999,
            cohort_net_pnl_usdc=-0.299999,
        )
    )

    assert decision.action_mode is AdaptiveActionMode.CONTROL
    assert decision.permits_order is True


@pytest.mark.parametrize(
    "lane,state,evidence",
    [
        ("RP1", "fast_reclaim", None),
        ("STUP-S", "mixed", _qualified_evidence()),
        ("CNL-WPR-L", "discount_mixed", None),
        ("OTHER", "clean_extension", None),
    ],
)
def test_rejected_incumbent_is_never_permitted(
    lane: str,
    state: str,
    evidence: WeakStateShadowEvidence | None,
) -> None:
    decision = _select(lane, state, accepted=False, evidence=evidence)

    assert decision.incumbent_accepted is False
    assert decision.action_mode is AdaptiveActionMode.SHADOW_BLOCK
    assert decision.permits_order is False
    assert decision.risk_scale == 0.0
    assert decision.matrix_rule_id == "v1460.incumbent.rejected"


def test_candidate_only_annotates_copy_without_mutating_trading_fields() -> None:
    original = _codex_decision()
    original_metrics = dict(original.metrics or {})
    adaptive = _select("STUP-S", "mixed", evidence=_qualified_evidence())

    annotated = apply_lane_adaptive_decision(
        original,
        adaptive,
        mode="candidate-only",
    )

    assert annotated is not original
    assert original.metrics == original_metrics
    assert "v1460_lane_adaptive" not in (original.metrics or {})
    for field in (
        "accepted",
        "entry_offset_bp",
        "size_mult",
        "notional_mult",
        "requested_notional_usdc",
        "reason",
        "regime",
        "risk_tags",
        "policy_tag",
    ):
        assert getattr(annotated, field) == getattr(original, field)
    assert annotated.metrics is not None
    telemetry = annotated.metrics["v1460_lane_adaptive"]
    assert telemetry["action_mode"] == "PROBATION_0_5"
    assert telemetry["incumbent_accepted"] is True
    assert telemetry["matrix_rule_id"] == adaptive.matrix_rule_id
    assert telemetry["policy_hash"] == adaptive.policy_hash
    assert telemetry["mode"] == "candidate-only"


def test_enforcement_half_sizes_and_preserves_non_risk_strategy_fields() -> None:
    original = _codex_decision()
    adaptive = _select("STUP-S", "mixed", evidence=_qualified_evidence())

    enforced = apply_lane_adaptive_decision(original, adaptive, mode="enforcement")

    assert enforced.accepted is True
    assert enforced.size_mult == pytest.approx(0.6)
    assert enforced.notional_mult == pytest.approx(0.75)
    assert enforced.requested_notional_usdc == pytest.approx(25.0)
    assert enforced.metrics["applied_notional_cap_usdc"] == pytest.approx(25.0)
    assert enforced.entry_offset_bp == original.entry_offset_bp
    assert enforced.reason == original.reason
    assert enforced.regime == original.regime
    assert enforced.risk_tags == original.risk_tags
    assert enforced.policy_tag == original.policy_tag
    assert enforced.metrics is not None
    for key in ("tp_bp", "sl_bp", "ttl_s", "hold_s"):
        assert enforced.metrics[key] == original.metrics[key]  # type: ignore[index]


def test_enforcement_control_never_increases_and_applies_control_cap() -> None:
    original = _codex_decision(requested_notional_usdc=75.0)
    adaptive = _select("RP1", "fast_reclaim")

    enforced = apply_lane_adaptive_decision(original, adaptive, mode="enforcement")

    assert enforced.accepted is True
    assert enforced.size_mult == original.size_mult
    assert enforced.notional_mult == original.notional_mult
    assert enforced.requested_notional_usdc == 50.0
    assert enforced.metrics["applied_notional_cap_usdc"] == pytest.approx(50.0)


def test_enforcement_preserves_stricter_existing_notional_cap() -> None:
    original = _codex_decision(
        metrics={
            "tp_bp": 6.0,
            "sl_bp": 15.0,
            "ttl_s": 60,
            "hold_s": 720,
            "applied_notional_cap_usdc": 18.0,
        }
    )
    adaptive = _select("STUP-S", "mixed", evidence=_qualified_evidence())

    enforced = apply_lane_adaptive_decision(original, adaptive, mode="enforcement")

    assert enforced.metrics["applied_notional_cap_usdc"] == pytest.approx(18.0)


@pytest.mark.parametrize(
    "adaptive",
    [
        _select("STUP-S", "weak_chop"),
        _select("RP1", "shock"),
        _select(risk=LaneAdaptiveRiskInput(integrity_safe=False)),
    ],
)
def test_enforcement_block_or_halt_zeroes_only_admission_and_sizing(
    adaptive: LaneAdaptiveDecision,
) -> None:
    original = _codex_decision()

    enforced = apply_lane_adaptive_decision(original, adaptive, mode="enforcement")

    assert enforced.accepted is False
    assert enforced.size_mult == 0.0
    assert enforced.notional_mult == 0.0
    assert enforced.requested_notional_usdc == 0.0
    assert enforced.entry_offset_bp == original.entry_offset_bp
    assert enforced.reason == original.reason
    assert enforced.regime == original.regime
    assert enforced.policy_tag == original.policy_tag
    assert enforced.metrics is not None
    for key in ("tp_bp", "sl_bp", "ttl_s", "hold_s"):
        assert enforced.metrics[key] == original.metrics[key]  # type: ignore[index]


def test_enforcement_cannot_reenable_rejected_codex_decision() -> None:
    original = _codex_decision(accepted=False, requested_notional_usdc=0.0)
    adaptive = _select("RP1", "fast_reclaim", accepted=True)

    enforced = apply_lane_adaptive_decision(original, adaptive, mode="enforcement")

    assert enforced.accepted is False
    assert enforced.requested_notional_usdc == 0.0
    assert enforced.size_mult == 0.0
    assert enforced.notional_mult == 0.0


def test_apply_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError):
        apply_lane_adaptive_decision(
            _codex_decision(),
            _select(),
            mode="live",  # type: ignore[arg-type]
        )


def test_policy_hash_is_stable_and_exposed_on_config_and_decisions() -> None:
    first = LaneAdaptiveConfig()
    second = LaneAdaptiveConfig()
    decision = _select(config=first)

    assert policy_hash(first) == policy_hash(second)
    assert first.policy_hash == policy_hash(first)
    assert decision.policy_hash == first.policy_hash
    assert len(first.policy_hash) == 64


@pytest.mark.parametrize(
    "changes",
    [
        {"version": "v1.4.60-test"},
        {"policy_name": "codex-v1.4.60-lane-adaptive-test"},
        {"weak_min_evaluable": 9},
        {"weak_min_tp_first": 7},
        {"weak_min_cost_adjusted_ev_per_opportunity": 0.001},
        {"control_max_notional_usdc": 49.0},
        {"probation_max_notional_usdc": 24.0},
        {"lane_loss_streak_limit": 1},
        {"lane_loss_limit_usdc": 0.11},
        {"cohort_loss_limit_usdc": 0.29},
    ],
)
def test_policy_hash_changes_for_every_configurable_identity_or_threshold(
    changes: dict[str, object],
) -> None:
    baseline = LaneAdaptiveConfig()
    changed = replace(baseline, **changes)

    assert policy_hash(changed) != policy_hash(baseline)


def test_policy_decision_rejects_inconsistent_permit_contract() -> None:
    with pytest.raises(ValueError):
        LaneAdaptiveDecision(
            action_mode=AdaptiveActionMode.SHADOW_BLOCK,
            incumbent_accepted=True,
            matrix_rule_id="test.rule",
            risk_scale=0.0,
            max_notional_usdc=0.0,
            evidence_gate={},
            policy_hash=LaneAdaptiveConfig().policy_hash,
            shadow_candidates=(),
            reason="test",
            permits_order=True,
        )
