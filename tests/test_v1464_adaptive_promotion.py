from __future__ import annotations

from dataclasses import replace

import pytest

from src.gridbot.mainnet.v1464_adaptive_promotion import (
    AdaptivePromotionConfig,
    PromotionEvidenceSnapshot,
    PromotionLeaseSnapshot,
    PromotionRegimeInput,
    PromotionRiskInput,
    PromotionState,
    canonicalize_stable_profile,
    promotion_cohort_key,
    select_adaptive_promotion_decision,
    stable_profile_hash,
)


NOW = 200_000_000
PROFILE_HASH = "profile-" + ("a" * 64)


def _ticket(**changes: object) -> dict:
    ticket = {
        "classifier_lane": "W3A",
        "effective_lane": "W3A",
        "classifier_side": "LONG",
        "effective_side": "LONG",
        "strategy": "S6_TrendPull",
        "market_state": "trend_reclaim",
        "policy_tag": "v1464_test_profile",
        "entry_offset_bp": 0.0,
        "requested_notional_usdc": 50.0,
        "runtime_id": "runtime-a",
        "opportunity_id": "opp-a",
        "observed_at_ms": 1_000,
        "action_parameters": {
            "tp1_bp": 8.0,
            "sl_bp": 10.0,
            "full_tp_bp": 16.0,
            "partial_exit_pct": 0.5,
            "ttl_s": 60,
            "max_hold_s": 600,
            "profile_id": "trend-reclaim-v1",
            "profile_anchor": "entry",
            "action_id": "TP8_SL10",
        },
    }
    ticket.update(changes)
    return ticket


def _plan(**changes: object) -> dict:
    plan = {
        "schema": "v1463.frozen-effective-ticket.1",
        "side": "LONG",
        "strategy": "S6_TrendPull",
        "entry_price": 2_000.0,
        "tp1_price": 2_001.6,
        "sl_price": 1_998.0,
        "full_tp_price": 2_003.2,
        "entry_offset_bp": 0.0,
        "tp1_bp": 8.0,
        "sl_bp": 10.0,
        "partial_exit_pct": 0.5,
        "planned_notional_usdc": 50.0,
        "entry_ttl_s": 60,
        "outcome_ttl_s": 600,
        "run_id": "run-a",
        "opportunity_id": "opp-a",
        "observed_at_ms": 1_000,
        "action_parameters": {
            "full_tp_bp": 16.0,
            "profile_id": "trend-reclaim-v1",
            "profile_anchor": "entry",
            "action_id": "TP8_SL10",
        },
    }
    plan.update(changes)
    return plan


def _evidence(**changes: object) -> PromotionEvidenceSnapshot:
    values = {
        "evidence_revision": "r1",
        "snapshot_at_ms": NOW,
        "window_started_at_ms": NOW - 30 * 60 * 1000,
        "window_ended_at_ms": NOW,
        "last_outcome_at_ms": NOW - 60_000,
        "last_outcome": "tp_first",
        "opportunities": 4,
        "evaluable": 4,
        "tp_first": 3,
        "sl_first": 1,
        "fee_net_pnl_usdc": 0.04,
    }
    values.update(changes)
    return PromotionEvidenceSnapshot(**values)


def _regime(**changes: object) -> PromotionRegimeInput:
    values = {
        "supportive": True,
        "confirmed": True,
        "fresh": True,
        "exact_cohort_match": True,
    }
    values.update(changes)
    return PromotionRegimeInput(**values)


def _risk(**changes: object) -> PromotionRiskInput:
    values = {
        "raw_accepted": True,
        "pre_gate_accepted": True,
        "final_incumbent_accepted": True,
        "identity_valid": True,
        "integrity_safe": True,
        "execution_controls_safe": True,
        "database_healthy": True,
    }
    values.update(changes)
    return PromotionRiskInput(**values)


def _lease(
    state: PromotionState,
    *,
    evidence: PromotionEvidenceSnapshot | None = None,
    config: AdaptivePromotionConfig | None = None,
    **changes: object,
) -> PromotionLeaseSnapshot:
    active = config or AdaptivePromotionConfig()
    facts = evidence or _evidence()
    values = {
        "state": state,
        "lease_id": "lease-1",
        "cohort_key": promotion_cohort_key(PROFILE_HASH, active.policy_hash),
        "policy_hash": active.policy_hash,
        "issued_at_ms": NOW - 60_000,
        "expires_at_ms": NOW + 60_000,
        "evidence_revision": facts.evidence_revision,
        "evidence_as_of_ms": facts.snapshot_at_ms,
        "soft_breach_count": 0,
    }
    values.update(changes)
    return PromotionLeaseSnapshot(**values)


def _select(**changes: object):
    values = {
        "profile_hash": PROFILE_HASH,
        "candidate_notional_usdc": 50.0,
        "evidence": _evidence(),
        "regime": _regime(),
        "risk": _risk(),
        "now_ms": NOW,
    }
    values.update(changes)
    return select_adaptive_promotion_decision(**values)


def test_stable_profile_ignores_prices_timestamps_and_runtime_ids() -> None:
    first = stable_profile_hash(_ticket(), _plan())
    second = stable_profile_hash(
        _ticket(
            runtime_id="runtime-b",
            opportunity_id="opp-b",
            observed_at_ms=99_999,
        ),
        _plan(
            entry_price=2_100.0,
            tp1_price=2_101.68,
            sl_price=2_097.9,
            full_tp_price=2_103.36,
            run_id="run-b",
            opportunity_id="opp-b",
            observed_at_ms=99_999,
        ),
    )
    assert first == second
    canonical = canonicalize_stable_profile(_ticket(), _plan())
    rendered = repr(canonical)
    for forbidden in (
        "entry_price",
        "tp1_price",
        "sl_price",
        "full_tp_price",
        "observed_at_ms",
        "opportunity_id",
        "run_id",
    ):
        assert forbidden not in rendered


def test_stable_profile_rounds_float_noise_without_splitting_cohort() -> None:
    noisy = _plan(tp1_bp=7.9999999997, sl_bp=10.0000000001)
    exact = _plan(tp1_bp=8.0, sl_bp=10.0)
    assert stable_profile_hash(_ticket(), noisy) == stable_profile_hash(
        _ticket(), exact
    )


def test_ticket_explicit_geometry_precedes_nested_action_when_plan_omits_it() -> None:
    baseline_plan = _plan()
    baseline_plan.pop("entry_offset_bp")
    baseline_plan["action_parameters"] = {
        **baseline_plan["action_parameters"],
        "entry_bp": 0.0,
    }

    assert stable_profile_hash(
        _ticket(entry_offset_bp=2.0), baseline_plan
    ) != stable_profile_hash(_ticket(entry_offset_bp=0.0), baseline_plan)


@pytest.mark.parametrize(
    "ticket,plan",
    [
        (_ticket(), _plan(tp1_bp=9.0)),
        (_ticket(), _plan(sl_bp=11.0)),
        (_ticket(), _plan(partial_exit_pct=0.7)),
        (_ticket(), _plan(entry_ttl_s=90)),
        (_ticket(), _plan(outcome_ttl_s=900)),
        (
            _ticket(),
            _plan(
                action_parameters={
                    "full_tp_bp": 16.0,
                    "profile_id": "trend-reclaim-v2",
                    "profile_anchor": "entry",
                    "action_id": "TP8_SL10",
                }
            ),
        ),
    ],
)
def test_geometry_or_stable_profile_change_splits_cohort(
    ticket: dict,
    plan: dict,
) -> None:
    assert stable_profile_hash(ticket, plan) != stable_profile_hash(
        _ticket(), _plan()
    )


def test_dynamic_sizing_and_selector_labels_do_not_split_cohort() -> None:
    dynamic = _plan(
        planned_notional_usdc=25.0,
        policy_tag="temporary_selector_label",
        action_parameters={
            **_plan()["action_parameters"],
            "live_action": "PROBE",
            "v1455_action": "SHADOW",
            "v1441_research_selector_action": "WATCH",
            "matrix_rule_id": "runtime-rule",
        },
    )
    assert stable_profile_hash(_ticket(), dynamic) == stable_profile_hash(
        _ticket(), _plan()
    )


def test_policy_hash_and_cohort_key_are_deterministic_and_config_bound() -> None:
    base = AdaptivePromotionConfig()
    changed = replace(base, lease_ttl_seconds=600)
    assert base.policy_hash == AdaptivePromotionConfig().policy_hash
    assert base.policy_hash != changed.policy_hash
    assert promotion_cohort_key(PROFILE_HASH, base.policy_hash) == promotion_cohort_key(
        PROFILE_HASH, base.policy_hash
    )
    assert promotion_cohort_key(
        PROFILE_HASH, base.policy_hash
    ) != promotion_cohort_key(PROFILE_HASH, changed.policy_hash)


def test_explicit_runtime_cohort_key_matches_and_retains_existing_lease() -> None:
    runtime_key = "runtime-canonical-cohort-key"
    lease = _lease(PromotionState.LIVE, cohort_key=runtime_key)

    decision = _select(cohort_key=runtime_key, existing_lease=lease)

    assert decision.state is PromotionState.LIVE
    assert decision.cohort_key == runtime_key
    assert decision.reason == "lease_retained"
    assert decision.revoke_existing_lease is False


def test_runner_reprice_and_dca_cannot_be_enabled_by_policy() -> None:
    with pytest.raises(ValueError, match="runner"):
        AdaptivePromotionConfig(runner_enabled=True)
    with pytest.raises(ValueError, match="runner"):
        AdaptivePromotionConfig(one_step_reprice_enabled=True)
    with pytest.raises(ValueError, match="runner"):
        AdaptivePromotionConfig(dca_enabled=True)


def test_recent_complete_four_of_three_enters_25u_probation() -> None:
    decision = _select()
    assert decision.state is PromotionState.PROBATION
    assert decision.permits_order is True
    assert decision.max_notional_usdc == 25.0
    assert decision.applied_notional_usdc == 25.0
    assert decision.issue_new_lease is True
    assert decision.lease_expires_at_ms == NOW + 15 * 60 * 1000
    assert decision.telemetry["execution_controls"] == {
        "runner_enabled": False,
        "one_step_reprice_enabled": False,
        "dca_enabled": False,
    }


@pytest.mark.parametrize(
    "evidence",
    [
        _evidence(evaluable=3, tp_first=3, sl_first=0),
        _evidence(tp_first=2, sl_first=2),
        _evidence(fee_net_pnl_usdc=0.0),
        _evidence(last_outcome="sl_first"),
    ],
)
def test_shadow_to_probation_requires_strict_four_of_three_positive_gate(
    evidence: PromotionEvidenceSnapshot,
) -> None:
    decision = _select(evidence=evidence)
    assert decision.state is PromotionState.SHADOW
    assert decision.permits_order is False


def test_six_of_four_plus_three_paid_two_wins_enters_50u_live() -> None:
    evidence = _evidence(
        evidence_revision="r2",
        opportunities=6,
        evaluable=6,
        tp_first=4,
        sl_first=2,
        fee_net_pnl_usdc=0.08,
        paid_complete=3,
        paid_wins=2,
        paid_net_pnl_usdc=0.03,
    )
    lease = _lease(PromotionState.PROBATION, evidence=_evidence())
    decision = _select(evidence=evidence, existing_lease=lease)
    assert decision.state is PromotionState.LIVE
    assert decision.permits_order is True
    assert decision.max_notional_usdc == 50.0
    assert decision.applied_notional_usdc == 50.0
    assert decision.reason == "paid_probation_passed"


def test_live_gate_does_not_skip_probation_when_no_existing_lease() -> None:
    evidence = _evidence(
        opportunities=6,
        evaluable=6,
        tp_first=4,
        sl_first=2,
        paid_complete=3,
        paid_wins=2,
        paid_net_pnl_usdc=0.03,
    )
    assert _select(evidence=evidence).state is PromotionState.PROBATION


def test_expired_lease_requires_fresh_evidence_to_renew() -> None:
    current = _evidence()
    expired = _lease(
        PromotionState.PROBATION,
        evidence=current,
        expires_at_ms=NOW,
        evidence_as_of_ms=NOW - 1,
    )
    stale_revision = _select(evidence=current, existing_lease=expired)
    assert stale_revision.state is PromotionState.SHADOW
    assert stale_revision.reason == "lease_expired_without_fresh_evidence"

    fresh = replace(
        current,
        evidence_revision="r2",
        snapshot_at_ms=NOW,
    )
    renewed = _select(evidence=fresh, existing_lease=expired)
    assert renewed.state is PromotionState.PROBATION
    assert renewed.issue_new_lease is True
    assert renewed.revoke_existing_lease is True
    assert renewed.lease_expires_at_ms == NOW + 15 * 60 * 1000


def test_soft_retain_floor_demotes_only_after_two_consecutive_breaches() -> None:
    weak = _evidence(
        evaluable=4,
        tp_first=2,
        sl_first=2,
        fee_net_pnl_usdc=0.01,
    )
    lease = _lease(PromotionState.LIVE)
    first = _select(evidence=weak, existing_lease=lease)
    assert first.state is PromotionState.LIVE
    assert first.permits_order is True
    assert first.soft_breach_count == 1
    assert first.reason == "soft_retain_breach_pending"

    second_lease = replace(lease, soft_breach_count=first.soft_breach_count)
    second = _select(evidence=weak, existing_lease=second_lease)
    assert second.state is PromotionState.SHADOW
    assert second.permits_order is False
    assert second.revoke_existing_lease is True
    assert second.reason == "soft_retain_breach_limit"


@pytest.mark.parametrize(
    ("risk", "reason", "state"),
    [
        (_risk(raw_accepted=False), "raw_rejected", PromotionState.SHADOW),
        (_risk(pre_gate_accepted=False), "pre_gate_rejected", PromotionState.SHADOW),
        (
            _risk(final_incumbent_accepted=False),
            "final_incumbent_rejected",
            PromotionState.SHADOW,
        ),
        (
            _risk(reject_lineage=("legacy_reject",)),
            "reject_lineage_present",
            PromotionState.SHADOW,
        ),
        (_risk(identity_valid=False), "identity_invalid", PromotionState.SHADOW),
        (
            _risk(execution_controls_safe=False),
            "execution_controls_unsafe",
            PromotionState.HALTED,
        ),
        (
            _risk(database_healthy=False),
            "database_unhealthy",
            PromotionState.HALTED,
        ),
    ],
)
def test_hard_blockers_immediately_remove_order_authority(
    risk: PromotionRiskInput,
    reason: str,
    state: PromotionState,
) -> None:
    decision = _select(
        risk=risk,
        existing_lease=_lease(PromotionState.LIVE),
    )
    assert decision.state is state
    assert decision.permits_order is False
    assert decision.max_notional_usdc == 0.0
    assert decision.reason == reason
    assert decision.revoke_existing_lease is True


@pytest.mark.parametrize(
    "evidence",
    [
        _evidence(data_complete=False),
        _evidence(identity_conflicts=1),
        _evidence(data_conflicts=1),
        _evidence(incomplete=1),
        _evidence(ambiguous=1),
        _evidence(dropped=1),
        _evidence(overdue=1),
    ],
)
def test_data_quality_blockers_are_immediate(
    evidence: PromotionEvidenceSnapshot,
) -> None:
    decision = _select(
        evidence=evidence,
        existing_lease=_lease(PromotionState.LIVE),
    )
    assert decision.state is PromotionState.SHADOW
    assert decision.permits_order is False
    assert decision.revoke_existing_lease is True


@pytest.mark.parametrize(
    "regime",
    [
        _regime(supportive=False),
        _regime(confirmed=False),
        _regime(fresh=False),
        _regime(exact_cohort_match=False),
    ],
)
def test_regime_must_be_supportive_confirmed_fresh_and_exact(
    regime: PromotionRegimeInput,
) -> None:
    decision = _select(
        regime=regime,
        existing_lease=_lease(PromotionState.LIVE),
    )
    assert decision.state is PromotionState.SHADOW
    assert decision.permits_order is False
    assert decision.revoke_existing_lease is True


def test_evidence_window_and_freshness_are_bounded_to_90_minutes() -> None:
    old_window = _evidence(
        window_started_at_ms=NOW - 90 * 60 * 1000 - 1,
    )
    stale = _evidence(
        last_outcome_at_ms=NOW - 90 * 60 * 1000 - 1,
    )
    assert _select(evidence=old_window).reason == "evidence_window_not_recent"
    assert _select(evidence=stale).reason == "evidence_stale"


@pytest.mark.parametrize(
    "risk",
    [
        _risk(consecutive_paid_losses=2),
        _risk(lane_net_pnl_usdc=-0.12),
        _risk(cohort_net_pnl_usdc=-0.30),
    ],
)
def test_paid_risk_boundaries_immediately_quarantine(
    risk: PromotionRiskInput,
) -> None:
    decision = _select(
        risk=risk,
        existing_lease=_lease(PromotionState.LIVE),
    )
    assert decision.state is PromotionState.COOLDOWN
    assert decision.permits_order is False
    assert decision.reason == "paid_risk_quarantine"
    assert decision.revoke_existing_lease is True


@pytest.mark.parametrize(
    ("candidate", "expected_state", "expected_applied"),
    [
        (10.0, PromotionState.PROBATION, 10.0),
        (25.0, PromotionState.PROBATION, 25.0),
        (50.0, PromotionState.PROBATION, 25.0),
        (100.0, PromotionState.PROBATION, 25.0),
    ],
)
def test_probation_notional_cap_never_amplifies_candidate(
    candidate: float,
    expected_state: PromotionState,
    expected_applied: float,
) -> None:
    decision = _select(candidate_notional_usdc=candidate)
    assert decision.state is expected_state
    assert decision.applied_notional_usdc == expected_applied
    assert decision.applied_notional_usdc <= candidate


def test_live_notional_cap_never_amplifies_or_exceeds_50u() -> None:
    evidence = _evidence(
        evidence_revision="r2",
        opportunities=6,
        evaluable=6,
        tp_first=4,
        sl_first=2,
        paid_complete=3,
        paid_wins=2,
        paid_net_pnl_usdc=0.03,
    )
    lease = _lease(PromotionState.PROBATION)
    small = _select(
        candidate_notional_usdc=20.0,
        evidence=evidence,
        existing_lease=lease,
    )
    large = _select(
        candidate_notional_usdc=100.0,
        evidence=evidence,
        existing_lease=lease,
    )
    assert small.state is PromotionState.LIVE
    assert small.applied_notional_usdc == 20.0
    assert large.state is PromotionState.LIVE
    assert large.applied_notional_usdc == 50.0


def test_nonpositive_candidate_has_no_order_authority() -> None:
    decision = _select(candidate_notional_usdc=0.0)
    assert decision.state is PromotionState.SHADOW
    assert decision.permits_order is False
    assert decision.applied_notional_usdc == 0.0
