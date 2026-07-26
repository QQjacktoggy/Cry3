from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.gridbot.mainnet.v1459_app_runtime import build_v1459_app_runtime
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

    def __init__(self) -> None:
        self.calls = 0

    async def get_position_mode(self) -> str:
        self.calls += 1
        return "ONE_WAY"


@pytest.mark.asyncio
async def test_disabled_bootstrap_does_not_probe_exchange(tmp_path) -> None:
    settings = _settings(
        tmp_path,
        mainnet_v1459_observation_enabled=False,
        mainnet_v1459_observation_persist_session_enabled=False,
        mainnet_v1459_observation_record_opportunities_enabled=False,
    )
    client = _ReadOnlyClient()
    result = await build_v1459_app_runtime(
        settings=settings,
        db=Database(settings.db_path),
        read_only_identity_client=client,
        code_version="v1.4.59",
    )
    assert result.runtime is None
    assert client.calls == 0
    assert result.permits_order_mutation is False


@pytest.mark.asyncio
async def test_enabled_bootstrap_reads_mode_and_builds_orderless_runtime(tmp_path) -> None:
    (tmp_path / "account.fingerprint").write_text(
        "cry3-main-account-01", encoding="utf-8"
    )
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    await db.initialize()
    try:
        client = _ReadOnlyClient()
        result = await build_v1459_app_runtime(
            settings=settings,
            db=db,
            read_only_identity_client=client,
            code_version="v1.4.59",
        )
        assert result.runtime is not None
        assert result.runtime.permits_order_mutation is False
        assert client.calls == 1
        assert not hasattr(result.runtime, "create_order")
        assert not hasattr(result.runtime, "cancel_order")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_restart_loads_durable_expected_identity_and_pauses_commit_mismatch(
    tmp_path,
) -> None:
    (tmp_path / "account.fingerprint").write_text(
        "cry3-main-account-01", encoding="utf-8"
    )
    settings = _settings(tmp_path)
    db = Database(settings.db_path)
    await db.initialize()
    try:
        first = await build_v1459_app_runtime(
            settings=settings,
            db=db,
            read_only_identity_client=_ReadOnlyClient(),
            code_version="v1.4.59",
        )
        session = {
            "session_id": "session-a",
            "started_at_ms": 1_000,
            "terminal_runs": 0,
            "net_pnl_usdc": 0.0,
            "high_water_net_pnl_usdc": 0.0,
            "rearm_enabled": True,
            "counters": {},
            "disabled_states": set(),
        }
        assert first.runtime is not None
        assert (
            await first.runtime.checkpoint_session(session, checkpoint_at_ms=1_000)
        ).status == "ACTIVE"

        changed = _settings(tmp_path, mainnet_v1459_deployment_commit="abcdef2")
        restarted = await build_v1459_app_runtime(
            settings=changed,
            db=db,
            read_only_identity_client=_ReadOnlyClient(),
            code_version="v1.4.59",
        )
        assert restarted.runtime is not None
        result = await restarted.runtime.checkpoint_session(
            session, checkpoint_at_ms=2_000
        )
        assert result.status == "PAUSED_REQUIRES_ACK"
        assert result.reason == "deployment_commit_mismatch"
    finally:
        await db.close()

