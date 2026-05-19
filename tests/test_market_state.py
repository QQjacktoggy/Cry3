import unittest
from dataclasses import replace

from src.gridbot.strategy.market_state import build_market_state_context, classify_market_state
from tests.test_long_orb_strategy import make_orb_candles, make_short_orb_candles


class TestMarketState(unittest.TestCase):
    def test_market_state_uses_bounded_schema(self):
        candles = make_orb_candles(days=5)
        context = build_market_state_context(candles)

        decision = classify_market_state(candles, 360, context)

        self.assertIsNotNone(decision)
        assert decision is not None
        self.assertIn(decision.trend, {"up", "down", "range"})
        self.assertIn(decision.ma20_structure, {"above_rising", "below_falling", "flat_crossing"})
        self.assertIn(decision.n_pattern, {"bullish", "bearish", "none"})
        self.assertIn(decision.breakout_quality, {"strong", "weak", "fake_risk"})
        self.assertIn(decision.pullback_quality, {"healthy", "deep", "broken", "none"})
        self.assertIn(decision.volatility, {"low", "normal", "high"})
        self.assertIn(
            decision.playbook,
            {"long_breakout", "long_pullback", "short_breakdown", "vwap_reversion", "no_trade"},
        )
        self.assertIn(decision.risk_mode, {"off", "small", "normal", "aggressive"})
        self.assertGreaterEqual(decision.confidence, 0.0)
        self.assertLessEqual(decision.confidence, 1.0)

    def test_market_state_does_not_look_ahead(self):
        candles = make_orb_candles(days=5)
        index = 360
        context = build_market_state_context(candles)
        decision = classify_market_state(candles, index, context)

        mutated = list(candles)
        for future_index in range(index + 1, len(mutated)):
            candle = mutated[future_index]
            mutated[future_index] = replace(
                candle,
                open=candle.open * 3,
                high=candle.high * 3,
                low=candle.low * 3,
                close=candle.close * 3,
                volume=candle.volume * 20,
            )
        mutated_context = build_market_state_context(mutated)
        mutated_decision = classify_market_state(mutated, index, mutated_context)

        self.assertEqual(decision, mutated_decision)

    def test_market_state_can_detect_short_playbook(self):
        candles = make_short_orb_candles(days=5)
        context = build_market_state_context(candles)

        decisions = [
            classify_market_state(candles, index, context)
            for index in range(300, len(candles) - 1)
        ]
        playbooks = {decision.playbook for decision in decisions if decision is not None}

        self.assertTrue(playbooks & {"short_breakdown", "no_trade"})


if __name__ == "__main__":
    unittest.main()
