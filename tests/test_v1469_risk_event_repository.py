from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from src.gridbot.mainnet.v1469_risk_policy import (
    DEFAULT_RISK_POLICY,
    DailyRiskEvent,
    reduce_daily_risk,
)
from src.gridbot.storage.database import Database
from src.gridbot.storage.v1469_risk_event_repository import (
    V1469RiskEventConflictError,
    V1469RiskEventRepository,
)


@pytest.mark.asyncio
async def test_paid_close_events_survive_restart_and_reduce_idempotently(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "risk.db"
    policy_hash = DEFAULT_RISK_POLICY.policy_hash
    first = DailyRiskEvent(
        event_id="close-1",
        occurred_at_ms=1_753_401_600_000,
        fee_net_pnl_delta_usdc=0.16,
        risk_policy_hash=policy_hash,
    )
    second = DailyRiskEvent(
        event_id="close-2",
        occurred_at_ms=1_753_401_660_000,
        fee_net_pnl_delta_usdc=-0.15,
        risk_policy_hash=policy_hash,
    )

    db = Database(str(db_path))
    await db.initialize()
    repo = V1469RiskEventRepository(db)
    await repo.assert_schema_ready()
    assert await repo.append_event(
        first,
        environment="MAINNET",
        symbol="BTCUSDC",
        source_run_id="run-1",
        source_trade_id="trade-1",
    )
    assert not await repo.append_event(
        first,
        environment="MAINNET",
        symbol="BTCUSDC",
        source_run_id="run-1",
        source_trade_id="trade-1",
    )
    assert await repo.append_event(
        second,
        environment="MAINNET",
        symbol="BTCUSDC",
        source_run_id="run-2",
        source_trade_id="trade-2",
    )
    await db.close()

    restarted_db = Database(str(db_path))
    await restarted_db.initialize()
    restarted_repo = V1469RiskEventRepository(restarted_db)
    try:
        events = await restarted_repo.load_active_day_events(
            environment="MAINNET",
            symbol="BTCUSDC",
            as_of_ms=1_753_401_720_000,
        )
        assert events == (first, second)
        snapshot = reduce_daily_risk(
            events,
            as_of_ms=1_753_401_720_000,
            expected_risk_policy_hash=policy_hash,
        )
        assert snapshot.data_valid is True
        assert snapshot.closed_fee_net_pnl_usdc == pytest.approx(0.01)
        assert snapshot.high_water_usdc == pytest.approx(0.16)
        assert snapshot.profit_floor_triggered is True
        assert snapshot.entry_blocked is True
    finally:
        await restarted_db.close()


@pytest.mark.asyncio
async def test_risk_event_conflicts_and_mutation_fail_closed(
    tmp_path: Path,
) -> None:
    db = Database(str(tmp_path / "risk-conflict.db"))
    await db.initialize()
    repo = V1469RiskEventRepository(db)
    policy_hash = DEFAULT_RISK_POLICY.policy_hash
    event = DailyRiskEvent(
        event_id="close-1",
        occurred_at_ms=1_753_401_600_000,
        fee_net_pnl_delta_usdc=-0.05,
        risk_policy_hash=policy_hash,
    )
    try:
        await repo.append_event(
            event,
            environment="MAINNET",
            symbol="BTCUSDC",
            source_trade_id="trade-1",
        )
        conflicting = DailyRiskEvent(
            event_id="close-1",
            occurred_at_ms=event.occurred_at_ms,
            fee_net_pnl_delta_usdc=-0.06,
            risk_policy_hash=policy_hash,
        )
        with pytest.raises(
            V1469RiskEventConflictError,
            match="event_id reused",
        ):
            await repo.append_event(
                conflicting,
                environment="MAINNET",
                symbol="BTCUSDC",
                source_trade_id="trade-1",
            )
        with pytest.raises(
            V1469RiskEventConflictError,
            match="UNIQUE constraint",
        ):
            await repo.append_event(
                DailyRiskEvent(
                    event_id="close-2",
                    occurred_at_ms=event.occurred_at_ms,
                    fee_net_pnl_delta_usdc=-0.05,
                    risk_policy_hash=policy_hash,
                ),
                environment="MAINNET",
                symbol="BTCUSDC",
                source_trade_id="trade-1",
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            await db.conn.execute(
                """UPDATE v1469_daily_risk_events
                SET fee_net_pnl_delta_usdc = 1
                WHERE event_id = 'close-1'"""
            )
    finally:
        await db.conn.rollback()
        await db.close()


@pytest.mark.asyncio
async def test_active_day_load_fails_closed_instead_of_truncating_newest_loss(
    tmp_path: Path,
) -> None:
    db = Database(str(tmp_path / "risk-truncation.db"))
    await db.initialize()
    repo = V1469RiskEventRepository(db)
    policy_hash = DEFAULT_RISK_POLICY.policy_hash
    try:
        for index, pnl in enumerate((0.10, 0.10, -0.30), start=1):
            await repo.append_event(
                DailyRiskEvent(
                    event_id=f"close-{index}",
                    occurred_at_ms=1_753_401_600_000 + index,
                    fee_net_pnl_delta_usdc=pnl,
                    risk_policy_hash=policy_hash,
                ),
                environment="MAINNET",
                symbol="BTCUSDC",
                source_trade_id=f"trade-{index}",
            )

        with pytest.raises(
            RuntimeError,
            match="exceeds bounded load limit",
        ):
            await repo.load_active_day_events(
                environment="MAINNET",
                symbol="BTCUSDC",
                as_of_ms=1_753_401_700_000,
                limit=2,
            )
    finally:
        await db.close()
