from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from src.gridbot.mainnet.one_run import MainnetOneRunManager
from src.gridbot.storage.database import Database
from src.gridbot.storage.v1469_arm_observation_repository import (
    ArmEvidenceConflictError,
    ArmObservationConflictError,
    ArmObservationPersistenceError,
    V1469ArmObservationRepository,
    candidate_identity,
)
from src.gridbot.mainnet.v1469_lane_observation import (
    build_v1469_lane_observation,
)
from src.gridbot.strategy.codex_v1_live import select_codex_v1_lane


MIGRATION = Path(
    "src/gridbot/storage/migrations/016_v1469_adaptive_arm_observation.sql"
)


def _opportunity(opportunity_id: str = "opp-1", **overrides) -> dict:
    values = {
        "opportunity_id": opportunity_id,
        "environment": "mainnet",
        "symbol": "ETHUSDC",
        "observed_at_ms": 100,
        "feature_at_ms": 95,
        "coarse_regime": "RANGE",
        "regime_confidence": 0.8,
        "feature_schema": "v1469.feature.1",
        "feature_snapshot": {"score": 71, "rng15": 24.5},
        "source_run_id": "run-1",
        "source_event_id": f"event:{opportunity_id}",
        "data_quality": "COMPLETE",
        "created_at_ms": 100,
    }
    values.update(overrides)
    return values


def _candidate(
    opportunity_id: str = "opp-1",
    *,
    lane_code: str = "W6A",
    **overrides,
) -> dict:
    values = {
        "opportunity_id": opportunity_id,
        "lane_code": lane_code,
        "effective_side": "LONG",
        "strategy": "S1_BB_RSI",
        "match_status": "MATCH",
        "safety_status": "SAFE",
        "is_selected": False,
        "selection_rank": 1,
        "suppression_reason": "shadow_observation_only",
        "suppressed_by_lane_code": None,
        "matcher_version": "v1.4.69",
        "matcher_hash": "matcher-a",
        "data_complete": True,
        "annotations": {"distance": 0.0},
        "created_at_ms": 101,
    }
    values.update(overrides)
    return values


def _evidence(candidate: dict, **overrides) -> dict:
    values = {
        "opportunity_id": candidate["opportunity_id"],
        "candidate_id": candidate_identity(candidate),
        "execution_profile_id": "RANGE_SCALP",
        "execution_profile_schema": "v1469.execution-profile.1",
        "execution_profile_hash": "profile-range-scalp",
        "source_type": "SHADOW",
        "diagnostic_only": False,
        "observed_at_ms": 100,
        "created_at_ms": 101,
    }
    values.update(overrides)
    return values


def _terminal(**overrides) -> dict:
    values = {
        "status": "TERMINAL",
        "terminal_at_ms": 120,
        "outcome": "tp1_first",
        "fill_status": "FILLED",
        "data_complete": True,
        "ambiguous": False,
        "reward_net_bp": 4.2,
        "mfe_bp": 7.0,
        "mae_bp": 1.5,
        "terminal_reason": "tp1_first",
        "terminal_payload": {"fill_age_ms": 2_000},
        "updated_at_ms": 120,
    }
    values.update(overrides)
    return values


async def _repository(
    tmp_path: Path,
) -> tuple[Database, V1469ArmObservationRepository]:
    db = Database(str(tmp_path / "v1469.db"))
    await db.initialize()
    return db, V1469ArmObservationRepository(db)


@pytest.mark.asyncio
async def test_schema_readiness_fingerprint_and_missing_trigger(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        fingerprint = await repo.assert_schema_ready()
        assert len(fingerprint) == 64
        await db.conn.execute(
            "DROP TRIGGER trg_v1469_arm_events_no_update"
        )
        await db.conn.commit()
        with pytest.raises(
            ArmObservationPersistenceError,
            match="missing_triggers",
        ):
            await repo.assert_schema_ready()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_same_bucket_restart_snapshot_does_not_conflict_durably(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    features = {
        "symbol": "ETHUSDC",
        "strategy": "S1_BB_RSI",
        "side": "LONG",
        "score": 70.0,
        "rng15": 40.0,
        "range_bp": 10.0,
        "feature_age_seconds": 0.0,
    }
    selected = select_codex_v1_lane(features)
    try:
        first = build_v1469_lane_observation(
            environment="MAINNET",
            run_id="run-restart",
            observed_at_ms=360_001,
            bucket_seconds=120,
            features=features,
            feature_snapshot=features,
            selector_decision=selected,
            effective_decision=selected,
        )
        second = build_v1469_lane_observation(
            environment="MAINNET",
            run_id="run-restart",
            observed_at_ms=479_999,
            bucket_seconds=120,
            features={**features, "score": 71.0},
            feature_snapshot={**features, "score": 71.0},
            selector_decision=selected,
            effective_decision=selected,
        )
        assert first.dedup_key == second.dedup_key
        assert first.opportunity_id != second.opportunity_id

        first_result = await repo.insert_observation(
            first.opportunity,
            first.candidates,
        )
        second_result = await repo.insert_observation(
            second.opportunity,
            second.candidates,
        )
        assert first_result["opportunity_inserted"] is True
        assert second_result["source_replay"] is True
        assert second_result["durable_opportunity_id"] == first.opportunity_id
        durable = await repo.load_observation_bundle(
            second_result["durable_opportunity_id"]
        )
        assert durable is not None
        stored_opportunity = durable["opportunity"]
        assert stored_opportunity["opportunity_id"] == first.opportunity_id
        assert (
            stored_opportunity["feature_snapshot"]
            == first.opportunity["feature_snapshot"]
        )
        assert (
            stored_opportunity["feature_snapshot"]
            != second.opportunity["feature_snapshot"]
        )
        canonical_snapshot = json.dumps(
            first.opportunity["feature_snapshot"],
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        assert stored_opportunity["feature_hash"] == hashlib.sha256(
            canonical_snapshot.encode("utf-8")
        ).hexdigest()
        expected_candidates = sorted(
            first.candidates,
            key=lambda item: (
                item.get("selection_rank") is None,
                item.get("selection_rank") or 0,
                candidate_identity(item),
            ),
        )
        assert [item["candidate_id"] for item in durable["candidates"]] == [
            candidate_identity(item) for item in expected_candidates
        ]
        assert all(
            isinstance(item["annotations"], dict)
            and isinstance(item["is_selected"], bool)
            and isinstance(item["data_complete"], bool)
            for item in durable["candidates"]
        )
        assert await db.fetchone(
            "SELECT COUNT(*) AS n FROM v1469_market_opportunities"
        ) == {"n": 1}
    finally:
        await db.close()


def test_v1469_migration_is_idempotent_normalized_and_append_guarded() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    sql = MIGRATION.read_text(encoding="utf-8")
    connection.executescript(sql)
    connection.executescript(sql)

    tables = {
        row[0]
        for row in connection.execute(
            """SELECT name FROM sqlite_master
            WHERE type = 'table' AND name LIKE 'v1469_%'"""
        )
    }
    assert tables == {
        "v1469_market_opportunities",
        "v1469_lane_candidates",
        "v1469_arm_evidence",
        "v1469_arm_leases",
        "v1469_arm_events",
    }
    connection.execute(
        """INSERT INTO v1469_market_opportunities (
            opportunity_id, environment, symbol, observed_at_ms, feature_at_ms,
            coarse_regime, regime_confidence, feature_schema, feature_hash,
            feature_snapshot_json, source_run_id, source_event_id,
            data_quality, created_at_ms
        ) VALUES (
            'opp', 'MAINNET', 'ETHUSDC', 100, 90, 'RANGE', 0.8,
            'schema', 'hash', '{}', 'run', 'event', 'COMPLETE', 100
        )"""
    )
    connection.execute(
        """INSERT INTO v1469_lane_candidates (
            candidate_id, opportunity_id, lane_code, effective_side, strategy,
            match_status, safety_status, is_selected, selection_rank,
            suppression_reason, suppressed_by_lane_code, matcher_version,
            matcher_hash, data_complete, annotations_json, created_at_ms
        ) VALUES (
            'candidate', 'opp', 'W6A', 'LONG', 'S1', 'MATCH', 'SAFE',
            0, 1, 'shadow', NULL, 'v1', 'matcher', 1, '{}', 101
        )"""
    )
    connection.execute(
        """INSERT INTO v1469_arm_evidence (
            evidence_id, opportunity_id, candidate_id, arm_key,
            execution_profile_id, execution_profile_schema,
            execution_profile_hash, source_type, diagnostic_only,
            observed_at_ms, status, data_complete, ambiguous,
            created_at_ms, updated_at_ms
        ) VALUES (
            'evidence', 'opp', 'candidate', 'arm', 'RANGE_SCALP',
            'profile-schema', 'profile-hash', 'SHADOW', 0,
            100, 'PENDING', 0, 0, 101, 101
        )"""
    )
    connection.execute(
        """UPDATE v1469_arm_evidence SET
            status = 'TERMINAL', terminal_at_ms = 120,
            outcome = 'tp1_first', fill_status = 'FILLED',
            data_complete = 1, reward_net_bp = 4.0,
            terminal_payload_json = '{}', evidence_hash = 'terminal-hash',
            updated_at_ms = 120
        WHERE evidence_id = 'evidence'"""
    )
    with pytest.raises(sqlite3.IntegrityError, match="already terminal"):
        connection.execute(
            """UPDATE v1469_arm_evidence SET reward_net_bp = 999
            WHERE evidence_id = 'evidence'"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        connection.execute(
            "DELETE FROM v1469_arm_evidence WHERE evidence_id = 'evidence'"
        )

    connection.execute(
        """INSERT INTO v1469_arm_events (
            idempotency_key, arm_key, opportunity_id, candidate_id,
            event_time_ms, event_type, actor, payload_json
        ) VALUES (
            'event-1', 'arm', 'opp', 'candidate',
            110, 'OBSERVED', 'test', '{}'
        )"""
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            "UPDATE v1469_arm_events SET actor = 'mutated'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM v1469_arm_events")
    connection.close()


@pytest.mark.asyncio
async def test_paired_evidence_bundle_is_atomic_and_restart_query_is_bounded(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    opportunity = _opportunity()
    candidate = _candidate()
    range_payload = _evidence(candidate)
    balanced_payload = _evidence(
        candidate,
        execution_profile_id="PASSIVE_BALANCED",
        execution_profile_hash="profile-passive-balanced",
    )
    third_payload = _evidence(
        candidate,
        execution_profile_id="TREND_PARTIAL",
        execution_profile_hash="profile-trend-partial",
    )
    try:
        await repo.insert_observation(opportunity, [candidate])
        result = await repo.append_evidence_bundle(
            [range_payload, balanced_payload]
        )
        assert result["inserted"] == 2
        assert result["existing"] == 0
        assert result["count"] == 2

        retry = await repo.append_evidence_bundle(
            [range_payload, balanced_payload]
        )
        assert retry["inserted"] == 0
        assert retry["existing"] == 2

        pending = await repo.list_pending_evidence(
            environment="MAINNET",
            symbol="ETHUSDC",
            source_run_id="run-1",
            observed_after_ms=0,
            limit=10,
        )
        assert {row["execution_profile_id"] for row in pending} == {
            "RANGE_SCALP",
            "PASSIVE_BALANCED",
        }
        assert all(row["feature_snapshot"]["score"] == 71 for row in pending)

        range_row = next(
            row
            for row in result["evidence"]
            if row["execution_profile_id"] == "RANGE_SCALP"
        )
        await repo.terminal_evidence(
            range_row["evidence_id"],
            _terminal(),
        )
        terminal = await repo.terminal_evidence_window(
            environment="MAINNET",
            symbol="ETHUSDC",
            window_start_ms=0,
            as_of_ms=1_000,
            limit=10,
        )
        assert [row["execution_profile_id"] for row in terminal] == [
            "RANGE_SCALP"
        ]
        balanced_row = next(
            row
            for row in result["evidence"]
            if row["execution_profile_id"] == "PASSIVE_BALANCED"
        )
        terminal_bundle = await repo.terminal_evidence_bundle(
            [
                {
                    "evidence_id": range_row["evidence_id"],
                    "terminal": _terminal(),
                },
                {
                    "evidence_id": balanced_row["evidence_id"],
                    "terminal": _terminal(
                        terminal_at_ms=130,
                        updated_at_ms=130,
                        outcome="sl",
                        reward_net_bp=-8.0,
                        terminal_reason="SL",
                    ),
                },
            ]
        )
        assert terminal_bundle == {
            "updated": 1,
            "existing": 1,
            "count": 2,
        }

        with pytest.raises(
            ArmEvidenceConflictError,
            match="already-terminal",
        ):
            await repo.append_evidence_bundle(
                [range_payload, third_payload]
            )
        count = await db.fetchone(
            """SELECT COUNT(*) AS n FROM v1469_arm_evidence
            WHERE execution_profile_id = 'TREND_PARTIAL'"""
        )
        assert count == {"n": 0}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_opportunity_and_candidate_exact_retry_conflict_and_fk(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        opportunity = _opportunity()
        assert await repo.insert_opportunity(opportunity) is True
        assert await repo.insert_opportunity(
            {**opportunity, "created_at_ms": 105}
        ) is False
        with pytest.raises(
            ArmObservationConflictError, match="conflicting durable"
        ):
            await repo.insert_opportunity(
                {**opportunity, "coarse_regime": "TREND"}
            )
        with pytest.raises(
            ArmObservationConflictError, match="source event"
        ):
            await repo.insert_opportunity(
                _opportunity("opp-2", source_event_id="event:opp-1")
            )

        candidate = _candidate()
        assert await repo.insert_candidate(candidate) is True
        assert await repo.insert_candidate(
            {**candidate, "created_at_ms": 110}
        ) is False
        with pytest.raises(
            ArmObservationConflictError, match="conflicting lane"
        ):
            await repo.insert_candidate(
                {**candidate, "suppression_reason": "different"}
            )
        with pytest.raises(
            ArmObservationConflictError, match="does not exist"
        ):
            await repo.insert_candidate(_candidate("missing"))
        selected_blocked = _candidate(
            lane_code="ANCHOR-S",
            matcher_hash="matcher-selected-blocked",
            is_selected=True,
            safety_status="HARD_BLOCK",
            data_complete=False,
            suppression_reason="legacy_lane_disabled",
        )
        assert await repo.insert_candidate(selected_blocked) is True
        stored_blocked = await db.fetchone(
            """SELECT is_selected, safety_status, data_complete
            FROM v1469_lane_candidates WHERE candidate_id = ?""",
            (candidate_identity(selected_blocked),),
        )
        assert stored_blocked == {
            "is_selected": 1,
            "safety_status": "HARD_BLOCK",
            "data_complete": 0,
        }
        with pytest.raises(ValueError, match="selected candidate must be matched"):
            await repo.insert_candidate(
                _candidate(is_selected=True, match_status="NEAR_MATCH")
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_observation_bundle_exact_retry_is_all_noop(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        opportunity = _opportunity()
        candidates = [
            _candidate(lane_code="W6A", matcher_hash="matcher-w6a"),
            _candidate(lane_code="W1B", matcher_hash="matcher-w1b"),
        ]
        first = await repo.insert_observation(opportunity, candidates)
        assert first == {
            "opportunity_inserted": True,
            "opportunity_existing": False,
            "candidates_inserted": 2,
            "candidates_existing": 0,
            "candidate_count": 2,
        }
        replay = await repo.insert_observation(
            {**opportunity, "created_at_ms": 110},
            [
                {**candidate, "created_at_ms": 111}
                for candidate in candidates
            ],
        )
        assert replay == {
            "opportunity_inserted": False,
            "opportunity_existing": True,
            "candidates_inserted": 0,
            "candidates_existing": 2,
            "candidate_count": 2,
        }
        assert (
            await db.fetchone(
                "SELECT COUNT(*) AS n FROM v1469_market_opportunities"
            )
        ) == {"n": 1}
        assert (
            await db.fetchone(
                "SELECT COUNT(*) AS n FROM v1469_lane_candidates"
            )
        ) == {"n": 2}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_observation_bundle_candidate_conflict_rolls_back_all_new_rows(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        await repo.insert_opportunity(_opportunity("opp-old"))
        opportunity = _opportunity("opp-new")
        first_candidate = _candidate(
            "opp-new",
            lane_code="W6A",
            matcher_hash="matcher-first",
        )
        conflicting_candidate = _candidate(
            "opp-new",
            lane_code="W1B",
            matcher_hash="matcher-conflict",
        )
        conflicting_id = candidate_identity(conflicting_candidate)
        # Simulate a pre-existing corrupt/legacy identity collision.  The
        # bundle must insert neither the new opportunity nor the candidate
        # that precedes this conflict in the fan-out.
        await db.conn.execute(
            """INSERT INTO v1469_lane_candidates (
                candidate_id, opportunity_id, lane_code, effective_side,
                strategy, match_status, safety_status, is_selected,
                selection_rank, suppression_reason, suppressed_by_lane_code,
                matcher_version, matcher_hash, data_complete,
                annotations_json, created_at_ms
            ) VALUES (
                ?, 'opp-old', 'W1B', 'LONG', 'S1_BB_RSI', 'MATCH', 'SAFE',
                0, 1, 'legacy-corruption', NULL, 'v1.4.69',
                'matcher-conflict', 1, '{}', 101
            )""",
            (conflicting_id,),
        )
        await db.conn.commit()

        with pytest.raises(
            ArmObservationConflictError, match="conflicting lane candidate"
        ):
            await repo.insert_observation(
                opportunity,
                [first_candidate, conflicting_candidate],
            )
        assert await db.fetchone(
            """SELECT opportunity_id FROM v1469_market_opportunities
            WHERE opportunity_id = 'opp-new'"""
        ) is None
        assert await db.fetchone(
            """SELECT candidate_id FROM v1469_lane_candidates
            WHERE candidate_id = ?""",
            (candidate_identity(first_candidate),),
        ) is None
        assert await db.fetchone(
            """SELECT opportunity_id FROM v1469_lane_candidates
            WHERE candidate_id = ?""",
            (conflicting_id,),
        ) == {"opportunity_id": "opp-old"}
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_single_evidence_append_rejects_legacy_without_sidecar(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        opportunity = _opportunity()
        candidate = _candidate()
        await repo.insert_opportunity(opportunity)
        await repo.insert_candidate(candidate)
        with pytest.raises(
            ValueError,
            match="append_evidence_bundle sidecar",
        ):
            await repo.append_evidence(
                {
                    **_evidence(candidate),
                    "execution_profile_id": "LEGACY_CONTROL",
                }
            )
        assert await db.fetchone(
            "SELECT COUNT(*) AS n FROM v1469_arm_evidence"
        ) == {"n": 0}
    finally:
        await db.close()

@pytest.mark.asyncio
async def test_evidence_append_terminal_is_idempotent_and_conflict_safe(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        opportunity = _opportunity()
        candidate = _candidate()
        await repo.insert_opportunity(opportunity)
        await repo.insert_candidate(candidate)
        evidence = _evidence(candidate)
        assert await repo.append_evidence(evidence) is True
        assert await repo.append_evidence(
            {**evidence, "created_at_ms": 105}
        ) is False
        row = await db.fetchone("SELECT * FROM v1469_arm_evidence")
        assert row is not None
        assert row["status"] == "PENDING"
        assert row["arm_key"].startswith("v1469a_")

        assert await repo.terminal_evidence(
            row["evidence_id"], _terminal()
        ) is True
        assert await repo.terminal_evidence(
            row["evidence_id"],
            _terminal(updated_at_ms=130),
        ) is False
        with pytest.raises(
            ArmEvidenceConflictError, match="conflicting terminal"
        ):
            await repo.terminal_evidence(
                row["evidence_id"],
                _terminal(outcome="sl_first", reward_net_bp=-8.0),
            )
        stored = await db.fetchone(
            """SELECT status, outcome, reward_net_bp, evidence_hash
            FROM v1469_arm_evidence"""
        )
        assert stored is not None
        assert stored["status"] == "TERMINAL"
        assert stored["outcome"] == "tp1_first"
        assert stored["reward_net_bp"] == pytest.approx(4.2)
        assert len(stored["evidence_hash"]) == 64
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_monitor_summary_and_compact_event_idempotency(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        await repo.insert_opportunity(_opportunity())
        w6a = _candidate()
        w1b = _candidate(
            lane_code="W1B",
            matcher_hash="matcher-b",
            match_status="MATCH",
            safety_status="HARD_BLOCK",
            is_selected=True,
            data_complete=False,
            suppression_reason="shock_veto",
        )
        await repo.insert_candidate(w6a)
        await repo.insert_candidate(w1b)
        await repo.append_evidence(_evidence(w6a))
        await repo.append_evidence(
            _evidence(
                w6a,
                execution_profile_id="PASSIVE_BALANCED",
                execution_profile_hash="profile-passive-balanced",
            )
        )
        evidence = await db.fetchone(
            """SELECT evidence_id, arm_key FROM v1469_arm_evidence
            WHERE execution_profile_id = 'RANGE_SCALP'"""
        )
        assert evidence is not None
        await repo.terminal_evidence(evidence["evidence_id"], _terminal())

        event = {
            "idempotency_key": "observed-1",
            "arm_key": evidence["arm_key"],
            "opportunity_id": "opp-1",
            "candidate_id": candidate_identity(w6a),
            "generation_before": None,
            "generation_after": None,
            "event_time_ms": 105,
            "event_type": "OBSERVED",
            "actor": "test",
            "payload": {"profile": "RANGE_SCALP"},
        }
        assert await repo.append_arm_event(event) is True
        assert await repo.append_arm_event(event) is False
        with pytest.raises(
            ArmObservationConflictError, match="idempotency key"
        ):
            await repo.append_arm_event({**event, "actor": "other"})

        summary = await repo.get_monitor_summary(
            environment="mainnet",
            symbol="ETHUSDC",
            window_start_ms=0,
            as_of_ms=200,
        )
        assert summary["opportunities"] == {
            "opportunities": 1,
            "complete_opportunities": 1,
            "regimes": 1,
            "last_observed_at_ms": 100,
        }
        by_lane = {row["lane_code"]: row for row in summary["lanes"]}
        assert by_lane["W6A"]["candidates"] == 1
        assert by_lane["W6A"]["matched"] == 1
        assert by_lane["W6A"]["terminal"] == 1
        assert by_lane["W6A"]["pending"] == 1
        assert by_lane["W6A"]["evidence"] == 2
        assert by_lane["W6A"]["evaluable"] == 1
        assert by_lane["W6A"]["evaluable_reward_net_bp"] == pytest.approx(4.2)
        assert by_lane["W1B"]["hard_blocked"] == 1
        assert by_lane["W1B"]["selected"] == 1
        assert len(summary["arms"]) == 2
        assert {
            row["execution_profile_id"]: row["evidence"]
            for row in summary["arms"]
        } == {"PASSIVE_BALANCED": 1, "RANGE_SCALP": 1}
        assert summary["outcomes"] == [
            {"lane_code": "W6A", "outcome": "tp1_first", "samples": 1}
        ]
        assert summary["suppressed_by"] == [
            {
                "lane_code": "W1B",
                "suppressed_by_lane_code": "UNSPECIFIED",
                "candidates": 1,
            },
            {
                "lane_code": "W6A",
                "suppressed_by_lane_code": "UNSPECIFIED",
                "candidates": 1,
            },
        ]
        assert summary["leases"] == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_json_size_limits_and_feature_hash_are_enforced(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        with pytest.raises(ValueError, match="feature_hash"):
            await repo.insert_opportunity(
                _opportunity(feature_hash="not-the-snapshot-hash")
            )
        with pytest.raises(ValueError, match="32768 bytes"):
            await repo.insert_opportunity(
                _opportunity(feature_snapshot={"raw": "x" * 33_000})
            )
    finally:
        await db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("coarse_regime", ["TREND_UP", "TREND_DOWN"])
async def test_directional_regime_round_trips_into_arm_identity(
    tmp_path: Path,
    coarse_regime: str,
) -> None:
    db, repo = await _repository(tmp_path)
    try:
        opportunity = _opportunity(coarse_regime=coarse_regime)
        candidate = _candidate()
        await repo.insert_opportunity(opportunity)
        await repo.insert_candidate(candidate)
        assert await repo.append_evidence(_evidence(candidate)) is True
        stored = await db.fetchone(
            """SELECT o.coarse_regime, e.arm_key
            FROM v1469_market_opportunities o
            JOIN v1469_arm_evidence e USING(opportunity_id)"""
        )
        assert stored is not None
        assert stored["coarse_regime"] == coarse_regime
        assert stored["arm_key"].startswith("v1469a_")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_data_blocked_exact_snapshot_reason_round_trips_in_candidate_annotations(
    tmp_path: Path,
) -> None:
    db, repo = await _repository(tmp_path)
    manager = object.__new__(MainnetOneRunManager)
    manager._v1469_observation_tasks = set()
    manager._v1469_observation_inflight_ids = set()
    manager._v1469_observed_opportunity_ids = set()
    manager._v1469_paired_shadow_runtime = None
    reason = "exact_snapshot_data_blocked:unsupported_gtc_fallback"
    opportunity = _opportunity()
    candidate = _candidate()
    original_snapshot = dict(opportunity["feature_snapshot"])
    canonical_snapshot = json.dumps(
        original_snapshot,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    original_hash = hashlib.sha256(
        canonical_snapshot.encode("utf-8")
    ).hexdigest()

    try:
        manager._v1469_start_adaptive_only_observation(
            repo,
            "run-1",
            "dedup-1",
            opportunity,
            (candidate,),
            reason=reason,
        )
        task = next(iter(manager._v1469_observation_tasks))
        await task

        stored_opportunity = await db.fetchone(
            """SELECT data_quality, feature_snapshot_json, feature_hash
            FROM v1469_market_opportunities
            WHERE opportunity_id = ?""",
            (opportunity["opportunity_id"],),
        )
        assert stored_opportunity == {
            "data_quality": "DATA_INCOMPLETE",
            "feature_snapshot_json": canonical_snapshot,
            "feature_hash": original_hash,
        }
        stored_candidate = await db.fetchone(
            """SELECT safety_status, data_complete, annotations_json
            FROM v1469_lane_candidates
            WHERE candidate_id = ?""",
            (candidate_identity(candidate),),
        )
        assert stored_candidate is not None
        assert stored_candidate["safety_status"] == "DATA_BLOCKED"
        assert stored_candidate["data_complete"] == 0
        assert json.loads(stored_candidate["annotations_json"])[
            "exact_snapshot_data_blocked"
        ] == reason
    finally:
        await db.close()


def test_migration_020_sidecar_is_rerunnable_and_append_only() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(MIGRATION.read_text(encoding="utf-8"))
    migration_020 = Path(
        "src/gridbot/storage/migrations/020_v1469_legacy_execution_snapshot.sql"
    ).read_text(encoding="utf-8")
    connection.executescript(migration_020)
    connection.executescript(migration_020)

    connection.execute(
        """INSERT INTO v1469_market_opportunities (
            opportunity_id, environment, symbol, observed_at_ms, feature_at_ms,
            coarse_regime, regime_confidence, feature_schema, feature_hash,
            feature_snapshot_json, source_run_id, source_event_id,
            data_quality, created_at_ms
        ) VALUES (
            'opp-020', 'MAINNET', 'ETHUSDC', 100, 90, 'RANGE', 0.8,
            'schema', 'hash', '{}', 'run-020', 'event-020', 'COMPLETE', 100
        )"""
    )
    connection.execute(
        """INSERT INTO v1469_lane_candidates (
            candidate_id, opportunity_id, lane_code, effective_side, strategy,
            match_status, safety_status, is_selected, selection_rank,
            suppression_reason, suppressed_by_lane_code, matcher_version,
            matcher_hash, data_complete, annotations_json, created_at_ms
        ) VALUES (
            'candidate-020', 'opp-020', 'W6A', 'LONG', 'S1_BB_RSI',
            'MATCH', 'SAFE', 1, 0, NULL, NULL, 'v1', 'matcher', 1, '{}', 100
        )"""
    )
    profile_hash = "a" * 64
    connection.execute(
        """INSERT INTO v1469_arm_evidence (
            evidence_id, opportunity_id, candidate_id, arm_key,
            execution_profile_id, execution_profile_schema,
            execution_profile_hash, source_type, diagnostic_only,
            observed_at_ms, status, data_complete, ambiguous,
            created_at_ms, updated_at_ms
        ) VALUES (
            'evidence-020', 'opp-020', 'candidate-020', 'arm-020',
            'LEGACY_CONTROL', 'v1469.execution-profile.1', ?, 'SHADOW', 0,
            100, 'PENDING', 0, 0, 100, 100
        )""",
        (profile_hash,),
    )
    connection.execute(
        """INSERT INTO v1469_arm_evidence_profile_payloads (
            evidence_id, opportunity_id, candidate_id, source_type,
            execution_profile_id, execution_profile_schema,
            execution_profile_hash, canonical_payload_json, created_at_ms
        ) VALUES (
            'evidence-020', 'opp-020', 'candidate-020', 'SHADOW',
            'LEGACY_CONTROL', 'v1469.execution-profile.1', ?, '{}', 100
        )""",
        (profile_hash,),
    )
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            """UPDATE v1469_arm_evidence_profile_payloads
            SET canonical_payload_json = '{\"changed\":true}'
            WHERE evidence_id = 'evidence-020'"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            """DELETE FROM v1469_arm_evidence_profile_payloads
            WHERE evidence_id = 'evidence-020'"""
        )
    connection.close()
