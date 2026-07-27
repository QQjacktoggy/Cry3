from __future__ import annotations

from decimal import Decimal, localcontext
import json

import pytest

from src.gridbot.mainnet.v1459_canary_runtime import (
    CanaryRuntimeError,
    V1459CanaryRuntime,
    aggregate_canary_kpi_snapshot,
    aggregate_canary_snapshot,
    build_canary_kpi_snapshot,
)


_START = 1_000_000
_DEADLINE = _START + 72 * 60 * 60 * 1000


def _membership(run_id: str, index: int, revision: int = 1) -> dict[str, object]:
    return {
        "session_id": "session-1",
        "opportunity_id": f"opportunity-{index}",
        "run_id": run_id,
        "reconciliation_revision": revision,
        "symbol": "ETHUSDC",
        "environment": "mainnet",
        "account_fingerprint": "account-1",
    }


def _session(run_ids: list[str], *, observed_at: int = _START) -> dict[str, object]:
    memberships = {
        run_id: _membership(run_id, index)
        for index, run_id in enumerate(run_ids, start=1)
    }
    return {
        "session_id": "session-1",
        "environment": "mainnet",
        "account_fingerprint": "account-1",
        "symbol": "ETHUSDC",
        "accepted_opportunities": len(run_ids),
        "counters_json": json.dumps({"accepted_opportunities": len(run_ids)}),
        "run_memberships": memberships,
        "started_at_ms": _START,
        "last_checkpoint_at_ms": observed_at,
    }


def _scope(run_id: str, index: int, revision: int = 1) -> dict[str, object]:
    return {
        "session_id": "session-1",
        "opportunity_id": f"opportunity-{index}",
        "run_id": run_id,
        "reconciliation_revision": revision,
        "environment": "mainnet",
        "account_fingerprint": "account-1",
        "symbol": "ETHUSDC",
    }


def _complete(
    run_id: str,
    index: int,
    *,
    gross: str = "0.10",
    commission: str = "0.01",
    funding: str = "0",
    net: str = "0.09",
    entry_fills: int = 1,
    exit_fills: int = 1,
    eligible: bool | None = True,
    trade_ids: tuple[str, ...] | None = None,
    income_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    source: dict[str, object] = {
        "exchange_trade_ids": list(
            trade_ids if trade_ids is not None else (f"trade-{index}",)
        ),
        "exchange_income_ids": list(income_ids),
    }
    if eligible is not None:
        source["eligible_for_wr_ev"] = eligible
    return {
        **_scope(run_id, index),
        "reconciliation_status": "COMPLETE",
        "gross_realized_pnl_usdc": Decimal(gross),
        "commission_usdc": Decimal(commission),
        "funding_usdc": Decimal(funding),
        "net_pnl_usdc": Decimal(net),
        "entry_fills": entry_fills,
        "exit_fills": exit_fills,
        "source_json": json.dumps(source),
    }


def _incomplete(run_id: str, index: int) -> dict[str, object]:
    return {
        **_scope(run_id, index),
        "reconciliation_status": "DATA_INCOMPLETE",
    }


def test_exact_paid_closed_fills_build_existing_snapshot_contract() -> None:
    session = _session(["win", "loss", "open"])
    records = [
        _complete("win", 1),
        _complete(
            "loss",
            2,
            gross="-0.02",
            commission="0.01",
            net="-0.03",
        ),
    ]

    snapshot = build_canary_kpi_snapshot(session, records)

    assert snapshot.paid_closed_fills == 2
    assert snapshot.exact_reconciled_paid_closed_fills == 2
    assert (snapshot.wins, snapshot.losses, snapshot.flats) == (1, 1, 0)
    assert snapshot.accepted_opportunities == 3
    assert snapshot.net_pnl_usdc == Decimal("0.06")
    assert snapshot.incomplete_reconciliations == 0
    assert snapshot.deadline_reached is False
    runtime = V1459CanaryRuntime()
    assert runtime.permits_order_mutation is False
    assert runtime.aggregate(session, records) == snapshot
    assert aggregate_canary_kpi_snapshot(session, records) == snapshot
    assert aggregate_canary_snapshot(session, records) == snapshot


def test_no_fill_incomplete_one_sided_and_ineligible_never_enter_wr_or_pnl() -> None:
    run_ids = ["paid", "no-fill", "partial", "ineligible", "incomplete"]
    records = [
        _complete("paid", 1),
        _complete(
            "no-fill",
            2,
            gross="0",
            commission="0",
            net="0",
            entry_fills=0,
            exit_fills=0,
            eligible=None,
            trade_ids=(),
        ),
        _complete("partial", 3, exit_fills=0),
        _complete("ineligible", 4, eligible=False),
        _incomplete("incomplete", 5),
    ]

    snapshot = build_canary_kpi_snapshot(_session(run_ids), records)

    assert snapshot.paid_closed_fills == 1
    assert snapshot.wins == 1
    assert snapshot.net_pnl_usdc == Decimal("0.09")
    assert snapshot.incomplete_reconciliations == 3


def test_missing_eligibility_is_not_implicitly_true() -> None:
    record = _complete("run-1", 1, eligible=None)
    snapshot = build_canary_kpi_snapshot(_session(["run-1"]), [record])
    assert snapshot.paid_closed_fills == 0
    assert snapshot.incomplete_reconciliations == 1


def test_exact_duplicate_retry_collapses_but_conflicting_duplicate_fails() -> None:
    session = _session(["run-1"])
    record = _complete("run-1", 1)
    assert build_canary_kpi_snapshot(session, [record, dict(record)]).paid_closed_fills == 1

    conflict = dict(record)
    conflict["net_pnl_usdc"] = Decimal("0.08")
    with pytest.raises(CanaryRuntimeError, match="not exact|conflicting duplicate"):
        build_canary_kpi_snapshot(session, [record, conflict])


@pytest.mark.parametrize("id_field", ["exchange_trade_ids", "exchange_income_ids"])
def test_exchange_ids_cannot_be_reused_across_runs(id_field: str) -> None:
    first = _complete("run-1", 1, income_ids=("income-shared",))
    second = _complete("run-2", 2, income_ids=("income-shared",))
    if id_field == "exchange_trade_ids":
        for record in (first, second):
            source = json.loads(str(record["source_json"]))
            source["exchange_trade_ids"] = ["trade-shared"]
            source["exchange_income_ids"] = []
            record["source_json"] = json.dumps(source)
    with pytest.raises(CanaryRuntimeError, match=id_field[:-1]):
        build_canary_kpi_snapshot(_session(["run-1", "run-2"]), [first, second])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("session_id", "other", "session_id"),
        ("opportunity_id", "other", "opportunity_id"),
        ("environment", "testnet", "environment"),
        ("account_fingerprint", "other", "account_fingerprint"),
        ("symbol", "BTCUSDC", "symbol"),
        ("reconciliation_revision", 2, "reconciliation_revision"),
    ],
)
def test_scope_and_membership_conflicts_fail_closed(
    field: str, value: object, message: str
) -> None:
    record = _complete("run-1", 1)
    record[field] = value
    with pytest.raises(CanaryRuntimeError, match=message):
        build_canary_kpi_snapshot(_session(["run-1"]), [record])


def test_out_of_membership_reconciliation_fails_closed() -> None:
    with pytest.raises(CanaryRuntimeError, match="outside accepted membership"):
        build_canary_kpi_snapshot(_session([]), [_complete("run-1", 1)])


def test_fixed_point_accounting_is_independent_of_decimal_context() -> None:
    session = _session(["run-1", "run-2"])
    records = [
        _complete(
            "run-1",
            1,
            gross="123456789012345678.000000000001",
            commission="0.000000000001",
            net="123456789012345678.000000000000",
        ),
        _complete(
            "run-2",
            2,
            gross="-123456789012345677.999999999998",
            commission="0.000000000001",
            net="-123456789012345677.999999999999",
        ),
    ]
    with localcontext() as context:
        context.prec = 2
        snapshot = build_canary_kpi_snapshot(session, records)
    assert snapshot.net_pnl_usdc == Decimal("0.000000000001")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("net_pnl_usdc", Decimal("NaN"), "finite"),
        ("net_pnl_usdc", Decimal("0.0000000000001"), "12 decimal places"),
        ("gross_realized_pnl_usdc", Decimal("1000000000000000000"), "18 integer digits"),
        ("commission_usdc", Decimal("-0.01"), "non-negative"),
    ],
)
def test_invalid_money_evidence_fails_closed(
    field: str, value: Decimal, message: str
) -> None:
    record = _complete("run-1", 1)
    record[field] = value
    with pytest.raises(CanaryRuntimeError, match=message):
        build_canary_kpi_snapshot(_session(["run-1"]), [record])


def test_conflicting_gross_alias_and_non_exact_identity_fail_closed() -> None:
    alias_conflict = _complete("run-1", 1)
    alias_conflict["gross_pnl_usdc"] = Decimal("0.11")
    with pytest.raises(CanaryRuntimeError, match="conflicting gross"):
        build_canary_kpi_snapshot(_session(["run-1"]), [alias_conflict])

    non_exact = _complete("run-1", 1)
    non_exact["net_pnl_usdc"] = Decimal("0.08")
    with pytest.raises(CanaryRuntimeError, match="not exact"):
        build_canary_kpi_snapshot(_session(["run-1"]), [non_exact])


def test_paid_fill_requires_trade_id_provenance() -> None:
    record = _complete("run-1", 1, trade_ids=())
    with pytest.raises(CanaryRuntimeError, match="requires exchange_trade_ids"):
        build_canary_kpi_snapshot(_session(["run-1"]), [record])


def test_deadline_is_derived_and_explicit_values_only_cross_check() -> None:
    session = _session([], observed_at=_DEADLINE)
    session["deadline_at_ms"] = _DEADLINE
    session["deadline_reached"] = True
    assert build_canary_kpi_snapshot(session, []).deadline_reached is True

    session["deadline_reached"] = False
    with pytest.raises(CanaryRuntimeError, match="conflicts with timestamp"):
        build_canary_kpi_snapshot(session, [])


def test_deadline_requires_timestamp_evidence_and_immutable_boundary() -> None:
    missing = _session([])
    del missing["last_checkpoint_at_ms"]
    with pytest.raises(CanaryRuntimeError, match="observed timestamp"):
        build_canary_kpi_snapshot(missing, [])

    wrong = _session([])
    wrong["deadline_at_ms"] = _DEADLINE + 1
    with pytest.raises(CanaryRuntimeError, match="immutable 72h"):
        build_canary_kpi_snapshot(wrong, [])


def test_accepted_denominator_and_membership_count_must_agree() -> None:
    session = _session(["run-1"])
    session["accepted_opportunities"] = 2
    session["counters_json"] = json.dumps({"accepted_opportunities": 2})
    with pytest.raises(CanaryRuntimeError, match="run memberships"):
        build_canary_kpi_snapshot(session, [])

    session = _session(["run-1"])
    session["counters"] = {"accepted_opportunities": 2}
    with pytest.raises(CanaryRuntimeError, match="conflicting accepted"):
        build_canary_kpi_snapshot(session, [])


def test_more_than_twenty_paid_fills_fails_closed() -> None:
    run_ids = [f"run-{index}" for index in range(21)]
    records = [_complete(run_id, index) for index, run_id in enumerate(run_ids, 1)]
    with pytest.raises(CanaryRuntimeError, match="exceed the canary target"):
        build_canary_kpi_snapshot(_session(run_ids), records)
