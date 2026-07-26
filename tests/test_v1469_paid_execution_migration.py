from __future__ import annotations

import asyncio
from pathlib import Path
import pytest

from src.gridbot.storage.database import Database
from src.gridbot.storage.v1469_paid_execution_claim_repository import (
    V1469PaidClaimConflictError, V1469PaidClaimPersistenceError,
    V1469PaidExecutionClaimRepository,
)
from tests.test_v1469_paid_execution_claim_repository import (
    ARM_KEY, ENVIRONMENT, LEASE_ID, SYMBOL, _seed_opportunities_and_active_lease,
)


def test_019_rebuild_preserves_rows_and_enables_submission_lifecycle(tmp_path: Path):
    async def scenario():
        path = tmp_path / "upgrade.db"
        db = Database(str(path)); await db.initialize()
        repo = V1469PaidExecutionClaimRepository(db)
        await _seed_opportunities_and_active_lease(db, "claimed", "terminal")
        claimed = (await repo.claim(environment=ENVIRONMENT, symbol=SYMBOL,
            opportunity_id="claimed", arm_key=ARM_KEY, lease_id=LEASE_ID,
            claimed_at_ms=1100, idempotency_key="claim:a", actor="test")).claim
        terminal_seed = (await repo.claim(environment=ENVIRONMENT, symbol=SYMBOL,
            opportunity_id="terminal", arm_key=ARM_KEY, lease_id=LEASE_ID,
            claimed_at_ms=1101, idempotency_key="claim:b", actor="test")).claim
        terminal = (await repo.terminalize_claim(claim_id=terminal_seed.claim_id,
            expected_generation=1, terminal_at_ms=1200, terminal_reason="CLOSED",
            idempotency_key="terminal:b", actor="test", result_payload={})).claim
        # Re-run the real ordered migration runner. This exercises the same
        # table rebuild used when 018 is already recorded on an installation.
        await db.conn.execute("DELETE FROM _migrations WHERE filename = ?",
            ("019_v1469_paid_execution_claim_upgrade.sql",)); await db.conn.commit()
        await db.close()
        upgraded = Database(str(path)); await upgraded.initialize()
        upgraded_repo = V1469PaidExecutionClaimRepository(upgraded)
        await upgraded_repo.assert_schema_ready()
        assert await upgraded_repo.get_claim_by_id(terminal.claim_id) == terminal
        durable = await upgraded_repo.get_claim_by_id(claimed.claim_id)
        assert durable == claimed
        with pytest.raises(V1469PaidClaimConflictError):
            await upgraded_repo.transition_submission(claim_id=claimed.claim_id,
                expected_generation=1, target_status="SUBMITTED", transition_at_ms=1300,
                idempotency_key="invalid", actor="test", payload={"client_order_id":"cid"})
        for status, at in (("SUBMITTING", 1300), ("UNKNOWN", 1400),
                           ("SUBMITTED", 1500)):
            durable = (await upgraded_repo.transition_submission(
                claim_id=durable.claim_id, expected_generation=durable.generation,
                target_status=status, transition_at_ms=at,
                idempotency_key=f"{status}:{durable.generation}", actor="test",
                payload={"client_order_id":"cid"})).claim
        durable = (await upgraded_repo.terminalize_claim(claim_id=durable.claim_id,
            expected_generation=durable.generation, terminal_at_ms=1600,
            terminal_reason="CLOSED", idempotency_key="terminal:a", actor="test",
            result_payload={"client_order_id":"cid"})).claim
        assert durable.status == "TERMINAL"
        assert (await upgraded.fetchone("PRAGMA foreign_key_check")) is None
        assert (await upgraded.fetchone("SELECT COUNT(*) AS n FROM v1469_paid_execution_claim_events"))["n"] == 7
        await upgraded.close()
    asyncio.run(scenario())


def test_schema_readiness_requires_019_marker(tmp_path: Path):
    async def scenario():
        db = Database(str(tmp_path / "schema.db")); await db.initialize()
        repo = V1469PaidExecutionClaimRepository(db); await repo.assert_schema_ready()
        await db.conn.execute("DELETE FROM _migrations WHERE filename = ?",
            ("019_v1469_paid_execution_claim_upgrade.sql",)); await db.conn.commit()
        with pytest.raises(V1469PaidClaimPersistenceError, match="upgrade_019_applied=False"):
            await repo.assert_schema_ready()
        await db.close()
    asyncio.run(scenario())
