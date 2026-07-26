from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.gridbot.mainnet.v1459_reconciliation_hook import (
    V1459TerminalReconciliationHook,
)


class _Runtime:
    permits_order_mutation = False

    def __init__(self, *, enabled=True, response=None, error=None):
        self.flags = SimpleNamespace(record_reconciliation=enabled)
        self.response = response
        self.error = error
        self.calls = 0

    async def record_reconciliation(self, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response or (
            SimpleNamespace(
                reconciliation_status="COMPLETE", completeness_reason=None
            ),
            SimpleNamespace(
                attempted=True, inserted=True, status="COMPLETE", reason=None
            ),
        )


def _payload():
    return {
        "trades": (),
        "incomes": (),
        "persistence_trades": (),
        "persistence_incomes": (),
        "run_id": "run-a",
        "reconciliation_revision": 0,
        "reconciled_at_ms": 2_000,
    }


@pytest.mark.asyncio
async def test_flags_off_is_zero_io_and_exposes_no_order_capability() -> None:
    runtime = _Runtime(enabled=False)
    hook = V1459TerminalReconciliationHook(runtime)

    result = await hook.record(**_payload())

    assert result.continue_live and result.status == "DISABLED"
    assert runtime.calls == 0
    assert not hook.entry_paused
    for name in ("create_order", "cancel_order", "place_order", "amend_order"):
        assert not hasattr(hook, name)


@pytest.mark.asyncio
async def test_complete_and_exact_retry_continue() -> None:
    complete = await V1459TerminalReconciliationHook(_Runtime()).record(
        **_payload()
    )
    assert complete.continue_live
    assert complete.reconciliation.reconciliation_status == "COMPLETE"

    runtime = _Runtime(
        response=(
            SimpleNamespace(
                reconciliation_status="COMPLETE", completeness_reason=None
            ),
            SimpleNamespace(
                attempted=True,
                inserted=False,
                status="COMPLETE",
                reason=None,
            ),
        )
    )
    retry = await V1459TerminalReconciliationHook(runtime).record(**_payload())
    assert retry.continue_live and retry.reason == "IDEMPOTENT_RETRY"


@pytest.mark.asyncio
async def test_incomplete_or_unwritten_result_latches_before_rearm() -> None:
    runtime = _Runtime(
        response=(
            SimpleNamespace(
                reconciliation_status="DATA_INCOMPLETE",
                completeness_reason="MISSING_EXIT_TRADE",
            ),
            SimpleNamespace(
                attempted=True,
                inserted=True,
                status="DATA_INCOMPLETE",
                reason=None,
            ),
        )
    )
    hook = V1459TerminalReconciliationHook(runtime)
    first = await hook.record(**_payload())
    second = await hook.record(**_payload())

    assert not first.continue_live
    assert first.status == "RECONCILIATION_INCOMPLETE"
    assert first.reason == "MISSING_EXIT_TRADE"
    assert second is first and runtime.calls == 1
    assert hook.entry_paused


@pytest.mark.asyncio
async def test_persistence_error_latches_without_identity_claim() -> None:
    hook = V1459TerminalReconciliationHook(
        _Runtime(error=RuntimeError("db unavailable"))
    )

    result = await hook.record(**_payload())

    assert not result.continue_live
    assert result.status == "PERSISTENCE_ERROR"
    assert result.reason == "RuntimeError"
    assert hook.entry_paused
