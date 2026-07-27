from types import SimpleNamespace

import pytest

from src.gridbot.mainnet.run_reconciler import reconcile_run
from src.gridbot.mainnet.v1459_live_reconciliation import (
    build_reconciliation_payloads,
)
from src.gridbot.mainnet.v1459_reconciliation_hook import (
    V1459TerminalReconciliationHook,
)


def test_client_algo_sl_prefix_proves_exit_fill_ownership() -> None:
    run_id = "cry3mn_exact"
    payloads = build_reconciliation_payloads(
        run_id=run_id,
        orders=[
            {"orderId": 11, "clientOrderId": f"{run_id}_entry"},
            {"algoId": 12, "clientAlgoId": f"{run_id}_sl"},
        ],
        trades=[
            {
                "id": 101,
                "orderId": 11,
                "maker": True,
                "realizedPnl": "0",
                "commission": "0.01",
                "commissionAsset": "USDC",
            },
            {
                "id": 102,
                "orderId": 12,
                "maker": False,
                "realizedPnl": "-0.10",
                "commission": "0.02",
                "commissionAsset": "USDC",
            },
        ],
    )

    result = reconcile_run(
        payloads.trades,
        payloads.incomes,
        require_closed_run=True,
    )

    assert result.reconciliation_status == "COMPLETE"
    assert [row["role"] for row in payloads.persistence_trades] == [
        "ENTRY",
        "EXIT",
    ]
    assert result.net_pnl_usdc == pytest.approx(-0.13)


class _Runtime:
    permits_order_mutation = False

    def __init__(self) -> None:
        self.flags = SimpleNamespace(record_reconciliation=True)
        self.calls = 0

    async def record_reconciliation(self, **kwargs):
        self.calls += 1
        raise AssertionError("latched collection failure must not persist")


@pytest.mark.asyncio
async def test_collection_failure_latches_before_persistence_or_rearm() -> None:
    runtime = _Runtime()
    hook = V1459TerminalReconciliationHook(runtime)

    first = hook.fail_closed(reason="income page may be truncated")
    second = await hook.record(
        trades=(),
        incomes=(),
        persistence_trades=(),
        persistence_incomes=(),
        run_id="cry3mn_exact",
        reconciliation_revision=0,
        reconciled_at_ms=1,
    )

    assert first is second
    assert first.continue_live is False
    assert first.status == "COLLECTION_ERROR"
    assert hook.entry_paused is True
    assert runtime.calls == 0
