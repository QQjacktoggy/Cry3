"""Atomic v1.4.69 authority, risk, plan, and paid-claim preparation."""

from __future__ import annotations

from dataclasses import dataclass

from src.gridbot.mainnet.v1469_adaptive_identity import (
    EXECUTION_PROFILE_SCHEMA,
    RiskPolicy,
)
from src.gridbot.mainnet.v1469_arm_arbiter import LeaseAction, LeasePhase
from src.gridbot.mainnet.v1469_arm_profiles import get_arm_profile
from src.gridbot.mainnet.v1469_authority_runtime import (
    AuthorityRuntimeInput,
    AuthorityRuntimeResult,
    LeaseApplyRequest,
    V1469AuthorityRuntime,
)
from src.gridbot.mainnet.v1469_execution_plan import (
    V1469PaidExecutionPlan,
    build_paid_execution_plan,
)
from src.gridbot.mainnet.v1469_risk_runtime import (
    V1469RiskAdmission,
    V1469RiskAdmissionRuntime,
)
from src.gridbot.storage.v1469_lease_repository import LeaseContext
from src.gridbot.storage.v1469_paid_execution_claim_repository import (
    DurablePaidExecutionClaim,
    V1469PaidExecutionClaimRepository,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PaidEntryPreparationRequest:
    authority_input: AuthorityRuntimeInput
    risk_policy: RiskPolicy
    desired_notional_usdc: float
    exchange_min_notional_usdc: float
    roundtrip_fee_bp: float
    slippage_bp: float
    probation_notional_usdc: float
    live_notional_usdc: float
    owner_id: str
    boot_id: str
    actor: str = "v1469-paid-entry-runtime"


@dataclass(frozen=True, slots=True)
class PaidEntryPreparation:
    authority: AuthorityRuntimeResult
    risk: V1469RiskAdmission
    plan: V1469PaidExecutionPlan
    claim: DurablePaidExecutionClaim


class V1469PaidEntryRuntime:
    """Prepare exactly one durable claim; this class never calls Binance."""

    def __init__(
        self,
        *,
        authority_runtime: V1469AuthorityRuntime,
        risk_runtime: V1469RiskAdmissionRuntime,
        claim_repository: V1469PaidExecutionClaimRepository,
    ) -> None:
        self._authority_runtime = authority_runtime
        self._risk_runtime = risk_runtime
        self._claim_repository = claim_repository

    async def prepare(
        self,
        request: PaidEntryPreparationRequest,
    ) -> PaidEntryPreparation:
        if not isinstance(request, PaidEntryPreparationRequest):
            raise TypeError("request must be PaidEntryPreparationRequest")
        authority_input = request.authority_input
        preview = await self._authority_runtime.evaluate(authority_input)
        decision = preview.decision
        proposal = decision.lease_proposal
        winner = decision.winner
        if winner is None:
            raise RuntimeError(
                "v1469 authority blocked: " + ",".join(preview.blockers)
            )

        authority = preview
        if proposal.action in {LeaseAction.GRANT, LeaseAction.RENEW}:
            if proposal.phase is LeasePhase.PROBATION:
                notional_cap = float(request.probation_notional_usdc)
            elif proposal.phase is LeasePhase.LIVE:
                notional_cap = float(request.live_notional_usdc)
            else:
                raise RuntimeError("v1469 lease proposal is not paid-capable")
            authority = await self._authority_runtime.evaluate(
                authority_input,
                lease_apply=LeaseApplyRequest(
                    context=LeaseContext(
                        environment=authority_input.environment,
                        symbol=authority_input.symbol,
                        execution_profile_schema=EXECUTION_PROFILE_SCHEMA,
                        notional_cap_usdc=notional_cap,
                        risk_policy_hash=request.risk_policy.policy_hash,
                        evidence_as_of_ms=authority_input.as_of_ms,
                        owner_id=str(request.owner_id),
                        boot_id=str(request.boot_id),
                    ),
                    idempotency_key=(
                        "v1469:lease:"
                        f"{authority_input.opportunity_id}:"
                        f"{decision.evidence_revision}:"
                        f"{proposal.action.value}"
                    ),
                    actor=str(request.actor),
                    expected_arm_key=winner.arm_key,
                    expected_evidence_revision=decision.evidence_revision,
                    expected_action=proposal.action,
                    expected_phase=proposal.phase,
                    expected_execution_profile_hash=(
                        winner.execution_profile_hash
                    ),
                    expected_regime=winner.regime,
                ),
            )
        if not authority.submit_admissible or authority.durable_lease is None:
            raise RuntimeError(
                "v1469 authority blocked: " + ",".join(authority.blockers)
            )

        winner = authority.decision.winner
        if winner is None:
            raise RuntimeError("v1469 final authority has no winner")
        profile = get_arm_profile(winner.execution_profile_id)
        execution = profile.execution_profile
        if execution is None:
            raise RuntimeError("v1469 winner has no paid execution profile")
        risk = await self._risk_runtime.evaluate(
            authority,
            desired_notional_usdc=float(request.desired_notional_usdc),
            sl_bp=float(execution.sl_bp),
            roundtrip_fee_bp=float(request.roundtrip_fee_bp),
            slippage_bp=float(request.slippage_bp),
            exchange_min_notional_usdc=float(
                request.exchange_min_notional_usdc
            ),
            policy=request.risk_policy,
            now_ms=authority_input.as_of_ms,
        )
        if not risk.allowed:
            raise RuntimeError(f"v1469 risk blocked: {risk.reason}")
        plan = build_paid_execution_plan(
            authority,
            approved_notional_usdc=risk.approved_notional_usdc,
        )
        lease = authority.durable_lease
        claim = (
            await self._claim_repository.claim(
                environment=authority_input.environment,
                symbol=authority_input.symbol,
                opportunity_id=authority_input.opportunity_id,
                arm_key=plan.arm_key,
                lease_id=plan.lease_id,
                claimed_at_ms=authority_input.as_of_ms,
                idempotency_key=(
                    f"v1469:claim:{authority_input.opportunity_id}"
                ),
                actor=str(request.actor),
                payload={"execution_plan": plan.to_payload()},
                expected_lease_generation=plan.lease_generation,
                expected_evidence_revision=plan.evidence_revision,
                expected_regime=plan.regime,
                expected_execution_profile_hash=(
                    plan.execution_profile_hash
                ),
                expected_risk_policy_hash=plan.risk_policy_hash,
                approved_notional_usdc=risk.approved_notional_usdc,
                reserved_loss_usdc=risk.reserved_loss_usdc,
                global_notional_cap_usdc=(
                    request.risk_policy.global_open_notional_cap_usdc
                ),
                lane_notional_cap_usdc=(
                    request.risk_policy.lane_open_notional_cap_usdc
                ),
                daily_reserved_loss_cap_usdc=(
                    risk.snapshot.remaining_daily_risk_usdc
                ),
            )
        ).claim
        return PaidEntryPreparation(
            authority=authority,
            risk=risk,
            plan=plan,
            claim=claim,
        )


__all__ = [
    "PaidEntryPreparation",
    "PaidEntryPreparationRequest",
    "V1469PaidEntryRuntime",
]
