from dataclasses import replace

import pytest

from src.gridbot.mainnet.v1469_arm_arbiter import (
    ArmCandidate,
    ArmCooldown,
    ArmEvidence,
    ArmIdentity,
    ArbiterRequest,
    CurrentLease,
    EvidenceOutcome,
    LeaseAction,
    LeasePhase,
    RegimeSnapshot,
    evidence_revision,
    evaluate_rolling_arbiter,
    normalize_evidence_outcome,
)


NOW = 20_000_000
MINUTE = 60_000


def _identity(
    arm_key: str,
    *,
    side: str = "LONG",
    regime: str = "RANGE",
) -> ArmIdentity:
    return ArmIdentity(
        arm_key=arm_key,
        lane_code=arm_key.upper(),
        side=side,
        strategy="S1_BB_RSI",
        regime=regime,
        execution_profile_id=f"{arm_key}-profile",
        execution_profile_hash=f"{arm_key}-hash",
    )


def _evidence(
    identity: ArmIdentity,
    opportunity_id: str,
    *,
    age_ms: int,
    outcome: EvidenceOutcome,
    reward_net_bp: float,
    paired: bool = True,
    hard_loss: bool = False,
) -> ArmEvidence:
    observed_at_ms = NOW - age_ms
    return ArmEvidence(
        arm_key=identity.arm_key,
        opportunity_id=opportunity_id,
        observed_at_ms=observed_at_ms,
        terminal_at_ms=observed_at_ms + 1_000,
        deadline_at_ms=observed_at_ms + MINUTE,
        outcome=outcome,
        reward_net_bp=reward_net_bp,
        regime=identity.regime,
        paired=paired,
        hard_loss=hard_loss,
    )


def _candidate(
    arm_key: str,
    *,
    tp_reward_bp: float = 4.0,
    side: str = "LONG",
    regime: str = "RANGE",
    authority_ages_ms: tuple[int, ...] = (
        1 * MINUTE,
        2 * MINUTE,
        3 * MINUTE,
        4 * MINUTE,
    ),
) -> ArmCandidate:
    identity = _identity(arm_key, side=side, regime=regime)
    evidence = (
        _evidence(
            identity,
            "opp-1",
            age_ms=authority_ages_ms[0],
            outcome=EvidenceOutcome.TP_FIRST,
            reward_net_bp=tp_reward_bp,
        ),
        _evidence(
            identity,
            "opp-2",
            age_ms=authority_ages_ms[1],
            outcome=EvidenceOutcome.TP_FIRST,
            reward_net_bp=tp_reward_bp,
        ),
        _evidence(
            identity,
            "opp-3",
            age_ms=authority_ages_ms[2],
            outcome=EvidenceOutcome.TP_FIRST,
            reward_net_bp=tp_reward_bp,
        ),
        _evidence(
            identity,
            "opp-4",
            age_ms=authority_ages_ms[3],
            outcome=EvidenceOutcome.NO_FILL,
            reward_net_bp=99.0,
        ),
        _evidence(
            identity,
            "opp-5",
            age_ms=60 * MINUTE,
            outcome=EvidenceOutcome.MAX_HOLD,
            reward_net_bp=0.0,
        ),
        _evidence(
            identity,
            "opp-6",
            age_ms=180 * MINUTE,
            outcome=EvidenceOutcome.MAX_HOLD,
            reward_net_bp=0.0,
        ),
    )
    provisional = ArmCandidate(
        identity=identity,
        evidence=evidence,
        source_evidence_revision="pending",
    )
    return replace(
        provisional,
        source_evidence_revision=evidence_revision(provisional),
    )


def _regime(
    regime: str = "RANGE",
    *,
    age_ms: int = 20_000,
    confirmations: tuple[int, ...] | None = None,
    valid_sides: frozenset[str] = frozenset({"LONG"}),
) -> RegimeSnapshot:
    observed = NOW - age_ms
    return RegimeSnapshot(
        regime=regime,
        observed_at_ms=observed,
        confirmation_at_ms=(
            confirmations
            if confirmations is not None
            else (observed - 20_000, observed)
        ),
        direction_valid_sides=valid_sides,
    )


def _submit(
    regime: str = "RANGE",
    *,
    age_ms: int = 5_000,
    valid_sides: frozenset[str] = frozenset({"LONG"}),
) -> RegimeSnapshot:
    return RegimeSnapshot(
        regime=regime,
        observed_at_ms=NOW - age_ms,
        direction_valid_sides=valid_sides,
    )


def _request(
    *candidates: ArmCandidate,
    regime_snapshot: RegimeSnapshot | None = None,
    submit_snapshot: RegimeSnapshot | None = None,
    incumbent_arm_key: str | None = None,
    current_lease: CurrentLease | None = None,
    cooldowns: tuple[ArmCooldown, ...] = (),
) -> ArbiterRequest:
    return ArbiterRequest(
        as_of_ms=NOW,
        regime_snapshot=regime_snapshot or _regime(),
        submit_snapshot=submit_snapshot or _submit(),
        candidates=tuple(candidates),
        incumbent_arm_key=incumbent_arm_key,
        current_lease=current_lease,
        cooldowns=cooldowns,
    )


def _evaluation(decision, arm_key: str):
    return next(
        item for item in decision.evaluations
        if item.identity.arm_key == arm_key
    )


def test_window_boundaries_are_inclusive_and_older_authority_row_is_excluded():
    candidate = _candidate(
        "arm-a",
        authority_ages_ms=(
            1 * MINUTE,
            2 * MINUTE,
            3 * MINUTE,
            45 * MINUTE,
        ),
    )

    boundary = evaluate_rolling_arbiter(_request(candidate))

    assert boundary.winner == candidate.identity
    metrics = boundary.evaluations[0].metrics
    assert metrics.authority_paired_evaluable == 4
    assert metrics.guard_evaluable == 6

    just_outside = replace(
        candidate.evidence[3],
        observed_at_ms=NOW - 45 * MINUTE - 1,
        terminal_at_ms=NOW - 45 * MINUTE + 999,
        deadline_at_ms=NOW - 44 * MINUTE,
    )
    outside_candidate = replace(
        candidate,
        evidence=(
            *candidate.evidence[:3],
            just_outside,
            *candidate.evidence[4:],
        ),
    )
    outside = evaluate_rolling_arbiter(_request(outside_candidate))

    assert outside.winner is None
    assert "authority_paired_evaluable=3/4" in outside.evaluations[0].blockers


@pytest.mark.parametrize(
    ("bad_evidence", "blocker_prefix"),
    [
        (
            ArmEvidence(
                arm_key="arm-a",
                opportunity_id="future",
                observed_at_ms=NOW + 1,
                terminal_at_ms=NOW + 2,
                deadline_at_ms=NOW + MINUTE,
                outcome=EvidenceOutcome.TP_FIRST,
                reward_net_bp=4.0,
                regime="RANGE",
            ),
            "future_evidence:",
        ),
        (
            ArmEvidence(
                arm_key="arm-a",
                opportunity_id="late",
                observed_at_ms=NOW - 2 * MINUTE,
                terminal_at_ms=NOW - MINUTE,
                deadline_at_ms=NOW - MINUTE - 1,
                outcome=EvidenceOutcome.TP_FIRST,
                reward_net_bp=4.0,
                regime="RANGE",
            ),
            "late_evidence:",
        ),
    ],
)
def test_future_and_late_evidence_fail_closed(bad_evidence, blocker_prefix):
    candidate = _candidate("arm-a")
    candidate = replace(
        candidate,
        evidence=(*candidate.evidence, bad_evidence),
    )

    decision = evaluate_rolling_arbiter(_request(candidate))

    assert decision.winner is None
    assert any(
        blocker.startswith(blocker_prefix)
        for blocker in decision.evaluations[0].blockers
    )


def test_no_fill_is_zero_in_authority_denominator():
    decision = evaluate_rolling_arbiter(_request(_candidate("arm-a")))

    metrics = decision.evaluations[0].metrics
    assert metrics.authority_paired_evaluable == 4
    assert metrics.authority_tp_first == 3
    assert metrics.authority_no_fill == 1
    assert metrics.authority_fee_net_ev_bp == pytest.approx(3.0)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("tp1_first", EvidenceOutcome.TP_FIRST),
        ("tp_first", EvidenceOutcome.TP_FIRST),
        ("TP-FIRST", EvidenceOutcome.TP_FIRST),
        ("sl_first", EvidenceOutcome.SL_FIRST),
        ("no_fill", EvidenceOutcome.NO_FILL),
        ("max_hold", EvidenceOutcome.MAX_HOLD),
        ("unknown", None),
    ],
)
def test_external_outcome_strings_map_deterministically(raw, expected):
    assert normalize_evidence_outcome(raw) == expected


def test_repository_outcome_aliases_are_accepted_and_revision_canonicalized():
    candidate = _candidate("arm-a")
    aliases = ("tp1_first", "tp_first", "TP_FIRST", "no_fill")
    aliased = replace(
        candidate,
        evidence=(
            *(
                replace(item, outcome=aliases[index])
                for index, item in enumerate(candidate.evidence[:4])
            ),
            replace(candidate.evidence[4], outcome="max_hold"),
            replace(candidate.evidence[5], outcome="MAX_HOLD"),
        ),
    )

    decision = evaluate_rolling_arbiter(_request(aliased))

    assert decision.winner == candidate.identity
    assert decision.evaluations[0].metrics.authority_tp_first == 3
    assert decision.evaluations[0].metrics.authority_no_fill == 1
    assert evidence_revision(aliased) == evidence_revision(candidate)


def test_missing_durable_source_revision_fails_closed():
    candidate = replace(
        _candidate("arm-a"),
        source_evidence_revision="",
    )

    decision = evaluate_rolling_arbiter(_request(candidate))

    assert decision.winner is None
    assert "missing_source_evidence_revision" in decision.evaluations[0].blockers


@pytest.mark.parametrize("older_reward_bp", [-6.0, -7.0])
def test_guard_vetoes_non_positive_ev_after_minimum_sample(older_reward_bp):
    candidate = _candidate("arm-a")
    guarded = replace(
        candidate,
        evidence=(
            *candidate.evidence[:4],
            replace(candidate.evidence[4], reward_net_bp=older_reward_bp),
            replace(candidate.evidence[5], reward_net_bp=older_reward_bp),
        ),
    )
    guarded = replace(
        guarded,
        source_evidence_revision=evidence_revision(guarded),
    )

    decision = evaluate_rolling_arbiter(_request(guarded))

    metrics = decision.evaluations[0].metrics
    assert metrics.authority_fee_net_ev_bp == pytest.approx(3.0)
    assert metrics.guard_evaluable == 6
    assert metrics.guard_fee_net_ev_bp <= 0.0
    assert decision.winner is None
    assert any(
        blocker.startswith("guard_fee_net_ev_bp=")
        for blocker in decision.evaluations[0].blockers
    )


def test_guard_ev_veto_waits_for_guard_sample_floor():
    candidate = _candidate("arm-a")
    five_rows = replace(
        candidate,
        evidence=(
            *candidate.evidence[:4],
            replace(candidate.evidence[4], reward_net_bp=-13.0),
        ),
    )
    five_rows = replace(
        five_rows,
        source_evidence_revision=evidence_revision(five_rows),
    )

    decision = evaluate_rolling_arbiter(_request(five_rows))

    evaluation = decision.evaluations[0]
    assert evaluation.metrics.guard_evaluable == 5
    assert evaluation.metrics.guard_fee_net_ev_bp < 0.0
    assert "guard_evaluable=5/6" in evaluation.blockers
    assert not any(
        blocker.startswith("guard_fee_net_ev_bp=")
        for blocker in evaluation.blockers
    )


def test_guard_hard_loss_persists_after_safety_window_and_vetoes():
    candidate = _candidate("arm-a")
    guarded = replace(
        candidate,
        evidence=(
            *candidate.evidence[:4],
            replace(candidate.evidence[4], hard_loss=True),
            candidate.evidence[5],
        ),
    )
    guarded = replace(
        guarded,
        source_evidence_revision=evidence_revision(guarded),
    )

    decision = evaluate_rolling_arbiter(_request(guarded))

    evaluation = decision.evaluations[0]
    assert evaluation.metrics.authority_fee_net_ev_bp == pytest.approx(3.0)
    assert evaluation.metrics.guard_fee_net_ev_bp > 0.0
    assert evaluation.metrics.guard_hard_losses == 1
    assert "latest_result_hard_loss" not in evaluation.blockers
    assert "guard_hard_losses=1/0" in evaluation.blockers
    assert decision.winner is None


@pytest.mark.parametrize(
    ("regime_snapshot", "submit_snapshot", "blocker"),
    [
        (_regime(age_ms=60_001), _submit(), "regime_snapshot_stale"),
        (_regime(), _submit(age_ms=10_001), "submit_snapshot_stale"),
        (
            _regime(confirmations=(NOW - 20_000,)),
            _submit(),
            "regime_confirmations=1/2",
        ),
        (
            _regime(confirmations=(NOW - 40_000, NOW - 25_001)),
            _submit(),
            "regime_dwell_ms=14999/15000",
        ),
        (
            _regime(age_ms=5_000),
            _submit(age_ms=6_000),
            "submit_snapshot_precedes_confirmed_regime",
        ),
    ],
)
def test_regime_freshness_confirmations_and_dwell_fail_closed(
    regime_snapshot,
    submit_snapshot,
    blocker,
):
    decision = evaluate_rolling_arbiter(
        _request(
            _candidate("arm-a"),
            regime_snapshot=regime_snapshot,
            submit_snapshot=submit_snapshot,
        )
    )

    assert decision.winner is None
    assert blocker in decision.blockers


def test_candidate_and_evidence_order_do_not_change_unique_winner():
    arm_a = _candidate("arm-a", tp_reward_bp=4.0)
    arm_b = _candidate("arm-b", tp_reward_bp=6.0)
    forward = evaluate_rolling_arbiter(_request(arm_a, arm_b))
    reversed_input = evaluate_rolling_arbiter(
        _request(
            replace(arm_b, evidence=tuple(reversed(arm_b.evidence))),
            replace(arm_a, evidence=tuple(reversed(arm_a.evidence))),
        )
    )

    assert forward == reversed_input
    assert forward.winner == arm_b.identity
    assert forward.lease_proposal.action == LeaseAction.GRANT


def test_exact_tie_uses_stable_arm_key_not_input_order():
    arm_a = _candidate("arm-a")
    arm_b = _candidate("arm-b")

    forward = evaluate_rolling_arbiter(_request(arm_b, arm_a))
    reverse = evaluate_rolling_arbiter(_request(arm_a, arm_b))

    assert forward.winner == arm_a.identity
    assert reverse.winner == arm_a.identity


def test_challenger_requires_two_bp_paired_delta_and_three_wins():
    incumbent = _candidate("arm-a", tp_reward_bp=4.0)
    insufficient = _candidate("arm-b", tp_reward_bp=6.0)
    held = evaluate_rolling_arbiter(
        _request(
            incumbent,
            insufficient,
            incumbent_arm_key="arm-a",
        )
    )

    assert held.winner == incumbent.identity
    insufficient_eval = _evaluation(held, "arm-b")
    assert insufficient_eval.paired_wins_vs_incumbent == 3
    assert insufficient_eval.paired_ev_delta_vs_incumbent_bp == pytest.approx(
        1.5
    )
    assert insufficient_eval.selection_blockers

    qualified = _candidate("arm-b", tp_reward_bp=7.0)
    switched = evaluate_rolling_arbiter(
        _request(
            incumbent,
            qualified,
            incumbent_arm_key="arm-a",
        )
    )

    assert switched.winner == qualified.identity
    qualified_eval = _evaluation(switched, "arm-b")
    assert qualified_eval.paired_wins_vs_incumbent == 3
    assert qualified_eval.paired_ev_delta_vs_incumbent_bp == pytest.approx(
        2.25
    )
    assert qualified_eval.selection_blockers == ()


def test_safety_hard_loss_and_two_sl_veto():
    candidate = _candidate("arm-a")
    hard_loss_candidate = replace(
        candidate,
        evidence=(
            replace(candidate.evidence[0], hard_loss=True),
            *candidate.evidence[1:],
        ),
    )
    hard_loss = evaluate_rolling_arbiter(_request(hard_loss_candidate))
    assert hard_loss.winner is None
    assert "latest_result_hard_loss" in hard_loss.evaluations[0].blockers

    two_sl_candidate = replace(
        candidate,
        evidence=(
            replace(candidate.evidence[0], outcome=EvidenceOutcome.SL_FIRST),
            replace(candidate.evidence[1], outcome=EvidenceOutcome.SL_FIRST),
            *candidate.evidence[2:],
        ),
    )
    two_sl = evaluate_rolling_arbiter(_request(two_sl_candidate))
    assert two_sl.winner is None
    assert "safety_sl_first=2/2" in two_sl.evaluations[0].blockers


def test_shock_and_direction_invalid_veto():
    candidate = _candidate("arm-a")
    shock = evaluate_rolling_arbiter(
        _request(
            candidate,
            regime_snapshot=_regime("SHOCK"),
            submit_snapshot=_submit("SHOCK"),
        )
    )
    assert shock.winner is None
    assert "shock_regime" in shock.blockers

    invalid_direction = evaluate_rolling_arbiter(
        _request(
            candidate,
            regime_snapshot=_regime(valid_sides=frozenset({"SHORT"})),
            submit_snapshot=_submit(valid_sides=frozenset({"SHORT"})),
        )
    )
    assert invalid_direction.winner is None
    assert "direction_invalid" in invalid_direction.evaluations[0].blockers


def test_regime_and_evidence_drift_immediately_revoke_current_lease():
    candidate = _candidate("arm-a")
    initial = evaluate_rolling_arbiter(_request(candidate))
    lease = CurrentLease(
        arm_key="arm-a",
        phase=LeasePhase.PROBATION,
        regime="RANGE",
        evidence_revision=initial.evidence_revision or "",
        issued_at_ms=NOW - MINUTE,
        expires_at_ms=NOW + MINUTE,
    )

    regime_drift = evaluate_rolling_arbiter(
        _request(
            candidate,
            regime_snapshot=_regime("TREND_UP"),
            submit_snapshot=_submit("TREND_UP"),
            current_lease=lease,
        )
    )
    assert regime_drift.winner is None
    assert regime_drift.revocations[0].reason == "regime_drift"

    insufficient = replace(
        candidate,
        evidence=candidate.evidence[:-1],
    )
    evidence_drift = evaluate_rolling_arbiter(
        _request(insufficient, current_lease=lease)
    )
    assert evidence_drift.winner is None
    assert evidence_drift.revocations[0].reason.startswith("evidence_drift:")


def test_lease_grant_keep_and_new_revision_renewal_durations():
    candidate = _candidate("arm-a")
    initial = evaluate_rolling_arbiter(_request(candidate))

    assert initial.lease_proposal.action == LeaseAction.GRANT
    assert initial.lease_proposal.phase == LeasePhase.PROBATION
    assert initial.lease_proposal.expires_at_ms == NOW + 5 * MINUTE

    current = CurrentLease(
        arm_key="arm-a",
        phase=LeasePhase.PROBATION,
        regime="RANGE",
        evidence_revision=initial.evidence_revision or "",
        issued_at_ms=NOW - MINUTE,
        expires_at_ms=NOW + 2 * MINUTE,
    )
    kept = evaluate_rolling_arbiter(
        _request(candidate, current_lease=current)
    )
    assert kept.lease_proposal.action == LeaseAction.KEEP
    assert kept.lease_proposal.expires_at_ms == current.expires_at_ms

    new_row = _evidence(
        candidate.identity,
        "opp-new",
        age_ms=30_000,
        outcome=EvidenceOutcome.TP_FIRST,
        reward_net_bp=4.0,
    )
    advanced = replace(
        candidate,
        evidence=(*candidate.evidence, new_row),
    )
    advanced = replace(
        advanced,
        source_evidence_revision=evidence_revision(advanced),
    )
    renewed = evaluate_rolling_arbiter(
        _request(advanced, current_lease=current)
    )
    assert renewed.lease_proposal.action == LeaseAction.RENEW
    assert renewed.lease_proposal.phase == LeasePhase.PROBATION
    assert renewed.lease_proposal.expires_at_ms == NOW + 5 * MINUTE

    live = replace(current, phase=LeasePhase.LIVE)
    live_renewed = evaluate_rolling_arbiter(
        _request(advanced, current_lease=live)
    )
    assert live_renewed.lease_proposal.action == LeaseAction.RENEW
    assert live_renewed.lease_proposal.phase == LeasePhase.LIVE
    assert live_renewed.lease_proposal.expires_at_ms == NOW + 10 * MINUTE

    expired = replace(current, expires_at_ms=NOW)
    not_extended = evaluate_rolling_arbiter(
        _request(candidate, current_lease=expired)
    )
    assert not_extended.lease_proposal.action == LeaseAction.NONE
    assert not_extended.lease_proposal.blockers == (
        "lease_expired_without_new_evidence_revision",
    )


def test_row_aging_out_of_guard_window_does_not_count_as_new_evidence():
    candidate = _candidate("arm-a")
    extra_guard_row = _evidence(
        candidate.identity,
        "opp-extra-guard",
        age_ms=179 * MINUTE,
        outcome=EvidenceOutcome.MAX_HOLD,
        reward_net_bp=0.0,
    )
    complete = replace(
        candidate,
        evidence=(*candidate.evidence, extra_guard_row),
    )
    complete = replace(
        complete,
        source_evidence_revision=evidence_revision(complete),
    )
    initial = evaluate_rolling_arbiter(_request(complete))
    current = CurrentLease(
        arm_key="arm-a",
        phase=LeasePhase.PROBATION,
        regime="RANGE",
        evidence_revision=initial.evidence_revision or "",
        issued_at_ms=NOW - MINUTE,
        expires_at_ms=NOW + 2 * MINUTE,
    )

    after_boundary = evaluate_rolling_arbiter(
        replace(
            _request(complete, current_lease=current),
            as_of_ms=NOW + 1,
        )
    )

    assert after_boundary.evaluations[0].metrics.guard_evaluable == 6
    assert after_boundary.lease_proposal.action == LeaseAction.KEEP
    assert after_boundary.lease_proposal.expires_at_ms == current.expires_at_ms


def test_exact_arm_cooldown_does_not_block_other_arm():
    cooled = _candidate("arm-a", tp_reward_bp=8.0)
    alternative = _candidate("arm-b", tp_reward_bp=4.0)

    decision = evaluate_rolling_arbiter(
        _request(
            cooled,
            alternative,
            cooldowns=(
                ArmCooldown(
                    arm_key="arm-a",
                    until_ms=NOW + MINUTE,
                ),
            ),
        )
    )

    assert decision.winner == alternative.identity
    assert any(
        blocker.startswith("exact_arm_cooldown_until:")
        for blocker in _evaluation(decision, "arm-a").blockers
    )


def test_opposite_direction_authority_stays_shadow_without_explicit_rule():
    long_arm = _candidate("arm-long", side="LONG")
    short_arm = _candidate("arm-short", side="SHORT")
    valid_both = frozenset({"LONG", "SHORT"})

    decision = evaluate_rolling_arbiter(
        _request(
            long_arm,
            short_arm,
            regime_snapshot=_regime(valid_sides=valid_both),
            submit_snapshot=_submit(valid_sides=valid_both),
        )
    )

    assert decision.winner is None
    assert "direction_conflict" in decision.blockers
