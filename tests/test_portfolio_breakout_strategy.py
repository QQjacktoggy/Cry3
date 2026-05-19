import unittest

from src.gridbot.strategy.long_pullback import Candle, StrategyConfig
from src.gridbot.strategy.portfolio_breakout import (
    PortfolioBreakoutConfig,
    run_portfolio_breakout_backtest,
    sweep_portfolio_breakout_configs,
)


def make_symbol_candles(count: int, start: float, breakout_bias: float) -> list[Candle]:
    candles: list[Candle] = []
    price = start
    for index in range(count):
        if index < 90:
            price += breakout_bias * 0.35
            volume = 100
        elif index < 130:
            price += breakout_bias * 0.7
            volume = 130
        else:
            price += breakout_bias * 1.15
            volume = 180
        candles.append(Candle(
            open_time_ms=1_700_000_000_000 + index * 300_000,
            open=price - 0.3,
            high=price + 0.9,
            low=price - 0.9,
            close=price,
            volume=volume,
            quote_volume=volume * price,
        ))
    return candles


class TestPortfolioBreakoutStrategy(unittest.TestCase):
    def test_backtest_runs(self):
        candles_by_symbol = {
            "ETHUSDC": make_symbol_candles(220, 2300.0, 1.0),
            "BTCUSDC": make_symbol_candles(220, 64000.0, 0.8),
            "SOLUSDC": make_symbol_candles(220, 140.0, 1.3),
        }
        summary = run_portfolio_breakout_backtest(
            candles_by_symbol,
            PortfolioBreakoutConfig(base=StrategyConfig()),
        )
        self.assertGreaterEqual(summary.total_trades, 0)
        self.assertIsInstance(summary.net_pnl_usdc, float)

    def test_sweep_runs(self):
        candles_by_symbol = {
            "ETHUSDC": make_symbol_candles(220, 2300.0, 1.0),
            "BTCUSDC": make_symbol_candles(220, 64000.0, 0.8),
            "SOLUSDC": make_symbol_candles(220, 140.0, 1.3),
        }
        results = sweep_portfolio_breakout_configs(candles_by_symbol, StrategyConfig())
        self.assertGreater(len(results), 0)
        self.assertTrue(any(result.config.risk_per_trade_pct >= 3.2 for result in results))

    def test_compounding_can_scale_with_wins(self):
        candles_by_symbol = {
            "ETHUSDC": make_symbol_candles(220, 2300.0, 1.0),
            "BTCUSDC": make_symbol_candles(220, 64000.0, 0.8),
            "SOLUSDC": make_symbol_candles(220, 140.0, 1.3),
        }
        static_summary = run_portfolio_breakout_backtest(
            candles_by_symbol,
            PortfolioBreakoutConfig(
                base=StrategyConfig(risk_per_trade_pct=3.2),
                require_benchmark_trend=False,
                portfolio_margin_cap_pct=90.0,
            ),
        )
        compounded_summary = run_portfolio_breakout_backtest(
            candles_by_symbol,
            PortfolioBreakoutConfig(
                base=StrategyConfig(risk_per_trade_pct=3.2, compounding_enabled=True),
                require_benchmark_trend=False,
                portfolio_margin_cap_pct=90.0,
            ),
        )
        self.assertTrue(compounded_summary.config.compounding_enabled)
        self.assertGreaterEqual(compounded_summary.net_pnl_usdc, static_summary.net_pnl_usdc)


if __name__ == "__main__":
    unittest.main()
