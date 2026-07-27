from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.gridbot.mainnet.v1459_app_runtime_v2 import build_v1459_app_runtime_v2
from src.gridbot.storage.adaptive_session_runtime_reader import (
    AdaptiveSessionRuntimeReader,
    AdaptiveSessionScopeConflict,
)
from src.gridbot.storage.database import Database


def _settings(tmp_path, **updates):
    values = {
        "mainnet_v1459_observation_enabled": True,
        "mainnet_v1459_observation_persist_session_enabled": True,
        "mainnet_v1459_observation_record_opportunities_enabled": True,
        "mainnet_v1459_observation_record_shadow_enabled": False,
        "mainnet_v1459_observation_record_reconciliation_enabled": False,
        "mainnet_v1459_account_fingerprint_marker_path": str(
            tmp_path / "account.fingerprint"
        ),
        "mainnet_v1459_deployment_commit": "abcdef1",
        "db_path": str(tmp_path / "gridbot.db"),
        "mainnet_symbol": "ETHUSDC",
        "mainnet_strategy_label": "codex",
        "mainnet_leverage": 75,
        "mainnet_effective_max_cumulative_notional_usdc": 50.0,
        "mainnet_max_session_loss_usdc": 2.0,
        "mainnet_one_run_enabled": True,
        "mainnet_codex_v1458_cnl_wpr_deep_gate_enabled": True,
    }
    values.update(updates)
    return SimpleNamespace(**values)


class _ReadOnlyClient:
    exchange_endpoint = "https://fapi.binance.com"
    is_testnet = False

    async def get_position_mode(self) -> str:
        return "ONE_WAY"


def _session(session_id="session-a"):
    return {
        "session_id": session_id,
        "started_at_ms": 1_000,
        "terminal_runs": 3,
        "net_pnl_usdc": 0.12,
        "high_water_net_pnl_usdc": 0.20,
        "rearm_enabled": True,
        "counters": {"opportunities": 9},
        "disabled_states": {"falling"},
    }


@pytest.mark.asyncio
async def test_restart_seeds_revision_and_persists_pause_as_next_revision(
    tmp_path,
) -> None:
    marker = tmp_path / "account.fingerprint"
    marker.write_text("cry3-main-account-01", encoding="utf-8")
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    await db.initialize()
    try:
        first = await build_v1459_app_runtime_v2(
            settings=settings,
            db=db,
            read_only_identity_client=_ReadOnlyClient(),
            code_version="v1.4.59",
        )
        assert first.runtime is not None
        assert (
            await first.runtime.checkpoint_session(_session(), checkpoint_at_ms=1_000)
        ).status == "ACTIVE"

        changed = _settings(tmp_path, mainnet_v1459_deployment_commit="abcdef2")
        restart = await build_v1459_app_runtime_v2(
            settings=changed,
            db=db,
            read_only_identity_client=_ReadOnlyClient(),
            code_version="v1.4.59",
        )
        assert restart.runtime is not None
        restored = restart.runtime.durable_session
        assert restored is not None
        assert restored["terminal_runs"] == 3
        assert restored["counters"]["opportunities"] == 9
        assert restored["rearm_enabled"] is False
        paused = await restart.runtime.checkpoint_session(
            restored, checkpoint_at_ms=2_000
        )
        assert paused.status == "PAUSED_REQUIRES_ACK"
        row = await first.composition.evidence_repository.get_session("session-a")
        assert row["status"] == "PAUSED_REQUIRES_ACK"
        assert row["pause_reason"] == "deployment_commit_mismatch"
        assert row["revision"] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_changed_account_marker_still_finds_old_scope_and_pauses(tmp_path) -> None:
    marker = tmp_path / "account.fingerprint"
    marker.write_text("cry3-main-account-01", encoding="utf-8")
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    await db.initialize()
    try:
        first = await build_v1459_app_runtime_v2(
            settings=settings,
            db=db,
            read_only_identity_client=_ReadOnlyClient(),
            code_version="v1.4.59",
        )
        assert first.runtime is not None
        await first.runtime.checkpoint_session(_session(), checkpoint_at_ms=1_000)
        marker.write_text("cry3-main-account-02", encoding="utf-8")
        restart = await build_v1459_app_runtime_v2(
            settings=settings,
            db=db,
            read_only_identity_client=_ReadOnlyClient(),
            code_version="v1.4.59",
        )
        assert restart.runtime is not None
        paused = await restart.runtime.checkpoint_session(
            restart.runtime.durable_session, checkpoint_at_ms=2_000
        )
        assert paused.reason == "account_fingerprint_mismatch"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reader_fails_closed_on_multiple_open_account_identities(tmp_path) -> None:
    db = Database(str(tmp_path / "gridbot.db"))
    await db.initialize()
    try:
        base = {
            "environment": "mainnet",
            "database_identity": str((tmp_path / "gridbot.db").resolve()),
            "exchange_endpoint": "https://fapi.binance.com",
            "is_testnet": False,
            "symbol": "ETHUSDC",
            "account_mode": "ONE_WAY",
            "deployment_commit": "abcdef1",
            "code_version": "v1.4.59",
            "config_sha256": "config-a",
            "status": "ACTIVE",
            "started_at_ms": 1_000,
            "last_checkpoint_at_ms": 1_000,
            "counters": {},
            "disabled_states": [],
            "route_stats": {},
            "revision": 0,
        }
        from src.gridbot.storage.adaptive_evidence_repository import (
            AdaptiveEvidenceRepository,
        )

        repo = AdaptiveEvidenceRepository(db)
        await repo.upsert_session(
            dict(base, session_id="a", account_fingerprint="account-01")
        )
        await repo.upsert_session(
            dict(base, session_id="b", account_fingerprint="account-02")
        )
        with pytest.raises(AdaptiveSessionScopeConflict):
            await AdaptiveSessionRuntimeReader(
                db
            ).get_open_session_for_runtime_scope(
                environment="mainnet",
                database_identity=base["database_identity"],
                symbol="ETHUSDC",
            )
    finally:
        await db.close()

