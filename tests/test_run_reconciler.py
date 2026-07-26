import pytest

from src.gridbot.mainnet.run_reconciler import reconcile_run


def _trade(
    trade_id: str,
    *,
    role: str = "ENTRY",
    maker: bool = True,
    realized: float = 0.0,
    commission: float = 0.01,
    asset: str = "USDC",
    owned: object = True,
    rate: float | None = None,
) -> dict:
    result = {
        "exchange_trade_id": trade_id,
        "owned": owned,
        "role": role,
        "is_maker": maker,
        "realized_pnl_usdc": realized,
        "commission_amount": commission,
        "commission_asset": asset,
    }
    if rate is not None:
        result["commission_conversion_rate_to_usdc"] = rate
    return result


def _funding(
    income_id: str,
    *,
    amount: float = -0.015,
    asset: str = "USDC",
    owned: object = True,
    rate: float | None = None,
) -> dict:
    result = {
        "exchange_income_id": income_id,
        "owned": owned,
        "income_type": "FUNDING_FEE",
        "amount": amount,
        "asset": asset,
    }
    if rate is not None:
        result["amount_conversion_rate_to_usdc"] = rate
    return result


def test_complete_reconciliation_counts_maker_taker_and_signed_funding_cost():
    result = reconcile_run(
        [
            _trade("entry-1", realized=0.0, maker=True, commission=0.01),
            _trade(
                "exit-1",
                role="exit",
                maker=False,
                realized=0.32,
                commission=0.00002,
                asset="BNB",
                rate=600.0,
            ),
        ],
        [_funding("funding-1", amount=-0.015)],
    )

    assert result.reconciliation_status == "COMPLETE"
    assert result.eligible_for_wr_ev is True
    assert result.gross_realized_pnl_usdc == pytest.approx(0.32)
    assert result.commission_usdc == pytest.approx(0.022)
    assert result.funding_usdc == pytest.approx(0.015)
    assert result.net_pnl_usdc == pytest.approx(0.283)
    assert (result.entry_maker_fills, result.entry_taker_fills) == (1, 0)
    assert (result.exit_maker_fills, result.exit_taker_fills) == (0, 1)


def test_exact_duplicate_ids_dedupe_but_conflicting_duplicate_ids_fail_closed():
    original = _trade("trade-1", realized=0.10)
    exact_duplicate = dict(original)
    complete = reconcile_run([original, exact_duplicate], [])
    assert complete.reconciliation_status == "COMPLETE"
    assert complete.exchange_trade_ids == ("trade-1",)
    assert complete.gross_realized_pnl_usdc == pytest.approx(0.10)

    conflict = dict(original, realized_pnl_usdc=0.20)
    incomplete = reconcile_run([original, conflict], [])
    assert incomplete.reconciliation_status == "DATA_INCOMPLETE"
    assert incomplete.completeness_reasons == ("CONFLICTING_TRADE_ID:trade-1",)
    assert incomplete.eligible_for_wr_ev is False
    assert incomplete.net_pnl_usdc is None


@pytest.mark.parametrize(
    ("trades", "incomes", "reason"),
    [
        ([dict(_trade("t-1"), exchange_trade_id="")], [], "MISSING_TRADE_ID"),
        ([_trade("t-1", asset="BNB")], [], "MISSING_COMMISSION_USDC_CONVERSION:t-1:BNB"),
        ([_trade("t-1", owned="unknown")], [], "UNKNOWN_TRADE_OWNERSHIP:t-1"),
        ([], [_funding("f-1", owned="unknown")], "UNKNOWN_INCOME_OWNERSHIP:f-1"),
        ([], [_funding("f-1", asset="BNB")], "MISSING_FUNDING_USDC_CONVERSION:f-1:BNB"),
    ],
)
def test_incomplete_inputs_are_stable_and_never_eligible(trades, incomes, reason):
    result = reconcile_run(trades, incomes)

    assert result.reconciliation_status == "DATA_INCOMPLETE"
    assert result.eligible_for_wr_ev is False
    assert reason in result.completeness_reasons
    assert result.gross_realized_pnl_usdc is None
    assert result.commission_usdc is None
    assert result.funding_usdc is None
    assert result.net_pnl_usdc is None


def test_unowned_funding_is_never_attributed_even_when_other_evidence_is_valid():
    result = reconcile_run(
        [_trade("exit-1", role="EXIT", realized=0.10)],
        [_funding("other-run-funding", amount=-9.0, owned=False)],
    )

    assert result.reconciliation_status == "DATA_INCOMPLETE"
    assert result.completeness_reasons == ("UNOWNED_INCOME:other-run-funding",)
    assert result.funding_usdc is None
    assert result.eligible_for_wr_ev is False


@pytest.mark.parametrize(
    ("trades", "reason"),
    [
        ([], "MISSING_ENTRY_TRADE"),
        ([_trade("entry-only")], "MISSING_EXIT_TRADE"),
        ([_trade("exit-only", role="EXIT")], "MISSING_ENTRY_TRADE"),
    ],
)
def test_closed_run_requires_entry_and_exit_exchange_trades(trades, reason):
    result = reconcile_run(trades, [], require_closed_run=True)

    assert result.reconciliation_status == "DATA_INCOMPLETE"
    assert result.eligible_for_wr_ev is False
    assert reason in result.completeness_reasons
    assert result.net_pnl_usdc is None
