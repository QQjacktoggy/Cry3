import unittest

from src.gridbot.strategy.long_orb import (
    OrbConfig,
    build_orb_context_with_derivatives,
    generate_orb_signal,
    generate_orb_signal_at,
    generate_orb_short_signal_at,
    generate_vwap_reversion_long_signal_at,
    generate_vwap_reversion_short_signal_at,
    run_orb_backtest,
    simulate_orb_short,
    sweep_orb_configs,
)
from src.gridbot.strategy.long_pullback import Candle, StrategyConfig


def make_orb_candles(days: int = 3, bars_per_day: int = 96, start: float = 2300.0) -> list[Candle]:
    candles: list[Candle] = []
    timestamp = 1_700_000_000_000
    price = start
    for day in range(days):
        day_open = price + day * 8
        for bar in range(bars_per_day):
            if bar < 12:
                close = day_open + (bar % 4) * 0.15
                high = close + 0.35
                low = close - 0.35
                volume = 120
            elif bar == 12:
                close = day_open + 2.8
                high = close + 0.8
                low = close - 0.4
                volume = 240
            else:
                close = day_open + 2.8 + (bar - 12) * 0.18
                high = close + 0.45
                low = close - 0.4
                volume = 170
            candles.append(
                Candle(
                    open_time_ms=timestamp,
                    open=close - 0.2,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    quote_volume=volume * close,
                )
            )
            timestamp += 300_000
        price = candles[-1].close
    return candles


def make_short_orb_candles(days: int = 3, bars_per_day: int = 96, start: float = 2300.0) -> list[Candle]:
    candles: list[Candle] = []
    timestamp = 1_700_000_000_000
    price = start
    for day in range(days):
        day_open = price - day * 8
        for bar in range(bars_per_day):
            if bar < 12:
                close = day_open - (bar % 4) * 0.15
                high = close + 0.35
                low = close - 0.35
                volume = 120
            elif bar == 12:
                close = day_open - 2.8
                high = close + 0.4
                low = close - 0.8
                volume = 240
            else:
                close = day_open - 2.8 - (bar - 12) * 0.18
                high = close + 0.4
                low = close - 0.45
                volume = 170
            candles.append(
                Candle(
                    open_time_ms=timestamp,
                    open=close + 0.2,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    quote_volume=volume * close,
                )
            )
            timestamp += 300_000
        price = candles[-1].close
    return candles


def make_vwap_reversion_candles(side: str = "long") -> list[Candle]:
    candles: list[Candle] = []
    timestamp = 1_700_000_000_000
    for index in range(140):
        close = 100.0 + (index % 3) * 0.05
        candles.append(
            Candle(
                open_time_ms=timestamp,
                open=close - 0.05,
                high=close + 0.25,
                low=close - 0.25,
                close=close,
                volume=100,
                quote_volume=100 * close,
            )
        )
        timestamp += 300_000
    if side == "long":
        candles.append(Candle(timestamp, 96.0, 97.8, 92.0, 97.5, 260, 260 * 97.5))
    else:
        candles.append(Candle(timestamp, 104.0, 108.0, 102.2, 102.5, 260, 260 * 102.5))
    return candles


class TestLongOrbStrategy(unittest.TestCase):
    def test_signal_shape(self):
        signal = generate_orb_signal(
            make_orb_candles(),
            OrbConfig(base=StrategyConfig(min_score=40)),
        )
        self.assertIn(signal.action, {"PLAN_LONG", "WAIT"})
        self.assertGreater(signal.price, 0)
        if signal.action == "PLAN_LONG":
            self.assertEqual(len(signal.entries), 1)
            self.assertLess(signal.stop_loss, signal.entries[0])

    def test_backtest_runs(self):
        summary = run_orb_backtest(
            make_orb_candles(days=5),
            OrbConfig(base=StrategyConfig(min_score=40)),
        )
        self.assertGreaterEqual(summary.total_trades, 0)
        self.assertIsInstance(summary.net_pnl_usdc, float)

    def test_sweep_runs(self):
        results = sweep_orb_configs(
            make_orb_candles(days=5),
            StrategyConfig(),
            profile="aggressive",
        )
        self.assertGreater(len(results), 0)
        self.assertTrue(any(result.config.risk_per_trade_pct >= 1.5 for result in results))

    def test_oi_and_funding_filters_gate_signal(self):
        candles = make_orb_candles()
        config = OrbConfig(
            base=StrategyConfig(min_score=40),
            require_oi_confirmation=True,
            min_oi_delta_pct=0.5,
            reject_extreme_funding=True,
            max_funding_rate=0.0003,
        )
        weak_context = build_orb_context_with_derivatives(
            candles,
            config,
            oi_delta_pct_values=[0.1] * len(candles),
            funding_rate_values=[0.0001] * len(candles),
        )
        weak_signal = generate_orb_signal_at(candles, len(candles) - 1, config, weak_context)
        self.assertEqual(weak_signal.action, "WAIT")

        hot_context = build_orb_context_with_derivatives(
            candles,
            config,
            oi_delta_pct_values=[1.0] * len(candles),
            funding_rate_values=[0.0005] * len(candles),
        )
        hot_signal = generate_orb_signal_at(candles, len(candles) - 1, config, hot_context)
        self.assertEqual(hot_signal.action, "WAIT")

    def test_short_signal_and_simulator_run(self):
        candles = make_short_orb_candles()
        config = OrbConfig(base=StrategyConfig(min_score=40, max_holding_bars=24))
        signal = generate_orb_short_signal_at(candles, len(candles) - 20, config)
        self.assertIn(signal.action, {"PLAN_SHORT", "WAIT"})
        if signal.action == "PLAN_SHORT":
            self.assertGreater(signal.stop_loss, signal.entries[0])
            self.assertTrue(all(tp < signal.entries[0] for tp in signal.take_profits))
            trade, next_index = simulate_orb_short(candles, len(candles) - 19, signal, config)
            self.assertGreater(next_index, len(candles) - 19)
            if trade:
                self.assertIsInstance(trade.pnl_usdc, float)

    def test_vwap_reversion_signal_shape(self):
        config = OrbConfig(base=StrategyConfig(min_score=40, max_holding_bars=24))
        long_candles = make_vwap_reversion_candles("long")
        long_signal = generate_vwap_reversion_long_signal_at(long_candles, len(long_candles) - 1, config)
        self.assertIn(long_signal.action, {"PLAN_LONG", "WAIT"})
        if long_signal.action == "PLAN_LONG":
            self.assertLess(long_signal.stop_loss, long_signal.entries[0])

        short_candles = make_vwap_reversion_candles("short")
        short_signal = generate_vwap_reversion_short_signal_at(short_candles, len(short_candles) - 1, config)
        self.assertIn(short_signal.action, {"PLAN_SHORT", "WAIT"})
        if short_signal.action == "PLAN_SHORT":
            self.assertGreater(short_signal.stop_loss, short_signal.entries[0])


if __name__ == "__main__":
    unittest.main()
