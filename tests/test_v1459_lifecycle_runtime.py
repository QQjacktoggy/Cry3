from __future__ import annotations

import pytest

from src.gridbot.mainnet.runtime_identity import RuntimeIdentity
from src.gridbot.mainnet.v1459_lifecycle_runtime import (
    V1459LifecycleObservationRuntime,
    restore_adaptive_session_snapshot,
)
from src.gridbot.mainnet.v1459_observation_contract import V1459ObservationFlags
from src.gridbot.mainnet.v1459_observation_coordinator import V1459ObservationCoordinator
from src.gridbot.mainnet.v1459_observation_runtime import V1459RuntimeContext


def _identity() -> RuntimeIdentity:
    return RuntimeIdentity(
        environment="mainnet",
        exchange_endpoint="https://fapi.binance.com",
        exchange_testnet=False,
        account_fingerprint="account-sha",
        db_namespace="gridbot_mainnet.db",
        symbol="ETHUSDC",
        account_mode="ONE_WAY",
        deployment_commit="commit-a",
        config_hash="config-a",
    )


class _EvidenceRepo:
    def __init__(self) -> None:
        self.sessions: list[dict] = []

    async def upsert_session(self, payload: dict) -> bool:
        self.sessions.append(dict(payload))
        return True


class _ResultRepo:
    pass


def _durable_row() -> dict:
    identity = _identity()
    return {
        "session_id": "old-session",
        "environment": identity.environment,
        "account_fingerprint": identity.account_fingerprint,
        "database_identity": identity.db_namespace,
        "exchange_endpoint": identity.exchange_endpoint,
        "is_testnet": int(identity.exchange_testnet),
        "symbol": identity.symbol,
        "account_mode": identity.account_mode,
        "deployment_commit": identity.deployment_commit,
        "code_version": "v1.4.59",
        "config_sha256": identity.config_hash,
        "status": "ACTIVE",
        "rearm_pending": 0,
        "started_at_ms": 1_000,
        "last_checkpoint_at_ms": 1_500,
        "terminal_runs": 2,
        "gross_pnl_usdc": 0.1,
        "commission_usdc": 0.01,
        "funding_usdc": 0.0,
        "net_pnl_usdc": 0.09,
        "high_water_net_pnl_usdc": 0.12,
        "counters_json": "{}",
        "disabled_states_json": "[]",
        "route_stats_json": "{}",
        "revision": 6,
        "stop_reason": None,
        "pause_reason": None,
    }


@pytest.mark.asyncio
async def test_retire_durable_session_writes_stopped_revision_and_clears_memory() -> None:
    evidence = _EvidenceRepo()
    coordinator = V1459ObservationCoordinator(
        flags=V1459ObservationFlags(enabled=True, persist_session=True),
        evidence_repo=evidence,
        result_repo=_ResultRepo(),
    )
    identity = _identity()
    runtime = V1459LifecycleObservationRuntime(
        coordinator=coordinator,
        context=V1459RuntimeContext(identity, identity, "v1.4.59"),
        durable_session=_durable_row(),
    )

    result = await runtime.retire_durable_session(
        checkpoint_at_ms=2_000,
        stop_reason="restart_orphaned_no_active_run",
    )

    assert result.inserted and result.status == "STOPPED"
    assert runtime.durable_session is None
    assert evidence.sessions[-1]["revision"] == 7
    assert evidence.sessions[-1]["status"] == "STOPPED"
    assert evidence.sessions[-1]["stop_reason"] == "restart_orphaned_no_active_run"


def test_restore_active_durable_session_preserves_explicit_rearm_authority() -> None:
    row = _durable_row()
    row["rearm_pending"] = 1

    restored = restore_adaptive_session_snapshot(row)

    assert restored["durable_status"] == "ACTIVE"
    assert restored["rearm_enabled"] is True
    assert restored["stop_requested"] is False
    assert restored["restart_recovered"] is True


@pytest.mark.parametrize(
    ("status", "rearm_pending", "pause_reason", "stop_reason"),
    [
        ("ACTIVE", 0, None, None),
        ("PAUSED_REQUIRES_ACK", 0, "config_hash_mismatch", None),
        ("STOPPED", 0, None, "manual_stop"),
    ],
)
def test_restore_durable_session_never_rearms_without_active_authority(
    status: str,
    rearm_pending: int,
    pause_reason: str | None,
    stop_reason: str | None,
) -> None:
    row = _durable_row()
    row.update(
        {
            "status": status,
            "rearm_pending": rearm_pending,
            "pause_reason": pause_reason,
            "stop_reason": stop_reason,
        }
    )

    restored = restore_adaptive_session_snapshot(row)

    assert restored["rearm_enabled"] is False
    assert restored["stop_requested"] is True
