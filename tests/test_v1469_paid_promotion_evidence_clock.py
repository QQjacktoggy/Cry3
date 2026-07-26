from __future__ import annotations

from pathlib import Path

import pytest

from src.gridbot.mainnet.v1469_arm_arbiter import LeasePhase
from src.gridbot.storage.database import Database
from src.gridbot.storage.v1469_lease_repository import (
    V1469LeaseConflictError,
    V1469LeaseRepository,
)
from src.gridbot.storage.v1469_paid_execution_claim_repository import (
    V1469PaidExecutionClaimRepository,
)
from tests.test_v1469_paid_execution_claim_authority_contract import (
    PROFILE_HASH,
    RISK_HASH,
    _claim_kwargs,
)
from tests.test_v1469_paid_execution_claim_repository import (
    ARM_KEY,
    ENVIRONMENT,
    LEASE_ID,
    SYMBOL,
    _seed_opportunities_and_active_lease,
    _transition_claim_to_submitted,
)


async def _terminal_fill(
    claims: V1469PaidExecutionClaimRepository,
    opportunity_id: str,
    *,
    claimed_at_ms: int,
    terminal_at_ms: int,
    pnl_usdc: float,
) -> None:
    claim = (
        await claims.claim(
            **_claim_kwargs(opportunity_id, claimed_at_ms=claimed_at_ms)
        )
    ).claim
    claim = await _transition_claim_to_submitted(
        claims,
        claim,
        submitting_at_ms=claimed_at_ms + 10,
        submitted_at_ms=claimed_at_ms + 20,
        key=f"clock-{opportunity_id}",
    )
    await claims.terminalize_claim(
        claim_id=claim.claim_id,
        expected_generation=claim.generation,
        terminal_at_ms=terminal_at_ms,
        terminal_reason="PAID_POSITION_CLOSED",
        idempotency_key=f"terminal:clock:{opportunity_id}",
        actor="promotion-clock-test",
        result_payload={
            "fee_net_pnl_usdc": pnl_usdc,
            "hard_loss_marker": False,
        },
    )


@pytest.mark.asyncio
async def test_new_no_fill_terminal_invalidates_prepared_live_promotion(
    tmp_path: Path,
) -> None:
    db = Database(str(tmp_path / "promotion-clock.db"))
    await db.initialize()
    claims = V1469PaidExecutionClaimRepository(db)
    leases = V1469LeaseRepository(db)
    await _seed_opportunities_and_active_lease(
        db, "fill-1", "fill-2", "fill-3", "no-fill-race"
    )
    try:
        await _terminal_fill(
            claims,
            "fill-1",
            claimed_at_ms=1_100,
            terminal_at_ms=1_200,
            pnl_usdc=0.02,
        )
        await _terminal_fill(
            claims,
            "fill-2",
            claimed_at_ms=1_300,
            terminal_at_ms=1_400,
            pnl_usdc=-0.01,
        )
        await _terminal_fill(
            claims,
            "fill-3",
            claimed_at_ms=1_500,
            terminal_at_ms=1_600,
            pnl_usdc=0.02,
        )
        evidence = await claims.load_paid_probation_evidence(
            environment=ENVIRONMENT,
            symbol=SYMBOL,
            arm_key=ARM_KEY,
            execution_profile_hash=PROFILE_HASH,
            regime="RANGE",
            window_start_ms=1_000,
            as_of_ms=1_600,
            limit=100,
            evidence_revision="promotion-revision",
        )
        assert evidence["terminal_fills"] == 3
        assert evidence["wins"] == 2
        assert evidence["fee_net_paid_pnl"] == pytest.approx(0.03)
        assert evidence["evidence_snapshot_durable"] is True
        assert evidence["evidence_clock_revision"] == 3

        no_fill = (
            await claims.claim(
                **_claim_kwargs("no-fill-race", claimed_at_ms=1_650)
            )
        ).claim
        no_fill = await _transition_claim_to_submitted(
            claims,
            no_fill,
            submitting_at_ms=1_675,
            submitted_at_ms=1_700,
            key="clock-no-fill-race",
        )
        await claims.terminalize_claim(
            claim_id=no_fill.claim_id,
            expected_generation=no_fill.generation,
            terminal_at_ms=1_750,
            terminal_reason="ENTRY_EXPIRED",
            idempotency_key="terminal:clock:no-fill-race",
            actor="promotion-clock-test",
            result_payload={
                "schema": "v1469.paid-no-fill.1",
                "outcome": "NO_FILL",
                "source_run_id": "run-no-fill-race",
            },
        )
        clock = await db.fetchone(
            """SELECT revision, terminal_count
            FROM v1469_paid_terminal_evidence_clocks
            WHERE environment = ? AND symbol = ? AND arm_key = ?
              AND execution_profile_hash = ? AND regime = 'RANGE'""",
            (ENVIRONMENT, SYMBOL, ARM_KEY, PROFILE_HASH),
        )
        assert clock == {"revision": 4, "terminal_count": 4}

        with pytest.raises(
            V1469LeaseConflictError,
            match="promotion_evidence_watermark_changed",
        ):
            await leases.promote_probation_to_live(
                environment=ENVIRONMENT,
                symbol=SYMBOL,
                arm_key=ARM_KEY,
                lease_id=LEASE_ID,
                expected_generation=1,
                expected_evidence_revision="revision-1",
                new_evidence_revision="promotion-revision",
                expected_execution_profile_hash=PROFILE_HASH,
                expected_regime="RANGE",
                expected_risk_policy_hash=RISK_HASH,
                live_notional_cap_usdc=10.0,
                evidence_as_of_ms=1_600,
                event_time_ms=1_800,
                expires_at_ms=20_000,
                hard_loss_marker=False,
                idempotency_key="promotion-after-no-fill-race",
                actor="promotion-clock-test",
            )
        lease = await leases.get_lease(ARM_KEY)
        assert lease is not None
        assert lease.phase is LeasePhase.PROBATION
        assert lease.generation == 1
    finally:
        await db.close()