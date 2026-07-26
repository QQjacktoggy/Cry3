from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3

import pytest

from src.gridbot.storage.database import Database
from src.gridbot.storage.v1469_paid_execution_claim_repository import (
    DurablePaidExecutionClaim,
    V1469PaidClaimConflictError,
    V1469PaidExecutionClaimRepository,
)


ENVIRONMENT = "MAINNET"
SYMBOL = "BTCUSDC"
ARM_KEY = "v1469a_" + ("a" * 64)
LEASE_ID = "v1469l_" + ("b" * 64)


async def _seed_opportunities_and_active_lease(
    db: Database,
    *opportunity_ids: str,
) -> None:
    for index, opportunity_id in enumerate(opportunity_ids):
        observed_at_ms = 1_000 + index
        await db.conn.execute(
            """INSERT INTO v1469_market_opportunities (
                opportunity_id, environment, symbol, observed_at_ms,
                feature_at_ms, coarse_regime, regime_confidence,
                feature_schema, feature_hash, feature_snapshot_json,
                source_run_id, source_event_id, data_quality, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                opportunity_id,
                ENVIRONMENT,
                SYMBOL,
                observed_at_ms,
                observed_at_ms,
                "RANGE",
                0.9,
                "v1469.test.features.1",
                f"feature-{opportunity_id}",
                "{}",
                "run-1",
                f"event-{opportunity_id}",
                "COMPLETE",
                observed_at_ms,
            ),
        )
    await db.conn.execute(
        """INSERT INTO v1469_arm_leases (
            arm_key, lease_id, generation, environment, symbol, lane_code,
            effective_side, strategy, coarse_regime,
            execution_profile_id, execution_profile_schema,
            execution_profile_hash, phase, status, notional_cap_usdc,
            risk_policy_hash, evidence_revision, evidence_as_of_ms,
            issued_at_ms, renewed_at_ms, expires_at_ms, owner_id, boot_id,
            demotion_reason, demoted_at_ms, cooldown_until_ms,
            created_at_ms, updated_at_ms
        ) VALUES (
            ?, ?, 1, ?, ?, 'W6A', 'LONG', 'TEST', 'RANGE',
            'RANGE_SCALP', 'v1469.execution-profile.1', ?,
            'PROBATION', 'ACTIVE', 10.0, ?, 'revision-1', 1000,
            1000, 1000, 100000, 'owner-1', 'boot-1',
            NULL, NULL, NULL, 1000, 1000
        )""",
        (
            ARM_KEY,
            LEASE_ID,
            ENVIRONMENT,
            SYMBOL,
            "c" * 64,
            "d" * 64,
        ),
    )
    await db.conn.commit()


async def _transition_claim_to_submitted(
    repo: V1469PaidExecutionClaimRepository,
    claim: DurablePaidExecutionClaim,
    *,
    submitting_at_ms: int,
    submitted_at_ms: int,
    key: str,
) -> DurablePaidExecutionClaim:
    submitting = await repo.transition_submission(
        claim_id=claim.claim_id,
        expected_generation=claim.generation,
        target_status="SUBMITTING",
        transition_at_ms=submitting_at_ms,
        idempotency_key=f"submitting:{key}",
        actor="test",
        payload={"client_order_id": f"cid-{key}"},
    )
    submitted = await repo.transition_submission(
        claim_id=claim.claim_id,
        expected_generation=submitting.claim.generation,
        target_status="SUBMITTED",
        transition_at_ms=submitted_at_ms,
        idempotency_key=f"submitted:{key}",
        actor="test",
        payload={"client_order_id": f"cid-{key}"},
    )
    return submitted.claim


@pytest.mark.asyncio
async def test_paid_claim_is_unique_idempotent_and_survives_restart(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "paid-claim.db"
    db = Database(str(db_path))
    await db.initialize()
    repo = V1469PaidExecutionClaimRepository(db)
    await repo.assert_schema_ready()
    await _seed_opportunities_and_active_lease(db, "opp-1")

    first = await repo.claim(
        environment=ENVIRONMENT,
        symbol=SYMBOL,
        opportunity_id="opp-1",
        arm_key=ARM_KEY,
        lease_id=LEASE_ID,
        claimed_at_ms=1_100,
        idempotency_key="claim:opp-1",
        actor="test",
        payload={"source": "unit-test"},
    )
    assert first.applied is True
    assert first.replayed is False
    assert first.claim.status == "CLAIMED"
    assert first.claim.generation == 1

    retry = await repo.claim(
        environment=ENVIRONMENT.lower(),
        symbol=SYMBOL.lower(),
        opportunity_id="opp-1",
        arm_key=ARM_KEY,
        lease_id=LEASE_ID,
        claimed_at_ms=1_101,
        idempotency_key="claim:opp-1:retry",
        actor="retry",
        payload={"source": "retry"},
    )
    assert retry.applied is False
    assert retry.replayed is True
    assert retry.claim == first.claim

    with pytest.raises(
        V1469PaidClaimConflictError,
        match="different arm or lease",
    ):
        await repo.claim(
            environment=ENVIRONMENT,
            symbol=SYMBOL,
            opportunity_id="opp-1",
            arm_key="v1469a_" + ("e" * 64),
            lease_id=LEASE_ID,
            claimed_at_ms=1_102,
            idempotency_key="claim:opp-1:wrong-arm",
            actor="test",
        )

    counts = await db.fetchone(
        """SELECT
            (SELECT COUNT(*) FROM v1469_paid_execution_claims)
                AS claim_count,
            (SELECT COUNT(*) FROM v1469_paid_execution_claim_events)
                AS event_count"""
    )
    assert counts == {"claim_count": 1, "event_count": 1}
    await db.close()

    restarted_db = Database(str(db_path))
    await restarted_db.initialize()
    restarted_repo = V1469PaidExecutionClaimRepository(restarted_db)
    try:
        durable = await restarted_repo.get_claim(
            environment=ENVIRONMENT,
            symbol=SYMBOL,
            opportunity_id="opp-1",
        )
        assert durable == first.claim
        assert (
            await restarted_repo.get_claim_by_id(first.claim.claim_id)
            == first.claim
        )
    finally:
        await restarted_db.close()


@pytest.mark.asyncio
async def test_paid_claim_terminal_and_abandon_are_cas_and_audited(
    tmp_path: Path,
) -> None:
    db = Database(str(tmp_path / "paid-claim-terminal.db"))
    await db.initialize()
    repo = V1469PaidExecutionClaimRepository(db)
    await _seed_opportunities_and_active_lease(db, "opp-1", "opp-2")
    try:
        first = await repo.claim(
            environment=ENVIRONMENT,
            symbol=SYMBOL,
            opportunity_id="opp-1",
            arm_key=ARM_KEY,
            lease_id=LEASE_ID,
            claimed_at_ms=1_100,
            idempotency_key="claim:opp-1",
            actor="test",
        )
        second = await repo.claim(
            environment=ENVIRONMENT,
            symbol=SYMBOL,
            opportunity_id="opp-2",
            arm_key=ARM_KEY,
            lease_id=LEASE_ID,
            claimed_at_ms=1_101,
            idempotency_key="claim:opp-2",
            actor="test",
        )

        first_submitted = await _transition_claim_to_submitted(
            repo,
            first.claim,
            submitting_at_ms=1_200,
            submitted_at_ms=1_300,
            key="opp-1",
        )
        terminal = await repo.terminalize_claim(
            claim_id=first.claim.claim_id,
            expected_generation=first_submitted.generation,
            terminal_at_ms=2_000,
            terminal_reason="PAID_POSITION_CLOSED",
            idempotency_key="terminal:opp-1",
            actor="test",
            result_payload={"fee_net_pnl_usdc": 0.12},
        )
        assert terminal.applied is True
        assert terminal.claim.status == "TERMINAL"
        assert terminal.claim.generation == 4
        assert terminal.claim.result_payload == {
            "fee_net_pnl_usdc": 0.12
        }

        terminal_retry = await repo.terminalize_claim(
            claim_id=first.claim.claim_id,
            expected_generation=first_submitted.generation,
            terminal_at_ms=2_000,
            terminal_reason="PAID_POSITION_CLOSED",
            idempotency_key="terminal:opp-1:retry",
            actor="retry",
            result_payload={"fee_net_pnl_usdc": 0.12},
        )
        assert terminal_retry.applied is False
        assert terminal_retry.replayed is True
        assert terminal_retry.claim == terminal.claim

        with pytest.raises(
            V1469PaidClaimConflictError,
            match="already terminal",
        ):
            await repo.abandon_claim(
                claim_id=first.claim.claim_id,
                expected_generation=1,
                abandoned_at_ms=2_000,
                terminal_reason="NOT_SUBMITTED",
                idempotency_key="abandon:opp-1",
                actor="test",
            )

        with pytest.raises(
            V1469PaidClaimConflictError,
            match="generation changed",
        ):
            await repo.abandon_claim(
                claim_id=second.claim.claim_id,
                expected_generation=2,
                abandoned_at_ms=2_001,
                terminal_reason="ENTRY_EXPIRED",
                idempotency_key="abandon:opp-2:stale",
                actor="test",
            )

        abandoned = await repo.abandon_claim(
            claim_id=second.claim.claim_id,
            expected_generation=1,
            abandoned_at_ms=2_001,
            terminal_reason="ENTRY_EXPIRED",
            idempotency_key="abandon:opp-2",
            actor="test",
            result_payload={"submitted": False},
        )
        assert abandoned.claim.status == "ABANDONED"
        assert abandoned.claim.generation == 2

        events = await db.fetchall(
            """SELECT event_type, generation_before, generation_after
            FROM v1469_paid_execution_claim_events
            ORDER BY id"""
        )
        assert events == [
            {
                "event_type": "CLAIMED",
                "generation_before": 0,
                "generation_after": 1,
            },
            {
                "event_type": "CLAIMED",
                "generation_before": 0,
                "generation_after": 1,
            },
            {
                "event_type": "SUBMITTING",
                "generation_before": 1,
                "generation_after": 2,
            },
            {
                "event_type": "SUBMITTED",
                "generation_before": 2,
                "generation_after": 3,
            },
            {
                "event_type": "TERMINAL",
                "generation_before": 3,
                "generation_after": 4,
            },
            {
                "event_type": "ABANDONED",
                "generation_before": 1,
                "generation_after": 2,
            },
        ]

        with pytest.raises(sqlite3.IntegrityError, match="identity mismatch"):
            await db.conn.execute(
                """INSERT INTO v1469_paid_execution_claim_events (
                    idempotency_key, claim_id, opportunity_id, arm_key,
                    lease_id, generation_before, generation_after,
                    event_time_ms, event_type, actor, payload_json
                ) VALUES (?, ?, ?, ?, ?, 1, 2, 2000, 'TERMINAL',
                          'tamper', '{}')""",
                (
                    "tampered-identity",
                    first.claim.claim_id,
                    "opp-2",
                    ARM_KEY,
                    LEASE_ID,
                ),
            )
        await db.conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            await db.conn.execute(
                """UPDATE v1469_paid_execution_claim_events
                SET actor = 'tampered' WHERE id = 1"""
            )
        await db.conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            await db.conn.execute(
                """DELETE FROM v1469_paid_execution_claims
                WHERE claim_id = ?""",
                (first.claim.claim_id,),
            )
    finally:
        await db.conn.rollback()
        await db.close()


@pytest.mark.asyncio
async def test_terminal_and_abandon_require_unambiguous_lifecycle_states(
    tmp_path: Path,
) -> None:
    db = Database(str(tmp_path / "paid-claim-lifecycle-guards.db"))
    await db.initialize()
    repo = V1469PaidExecutionClaimRepository(db)
    await _seed_opportunities_and_active_lease(
        db,
        "claimed-terminal",
        "unknown-abandon",
        "submitted-abandon",
    )
    try:
        claimed = (
            await repo.claim(
                environment=ENVIRONMENT,
                symbol=SYMBOL,
                opportunity_id="claimed-terminal",
                arm_key=ARM_KEY,
                lease_id=LEASE_ID,
                claimed_at_ms=1_100,
                idempotency_key="claim:claimed-terminal",
                actor="test",
            )
        ).claim
        with pytest.raises(
            V1469PaidClaimConflictError,
            match="must be SUBMITTED before TERMINAL",
        ):
            await repo.terminalize_claim(
                claim_id=claimed.claim_id,
                expected_generation=1,
                terminal_at_ms=1_500,
                terminal_reason="INVALID_CLOSE",
                idempotency_key="terminal:claimed-terminal",
                actor="test",
                result_payload={"fee_net_pnl_usdc": 0.0},
            )

        unknown = (
            await repo.claim(
                environment=ENVIRONMENT,
                symbol=SYMBOL,
                opportunity_id="unknown-abandon",
                arm_key=ARM_KEY,
                lease_id=LEASE_ID,
                claimed_at_ms=1_101,
                idempotency_key="claim:unknown-abandon",
                actor="test",
            )
        ).claim
        unknown = (
            await repo.transition_submission(
                claim_id=unknown.claim_id,
                expected_generation=1,
                target_status="SUBMITTING",
                transition_at_ms=1_200,
                idempotency_key="submitting:unknown-abandon",
                actor="test",
                payload={"client_order_id": "cid-unknown"},
            )
        ).claim
        unknown = (
            await repo.transition_submission(
                claim_id=unknown.claim_id,
                expected_generation=2,
                target_status="UNKNOWN",
                transition_at_ms=1_300,
                idempotency_key="unknown:unknown-abandon",
                actor="test",
                payload={"client_order_id": "cid-unknown"},
            )
        ).claim
        with pytest.raises(
            V1469PaidClaimConflictError,
            match="only unambiguous unsubmitted claim",
        ):
            await repo.abandon_claim(
                claim_id=unknown.claim_id,
                expected_generation=unknown.generation,
                abandoned_at_ms=1_500,
                terminal_reason="UNSAFE_RELEASE",
                idempotency_key="abandon:unknown",
                actor="test",
            )

        submitted_seed = (
            await repo.claim(
                environment=ENVIRONMENT,
                symbol=SYMBOL,
                opportunity_id="submitted-abandon",
                arm_key=ARM_KEY,
                lease_id=LEASE_ID,
                claimed_at_ms=1_102,
                idempotency_key="claim:submitted-abandon",
                actor="test",
            )
        ).claim
        submitted = await _transition_claim_to_submitted(
            repo,
            submitted_seed,
            submitting_at_ms=1_201,
            submitted_at_ms=1_301,
            key="submitted-abandon",
        )
        with pytest.raises(
            V1469PaidClaimConflictError,
            match="only unambiguous unsubmitted claim",
        ):
            await repo.abandon_claim(
                claim_id=submitted.claim_id,
                expected_generation=submitted.generation,
                abandoned_at_ms=1_500,
                terminal_reason="UNSAFE_RELEASE",
                idempotency_key="abandon:submitted",
                actor="test",
            )

        claimed_after = await repo.get_claim_by_id(claimed.claim_id)
        unknown_after = await repo.get_claim_by_id(unknown.claim_id)
        submitted_after = await repo.get_claim_by_id(submitted.claim_id)
        assert claimed_after is not None and claimed_after.status == "CLAIMED"
        assert unknown_after is not None and unknown_after.status == "UNKNOWN"
        assert submitted_after is not None and submitted_after.status == "SUBMITTED"
    finally:
        await db.close()

@pytest.mark.asyncio
async def test_paid_claim_audit_failure_rolls_back_claim_and_terminal_cas(
    tmp_path: Path,
) -> None:
    db = Database(str(tmp_path / "paid-claim-rollback.db"))
    await db.initialize()
    repo = V1469PaidExecutionClaimRepository(db)
    await _seed_opportunities_and_active_lease(db, "opp-1", "opp-2")
    try:
        first = await repo.claim(
            environment=ENVIRONMENT,
            symbol=SYMBOL,
            opportunity_id="opp-1",
            arm_key=ARM_KEY,
            lease_id=LEASE_ID,
            claimed_at_ms=1_100,
            idempotency_key="shared-audit-key",
            actor="test",
        )
        first_submitted = await _transition_claim_to_submitted(
            repo,
            first.claim,
            submitting_at_ms=1_200,
            submitted_at_ms=1_300,
            key="audit-opp-1",
        )

        with pytest.raises(
            V1469PaidClaimConflictError,
            match="UNIQUE constraint",
        ):
            await repo.terminalize_claim(
                claim_id=first.claim.claim_id,
                expected_generation=first_submitted.generation,
                terminal_at_ms=2_000,
                terminal_reason="CLOSED",
                idempotency_key="shared-audit-key",
                actor="test",
            )
        after_failed_terminal = await repo.get_claim_by_id(
            first.claim.claim_id
        )
        assert after_failed_terminal is not None
        assert after_failed_terminal.status == "SUBMITTED"
        assert after_failed_terminal.generation == 3

        with pytest.raises(
            V1469PaidClaimConflictError,
            match="UNIQUE constraint",
        ):
            await repo.claim(
                environment=ENVIRONMENT,
                symbol=SYMBOL,
                opportunity_id="opp-2",
                arm_key=ARM_KEY,
                lease_id=LEASE_ID,
                claimed_at_ms=1_101,
                idempotency_key="shared-audit-key",
                actor="test",
            )
        assert (
            await repo.get_claim(
                environment=ENVIRONMENT,
                symbol=SYMBOL,
                opportunity_id="opp-2",
            )
            is None
        )
        counts = await db.fetchone(
            """SELECT
                (SELECT COUNT(*) FROM v1469_paid_execution_claims)
                    AS claim_count,
                (SELECT COUNT(*) FROM v1469_paid_execution_claim_events)
                    AS event_count"""
        )
        assert counts == {"claim_count": 1, "event_count": 3}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_concurrent_repository_instances_choose_one_claim_winner(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "paid-claim-concurrent.db"
    first_db = Database(str(db_path))
    await first_db.initialize()
    await _seed_opportunities_and_active_lease(first_db, "opp-1")
    second_db = Database(str(db_path))
    await second_db.initialize()
    first_repo = V1469PaidExecutionClaimRepository(first_db)
    second_repo = V1469PaidExecutionClaimRepository(second_db)
    try:
        results = await asyncio.gather(
            first_repo.claim(
                environment=ENVIRONMENT,
                symbol=SYMBOL,
                opportunity_id="opp-1",
                arm_key=ARM_KEY,
                lease_id=LEASE_ID,
                claimed_at_ms=1_100,
                idempotency_key="concurrent:first",
                actor="first",
            ),
            second_repo.claim(
                environment=ENVIRONMENT,
                symbol=SYMBOL,
                opportunity_id="opp-1",
                arm_key=ARM_KEY,
                lease_id=LEASE_ID,
                claimed_at_ms=1_100,
                idempotency_key="concurrent:second",
                actor="second",
            ),
        )
        assert sum(result.applied for result in results) == 1
        assert sum(result.replayed for result in results) == 1
        assert results[0].claim == results[1].claim
        counts = await first_db.fetchone(
            """SELECT
                (SELECT COUNT(*) FROM v1469_paid_execution_claims)
                    AS claim_count,
                (SELECT COUNT(*) FROM v1469_paid_execution_claim_events)
                    AS event_count"""
        )
        assert counts == {"claim_count": 1, "event_count": 1}
    finally:
        await second_db.close()
        await first_db.close()
