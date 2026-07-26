"""Pure rolling-window Adaptive Arm arbiter for v1.4.69 Phase B/C.

The module deliberately owns no database, exchange client, settings object, or
order API.  It evaluates immutable snapshots and returns an immutable decision
plus a lease proposal that still requires a durable CAS claim before use.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
from math import isfinite
from typing import Iterable


class EvidenceOutcome(str, Enum):
    TP_FIRST = "TP_FIRST"
    SL_FIRST = "SL_FIRST"
    NO_FILL = "NO_FILL"
    MAX_HOLD = "MAX_HOLD"


class LeasePhase(str, Enum):
    PROBATION = "PROBATION"
    LIVE = "LIVE"


class LeaseAction(str, Enum):
    NONE = "NONE"
    GRANT = "GRANT"
    KEEP = "KEEP"
    RENEW = "RENEW"


@dataclass(frozen=True, slots=True)
class ArmIdentity:
    arm_key: str
    lane_code: str
    side: str
    strategy: str
    regime: str
    execution_profile_id: str
    execution_profile_hash: str


@dataclass(frozen=True, slots=True)
class ArmEvidence:
    arm_key: str
    opportunity_id: str
    observed_at_ms: int
    terminal_at_ms: int
    deadline_at_ms: int
    outcome: EvidenceOutcome | str
    reward_net_bp: float
    regime: str
    paired: bool = True
    evaluable: bool = True
    data_complete: bool = True
    identity_valid: bool = True
    hard_loss: bool = False


@dataclass(frozen=True, slots=True)
class ArmCandidate:
    identity: ArmIdentity
    evidence: tuple[ArmEvidence, ...]
    # Append-only durable ledger revision.  A bounded rolling-window query cannot
    # derive this safely: rows aging out must not look like newly arrived evidence.
    source_evidence_revision: str
    uncertainty_penalty_bp: float = 0.0
    tail_loss_penalty_bp: float = 0.0
    drawdown_penalty_bp: float = 0.0
    distribution_drift: bool = False


@dataclass(frozen=True, slots=True)
class RegimeSnapshot:
    regime: str
    observed_at_ms: int
    confirmation_at_ms: tuple[int, ...] = ()
    direction_valid_sides: frozenset[str] = frozenset({"LONG", "SHORT"})


@dataclass(frozen=True, slots=True)
class ArmCooldown:
    arm_key: str
    until_ms: int
    reason: str = "exact_arm_cooldown"


@dataclass(frozen=True, slots=True)
class CurrentLease:
    arm_key: str
    phase: LeasePhase
    regime: str
    evidence_revision: str
    issued_at_ms: int
    expires_at_ms: int


@dataclass(frozen=True, slots=True)
class ArbiterPolicy:
    safety_window_ms: int = 15 * 60 * 1000
    authority_window_ms: int = 45 * 60 * 1000
    guard_window_ms: int = 180 * 60 * 1000
    regime_max_age_ms: int = 60 * 1000
    submit_max_age_ms: int = 10 * 1000
    regime_confirmations: int = 2
    regime_min_dwell_ms: int = 15 * 1000
    authority_min_paired_evaluable: int = 4
    authority_min_tp_first: int = 3
    authority_min_ev_bp: float = 0.0
    guard_min_evaluable: int = 6
    guard_min_ev_bp: float = 0.0
    guard_max_hard_losses: int = 0
    challenger_min_delta_bp: float = 2.0
    challenger_min_paired_wins: int = 3
    probation_lease_ms: int = 5 * 60 * 1000
    live_lease_ms: int = 10 * 60 * 1000


@dataclass(frozen=True, slots=True)
class ArbiterRequest:
    as_of_ms: int
    regime_snapshot: RegimeSnapshot
    submit_snapshot: RegimeSnapshot
    candidates: tuple[ArmCandidate, ...]
    incumbent_arm_key: str | None = None
    current_lease: CurrentLease | None = None
    cooldowns: tuple[ArmCooldown, ...] = ()
    policy: ArbiterPolicy = ArbiterPolicy()


@dataclass(frozen=True, slots=True)
class ArmMetrics:
    safety_evaluable: int
    safety_sl_first: int
    authority_paired_evaluable: int
    authority_tp_first: int
    authority_no_fill: int
    authority_fee_net_ev_bp: float
    guard_evaluable: int
    guard_hard_losses: int
    guard_fee_net_ev_bp: float
    utility_bp: float


@dataclass(frozen=True, slots=True)
class ArmEvaluation:
    identity: ArmIdentity
    eligible: bool
    blockers: tuple[str, ...]
    selection_blockers: tuple[str, ...]
    metrics: ArmMetrics
    evidence_revision: str
    paired_vs_incumbent: int = 0
    paired_wins_vs_incumbent: int = 0
    paired_ev_delta_vs_incumbent_bp: float | None = None


@dataclass(frozen=True, slots=True)
class LeaseRevocation:
    arm_key: str
    reason: str
    revoke_at_ms: int


@dataclass(frozen=True, slots=True)
class LeaseProposal:
    action: LeaseAction
    arm_key: str | None
    phase: LeasePhase | None
    evidence_revision: str | None
    expires_at_ms: int | None
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ArbiterDecision:
    winner: ArmIdentity | None
    blockers: tuple[str, ...]
    evaluations: tuple[ArmEvaluation, ...]
    evidence_revision: str | None
    lease_proposal: LeaseProposal
    revocations: tuple[LeaseRevocation, ...]

    @property
    def ready_for_lease_cas(self) -> bool:
        return self.lease_proposal.action in {
            LeaseAction.GRANT,
            LeaseAction.RENEW,
        }


@dataclass(frozen=True, slots=True)
class _CandidateWork:
    candidate: ArmCandidate
    evaluation: ArmEvaluation
    authority_rewards: tuple[tuple[str, float], ...]


def _normalized(value: str) -> str:
    return str(value or "").strip().upper()


def _append_unique(blockers: list[str], blocker: str) -> None:
    if blocker not in blockers:
        blockers.append(blocker)


def normalize_evidence_outcome(
    value: EvidenceOutcome | str,
) -> EvidenceOutcome | None:
    """Map repository-facing outcome strings to the arbiter enum."""

    if isinstance(value, EvidenceOutcome):
        return value
    normalized = (
        str(value or "")
        .strip()
        .upper()
        .replace("-", "_")
        .replace(" ", "_")
    )
    aliases = {
        "TP1_FIRST": EvidenceOutcome.TP_FIRST,
        "TP_FIRST": EvidenceOutcome.TP_FIRST,
        "SL_FIRST": EvidenceOutcome.SL_FIRST,
        "NO_FILL": EvidenceOutcome.NO_FILL,
        "MAX_HOLD": EvidenceOutcome.MAX_HOLD,
    }
    return aliases.get(normalized)


def _reward(evidence: ArmEvidence) -> float:
    if normalize_evidence_outcome(evidence.outcome) == EvidenceOutcome.NO_FILL:
        return 0.0
    return float(evidence.reward_net_bp)


def _revision_float(value: float) -> float | str:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return "INVALID"
    return parsed if isfinite(parsed) else "NON_FINITE"


def evidence_revision(candidate: ArmCandidate) -> str:
    """Hash a complete evidence ledger into an order-independent revision.

    This helper is suitable when the caller has the complete immutable ledger.
    Production callers using a bounded query must instead supply a durable,
    append-only ``ArmCandidate.source_evidence_revision`` from storage.
    """

    rows = [
        {
            "arm_key": item.arm_key,
            "opportunity_id": item.opportunity_id,
            "observed_at_ms": item.observed_at_ms,
            "terminal_at_ms": item.terminal_at_ms,
            "deadline_at_ms": item.deadline_at_ms,
            "outcome": (
                normalized_outcome.value
                if (
                    normalized_outcome
                    := normalize_evidence_outcome(item.outcome)
                )
                else str(item.outcome)
            ),
            "reward_net_bp": _revision_float(item.reward_net_bp),
            "regime": item.regime,
            "paired": bool(item.paired),
            "evaluable": bool(item.evaluable),
            "data_complete": bool(item.data_complete),
            "identity_valid": bool(item.identity_valid),
            "hard_loss": bool(item.hard_loss),
        }
        for item in candidate.evidence
    ]
    rows.sort(
        key=lambda row: json.dumps(
            row,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    )
    encoded = json.dumps(
        {
            "schema": "v1469.arm-evidence-revision.1",
            "arm_key": candidate.identity.arm_key,
            "evidence": rows,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "v1469r_" + hashlib.sha256(encoded).hexdigest()


def _identity_blockers(identity: ArmIdentity) -> tuple[str, ...]:
    blockers: list[str] = []
    required = {
        "arm_key": identity.arm_key,
        "lane_code": identity.lane_code,
        "side": identity.side,
        "strategy": identity.strategy,
        "regime": identity.regime,
        "execution_profile_id": identity.execution_profile_id,
        "execution_profile_hash": identity.execution_profile_hash,
    }
    for name, value in required.items():
        if not str(value or "").strip():
            blockers.append(f"identity_missing:{name}")
    if _normalized(identity.side) not in {"LONG", "SHORT"}:
        blockers.append("identity_invalid:side")
    return tuple(blockers)


def _global_blockers(request: ArbiterRequest) -> tuple[str, ...]:
    blockers: list[str] = []
    now = int(request.as_of_ms)
    policy = request.policy
    if now < 0:
        blockers.append("invalid_as_of_ms")
        return tuple(blockers)

    regime = request.regime_snapshot
    regime_age = now - int(regime.observed_at_ms)
    if regime_age < 0:
        blockers.append("regime_snapshot_future")
    elif regime_age > policy.regime_max_age_ms:
        blockers.append("regime_snapshot_stale")

    raw_confirmations = tuple(
        int(value) for value in regime.confirmation_at_ms
    )
    if any(
        value < 0
        or value > int(regime.observed_at_ms)
        or value > now
        for value in raw_confirmations
    ):
        blockers.append("regime_confirmation_time_invalid")
    confirmations = sorted(set(raw_confirmations))
    if len(confirmations) < policy.regime_confirmations:
        blockers.append(
            "regime_confirmations="
            f"{len(confirmations)}/{policy.regime_confirmations}"
        )
    elif confirmations[-1] - confirmations[0] < policy.regime_min_dwell_ms:
        blockers.append(
            "regime_dwell_ms="
            f"{confirmations[-1] - confirmations[0]}/"
            f"{policy.regime_min_dwell_ms}"
        )

    submit = request.submit_snapshot
    submit_age = now - int(submit.observed_at_ms)
    if submit_age < 0:
        blockers.append("submit_snapshot_future")
    elif submit_age > policy.submit_max_age_ms:
        blockers.append("submit_snapshot_stale")
    if int(submit.observed_at_ms) < int(regime.observed_at_ms):
        blockers.append("submit_snapshot_precedes_confirmed_regime")
    if _normalized(submit.regime) != _normalized(regime.regime):
        blockers.append("regime_drift")
    if _normalized(submit.regime) == "SHOCK":
        blockers.append("shock_regime")
    return tuple(blockers)


def _active_cooldown(
    arm_key: str,
    cooldowns: Iterable[ArmCooldown],
    as_of_ms: int,
) -> ArmCooldown | None:
    active = [
        item
        for item in cooldowns
        if item.arm_key == arm_key and int(item.until_ms) > int(as_of_ms)
    ]
    if not active:
        return None
    return sorted(active, key=lambda item: (-int(item.until_ms), item.reason))[0]


def _evaluate_candidate(
    candidate: ArmCandidate,
    request: ArbiterRequest,
    global_blockers: tuple[str, ...],
) -> _CandidateWork:
    identity = candidate.identity
    policy = request.policy
    now = int(request.as_of_ms)
    blockers = list(global_blockers)
    blockers.extend(_identity_blockers(identity))
    source_revision = str(candidate.source_evidence_revision or "").strip()
    if not source_revision:
        _append_unique(blockers, "missing_source_evidence_revision")

    current_regime = _normalized(request.submit_snapshot.regime)
    if _normalized(identity.regime) != current_regime:
        _append_unique(blockers, "arm_regime_mismatch")
    valid_sides = {
        _normalized(side)
        for side in request.submit_snapshot.direction_valid_sides
    }
    if _normalized(identity.side) not in valid_sides:
        _append_unique(blockers, "direction_invalid")
    cooldown = _active_cooldown(identity.arm_key, request.cooldowns, now)
    if cooldown is not None:
        _append_unique(
            blockers,
            f"exact_arm_cooldown_until:{int(cooldown.until_ms)}",
        )
    if candidate.distribution_drift:
        _append_unique(blockers, "evidence_distribution_drift")

    penalties = (
        candidate.uncertainty_penalty_bp,
        candidate.tail_loss_penalty_bp,
        candidate.drawdown_penalty_bp,
    )
    if any(not isfinite(float(value)) or float(value) < 0 for value in penalties):
        _append_unique(blockers, "invalid_utility_penalty")

    guard_start = now - policy.guard_window_ms
    valid_evidence: list[ArmEvidence] = []
    seen_opportunities: set[str] = set()
    for item in sorted(
        candidate.evidence,
        key=lambda row: (
            int(row.observed_at_ms),
            int(row.terminal_at_ms),
            row.opportunity_id,
        ),
    ):
        observed = int(item.observed_at_ms)
        terminal = int(item.terminal_at_ms)
        deadline = int(item.deadline_at_ms)
        relevant = observed >= guard_start or observed > now
        if not relevant:
            continue
        if observed > now or terminal > now:
            _append_unique(
                blockers,
                f"future_evidence:{item.opportunity_id}",
            )
            continue
        if observed < 0 or terminal < observed or deadline < observed:
            _append_unique(
                blockers,
                f"invalid_evidence_time:{item.opportunity_id}",
            )
            continue
        if terminal > deadline:
            _append_unique(
                blockers,
                f"late_evidence:{item.opportunity_id}",
            )
            continue
        if item.arm_key != identity.arm_key or not item.identity_valid:
            _append_unique(
                blockers,
                f"evidence_identity_mismatch:{item.opportunity_id}",
            )
            continue
        if _normalized(item.regime) != _normalized(identity.regime):
            _append_unique(
                blockers,
                f"evidence_regime_mismatch:{item.opportunity_id}",
            )
            continue
        if not item.evaluable:
            continue
        if not item.data_complete:
            _append_unique(
                blockers,
                f"data_incomplete:{item.opportunity_id}",
            )
            continue
        normalized_outcome = normalize_evidence_outcome(item.outcome)
        if normalized_outcome is None:
            _append_unique(
                blockers,
                f"invalid_outcome:{item.opportunity_id}",
            )
            continue
        try:
            reward = float(item.reward_net_bp)
        except (TypeError, ValueError, OverflowError):
            reward = float("nan")
        if not isfinite(reward):
            _append_unique(
                blockers,
                f"invalid_reward:{item.opportunity_id}",
            )
            continue
        if not item.opportunity_id:
            _append_unique(blockers, "evidence_missing:opportunity_id")
            continue
        if item.opportunity_id in seen_opportunities:
            _append_unique(
                blockers,
                f"duplicate_opportunity:{item.opportunity_id}",
            )
            continue
        seen_opportunities.add(item.opportunity_id)
        if normalized_outcome is not item.outcome:
            item = replace(item, outcome=normalized_outcome)
        valid_evidence.append(item)

    safety_start = now - policy.safety_window_ms
    authority_start = now - policy.authority_window_ms
    safety = [
        item for item in valid_evidence
        if int(item.observed_at_ms) >= safety_start
    ]
    authority = [
        item for item in valid_evidence
        if int(item.observed_at_ms) >= authority_start and item.paired
    ]
    guard = valid_evidence

    latest = max(
        safety,
        key=lambda item: (
            int(item.terminal_at_ms),
            item.opportunity_id,
        ),
        default=None,
    )
    if latest is not None and latest.hard_loss:
        _append_unique(blockers, "latest_result_hard_loss")
    safety_sl = sum(
        item.outcome == EvidenceOutcome.SL_FIRST for item in safety
    )
    if safety_sl >= 2:
        _append_unique(blockers, f"safety_sl_first={safety_sl}/2")

    authority_tp = sum(
        item.outcome == EvidenceOutcome.TP_FIRST for item in authority
    )
    authority_no_fill = sum(
        item.outcome == EvidenceOutcome.NO_FILL for item in authority
    )
    authority_ev = (
        sum(_reward(item) for item in authority) / len(authority)
        if authority
        else 0.0
    )
    guard_ev = (
        sum(_reward(item) for item in guard) / len(guard)
        if guard
        else 0.0
    )
    guard_hard_losses = sum(bool(item.hard_loss) for item in guard)
    if len(authority) < policy.authority_min_paired_evaluable:
        _append_unique(
            blockers,
            "authority_paired_evaluable="
            f"{len(authority)}/{policy.authority_min_paired_evaluable}",
        )
    if authority_tp < policy.authority_min_tp_first:
        _append_unique(
            blockers,
            f"authority_tp_first={authority_tp}/"
            f"{policy.authority_min_tp_first}",
        )
    if authority_ev <= policy.authority_min_ev_bp:
        _append_unique(
            blockers,
            "authority_fee_net_ev_bp="
            f"{authority_ev:.6f}>{policy.authority_min_ev_bp:.6f}",
        )
    if len(guard) < policy.guard_min_evaluable:
        _append_unique(
            blockers,
            f"guard_evaluable={len(guard)}/{policy.guard_min_evaluable}",
        )
    else:
        if guard_ev <= policy.guard_min_ev_bp:
            _append_unique(
                blockers,
                "guard_fee_net_ev_bp="
                f"{guard_ev:.6f}>{policy.guard_min_ev_bp:.6f}",
            )
    if guard_hard_losses > policy.guard_max_hard_losses:
        _append_unique(
            blockers,
            f"guard_hard_losses={guard_hard_losses}/"
            f"{policy.guard_max_hard_losses}",
        )

    utility = authority_ev - sum(float(value) for value in penalties)
    metrics = ArmMetrics(
        safety_evaluable=len(safety),
        safety_sl_first=safety_sl,
        authority_paired_evaluable=len(authority),
        authority_tp_first=authority_tp,
        authority_no_fill=authority_no_fill,
        authority_fee_net_ev_bp=authority_ev,
        guard_evaluable=len(guard),
        guard_hard_losses=guard_hard_losses,
        guard_fee_net_ev_bp=guard_ev,
        utility_bp=utility,
    )
    authority_rewards = tuple(
        sorted(
            (
                (item.opportunity_id, _reward(item))
                for item in authority
            ),
            key=lambda pair: pair[0],
        )
    )
    evaluation = ArmEvaluation(
        identity=identity,
        eligible=not blockers,
        blockers=tuple(blockers),
        selection_blockers=(),
        metrics=metrics,
        evidence_revision=source_revision,
    )
    return _CandidateWork(
        candidate=candidate,
        evaluation=evaluation,
        authority_rewards=authority_rewards,
    )


def _rank_key(work: _CandidateWork) -> tuple[float, float, str]:
    metrics = work.evaluation.metrics
    return (
        -metrics.utility_bp,
        -metrics.authority_fee_net_ev_bp,
        work.evaluation.identity.arm_key,
    )


def _challenger_comparison(
    challenger: _CandidateWork,
    incumbent: _CandidateWork,
) -> tuple[int, int, float | None]:
    challenger_rewards = dict(challenger.authority_rewards)
    incumbent_rewards = dict(incumbent.authority_rewards)
    shared = sorted(set(challenger_rewards) & set(incumbent_rewards))
    if not shared:
        return 0, 0, None
    deltas = [
        challenger_rewards[key] - incumbent_rewards[key]
        for key in shared
    ]
    return (
        len(shared),
        sum(delta > 0.0 for delta in deltas),
        sum(deltas) / len(deltas),
    )


def _no_lease_proposal(*blockers: str) -> LeaseProposal:
    return LeaseProposal(
        action=LeaseAction.NONE,
        arm_key=None,
        phase=None,
        evidence_revision=None,
        expires_at_ms=None,
        blockers=tuple(blockers),
    )


def _lease_duration(policy: ArbiterPolicy, phase: LeasePhase) -> int:
    if phase == LeasePhase.LIVE:
        return policy.live_lease_ms
    return policy.probation_lease_ms


def evaluate_rolling_arbiter(request: ArbiterRequest) -> ArbiterDecision:
    """Select at most one arm and propose, but never grant, lease authority."""

    global_blockers = list(_global_blockers(request))
    arm_keys = [item.identity.arm_key for item in request.candidates]
    if len(arm_keys) != len(set(arm_keys)):
        _append_unique(global_blockers, "duplicate_arm_key")

    works = [
        _evaluate_candidate(candidate, request, tuple(global_blockers))
        for candidate in request.candidates
    ]
    works.sort(key=lambda item: item.evaluation.identity.arm_key)
    by_key = {
        item.evaluation.identity.arm_key: item
        for item in works
    }
    eligible = [item for item in works if item.evaluation.eligible]

    if len({_normalized(item.evaluation.identity.side) for item in eligible}) > 1:
        _append_unique(global_blockers, "direction_conflict")
        eligible = []

    incumbent_key = (
        request.incumbent_arm_key
        or (
            request.current_lease.arm_key
            if request.current_lease is not None
            else None
        )
    )
    incumbent = by_key.get(incumbent_key or "")
    winner_work: _CandidateWork | None = None
    if eligible:
        eligible_by_key = {
            item.evaluation.identity.arm_key: item for item in eligible
        }
        eligible_incumbent = eligible_by_key.get(incumbent_key or "")
        if eligible_incumbent is None:
            winner_work = sorted(eligible, key=_rank_key)[0]
        else:
            qualified_challengers: list[_CandidateWork] = []
            updated: dict[str, _CandidateWork] = {}
            for item in eligible:
                if item is eligible_incumbent:
                    continue
                paired, wins, delta = _challenger_comparison(
                    item,
                    eligible_incumbent,
                )
                selection_blockers: list[str] = []
                if (
                    delta is None
                    or delta < request.policy.challenger_min_delta_bp
                ):
                    selection_blockers.append(
                        "challenger_paired_ev_delta="
                        f"{delta if delta is not None else 'NONE'}/"
                        f"{request.policy.challenger_min_delta_bp:.6f}"
                    )
                if wins < request.policy.challenger_min_paired_wins:
                    selection_blockers.append(
                        f"challenger_paired_wins={wins}/"
                        f"{request.policy.challenger_min_paired_wins}"
                    )
                evaluation = replace(
                    item.evaluation,
                    selection_blockers=tuple(selection_blockers),
                    paired_vs_incumbent=paired,
                    paired_wins_vs_incumbent=wins,
                    paired_ev_delta_vs_incumbent_bp=delta,
                )
                updated_item = replace(item, evaluation=evaluation)
                updated[item.evaluation.identity.arm_key] = updated_item
                if not selection_blockers:
                    qualified_challengers.append(updated_item)
            works = [
                updated.get(item.evaluation.identity.arm_key, item)
                for item in works
            ]
            winner_work = (
                sorted(qualified_challengers, key=_rank_key)[0]
                if qualified_challengers
                else eligible_incumbent
            )

    revocations: list[LeaseRevocation] = []
    current = request.current_lease
    current_work = by_key.get(current.arm_key) if current is not None else None
    current_revoke_reason: str | None = None
    if current is not None:
        if "direction_conflict" in global_blockers:
            current_revoke_reason = "direction_conflict"
        elif _normalized(current.regime) != _normalized(
            request.submit_snapshot.regime
        ):
            current_revoke_reason = "regime_drift"
        elif current_work is None:
            current_revoke_reason = "leased_arm_missing"
        elif not current_work.evaluation.eligible:
            detail = (
                current_work.evaluation.blockers[0]
                if current_work.evaluation.blockers
                else "ineligible"
            )
            current_revoke_reason = f"evidence_drift:{detail}"
        elif (
            winner_work is not None
            and winner_work.evaluation.identity.arm_key != current.arm_key
        ):
            current_revoke_reason = "winner_changed"
        if current_revoke_reason is not None:
            revocations.append(
                LeaseRevocation(
                    arm_key=current.arm_key,
                    reason=current_revoke_reason,
                    revoke_at_ms=int(request.as_of_ms),
                )
            )

    if winner_work is None:
        blockers = tuple(global_blockers) or ("no_eligible_arm",)
        return ArbiterDecision(
            winner=None,
            blockers=blockers,
            evaluations=tuple(item.evaluation for item in works),
            evidence_revision=None,
            lease_proposal=_no_lease_proposal(*blockers),
            revocations=tuple(revocations),
        )

    winner = winner_work.evaluation
    now = int(request.as_of_ms)
    proposal: LeaseProposal
    if current is None or current.arm_key != winner.identity.arm_key:
        proposal = LeaseProposal(
            action=LeaseAction.GRANT,
            arm_key=winner.identity.arm_key,
            phase=LeasePhase.PROBATION,
            evidence_revision=winner.evidence_revision,
            expires_at_ms=now + request.policy.probation_lease_ms,
        )
    elif current_revoke_reason is not None:
        proposal = _no_lease_proposal(
            f"current_lease_revoked:{current_revoke_reason}"
        )
    elif current.evidence_revision == winner.evidence_revision:
        if int(current.expires_at_ms) <= now:
            proposal = _no_lease_proposal(
                "lease_expired_without_new_evidence_revision"
            )
        else:
            proposal = LeaseProposal(
                action=LeaseAction.KEEP,
                arm_key=winner.identity.arm_key,
                phase=current.phase,
                evidence_revision=winner.evidence_revision,
                expires_at_ms=int(current.expires_at_ms),
            )
    else:
        duration = _lease_duration(request.policy, current.phase)
        proposal = LeaseProposal(
            action=LeaseAction.RENEW,
            arm_key=winner.identity.arm_key,
            phase=current.phase,
            evidence_revision=winner.evidence_revision,
            expires_at_ms=now + duration,
        )

    return ArbiterDecision(
        winner=winner.identity,
        blockers=tuple(global_blockers),
        evaluations=tuple(item.evaluation for item in works),
        evidence_revision=winner.evidence_revision,
        lease_proposal=proposal,
        revocations=tuple(revocations),
    )


__all__ = [
    "ArmCandidate",
    "ArmCooldown",
    "ArmEvaluation",
    "ArmEvidence",
    "ArmIdentity",
    "ArmMetrics",
    "ArbiterDecision",
    "ArbiterPolicy",
    "ArbiterRequest",
    "CurrentLease",
    "EvidenceOutcome",
    "LeaseAction",
    "LeasePhase",
    "LeaseProposal",
    "LeaseRevocation",
    "RegimeSnapshot",
    "evaluate_rolling_arbiter",
    "evidence_revision",
    "normalize_evidence_outcome",
]
