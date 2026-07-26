"""Repository-backed automatic PROBATION to LIVE promotion for v1.4.69."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .v1469_arm_arbiter import ArbiterDecision
from .v1469_paid_authority import (
    PaidProbationEvidence,
    apply_automatic_live_phase,
)


class PaidProbationEvidenceSource(Protocol):
    async def load_paid_probation_evidence(
        self,
        *,
        environment: str,
        symbol: str,
        arm_key: str,
        execution_profile_hash: str,
        regime: str,
        window_start_ms: int,
        as_of_ms: int,
        limit: int,
        evidence_revision: str,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class PaidPromotionResult:
    decision: ArbiterDecision
    evidence: PaidProbationEvidence | None
    promoted: bool
    blockers: tuple[str, ...] = ()


class V1469PaidPromotionRuntime:
    """Derive current exact evidence and promote without calendar-day waits."""

    def __init__(
        self,
        source: PaidProbationEvidenceSource,
        *,
        evidence_window_ms: int,
        evidence_limit: int = 100,
    ) -> None:
        if evidence_window_ms <= 0:
            raise ValueError("evidence_window_ms must be positive")
        if evidence_limit < 1 or evidence_limit > 1000:
            raise ValueError("evidence_limit must be from 1 to 1000")
        self._source = source
        self._evidence_window_ms = int(evidence_window_ms)
        self._evidence_limit = int(evidence_limit)

    async def evaluate(
        self,
        decision: ArbiterDecision,
        *,
        environment: str,
        symbol: str,
        now_ms: int,
        live_lease_ms: int,
    ) -> PaidPromotionResult:
        winner = decision.winner
        revision = str(decision.evidence_revision or "").strip()
        if winner is None or not revision:
            return PaidPromotionResult(
                decision=decision,
                evidence=None,
                promoted=False,
                blockers=("promotion_winner_or_revision_missing",),
            )
        try:
            row = await self._source.load_paid_probation_evidence(
                environment=str(environment).strip().upper(),
                symbol=str(symbol).strip().upper(),
                arm_key=winner.arm_key,
                execution_profile_hash=winner.execution_profile_hash,
                regime=winner.regime,
                window_start_ms=max(0, int(now_ms) - self._evidence_window_ms),
                as_of_ms=int(now_ms),
                limit=self._evidence_limit,
                evidence_revision=revision,
            )
            watermark = str(row.get("evidence_watermark") or "").strip()
            if (
                row.get("evidence_snapshot_durable") is not True
                or len(watermark) != 64
            ):
                raise ValueError("durable paid evidence snapshot is missing")
            evidence = PaidProbationEvidence(
                arm_key=winner.arm_key,
                execution_profile_hash=winner.execution_profile_hash,
                evidence_revision=revision,
                regime=winner.regime,
                as_of_ms=int(now_ms),
                terminal_fills=int(row.get("terminal_fills") or 0),
                wins=int(row.get("wins") or 0),
                fee_net_paid_pnl=float(row.get("fee_net_paid_pnl") or 0.0),
                hard_loss_marker=bool(row.get("hard_loss_marker", False)),
            )
        except Exception as exc:  # fail closed; never synthesize LIVE evidence
            return PaidPromotionResult(
                decision=decision,
                evidence=None,
                promoted=False,
                blockers=(
                    f"promotion_evidence_unavailable:{type(exc).__name__}",
                ),
            )
        promoted = apply_automatic_live_phase(
            decision,
            evidence,
            now_ms=int(now_ms),
            live_lease_ms=int(live_lease_ms),
        )
        return PaidPromotionResult(
            decision=promoted,
            evidence=evidence,
            promoted=promoted != decision,
        )


__all__ = [
    "PaidProbationEvidenceSource",
    "PaidPromotionResult",
    "V1469PaidPromotionRuntime",
]
