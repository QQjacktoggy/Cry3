"""Deterministic repository-backed paid authority resolver (no exchange API)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from math import isfinite
from typing import Any

from .v1469_arm_arbiter import ArbiterDecision, ArmIdentity, LeaseAction, LeasePhase
from .v1469_legacy_control import LegacyExecutionSnapshot


class PaidAuthorityKind(str, Enum):
    LEGACY_CONTROL = "LEGACY_CONTROL"
    ADAPTIVE = "ADAPTIVE"
    BLOCK = "BLOCK"


@dataclass(frozen=True, slots=True, kw_only=True)
class LegacyPaidDecision:
    allowed: bool
    decision_payload: Any
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class PaidProbationEvidence:
    arm_key: str
    execution_profile_hash: str
    evidence_revision: str
    regime: str
    as_of_ms: int
    terminal_fills: int
    wins: int
    fee_net_paid_pnl: float
    hard_loss_marker: bool = False

    def __post_init__(self) -> None:
        for name in (
            "arm_key", "execution_profile_hash", "evidence_revision", "regime"
        ):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} is required")
        for name in ("as_of_ms", "terminal_fills", "wins"):
            value = getattr(self, name)
            if isinstance(value, bool) or int(value) < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if int(self.wins) > int(self.terminal_fills):
            raise ValueError("wins must not exceed terminal_fills")
        if (isinstance(self.fee_net_paid_pnl, bool)
                or not isfinite(float(self.fee_net_paid_pnl))):
            raise ValueError("fee_net_paid_pnl must be finite")
        if not isinstance(self.hard_loss_marker, bool):
            raise ValueError("hard_loss_marker must be a boolean")

    @property
    def live_ready(self) -> bool:
        return (self.terminal_fills >= 3 and self.wins >= 2
                and self.fee_net_paid_pnl > 0 and not self.hard_loss_marker)


@dataclass(frozen=True, slots=True, kw_only=True)
class PaidAuthorityDecision:
    kind: PaidAuthorityKind
    reason: str
    legacy_decision: LegacyPaidDecision
    arm: ArmIdentity | None = None
    lease_id: str | None = None
    execution_payload: dict[str, Any] | None = None


def resolve_paid_authority(*, legacy_snapshot: LegacyExecutionSnapshot,
                           legacy_decision: LegacyPaidDecision,
                           arbiter_decision: ArbiterDecision,
                           durable_lease: Any | None,
                           probation_evidence: PaidProbationEvidence | None,
                           daily_risk_snapshot: Any,
                           enforcement_enabled: bool,
                           now_ms: int, regime: str) -> PaidAuthorityDecision:
    """Return exactly one authority; missing or inconsistent state fails safe."""
    fallback = PaidAuthorityDecision(
        kind=(PaidAuthorityKind.LEGACY_CONTROL if legacy_decision.allowed
              else PaidAuthorityKind.BLOCK),
        reason="legacy_equivalence" if legacy_decision.allowed else "legacy_blocked",
        legacy_decision=legacy_decision,
        execution_payload=(legacy_snapshot.submit_authority_payload()
                           if legacy_decision.allowed else None))
    if not enforcement_enabled:
        return fallback
    winner = arbiter_decision.winner
    risk_age = int(now_ms) - int(getattr(daily_risk_snapshot, "as_of_ms", -1))
    risk_ok = bool(daily_risk_snapshot is not None
                   and getattr(daily_risk_snapshot, "data_valid", False)
                   and not getattr(daily_risk_snapshot, "entry_blocked", True)
                   and 0 <= risk_age <= 10_000)
    valid = bool(winner is not None and durable_lease is not None and risk_ok)
    if valid:
        valid = (getattr(durable_lease, "status", None) == "ACTIVE"
                 and int(getattr(durable_lease, "expires_at_ms", 0)) > int(now_ms)
                 and getattr(durable_lease, "arm_key", None) == winner.arm_key
                 and getattr(durable_lease, "coarse_regime", "").upper() == regime.upper()
                 and getattr(durable_lease, "execution_profile_hash", None) == winner.execution_profile_hash
                 and getattr(durable_lease, "evidence_revision", None) == arbiter_decision.evidence_revision)
    if valid and getattr(durable_lease, "phase", None) == LeasePhase.LIVE:
        evidence = probation_evidence
        valid = bool(evidence is not None and evidence.live_ready
                     and evidence.arm_key == winner.arm_key
                     and evidence.execution_profile_hash == winner.execution_profile_hash
                     and evidence.evidence_revision == arbiter_decision.evidence_revision
                     and evidence.regime.upper() == regime.upper()
                     and int(now_ms) - evidence.as_of_ms <= 10_000)
    if not valid:
        # Once v1.4.69 enforcement is explicitly enabled, the only entry
        # authority is an exact, fresh adaptive lease.  Falling back to the
        # legacy decision here would bypass the paid-claim contract and could
        # create an order without the lease/risk snapshot that enforcement is
        # meant to require.
        return PaidAuthorityDecision(
            kind=PaidAuthorityKind.BLOCK,
            reason="adaptive_authority_invalid_block",
            legacy_decision=legacy_decision,
        )
    return PaidAuthorityDecision(kind=PaidAuthorityKind.ADAPTIVE,
        reason="exact_active_lease", legacy_decision=legacy_decision,
        arm=winner, lease_id=str(durable_lease.lease_id),
        execution_payload={"execution_profile_hash": winner.execution_profile_hash,
                           "risk_policy_hash": str(durable_lease.risk_policy_hash),
                           "notional_cap_usdc": float(durable_lease.notional_cap_usdc)})


def apply_automatic_live_phase(decision: ArbiterDecision,
                               evidence: PaidProbationEvidence | None,
                               *, now_ms: int,
                               live_lease_ms: int = 10 * 60 * 1000) -> ArbiterDecision:
    """Promote only a matching probation proposal; never uses calendar dates."""
    proposal = decision.lease_proposal
    if (decision.winner is None or evidence is None or not evidence.live_ready
            or proposal.phase is not LeasePhase.PROBATION
            or proposal.action not in {LeaseAction.KEEP, LeaseAction.RENEW}
            or evidence.arm_key != decision.winner.arm_key
            or evidence.execution_profile_hash != decision.winner.execution_profile_hash
            or evidence.evidence_revision != decision.evidence_revision
            or evidence.regime.upper() != decision.winner.regime.upper()):
        return decision
    age = int(now_ms) - int(evidence.as_of_ms)
    if age < 0 or age > 10_000:
        return decision
    duration = int(live_lease_ms)
    if duration <= 0:
        raise ValueError("live_lease_ms must be positive")
    return replace(decision, lease_proposal=replace(
        proposal, action=LeaseAction.RENEW, phase=LeasePhase.LIVE,
        expires_at_ms=int(now_ms) + duration,
    ))


__all__ = ["LegacyPaidDecision", "PaidAuthorityDecision", "PaidAuthorityKind",
           "PaidProbationEvidence", "apply_automatic_live_phase",
           "resolve_paid_authority"]
