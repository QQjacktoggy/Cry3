import unittest

from src.gridbot.strategy.long_pullback import StrategyConfig
from src.gridbot.strategy.portfolio_orb import (
    PortfolioOrbConfig,
    run_portfolio_orb_backtest,
    sweep_portfolio_orb_configs,
)
from tests.test_long_orb_strategy import make_orb_candles


class TestPortfolioOrbStrategy(unittest.TestCase):
    def test_backtest_runs(self):
        candles = {
            "ETHUSDC": make_orb_candles(days=4, start=2300.0),
            "BTCUSDC": make_orb_candles(days=4, start=43000.0),
            "SOLUSDC": make_orb_candles(days=4, start=120.0),
        }
        summary = run_portfolio_orb_backtest(
            candles,
            PortfolioOrbConfig(base=StrategyConfig(compounding_enabled=True)),
        )
        self.assertGreaterEqual(summary.total_trades, 0)
        self.assertIsInstance(summary.net_pnl_usdc, float)

    def test_sweep_runs(self):
        candles = {
            "ETHUSDC": make_orb_candles(days=4, start=2300.0),
            "BTCUSDC": make_orb_candles(days=4, start=43000.0),
            "SOLUSDC": make_orb_candles(days=4, start=120.0),
        }
        results = sweep_portfolio_orb_configs(candles, StrategyConfig(compounding_enabled=True))
        self.assertGreater(len(results), 0)


if __name__ == "__main__":
    unittest.main()
