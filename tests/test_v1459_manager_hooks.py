from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.gridbot.mainnet.v1459_manager_hooks import V1459ManagerObservationHooks


class _Runtime:
    permits_order_mutation = False

    def __init__(self, *, checkpoint=None, opportunity=None, durable=None, retire=None):
        self._checkpoint = checkpoint
        self._opportunity = opportunity
        self._retire = retire
        self.durable_session = durable
        self.calls = []

    async def checkpoint_session(self, session, *, checkpoint_at_ms):
        self.calls.append(("checkpoint", session, checkpoint_at_ms))
        if isinstance(self._checkpoint, Exception):
            raise self._checkpoint
        return self._checkpoint or SimpleNamespace(
            attempted=True, inserted=True, status="ACTIVE", reason=None
        )

    async def record_opportunity(self, **kwargs):
        self.calls.append(("opportunity", kwargs))
        if isinstance(self._opportunity, Exception):
            raise self._opportunity
        return self._opportunity or SimpleNamespace(
            attempted=True,
            inserted=True,
            status="ACCEPTED_OBSERVED",
            reason=None,
        )

    async def retire_durable_session(self, *, checkpoint_at_ms, stop_reason):
        self.calls.append(("retire", checkpoint_at_ms, stop_reason))
        if isinstance(self._retire, Exception):
            raise self._retire
        return self._retire or SimpleNamespace(
            attempted=False,
            inserted=False,
            status="NO_DURABLE_SESSION",
            reason=None,
        )


def _decision():
    return {
        "opportunity_id": "opp-a",
        "source_run_id": "run-a",
        "opportunity_bucket": 33,
        "decision_at_ms": 1_999,
        "symbol": "ETHUSDC",
        "side": "SHORT",
        "lane_code": "STUP-S",
        "market_state": "clean_extension",
        "execution_quality": "EXECUTABLE",
        "raw_classifier_accepted": True,
        "raw_classifier_reason": None,
        "live_effective_route": "NORMAL",
        "live_effective_action": {"entry": "E2"},
        "enforcement_applied": False,
        "observation_features": {"rng15_bp": 20.0},
        "observation_feature_timestamps": {"rng15_bp": 1_998},
    }


@pytest.mark.asyncio
async def test_disabled_hooks_continue_without_any_runtime_or_order_api() -> None:
    hooks = V1459ManagerObservationHooks(None)
    assert hooks.enabled is False
    assert (await hooks.checkpoint({}, checkpoint_at_ms=1)).continue_live
    assert (
        await hooks.record_opportunity(
            session_id="s", decision_payload={}, observed_at_ms=1
        )
    ).continue_live
    assert not hasattr(hooks, "create_order")
    assert not hasattr(hooks, "cancel_order")


@pytest.mark.asyncio
async def test_identity_pause_and_checkpoint_failure_stop_before_live_continuation() -> None:
    paused = _Runtime(
        checkpoint=SimpleNamespace(
            attempted=True,
            inserted=True,
            status="PAUSED_REQUIRES_ACK",
            reason="account_fingerprint_mismatch",
        )
    )
    result = await V1459ManagerObservationHooks(paused).checkpoint(
        {"session_id": "s"}, checkpoint_at_ms=1
    )
    assert result.continue_live is False
    assert result.reason == "account_fingerprint_mismatch"

    failed = await V1459ManagerObservationHooks(
        _Runtime(checkpoint=RuntimeError("db down"))
    ).checkpoint({"session_id": "s"}, checkpoint_at_ms=1)
    assert failed.continue_live is False
    assert failed.status == "PERSISTENCE_ERROR"


@pytest.mark.asyncio
async def test_orphaned_durable_session_is_retired_before_a_new_session_can_start() -> None:
    runtime = _Runtime(
        durable={"session_id": "old-session"},
        retire=SimpleNamespace(
            attempted=True,
            inserted=True,
            status="STOPPED",
            reason=None,
        ),
    )

    result = await V1459ManagerObservationHooks(runtime).retire_durable_session(
        checkpoint_at_ms=2_000,
        stop_reason="restart_orphaned_no_active_run",
    )

    assert result.continue_live is True
    assert runtime.calls == [("retire", 2_000, "restart_orphaned_no_active_run")]


@pytest.mark.asyncio
async def test_identical_checkpoint_retry_continues_but_unknown_duplicate_stops() -> None:
    retry = _Runtime(
        checkpoint=SimpleNamespace(
            attempted=True, inserted=False, status="ACTIVE", reason="IDEMPOTENT_RETRY"
        )
    )
    result = await V1459ManagerObservationHooks(retry).checkpoint(
        {"session_id": "s"}, checkpoint_at_ms=1
    )
    assert result.continue_live is True and result.reason == "IDEMPOTENT_RETRY"

    unknown = _Runtime(
        checkpoint=SimpleNamespace(attempted=True, inserted=False, status="ACTIVE", reason=None)
    )
    blocked = await V1459ManagerObservationHooks(unknown).checkpoint(
        {"session_id": "s"}, checkpoint_at_ms=1
    )
    assert blocked.continue_live is False and blocked.status == "CHECKPOINT_NOT_WRITTEN"


@pytest.mark.asyncio
async def test_opportunity_is_persisted_before_caller_may_continue() -> None:
    runtime = _Runtime()
    hooks = V1459ManagerObservationHooks(runtime)
    result = await hooks.record_opportunity(
        session_id="session-a",
        decision_payload=_decision(),
        observed_at_ms=2_000,
    )
    assert result.continue_live is True
    name, payload = runtime.calls[0]
    assert name == "opportunity"
    assert payload["effective_decision"]["accepted"] is True
    assert payload["features"] == {"rng15_bp": 20.0}
    assert payload["source_run_id"] == "run-a"
    assert payload["opportunity_bucket"] == 33
    assert payload["decision_at_ms"] == 1_999
    assert payload["feature_timestamps"] == {"rng15_bp": 1_998}


@pytest.mark.asyncio
async def test_opportunity_persistence_error_fails_closed_and_retry_is_safe() -> None:
    error = await V1459ManagerObservationHooks(
        _Runtime(opportunity=RuntimeError("db down"))
    ).record_opportunity(
        session_id="session-a",
        decision_payload=_decision(),
        observed_at_ms=2_000,
    )
    assert error.continue_live is False

    duplicate = _Runtime(
        opportunity=SimpleNamespace(
            attempted=True,
            inserted=False,
            status="ACCEPTED_OBSERVED",
            reason=None,
        )
    )
    retry = await V1459ManagerObservationHooks(duplicate).record_opportunity(
        session_id="session-a",
        decision_payload=_decision(),
        observed_at_ms=2_000,
    )
    assert retry.continue_live is True
    assert retry.reason == "IDEMPOTENT_RETRY"


def test_restored_session_is_no_rearm_and_hooks_expose_no_order_methods() -> None:
    durable = {"session_id": "s", "rearm_enabled": False}
    hooks = V1459ManagerObservationHooks(_Runtime(durable=durable))
    assert hooks.restored_session() == durable
    for name in ("place_order", "create_order", "cancel_order", "amend_order"):
        assert not hasattr(hooks, name)
