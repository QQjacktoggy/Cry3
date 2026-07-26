from src.gridbot.mainnet.v1469_paid_execution_adapter import (
    deterministic_client_order_id,
)


def test_deterministic_client_order_id_is_stable_unique_and_binance_safe():
    first = deterministic_client_order_id("v1469c_one")
    assert first == deterministic_client_order_id("v1469c_one")
    assert first != deterministic_client_order_id("v1469c_two")
    assert first.startswith("c69_")
    assert len(first) == 36
    assert first.isascii() and first.replace("_", "").isalnum()


def test_deterministic_client_order_id_rejects_missing_claim():
    try:
        deterministic_client_order_id("  ")
    except ValueError as exc:
        assert str(exc) == "claim_id must be non-empty"
    else:
        raise AssertionError("missing claim id was accepted")

import asyncio
from pathlib import Path

from src.gridbot.mainnet.v1469_paid_execution_adapter import V1469PaidExecutionAdapter
from src.gridbot.storage.database import Database
from src.gridbot.storage.v1469_paid_execution_claim_repository import V1469PaidExecutionClaimRepository
from tests.test_v1469_paid_execution_claim_repository import (
    ARM_KEY, ENVIRONMENT, LEASE_ID, SYMBOL, _seed_opportunities_and_active_lease,
)


def test_two_adapters_only_cas_winner_submits_and_restart_reconciles(tmp_path: Path):
    async def scenario():
        path = tmp_path / "adapter-race.db"
        db1, db2 = Database(str(path)), Database(str(path))
        await db1.initialize(); await db2.initialize()
        repo1, repo2 = V1469PaidExecutionClaimRepository(db1), V1469PaidExecutionClaimRepository(db2)
        await _seed_opportunities_and_active_lease(db1, "race-opp")
        claim = (await repo1.claim(
            environment=ENVIRONMENT, symbol=SYMBOL, opportunity_id="race-opp",
            arm_key=ARM_KEY, lease_id=LEASE_ID, claimed_at_ms=1100,
            idempotency_key="race-claim", actor="test",
        )).claim
        calls, visible = [], {}
        async def find(cid):
            return visible.get(cid)
        async def submit(cid):
            calls.append(cid)
            await asyncio.sleep(.05)
            visible[cid] = {"clientOrderId": cid}
            return visible[cid]
        results = await asyncio.gather(*(
            V1469PaidExecutionAdapter(repo).submit_or_reconcile(
                claim=claim, now_ms=1200, find_by_client_order_id=find,
                submit=submit, actor=f"runner-{i}")
            for i, repo in enumerate((repo1, repo2))
        ))
        assert len(calls) == 1
        assert calls == [deterministic_client_order_id(claim.claim_id)]
        assert sum(result.submitted_now for result in results) == 1
        durable = await repo1.get_claim_by_id(claim.claim_id)
        assert durable and durable.status == "SUBMITTED"
        restarted = V1469PaidExecutionAdapter(repo2)
        replay = await restarted.submit_or_reconcile(
            claim=claim, now_ms=1300, find_by_client_order_id=find,
            submit=submit, actor="restart")
        assert replay.submitted_now is False and len(calls) == 1
        await db1.close(); await db2.close()
    asyncio.run(scenario())


def test_timeout_becomes_unknown_and_never_blindly_retries(tmp_path: Path):
    async def scenario():
        db = Database(str(tmp_path / "unknown.db")); await db.initialize()
        repo = V1469PaidExecutionClaimRepository(db)
        await _seed_opportunities_and_active_lease(db, "unknown-opp")
        claim = (await repo.claim(
            environment=ENVIRONMENT, symbol=SYMBOL, opportunity_id="unknown-opp",
            arm_key=ARM_KEY, lease_id=LEASE_ID, claimed_at_ms=1100,
            idempotency_key="unknown-claim", actor="test")).claim
        calls = 0
        async def absent(cid): return None
        async def timeout(cid):
            nonlocal calls; calls += 1; raise TimeoutError("ambiguous")
        adapter = V1469PaidExecutionAdapter(repo)
        try:
            await adapter.submit_or_reconcile(claim=claim, now_ms=1200,
                find_by_client_order_id=absent, submit=timeout, actor="runner")
        except TimeoutError: pass
        durable = await repo.get_claim_by_id(claim.claim_id)
        assert durable and durable.status == "UNKNOWN"
        result = await adapter.submit_or_reconcile(claim=durable, now_ms=1300,
            find_by_client_order_id=absent, submit=timeout, actor="restart")
        assert result.submitted_now is False and calls == 1
        await db.close()
    asyncio.run(scenario())
