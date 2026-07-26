import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest

from src.gridbot.core.app import App


def _result(*, errors=()):
    return SimpleNamespace(
        ok=not errors,
        requested_limit=100,
        enumerated_claims=1,
        processed_claims=1,
        lookup_calls=1,
        visible_orders=1,
        absent_orders=0,
        transitioned_claims=1,
        already_submitted_claims=0,
        telemetry=(),
        errors=tuple(errors),
    )


def _bare_app(*, enforcement_enabled: bool) -> App:
    app = object.__new__(App)
    app.settings = SimpleNamespace(
        mainnet_codex_v1469_live_enforcement_enabled=enforcement_enabled,
        mainnet_codex_v1469_per_trade_loss_cap_usdc=0.15,
    )
    app.v1469_authority_ready = True
    return app


def test_enforcement_off_has_no_reconciler_or_exchange_lookup_side_effect() -> None:
    app = _bare_app(enforcement_enabled=False)
    app.v1469_paid_reconciler = SimpleNamespace()
    app.mainnet_binance = SimpleNamespace()

    result = asyncio.run(app._reconcile_v1469_paid_claims_on_restart())

    assert result is None


def test_enforcement_on_reconciles_mainnet_with_bounded_read_only_lookup() -> None:
    calls: list[tuple[str, str]] = []

    class LookupOnlyClient:
        async def get_order_by_client_order_id(
            self, symbol: str, client_order_id: str
        ):
            calls.append((symbol, client_order_id))
            return {
                "symbol": symbol,
                "clientOrderId": client_order_id,
                "status": "NEW",
            }

    class RecordingReconciler:
        kwargs = None

        async def reconcile_on_restart(self, **kwargs):
            self.kwargs = kwargs
            await kwargs["find_by_client_order_id"](
                "BTCUSDC", "cry3v1469_expected"
            )
            return _result()

    app = _bare_app(enforcement_enabled=True)
    app.mainnet_binance = LookupOnlyClient()
    app.v1469_paid_reconciler = RecordingReconciler()

    result = asyncio.run(app._reconcile_v1469_paid_claims_on_restart())

    assert result.ok is True
    assert calls == [("BTCUSDC", "cry3v1469_expected")]
    assert app.v1469_paid_reconciler.kwargs["environment"] == "MAINNET"
    assert app.v1469_paid_reconciler.kwargs["symbol"] is None
    assert app.v1469_paid_reconciler.kwargs["limit"] == 100
    assert app.v1469_paid_reconciler.kwargs["now_ms"] > 0


def test_enforcement_on_fails_startup_closed_for_unclean_result() -> None:
    class UncleanReconciler:
        async def reconcile_on_restart(self, **_kwargs):
            return _result(
                errors=(
                    SimpleNamespace(
                        code="LOOKUP_ERROR",
                        detail="transport ambiguous",
                    ),
                )
            )

    app = _bare_app(enforcement_enabled=True)
    app.mainnet_binance = SimpleNamespace(
        get_order_by_client_order_id=lambda *_args: None
    )
    app.v1469_paid_reconciler = UncleanReconciler()

    with pytest.raises(
        RuntimeError,
        match="not clean: LOOKUP_ERROR:transport ambiguous",
    ):
        asyncio.run(app._reconcile_v1469_paid_claims_on_restart())


def test_enforcement_on_fails_startup_closed_for_reconciler_exception() -> None:
    class FailingReconciler:
        async def reconcile_on_restart(self, **_kwargs):
            raise OSError("repository unavailable")

    app = _bare_app(enforcement_enabled=True)
    app.mainnet_binance = SimpleNamespace(
        get_order_by_client_order_id=lambda *_args: None
    )
    app.v1469_paid_reconciler = FailingReconciler()

    with pytest.raises(
        RuntimeError,
        match="paid restart reconciliation failed: OSError:repository unavailable",
    ):
        asyncio.run(app._reconcile_v1469_paid_claims_on_restart())


def test_restart_reconciliation_is_between_connect_and_scheduler_start() -> None:
    source = inspect.getsource(App.start)

    assert source.index("await self.initialize()") < source.index(
        "await self._reconcile_v1469_paid_claims_on_restart()"
    )
    assert source.index(
        "await self._reconcile_v1469_paid_claims_on_restart()"
    ) < source.index("self.scheduler.start()")

def _paid_completed_run() -> dict:
    return {
        "run_id": "run-paid-1",
        "status": "COMPLETED",
        "signal_json": json.dumps(
            {
                "v1469_paid_claim_id": "claim-1",
                "v1469_paid_execution": {
                    "schema": "v1469.paid-execution-plan.1",
                },
            }
        ),
    }


def _completed_event(**overrides) -> dict:
    details = {
        "eligible_for_wr_ev": True,
        "reconciliation_status": "COMPLETE",
        "exit_reason_final": "SL",
        "net_pnl": -0.15,
        "terminal_at_ms": 12_345,
    }
    details.update(overrides)
    return {"event_type": "completed", "details_json": json.dumps(details)}


def test_terminal_backfill_is_dormant_when_enforcement_is_off() -> None:
    app = _bare_app(enforcement_enabled=False)
    app.mainnet_run_repo = SimpleNamespace()
    app.v1469_paid_close_runtime = SimpleNamespace()

    assert (
        asyncio.run(app._backfill_v1469_paid_terminal_claims_on_restart())
        == 0
    )


def test_terminal_backfill_repairs_one_exact_completed_paid_run() -> None:
    class Repo:
        recent_limit = None
        event_args = None

        async def get_recent_runs(self, limit):
            self.recent_limit = limit
            return [_paid_completed_run()]

        async def get_events_by_types(
            self, run_id, event_types, limit=None
        ):
            self.event_args = (run_id, event_types, limit)
            return [_completed_event()]

    class CloseRuntime:
        kwargs = None

        async def record_close(self, **kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(replayed=False)

    app = _bare_app(enforcement_enabled=True)
    app.mainnet_run_repo = Repo()
    app.v1469_paid_close_runtime = CloseRuntime()

    repaired = asyncio.run(
        app._backfill_v1469_paid_terminal_claims_on_restart()
    )

    assert repaired == 1
    assert app.mainnet_run_repo.recent_limit == 100
    assert app.mainnet_run_repo.event_args == (
        "run-paid-1",
        ("completed",),
        5,
    )
    assert app.v1469_paid_close_runtime.kwargs == {
        "claim_id": "claim-1",
        "fee_net_pnl_usdc": -0.15,
        "terminal_reason": "SL",
        "occurred_at_ms": 12_345,
        "source_run_id": "run-paid-1",
        "hard_loss_marker": True,
        "actor": "v1469-startup-terminal-backfill",
    }


def test_terminal_backfill_ignores_completed_legacy_run() -> None:
    class Repo:
        async def get_recent_runs(self, limit):
            assert limit == 100
            return [
                {
                    "run_id": "legacy",
                    "status": "COMPLETED",
                    "signal_json": "{}",
                }
            ]

        async def get_events_by_types(self, *_args, **_kwargs):
            raise AssertionError("legacy run must not read completed events")

    class CloseRuntime:
        async def record_close(self, **_kwargs):
            raise AssertionError("legacy run must not close a paid claim")

    app = _bare_app(enforcement_enabled=True)
    app.mainnet_run_repo = Repo()
    app.v1469_paid_close_runtime = CloseRuntime()

    assert (
        asyncio.run(app._backfill_v1469_paid_terminal_claims_on_restart())
        == 0
    )


@pytest.mark.parametrize(
    "event",
    [
        _completed_event(eligible_for_wr_ev=False),
        _completed_event(reconciliation_status="INCOMPLETE"),
        _completed_event(net_pnl=float("nan")),
        _completed_event(terminal_at_ms=0),
    ],
)
def test_terminal_backfill_fails_startup_closed_for_inexact_facts(
    event,
) -> None:
    class Repo:
        async def get_recent_runs(self, limit):
            assert limit == 100
            return [_paid_completed_run()]

        async def get_events_by_types(self, *_args, **_kwargs):
            return [event]

    app = _bare_app(enforcement_enabled=True)
    app.mainnet_run_repo = Repo()
    app.v1469_paid_close_runtime = SimpleNamespace()

    with pytest.raises(
        RuntimeError,
        match="v1.4.69 paid terminal backfill failed",
    ):
        asyncio.run(app._backfill_v1469_paid_terminal_claims_on_restart())


def test_terminal_backfill_fails_startup_closed_for_replay_conflict() -> None:
    class Repo:
        async def get_recent_runs(self, limit):
            assert limit == 100
            return [_paid_completed_run()]

        async def get_events_by_types(self, *_args, **_kwargs):
            return [_completed_event()]

    class CloseRuntime:
        async def record_close(self, **_kwargs):
            raise RuntimeError("terminal paid close facts differ")

    app = _bare_app(enforcement_enabled=True)
    app.mainnet_run_repo = Repo()
    app.v1469_paid_close_runtime = CloseRuntime()

    with pytest.raises(
        RuntimeError,
        match="terminal paid close facts differ",
    ):
        asyncio.run(app._backfill_v1469_paid_terminal_claims_on_restart())


def test_app_wires_paid_runtimes_only_after_authority_readiness() -> None:
    init_source = inspect.getsource(App.__init__)
    start_source = inspect.getsource(App.start)

    for runtime_name in (
        "v1469_paid_promotion_runtime",
        "v1469_authority_runtime",
        "v1469_risk_runtime",
        "v1469_paid_entry_runtime",
        "v1469_paid_close_runtime",
    ):
        assert runtime_name in init_source
    assert "migrations 016 through 024" in inspect.getsource(App.initialize)
    assert start_source.index(
        "await self._reconcile_v1469_paid_claims_on_restart()"
    ) < start_source.index(
        "await self._backfill_v1469_paid_terminal_claims_on_restart()"
    )
    assert start_source.index(
        "await self._backfill_v1469_paid_terminal_claims_on_restart()"
    ) < start_source.index("self.scheduler.start()")
    assert (
        start_source.count(
            "if self.v1469_authority_ready\n                    else None"
        )
        >= 5
    )
    assert (
        "self.v1469_lease_repo if self.v1469_authority_ready else None"
        in start_source
    )
