"""Fail-closed v1.4.69 adaptive authority evaluation.

This module deliberately stops before paid-order claim or exchange submission.
It turns complete durable shadow evidence plus caller-owned fresh market state
into an immutable admission result.  A result is submit-admissible only when
the selected arm also belongs to the *current* durable opportunity and is
backed by an exact active durable lease.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from src.gridbot.mainnet.v1469_adaptive_identity import (
    EXECUTION_PROFILE_SCHEMA,
)
from src.gridbot.mainnet.v1469_arbiter_evidence_mapper import (
    DurableEvidenceMapping,
    EvidenceMappingIssue,
    map_durable_paired_evidence,
)
from src.gridbot.mainnet.v1469_arm_arbiter import (
    ArmCooldown,
    ArmIdentity,
    ArbiterDecision,
    ArbiterPolicy,
    ArbiterRequest,
    CurrentLease,
    LeaseAction,
    LeasePhase,
    LeaseProposal,
    RegimeSnapshot,
    evaluate_rolling_arbiter,
)
from src.gridbot.mainnet.v1469_arm_profiles import (
    LEGACY_CONTROL,
    get_arm_profile,
)
from src.gridbot.storage.v1469_arm_observation_repository import (
    V1469ArmObservationRepository,
    arm_identity,
)
from src.gridbot.storage.v1469_lease_repository import (
    DurableArmLease,
    LeaseContext,
    LeaseMutationResult,
    V1469LeaseRepository,
)
from src.gridbot.mainnet.v1469_paid_promotion_runtime import (
    V1469PaidPromotionRuntime,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthorityRuntimeInput:
    """Caller-owned causal inputs for one submit-time authority evaluation."""

    environment: str
    symbol: str
    opportunity_id: str
    as_of_ms: int
    regime_snapshot: RegimeSnapshot
    submit_snapshot: RegimeSnapshot
    current_lease: CurrentLease | DurableArmLease | None = None
    incumbent_arm_key: str | None = None
    cooldowns: tuple[ArmCooldown, ...] = ()
    policy: ArbiterPolicy = ArbiterPolicy()
    ledger_limit: int = 50_000


@dataclass(frozen=True, slots=True, kw_only=True)
class LeaseApplyRequest:
    """Explicit opt-in and audit context for a durable lease mutation."""

    context: LeaseContext
    idempotency_key: str
    actor: str
    expected_arm_key: str | None = None
    expected_evidence_revision: str | None = None
    expected_action: LeaseAction | None = None
    expected_phase: LeasePhase | None = None
    expected_execution_profile_hash: str | None = None
    expected_regime: str | None = None


@dataclass(frozen=True, slots=True)
class CurrentOpportunityEligibility:
    """Exact current durable candidate/profile membership for the winner."""

    opportunity_id: str
    candidate_id: str
    observed_at_ms: int
    arm_key: str
    lane_code: str
    side: str
    strategy: str
    regime: str
    execution_profile_id: str
    execution_profile_hash: str


@dataclass(frozen=True, slots=True)
class AuthorityRuntimeResult:
    """Immutable, fail-closed output consumed by a later claim boundary."""

    submit_admissible: bool
    blockers: tuple[str, ...]
    decision: ArbiterDecision
    arbiter_request: ArbiterRequest | None
    evidence_mapping: DurableEvidenceMapping | None
    current_opportunity: CurrentOpportunityEligibility | None
    durable_lease: DurableArmLease | None
    lease_mutation: LeaseMutationResult | None
    ledger_row_count: int
    ledger_scope_complete: bool
    ledger_revision: str | None

    @property
    def winner(self) -> ArmIdentity | None:
        return self.decision.winner

    @property
    def mapping_issues(self) -> tuple[EvidenceMappingIssue, ...]:
        if self.evidence_mapping is None:
            return ()
        return self.evidence_mapping.issues


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


def _blocked_decision(blockers: Sequence[str]) -> ArbiterDecision:
    reasons = _unique(blockers) or ("authority_runtime_blocked",)
    return ArbiterDecision(
        winner=None,
        blockers=reasons,
        evaluations=(),
        evidence_revision=None,
        lease_proposal=LeaseProposal(
            action=LeaseAction.NONE,
            arm_key=None,
            phase=None,
            evidence_revision=None,
            expires_at_ms=None,
            blockers=reasons,
        ),
        revocations=(),
    )


def _exception_blocker(prefix: str, exc: BaseException) -> str:
    return f"{prefix}:{type(exc).__name__}"


def _normalized_text(value: Any) -> str:
    return str(value or "").strip().upper()


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return parsed


def _current_lease_view(
    lease: CurrentLease | DurableArmLease | None,
) -> CurrentLease | None:
    if lease is None:
        return None
    if isinstance(lease, DurableArmLease):
        return lease.as_current_lease()
    if isinstance(lease, CurrentLease):
        return lease
    raise TypeError("current_lease must be CurrentLease or DurableArmLease")


def _current_lease_input_blockers(
    lease: CurrentLease | DurableArmLease | None,
    *,
    environment: str,
    symbol: str,
    as_of_ms: int,
) -> tuple[str, ...]:
    if lease is None:
        return ()
    view = _current_lease_view(lease)
    assert view is not None
    blockers: list[str] = []
    if not str(view.arm_key or "").strip():
        blockers.append("current_lease_arm_missing")
    if not str(view.evidence_revision or "").strip():
        blockers.append("current_lease_revision_missing")
    if not isinstance(view.phase, LeasePhase):
        blockers.append("current_lease_phase_invalid")
    if int(view.issued_at_ms) < 0 or int(view.issued_at_ms) > as_of_ms:
        blockers.append("current_lease_issued_at_invalid")
    if int(view.expires_at_ms) <= as_of_ms:
        blockers.append("current_lease_not_fresh")
    if isinstance(lease, DurableArmLease):
        if _normalized_text(lease.environment) != environment:
            blockers.append("current_lease_environment_mismatch")
        if _normalized_text(lease.symbol) != symbol:
            blockers.append("current_lease_symbol_mismatch")
        if _normalized_text(lease.status) != "ACTIVE":
            blockers.append("current_lease_not_active")
        if int(lease.evidence_as_of_ms) < 0 or int(
            lease.evidence_as_of_ms
        ) > as_of_ms:
            blockers.append("current_lease_evidence_time_invalid")
    return _unique(blockers)


def _same_current_lease(
    durable: DurableArmLease,
    expected: CurrentLease | DurableArmLease | None,
) -> bool:
    if expected is None:
        return False
    if isinstance(expected, DurableArmLease):
        return durable == expected
    return durable.as_current_lease() == _current_lease_view(expected)


def _validate_bundle(
    bundle: Any,
    runtime_input: AuthorityRuntimeInput,
    *,
    environment: str,
    symbol: str,
    as_of_ms: int,
) -> tuple[dict[str, Any] | None, tuple[dict[str, Any], ...], tuple[str, ...]]:
    if not isinstance(bundle, Mapping):
        return None, (), ("current_opportunity_bundle_invalid",)
    raw_opportunity = bundle.get("opportunity")
    raw_candidates = bundle.get("candidates")
    if not isinstance(raw_opportunity, Mapping):
        return None, (), ("current_opportunity_invalid",)
    if (
        not isinstance(raw_candidates, Sequence)
        or isinstance(raw_candidates, (str, bytes, bytearray))
        or any(not isinstance(item, Mapping) for item in raw_candidates)
    ):
        return None, (), ("current_candidates_invalid",)

    opportunity = dict(raw_opportunity)
    candidates = tuple(dict(item) for item in raw_candidates)
    blockers: list[str] = []
    try:
        observed_at_ms = _non_negative_int(
            opportunity.get("observed_at_ms"),
            "opportunity.observed_at_ms",
        )
    except (TypeError, ValueError, OverflowError):
        observed_at_ms = -1
        blockers.append("current_opportunity_time_invalid")
    if str(opportunity.get("opportunity_id") or "").strip() != str(
        runtime_input.opportunity_id
    ).strip():
        blockers.append("current_opportunity_id_mismatch")
    if _normalized_text(opportunity.get("environment")) != environment:
        blockers.append("current_opportunity_environment_mismatch")
    if _normalized_text(opportunity.get("symbol")) != symbol:
        blockers.append("current_opportunity_symbol_mismatch")
    if _normalized_text(opportunity.get("data_quality")) != "COMPLETE":
        blockers.append("current_opportunity_data_incomplete")
    if observed_at_ms >= 0:
        age_ms = as_of_ms - observed_at_ms
        if age_ms < 0:
            blockers.append("current_opportunity_future")
        elif age_ms > int(runtime_input.policy.submit_max_age_ms):
            blockers.append("current_opportunity_stale")
        if observed_at_ms > int(runtime_input.submit_snapshot.observed_at_ms):
            blockers.append("current_opportunity_after_submit_snapshot")
    opportunity_regime = _normalized_text(opportunity.get("coarse_regime"))
    if opportunity_regime != _normalized_text(
        runtime_input.regime_snapshot.regime
    ):
        blockers.append("current_opportunity_regime_mismatch")
    if opportunity_regime != _normalized_text(
        runtime_input.submit_snapshot.regime
    ):
        blockers.append("current_opportunity_submit_regime_mismatch")
    if not candidates:
        blockers.append("current_opportunity_has_no_candidates")
    return opportunity, candidates, _unique(blockers)


def _current_opportunity_match(
    *,
    opportunity: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    winner: ArmIdentity,
    opportunity_id: str,
) -> tuple[CurrentOpportunityEligibility | None, tuple[str, ...]]:
    if _normalized_text(winner.execution_profile_id) == LEGACY_CONTROL:
        # The dynamic legacy profile sidecar is intentionally not reconstructed
        # from a static menu.  Legacy remains an incumbent/fallback authority
        # outside this adaptive submit-admission result.
        return None, ("legacy_control_requires_legacy_paid_path",)

    matching_candidates = [
        item
        for item in candidates
        if _normalized_text(item.get("lane_code"))
        == _normalized_text(winner.lane_code)
        and _normalized_text(item.get("effective_side"))
        == _normalized_text(winner.side)
        and _normalized_text(item.get("strategy"))
        == _normalized_text(winner.strategy)
    ]
    if not matching_candidates:
        return None, ("winner_not_in_current_candidates",)

    safe_candidates = [
        item
        for item in matching_candidates
        if _normalized_text(item.get("match_status")) == "MATCH"
        and _normalized_text(item.get("safety_status"))
        in {"SAFE", "NOT_EVALUATED"}
        and item.get("data_complete") in {True, 1}
    ]
    if not safe_candidates:
        return None, ("winner_current_candidate_not_safe",)

    try:
        profile = get_arm_profile(winner.execution_profile_id)
    except (TypeError, ValueError):
        return None, ("winner_profile_not_in_closed_menu",)
    if profile.execution_profile is None or profile.risk_off:
        return None, ("winner_profile_has_no_paid_execution",)
    regime = _normalized_text(opportunity.get("coarse_regime"))
    if regime not in {
        _normalized_text(value) for value in profile.allowed_regimes
    }:
        return None, ("winner_profile_not_legal_for_current_regime",)
    if profile.execution_profile_hash != winner.execution_profile_hash:
        return None, ("winner_profile_hash_mismatch",)

    exact_matches: list[CurrentOpportunityEligibility] = []
    for candidate in safe_candidates:
        try:
            expected_arm_key = arm_identity(
                {
                    "lane_code": str(candidate.get("lane_code") or "").strip(),
                    "effective_side": _normalized_text(
                        candidate.get("effective_side")
                    ),
                    "strategy": str(candidate.get("strategy") or "").strip(),
                    "coarse_regime": regime,
                    "execution_profile_id": profile.profile_id,
                    "execution_profile_schema": EXECUTION_PROFILE_SCHEMA,
                    "execution_profile_hash": profile.execution_profile_hash,
                }
            )
        except (TypeError, ValueError):
            continue
        if expected_arm_key != winner.arm_key:
            continue
        try:
            observed_at_ms = _non_negative_int(
                opportunity.get("observed_at_ms"),
                "opportunity.observed_at_ms",
            )
        except (TypeError, ValueError, OverflowError):
            continue
        exact_matches.append(
            CurrentOpportunityEligibility(
                opportunity_id=str(opportunity_id).strip(),
                candidate_id=str(candidate.get("candidate_id") or "").strip(),
                observed_at_ms=observed_at_ms,
                arm_key=winner.arm_key,
                lane_code=winner.lane_code,
                side=winner.side,
                strategy=winner.strategy,
                regime=winner.regime,
                execution_profile_id=winner.execution_profile_id,
                execution_profile_hash=winner.execution_profile_hash,
            )
        )
    if len(exact_matches) != 1:
        return None, (
            "winner_current_arm_identity_missing"
            if not exact_matches
            else "winner_current_arm_identity_ambiguous",
        )
    return exact_matches[0], ()


def _derive_initial_legacy_incumbent(
    *,
    opportunity: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    mapped_candidates: Sequence[Any],
) -> tuple[str | None, tuple[str, ...]]:
    """Resolve the one durable selected legacy arm for first arbitration."""

    selected_candidates = [
        item
        for item in candidates
        if item.get("is_selected") in {True, 1}
        and _normalized_text(item.get("match_status")) == "MATCH"
        and _normalized_text(item.get("safety_status"))
        in {"SAFE", "NOT_EVALUATED"}
        and item.get("data_complete") in {True, 1}
    ]
    if not selected_candidates:
        # First arbitration may start without an incumbent.
        return None, ()
    if len(selected_candidates) != 1:
        return None, ("initial_legacy_selected_candidate_ambiguous",)

    selected = selected_candidates[0]
    regime = _normalized_text(opportunity.get("coarse_regime"))
    exact_legacy_arms = [
        candidate.identity.arm_key
        for candidate in mapped_candidates
        if _normalized_text(candidate.identity.execution_profile_id)
        == LEGACY_CONTROL
        and _normalized_text(candidate.identity.lane_code)
        == _normalized_text(selected.get("lane_code"))
        and _normalized_text(candidate.identity.side)
        == _normalized_text(selected.get("effective_side"))
        and _normalized_text(candidate.identity.strategy)
        == _normalized_text(selected.get("strategy"))
        and _normalized_text(candidate.identity.regime) == regime
    ]
    if not exact_legacy_arms:
        # The selected durable candidate may itself be adaptive.
        return None, ()
    if len(exact_legacy_arms) != 1:
        return None, ("initial_legacy_incumbent_ambiguous",)
    return exact_legacy_arms[0], ()


def _lease_authority_blockers(
    lease: DurableArmLease | None,
    *,
    winner: ArmIdentity | None,
    evidence_revision: str | None,
    environment: str,
    symbol: str,
    as_of_ms: int,
) -> tuple[str, ...]:
    if winner is None:
        return ("arbiter_no_winner",)
    if lease is None:
        return ("durable_active_lease_missing",)
    blockers: list[str] = []
    if _normalized_text(lease.environment) != environment:
        blockers.append("authority_lease_environment_mismatch")
    if _normalized_text(lease.symbol) != symbol:
        blockers.append("authority_lease_symbol_mismatch")
    if _normalized_text(lease.status) != "ACTIVE":
        blockers.append("authority_lease_not_active")
    if int(lease.expires_at_ms) <= as_of_ms:
        blockers.append("authority_lease_expired")
    if lease.arm_key != winner.arm_key:
        blockers.append("authority_lease_arm_mismatch")
    if _normalized_text(lease.lane_code) != _normalized_text(winner.lane_code):
        blockers.append("authority_lease_lane_mismatch")
    if _normalized_text(lease.effective_side) != _normalized_text(winner.side):
        blockers.append("authority_lease_side_mismatch")
    if _normalized_text(lease.strategy) != _normalized_text(winner.strategy):
        blockers.append("authority_lease_strategy_mismatch")
    if _normalized_text(lease.coarse_regime) != _normalized_text(winner.regime):
        blockers.append("authority_lease_regime_mismatch")
    if _normalized_text(
        lease.execution_profile_id
    ) != _normalized_text(winner.execution_profile_id):
        blockers.append("authority_lease_profile_mismatch")
    if lease.execution_profile_hash != winner.execution_profile_hash:
        blockers.append("authority_lease_profile_hash_mismatch")
    if lease.evidence_revision != str(evidence_revision or ""):
        blockers.append("authority_lease_revision_mismatch")
    if int(lease.evidence_as_of_ms) < 0 or int(
        lease.evidence_as_of_ms
    ) > as_of_ms:
        blockers.append("authority_lease_evidence_time_invalid")
    if not str(lease.risk_policy_hash or "").strip():
        blockers.append("authority_lease_risk_policy_missing")
    if float(lease.notional_cap_usdc) <= 0:
        blockers.append("authority_lease_notional_cap_invalid")
    return _unique(blockers)


class V1469AuthorityRuntime:
    """Load, map, evaluate, and optionally persist one adaptive lease proposal."""

    def __init__(
        self,
        observation_repository: V1469ArmObservationRepository,
        lease_repository: V1469LeaseRepository | None = None,
        promotion_runtime: V1469PaidPromotionRuntime | None = None,
    ) -> None:
        self._observation_repository = observation_repository
        self._lease_repository = lease_repository
        self._promotion_runtime = promotion_runtime

    def _blocked_result(
        self,
        blockers: Sequence[str],
        *,
        decision: ArbiterDecision | None = None,
        arbiter_request: ArbiterRequest | None = None,
        evidence_mapping: DurableEvidenceMapping | None = None,
        current_opportunity: CurrentOpportunityEligibility | None = None,
        durable_lease: DurableArmLease | None = None,
        lease_mutation: LeaseMutationResult | None = None,
        ledger_row_count: int = 0,
        ledger_scope_complete: bool = False,
    ) -> AuthorityRuntimeResult:
        reasons = _unique(blockers) or ("authority_runtime_blocked",)
        return AuthorityRuntimeResult(
            submit_admissible=False,
            blockers=reasons,
            decision=decision or _blocked_decision(reasons),
            arbiter_request=arbiter_request,
            evidence_mapping=evidence_mapping,
            current_opportunity=current_opportunity,
            durable_lease=durable_lease,
            lease_mutation=lease_mutation,
            ledger_row_count=ledger_row_count,
            ledger_scope_complete=ledger_scope_complete,
            ledger_revision=(
                evidence_mapping.ledger_revision
                if evidence_mapping is not None
                else None
            ),
        )

    async def evaluate(
        self,
        runtime_input: AuthorityRuntimeInput,
        *,
        lease_apply: LeaseApplyRequest | None = None,
    ) -> AuthorityRuntimeResult:
        """Return authority that is safe to present to an atomic claim step."""

        if not isinstance(runtime_input, AuthorityRuntimeInput):
            return self._blocked_result(("authority_runtime_input_invalid",))
        try:
            environment = _normalized_text(runtime_input.environment)
            symbol = _normalized_text(runtime_input.symbol)
            opportunity_id = str(runtime_input.opportunity_id or "").strip()
            as_of_ms = _non_negative_int(runtime_input.as_of_ms, "as_of_ms")
            ledger_limit = _non_negative_int(
                runtime_input.ledger_limit, "ledger_limit"
            )
            if not environment or not symbol or not opportunity_id:
                raise ValueError("authority scope must be non-empty")
            if not 1 <= ledger_limit <= 100_000:
                raise ValueError("ledger_limit must be between 1 and 100000")
            if not isinstance(runtime_input.regime_snapshot, RegimeSnapshot):
                raise TypeError("regime_snapshot must be RegimeSnapshot")
            if not isinstance(runtime_input.submit_snapshot, RegimeSnapshot):
                raise TypeError("submit_snapshot must be RegimeSnapshot")
            if not isinstance(runtime_input.policy, ArbiterPolicy):
                raise TypeError("policy must be ArbiterPolicy")
            if not isinstance(runtime_input.cooldowns, tuple) or any(
                not isinstance(item, ArmCooldown)
                for item in runtime_input.cooldowns
            ):
                raise TypeError("cooldowns must be a tuple of ArmCooldown")
            current_view = _current_lease_view(runtime_input.current_lease)
        except (TypeError, ValueError, OverflowError) as exc:
            return self._blocked_result(
                (_exception_blocker("authority_runtime_input_invalid", exc),)
            )

        current_input_blockers = _current_lease_input_blockers(
            runtime_input.current_lease,
            environment=environment,
            symbol=symbol,
            as_of_ms=as_of_ms,
        )
        if current_input_blockers:
            return self._blocked_result(current_input_blockers)

        try:
            bundle = await self._observation_repository.load_observation_bundle(
                opportunity_id
            )
        except Exception as exc:  # repository/read corruption fails closed
            return self._blocked_result(
                (_exception_blocker("current_opportunity_load_failed", exc),)
            )
        if bundle is None:
            return self._blocked_result(("current_opportunity_missing",))
        opportunity, current_candidates, bundle_blockers = _validate_bundle(
            bundle,
            runtime_input,
            environment=environment,
            symbol=symbol,
            as_of_ms=as_of_ms,
        )
        if bundle_blockers or opportunity is None:
            return self._blocked_result(bundle_blockers)

        try:
            ledger = (
                await self._observation_repository.durable_terminal_evidence_ledger(
                    environment=environment,
                    symbol=symbol,
                    as_of_ms=as_of_ms,
                    limit=ledger_limit,
                )
            )
        except Exception as exc:  # repository/read corruption fails closed
            return self._blocked_result(
                (_exception_blocker("durable_ledger_load_failed", exc),)
            )
        if not isinstance(ledger, Mapping):
            return self._blocked_result(("durable_ledger_envelope_invalid",))

        rows = ledger.get("rows")
        try:
            scope_complete = ledger.get("scope_complete")
            row_count = _non_negative_int(
                ledger.get("row_count"), "ledger.row_count"
            )
            returned_limit = _non_negative_int(
                ledger.get("limit"), "ledger.limit"
            )
            returned_as_of = _non_negative_int(
                ledger.get("as_of_ms"), "ledger.as_of_ms"
            )
            truncated = ledger.get("truncated")
            if (
                not isinstance(rows, Sequence)
                or isinstance(rows, (str, bytes, bytearray))
                or scope_complete not in {False, True}
                or truncated not in {False, True}
                or row_count != len(rows)
                or row_count > ledger_limit
                or returned_limit != ledger_limit
                or returned_as_of != as_of_ms
                or bool(truncated) == bool(scope_complete)
            ):
                raise ValueError("durable ledger envelope mismatch")
        except (TypeError, ValueError, OverflowError):
            return self._blocked_result(("durable_ledger_envelope_invalid",))

        try:
            mapping = map_durable_paired_evidence(
                rows,
                ledger_scope_complete=bool(scope_complete),
            )
        except Exception as exc:  # exact mapper corruption fails closed
            return self._blocked_result(
                (_exception_blocker("durable_evidence_mapping_failed", exc),),
                ledger_row_count=row_count,
                ledger_scope_complete=bool(scope_complete),
            )
        if not scope_complete:
            return self._blocked_result(
                ("durable_ledger_scope_incomplete",),
                evidence_mapping=mapping,
                ledger_row_count=row_count,
                ledger_scope_complete=False,
            )

        effective_incumbent_arm_key = runtime_input.incumbent_arm_key
        if effective_incumbent_arm_key is None and current_view is None:
            (
                effective_incumbent_arm_key,
                incumbent_blockers,
            ) = _derive_initial_legacy_incumbent(
                opportunity=opportunity,
                candidates=current_candidates,
                mapped_candidates=mapping.candidates,
            )
            if incumbent_blockers:
                return self._blocked_result(
                    incumbent_blockers,
                    evidence_mapping=mapping,
                    ledger_row_count=row_count,
                    ledger_scope_complete=True,
                )

        arbiter_request = ArbiterRequest(
            as_of_ms=as_of_ms,
            regime_snapshot=runtime_input.regime_snapshot,
            submit_snapshot=runtime_input.submit_snapshot,
            candidates=mapping.candidates,
            incumbent_arm_key=effective_incumbent_arm_key,
            current_lease=current_view,
            cooldowns=runtime_input.cooldowns,
            policy=runtime_input.policy,
        )
        try:
            decision = evaluate_rolling_arbiter(arbiter_request)
        except Exception as exc:
            return self._blocked_result(
                (_exception_blocker("arbiter_evaluation_failed", exc),),
                arbiter_request=arbiter_request,
                evidence_mapping=mapping,
                ledger_row_count=row_count,
                ledger_scope_complete=True,
            )
        if decision.winner is None:
            return self._blocked_result(
                ("arbiter_no_winner", *decision.blockers),
                decision=decision,
                arbiter_request=arbiter_request,
                evidence_mapping=mapping,
                ledger_row_count=row_count,
                ledger_scope_complete=True,
            )

        current_match, current_match_blockers = _current_opportunity_match(
            opportunity=opportunity,
            candidates=current_candidates,
            winner=decision.winner,
            opportunity_id=opportunity_id,
        )
        if current_match_blockers or current_match is None:
            return self._blocked_result(
                current_match_blockers,
                decision=decision,
                arbiter_request=arbiter_request,
                evidence_mapping=mapping,
                ledger_row_count=row_count,
                ledger_scope_complete=True,
            )

        if self._lease_repository is None:
            return self._blocked_result(
                ("lease_repository_unavailable",),
                decision=decision,
                arbiter_request=arbiter_request,
                evidence_mapping=mapping,
                current_opportunity=current_match,
                ledger_row_count=row_count,
                ledger_scope_complete=True,
            )
        try:
            source_active_lease = await self._lease_repository.get_active_lease(
                environment=environment,
                symbol=symbol,
                now_ms=as_of_ms,
            )
        except Exception as exc:
            return self._blocked_result(
                (_exception_blocker("active_lease_load_failed", exc),),
                decision=decision,
                arbiter_request=arbiter_request,
                evidence_mapping=mapping,
                current_opportunity=current_match,
                ledger_row_count=row_count,
                ledger_scope_complete=True,
            )
        if runtime_input.current_lease is None:
            if source_active_lease is not None:
                return self._blocked_result(
                    ("caller_current_lease_mismatch",),
                    decision=decision,
                    arbiter_request=arbiter_request,
                    evidence_mapping=mapping,
                    current_opportunity=current_match,
                    durable_lease=source_active_lease,
                    ledger_row_count=row_count,
                    ledger_scope_complete=True,
                )
        elif source_active_lease is None or not _same_current_lease(
            source_active_lease, runtime_input.current_lease
        ):
            return self._blocked_result(
                ("caller_current_lease_mismatch",),
                decision=decision,
                arbiter_request=arbiter_request,
                evidence_mapping=mapping,
                current_opportunity=current_match,
                durable_lease=source_active_lease,
                ledger_row_count=row_count,
                ledger_scope_complete=True,
            )

        # Caller state is only an expected snapshot. Final authority always
        # originates from the repository row loaded above (and is reloaded
        # after a mutation below). Claim-time CAS validates it once more.
        authority_lease = source_active_lease
        lease_mutation: LeaseMutationResult | None = None
        promotion = None
        if self._promotion_runtime is not None:
            promotion = await self._promotion_runtime.evaluate(
                decision,
                environment=environment,
                symbol=symbol,
                now_ms=as_of_ms,
                live_lease_ms=runtime_input.policy.live_lease_ms,
            )
            decision = promotion.decision
        proposal = decision.lease_proposal
        if lease_apply is not None:
            if not isinstance(lease_apply, LeaseApplyRequest):
                return self._blocked_result(
                    ("lease_apply_request_invalid",),
                    decision=decision,
                    arbiter_request=arbiter_request,
                    evidence_mapping=mapping,
                    current_opportunity=current_match,
                    ledger_row_count=row_count,
                    ledger_scope_complete=True,
                )
            context = lease_apply.context
            if not isinstance(context, LeaseContext):
                return self._blocked_result(
                    ("lease_context_invalid",),
                    decision=decision,
                    arbiter_request=arbiter_request,
                    evidence_mapping=mapping,
                    current_opportunity=current_match,
                    ledger_row_count=row_count,
                    ledger_scope_complete=True,
                )
            winner = decision.winner
            apply_identity_changed = (
                winner is None
                or (
                    lease_apply.expected_arm_key is not None
                    and winner.arm_key != lease_apply.expected_arm_key
                )
                or (
                    lease_apply.expected_evidence_revision is not None
                    and decision.evidence_revision
                    != lease_apply.expected_evidence_revision
                )
                or (
                    lease_apply.expected_action is not None
                    and proposal.action is not lease_apply.expected_action
                )
                or (
                    lease_apply.expected_phase is not None
                    and proposal.phase is not lease_apply.expected_phase
                )
                or (
                    lease_apply.expected_execution_profile_hash is not None
                    and winner.execution_profile_hash
                    != lease_apply.expected_execution_profile_hash
                )
                or (
                    lease_apply.expected_regime is not None
                    and winner.regime != lease_apply.expected_regime
                )
            )
            if apply_identity_changed:
                return self._blocked_result(
                    ("lease_apply_decision_changed",),
                    decision=decision,
                    arbiter_request=arbiter_request,
                    evidence_mapping=mapping,
                    current_opportunity=current_match,
                    durable_lease=authority_lease,
                    ledger_row_count=row_count,
                    ledger_scope_complete=True,
                )
            if (
                _normalized_text(context.environment) != environment
                or _normalized_text(context.symbol) != symbol
            ):
                return self._blocked_result(
                    ("lease_context_scope_mismatch",),
                    decision=decision,
                    arbiter_request=arbiter_request,
                    evidence_mapping=mapping,
                    current_opportunity=current_match,
                    ledger_row_count=row_count,
                    ledger_scope_complete=True,
                )
            if decision.revocations:
                return self._blocked_result(
                    ("lease_revocation_required",),
                    decision=decision,
                    arbiter_request=arbiter_request,
                    evidence_mapping=mapping,
                    current_opportunity=current_match,
                    durable_lease=authority_lease,
                    ledger_row_count=row_count,
                    ledger_scope_complete=True,
                )
            try:
                target = await self._lease_repository.get_lease(
                    decision.winner.arm_key
                )
                if proposal.action == LeaseAction.KEEP:
                    if target is None or not _same_current_lease(
                        target, runtime_input.current_lease
                    ):
                        raise RuntimeError("lease_state_changed")
                    authority_lease = target
                elif proposal.action in {
                    LeaseAction.GRANT,
                    LeaseAction.RENEW,
                }:
                    if proposal.action == LeaseAction.RENEW:
                        if target is None or not _same_current_lease(
                            target, runtime_input.current_lease
                        ):
                            raise RuntimeError("lease_state_changed")
                    elif target is not None and _normalized_text(
                        target.status
                    ) == "ACTIVE":
                        raise RuntimeError("lease_state_changed")
                    if promotion is not None and promotion.promoted:
                        if target is None or not _same_current_lease(
                            target, runtime_input.current_lease
                        ):
                            raise RuntimeError("lease_state_changed")
                        if target.phase is not LeasePhase.PROBATION:
                            raise RuntimeError(
                                "promotion_source_not_probation"
                            )
                        if (
                            str(context.risk_policy_hash)
                            != str(target.risk_policy_hash)
                        ):
                            raise RuntimeError(
                                "promotion_risk_policy_changed"
                            )
                        evidence = promotion.evidence
                        if evidence is None or evidence.hard_loss_marker:
                            raise RuntimeError(
                                "promotion_evidence_not_safe"
                            )
                        if proposal.expires_at_ms is None:
                            raise RuntimeError(
                                "promotion_expiry_missing"
                            )
                        lease_mutation = (
                            await self._lease_repository.promote_probation_to_live(
                                environment=environment,
                                symbol=symbol,
                                arm_key=target.arm_key,
                                lease_id=target.lease_id,
                                expected_generation=int(target.generation),
                                expected_evidence_revision=(
                                    target.evidence_revision
                                ),
                                new_evidence_revision=(
                                    proposal.evidence_revision or ""
                                ),
                                expected_execution_profile_hash=(
                                    target.execution_profile_hash
                                ),
                                expected_regime=target.coarse_regime,
                                expected_risk_policy_hash=(
                                    context.risk_policy_hash
                                ),
                                live_notional_cap_usdc=(
                                    context.notional_cap_usdc
                                ),
                                evidence_as_of_ms=evidence.as_of_ms,
                                event_time_ms=as_of_ms,
                                expires_at_ms=proposal.expires_at_ms,
                                hard_loss_marker=(
                                    evidence.hard_loss_marker
                                ),
                                idempotency_key=str(
                                    lease_apply.idempotency_key or ""
                                ).strip(),
                                actor=str(
                                    lease_apply.actor or ""
                                ).strip(),
                            )
                        )
                    else:
                        lease_mutation = (
                            await self._lease_repository.apply_proposal(
                                decision.winner,
                                proposal,
                                context,
                                expected_generation=(
                                    0
                                    if target is None
                                    else int(target.generation)
                                ),
                                expected_evidence_revision=(
                                    None
                                    if target is None
                                    else target.evidence_revision
                                ),
                                event_time_ms=as_of_ms,
                                idempotency_key=str(
                                    lease_apply.idempotency_key or ""
                                ).strip(),
                                actor=str(
                                    lease_apply.actor or ""
                                ).strip(),
                            )
                        )
                    reloaded = await self._lease_repository.get_active_lease(
                        environment=environment,
                        symbol=symbol,
                        now_ms=as_of_ms,
                    )
                    if reloaded is None or reloaded != lease_mutation.lease:
                        raise RuntimeError("lease_post_apply_mismatch")
                    authority_lease = reloaded
            except Exception as exc:
                return self._blocked_result(
                    (_exception_blocker("lease_apply_failed", exc),),
                    decision=decision,
                    arbiter_request=arbiter_request,
                    evidence_mapping=mapping,
                    current_opportunity=current_match,
                    durable_lease=authority_lease,
                    lease_mutation=lease_mutation,
                    ledger_row_count=row_count,
                    ledger_scope_complete=True,
                )
        elif proposal.action in {LeaseAction.GRANT, LeaseAction.RENEW}:
            return self._blocked_result(
                ("lease_proposal_not_applied",),
                decision=decision,
                arbiter_request=arbiter_request,
                evidence_mapping=mapping,
                current_opportunity=current_match,
                durable_lease=authority_lease,
                ledger_row_count=row_count,
                ledger_scope_complete=True,
            )

        lease_blockers = _lease_authority_blockers(
            authority_lease,
            winner=decision.winner,
            evidence_revision=decision.evidence_revision,
            environment=environment,
            symbol=symbol,
            as_of_ms=as_of_ms,
        )
        if lease_blockers:
            return self._blocked_result(
                lease_blockers,
                decision=decision,
                arbiter_request=arbiter_request,
                evidence_mapping=mapping,
                current_opportunity=current_match,
                durable_lease=authority_lease,
                lease_mutation=lease_mutation,
                ledger_row_count=row_count,
                ledger_scope_complete=True,
            )

        return AuthorityRuntimeResult(
            submit_admissible=True,
            blockers=(),
            decision=decision,
            arbiter_request=arbiter_request,
            evidence_mapping=mapping,
            current_opportunity=current_match,
            durable_lease=authority_lease,
            lease_mutation=lease_mutation,
            ledger_row_count=row_count,
            ledger_scope_complete=True,
            ledger_revision=mapping.ledger_revision,
        )


__all__ = [
    "AuthorityRuntimeInput",
    "AuthorityRuntimeResult",
    "CurrentOpportunityEligibility",
    "LeaseApplyRequest",
    "V1469AuthorityRuntime",
]
