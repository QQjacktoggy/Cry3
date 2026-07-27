import pytest

from src.gridbot.mainnet.run_reconciler import reconcile_run
from src.gridbot.mainnet.v1459_live_reconciliation import (
    build_reconciliation_payloads,
)


RUN_ID = "cry3mn_1459"


def _binance_trade(
    trade_id: int,
    order_id: int,
    *,
    maker: bool = True,
    realized_pnl: str = "0.0",
    commission: str = "0.01",
    commission_asset: str = "USDC",
    source: dict | None = None,
    **extra,
) -> dict:
    return {
        "id": trade_id,
        "orderId": order_id,
        "maker": maker,
        "realizedPnl": realized_pnl,
        "commission": commission,
        "commissionAsset": commission_asset,
        "source": source or {"collector": "account_trades"},
        **extra,
    }


@pytest.mark.parametrize(
    ("suffix", "expected_role"),
    [
        ("_entry", "ENTRY"),
        ("_entry_r2", "ENTRY"),
        ("_dca1", "ENTRY"),
        ("_dca2_pre", "ENTRY"),
        ("_close", "EXIT"),
        ("_tp1", "EXIT"),
        ("_tp2_floor", "EXIT"),
        ("_trail", "EXIT"),
        ("_be", "EXIT"),
        ("_no_bounce", "EXIT"),
        ("_dust", "EXIT"),
    ],
)
def test_run_client_order_suffix_proves_ownership_and_role(
    suffix: str, expected_role: str
) -> None:
    payloads = build_reconciliation_payloads(
        run_id=RUN_ID,
        orders=[{"orderId": 11, "clientOrderId": f"{RUN_ID}{suffix}"}],
        trades=[_binance_trade(101, 11)],
    )

    trade = payloads.reconciler_trades[0]
    assert trade["exchange_trade_id"] == "101"
    assert trade["owned"] is True
    assert trade["role"] == expected_role
    assert payloads.persistence_trades[0]["order_id"] == "11"
    assert reconcile_run(payloads.trades, payloads.incomes).reconciliation_status == "COMPLETE"


def test_unmapped_trade_order_never_trusts_trade_owned_flag_and_fails_closed() -> None:
    payloads = build_reconciliation_payloads(
        run_id=RUN_ID,
        orders=[
            {"orderId": 20, "clientOrderId": f"{RUN_ID}0_entry"},
            {"orderId": 21, "clientOrderId": "manual-entry"},
        ],
        trades=[_binance_trade(201, 21, owned=True, role="ENTRY")],
    )

    trade = payloads.reconciler_trades[0]
    result = reconcile_run(payloads.trades, payloads.incomes)
    assert trade["owned"] is False
    assert trade["role"] is None
    assert result.reconciliation_status == "DATA_INCOMPLETE"
    assert result.completeness_reasons == ("UNOWNED_TRADE:201",)
    assert payloads.persistence_trades == ()


def test_unknown_run_suffix_is_owned_but_has_no_guessed_role() -> None:
    payloads = build_reconciliation_payloads(
        run_id=RUN_ID,
        orders=[{"orderId": 30, "clientOrderId": f"{RUN_ID}_mystery"}],
        trades=[_binance_trade(301, 30)],
    )

    trade = payloads.reconciler_trades[0]
    result = reconcile_run(payloads.trades, payloads.incomes)
    assert trade["owned"] is True
    assert trade["role"] is None
    assert result.reconciliation_status == "DATA_INCOMPLETE"
    assert result.completeness_reasons == ("INVALID_TRADE_ROLE:301",)
    assert payloads.persistence_trades == ()


def test_explicit_sl_order_id_is_event_owned_exit_without_prefix_order() -> None:
    source = {"collector": "account_trades", "page": 2}
    payloads = build_reconciliation_payloads(
        run_id=RUN_ID,
        orders=[],
        trades=[
            _binance_trade(
                401,
                40,
                maker=False,
                realized_pnl="-0.25",
                source=source,
            )
        ],
        explicit_sl_order_ids={40},
    )

    reconciler_trade = payloads.reconciler_trades[0]
    persisted_trade = payloads.persistence_trades[0]
    assert reconciler_trade["owned"] is True
    assert reconciler_trade["role"] == "EXIT"
    assert reconciler_trade["is_maker"] is False
    assert reconciler_trade["realized_pnl_usdc"] == pytest.approx(-0.25)
    assert reconciler_trade["commission_amount"] == pytest.approx(0.01)
    assert reconciler_trade["commission_asset"] == "USDC"
    assert reconciler_trade["source"] == source
    assert persisted_trade["commission_usdc"] == pytest.approx(0.01)
    assert persisted_trade["source"] == source


def test_event_proven_actual_stop_fill_order_is_an_exit_without_run_prefix() -> None:
    payloads = build_reconciliation_payloads(
        run_id=RUN_ID,
        orders=[
            {"orderId": 11, "clientOrderId": f"{RUN_ID}_entry"},
            {"orderId": 99, "clientOrderId": "x-Cb7y-stop-algo"},
        ],
        trades=[
            _binance_trade(701, 11, realized_pnl="0", commission="0"),
            _binance_trade(702, 99, maker=False, realized_pnl="-0.10", commission="0.02"),
        ],
        event_owned_order_roles={99: "EXIT"},
    )

    result = reconcile_run(payloads.trades, payloads.incomes, require_closed_run=True)

    assert result.reconciliation_status == "COMPLETE"
    assert result.net_pnl_usdc == pytest.approx(-0.12)
    assert [row["role"] for row in payloads.persistence_trades] == ["ENTRY", "EXIT"]


def test_event_owned_order_still_requires_role_evidence() -> None:
    payloads = build_reconciliation_payloads(
        run_id=RUN_ID,
        orders=[{"orderId": 50, "clientOrderId": "x-algo-order"}],
        trades=[_binance_trade(501, 50)],
        event_owned_order_ids={50},
    )

    trade = payloads.reconciler_trades[0]
    result = reconcile_run(payloads.trades, payloads.incomes)
    assert trade["owned"] is True
    assert trade["role"] is None
    assert result.reconciliation_status == "DATA_INCOMPLETE"
    assert payloads.persistence_trades == ()


def test_non_usdc_commission_requires_explicit_rate_and_never_guesses() -> None:
    order = {"orderId": 60, "clientOrderId": f"{RUN_ID}_entry"}
    missing = build_reconciliation_payloads(
        run_id=RUN_ID,
        orders=[order],
        trades=[
            _binance_trade(
                601, 60, commission="0.00002", commission_asset="BNB"
            )
        ],
    )
    missing_result = reconcile_run(missing.trades, missing.incomes)
    assert "commission_conversion_rate_to_usdc" not in missing.trades[0]
    assert missing_result.reconciliation_status == "DATA_INCOMPLETE"
    assert missing_result.completeness_reasons == (
        "MISSING_COMMISSION_USDC_CONVERSION:601:BNB",
    )
    assert missing.persistence_trades == ()

    explicit = build_reconciliation_payloads(
        run_id=RUN_ID,
        orders=[order],
        trades=[
            _binance_trade(
                602,
                60,
                commission="0.00002",
                commission_asset="BNB",
                commission_conversion_rate_to_usdc="600",
            )
        ],
    )
    assert reconcile_run(explicit.trades, explicit.incomes).reconciliation_status == "COMPLETE"
    assert explicit.trades[0]["commission_conversion_rate_to_usdc"] == pytest.approx(600.0)
    assert explicit.persistence_trades[0]["commission_usdc"] == pytest.approx(0.012)


@pytest.mark.parametrize("owned", [None, False, "true"])
def test_funding_requires_caller_explicit_owned_true(owned) -> None:
    income = {
        "tranId": 701,
        "incomeType": "FUNDING_FEE",
        "income": "-0.02",
        "asset": "USDC",
        "source": {"collector": "income_history"},
    }
    if owned is not None:
        income["owned"] = owned

    payloads = build_reconciliation_payloads(
        run_id=RUN_ID,
        orders=[],
        trades=[],
        funding_incomes=[income],
    )
    result = reconcile_run(payloads.trades, payloads.incomes)
    assert payloads.reconciler_incomes[0]["owned"] is False
    assert result.reconciliation_status == "DATA_INCOMPLETE"
    assert result.completeness_reasons == ("UNOWNED_INCOME:701",)
    assert payloads.persistence_incomes == ()


def test_owned_funding_preserves_id_amount_asset_and_source() -> None:
    source = {"collector": "income_history", "window": "run"}
    payloads = build_reconciliation_payloads(
        run_id=RUN_ID,
        orders=[],
        trades=[],
        funding_incomes=[
            {
                "tranId": 801,
                "incomeType": "FUNDING_FEE",
                "income": "-0.02",
                "asset": "USDC",
                "owned": True,
                "source": source,
            }
        ],
    )

    income = payloads.reconciler_incomes[0]
    persisted = payloads.persistence_incomes[0]
    result = reconcile_run(payloads.trades, payloads.incomes)
    assert result.reconciliation_status == "COMPLETE"
    assert result.funding_usdc == pytest.approx(0.02)
    assert income == {
        "exchange_income_id": "801",
        "owned": True,
        "income_type": "FUNDING_FEE",
        "amount": -0.02,
        "asset": "USDC",
        "source": source,
    }
    assert persisted["exchange_income_id"] == "801"
    assert persisted["amount_usdc"] == pytest.approx(-0.02)
    assert persisted["source"] == source


def test_non_usdc_funding_without_rate_stays_incomplete() -> None:
    payloads = build_reconciliation_payloads(
        run_id=RUN_ID,
        orders=[],
        trades=[],
        funding_incomes=[
            {
                "tranId": 901,
                "incomeType": "FUNDING_FEE",
                "income": "-0.001",
                "asset": "BNB",
                "owned": True,
                "source": {"collector": "income_history"},
            }
        ],
    )

    result = reconcile_run(payloads.trades, payloads.incomes)
    assert "amount_conversion_rate_to_usdc" not in payloads.incomes[0]
    assert result.reconciliation_status == "DATA_INCOMPLETE"
    assert result.completeness_reasons == (
        "MISSING_FUNDING_USDC_CONVERSION:901:BNB",
    )
    assert payloads.persistence_incomes == ()


def test_valid_children_remain_id_aligned_when_other_evidence_is_incomplete() -> None:
    payloads = build_reconciliation_payloads(
        run_id=RUN_ID,
        orders=[{"orderId": 100, "clientOrderId": f"{RUN_ID}_entry"}],
        trades=[_binance_trade(1001, 100), _binance_trade(1002, 999)],
        funding_incomes=[],
    )

    result = reconcile_run(payloads.trades, payloads.incomes)
    assert result.reconciliation_status == "DATA_INCOMPLETE"
    assert result.exchange_trade_ids == ("1001",)
    assert tuple(
        row["exchange_trade_id"] for row in payloads.persistence_trades
    ) == result.exchange_trade_ids
    assert set(payloads.as_record_reconciliation_kwargs()) == {
        "trades",
        "incomes",
        "persistence_trades",
        "persistence_incomes",
    }
