from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from src.gridbot.mainnet.v1469_paid_execution_adapter import (
    deterministic_client_order_id,
)
from src.gridbot.mainnet.v1469_paid_reconciler import (
    MAX_RECONCILE_CLAIMS,
    V1469PaidReconciler,
)
from src.gridbot.storage.v1469_paid_execution_claim_repository import (
    DurablePaidExecutionClaim,
    PaidClaimMutationResult,
    V1469PaidClaimConflictError,
)


def _claim(
    claim_id: str,
    status: str,
    *,
    generation: int,
    updated_at_ms: int,
) -> DurablePaidExecutionClaim:
    return DurablePaidExecutionClaim(
        claim_id=claim_id,
        environment="MAINNET",
        symbol="BTCUSDC",
        opportunity_id=f"opportunity-{claim_id}",
        arm_key=f"arm-{claim_id}",
        lease_id=f"lease-{claim_id}",
        lease_generation=1,
        evidence_revision="revision-1",
        regime="RANGE",
        execution_profile_hash="a" * 64,
        risk_policy_hash="b" * 64,
        approved_notional_usdc=10.0,
        reserved_loss_usdc=0.1,
        status=status,
        generation=generation,
        claimed_at_ms=10,
        terminal_at_ms=None,
        terminal_reason=None,
        result_payload=None,
        created_at_ms=10,
        updated_at_ms=updated_at_ms,
    )


class _FakeRepository:
    def __init__(
        self,
        claims: tuple[DurablePaidExecutionClaim, ...] = (),
        *,
        list_error: Exception | None = None,
        transition_conflicts: frozenset[str] = frozenset(),
    ) -> None:
        self.claims = claims
        self.list_error = list_error
        self.transition_conflicts = transition_conflicts
        self.list_limits: list[int] = []
        self.transition_calls: list[dict[str, object]] = []
        self.abandon_calls: list[dict[str, object]] = []

    async def list_reconcilable_claims(
        self,
        *,
        environment: str,
        limit: int,
        symbol: str | None = None,
    ) -> tuple[DurablePaidExecutionClaim, ...]:
        self.list_limits.append(limit)
        if self.list_error is not None:
            raise self.list_error
        # Deliberately do not slice. Some tests verify the runtime defends
        # against a repository that violates its bounded page contract.
        return self.claims

    async def transition_submission(self, **kwargs) -> PaidClaimMutationResult:
        self.transition_calls.append(dict(kwargs))
        claim_id = str(kwargs["claim_id"])
        if claim_id in self.transition_conflicts:
            raise V1469PaidClaimConflictError("generation changed")
        expected = int(kwargs["expected_generation"])
        claim = next(
            item
            for item in reversed(self.claims)
            if item.claim_id == claim_id
            and item.generation == expected
        )
        updated = replace(
            claim,
            status="SUBMITTED",
            generation=expected + 1,
            updated_at_ms=int(kwargs["transition_at_ms"]),
        )
        return PaidClaimMutationResult(
            claim=updated,
            applied=True,
            replayed=False,
        )

    async def abandon_claim(self, **kwargs) -> PaidClaimMutationResult:
        self.abandon_calls.append(dict(kwargs))
        expected = int(kwargs["expected_generation"])
        claim = next(
            item for item in self.claims
            if item.claim_id == kwargs["claim_id"]
            and item.generation == expected
        )
        updated = replace(
            claim,
            status="ABANDONED",
            generation=expected + 1,
            terminal_at_ms=int(kwargs["abandoned_at_ms"]),
            terminal_reason=str(kwargs["terminal_reason"]),
            result_payload=dict(kwargs["result_payload"]),
            updated_at_ms=int(kwargs["abandoned_at_ms"]),
        )
        return PaidClaimMutationResult(updated, True, False)


def test_restart_abandons_claimed_only_after_exchange_absence() -> None:
    async def scenario() -> None:
        claimed = _claim(
            "claim-pre", "CLAIMED", generation=0, updated_at_ms=10
        )
        repository = _FakeRepository((claimed,))

        async def absent(_symbol: str, _cid: str):
            return None

        result = await V1469PaidReconciler(repository).reconcile_on_restart(
            environment="MAINNET",
            now_ms=200,
            limit=10,
            find_by_client_order_id=absent,
        )
        assert result.ok
        assert result.transitioned_claims == 1
        assert result.telemetry[0].outcome == "ABSENT_CLAIMED_ABANDONED"
        assert repository.abandon_calls[0]["terminal_reason"] == (
            "RESTART_PRE_SUBMIT_NO_ORDER"
        )

    asyncio.run(scenario())


def test_restart_reconciles_visible_and_leaves_absence_fail_closed() -> None:
    async def scenario() -> None:
        submitting = _claim(
            "claim-a",
            "SUBMITTING",
            generation=2,
            updated_at_ms=100,
        )
        unknown = _claim(
            "claim-b",
            "UNKNOWN",
            generation=3,
            updated_at_ms=101,
        )
        submitted = _claim(
            "claim-c",
            "SUBMITTED",
            generation=3,
            updated_at_ms=102,
        )
        repository = _FakeRepository(
            (submitting, unknown, submitted)
        )
        calls: list[tuple[str, str]] = []
        visible_ids = {
            deterministic_client_order_id("claim-a"),
            deterministic_client_order_id("claim-c"),
        }

        async def find(symbol: str, cid: str):
            calls.append((symbol, cid))
            if cid not in visible_ids:
                return None
            return {
                "symbol": symbol,
                "clientOrderId": cid,
                "orderId": 42,
            }

        result = await V1469PaidReconciler(
            repository
        ).reconcile_on_restart(
            environment="MAINNET",
            now_ms=200,
            limit=3,
            find_by_client_order_id=find,
        )

        assert repository.list_limits == [3]
        assert len(calls) == 3
        assert len(set(calls)) == 3
        assert result.enumerated_claims == 3
        assert result.processed_claims == 3
        assert result.lookup_calls == 3
        assert result.visible_orders == 2
        assert result.absent_orders == 1
        assert result.transitioned_claims == 1
        assert result.already_submitted_claims == 1
        assert not result.ok
        assert [error.code for error in result.errors] == [
            "ORDER_ABSENT_UNRESOLVED"
        ]
        assert [call["claim_id"] for call in repository.transition_calls] == [
            "claim-a"
        ]
        transition = repository.transition_calls[0]
        assert transition["target_status"] == "SUBMITTED"
        assert transition["payload"] == {
            "client_order_id": deterministic_client_order_id("claim-a"),
            "reconciled": True,
        }
        outcomes = {
            item.claim_id: item.outcome for item in result.telemetry
        }
        assert outcomes == {
            "claim-a": "RECONCILED_SUBMITTED",
            "claim-b": "ABSENT_FAIL_CLOSED",
            "claim-c": "VISIBLE_ALREADY_SUBMITTED",
        }

    asyncio.run(scenario())


def test_duplicate_rows_and_repository_overrun_never_duplicate_lookup() -> None:
    async def scenario() -> None:
        stale = _claim(
            "claim-duplicate",
            "SUBMITTING",
            generation=2,
            updated_at_ms=100,
        )
        latest = _claim(
            "claim-duplicate",
            "UNKNOWN",
            generation=3,
            updated_at_ms=101,
        )
        outside_limit = _claim(
            "claim-outside",
            "UNKNOWN",
            generation=3,
            updated_at_ms=102,
        )
        repository = _FakeRepository(
            (stale, latest, outside_limit)
        )
        calls: list[tuple[str, str]] = []

        async def find(symbol: str, cid: str):
            calls.append((symbol, cid))
            return {"symbol": symbol, "clientOrderId": cid}

        result = await V1469PaidReconciler(
            repository
        ).reconcile_on_restart(
            environment="MAINNET",
            now_ms=200,
            limit=2,
            find_by_client_order_id=find,
        )

        assert calls == [
            (
                "BTCUSDC",
                deterministic_client_order_id("claim-duplicate"),
            )
        ]
        assert result.lookup_calls == 1
        assert result.processed_claims == 1
        assert result.transitioned_claims == 1
        assert repository.transition_calls[0]["expected_generation"] == 3
        assert {error.code for error in result.errors} == {
            "DUPLICATE_CLAIM",
            "REPOSITORY_LIMIT_VIOLATION",
        }

    asyncio.run(scenario())


def test_lookup_identity_and_transition_errors_are_bounded_and_continue() -> None:
    async def scenario() -> None:
        claims = (
            _claim("claim-error", "UNKNOWN", generation=3, updated_at_ms=100),
            _claim("claim-mismatch", "UNKNOWN", generation=3, updated_at_ms=101),
            _claim("claim-conflict", "UNKNOWN", generation=3, updated_at_ms=102),
        )
        repository = _FakeRepository(
            claims,
            transition_conflicts=frozenset({"claim-conflict"}),
        )
        calls: list[str] = []

        async def find(symbol: str, cid: str):
            calls.append(cid)
            if cid == deterministic_client_order_id("claim-error"):
                raise TimeoutError("exchange timeout")
            if cid == deterministic_client_order_id("claim-mismatch"):
                return {
                    "symbol": symbol,
                    "clientOrderId": "foreign-cid",
                }
            return {"symbol": symbol, "clientOrderId": cid}

        result = await V1469PaidReconciler(
            repository
        ).reconcile_on_restart(
            environment="MAINNET",
            now_ms=200,
            limit=3,
            find_by_client_order_id=find,
        )

        assert len(calls) == 3
        assert len(set(calls)) == 3
        assert result.lookup_calls == 3
        assert result.transitioned_claims == 0
        assert len(result.telemetry) == 3
        assert len(result.errors) == 3
        assert {error.code for error in result.errors} == {
            "LOOKUP_ERROR",
            "TRANSITION_CONFLICT",
            "VISIBLE_ORDER_IDENTITY_MISMATCH",
        }
        assert all(len(error.detail) <= 240 for error in result.errors)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "limit",
    (0, MAX_RECONCILE_CLAIMS + 1, True),
)
def test_restart_reconciliation_rejects_unbounded_limit(limit: object) -> None:
    async def scenario() -> None:
        repository = _FakeRepository()

        async def find(symbol: str, cid: str):
            raise AssertionError("lookup must not run")

        with pytest.raises(ValueError, match="limit must be an integer"):
            await V1469PaidReconciler(
                repository
            ).reconcile_on_restart(
                environment="MAINNET",
                now_ms=200,
                limit=limit,
                find_by_client_order_id=find,
            )
        assert not repository.list_limits

    asyncio.run(scenario())


def test_repository_enumeration_error_is_returned_without_exchange_call() -> None:
    async def scenario() -> None:
        repository = _FakeRepository(
            list_error=RuntimeError("database unavailable")
        )
        calls = 0

        async def find(symbol: str, cid: str):
            nonlocal calls
            calls += 1
            return None

        result = await V1469PaidReconciler(
            repository
        ).reconcile_on_restart(
            environment="MAINNET",
            now_ms=200,
            limit=5,
            find_by_client_order_id=find,
        )

        assert calls == 0
        assert result.lookup_calls == 0
        assert result.processed_claims == 0
        assert not result.ok
        assert tuple(error.code for error in result.errors) == (
            "REPOSITORY_LIST_ERROR",
        )

    asyncio.run(scenario())
