import pytest

from src.gridbot.binance.models import FuturesTrade, IncomeRecord
from src.gridbot.testnet.pnl import calculate_testnet_pnl_breakdown


def test_calculate_testnet_pnl_breakdown_splits_maker_and_non_maker_fees():
    records = [
        IncomeRecord(1, "ETHUSDC", "REALIZED_PNL", 2.0, "USDC", 1, "", "101"),
        IncomeRecord(2, "ETHUSDC", "COMMISSION", -0.1, "USDC", 1, "", "101"),
        IncomeRecord(3, "ETHUSDC", "COMMISSION", -0.2, "USDC", 1, "", "102"),
        IncomeRecord(4, "ETHUSDC", "FUNDING_FEE", -0.05, "USDC", 1, "", ""),
    ]
    trades = [
        FuturesTrade(
            trade_id=101,
            order_id=201,
            symbol="ETHUSDC",
            side="BUY",
            price=2100.0,
            qty=0.01,
            quote_qty=21.0,
            realized_pnl=0.0,
            commission=0.1,
            commission_asset="USDC",
            time_ms=1,
            position_side="BOTH",
            is_buyer=True,
            is_maker=True,
        ),
        FuturesTrade(
            trade_id=102,
            order_id=202,
            symbol="ETHUSDC",
            side="SELL",
            price=2105.0,
            qty=0.01,
            quote_qty=21.05,
            realized_pnl=0.0,
            commission=0.2,
            commission_asset="USDC",
            time_ms=1,
            position_side="BOTH",
            is_buyer=False,
            is_maker=False,
        ),
    ]

    breakdown = calculate_testnet_pnl_breakdown(records, trades=trades)

    assert breakdown.realized == pytest.approx(2.0)
    assert breakdown.funding == pytest.approx(-0.05)
    assert breakdown.maker_fee == pytest.approx(0.1)
    assert breakdown.non_maker_fee == pytest.approx(0.2)
    assert breakdown.effective_net(ignore_maker_fees=True) == pytest.approx(1.75)
    assert breakdown.full_net == pytest.approx(1.65)


def test_calculate_testnet_pnl_breakdown_tracks_residual_cleanup_fee_separately():
    records = [
        IncomeRecord(1, "ETHUSDC", "COMMISSION", -0.1, "USDC", 1, "", "101"),
        IncomeRecord(2, "ETHUSDC", "COMMISSION", -0.2, "USDC", 1, "", "102"),
    ]
    trades = [
        FuturesTrade(
            trade_id=101,
            order_id=201,
            symbol="ETHUSDC",
            side="BUY",
            price=2100.0,
            qty=0.01,
            quote_qty=21.0,
            realized_pnl=0.0,
            commission=0.1,
            commission_asset="USDC",
            time_ms=1,
            position_side="BOTH",
            is_buyer=True,
            is_maker=True,
        ),
        FuturesTrade(
            trade_id=102,
            order_id=202,
            symbol="ETHUSDC",
            side="SELL",
            price=2105.0,
            qty=0.01,
            quote_qty=21.05,
            realized_pnl=0.0,
            commission=0.2,
            commission_asset="USDC",
            time_ms=1,
            position_side="BOTH",
            is_buyer=False,
            is_maker=False,
        ),
    ]

    breakdown = calculate_testnet_pnl_breakdown(
        records,
        trades=trades,
        residual_cleanup_order_ids={202},
    )

    assert breakdown.residual_cleanup_fee == pytest.approx(0.2)
