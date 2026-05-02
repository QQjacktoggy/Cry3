"""Tests for AI prompt construction — verifies data flows into prompts correctly."""

import unittest

from config.strategies import get_strategy, STRATEGY_REGISTRY
from src.gridbot.ai.prompts import build_system_prompt, build_user_prompt
from src.gridbot.binance.models import MarketSnapshot, PositionInfo
from src.gridbot.grid.models import GridMetrics


class TestSystemPrompt(unittest.TestCase):
    def test_system_prompt_contains_strategy_json(self):
        """System prompt must inject the strategy bounds JSON."""
        prompt = build_system_prompt()
        # Strategy names are injected as keys in the JSON
        for name in STRATEGY_REGISTRY:
            self.assertIn(name, prompt)
        # Bounds are serialized as arrays: "leverage": [min, max]
        self.assertIn('"leverage"', prompt)
        self.assertIn('"grid_spacing_pct"', prompt)
        self.assertIn('"allowed_directions"', prompt)

    def test_system_prompt_language(self):
        """System prompt should instruct Gemini to respond in Traditional Chinese."""
        prompt = build_system_prompt()
        self.assertIn("繁體中文", prompt)


class TestUserPrompt(unittest.TestCase):
    def _make_metrics(self, symbol: str = "BTCUSDC") -> GridMetrics:
        return GridMetrics(
            symbol=symbol,
            total_trades=50,
            buy_trades=25,
            sell_trades=25,
            maker_trades=40,
            taker_trades=10,
            maker_ratio=0.8,
            realized_pnl=10.0,
            unrealized_pnl=2.0,
            commission_total=1.5,
            funding_cost=0.5,
            net_pnl=8.0,
            fill_rate=0.9,
            price_range_utilization=0.85,
            trades_per_hour=5.0,
            avg_trade_interval_minutes=12.0,
            apr_estimate=120.0,
            grid_lower_price=79000,
            grid_upper_price=81000,
        )

    def _make_market(self) -> MarketSnapshot:
        return MarketSnapshot(
            symbol="BTCUSDC",
            current_price=80000.0,
            high_24h=80500.0,
            low_24h=79500.0,
            volume_24h=100000,
            price_change_pct_24h=0.5,
            funding_rate=0.0001,
            next_funding_time_ms=1700000000000,
            mark_price=80000.0,
            klines=[],
        )

    def test_user_prompt_includes_all_symbols(self):
        """User prompt should contain data for every symbol passed."""
        metrics = {"BTCUSDC": self._make_metrics("BTCUSDC"), "ETHUSDC": self._make_metrics("ETHUSDC")}
        markets = {
            "BTCUSDC": self._make_market(),
            "ETHUSDC": MarketSnapshot(
                symbol="ETHUSDC", current_price=2300.0, high_24h=2350.0,
                low_24h=2250.0, volume_24h=50000, price_change_pct_24h=-0.3,
                funding_rate=0.00005, next_funding_time_ms=1700000000000,
                mark_price=2300.0, klines=[],
            ),
        }
        positions = {"BTCUSDC": None, "ETHUSDC": None}
        funding = {"BTCUSDC": [], "ETHUSDC": []}

        prompt = build_user_prompt(
            metrics=metrics, markets=markets, positions=positions,
            funding_rates=funding, current_strategy="moderate",
        )
        self.assertIn("BTCUSDC", prompt)
        self.assertIn("ETHUSDC", prompt)
        self.assertIn("80,000", prompt)  # BTC price (comma formatted)
        self.assertIn("2,300", prompt)  # ETH price (comma formatted)

    def test_user_prompt_includes_strategy(self):
        """Current strategy should be visible in the prompt."""
        metrics = {"BTCUSDC": self._make_metrics()}
        markets = {"BTCUSDC": self._make_market()}
        positions = {"BTCUSDC": None}
        funding = {"BTCUSDC": []}

        prompt = build_user_prompt(
            metrics=metrics, markets=markets, positions=positions,
            funding_rates=funding, current_strategy="aggressive",
        )
        self.assertIn("aggressive", prompt)


class TestStrategyRegistry(unittest.TestCase):
    def test_all_strategies_have_bounds(self):
        """Every registered strategy must have valid bounds."""
        for name, strategy in STRATEGY_REGISTRY.items():
            b = strategy.bounds
            self.assertGreater(b.leverage_max, 0, f"{name} leverage_max must be > 0")
            self.assertLessEqual(b.leverage_min, b.leverage_max, f"{name} leverage bounds inverted")
            self.assertTrue(b.allowed_directions, f"{name} must have at least one direction")

    def test_get_strategy_invalid_raises(self):
        """Unknown strategy name should raise ValueError."""
        with self.assertRaises(ValueError):
            get_strategy("does_not_exist")


if __name__ == "__main__":
    unittest.main()
