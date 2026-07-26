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


async def _seed_promotion_snapshot(
    db: Database,
    *,
    evidence_revision: str = "revision-2",
    as_of_ms: int = NOW + 99,
    clock_revision: int = 0,
    terminal_fills: int = 3,
    wins: int = 2,
    fee_net_paid_pnl: float = 0.03,
    hard_loss_marker: bool = False,
) -> None:
    if clock_revision:
        await db.conn.execute(
            """INSERT INTO v1469_paid_terminal_evidence_clocks (
                environment, symbol, arm_key, execution_profile_hash,
                regime, revision, terminal_count, latest_terminal_at_ms,
                latest_claim_id, updated_at_ms
            ) VALUES (
                'MAINNET', 'ETHUSDC', 'arm-a', 'profile-hash-a',
                'RANGE', ?, ?, ?, 'claim-watermark', ?
            )""",
            (clock_revision, clock_revision, as_of_ms - 1, as_of_ms),
        )
    await db.conn.execute(
        """INSERT INTO v1469_paid_promotion_evidence_snapshots (
            environment, symbol, arm_key, execution_profile_hash, regime,
            evidence_revision, window_start_ms, as_of_ms, evidence_limit,
            clock_revision, evidence_watermark, terminal_fills, wins,
            fee_net_paid_pnl, hard_loss_marker, latest_terminal_at_ms,
            truncated, created_at_ms
        ) VALUES (
            'MAINNET', 'ETHUSDC', 'arm-a', 'profile-hash-a', 'RANGE',
            ?, 0, ?, 100, ?, ?, ?, ?, ?, ?, ?, 0, ?
        )""",
        (
            evidence_revision,
            as_of_ms,
            clock_revision,
            "f" * 64,
            terminal_fills,
            wins,
            fee_net_paid_pnl,
            int(hard_loss_marker),
            as_of_ms - 1 if terminal_fills else None,
            as_of_ms,
        ),
    )
    await db.conn.commit()


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

@pytest.mark.asyncio
async def test_promote_probation_to_live_exact_cas_and_replay(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        granted = await repo.apply_proposal(
            _identity(),
            _proposal(),
            _context(),
            expected_generation=0,
            expected_evidence_revision=None,
            event_time_ms=NOW,
            idempotency_key="grant-for-promotion",
            actor="test",
        )
        await _seed_promotion_snapshot(db)
        kwargs = dict(
            environment="MAINNET",
            symbol="ETHUSDC",
            arm_key="arm-a",
            lease_id=granted.lease.lease_id,
            expected_generation=1,
            expected_evidence_revision="revision-1",
            new_evidence_revision="revision-2",
            expected_execution_profile_hash="profile-hash-a",
            expected_regime="RANGE",
            expected_risk_policy_hash="risk-policy-a",
            live_notional_cap_usdc=40.0,
            evidence_as_of_ms=NOW + 99,
            event_time_ms=NOW + 100,
            expires_at_ms=NOW + 10_000,
            hard_loss_marker=False,
            idempotency_key="promote-a",
            actor="test",
        )
        promoted = await repo.promote_probation_to_live(**kwargs)
        assert promoted.applied is True
        assert promoted.replayed is False
        assert promoted.lease.phase is LeasePhase.LIVE
        assert promoted.lease.generation == 2
        assert promoted.lease.notional_cap_usdc == pytest.approx(40.0)
        assert promoted.lease.evidence_revision == "revision-2"

        replay = await repo.promote_probation_to_live(**kwargs)
        assert replay.applied is False
        assert replay.replayed is True
        assert replay.lease == promoted.lease

        with pytest.raises(
            V1469LeaseConflictError, match="live_promotion_cas_lost"
        ):
            await repo.promote_probation_to_live(
                **{**kwargs, "idempotency_key": "competing-promotion"}
            )
        assert await db.fetchone(
            """SELECT COUNT(*) AS n FROM v1469_arm_events
            WHERE event_type = 'LIVE_PROMOTED'"""
        ) == {"n": 1}
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("override", "error"),
    [
        ({"environment": "TESTNET"}, "live_promotion_cas_lost"),
        ({"symbol": "BTCUSDC"}, "live_promotion_cas_lost"),
        ({"arm_key": "wrong-arm"}, "lease_missing"),
        ({"lease_id": "wrong-lease"}, "live_promotion_cas_lost"),
        ({"expected_generation": 2}, "live_promotion_cas_lost"),
        ({"expected_evidence_revision": "wrong"},
         "live_promotion_cas_lost"),
        ({"live_notional_cap_usdc": 50.01},
         "LIVE promotion notional cap must be positive and <= 50 USDC"),
        ({"expected_execution_profile_hash": "wrong"},
         "live_promotion_cas_lost"),
        ({"expected_regime": "TREND_UP"}, "live_promotion_cas_lost"),
        ({"expected_risk_policy_hash": "wrong"},
         "live_promotion_cas_lost"),
        ({"event_time_ms": NOW + 5_000,
          "expires_at_ms": NOW + 10_000,
          "evidence_as_of_ms": NOW + 4_999},
         "live_promotion_cas_lost"),
        ({"hard_loss_marker": True},
         "hard_loss_marker_blocks_live_promotion"),
    ],
)
async def test_promotion_scope_expiry_and_hard_loss_fail_closed(
    tmp_path: Path,
    override: dict[str, object],
    error: str,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        granted = await repo.apply_proposal(
            _identity(),
            _proposal(),
            _context(),
            expected_generation=0,
            expected_evidence_revision=None,
            event_time_ms=NOW,
            idempotency_key="grant-for-blocked-promotion",
            actor="test",
        )
        await _seed_promotion_snapshot(db)
        kwargs = dict(
            environment="MAINNET",
            symbol="ETHUSDC",
            arm_key="arm-a",
            lease_id=granted.lease.lease_id,
            expected_generation=1,
            expected_evidence_revision="revision-1",
            new_evidence_revision="revision-2",
            expected_execution_profile_hash="profile-hash-a",
            expected_regime="RANGE",
            expected_risk_policy_hash="risk-policy-a",
            live_notional_cap_usdc=40.0,
            evidence_as_of_ms=NOW + 99,
            event_time_ms=NOW + 100,
            expires_at_ms=NOW + 10_000,
            hard_loss_marker=False,
            idempotency_key="blocked-promotion",
            actor="test",
        )
        kwargs.update(override)
        with pytest.raises((V1469LeaseConflictError, ValueError), match=error):
            await repo.promote_probation_to_live(**kwargs)
        current = await repo.get_lease("arm-a")
        assert current is not None
        assert current.phase is LeasePhase.PROBATION
        assert current.generation == 1
        assert await db.fetchone(
            """SELECT COUNT(*) AS n FROM v1469_arm_events
            WHERE event_type = 'LIVE_PROMOTED'"""
        ) == {"n": 0}
    finally:
        await db.close()
@pytest.mark.asyncio
async def test_live_promotion_rejects_changed_paid_evidence_clock(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        granted = await repo.apply_proposal(
            _identity(),
            _proposal(),
            _context(),
            expected_generation=0,
            expected_evidence_revision=None,
            event_time_ms=NOW,
            idempotency_key="grant-for-watermark-race",
            actor="test",
        )
        await _seed_promotion_snapshot(db)
        # Simulate a new same-scope TERMINAL commit after evidence was read.
        await db.conn.execute(
            """INSERT INTO v1469_paid_terminal_evidence_clocks (
                environment, symbol, arm_key, execution_profile_hash,
                regime, revision, terminal_count, latest_terminal_at_ms,
                latest_claim_id, updated_at_ms
            ) VALUES (
                'MAINNET', 'ETHUSDC', 'arm-a', 'profile-hash-a',
                'RANGE', 1, 1, ?, 'new-hard-loss', ?
            )""",
            (NOW + 99, NOW + 99),
        )
        await db.conn.commit()

        with pytest.raises(
            V1469LeaseConflictError,
            match="promotion_evidence_watermark_changed",
        ):
            await repo.promote_probation_to_live(
                environment="MAINNET",
                symbol="ETHUSDC",
                arm_key="arm-a",
                lease_id=granted.lease.lease_id,
                expected_generation=1,
                expected_evidence_revision="revision-1",
                new_evidence_revision="revision-2",
                expected_execution_profile_hash="profile-hash-a",
                expected_regime="RANGE",
                expected_risk_policy_hash="risk-policy-a",
                live_notional_cap_usdc=40.0,
                evidence_as_of_ms=NOW + 99,
                event_time_ms=NOW + 100,
                expires_at_ms=NOW + 10_000,
                hard_loss_marker=False,
                idempotency_key="stale-watermark-promotion",
                actor="test",
            )
        current = await repo.get_lease("arm-a")
        assert current is not None
        assert current.phase is LeasePhase.PROBATION
        assert current.generation == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_live_promotion_rejects_stale_or_missing_snapshot(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        granted = await repo.apply_proposal(
            _identity(),
            _proposal(),
            _context(),
            expected_generation=0,
            expected_evidence_revision=None,
            event_time_ms=NOW,
            idempotency_key="grant-for-snapshot-contract",
            actor="test",
        )
        base = dict(
            environment="MAINNET",
            symbol="ETHUSDC",
            arm_key="arm-a",
            lease_id=granted.lease.lease_id,
            expected_generation=1,
            expected_evidence_revision="revision-1",
            new_evidence_revision="revision-2",
            expected_execution_profile_hash="profile-hash-a",
            expected_regime="RANGE",
            expected_risk_policy_hash="risk-policy-a",
            live_notional_cap_usdc=40.0,
            expires_at_ms=NOW + 30_000,
            hard_loss_marker=False,
            actor="test",
        )
        with pytest.raises(
            V1469LeaseConflictError,
            match="promotion_evidence_snapshot_missing",
        ):
            await repo.promote_probation_to_live(
                **base,
                evidence_as_of_ms=NOW + 99,
                event_time_ms=NOW + 100,
                idempotency_key="missing-snapshot",
            )
        await _seed_promotion_snapshot(db, as_of_ms=NOW + 99)
        with pytest.raises(
            V1469LeaseConflictError,
            match="promotion_evidence_snapshot_stale",
        ):
            await repo.promote_probation_to_live(
                **base,
                evidence_as_of_ms=NOW + 99,
                event_time_ms=NOW + 10_100,
                idempotency_key="stale-snapshot",
            )
    finally:
        await db.close()
