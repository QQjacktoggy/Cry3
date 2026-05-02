"""Tests for grid analyzer — verifies P&L computation and grid-only filtering.

Tests the core metric calculations that are critical for data accuracy.
"""

import unittest

from src.gridbot.binance.models import (
    FetchResult,
    FuturesTrade,
    IncomeRecord,
    MarketSnapshot,
    PositionInfo,
)
from src.gridbot.grid.analyzer import compute_metrics


def _make_trade(
    trade_id: int,
    symbol: str = "BTCUSDC",
    side: str = "BUY",
    price: float = 80000.0,
    qty: float = 0.001,
    commission: float = 0.01,
    realized_pnl: float = 0.0,
    is_maker: bool = True,
    time_ms: int = 1700000000000,
) -> FuturesTrade:
    return FuturesTrade(
        trade_id=trade_id,
        order_id=trade_id * 10,
        symbol=symbol,
        side=side,
        price=price,
        qty=qty,
        quote_qty=price * qty,
        realized_pnl=realized_pnl,
        commission=commission,
        commission_asset="USDC",
        time_ms=time_ms,
        position_side="BOTH",
        is_buyer=(side == "BUY"),
        is_maker=is_maker,
    )


def _make_income(
    tran_id: int,
    income_type: str = "REALIZED_PNL",
    income: float = 1.5,
    symbol: str = "BTCUSDC",
    trade_id: str = "",
    time_ms: int = 1700000000000,
) -> IncomeRecord:
    return IncomeRecord(
        tran_id=tran_id,
        symbol=symbol,
        income_type=income_type,
        income=income,
        asset="USDC",
        time_ms=time_ms,
        info="",
        trade_id=trade_id,
    )


def _make_market(symbol: str = "BTCUSDC", price: float = 80000.0) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        current_price=price,
        high_24h=price * 1.01,
        low_24h=price * 0.99,
        volume_24h=100000,
        price_change_pct_24h=0.5,
        funding_rate=0.0001,
        next_funding_time_ms=1700000000000,
        mark_price=price,
        klines=[],
    )


def _make_position(
    symbol: str = "BTCUSDC",
    amt: float = 0.001,
    entry: float = 79000.0,
    leverage: int = 3,
    liq: float = 75000.0,
) -> PositionInfo:
    return PositionInfo(
        symbol=symbol,
        position_amt=amt,
        entry_price=entry,
        mark_price=80000.0,
        unrealized_pnl=1.0,
        leverage=leverage,
        liquidation_price=liq,
        margin_type="isolated",
    )


class TestComputeMetrics(unittest.TestCase):
    """Test compute_metrics with various inputs."""

    def test_basic_metrics_from_trades(self):
        """Metrics should compute from trade data when no income records."""
        trades = [
            _make_trade(1, side="BUY", price=80000, qty=0.001, commission=0.01, time_ms=1700000000000),
            _make_trade(2, side="SELL", price=80100, qty=0.001, commission=0.01, time_ms=1700000060000),
        ]
        market = _make_market()
        position = _make_position()
        result = FetchResult(
            symbol="BTCUSDC", trades=trades, income_records=[],
            market=market, position=position, account=None,
        )
        m = compute_metrics(result)
        self.assertEqual(m.total_trades, 2)
        self.assertEqual(m.buy_trades, 1)
        self.assertEqual(m.sell_trades, 1)
        # commission_total comes from income records, not trades.
        # With no income records provided, income_records defaults to
        # result.income_records (empty []), so commission is 0.
        self.assertAlmostEqual(m.commission_total, 0.0, places=4)

    def test_income_based_pnl(self):
        """When income records are provided, they should drive P&L calculation."""
        trades = [_make_trade(1, time_ms=1700000000000)]
        market = _make_market()
        income = [
            _make_income(1, "REALIZED_PNL", 5.0, time_ms=1700000000000),
            _make_income(2, "COMMISSION", -0.5, time_ms=1700000001000),
            _make_income(3, "FUNDING_FEE", -0.2, time_ms=1700000002000),
        ]
        result = FetchResult(
            symbol="BTCUSDC", trades=trades, income_records=[],
            market=market, position=None, account=None,
        )
        m = compute_metrics(result, income_records=income)
        self.assertAlmostEqual(m.realized_pnl, 5.0, places=4)
        self.assertAlmostEqual(m.commission_total, 0.5, places=4)
        self.assertAlmostEqual(m.funding_cost, 0.2, places=4)
        # net = 5.0 - 0.5 - 0.2 = 4.3
        self.assertAlmostEqual(m.net_pnl, 4.3, places=4)

    def test_empty_trades_no_crash(self):
        """Should not crash with empty trades."""
        market = _make_market()
        result = FetchResult(
            symbol="BTCUSDC", trades=[], income_records=[],
            market=market, position=None, account=None,
        )
        m = compute_metrics(result)
        self.assertEqual(m.total_trades, 0)
        self.assertEqual(m.net_pnl, 0.0)

    def test_maker_taker_ratio(self):
        """Maker/taker breakdown should be accurate."""
        trades = [
            _make_trade(1, is_maker=True),
            _make_trade(2, is_maker=True),
            _make_trade(3, is_maker=False),
        ]
        market = _make_market()
        result = FetchResult(
            symbol="BTCUSDC", trades=trades, income_records=[],
            market=market, position=None, account=None,
        )
        m = compute_metrics(result)
        self.assertEqual(m.maker_trades, 2)
        self.assertEqual(m.taker_trades, 1)
        self.assertAlmostEqual(m.maker_ratio, 2 / 3, places=4)

    def test_position_direction_detection(self):
        """Long/Short direction should be detected from position."""
        market = _make_market()
        long_pos = _make_position(amt=0.001)
        result = FetchResult(
            symbol="BTCUSDC", trades=[], income_records=[],
            market=market, position=long_pos, account=None,
        )
        m = compute_metrics(result)
        self.assertEqual(m.position_direction, "LONG")

        short_pos = _make_position(amt=-0.001)
        result2 = FetchResult(
            symbol="BTCUSDC", trades=[], income_records=[],
            market=market, position=short_pos, account=None,
        )
        m2 = compute_metrics(result2)
        self.assertEqual(m2.position_direction, "SHORT")

    def test_session_profit_and_apr(self):
        """Session invested amount should produce correct APR after sufficient time."""
        trades = [_make_trade(1, time_ms=1700000000000)]
        market = _make_market()
        income = [
            _make_income(1, "REALIZED_PNL", 10.0, time_ms=1700000000000),
            _make_income(2, "COMMISSION", -1.0, time_ms=1700000000000),
        ]
        result = FetchResult(
            symbol="BTCUSDC", trades=trades, income_records=[],
            market=market, position=None, account=None,
        )
        # Session started 48 hours ago (enough for APR calculation)
        now_ms = 1700000000000
        session_start = now_ms - (48 * 3600 * 1000)

        m = compute_metrics(
            result,
            income_records=income,
            session_invested=100.0,
            session_start_ms=session_start,
        )
        self.assertAlmostEqual(m.investment_amount, 100.0, places=2)
        self.assertIsNotNone(m.apr_estimate)  # Should compute APR with >24h

    def test_grid_trades_override_excludes_manual(self):
        """When grid_trades is provided, result.trades should be ignored.

        This is the core fix for issue #1: trade-based statistics
        (total_trades, maker/taker, fill_rate, grid_range) must only
        count grid trades, not manual futures trades.
        """
        # result.trades has 3 trades (including manual)
        all_trades = [
            _make_trade(1, side="BUY", price=80000, time_ms=1700000000000),
            _make_trade(2, side="SELL", price=80100, time_ms=1700000060000),
            _make_trade(3, side="BUY", price=79500, time_ms=1700000120000),  # manual
        ]
        # grid_trades only has the 2 grid trades
        grid_only = [
            _make_trade(1, side="BUY", price=80000, time_ms=1700000000000),
            _make_trade(2, side="SELL", price=80100, time_ms=1700000060000),
        ]
        market = _make_market()
        result = FetchResult(
            symbol="BTCUSDC", trades=all_trades, income_records=[],
            market=market, position=None, account=None,
        )

        # Without grid_trades: uses all 3 trades
        m_all = compute_metrics(result)
        self.assertEqual(m_all.total_trades, 3)

        # With grid_trades: uses only 2 grid trades
        m_grid = compute_metrics(result, grid_trades=grid_only)
        self.assertEqual(m_grid.total_trades, 2)
        self.assertEqual(m_grid.buy_trades, 1)
        self.assertEqual(m_grid.sell_trades, 1)

    def test_empty_grid_trades_does_not_fallback_to_manual(self):
        """Empty grid_trades=[] must NOT fallback to result.trades.

        Regression test for PR #2 review: if DB returns zero grid trades
        but the account has manual trades, metrics must report 0 trades,
        not silently include the manual ones.
        """
        manual_trades = [
            _make_trade(1, side="BUY", price=80000, time_ms=1700000000000),
            _make_trade(2, side="SELL", price=80100, time_ms=1700000060000),
        ]
        market = _make_market()
        result = FetchResult(
            symbol="BTCUSDC", trades=manual_trades, income_records=[],
            market=market, position=None, account=None,
        )

        # grid_trades=[] means "DB confirmed zero grid trades"
        m = compute_metrics(result, grid_trades=[])
        self.assertEqual(m.total_trades, 0)
        self.assertEqual(m.buy_trades, 0)
        self.assertEqual(m.sell_trades, 0)
        self.assertEqual(m.maker_trades, 0)
        self.assertAlmostEqual(m.maker_ratio, 0.0)


class TestIncomeGridFiltering(unittest.TestCase):
    """Test that income tagging logic works correctly.

    These tests verify the classifier in fetcher.py.
    """

    def test_realized_pnl_tagged_by_trade_id(self):
        """REALIZED_PNL income with known grid tradeId should be tagged as grid."""
        from src.gridbot.binance.fetcher import BinanceFetcher

        income = _make_income(1, "REALIZED_PNL", 5.0, trade_id="12345")
        grid_trade_ids = {"12345"}

        # Direct test of the classifier
        fetcher = BinanceFetcher.__new__(BinanceFetcher)
        result = fetcher._classify_income_grid(income, grid_trade_ids)
        self.assertEqual(result, 1)  # grid

    def test_manual_trade_income_tagged_zero(self):
        """REALIZED_PNL income with non-grid tradeId should be tagged as manual."""
        from src.gridbot.binance.fetcher import BinanceFetcher

        income = _make_income(1, "REALIZED_PNL", 5.0, trade_id="99999")
        grid_trade_ids = {"12345"}

        fetcher = BinanceFetcher.__new__(BinanceFetcher)
        result = fetcher._classify_income_grid(income, grid_trade_ids)
        self.assertEqual(result, 0)  # manual

    def test_funding_fee_always_grid(self):
        """FUNDING_FEE is position-level and should always be tagged as grid."""
        from src.gridbot.binance.fetcher import BinanceFetcher

        income = _make_income(1, "FUNDING_FEE", -0.01)
        fetcher = BinanceFetcher.__new__(BinanceFetcher)
        result = fetcher._classify_income_grid(income, set())
        self.assertEqual(result, 1)  # grid (position-level)

    def test_unknown_income_type(self):
        """Unknown income types should be -1."""
        from src.gridbot.binance.fetcher import BinanceFetcher

        income = _make_income(1, "STRATEGY_UMFUTURES_TRANSFER", 100.0)
        fetcher = BinanceFetcher.__new__(BinanceFetcher)
        result = fetcher._classify_income_grid(income, set())
        self.assertEqual(result, -1)  # unknown


if __name__ == "__main__":
    unittest.main()
