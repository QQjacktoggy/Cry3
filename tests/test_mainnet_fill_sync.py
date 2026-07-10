from unittest.mock import AsyncMock

import pytest

from src.gridbot.mainnet.one_run import MainnetOneRunManager


@pytest.mark.asyncio
async def test_running_poll_syncs_fills_before_flat_terminalization():
    manager = object.__new__(MainnetOneRunManager)
    manager._client = type("Client", (), {"get_position": AsyncMock(return_value=None)})()
    manager._sync_fill_v1_events = AsyncMock(return_value=1)
    manager._finish_flat_run = AsyncMock()
    run = {"run_id": "run_1", "symbol": "ETHUSDC"}

    await manager._run_running(run)

    manager._sync_fill_v1_events.assert_awaited_once_with(run, "running_poll")
    manager._finish_flat_run.assert_awaited_once_with(run, "flat_detected")


@pytest.mark.asyncio
async def test_fill_sync_failure_is_non_blocking(monkeypatch):
    manager = object.__new__(MainnetOneRunManager)
    manager._repo = object()
    manager._client = object()
    manager._trade_repo = None

    async def fail(**kwargs):
        raise RuntimeError("exchange unavailable")

    monkeypatch.setattr("src.gridbot.mainnet.one_run.emit_fill_v1_events", fail)

    assert await manager._sync_fill_v1_events({"run_id": "run_1"}, "running_poll") == 0
