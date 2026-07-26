from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3

import pytest

from src.gridbot.storage.database import Database
from src.gridbot.storage.v1469_paid_execution_claim_repository import (
    PaidClaimMutationResult,
    V1469PaidClaimConflictError,
    V1469PaidClaimPersistenceError,
    V1469PaidExecutionClaimRepository,
)
from tests.test_v1469_paid_execution_claim_repository import (
    ARM_KEY,
    ENVIRONMENT,
    LEASE_ID,
    SYMBOL,
    _seed_opportunities_and_active_lease,
    _transition_claim_to_submitted,
)


PROFILE_HASH = "c" * 64
RISK_HASH = "d" * 64


def _claim_kwargs(
    opportunity_id: str,
    *,
    claimed_at_ms: int,
    lease_generation: int = 1,
    evidence_revision: str = "revision-1",
    approved_notional_usdc: float = 6.0,
    reserved_loss_usdc: float = 0.06,
) -> dict[str, object]:
    return {
        "environment": ENVIRONMENT,
        "symbol": SYMBOL,
        "opportunity_id": opportunity_id,
        "arm_key": ARM_KEY,
        "lease_id": LEASE_ID,
        "claimed_at_ms": claimed_at_ms,
        "idempotency_key": f"claim:{opportunity_id}",
        "actor": "authority-contract-test",
        "expected_lease_generation": lease_generation,
        "expected_evidence_revision": evidence_revision,
        "expected_regime": "RANGE",
        "expected_execution_profile_hash": PROFILE_HASH,
        "expected_risk_policy_hash": RISK_HASH,
        "approved_notional_usdc": approved_notional_usdc,
        "reserved_loss_usdc": reserved_loss_usdc,
        "global_notional_cap_usdc": 100.0,
        "lane_notional_cap_usdc": 100.0,
        "daily_reserved_loss_cap_usdc": 100.0,
    }


async def _seed_scope(
    db: Database,
    *,
    environment: str,
    symbol: str,
    arm_key: str,
    lease_id: str,
    lane_code: str,
    opportunity_ids: tuple[str, ...],
) -> None:
    for index, opportunity_id in enumerate(opportunity_ids):
        observed_at_ms = 1_000 + index
        await db.conn.execute(
            """INSERT INTO v1469_market_opportunities (
                opportunity_id, environment, symbol, observed_at_ms,
                feature_at_ms, coarse_regime, regime_confidence,
                feature_schema, feature_hash, feature_snapshot_json,
                source_run_id, source_event_id, data_quality, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, 'RANGE', 0.9, ?, ?, '{}',
                      ?, ?, 'COMPLETE', ?)""",
            (
                opportunity_id,
                environment,
                symbol,
                observed_at_ms,
                observed_at_ms,
                "v1469.test.features.1",
                f"feature-{opportunity_id}",
                f"run-{opportunity_id}",
                f"event-{opportunity_id}",
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
            ?, ?, 1, ?, ?, ?, 'LONG', 'TEST', 'RANGE',
            'RANGE_SCALP', 'v1469.execution-profile.1', ?,
            'PROBATION', 'ACTIVE', 10.0, ?, 'revision-1', 1000,
            1000, 1000, 1000000000, 'owner-1', 'boot-1',
            NULL, NULL, NULL, 1000, 1000
        )""",
        (
            arm_key,
            lease_id,
            environment,
            symbol,
            lane_code,
            PROFILE_HASH,
            RISK_HASH,
        ),
    )
    await db.conn.commit()


async def _claim_scope(
    repo: V1469PaidExecutionClaimRepository,
    *,
    environment: str,
    symbol: str,
    opportunity_id: str,
    arm_key: str,
    lease_id: str,
    claimed_at_ms: int,
) -> PaidClaimMutationResult:
    return await repo.claim(
        environment=environment,
        symbol=symbol,
        opportunity_id=opportunity_id,
        arm_key=arm_key,
        lease_id=lease_id,
        claimed_at_ms=claimed_at_ms,
        idempotency_key=f"claim:{environment}:{symbol}:{opportunity_id}",
        actor="authority-contract-test",
        expected_lease_generation=1,
        expected_evidence_revision="revision-1",
        expected_regime="RANGE",
        expected_execution_profile_hash=PROFILE_HASH,
        expected_risk_policy_hash=RISK_HASH,
        approved_notional_usdc=6.0,
        reserved_loss_usdc=0.06,
        global_notional_cap_usdc=100.0,
        lane_notional_cap_usdc=100.0,
        daily_reserved_loss_cap_usdc=100.0,
    )


@pytest.mark.asyncio
async def test_exact_authority_snapshot_is_atomic_immutable_and_restart_safe(
    tmp_path: Path,
) -> None:
    path = tmp_path / "authority-snapshot.db"
    db = Database(str(path))
    await db.initialize()
    repo = V1469PaidExecutionClaimRepository(db)
    await _seed_opportunities_and_active_lease(db, "opp-1")
    claim = (await repo.claim(**_claim_kwargs("opp-1", claimed_at_ms=1_100))).claim

    assert claim.lease_generation == 1
    assert claim.evidence_revision == "revision-1"
    assert claim.regime == "RANGE"
    assert claim.execution_profile_hash == PROFILE_HASH
    assert claim.risk_policy_hash == RISK_HASH
    assert claim.approved_notional_usdc == 6.0
    assert claim.reserved_loss_usdc == 0.06
    raw = await db.fetchone(
        """SELECT lease_generation, evidence_revision, regime,
                  execution_profile_hash, risk_policy_hash,
                  approved_notional_usdc, reserved_loss_usdc
           FROM v1469_paid_execution_claim_authority
           WHERE claim_id = ?""",
        (claim.claim_id,),
    )
    assert raw == {
        "lease_generation": 1,
        "evidence_revision": "revision-1",
        "regime": "RANGE",
        "execution_profile_hash": PROFILE_HASH,
        "risk_policy_hash": RISK_HASH,
        "approved_notional_usdc": 6.0,
        "reserved_loss_usdc": 0.06,
    }
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        await db.conn.execute(
            """UPDATE v1469_paid_execution_claim_authority
               SET evidence_revision = 'tampered'
               WHERE claim_id = ?""",
            (claim.claim_id,),
        )
    await db.conn.rollback()
    await db.close()

    restarted = Database(str(path))
    await restarted.initialize()
    try:
        assert (
            await V1469PaidExecutionClaimRepository(
                restarted
            ).get_claim_by_id(claim.claim_id)
            == claim
        )
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_021_backfill_keeps_pre_authority_claim_fail_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "authority-backfill.db"
    db = Database(str(path))
    await db.initialize()
    repo = V1469PaidExecutionClaimRepository(db)
    await _seed_opportunities_and_active_lease(db, "pre-021")
    claim = (
        await repo.claim(**_claim_kwargs("pre-021", claimed_at_ms=1_100))
    ).claim
    await db.conn.executescript(
        """DROP TRIGGER trg_v1469_paid_claim_authority_no_delete;
        DROP TRIGGER trg_v1469_paid_claim_authority_no_update;
        DROP TRIGGER trg_v1469_paid_claim_authority_claim_exists;
        DELETE FROM v1469_paid_execution_claim_authority;
        DELETE FROM _migrations
        WHERE filename = '021_v1469_paid_claim_authority_snapshot.sql';"""
    )
    await db.conn.commit()
    await db.close()

    upgraded = Database(str(path))
    await upgraded.initialize()
    upgraded_repo = V1469PaidExecutionClaimRepository(upgraded)
    try:
        await upgraded_repo.assert_schema_ready()
        durable = await upgraded_repo.get_claim_by_id(claim.claim_id)
        assert durable is not None
        assert durable.lease_generation == 0
        assert durable.evidence_revision == "LEGACY_UNBOUND"
        assert durable.execution_profile_hash == "LEGACY_UNBOUND"
        with pytest.raises(
            V1469PaidClaimConflictError,
            match="current lease authority differs",
        ):
            await upgraded_repo.transition_submission(
                claim_id=claim.claim_id,
                expected_generation=1,
                target_status="SUBMITTING",
                transition_at_ms=1_200,
                idempotency_key="submit:pre-021",
                actor="authority-contract-test",
                payload={"client_order_id": "cid-pre-021"},
            )
    finally:
        await upgraded.close()

@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("generation", 2),
        ("evidence_revision", "revision-2"),
        ("coarse_regime", "TREND_UP"),
        ("execution_profile_hash", "e" * 64),
        ("risk_policy_hash", "f" * 64),
    ),
)
async def test_claimed_to_submitting_revalidates_exact_current_authority(
    tmp_path: Path,
    column: str,
    value: object,
) -> None:
    db = Database(str(tmp_path / f"submit-{column}.db"))
    await db.initialize()
    repo = V1469PaidExecutionClaimRepository(db)
    await _seed_opportunities_and_active_lease(db, "opp-1")
    claim = (await repo.claim(**_claim_kwargs("opp-1", claimed_at_ms=1_100))).claim
    try:
        await db.conn.execute(
            f"UPDATE v1469_arm_leases SET {column} = ? WHERE arm_key = ?",
            (value, ARM_KEY),
        )
        await db.conn.commit()
        with pytest.raises(
            V1469PaidClaimConflictError,
            match="current lease authority differs",
        ):
            await repo.transition_submission(
                claim_id=claim.claim_id,
                expected_generation=1,
                target_status="SUBMITTING",
                transition_at_ms=1_200,
                idempotency_key=f"submit:{column}",
                actor="authority-contract-test",
                payload={"client_order_id": f"cid-{column}"},
            )
        durable = await repo.get_claim_by_id(claim.claim_id)
        assert durable is not None
        assert durable.status == "CLAIMED"
        assert durable.generation == 1
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expected_reason", "global_cap", "lane_cap", "daily_cap"),
    (
        ("global outstanding notional cap exceeded", 10.0, 100.0, 100.0),
        ("lane outstanding notional cap exceeded", 100.0, 10.0, 100.0),
        ("daily reserved loss cap exceeded", 100.0, 100.0, 0.1),
    ),
)
async def test_concurrent_claims_atomically_enforce_each_reservation_cap(
    tmp_path: Path,
    expected_reason: str,
    global_cap: float,
    lane_cap: float,
    daily_cap: float,
) -> None:
    path = tmp_path / f"cap-{expected_reason.split()[0]}.db"
    first_db = Database(str(path))
    await first_db.initialize()
    await _seed_opportunities_and_active_lease(first_db, "opp-1", "opp-2")
    second_db = Database(str(path))
    await second_db.initialize()
    first_repo = V1469PaidExecutionClaimRepository(first_db)
    second_repo = V1469PaidExecutionClaimRepository(second_db)
    common = {
        "expected_lease_generation": 1,
        "expected_evidence_revision": "revision-1",
        "expected_regime": "RANGE",
        "expected_execution_profile_hash": PROFILE_HASH,
        "expected_risk_policy_hash": RISK_HASH,
        "approved_notional_usdc": 6.0,
        "reserved_loss_usdc": 0.06,
        "global_notional_cap_usdc": global_cap,
        "lane_notional_cap_usdc": lane_cap,
        "daily_reserved_loss_cap_usdc": daily_cap,
    }
    try:
        results = await asyncio.gather(
            first_repo.claim(
                environment=ENVIRONMENT,
                symbol=SYMBOL,
                opportunity_id="opp-1",
                arm_key=ARM_KEY,
                lease_id=LEASE_ID,
                claimed_at_ms=1_100,
                idempotency_key=f"cap:first:{expected_reason}",
                actor="first",
                **common,
            ),
            second_repo.claim(
                environment=ENVIRONMENT,
                symbol=SYMBOL,
                opportunity_id="opp-2",
                arm_key=ARM_KEY,
                lease_id=LEASE_ID,
                claimed_at_ms=1_100,
                idempotency_key=f"cap:second:{expected_reason}",
                actor="second",
                **common,
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(item, PaidClaimMutationResult) for item in results) == 1
        conflicts = [
            item for item in results
            if isinstance(item, V1469PaidClaimConflictError)
        ]
        assert len(conflicts) == 1
        assert expected_reason in str(conflicts[0])
        counts = await first_db.fetchone(
            """SELECT
                (SELECT COUNT(*) FROM v1469_paid_execution_claims) AS claims,
                (SELECT COUNT(*) FROM v1469_paid_execution_claim_authority)
                    AS authority,
                (SELECT COUNT(*) FROM v1469_paid_execution_claim_events)
                    AS events"""
        )
        assert counts == {"claims": 1, "authority": 1, "events": 1}
    finally:
        await second_db.close()
        await first_db.close()


@pytest.mark.asyncio
async def test_daily_reservation_uses_taipei_midnight_boundary(
    tmp_path: Path,
) -> None:
    db = Database(str(tmp_path / "tpe-day.db"))
    await db.initialize()
    repo = V1469PaidExecutionClaimRepository(db)
    await _seed_scope(
        db,
        environment=ENVIRONMENT,
        symbol=SYMBOL,
        arm_key=ARM_KEY,
        lease_id=LEASE_ID,
        lane_code="W6A",
        opportunity_ids=("before-midnight", "after-midnight"),
    )
    common = {
        "expected_lease_generation": 1,
        "expected_evidence_revision": "revision-1",
        "expected_regime": "RANGE",
        "expected_execution_profile_hash": PROFILE_HASH,
        "expected_risk_policy_hash": RISK_HASH,
        "approved_notional_usdc": 1.0,
        "reserved_loss_usdc": 0.06,
        "global_notional_cap_usdc": 100.0,
        "lane_notional_cap_usdc": 100.0,
        "daily_reserved_loss_cap_usdc": 0.06,
        "actor": "authority-contract-test",
    }
    try:
        before = await repo.claim(
            environment=ENVIRONMENT,
            symbol=SYMBOL,
            opportunity_id="before-midnight",
            arm_key=ARM_KEY,
            lease_id=LEASE_ID,
            claimed_at_ms=57_599_999,
            idempotency_key="claim:before-midnight",
            **common,
        )
        after = await repo.claim(
            environment=ENVIRONMENT,
            symbol=SYMBOL,
            opportunity_id="after-midnight",
            arm_key=ARM_KEY,
            lease_id=LEASE_ID,
            claimed_at_ms=57_600_000,
            idempotency_key="claim:after-midnight",
            **common,
        )
        assert before.applied is True
        assert after.applied is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reconcilable_list_is_mainnet_scoped_ordered_and_bounded(
    tmp_path: Path,
) -> None:
    db = Database(str(tmp_path / "reconcilable.db"))
    await db.initialize()
    repo = V1469PaidExecutionClaimRepository(db)
    main_arm = "v1469a_" + "1" * 64
    main_lease = "v1469l_" + "1" * 64
    test_arm = "v1469a_" + "2" * 64
    test_lease = "v1469l_" + "2" * 64
    eth_arm = "v1469a_" + "3" * 64
    eth_lease = "v1469l_" + "3" * 64
    await _seed_scope(
        db,
        environment="MAINNET",
        symbol="BTCUSDC",
        arm_key=main_arm,
        lease_id=main_lease,
        lane_code="W6A",
        opportunity_ids=("main-old", "main-later", "main-claimed"),
    )
    await _seed_scope(
        db,
        environment="TESTNET",
        symbol="BTCUSDC",
        arm_key=test_arm,
        lease_id=test_lease,
        lane_code="W6A",
        opportunity_ids=("test-only",),
    )
    await _seed_scope(
        db,
        environment="MAINNET",
        symbol="ETHUSDC",
        arm_key=eth_arm,
        lease_id=eth_lease,
        lane_code="W6A",
        opportunity_ids=("eth-first",),
    )
    try:
        old = (
            await _claim_scope(
                repo,
                environment="MAINNET",
                symbol="BTCUSDC",
                opportunity_id="main-old",
                arm_key=main_arm,
                lease_id=main_lease,
                claimed_at_ms=1_050,
            )
        ).claim
        later = (
            await _claim_scope(
                repo,
                environment="MAINNET",
                symbol="BTCUSDC",
                opportunity_id="main-later",
                arm_key=main_arm,
                lease_id=main_lease,
                claimed_at_ms=1_051,
            )
        ).claim
        claimed = (
            await _claim_scope(
                repo,
                environment="MAINNET",
                symbol="BTCUSDC",
                opportunity_id="main-claimed",
                arm_key=main_arm,
                lease_id=main_lease,
                claimed_at_ms=1_052,
            )
        ).claim
        test_only = (
            await _claim_scope(
                repo,
                environment="TESTNET",
                symbol="BTCUSDC",
                opportunity_id="test-only",
                arm_key=test_arm,
                lease_id=test_lease,
                claimed_at_ms=1_053,
            )
        ).claim
        eth = (
            await _claim_scope(
                repo,
                environment="MAINNET",
                symbol="ETHUSDC",
                opportunity_id="eth-first",
                arm_key=eth_arm,
                lease_id=eth_lease,
                claimed_at_ms=1_054,
            )
        ).claim

        async def transition(claim_id: str, at_ms: int, status: str, generation: int):
            return await repo.transition_submission(
                claim_id=claim_id,
                expected_generation=generation,
                target_status=status,
                transition_at_ms=at_ms,
                idempotency_key=f"{claim_id}:{status}:{generation}",
                actor="authority-contract-test",
                payload={"client_order_id": f"cid-{claim_id[-12:]}"},
            )

        old = (await transition(old.claim_id, 1_200, "SUBMITTING", 1)).claim
        old = (await transition(old.claim_id, 1_350, "UNKNOWN", 2)).claim
        later = (await transition(later.claim_id, 1_400, "SUBMITTING", 1)).claim
        later = (await transition(later.claim_id, 1_500, "SUBMITTED", 2)).claim
        test_only = (
            await transition(test_only.claim_id, 1_100, "SUBMITTING", 1)
        ).claim
        eth = (await transition(eth.claim_id, 1_200, "SUBMITTING", 1)).claim

        first_two = await repo.list_reconcilable_claims(
            environment="mainnet", limit=2
        )
        assert [item.claim_id for item in first_two] == [
            claimed.claim_id,
            eth.claim_id,
        ]
        btc = await repo.list_reconcilable_claims(
            environment="MAINNET", symbol="btcusdc", limit=10
        )
        assert [item.claim_id for item in btc] == [
            claimed.claim_id,
            old.claim_id,
            later.claim_id,
        ]
        assert test_only.claim_id not in {item.claim_id for item in btc}
        with pytest.raises(ValueError, match="1 to 100"):
            await repo.list_reconcilable_claims(
                environment="MAINNET", limit=0
            )
        with pytest.raises(ValueError, match="1 to 100"):
            await repo.list_reconcilable_claims(
                environment="MAINNET", limit=101
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_probation_evidence_aggregates_recent_exact_lineage_across_revisions(
    tmp_path: Path,
) -> None:
    db = Database(str(tmp_path / "probation-evidence.db"))
    await db.initialize()
    repo = V1469PaidExecutionClaimRepository(db)
    await _seed_opportunities_and_active_lease(
        db, "opp-1", "opp-2", "opp-3", "no-fill", "abandoned"
    )
    results = (
        ("opp-1", 1, "revision-1", 1_100, 1_200, 0.04, False),
        ("opp-2", 2, "revision-2", 1_300, 1_400, -0.01, False),
        ("opp-3", 3, "revision-3", 1_500, 1_600, 0.02, True),
    )
    try:
        for (
            opportunity_id,
            lease_generation,
            evidence_revision,
            claimed_at,
            terminal_at,
            pnl,
            hard_loss,
        ) in results:
            await db.conn.execute(
                """UPDATE v1469_arm_leases
                   SET generation = ?, evidence_revision = ?,
                       evidence_as_of_ms = ?, updated_at_ms = ?
                   WHERE arm_key = ?""",
                (
                    lease_generation,
                    evidence_revision,
                    claimed_at,
                    claimed_at,
                    ARM_KEY,
                ),
            )
            await db.conn.commit()
            claim = (
                await repo.claim(
                    **_claim_kwargs(
                        opportunity_id,
                        claimed_at_ms=claimed_at,
                        lease_generation=lease_generation,
                        evidence_revision=evidence_revision,
                    )
                )
            ).claim
            claim = await _transition_claim_to_submitted(
                repo,
                claim,
                submitting_at_ms=claimed_at + 25,
                submitted_at_ms=claimed_at + 50,
                key=f"evidence-{opportunity_id}",
            )
            await repo.terminalize_claim(
                claim_id=claim.claim_id,
                expected_generation=claim.generation,
                terminal_at_ms=terminal_at,
                terminal_reason="PAID_POSITION_CLOSED",
                idempotency_key=f"terminal:{opportunity_id}",
                actor="authority-contract-test",
                result_payload={
                    "fee_net_pnl_usdc": pnl,
                    "risk_policy_hard_loss": hard_loss,
                },
            )

        no_fill = (
            await repo.claim(
                **_claim_kwargs(
                    "no-fill",
                    claimed_at_ms=1_650,
                    lease_generation=3,
                    evidence_revision="revision-3",
                )
            )
        ).claim
        no_fill = await _transition_claim_to_submitted(
            repo,
            no_fill,
            submitting_at_ms=1_675,
            submitted_at_ms=1_700,
            key="evidence-no-fill",
        )
        await repo.terminalize_claim(
            claim_id=no_fill.claim_id,
            expected_generation=no_fill.generation,
            terminal_at_ms=1_750,
            terminal_reason="ENTRY_EXPIRED",
            idempotency_key="terminal:no-fill",
            actor="authority-contract-test",
            result_payload={
                "schema": "v1469.paid-no-fill.1",
                "outcome": "NO_FILL",
                "source_run_id": "run-no-fill",
            },
        )

        abandoned = (
            await repo.claim(
                **_claim_kwargs(
                    "abandoned",
                    claimed_at_ms=1_700,
                    lease_generation=3,
                    evidence_revision="revision-3",
                )
            )
        ).claim
        await repo.abandon_claim(
            claim_id=abandoned.claim_id,
            expected_generation=1,
            abandoned_at_ms=1_800,
            terminal_reason="NOT_SUBMITTED",
            idempotency_key="abandoned:not-submitted",
            actor="authority-contract-test",
            result_payload={"fee_net_pnl_usdc": 99.0},
        )

        evidence = await repo.load_paid_probation_evidence(
            environment="mainnet",
            symbol="btcusdc",
            arm_key=ARM_KEY,
            execution_profile_hash=PROFILE_HASH,
            regime="range",
            window_start_ms=1_000,
            as_of_ms=2_000,
            limit=100,
        )
        assert evidence["terminal_fills"] == 3
        assert evidence["wins"] == 2
        assert evidence["fee_net_paid_pnl"] == pytest.approx(0.05)
        assert evidence["hard_loss_marker"] is True
        assert evidence["latest_terminal_at"] == 1_600
        assert evidence["truncated"] is False

        recent = await repo.load_paid_probation_evidence(
            environment=ENVIRONMENT,
            symbol=SYMBOL,
            arm_key=ARM_KEY,
            execution_profile_hash=PROFILE_HASH,
            regime="RANGE",
            window_start_ms=1_300,
            as_of_ms=2_000,
            limit=1,
        )
        assert recent == {
            "terminal_fills": 1,
            "wins": 1,
            "fee_net_paid_pnl": pytest.approx(0.02),
            "hard_loss_marker": True,
            "latest_terminal_at": 1_600,
            "truncated": True,
        }

        empty = await repo.load_paid_probation_evidence(
            environment=ENVIRONMENT,
            symbol=SYMBOL,
            arm_key=ARM_KEY,
            execution_profile_hash="not-the-profile",
            regime="RANGE",
            window_start_ms=1_000,
            as_of_ms=2_000,
            limit=100,
        )
        assert empty["terminal_fills"] == 0
        assert empty["latest_terminal_at"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_probation_evidence_rejects_terminal_without_fee_net_result(
    tmp_path: Path,
) -> None:
    db = Database(str(tmp_path / "probation-invalid.db"))
    await db.initialize()
    repo = V1469PaidExecutionClaimRepository(db)
    await _seed_opportunities_and_active_lease(db, "opp-invalid")
    try:
        claim = (
            await repo.claim(
                **_claim_kwargs("opp-invalid", claimed_at_ms=1_100)
            )
        ).claim
        claim = await _transition_claim_to_submitted(
            repo,
            claim,
            submitting_at_ms=1_125,
            submitted_at_ms=1_150,
            key="invalid-evidence",
        )
        await repo.terminalize_claim(
            claim_id=claim.claim_id,
            expected_generation=claim.generation,
            terminal_at_ms=1_200,
            terminal_reason="PAID_POSITION_CLOSED",
            idempotency_key="terminal:invalid",
            actor="authority-contract-test",
            result_payload={},
        )
        with pytest.raises(
            V1469PaidClaimPersistenceError,
            match="invalid fee-net PnL",
        ):
            await repo.load_paid_probation_evidence(
                environment=ENVIRONMENT,
                symbol=SYMBOL,
                arm_key=ARM_KEY,
                execution_profile_hash=PROFILE_HASH,
                regime="RANGE",
                window_start_ms=1_000,
                as_of_ms=2_000,
                limit=100,
            )
    finally:
        await db.close()
