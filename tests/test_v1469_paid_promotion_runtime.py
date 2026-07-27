from __future__ import annotations

import pytest

from src.gridbot.mainnet.v1469_arm_arbiter import (
    ArbiterDecision,
    ArmIdentity,
    LeaseAction,
    LeasePhase,
    LeaseProposal,
)
from src.gridbot.mainnet.v1469_paid_promotion_runtime import (
    V1469PaidPromotionRuntime,
)


NOW = 10_000


def _decision() -> ArbiterDecision:
    arm = ArmIdentity(
        arm_key="v1469a_" + "a" * 64,
        lane_code="W6A",
        side="LONG",
        strategy="TEST",
        regime="RANGE",
        execution_profile_id="RANGE_SCALP",
        execution_profile_hash="b" * 64,
    )
    return ArbiterDecision(
        winner=arm,
        blockers=(),
        evaluations=(),
        evidence_revision="revision-current",
        lease_proposal=LeaseProposal(
            action=LeaseAction.RENEW,
            arm_key=arm.arm_key,
            phase=LeasePhase.PROBATION,
            evidence_revision="revision-current",
            expires_at_ms=NOW + 300_000,
        ),
        revocations=(),
    )


class _Source:
    def __init__(self, row=None, error: Exception | None = None) -> None:
        self.row = {
            "evidence_snapshot_durable": True,
            "evidence_watermark": "d" * 64,
            **(row or {}),
        }
        self.error = error
        self.calls = []

    async def load_paid_probation_evidence(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.row


@pytest.mark.asyncio
async def test_three_fills_two_wins_positive_net_promotes_immediately() -> None:
    source = _Source(
        {
            "terminal_fills": 3,
            "wins": 2,
            "fee_net_paid_pnl": 0.03,
            "hard_loss_marker": False,
        }
    )
    result = await V1469PaidPromotionRuntime(
        source, evidence_window_ms=45 * 60 * 1000
    ).evaluate(
        _decision(),
        environment="mainnet",
        symbol="btcusdc",
        now_ms=NOW,
        live_lease_ms=600_000,
    )

    assert result.promoted is True
    assert result.decision.lease_proposal.action is LeaseAction.RENEW
    assert result.decision.lease_proposal.phase is LeasePhase.LIVE
    assert result.decision.lease_proposal.expires_at_ms == NOW + 600_000
    assert result.evidence is not None
    assert result.evidence.evidence_revision == "revision-current"
    assert source.calls[0]["window_start_ms"] == 0
    assert source.calls[0]["evidence_revision"] == "revision-current"


@pytest.mark.asyncio
async def test_insufficient_or_hard_loss_evidence_stays_probation() -> None:
    source = _Source(
        {
            "terminal_fills": 3,
            "wins": 2,
            "fee_net_paid_pnl": 0.03,
            "hard_loss_marker": True,
        }
    )
    decision = _decision()
    result = await V1469PaidPromotionRuntime(
        source, evidence_window_ms=180_000
    ).evaluate(
        decision,
        environment="MAINNET",
        symbol="BTCUSDC",
        now_ms=NOW,
        live_lease_ms=600_000,
    )
    assert result.promoted is False
    assert result.decision == decision


@pytest.mark.asyncio
async def test_repository_failure_is_fail_closed() -> None:
    decision = _decision()
    result = await V1469PaidPromotionRuntime(
        _Source(error=RuntimeError("db unavailable")),
        evidence_window_ms=180_000,
    ).evaluate(
        decision,
        environment="MAINNET",
        symbol="BTCUSDC",
        now_ms=NOW,
        live_lease_ms=600_000,
    )
    assert result.promoted is False
    assert result.decision == decision
    assert result.blockers == (
        "promotion_evidence_unavailable:RuntimeError",
    )
