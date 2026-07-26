from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from src.gridbot.mainnet.v1469_arm_arbiter import (
    ArmIdentity,
    LeaseAction,
    LeasePhase,
    LeaseProposal,
    LeaseRevocation,
)
from src.gridbot.storage.database import Database
from src.gridbot.storage.v1469_lease_repository import (
    LeaseContext,
    V1469LeaseConflictError,
    V1469LeaseRepository,
)


NOW = 10_000


def _identity(arm_key: str = "arm-a") -> ArmIdentity:
    return ArmIdentity(
        arm_key=arm_key,
        lane_code="W6A",
        side="LONG",
        strategy="S1_BB_RSI",
        regime="RANGE",
        execution_profile_id="RANGE_SCALP",
        execution_profile_hash="profile-hash-a",
    )


def _context(
    *,
    evidence_as_of_ms: int = NOW - 1,
    symbol: str = "ETHUSDC",
) -> LeaseContext:
    return LeaseContext(
        environment="mainnet",
        symbol=symbol,
        execution_profile_schema="v1469.execution-profile.1",
        notional_cap_usdc=20.0,
        risk_policy_hash="risk-policy-a",
        evidence_as_of_ms=evidence_as_of_ms,
        owner_id="owner-a",
        boot_id="boot-a",
    )


def _proposal(
    action: LeaseAction = LeaseAction.GRANT,
    *,
    arm_key: str = "arm-a",
    revision: str = "revision-1",
    expires_at_ms: int = NOW + 5_000,
    phase: LeasePhase = LeasePhase.PROBATION,
) -> LeaseProposal:
    return LeaseProposal(
        action=action,
        arm_key=arm_key,
        phase=phase,
        evidence_revision=revision,
        expires_at_ms=expires_at_ms,
    )


async def _repository(
    tmp_path: Path,
) -> tuple[Database, V1469LeaseRepository]:
    db = Database(str(tmp_path / "v1469-lease.db"))
    await db.initialize()
    return db, V1469LeaseRepository(db)


@pytest.mark.asyncio
async def test_schema_ready_and_grant_exact_retry(tmp_path: Path) -> None:
    db, repo = await _repository(tmp_path)
    try:
        await repo.assert_schema_ready()
        granted = await repo.apply_proposal(
            _identity(),
            _proposal(),
            _context(),
            expected_generation=0,
            expected_evidence_revision=None,
            event_time_ms=NOW,
            idempotency_key="grant-a",
            actor="test",
        )
        assert granted.applied is True
        assert granted.replayed is False
        assert granted.event_generation == 1
        assert granted.lease.generation == 1
        assert granted.lease.status == "ACTIVE"
        assert granted.lease.phase == LeasePhase.PROBATION
        assert granted.lease.environment == "MAINNET"
        assert granted.lease.symbol == "ETHUSDC"
        assert granted.lease.notional_cap_usdc == pytest.approx(20.0)

        replay = await repo.apply_proposal(
            _identity(),
            _proposal(),
            _context(),
            expected_generation=0,
            expected_evidence_revision=None,
            event_time_ms=NOW,
            idempotency_key="grant-a",
            actor="test",
        )
        assert replay.applied is False
        assert replay.replayed is True
        assert replay.lease.generation == 1
        assert await db.fetchone(
            "SELECT COUNT(*) AS n FROM v1469_arm_events"
        ) == {"n": 1}

        current = await repo.load_current_lease(
            environment="mainnet",
            symbol="ethusdc",
            now_ms=NOW,
        )
        assert current is not None
        assert current.arm_key == "arm-a"
        assert current.evidence_revision == "revision-1"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_idempotency_conflict_and_event_failure_roll_back_grant(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        await repo.apply_proposal(
            _identity(),
            _proposal(),
            _context(),
            expected_generation=0,
            expected_evidence_revision=None,
            event_time_ms=NOW,
            idempotency_key="grant-a",
            actor="test",
        )
        with pytest.raises(
            V1469LeaseConflictError,
            match="idempotency key reused",
        ):
            await repo.apply_proposal(
                _identity(),
                replace(_proposal(), expires_at_ms=NOW + 6_000),
                _context(),
                expected_generation=0,
                expected_evidence_revision=None,
                event_time_ms=NOW,
                idempotency_key="grant-a",
                actor="test",
            )
        assert (await repo.get_lease("arm-a")).expires_at_ms == NOW + 5_000

        await db.conn.execute(
            """CREATE TRIGGER fail_v1469_test_event
            BEFORE INSERT ON v1469_arm_events
            WHEN NEW.idempotency_key = 'fail-event'
            BEGIN
                SELECT RAISE(ABORT, 'forced event failure');
            END"""
        )
        await db.conn.commit()
        with pytest.raises(V1469LeaseConflictError, match="forced event failure"):
            await repo.apply_proposal(
                _identity("arm-b"),
                _proposal(arm_key="arm-b"),
                _context(symbol="BTCUSDC"),
                expected_generation=0,
                expected_evidence_revision=None,
                event_time_ms=NOW,
                idempotency_key="fail-event",
                actor="test",
            )
        assert await repo.get_lease("arm-b") is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_only_one_active_arm_per_environment_symbol(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        await repo.apply_proposal(
            _identity(),
            _proposal(),
            _context(),
            expected_generation=0,
            expected_evidence_revision=None,
            event_time_ms=NOW,
            idempotency_key="grant-a",
            actor="test",
        )
        with pytest.raises(
            V1469LeaseConflictError,
            match="active_lease_exists:arm-a",
        ):
            await repo.apply_proposal(
                _identity("arm-b"),
                _proposal(arm_key="arm-b"),
                _context(),
                expected_generation=0,
                expected_evidence_revision=None,
                event_time_ms=NOW + 1,
                idempotency_key="grant-b",
                actor="test",
            )
        assert await repo.get_lease("arm-b") is None
        assert await db.fetchone(
            "SELECT COUNT(*) AS n FROM v1469_arm_events"
        ) == {"n": 1}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_renew_requires_exact_cas_and_newer_evidence(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        await repo.apply_proposal(
            _identity(),
            _proposal(),
            _context(),
            expected_generation=0,
            expected_evidence_revision=None,
            event_time_ms=NOW,
            idempotency_key="grant-a",
            actor="test",
        )
        renew_at = NOW + 100
        renewed = await repo.apply_proposal(
            _identity(),
            _proposal(
                LeaseAction.RENEW,
                revision="revision-2",
                expires_at_ms=renew_at + 5_000,
            ),
            _context(evidence_as_of_ms=renew_at - 1),
            expected_generation=1,
            expected_evidence_revision="revision-1",
            event_time_ms=renew_at,
            idempotency_key="renew-a",
            actor="test",
        )
        assert renewed.applied is True
        assert renewed.lease.generation == 2
        assert renewed.lease.evidence_revision == "revision-2"
        assert renewed.lease.issued_at_ms == NOW
        assert renewed.lease.renewed_at_ms == renew_at

        with pytest.raises(
            V1469LeaseConflictError,
            match="renewal requires a new evidence revision",
        ):
            await repo.apply_proposal(
                _identity(),
                _proposal(
                    LeaseAction.RENEW,
                    revision="revision-2",
                    expires_at_ms=renew_at + 6_000,
                ),
                _context(evidence_as_of_ms=renew_at),
                expected_generation=2,
                expected_evidence_revision="revision-2",
                event_time_ms=renew_at + 1,
                idempotency_key="same-revision",
                actor="test",
            )
        with pytest.raises(
            V1469LeaseConflictError,
            match="lease_generation_changed",
        ):
            await repo.apply_proposal(
                _identity(),
                _proposal(
                    LeaseAction.RENEW,
                    revision="revision-3",
                    expires_at_ms=renew_at + 6_000,
                ),
                _context(evidence_as_of_ms=renew_at),
                expected_generation=1,
                expected_evidence_revision="revision-2",
                event_time_ms=renew_at + 1,
                idempotency_key="stale-generation",
                actor="test",
            )
        assert (await repo.get_lease("arm-a")).generation == 2
        assert await db.fetchone(
            "SELECT COUNT(*) AS n FROM v1469_arm_events"
        ) == {"n": 2}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_expired_and_revoked_leases_fail_closed(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        await repo.apply_proposal(
            _identity(),
            _proposal(expires_at_ms=NOW + 5),
            _context(),
            expected_generation=0,
            expected_evidence_revision=None,
            event_time_ms=NOW,
            idempotency_key="grant-a",
            actor="test",
        )
        assert await repo.get_active_lease(
            environment="MAINNET",
            symbol="ETHUSDC",
            now_ms=NOW + 5,
        ) is None
        expired = await repo.get_lease("arm-a")
        assert expired is not None
        assert expired.status == "EXPIRED"
        assert expired.generation == 2
        assert expired.demotion_reason == "lease_expired"
        with pytest.raises(V1469LeaseConflictError, match="lease_not_active"):
            await repo.apply_proposal(
                _identity(),
                _proposal(
                    LeaseAction.RENEW,
                    revision="revision-2",
                    expires_at_ms=NOW + 10_000,
                ),
                _context(evidence_as_of_ms=NOW + 5),
                expected_generation=2,
                expected_evidence_revision="revision-1",
                event_time_ms=NOW + 5,
                idempotency_key="renew-expired",
                actor="test",
            )

        await repo.apply_proposal(
            _identity("arm-b"),
            _proposal(arm_key="arm-b", expires_at_ms=NOW + 10_000),
            _context(symbol="BTCUSDC"),
            expected_generation=0,
            expected_evidence_revision=None,
            event_time_ms=NOW + 5,
            idempotency_key="grant-b",
            actor="test",
        )
        revoked = await repo.revoke(
            LeaseRevocation(
                arm_key="arm-b",
                reason="regime_drift",
                revoke_at_ms=NOW + 6,
            ),
            expected_generation=1,
            expected_evidence_revision="revision-1",
            idempotency_key="revoke-b",
            actor="test",
        )
        assert revoked.lease.status == "REVOKED"
        assert revoked.lease.generation == 2
        assert await repo.get_active_lease(
            environment="MAINNET",
            symbol="BTCUSDC",
            now_ms=NOW + 6,
        ) is None

        replay = await repo.revoke(
            LeaseRevocation(
                arm_key="arm-b",
                reason="regime_drift",
                revoke_at_ms=NOW + 6,
            ),
            expected_generation=1,
            expected_evidence_revision="revision-1",
            idempotency_key="revoke-b",
            actor="test",
        )
        assert replay.replayed is True
        assert replay.applied is False
        with pytest.raises(
            V1469LeaseConflictError,
            match="lease_not_active",
        ):
            await repo.revoke(
                LeaseRevocation(
                    arm_key="arm-b",
                    reason="second_revoke",
                    revoke_at_ms=NOW + 7,
                ),
                expected_generation=2,
                expected_evidence_revision="revision-1",
                idempotency_key="revoke-b-again",
                actor="test",
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_new_arm_grant_atomically_expires_stale_active_arm(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        await repo.apply_proposal(
            _identity(),
            _proposal(expires_at_ms=NOW + 5),
            _context(),
            expected_generation=0,
            expected_evidence_revision=None,
            event_time_ms=NOW,
            idempotency_key="grant-a",
            actor="test",
        )

        granted = await repo.apply_proposal(
            _identity("arm-b"),
            _proposal(
                arm_key="arm-b",
                revision="revision-b",
                expires_at_ms=NOW + 5_000,
            ),
            _context(evidence_as_of_ms=NOW + 4),
            expected_generation=0,
            expected_evidence_revision=None,
            event_time_ms=NOW + 5,
            idempotency_key="grant-b-after-expiry",
            actor="test",
        )
        assert granted.lease.arm_key == "arm-b"
        assert granted.lease.status == "ACTIVE"

        old = await repo.get_lease("arm-a")
        assert old is not None
        assert old.status == "EXPIRED"
        assert old.generation == 2
        assert old.demotion_reason == "lease_expired"
        assert old.demoted_at_ms == NOW + 5
        assert await db.fetchone(
            """SELECT COUNT(*) AS n FROM v1469_arm_leases
            WHERE environment = 'MAINNET' AND symbol = 'ETHUSDC'
              AND status = 'ACTIVE'"""
        ) == {"n": 1}
        assert await db.fetchone(
            """SELECT arm_key FROM v1469_arm_leases
            WHERE environment = 'MAINNET' AND symbol = 'ETHUSDC'
              AND status = 'ACTIVE'"""
        ) == {"arm_key": "arm-b"}
        events = await db.fetchall(
            """SELECT arm_key, event_type, generation_before,
                      generation_after
            FROM v1469_arm_events ORDER BY id"""
        )
        assert events == [
            {
                "arm_key": "arm-a",
                "event_type": "PROBATION_GRANTED",
                "generation_before": None,
                "generation_after": 1,
            },
            {
                "arm_key": "arm-a",
                "event_type": "EXPIRED",
                "generation_before": 1,
                "generation_after": 2,
            },
            {
                "arm_key": "arm-b",
                "event_type": "PROBATION_GRANTED",
                "generation_before": None,
                "generation_after": 1,
            },
        ]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_post_expiry_grant_failure_rolls_back_expiry_and_event(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        await repo.apply_proposal(
            _identity(),
            _proposal(expires_at_ms=NOW + 5),
            _context(),
            expected_generation=0,
            expected_evidence_revision=None,
            event_time_ms=NOW,
            idempotency_key="grant-a",
            actor="test",
        )
        await db.conn.execute(
            """CREATE TRIGGER fail_post_expiry_grant_event
            BEFORE INSERT ON v1469_arm_events
            WHEN NEW.idempotency_key = 'fail-post-expiry-grant'
            BEGIN
                SELECT RAISE(ABORT, 'forced post-expiry grant failure');
            END"""
        )
        await db.conn.commit()

        with pytest.raises(
            V1469LeaseConflictError,
            match="forced post-expiry grant failure",
        ):
            await repo.apply_proposal(
                _identity("arm-b"),
                _proposal(
                    arm_key="arm-b",
                    revision="revision-b",
                    expires_at_ms=NOW + 5_000,
                ),
                _context(evidence_as_of_ms=NOW + 4),
                expected_generation=0,
                expected_evidence_revision=None,
                event_time_ms=NOW + 5,
                idempotency_key="fail-post-expiry-grant",
                actor="test",
            )

        old = await repo.get_lease("arm-a")
        assert old is not None
        assert old.status == "ACTIVE"
        assert old.generation == 1
        assert await repo.get_lease("arm-b") is None
        assert await db.fetchone(
            """SELECT COUNT(*) AS n FROM v1469_arm_events
            WHERE event_type = 'EXPIRED'"""
        ) == {"n": 0}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_regrant_after_revoke_requires_new_revision_and_exact_generation(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        await repo.apply_proposal(
            _identity(),
            _proposal(),
            _context(),
            expected_generation=0,
            expected_evidence_revision=None,
            event_time_ms=NOW,
            idempotency_key="grant-a",
            actor="test",
        )
        revoked = await repo.revoke(
            LeaseRevocation("arm-a", "winner_changed", NOW + 1),
            expected_generation=1,
            expected_evidence_revision="revision-1",
            idempotency_key="revoke-a",
            actor="test",
        )
        stable_lease_id = revoked.lease.lease_id

        with pytest.raises(
            V1469LeaseConflictError,
            match="re-grant requires a new evidence revision",
        ):
            await repo.apply_proposal(
                _identity(),
                _proposal(expires_at_ms=NOW + 6_000),
                _context(evidence_as_of_ms=NOW),
                expected_generation=2,
                expected_evidence_revision="revision-1",
                event_time_ms=NOW + 2,
                idempotency_key="regrant-stale",
                actor="test",
            )
        regranted = await repo.apply_proposal(
            _identity(),
            _proposal(
                revision="revision-2",
                expires_at_ms=NOW + 6_000,
            ),
            _context(evidence_as_of_ms=NOW + 1),
            expected_generation=2,
            expected_evidence_revision="revision-1",
            event_time_ms=NOW + 2,
            idempotency_key="regrant-new",
            actor="test",
        )
        assert regranted.lease.status == "ACTIVE"
        assert regranted.lease.generation == 3
        assert regranted.lease.lease_id == stable_lease_id
        assert regranted.lease.evidence_revision == "revision-2"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_keep_or_none_cannot_mutate_durable_authority(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        with pytest.raises(ValueError, match="only GRANT or RENEW"):
            await repo.apply_proposal(
                _identity(),
                _proposal(LeaseAction.KEEP),
                _context(),
                expected_generation=0,
                expected_evidence_revision=None,
                event_time_ms=NOW,
                idempotency_key="keep",
                actor="test",
            )
        assert await repo.get_lease("arm-a") is None
    finally:
        await db.close()
