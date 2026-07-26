from __future__ import annotations

import asyncio
from pathlib import Path
import sqlite3

import pytest

from src.gridbot.storage.database import Database
from src.gridbot.storage.v1465_w6a_profile_repository import (
    V1465W6AProfileRepository,
    W6AProfileConflictError,
    W6AProfilePersistenceError,
    W6ASelectionConflictError,
    W6ASelector,
)


MIGRATION = Path("src/gridbot/storage/migrations/014_v1465_w6a_profile_selector.sql")


def _selector(**overrides) -> W6ASelector:
    values = dict(environment="mainnet", symbol="ETHUSDC", lane_code="W6A",
                  market_state="supportive_range", effective_side="LONG",
                  strategy="S1_BB_RSI")
    values.update(overrides)
    return W6ASelector(**values)


def _evidence(evidence_id: str, *, opportunity_id: str = "opp-1", profile: str = "a", observed: int = 100, terminal: int = 120, **overrides) -> dict:
    scope = _selector()
    values = dict(evidence_id=evidence_id, opportunity_id=opportunity_id,
                  environment=scope.environment, symbol=scope.symbol, lane_code=scope.lane_code,
                  market_state=scope.market_state, effective_side=scope.effective_side,
                  strategy=scope.strategy, profile_id=f"profile-{profile}",
                  resolved_profile_hash=f"hash-{profile}", profile_plan_hash=f"plan-{profile}",
                  observed_at_ms=observed, terminal_at_ms=terminal, outcome="tp1_first",
                  data_complete=True, ambiguous=False, diagnostic_only=False,
                  net_pnl_bp=4.5, source_payload={"id": evidence_id}, created_at_ms=terminal)
    values.update(overrides)
    return values


def _selection(*, profile: str = "a", status: str = "PROBATION", renewed: int = 200, expires: int = 300, **overrides) -> dict:
    scope = _selector()
    values = dict(environment=scope.environment, symbol=scope.symbol, lane_code=scope.lane_code,
                  market_state=scope.market_state, effective_side=scope.effective_side,
                  strategy=scope.strategy, winner_profile_id=f"profile-{profile}",
                  winner_resolved_profile_hash=f"hash-{profile}", status=status,
                  notional_cap_usdc=20.0, issued_at_ms=200, renewed_at_ms=renewed,
                  expires_at_ms=expires, evidence_revision="revision-1",
                  evidence_snapshot={"wins": 3}, policy_hash="policy-1",
                  owner_id="owner-1", boot_id="boot-1", demotion_reason=None,
                  demoted_at_ms=None, cooldown_until_ms=None)
    values.update(overrides)
    return values


async def _repo(tmp_path: Path) -> tuple[Database, V1465W6AProfileRepository]:
    db = Database(str(tmp_path / "w6a.db"))
    await db.initialize()
    return db, V1465W6AProfileRepository(db)


def test_migration_is_idempotent_allows_three_profiles_and_guards_events() -> None:
    connection = sqlite3.connect(":memory:")
    sql = MIGRATION.read_text(encoding="utf-8")
    connection.executescript(sql)
    connection.executescript(sql)
    for profile in ("a", "b", "c"):
        connection.execute(
            """INSERT INTO v1465_w6a_profile_evidence (
                evidence_id, opportunity_id, environment, symbol, lane_code, market_state,
                effective_side, strategy, profile_id, resolved_profile_hash, profile_plan_hash,
                observed_at_ms, terminal_at_ms, outcome, data_complete, ambiguous,
                diagnostic_only, net_pnl_bp, source_payload_json, evidence_hash, created_at_ms
            ) VALUES (?, 'same-opp', 'mainnet', 'ETHUSDC', 'W6A', 'range', 'LONG',
                'S1', ?, ?, 'plan', 1, 2, 'tp', 1, 0, 0, 1.0, '{}', ?, 2)""",
            (f"e-{profile}", profile, f"hash-{profile}", f"evidence-{profile}"),
        )
    assert connection.execute("SELECT count(*) FROM v1465_w6a_profile_evidence").fetchone()[0] == 3
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE v1465_w6a_profile_evidence "
            "SET net_pnl_bp = 999 WHERE evidence_id = 'e-a'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "DELETE FROM v1465_w6a_profile_evidence "
            "WHERE evidence_id = 'e-a'"
        )
    connection.execute("""INSERT INTO v1465_w6a_profile_selection_events
        (idempotency_key, selector_key, event_time_ms, event_type, actor, payload_json)
        VALUES ('event-1', 'selector', 1, 'GRANTED', 'test', '{}')""")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("UPDATE v1465_w6a_profile_selection_events SET actor = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute("DELETE FROM v1465_w6a_profile_selection_events")
    connection.close()


@pytest.mark.asyncio
async def test_evidence_replay_conflict_and_window_query(tmp_path: Path) -> None:
    db, repo = await _repo(tmp_path)
    try:
        original = _evidence("e-a", observed=100, terminal=120)
        assert await repo.upsert_evidence(original) is True
        assert await repo.upsert_evidence(original) is False
        await repo.upsert_evidence(_evidence("e-b", profile="b", observed=150, terminal=160))
        await repo.upsert_evidence(_evidence("e-old", profile="c", observed=10, terminal=180))
        await repo.upsert_evidence(_evidence("e-incomplete", profile="c", opportunity_id="opp-2", observed=170, terminal=180, data_complete=False))
        visible = await repo.list_evidence(_selector(), window_start_ms=90, as_of_ms=200)
        assert [row["evidence_id"] for row in visible] == ["e-a", "e-b"]
        assert [row["evidence_id"] for row in await repo.list_evidence(_selector(), window_start_ms=90, as_of_ms=200, resolved_profile_hash="hash-b")] == ["e-b"]
        with pytest.raises(W6AProfileConflictError, match="conflicting"):
            await repo.upsert_evidence({**original, "outcome": "sl"})
        with pytest.raises(W6AProfileConflictError, match="conflicting"):
            await repo.upsert_evidence({**original, "created_at_ms": 130})
        with pytest.raises(W6AProfileConflictError, match="opportunity/profile"):
            await repo.upsert_evidence(_evidence("other-id", opportunity_id="opp-1", profile="a"))
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_selection_grant_renew_switch_idempotency_and_aba_cas(tmp_path: Path) -> None:
    db, repo = await _repo(tmp_path)
    try:
        granted = await repo.grant_selection(_selection(), expected_generation=0,
                                             event_time_ms=200, idempotency_key="grant", actor="eval")
        assert granted["generation"] == 1
        replay = await repo.grant_selection(_selection(), expected_generation=0,
                                            event_time_ms=200, idempotency_key="grant", actor="eval")
        assert replay["generation"] == 1
        renewed = await repo.renew_selection(_selection(renewed=220, expires=330), expected_generation=1,
                                             event_time_ms=220, idempotency_key="renew", actor="eval")
        assert renewed["generation"] == 2
        switched = await repo.switch_selection(_selection(profile="b", renewed=230, expires=340), expected_generation=2,
                                               event_time_ms=230, idempotency_key="switch", actor="eval")
        assert switched["generation"] == 3 and switched["winner_profile_id"] == "profile-b"
        historic_replay = await repo.grant_selection(
            _selection(),
            expected_generation=0,
            event_time_ms=200,
            idempotency_key="grant",
            actor="eval",
        )
        assert historic_replay["generation"] == 1
        assert historic_replay["winner_profile_id"] == "profile-a"
        with pytest.raises(W6ASelectionConflictError, match="generation"):
            await repo.renew_selection(_selection(renewed=240, expires=350), expected_generation=1,
                                       event_time_ms=240, idempotency_key="stale", actor="eval")
        events = await repo.list_selection_events(selector_key=_selector().key)
        assert [event["event_type"] for event in events] == ["GRANTED", "RENEWED", "SWITCHED"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_selection_demote_expire_and_schema_contract(tmp_path: Path) -> None:
    db, repo = await _repo(tmp_path)
    try:
        await repo.grant_selection(_selection(), expected_generation=0, event_time_ms=200, idempotency_key="grant", actor="eval")
        early = await repo.expire_selection(_selector(), expected_generation=1, now_ms=299, idempotency_key="too-early", actor="runtime")
        assert early is not None and early["status"] == "PROBATION"
        demoted = await repo.demote_selection(_selector(), expected_generation=1, reason="risk", event_time_ms=250,
                                               cooldown_until_ms=270, idempotency_key="demote", actor="runtime")
        assert demoted is not None and demoted["status"] == "DEMOTED" and demoted["generation"] == 2
        expired = await repo.expire_selection(_selector(), expected_generation=2, now_ms=300, idempotency_key="expire", actor="runtime")
        assert expired is not None and expired["status"] == "EXPIRED" and expired["generation"] == 3
        fingerprint = await repo.assert_schema_ready()
        assert len(fingerprint) == 64
        await db.conn.execute("DROP TRIGGER trg_v1465_w6a_selection_events_no_delete"); await db.conn.commit()
        with pytest.raises(W6AProfilePersistenceError, match="missing_triggers"):
            await repo.assert_schema_ready()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_concurrent_single_selector_allows_one_winner(tmp_path: Path) -> None:
    db, repo = await _repo(tmp_path)
    try:
        async def grant(profile: str):
            return await repo.grant_selection(_selection(profile=profile), expected_generation=0,
                                              event_time_ms=200, idempotency_key=f"grant-{profile}", actor="eval")
        results = await asyncio.gather(grant("a"), grant("b"), return_exceptions=True)
        successful = [result for result in results if isinstance(result, dict)]
        failures = [result for result in results if isinstance(result, W6ASelectionConflictError)]
        assert len(successful) == 1 and len(failures) == 1
        stored = await repo.get_selection(_selector())
        assert stored is not None and stored["generation"] == 1
        assert len(await repo.list_selection_events(selector_key=_selector().key)) == 1
    finally:
        await db.close()
