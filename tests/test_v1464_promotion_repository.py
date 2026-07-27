from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3

import pytest

from src.gridbot.storage.database import Database
from src.gridbot.storage.v1464_promotion_repository import (
    AdmissionClaimError,
    LeaseConflictError,
    PromotionCohort,
    PromotionConflictError,
    PromotionPersistenceError,
    V1464_EVIDENCE_SCHEMA_VERSION,
    V1464_PROFILE_IDENTITY_SCHEMA,
    V1464PromotionRepository,
    lease_row_to_engine_snapshot,
)


MIGRATION = Path(
    "src/gridbot/storage/migrations/013_v1464_adaptive_promotion.sql"
)


def _cohort(**overrides) -> PromotionCohort:
    values = {
        "environment": "mainnet",
        "symbol": "ETHUSDC",
        "lane_code": "W6A",
        "market_state": "supportive_range",
        "effective_side": "LONG",
        "strategy": "S1_BB_RSI",
        "resolved_profile_hash": "profile-a",
        "profile_identity_schema": V1464_PROFILE_IDENTITY_SCHEMA,
        "registry_version": "v1.4.63",
        "registry_hash": "registry-a",
        "lane_definition_hash": "lane-definition-a",
        "admission_policy_hash": "admission-a",
        "promotion_policy_hash": "promotion-a",
    }
    values.update(overrides)
    return PromotionCohort(**values)


def _evidence(
    opportunity_id: str,
    *,
    cohort: PromotionCohort | None = None,
    observed_at_ms: int = 1_000,
    terminal_at_ms: int = 1_100,
    **overrides,
) -> dict:
    active = cohort or _cohort()
    values = {
        "opportunity_id": opportunity_id,
        "environment": active.environment,
        "symbol": active.symbol,
        "lane_code": active.lane_code,
        "market_state": active.market_state,
        "effective_side": active.effective_side,
        "strategy": active.strategy,
        "resolved_profile_hash": active.resolved_profile_hash,
        "profile_identity_schema": active.profile_identity_schema,
        "registry_version": active.registry_version,
        "registry_hash": active.registry_hash,
        "lane_definition_hash": active.lane_definition_hash,
        "admission_policy_hash": active.admission_policy_hash,
        "evidence_schema_version": V1464_EVIDENCE_SCHEMA_VERSION,
        "observed_at_ms": observed_at_ms,
        "terminal_at_ms": terminal_at_ms,
        "outcome": "tp1_first",
        "data_complete": True,
        "ambiguous": False,
        "diagnostic_only": False,
        "net_pnl_usdc": 0.05,
        "source_type": "SHADOW",
        "source_id": f"event:{opportunity_id}",
        "source_payload": {"opportunity_id": opportunity_id},
        "created_at_ms": terminal_at_ms,
    }
    values.update(overrides)
    return values


def _lease(
    lease_id: str,
    *,
    cohort: PromotionCohort | None = None,
    issued_at_ms: int = 2_000,
    renewed_at_ms: int = 2_000,
    expires_at_ms: int = 3_000,
    **overrides,
) -> dict:
    active = cohort or _cohort()
    values = {
        "lease_id": lease_id,
        "environment": active.environment,
        "symbol": active.symbol,
        "lane_code": active.lane_code,
        "market_state": active.market_state,
        "effective_side": active.effective_side,
        "strategy": active.strategy,
        "resolved_profile_hash": active.resolved_profile_hash,
        "profile_identity_schema": active.profile_identity_schema,
        "registry_version": active.registry_version,
        "registry_hash": active.registry_hash,
        "lane_definition_hash": active.lane_definition_hash,
        "admission_policy_hash": active.admission_policy_hash,
        "promotion_policy_hash": active.promotion_policy_hash,
        "phase": "PROBATION",
        "status": "ACTIVE",
        "notional_cap_usdc": 25.0,
        "evidence_window_start_ms": 1_000,
        "evidence_as_of_ms": issued_at_ms,
        "evidence_watermark": 7,
        "evidence_snapshot": {"evaluable": 8, "tp_first": 6},
        "issued_at_ms": issued_at_ms,
        "renewed_at_ms": renewed_at_ms,
        "expires_at_ms": expires_at_ms,
        "boot_id": "boot-a",
        "owner_id": "worker-a",
        "soft_failures": 0,
        "demotion_reason": None,
        "demoted_at_ms": None,
        "cooldown_until_ms": None,
    }
    values.update(overrides)
    return values


async def _repository(tmp_path: Path) -> tuple[Database, V1464PromotionRepository]:
    db = Database(str(tmp_path / "promotion.db"))
    await db.initialize()
    return db, V1464PromotionRepository(db)


def test_v1464_migration_is_idempotent_and_enforces_probation_cap() -> None:
    connection = sqlite3.connect(":memory:")
    migration = MIGRATION.read_text(encoding="utf-8")

    connection.executescript(migration)
    connection.executescript(migration)

    tables = {
        row[0]
        for row in connection.execute(
            """SELECT name FROM sqlite_master
            WHERE type = 'table' AND name LIKE 'v1464_%'"""
        )
    }
    assert tables == {
        "v1464_promotion_evidence",
        "v1464_lane_promotion_leases",
        "v1464_lane_promotion_events",
    }
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """INSERT INTO v1464_lane_promotion_leases (
                cohort_key, lease_id, generation,
                environment, symbol, lane_code, market_state, effective_side,
                strategy, resolved_profile_hash, registry_hash,
                profile_identity_schema, registry_version,
                lane_definition_hash, admission_policy_hash,
                promotion_policy_hash,
                phase, status, notional_cap_usdc,
                evidence_window_start_ms, evidence_as_of_ms,
                evidence_watermark, evidence_snapshot_hash,
                evidence_snapshot_json, issued_at_ms, renewed_at_ms,
                expires_at_ms, boot_id, owner_id, soft_failures,
                demotion_reason, demoted_at_ms, created_at_ms, updated_at_ms
            ) VALUES (
                'cohort', 'lease', 1,
                'mainnet', 'ETHUSDC', 'W6A', 'range', 'LONG',
                'S1_BB_RSI', 'profile', 'registry',
                'v1464.stable-profile.1', 'v1.4.63',
                'lane-definition', 'admission', 'promotion',
                'PROBATION', 'ACTIVE', 25.01,
                0, 1, 0, 'snapshot', '{}', 1, 1, 2,
                'boot', 'owner', 0, NULL, NULL, 1, 1
            )"""
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """INSERT INTO v1464_lane_promotion_leases (
                cohort_key, lease_id, generation,
                environment, symbol, lane_code, market_state, effective_side,
                strategy, resolved_profile_hash, registry_hash,
                profile_identity_schema, registry_version,
                lane_definition_hash, admission_policy_hash,
                promotion_policy_hash,
                phase, status, notional_cap_usdc,
                evidence_window_start_ms, evidence_as_of_ms,
                evidence_watermark, evidence_snapshot_hash,
                evidence_snapshot_json, issued_at_ms, renewed_at_ms,
                expires_at_ms, boot_id, owner_id, soft_failures,
                demotion_reason, demoted_at_ms, cooldown_until_ms,
                created_at_ms, updated_at_ms
            ) VALUES (
                'control-cohort', 'control-lease', 1,
                'mainnet', 'ETHUSDC', 'W6A', 'range', 'LONG',
                'S1_BB_RSI', 'profile', 'registry',
                'v1464.stable-profile.1', 'v1.4.63',
                'lane-definition', 'admission', 'promotion',
                'CONTROL', 'ACTIVE', 50.01,
                0, 1, 0, 'snapshot', '{}', 1, 1, 2,
                'boot', 'owner', 0, NULL, NULL, NULL, 1, 1
            )"""
        )
    connection.execute(
        """INSERT INTO v1464_lane_promotion_events (
            idempotency_key, cohort_key, lease_id,
            generation_before, generation_after,
            event_time_ms, event_type, actor, payload_json
        ) VALUES (
            'event-1', 'cohort', NULL, NULL, NULL,
            1, 'EVALUATED', 'test', '{}'
        )"""
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            """UPDATE v1464_lane_promotion_events
            SET actor = 'mutated' WHERE idempotency_key = 'event-1'"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            """DELETE FROM v1464_lane_promotion_events
            WHERE idempotency_key = 'event-1'"""
        )
    connection.close()


@pytest.mark.asyncio
async def test_evidence_exact_retry_is_noop_and_conflict_never_overwrites(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        original = _evidence("opp-1")
        assert await repo.upsert_evidence(original) is True
        assert await repo.upsert_evidence({**original, "created_at_ms": 1_200}) is False

        with pytest.raises(PromotionConflictError, match="conflicting identity"):
            await repo.upsert_evidence({**original, "market_state": "trend"})

        with pytest.raises(PromotionConflictError, match="source identity"):
            await repo.upsert_evidence(
                _evidence(
                    "opp-2",
                    source_type=original["source_type"],
                    source_id=original["source_id"],
                )
            )

        stored = await db.fetchone(
            """SELECT market_state, outcome, evidence_hash
            FROM v1464_promotion_evidence WHERE opportunity_id = 'opp-1'"""
        )
        assert stored is not None
        assert stored["market_state"] == original["market_state"]
        assert stored["outcome"] == original["outcome"]
        assert len(stored["evidence_hash"]) == 64
        with pytest.raises(ValueError, match="evidence_schema_version"):
            await repo.upsert_evidence(
                _evidence(
                    "wrong-schema",
                    evidence_schema_version="v1464.unknown",
                )
            )
        with pytest.raises(ValueError, match="source_type"):
            await repo.upsert_evidence(
                _evidence("wrong-source", source_type="mainnet_run_event")
            )
        with pytest.raises(ValueError, match="profile_identity_schema"):
            await repo.upsert_evidence(
                _evidence(
                    "wrong-profile-schema",
                    profile_identity_schema="v1463.dynamic-profile",
                )
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sliding_query_uses_observation_time_and_exact_cohort(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        # Terminal is recent, but the opportunity is old: it must not promote.
        await repo.upsert_evidence(
            _evidence("old-late", observed_at_ms=100, terminal_at_ms=950)
        )
        await repo.upsert_evidence(
            _evidence("fresh", observed_at_ms=600, terminal_at_ms=700)
        )
        await repo.upsert_evidence(
            _evidence(
                "wrong-state",
                observed_at_ms=650,
                terminal_at_ms=750,
                market_state="trend",
            )
        )
        await repo.upsert_evidence(
            _evidence(
                "incomplete",
                observed_at_ms=700,
                terminal_at_ms=800,
                data_complete=False,
            )
        )
        await repo.upsert_evidence(
            _evidence(
                "too-slow",
                observed_at_ms=550,
                terminal_at_ms=900,
            )
        )

        eligible = await repo.list_sliding_evidence(
            _cohort(),
            window_start_ms=500,
            activation_cutoff_ms=400,
            as_of_ms=1_000,
            max_terminal_latency_ms=200,
        )
        assert [row["opportunity_id"] for row in eligible] == ["fresh"]

        all_quality = await repo.list_sliding_evidence(
            _cohort(),
            window_start_ms=500,
            as_of_ms=1_000,
            eligible_only=False,
        )
        assert [row["opportunity_id"] for row in all_quality] == [
            "too-slow",
            "fresh",
            "incomplete",
        ]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_lane_paid_query_crosses_exact_cohorts_but_not_lane_scope(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        first = _cohort(
            lane_code="W6A",
            market_state="range-a",
            resolved_profile_hash="profile-paid-a",
        )
        second = _cohort(
            lane_code="W6A",
            market_state="range-b",
            resolved_profile_hash="profile-paid-b",
        )
        other_lane = _cohort(
            lane_code="W6B",
            resolved_profile_hash="profile-other-lane",
        )
        other_symbol = _cohort(
            symbol="BTCUSDC",
            lane_code="W6A",
            resolved_profile_hash="profile-other-symbol",
        )
        rows = (
            _evidence(
                "paid-a",
                cohort=first,
                observed_at_ms=600,
                terminal_at_ms=850,
                outcome="sl_first",
                net_pnl_usdc=-0.30,
                source_type="PAID",
            ),
            _evidence(
                "paid-b",
                cohort=second,
                observed_at_ms=650,
                terminal_at_ms=800,
                outcome="tp1_first",
                net_pnl_usdc=0.20,
                source_type="PAID",
            ),
            _evidence(
                "shadow-same-lane",
                cohort=first,
                observed_at_ms=700,
                terminal_at_ms=750,
                source_type="SHADOW",
            ),
            _evidence(
                "old-paid",
                cohort=first,
                observed_at_ms=100,
                terminal_at_ms=900,
                outcome="sl_first",
                net_pnl_usdc=-1.0,
                source_type="PAID",
            ),
            _evidence(
                "future-terminal",
                cohort=first,
                observed_at_ms=700,
                terminal_at_ms=1_100,
                outcome="sl_first",
                net_pnl_usdc=-1.0,
                source_type="PAID",
                created_at_ms=1_100,
            ),
            _evidence(
                "incomplete-paid",
                cohort=first,
                observed_at_ms=720,
                terminal_at_ms=900,
                outcome="sl_first",
                net_pnl_usdc=-1.0,
                source_type="PAID",
                data_complete=False,
            ),
            _evidence(
                "other-lane-paid",
                cohort=other_lane,
                observed_at_ms=600,
                terminal_at_ms=700,
                outcome="sl_first",
                net_pnl_usdc=-1.0,
                source_type="PAID",
            ),
            _evidence(
                "other-symbol-paid",
                cohort=other_symbol,
                observed_at_ms=600,
                terminal_at_ms=700,
                outcome="sl_first",
                net_pnl_usdc=-1.0,
                source_type="PAID",
            ),
        )
        for row in rows:
            await repo.upsert_evidence(row)

        paid = await repo.list_lane_paid_evidence(
            environment=first.environment,
            symbol=first.symbol,
            lane_code=first.lane_code,
            window_start_ms=500,
            activation_cutoff_ms=550,
            as_of_ms=1_000,
        )
        assert [row["opportunity_id"] for row in paid] == [
            "paid-b",
            "paid-a",
        ]
        assert {row["resolved_profile_hash"] for row in paid} == {
            "profile-paid-a",
            "profile-paid-b",
        }
        assert sum(float(row["net_pnl_usdc"]) for row in paid) == pytest.approx(
            -0.10
        )
        assert paid[-1]["outcome"] == "sl_first"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_lease_grant_renew_is_idempotent_and_generation_cas_is_strict(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        granted_payload = _lease("lease-a")
        granted = await repo.upsert_lease(
            granted_payload,
            expected_generation=0,
            event_type="PROBATION_GRANTED",
            event_time_ms=2_000,
            idempotency_key="grant-a",
            actor="evaluator",
            event_payload={"reason": "shadow_enter_pass"},
        )
        assert granted["generation"] == 1
        assert granted["status"] == "ACTIVE"

        retry = await repo.upsert_lease(
            granted_payload,
            expected_generation=0,
            event_type="PROBATION_GRANTED",
            event_time_ms=2_000,
            idempotency_key="grant-a",
            actor="evaluator",
            event_payload={"reason": "shadow_enter_pass"},
        )
        assert retry["generation"] == 1

        renewed_payload = _lease(
            "lease-a",
            issued_at_ms=2_000,
            renewed_at_ms=2_500,
            expires_at_ms=3_500,
            evidence_as_of_ms=2_500,
            evidence_watermark=9,
        )
        renewed = await repo.upsert_lease(
            renewed_payload,
            expected_generation=1,
            event_type="LEASE_RENEWED",
            event_time_ms=2_500,
            idempotency_key="renew-a",
            actor="evaluator",
        )
        assert renewed["generation"] == 2
        assert renewed["expires_at_ms"] == 3_500
        probation_snapshot = lease_row_to_engine_snapshot(renewed)
        assert probation_snapshot.state.value == "PROBATION"
        assert probation_snapshot.cohort_key == _cohort().key
        assert probation_snapshot.policy_hash == _cohort().promotion_policy_hash

        control_payload = {
            **renewed_payload,
            "phase": "CONTROL",
            "notional_cap_usdc": 50.0,
            "renewed_at_ms": 2_550,
            "expires_at_ms": 3_550,
            "evidence_as_of_ms": 2_550,
        }
        controlled = await repo.upsert_lease(
            control_payload,
            expected_generation=2,
            event_type="CONTROL_GRANTED",
            event_time_ms=2_550,
            idempotency_key="control-a",
            actor="evaluator",
        )
        assert controlled["generation"] == 3
        assert lease_row_to_engine_snapshot(controlled).state.value == "LIVE"

        with pytest.raises(LeaseConflictError, match="generation mismatch"):
            await repo.upsert_lease(
                _lease(
                    "lease-a",
                    issued_at_ms=2_000,
                    renewed_at_ms=2_600,
                    expires_at_ms=3_600,
                    evidence_as_of_ms=2_600,
                ),
                expected_generation=1,
                event_type="LEASE_RENEWED",
                event_time_ms=2_600,
                idempotency_key="stale-renew",
                actor="evaluator",
            )

        events = await repo.list_events(cohort_key=_cohort().key)
        assert [event["event_type"] for event in events] == [
            "PROBATION_GRANTED",
            "LEASE_RENEWED",
            "CONTROL_GRANTED",
        ]
        assert await db.fetchone(
            """SELECT 1 FROM v1464_lane_promotion_events
            WHERE idempotency_key = 'stale-renew'"""
        ) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_demote_and_expire_are_atomic_terminal_transitions(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        first = _cohort()
        await repo.upsert_lease(
            _lease("lease-expire", cohort=first),
            expected_generation=0,
            event_type="PROBATION_GRANTED",
            event_time_ms=2_000,
            idempotency_key="grant-expire",
            actor="evaluator",
        )
        not_yet = await repo.expire_lease(
            first.key,
            expected_generation=1,
            now_ms=2_999,
            idempotency_key="expire-early",
            actor="evaluator",
        )
        assert not_yet is not None and not_yet["status"] == "ACTIVE"
        assert await db.fetchone(
            """SELECT 1 FROM v1464_lane_promotion_events
            WHERE idempotency_key = 'expire-early'"""
        ) is None

        expired = await repo.expire_lease(
            first.key,
            expected_generation=1,
            now_ms=3_000,
            idempotency_key="expire-now",
            actor="evaluator",
        )
        assert expired is not None
        assert expired["status"] == "EXPIRED"
        assert expired["generation"] == 2
        assert expired["demotion_reason"] == "lease_expired"

        second = _cohort(lane_code="W6B", resolved_profile_hash="profile-b")
        await repo.upsert_lease(
            _lease("lease-demote", cohort=second),
            expected_generation=0,
            event_type="PROBATION_GRANTED",
            event_time_ms=2_000,
            idempotency_key="grant-demote",
            actor="evaluator",
        )
        demoted = await repo.demote_lease(
            second.key,
            expected_generation=1,
            reason="regime_mismatch",
            event_time_ms=2_100,
            idempotency_key="demote-now",
            actor="runtime",
        )
        assert demoted is not None
        assert demoted["status"] == "DEMOTED"
        assert demoted["generation"] == 2
        assert demoted["demotion_reason"] == "regime_mismatch"

        events = await repo.list_events(cohort_key=second.key)
        assert [event["event_type"] for event in events] == [
            "PROBATION_GRANTED",
            "DEMOTED",
        ]
        assert events[-1]["generation_before"] == 1
        assert events[-1]["generation_after"] == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cooldown_and_halt_are_durable_with_or_without_active_lease(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        active = _cohort(lane_code="W1B", resolved_profile_hash="profile-cool")
        await repo.upsert_lease(
            _lease("lease-cool", cohort=active),
            expected_generation=0,
            event_type="PROBATION_GRANTED",
            event_time_ms=2_000,
            idempotency_key="grant-cool",
            actor="evaluator",
        )
        cooled = await repo.cooldown_lease(
            active.key,
            expected_generation=1,
            reason="paid_risk_quarantine",
            event_time_ms=2_100,
            cooldown_until_ms=2_700,
            idempotency_key="cool-active",
            actor="runtime",
        )
        assert cooled is not None
        assert cooled["status"] == "COOLDOWN"
        assert cooled["cooldown_until_ms"] == 2_700
        cooled_snapshot = lease_row_to_engine_snapshot(cooled)
        assert cooled_snapshot.state.value == "COOLDOWN"
        assert cooled_snapshot.cooldown_until_ms == 2_700

        fresh = _cohort(lane_code="W2A", resolved_profile_hash="profile-fresh")
        guarded = await repo.upsert_guard_state(
            _lease("lease-guard", cohort=fresh),
            expected_generation=0,
            status="COOLDOWN",
            reason="lane_loss_cap",
            event_time_ms=2_100,
            cooldown_until_ms=2_800,
            idempotency_key="guard-without-active",
            actor="runtime",
        )
        assert guarded["generation"] == 1
        assert guarded["status"] == "COOLDOWN"
        assert lease_row_to_engine_snapshot(guarded).state.value == "COOLDOWN"

        halted_cohort = _cohort(
            lane_code="W6B",
            resolved_profile_hash="profile-halted",
        )
        halted = await repo.upsert_guard_state(
            _lease("lease-halted", cohort=halted_cohort),
            expected_generation=0,
            status="HALTED",
            reason="integrity_unsafe",
            event_time_ms=2_100,
            idempotency_key="halt-without-active",
            actor="runtime",
        )
        assert halted["status"] == "HALTED"
        assert halted["cooldown_until_ms"] is None
        assert lease_row_to_engine_snapshot(halted).state.value == "HALTED"
        events = await repo.list_events(cohort_key=fresh.key)
        assert [event["event_type"] for event in events] == ["COOLDOWN"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_schema_fingerprint_checks_contract_and_append_only_triggers(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        fingerprint = await repo.assert_schema_ready()
        assert len(fingerprint) == 64
        assert await repo.schema_fingerprint() == fingerprint

        await db.conn.execute(
            "DROP TRIGGER trg_v1464_promotion_events_no_delete"
        )
        await db.conn.commit()
        with pytest.raises(PromotionPersistenceError, match="missing_triggers"):
            await repo.assert_schema_ready()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_claim_admission_is_atomic_single_use_and_idempotent(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        cohort = _cohort(lane_code="W1B", resolved_profile_hash="claim-profile")
        await repo.upsert_lease(
            _lease("claim-lease", cohort=cohort),
            expected_generation=0,
            event_type="PROBATION_GRANTED",
            event_time_ms=2_000,
            idempotency_key="claim-grant",
            actor="evaluator",
        )
        claimed = await repo.claim_admission(
            cohort.key,
            lease_id="claim-lease",
            expected_generation=1,
            current_identity=cohort,
            now_ms=2_100,
            actual_notional_usdc=20.0,
            idempotency_key="claim-consume",
            actor="pre_submit",
        )
        assert claimed["generation"] == 2
        assert claimed["claim_granted"] is True
        assert claimed["claim_replayed"] is False
        assert claimed["claim_generation"] == 2
        assert claimed["claimed_notional_usdc"] == 20.0

        replay = await repo.claim_admission(
            cohort.key,
            lease_id="claim-lease",
            expected_generation=1,
            current_identity=cohort,
            now_ms=2_100,
            actual_notional_usdc=20.0,
            idempotency_key="claim-consume",
            actor="pre_submit",
        )
        assert replay["generation"] == 2
        assert replay["claim_granted"] is False
        assert replay["claim_replayed"] is True

        with pytest.raises(AdmissionClaimError, match="generation"):
            await repo.claim_admission(
                cohort.key,
                lease_id="claim-lease",
                expected_generation=1,
                current_identity=cohort,
                now_ms=2_100,
                actual_notional_usdc=20.0,
                idempotency_key="claim-consume-again",
                actor="pre_submit",
            )
        events = await repo.list_events(cohort_key=cohort.key)
        assert [event["event_type"] for event in events] == [
            "PROBATION_GRANTED",
            "ADMISSION_CONSUMED",
        ]
        assert events[-1]["generation_before"] == 1
        assert events[-1]["generation_after"] == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_concurrent_claims_allow_only_one_consumer(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        cohort = _cohort(lane_code="W2A", resolved_profile_hash="race-profile")
        await repo.upsert_lease(
            _lease("race-lease", cohort=cohort),
            expected_generation=0,
            event_type="PROBATION_GRANTED",
            event_time_ms=2_000,
            idempotency_key="race-grant",
            actor="evaluator",
        )

        async def consume(suffix: str):
            return await repo.claim_admission(
                cohort.key,
                lease_id="race-lease",
                expected_generation=1,
                current_identity=cohort,
                now_ms=2_100,
                actual_notional_usdc=25.0,
                idempotency_key=f"race-{suffix}",
                actor="pre_submit",
            )

        results = await asyncio.gather(
            consume("a"),
            consume("b"),
            return_exceptions=True,
        )
        granted = [
            result
            for result in results
            if isinstance(result, dict) and result.get("claim_granted") is True
        ]
        rejected = [
            result for result in results if isinstance(result, AdmissionClaimError)
        ]
        assert len(granted) == 1
        assert len(rejected) == 1
        stored = await repo.get_lease(cohort.key)
        assert stored is not None and stored["generation"] == 2
        events = await repo.list_events(cohort_key=cohort.key)
        assert [event["event_type"] for event in events].count(
            "ADMISSION_CONSUMED"
        ) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_claim_rejects_expired_over_cap_and_identity_mismatch_without_mutation(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        expired = _cohort(
            lane_code="W6B",
            resolved_profile_hash="expired-profile",
        )
        over_cap = _cohort(
            lane_code="W2A",
            resolved_profile_hash="cap-profile",
        )
        identity = _cohort(
            lane_code="W1B",
            resolved_profile_hash="identity-profile",
        )
        for suffix, cohort, payload in (
            (
                "expired",
                expired,
                _lease(
                    "expired-lease",
                    cohort=expired,
                    expires_at_ms=2_100,
                ),
            ),
            ("cap", over_cap, _lease("cap-lease", cohort=over_cap)),
            (
                "identity",
                identity,
                _lease("identity-lease", cohort=identity),
            ),
        ):
            await repo.upsert_lease(
                payload,
                expected_generation=0,
                event_type="PROBATION_GRANTED",
                event_time_ms=2_000,
                idempotency_key=f"{suffix}-grant",
                actor="evaluator",
            )

        with pytest.raises(AdmissionClaimError, match="expired"):
            await repo.claim_admission(
                expired.key,
                lease_id="expired-lease",
                expected_generation=1,
                current_identity=expired,
                now_ms=2_100,
                actual_notional_usdc=20.0,
                idempotency_key="expired-claim",
                actor="pre_submit",
            )
        with pytest.raises(AdmissionClaimError, match="exceeds"):
            await repo.claim_admission(
                over_cap.key,
                lease_id="cap-lease",
                expected_generation=1,
                current_identity=over_cap,
                now_ms=2_100,
                actual_notional_usdc=25.01,
                idempotency_key="cap-claim",
                actor="pre_submit",
            )
        mismatched = _cohort(
            lane_code=identity.lane_code,
            resolved_profile_hash=identity.resolved_profile_hash,
            market_state="different-state",
        )
        with pytest.raises(AdmissionClaimError, match="identity or policy"):
            await repo.claim_admission(
                identity.key,
                lease_id="identity-lease",
                expected_generation=1,
                current_identity=mismatched,
                now_ms=2_100,
                actual_notional_usdc=20.0,
                idempotency_key="identity-claim",
                actor="pre_submit",
            )

        for cohort in (expired, over_cap, identity):
            stored = await repo.get_lease(cohort.key)
            assert stored is not None and stored["generation"] == 1
            events = await repo.list_events(cohort_key=cohort.key)
            assert [event["event_type"] for event in events] == [
                "PROBATION_GRANTED"
            ]
    finally:
        await db.close()
