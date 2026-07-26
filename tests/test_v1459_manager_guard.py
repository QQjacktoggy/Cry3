from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.gridbot.mainnet.v1459_manager_guard import (
    V1459ManagerObservationGuard,
)


class _Runtime:
    permits_order_mutation = False
    durable_session = None

    def __init__(self) -> None:
        self.checkpoint_calls = 0
        self.opportunity_calls = 0

    async def checkpoint_session(self, session, *, checkpoint_at_ms):
        self.checkpoint_calls += 1
        return SimpleNamespace(
            attempted=True,
            inserted=True,
            status="PAUSED_REQUIRES_ACK",
            reason="config_hash_mismatch",
        )

    async def record_opportunity(self, **kwargs):
        self.opportunity_calls += 1
        return SimpleNamespace(
            attempted=True,
            inserted=True,
            status="ACCEPTED_OBSERVED",
            reason=None,
        )


@pytest.mark.asyncio
async def test_failure_latches_across_cycles_and_prevents_later_calls() -> None:
    runtime = _Runtime()
    guard = V1459ManagerObservationGuard(runtime)
    first = await guard.checkpoint({"session_id": "s"}, checkpoint_at_ms=1)
    second = await guard.checkpoint({"session_id": "s"}, checkpoint_at_ms=2)
    opportunity = await guard.record_opportunity(
        session_id="s", decision_payload={}, observed_at_ms=3
    )
    assert not first.continue_live and second is first and opportunity is first
    assert guard.blocked and guard.blocked_reason == "config_hash_mismatch"
    assert guard.entry_paused and guard.identity_unsafe
    assert guard.permits_known_owned_risk_reduction is False
    assert runtime.checkpoint_calls == 1
    assert runtime.opportunity_calls == 0


@pytest.mark.asyncio
async def test_persistence_failure_pauses_entry_but_allows_owned_risk_reduction() -> None:
    class PersistenceFailureRuntime(_Runtime):
        async def checkpoint_session(self, session, *, checkpoint_at_ms):
            self.checkpoint_calls += 1
            raise RuntimeError("db unavailable")

    guard = V1459ManagerObservationGuard(PersistenceFailureRuntime())
    result = await guard.checkpoint({"session_id": "s"}, checkpoint_at_ms=1)

    assert result.status == "PERSISTENCE_ERROR"
    assert guard.entry_paused is True
    assert guard.identity_unsafe is False
    assert guard.permits_known_owned_risk_reduction is True


@pytest.mark.asyncio
async def test_disabled_guard_is_permanent_noop_without_order_capabilities() -> None:
    guard = V1459ManagerObservationGuard(None)
    assert (await guard.checkpoint({}, checkpoint_at_ms=1)).continue_live
    assert not guard.blocked
    assert not guard.entry_paused and not guard.identity_unsafe
    assert guard.permits_known_owned_risk_reduction is True
    for name in ("create_order", "cancel_order", "amend_order", "place_order"):
        assert not hasattr(guard, name)

