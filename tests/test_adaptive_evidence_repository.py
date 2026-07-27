import json
from hashlib import sha256
import sqlite3

import pytest

from src.gridbot.storage.adaptive_evidence_repository import AdaptiveEvidenceRepository
from src.gridbot.storage.database import Database


def _feature_hash(features: dict) -> str:
    encoded = json.dumps(
        features,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _session(session_id: str = "session-a", **overrides) -> dict:
    value = {
        "session_id": session_id,
        "environment": "mainnet",
        "account_fingerprint": "acct-sha256",
        "database_identity": "gridbot_mainnet.db",
        "exchange_endpoint": "https://fapi.binance.com",
        "is_testnet": False,
        "symbol": "BTCUSDC",
        "account_mode": "ONE_WAY",
        "deployment_commit": "abc123",
        "code_version": "v1.4.59",
        "config_sha256": "config-sha256",
        "status": "ACTIVE",
        "started_at_ms": 1_000,
        "last_checkpoint_at_ms": 1_001,
        "revision": 0,
        "counters": {"accepted": 1},
        "disabled_states": ["STATE_X"],
        "route_stats": {"route": {"pnl": 0}},
    }
    value.update(overrides)
    return value


def _opportunity(session_id: str = "session-a", opportunity_id: str = "opp-a", **overrides) -> dict:
    feature_snapshot = overrides.pop("feature_snapshot", {"rng15_bp": 20.0})
    value = {
        "session_id": session_id,
        "opportunity_id": opportunity_id,
        "observed_at_ms": 2_000,
        "decision_at_ms": 2_000,
        "source_run_id": "run-a",
        "opportunity_bucket": 33,
        "feature_hash": _feature_hash(feature_snapshot),
        "feature_snapshot": feature_snapshot,
        "feature_timestamps": {"rng15_bp": 1_990},
        "evidence_contract_version": "v1459-opportunity-evidence-v2",
        "outcome_blind": True,
        "symbol": "BTCUSDC",
        "side": "BUY",
        "lane_code": "STUP-S",
        "market_state": "TREND",
        "decision_schema_version": "v1",
        "action_schema": {"entry_offset_bp": 2},
        "raw_decision": {"accepted": False},
        "effective_decision": {"action": "BLOCK"},
        "quality_status": "OBSERVED",
    }
    value.update(overrides)
    return value


async def _repo(tmp_path):
    db = Database(str(tmp_path / "adaptive-evidence.db"))
    await db.initialize()
    return db, AdaptiveEvidenceRepository(db)


@pytest.mark.asyncio
async def test_fresh_database_applies_adaptive_evidence_migration(tmp_path):
    db, _ = await _repo(tmp_path)
    try:
        rows = await db.fetchall(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
        names = {row["name"] for row in rows}
        assert {
            "adaptive_sessions",
            "adaptive_opportunities",
            "shadow_evaluations",
            "run_reconciliations",
            "run_reconciliation_exchange_trades",
            "run_reconciliation_exchange_income",
        } <= names
        migration = await db.fetchone(
            "SELECT filename FROM _migrations WHERE filename = ?",
            ("006_adaptive_foundation_shadow.sql",),
        )
        assert migration == {"filename": "006_adaptive_foundation_shadow.sql"}
        evidence_v2 = await db.fetchone(
            "SELECT filename FROM _migrations WHERE filename = ?",
            ("007_adaptive_opportunity_evidence_v2.sql",),
        )
        assert evidence_v2 == {
            "filename": "007_adaptive_opportunity_evidence_v2.sql"
        }
        columns = {
            row["name"]
            for row in await db.fetchall("PRAGMA table_info(adaptive_opportunities)")
        }
        assert {"source_run_id", "opportunity_bucket", "decision_at_ms",
                "feature_snapshot_json", "feature_timestamps_json",
                "evidence_contract_version", "outcome_blind"} <= columns
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_session_insert_revision_guard_and_immutable_identity(tmp_path):
    db, repo = await _repo(tmp_path)
    try:
        assert await repo.upsert_session(_session()) is True
        initial = await repo.get_session("session-a")
        assert initial is not None
        assert initial["is_testnet"] == 0
        assert initial["counters_json"] == '{"accepted":1}'
        assert initial["created_at_ms"] > 0

        assert await repo.upsert_session(
            _session(
                status="PAUSED_REQUIRES_ACK",
                revision=1,
                last_checkpoint_at_ms=1_100,
                counters={"accepted": 2, "blocked": 3},
                net_pnl_usdc=1.25,
            )
        ) is True
        updated = await repo.get_session("session-a")
        assert updated["status"] == "PAUSED_REQUIRES_ACK"
        assert updated["revision"] == 1
        assert updated["net_pnl_usdc"] == pytest.approx(1.25)
        assert json.loads(updated["counters_json"]) == {"accepted": 2, "blocked": 3}
        assert updated["created_at_ms"] == initial["created_at_ms"]

        assert await repo.upsert_session(_session(revision=1, status="ACTIVE")) is False
        assert await repo.upsert_session(_session(revision=0, status="ACTIVE")) is False
        assert await repo.upsert_session(
            _session(revision=2, account_fingerprint="other-account", status="ACTIVE")
        ) is False
        unchanged = await repo.get_session("session-a")
        assert unchanged["revision"] == 1
        assert unchanged["account_fingerprint"] == "acct-sha256"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_open_session_scope_and_partial_unique_index(tmp_path):
    db, repo = await _repo(tmp_path)
    try:
        assert await repo.upsert_session(_session()) is True
        assert (await repo.get_open_session(
            environment="mainnet",
            account_fingerprint="acct-sha256",
            database_identity="gridbot_mainnet.db",
            symbol="BTCUSDC",
        ))["session_id"] == "session-a"
        assert await repo.get_open_session(
            environment="testnet",
            account_fingerprint="acct-sha256",
            database_identity="gridbot_mainnet.db",
            symbol="BTCUSDC",
        ) is None

        with pytest.raises(sqlite3.IntegrityError):
            await repo.upsert_session(_session("session-b"))
        assert await repo.upsert_session(_session("session-b", status="STOPPED")) is True
        assert await repo.get_open_session(
            environment="mainnet",
            account_fingerprint="acct-sha256",
            database_identity="gridbot_mainnet.db",
            symbol="ETHUSDC",
        ) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_opportunity_is_immutable_deduplicated_and_session_scoped(tmp_path):
    db, repo = await _repo(tmp_path)
    try:
        assert await repo.upsert_session(_session()) is True
        assert await repo.record_opportunity(_opportunity()) is True
        changed_features = {"rng15_bp": 21.0}
        assert await repo.record_opportunity(
            _opportunity(
                feature_snapshot=changed_features,
                feature_hash=_feature_hash(changed_features),
                raw_decision={"accepted": True},
            )
        ) is False
        original = await repo.get_opportunity("session-a", "opp-a")
        assert original["feature_hash"] == _feature_hash({"rng15_bp": 20.0})
        assert original["source_run_id"] == "run-a"
        assert original["opportunity_bucket"] == 33
        assert original["decision_at_ms"] == 2_000
        assert original["outcome_blind"] == 1
        assert json.loads(original["feature_snapshot_json"]) == {"rng15_bp": 20.0}
        assert json.loads(original["feature_timestamps_json"]) == {"rng15_bp": 1_990}
        assert json.loads(original["raw_decision_json"]) == {"accepted": False}

        assert await repo.upsert_session(_session("session-b", status="STOPPED")) is True
        assert await repo.record_opportunity(_opportunity("session-b", "opp-a")) is True
        assert await repo.count_opportunities("session-a") == 1
        assert await repo.count_opportunities("session-b") == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_opportunity_listing_filters_and_input_guards(tmp_path):
    db, repo = await _repo(tmp_path)
    try:
        assert await repo.upsert_session(_session()) is True
        assert await repo.record_opportunity(_opportunity(opportunity_id="opp-b", observed_at_ms=3_000))
        assert await repo.record_opportunity(
            _opportunity(
                opportunity_id="opp-a",
                observed_at_ms=2_000,
                quality_status="DATA_INCOMPLETE",
            )
        )
        rows = await repo.list_opportunities("session-a", since_ms=0)
        assert [row["opportunity_id"] for row in rows] == ["opp-a", "opp-b"]
        assert await repo.count_opportunities("session-a", quality_status="DATA_INCOMPLETE") == 1
        assert [row["opportunity_id"] for row in await repo.list_opportunities(
            "session-a", quality_status="OBSERVED", since_ms=2_500
        )] == ["opp-b"]

        with pytest.raises(ValueError, match="feature_hash does not match"):
            await repo.record_opportunity(
                _opportunity(opportunity_id="bad-hash", feature_hash="incorrect")
            )
        with pytest.raises(ValueError, match="outcome_blind"):
            await repo.record_opportunity(
                _opportunity(opportunity_id="outcome-leak", outcome_blind=False)
            )
        with pytest.raises(ValueError, match="cannot follow"):
            await repo.record_opportunity(
                _opportunity(
                    opportunity_id="future-decision",
                    observed_at_ms=2_000,
                    decision_at_ms=2_001,
                )
            )
        with pytest.raises(ValueError, match="feature timestamp cannot follow"):
            await repo.record_opportunity(
                _opportunity(
                    opportunity_id="future-feature",
                    decision_at_ms=2_000,
                    feature_timestamps={"rng15_bp": 2_001},
                )
            )
        with pytest.raises(ValueError, match="action_schema"):
            await repo.record_opportunity(_opportunity(opportunity_id="bad-json", action_schema={"x": float("nan")}))
        with pytest.raises(ValueError, match="limit"):
            await repo.list_opportunities("session-a", limit=0)
        with pytest.raises(sqlite3.IntegrityError):
            await repo.record_opportunity(_opportunity(session_id="missing-session"))
    finally:
        await db.close()
