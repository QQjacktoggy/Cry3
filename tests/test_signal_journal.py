import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from src.gridbot.strategy.long_orb import OrbConfig
from src.gridbot.strategy.long_pullback import SignalPlan, StrategyConfig
from src.gridbot.strategy.signal_journal import (
    LocalNimReview,
    _allocator_daily_state,
    _allocator_profile,
    _ai_risk_judge_ai_query_needed,
    _bounded_external_ai_risk_review,
    _local_ai_risk_review,
    _regime_allocator_adjusted_base,
    _journal_throttle_blocks,
    _journal_throttle_key,
    _journal_throttle_strategy_in_scope,
    _journal_throttle_update,
    _nim_review_rejected_by_market_state,
    _regime_router_adjusted_base,
    _rolling_loss_guard,
    _short_exhaustion_confirmed,
    _short_quality_filter_blocks,
    _strategy_holding_config,
    _strategy_trade_config,
    _uses_defensive_exit_profile,
    explain_router_allocator_v13_trend350_live_block,
    generate_router_allocator_high_return_live_decision,
    generate_router_allocator_v13_trend350_live_decision,
    run_orb_signal_journal,
    summarize_allocator_journal,
    summarize_signal_journal,
)
from tests.test_long_orb_strategy import make_orb_candles, make_short_orb_candles


class _FakeNimReviewer:
    def __init__(self):
        self.calls = 0

    def review(self, decision, cache_key, candidate=None):
        self.calls += 1

        class Review:
            playbook = "long_pullback"
            risk_mode = "normal"
            confidence = 0.7

        return Review()


def _fillable_orb_candles():
    return [replace(candle, low=min(candle.low, candle.close - 3.0)) for candle in make_orb_candles(days=8)]


def _fillable_short_orb_candles():
    return [replace(candle, high=max(candle.high, candle.close + 3.0)) for candle in make_short_orb_candles(days=8)]


class TestSignalJournal(unittest.TestCase):
    def test_high_return_live_bypasses_nim_hard_block_but_legacy_trend350_does_not(self):
        candles = _fillable_orb_candles()[-20:]
        base = StrategyConfig(symbol="ETHUSDC", min_score=44, risk_per_trade_pct=2.0, max_position_margin_pct=60.0)
        signal = SignalPlan(
            action="PLAN_LONG",
            confidence=68,
            score=68,
            symbol="ETHUSDC",
            price=2106.78,
            rsi=78,
            atr=2.03,
            support=2096.74,
            vwap=2094.92,
            entries=[2104.6732],
            stop_loss=2096.7431,
            take_profits=[2109.034755, 2113.39631, 2122.11942],
            planned_notional_usdc=300.0,
            leverage_cap=10.0,
            reasons=["close broke session OR high by 0.57 ATR"],
        )
        regime_decision = SimpleNamespace(
            regime="low_liquidity",
            risk_mode="off",
            confidence=0.78,
            features=SimpleNamespace(close_position_lookback=0.94, trend_slope_atr=2.33),
        )
        market_decision = SimpleNamespace(
            playbook="no_trade",
            risk_mode="off",
            trend="up",
            ma20_structure="above_rising",
            n_pattern="none",
            breakout_quality="weak",
            pullback_quality="deep",
            confidence=0.78,
            features=SimpleNamespace(volume_ratio=0.0312, atr_percentile=0.4877),
        )
        allocation = {"state": "normal", "profile": "exploratory_long", "scale": 0.35}
        trade_config = SimpleNamespace(base=SimpleNamespace(max_holding_bars=24))

        with (
            patch("src.gridbot.strategy.signal_journal.build_orb_context", return_value=object()),
            patch("src.gridbot.strategy.signal_journal.build_regime_context", return_value=object()),
            patch("src.gridbot.strategy.signal_journal.build_market_state_context", return_value=object()),
            patch("src.gridbot.strategy.signal_journal.classify_regime", return_value=regime_decision),
            patch("src.gridbot.strategy.signal_journal.classify_market_state", return_value=market_decision),
            patch("src.gridbot.strategy.signal_journal._daily_guard_reason", return_value=None),
            patch("src.gridbot.strategy.signal_journal._risk_adjusted_config", side_effect=lambda equity, day_pnl: equity),
            patch("src.gridbot.strategy.signal_journal._select_journal_signal", return_value=(signal, "orb_long")),
            patch("src.gridbot.strategy.signal_journal._regime_router_adjusted_base", side_effect=lambda *args, **kwargs: args[0]),
            patch(
                "src.gridbot.strategy.signal_journal._local_nim_policy_review",
                return_value=LocalNimReview("no_trade", "off", 0.84, ("local_volume_too_thin",)),
            ),
            patch("src.gridbot.strategy.signal_journal._nim_review_rejected_by_market_state", return_value=False),
            patch(
                "src.gridbot.strategy.signal_journal._regime_allocator_adjusted_base",
                return_value=(base, allocation),
            ),
            patch("src.gridbot.strategy.signal_journal._local_ai_risk_review", return_value=None),
            patch("src.gridbot.strategy.signal_journal._strategy_trade_config", return_value=trade_config),
        ):
            high_return = generate_router_allocator_high_return_live_decision(candles, base)
            legacy = generate_router_allocator_v13_trend350_live_decision(candles, base)
            legacy_block = explain_router_allocator_v13_trend350_live_block(candles, base)

        self.assertIsNotNone(high_return)
        self.assertEqual(high_return.strategy, "orb_long")
        self.assertEqual(high_return.allocator_profile, "exploratory_long")
        self.assertIsNone(legacy)
        self.assertIn("nim_scaled_to_zero", legacy_block)

    def test_orb_signal_journal_records_signal_before_entry(self):
        candles = _fillable_orb_candles()
        config = OrbConfig(
            base=StrategyConfig(
                symbol="ETHUSDC",
                compounding_enabled=True,
                min_score=44,
                risk_per_trade_pct=2.0,
                max_position_margin_pct=60.0,
                max_holding_bars=48,
            ),
            opening_range_bars=9,
            min_volume_ratio=0.8,
            stop_atr=0.6,
        )

        summary, rows = run_orb_signal_journal(candles, config)

        self.assertGreaterEqual(summary.total_trades, len(rows))
        self.assertGreater(len(rows), 0)
        for row in rows:
            self.assertEqual(row.symbol, "ETHUSDC")
            self.assertEqual(row.strategy, "orb_long")
            self.assertLess(row.signal_time_ms, row.entry_time_ms)
            self.assertIn(row.regime, {"trend_up", "trend_down", "range", "high_volatility", "low_liquidity", "chop"})
            self.assertIn(row.risk_mode, {"off", "small", "normal", "aggressive"})

    def test_orb_short_signal_journal_records_signal_before_entry(self):
        candles = _fillable_short_orb_candles()
        config = OrbConfig(
            base=StrategyConfig(
                symbol="ETHUSDC",
                compounding_enabled=True,
                min_score=44,
                risk_per_trade_pct=2.0,
                max_position_margin_pct=60.0,
                max_holding_bars=48,
            ),
            opening_range_bars=9,
            min_volume_ratio=0.8,
            stop_atr=0.6,
        )

        summary, rows = run_orb_signal_journal(candles, config, side="short")

        self.assertGreaterEqual(summary.total_trades, len(rows))
        self.assertGreater(len(rows), 0)
        for row in rows:
            self.assertEqual(row.symbol, "ETHUSDC")
            self.assertEqual(row.strategy, "orb_short")
            self.assertLess(row.signal_time_ms, row.entry_time_ms)

    def test_orb_signal_journal_can_select_both_sides(self):
        candles = _fillable_orb_candles() + _fillable_short_orb_candles()
        config = OrbConfig(
            base=StrategyConfig(symbol="ETHUSDC", min_score=44, risk_per_trade_pct=2.0),
            opening_range_bars=9,
            min_volume_ratio=0.8,
            stop_atr=0.6,
        )

        _, rows = run_orb_signal_journal(candles, config, side="both")

        self.assertTrue(all(row.strategy in {"orb_long", "orb_short"} for row in rows))

    def test_signal_journal_router_can_select_multiple_strategy_types(self):
        candles = _fillable_orb_candles() + _fillable_short_orb_candles()
        config = OrbConfig(
            base=StrategyConfig(symbol="ETHUSDC", min_score=44, risk_per_trade_pct=2.0),
            opening_range_bars=9,
            min_volume_ratio=0.8,
            stop_atr=0.6,
        )

        _, rows = run_orb_signal_journal(candles, config, side="router")

        self.assertTrue(all(row.strategy in {"orb_long", "orb_short", "vwap_long", "vwap_short"} for row in rows))

    def test_signal_journal_throttle_runs_with_compounding_equity(self):
        candles = _fillable_orb_candles()
        config = OrbConfig(
            base=StrategyConfig(symbol="ETHUSDC", min_score=44, risk_per_trade_pct=2.0, compounding_enabled=True),
            opening_range_bars=9,
            min_volume_ratio=0.8,
            stop_atr=0.6,
        )

        summary, rows = run_orb_signal_journal(
            candles,
            config,
            journal_throttle_enabled=True,
            journal_throttle_max_losses=1,
            journal_throttle_loss_pct=6.0,
        )

        self.assertGreaterEqual(summary.total_trades, 0)
        self.assertIsInstance(rows, list)

    def test_signal_journal_summary_counts_all_rows(self):
        candles = _fillable_orb_candles()
        _, rows = run_orb_signal_journal(candles, OrbConfig(base=StrategyConfig(symbol="ETHUSDC", min_score=44)))

        summary = summarize_signal_journal(rows)

        self.assertEqual(sum(bucket["trades"] for bucket in summary), len(rows))

    def test_signal_journal_can_block_selected_regimes(self):
        candles = _fillable_orb_candles()
        config = OrbConfig(base=StrategyConfig(symbol="ETHUSDC", min_score=44))

        _, rows = run_orb_signal_journal(candles, config, block_regimes=("chop", "range"))

        self.assertTrue(all(row.regime not in {"chop", "range"} for row in rows))

    def test_signal_journal_can_apply_market_state_reviewer(self):
        candles = _fillable_orb_candles()
        config = OrbConfig(base=StrategyConfig(symbol="ETHUSDC", min_score=44))

        _, rows = run_orb_signal_journal(candles, config, market_state_reviewer_enabled=True)

        self.assertTrue(all(row.market_playbook != "not_used" for row in rows))

    def test_signal_journal_can_apply_market_state_scale_mode(self):
        candles = _fillable_orb_candles()
        config = OrbConfig(base=StrategyConfig(symbol="ETHUSDC", min_score=44))

        _, rows = run_orb_signal_journal(
            candles,
            config,
            market_state_reviewer_enabled=True,
            market_state_reviewer_mode="scale",
        )

        self.assertTrue(all(row.market_playbook != "not_used" for row in rows))

    def test_signal_journal_can_apply_nim_candidate_reviewer(self):
        candles = _fillable_orb_candles()
        config = OrbConfig(base=StrategyConfig(symbol="ETHUSDC", min_score=44))

        _, rows = run_orb_signal_journal(candles, config, nim_reviewer=_FakeNimReviewer())

        self.assertTrue(all(row.market_playbook != "not_used" for row in rows))

    def test_signal_journal_auto_policy_reduces_nim_calls(self):
        candles = _fillable_orb_candles()
        config = OrbConfig(base=StrategyConfig(symbol="ETHUSDC", min_score=44))
        all_reviewer = _FakeNimReviewer()
        auto_reviewer = _FakeNimReviewer()

        run_orb_signal_journal(candles, config, nim_reviewer=all_reviewer, nim_query_policy="all")
        run_orb_signal_journal(candles, config, nim_reviewer=auto_reviewer, nim_query_policy="auto")

        self.assertLessEqual(auto_reviewer.calls, all_reviewer.calls)

    def test_rolling_loss_guard_pauses_after_recent_losses(self):
        base = StrategyConfig(symbol="ETHUSDC", equity_usdc=200)
        daily = {
            "2026-05-08": -3.0,
            "2026-05-09": -5.0,
            "2026-05-10": 0.0,
        }

        self.assertTrue(_rolling_loss_guard(base, "2026-05-10", daily, 2, 3.0))
        self.assertFalse(_rolling_loss_guard(base, "2026-05-10", daily, 2, 5.0))

    def test_nim_pullback_review_is_blocked_in_weak_no_trade_state(self):
        decision = SimpleNamespace(
            regime="trend_up",
            risk_mode="aggressive",
            features=SimpleNamespace(close_position_lookback=0.82),
        )
        market_decision = SimpleNamespace(
            playbook="no_trade",
            risk_mode="off",
            n_pattern="none",
            breakout_quality="weak",
            pullback_quality="deep",
            features=SimpleNamespace(atr_percentile=0.45),
        )
        review = SimpleNamespace(playbook="long_pullback", risk_mode="normal")

        self.assertTrue(_nim_review_rejected_by_market_state("orb_long", decision, market_decision, review))

    def test_nim_pullback_review_allows_low_atr_pullbacks(self):
        decision = SimpleNamespace(
            regime="trend_up",
            risk_mode="aggressive",
            features=SimpleNamespace(close_position_lookback=0.61),
        )
        market_decision = SimpleNamespace(
            playbook="no_trade",
            risk_mode="off",
            n_pattern="none",
            breakout_quality="weak",
            pullback_quality="deep",
            features=SimpleNamespace(atr_percentile=0.07),
        )
        review = SimpleNamespace(playbook="long_pullback", risk_mode="normal")

        self.assertFalse(_nim_review_rejected_by_market_state("orb_long", decision, market_decision, review))

    def test_nim_pullback_review_blocks_late_weak_pullbacks(self):
        decision = SimpleNamespace(
            regime="trend_up",
            risk_mode="aggressive",
            features=SimpleNamespace(close_position_lookback=0.75),
        )
        market_decision = SimpleNamespace(
            playbook="long_pullback",
            risk_mode="normal",
            n_pattern="none",
            breakout_quality="weak",
            pullback_quality="healthy",
            features=SimpleNamespace(atr_percentile=0.43),
        )
        review = SimpleNamespace(playbook="long_pullback", risk_mode="normal")

        self.assertTrue(_nim_review_rejected_by_market_state("orb_long", decision, market_decision, review))

    def test_nim_pullback_review_blocks_strong_deep_no_trade_pullbacks(self):
        decision = SimpleNamespace(
            regime="trend_up",
            risk_mode="aggressive",
            features=SimpleNamespace(close_position_lookback=0.69),
        )
        market_decision = SimpleNamespace(
            playbook="no_trade",
            risk_mode="off",
            n_pattern="none",
            breakout_quality="strong",
            pullback_quality="deep",
            features=SimpleNamespace(atr_percentile=0.55),
        )
        review = SimpleNamespace(playbook="long_pullback", risk_mode="aggressive")

        self.assertTrue(_nim_review_rejected_by_market_state("orb_long", decision, market_decision, review))

    def test_nim_review_blocks_orb_long_in_vwap_reversion_state(self):
        decision = SimpleNamespace(
            regime="range",
            risk_mode="small",
            features=SimpleNamespace(close_position_lookback=0.45),
        )
        market_decision = SimpleNamespace(
            playbook="vwap_reversion",
            risk_mode="small",
            n_pattern="none",
            breakout_quality="weak",
            pullback_quality="healthy",
            features=SimpleNamespace(atr_percentile=0.35),
        )
        review = SimpleNamespace(playbook="vwap_reversion", risk_mode="small")

        self.assertTrue(_nim_review_rejected_by_market_state("orb_long", decision, market_decision, review))

    def test_nim_review_blocks_chop_no_trade_state(self):
        decision = SimpleNamespace(
            regime="chop",
            risk_mode="off",
            features=SimpleNamespace(close_position_lookback=0.52),
        )
        market_decision = SimpleNamespace(
            playbook="no_trade",
            risk_mode="off",
            n_pattern="none",
            breakout_quality="weak",
            pullback_quality="none",
            features=SimpleNamespace(atr_percentile=0.45),
        )
        review = SimpleNamespace(playbook="no_trade", risk_mode="small")

        self.assertTrue(_nim_review_rejected_by_market_state("orb_long", decision, market_decision, review))

    def test_nim_review_blocks_normal_trend_no_trade_pullback_override(self):
        decision = SimpleNamespace(
            regime="trend_up",
            risk_mode="normal",
            features=SimpleNamespace(close_position_lookback=0.55),
        )
        market_decision = SimpleNamespace(
            playbook="no_trade",
            risk_mode="off",
            n_pattern="none",
            breakout_quality="weak",
            pullback_quality="none",
            features=SimpleNamespace(atr_percentile=0.20),
        )
        review = SimpleNamespace(playbook="long_pullback", risk_mode="normal")

        self.assertTrue(_nim_review_rejected_by_market_state("orb_long", decision, market_decision, review))

    def test_nim_review_blocks_high_volatility_no_trade_pullback_override(self):
        decision = SimpleNamespace(
            regime="high_volatility",
            risk_mode="normal",
            features=SimpleNamespace(close_position_lookback=0.70),
        )
        market_decision = SimpleNamespace(
            playbook="no_trade",
            risk_mode="off",
            n_pattern="none",
            breakout_quality="weak",
            pullback_quality="none",
            features=SimpleNamespace(atr_percentile=0.90),
        )
        review = SimpleNamespace(playbook="long_pullback", risk_mode="normal")

        self.assertTrue(_nim_review_rejected_by_market_state("orb_long", decision, market_decision, review))

    def test_nim_review_blocks_no_structure_aggressive_pullback_upgrade(self):
        decision = SimpleNamespace(
            regime="trend_up",
            risk_mode="aggressive",
            features=SimpleNamespace(close_position_lookback=0.89),
        )
        market_decision = SimpleNamespace(
            playbook="no_trade",
            risk_mode="off",
            n_pattern="none",
            breakout_quality="weak",
            pullback_quality="none",
            features=SimpleNamespace(atr_percentile=0.42, volume_ratio=4.3),
        )
        review = SimpleNamespace(playbook="long_pullback", risk_mode="aggressive", confidence=0.8)

        self.assertTrue(_nim_review_rejected_by_market_state("orb_long", decision, market_decision, review))

    def test_nim_review_blocks_weak_small_pullback_without_volume(self):
        decision = SimpleNamespace(
            regime="trend_up",
            risk_mode="normal",
            features=SimpleNamespace(close_position_lookback=0.65),
        )
        market_decision = SimpleNamespace(
            playbook="long_pullback",
            risk_mode="normal",
            n_pattern="none",
            breakout_quality="weak",
            pullback_quality="healthy",
            features=SimpleNamespace(atr_percentile=0.14, volume_ratio=0.90),
        )
        review = SimpleNamespace(playbook="long_pullback", risk_mode="small", confidence=0.5)

        self.assertTrue(_nim_review_rejected_by_market_state("orb_long", decision, market_decision, review))

    def test_regime_router_blocks_chop_long_entries(self):
        base = StrategyConfig(symbol="ETHUSDC", risk_per_trade_pct=10.0)
        decision = SimpleNamespace(regime="chop", risk_mode="off")
        market_decision = SimpleNamespace(
            playbook="no_trade",
            risk_mode="off",
            n_pattern="none",
            pullback_quality="none",
            breakout_quality="weak",
            features=SimpleNamespace(volume_ratio=1.0),
        )

        self.assertIsNone(_regime_router_adjusted_base(base, "orb_long", decision, market_decision, 0.35, 0.18))

    def test_regime_router_scales_normal_no_trade_entries(self):
        base = StrategyConfig(symbol="ETHUSDC", risk_per_trade_pct=10.0, max_position_margin_pct=50.0)
        decision = SimpleNamespace(regime="trend_up", risk_mode="normal")
        market_decision = SimpleNamespace(
            playbook="no_trade",
            risk_mode="off",
            n_pattern="none",
            pullback_quality="healthy",
            breakout_quality="weak",
            features=SimpleNamespace(volume_ratio=1.0),
        )

        routed = _regime_router_adjusted_base(base, "orb_long", decision, market_decision, 0.35, 0.18)

        self.assertAlmostEqual(routed.risk_per_trade_pct, 1.8)
        self.assertAlmostEqual(routed.max_position_margin_pct, 9.0)

    def test_regime_router_keeps_bullish_aggressive_entries_full_size(self):
        base = StrategyConfig(symbol="ETHUSDC", risk_per_trade_pct=10.0)
        decision = SimpleNamespace(regime="trend_up", risk_mode="aggressive")
        market_decision = SimpleNamespace(
            playbook="no_trade",
            risk_mode="off",
            n_pattern="bullish",
            pullback_quality="none",
            breakout_quality="weak",
            features=SimpleNamespace(volume_ratio=4.0),
        )

        self.assertIs(_regime_router_adjusted_base(base, "orb_long", decision, market_decision, 0.35, 0.18), base)

    def test_regime_router_blocks_short_breakdowns_in_vwap_reversion_state(self):
        base = StrategyConfig(symbol="ETHUSDC", risk_per_trade_pct=10.0)
        decision = SimpleNamespace(regime="trend_down", risk_mode="off")
        market_decision = SimpleNamespace(
            playbook="vwap_reversion",
            risk_mode="small",
            n_pattern="none",
            pullback_quality="healthy",
            breakout_quality="weak",
            features=SimpleNamespace(volume_ratio=1.0),
        )

        self.assertIsNone(_regime_router_adjusted_base(base, "orb_short", decision, market_decision, 0.35, 0.18))

    def test_regime_router_scales_no_trade_short_breakdowns(self):
        base = StrategyConfig(symbol="ETHUSDC", risk_per_trade_pct=10.0, max_position_margin_pct=50.0)
        decision = SimpleNamespace(regime="trend_down", risk_mode="off")
        market_decision = SimpleNamespace(
            playbook="no_trade",
            risk_mode="off",
            n_pattern="none",
            pullback_quality="none",
            breakout_quality="weak",
            features=SimpleNamespace(volume_ratio=1.0),
        )

        routed = _regime_router_adjusted_base(base, "orb_short", decision, market_decision, 0.35, 0.18)

        self.assertAlmostEqual(routed.risk_per_trade_pct, 3.5)
        self.assertAlmostEqual(routed.max_position_margin_pct, 17.5)

    def test_regime_router_keeps_high_volatility_shorts_exploratory(self):
        base = StrategyConfig(symbol="ETHUSDC", risk_per_trade_pct=10.0, max_position_margin_pct=50.0)
        decision = SimpleNamespace(regime="high_volatility", risk_mode="small")
        market_decision = SimpleNamespace(
            playbook="short_breakdown",
            risk_mode="normal",
            n_pattern="bearish",
            pullback_quality="none",
            breakout_quality="strong",
            features=SimpleNamespace(volume_ratio=3.0),
        )

        routed = _regime_router_adjusted_base(base, "orb_short", decision, market_decision, 0.70, 0.35)

        self.assertAlmostEqual(routed.risk_per_trade_pct, 3.5)
        self.assertAlmostEqual(routed.max_position_margin_pct, 17.5)

    def test_short_quality_filter_blocks_fake_risk_short(self):
        decision = SimpleNamespace(regime="trend_down")
        market_decision = SimpleNamespace(
            playbook="no_trade",
            breakout_quality="fake_risk",
        )

        self.assertTrue(_short_quality_filter_blocks("orb_short", decision, market_decision))

    def test_short_quality_filter_allows_high_volatility_non_fake_short(self):
        decision = SimpleNamespace(regime="high_volatility")
        market_decision = SimpleNamespace(
            playbook="no_trade",
            breakout_quality="strong",
        )

        self.assertFalse(_short_quality_filter_blocks("orb_short", decision, market_decision))

    def test_short_quality_filter_allows_clean_short_breakdown(self):
        decision = SimpleNamespace(regime="high_volatility")
        market_decision = SimpleNamespace(
            playbook="short_breakdown",
            breakout_quality="strong",
        )

        self.assertFalse(_short_quality_filter_blocks("orb_short", decision, market_decision))

    def test_regime_router_scales_vwap_reversion_sleeve(self):
        base = StrategyConfig(symbol="ETHUSDC", risk_per_trade_pct=10.0, max_position_margin_pct=50.0)
        decision = SimpleNamespace(regime="chop", risk_mode="off")
        market_decision = SimpleNamespace(
            trend="range",
            playbook="vwap_reversion",
            risk_mode="small",
            n_pattern="none",
            pullback_quality="healthy",
            breakout_quality="weak",
            features=SimpleNamespace(volume_ratio=1.0),
        )

        routed = _regime_router_adjusted_base(base, "vwap_long", decision, market_decision, 0.35, 0.18)

        self.assertAlmostEqual(routed.risk_per_trade_pct, 1.8)
        self.assertAlmostEqual(routed.max_position_margin_pct, 9.0)

    def test_journal_throttle_blocks_same_day_bucket_after_loss(self):
        state = {}
        key = ("orb_short", "trend_down", "off", "no_trade", "off")

        _journal_throttle_update("2026-05-10", key, state, -3.0)

        self.assertTrue(_journal_throttle_blocks(200.0, "2026-05-10", key, state, 1, 6.0))
        self.assertFalse(_journal_throttle_blocks(200.0, "2026-05-11", key, state, 1, 6.0))

    def test_journal_throttle_blocks_after_bucket_loss_pct(self):
        state = {}
        key = ("orb_long", "trend_up", "normal", "no_trade", "off")

        _journal_throttle_update("2026-05-10", key, state, -13.0)

        self.assertTrue(_journal_throttle_blocks(200.0, "2026-05-10", key, state, 0, 6.0))

    def test_journal_throttle_key_uses_strategy_and_market_bucket(self):
        decision = SimpleNamespace(regime="trend_down", risk_mode="off")
        market_decision = SimpleNamespace(playbook="no_trade", risk_mode="off")

        self.assertEqual(
            _journal_throttle_key("orb_short", decision, market_decision),
            ("orb_short", "trend_down", "off", "no_trade", "off"),
        )

    def test_journal_throttle_strategy_scope_can_target_shorts_only(self):
        self.assertTrue(_journal_throttle_strategy_in_scope("orb_short", "short"))
        self.assertTrue(_journal_throttle_strategy_in_scope("vwap_short", "short"))
        self.assertFalse(_journal_throttle_strategy_in_scope("orb_long", "short"))
        self.assertTrue(_journal_throttle_strategy_in_scope("orb_long", "all"))

    def test_allocator_daily_state_tracks_protect_and_lock(self):
        base = StrategyConfig(symbol="ETHUSDC", equity_usdc=200.0)

        self.assertEqual(_allocator_daily_state(base, -4.1, 2.0, 1.5), "protect")
        self.assertEqual(_allocator_daily_state(base, 3.1, 2.0, 1.5), "lock_profit")
        self.assertEqual(_allocator_daily_state(base, 1.0, 2.0, 1.5), "normal")

    def test_allocator_profile_classifies_core_sleeves(self):
        trend = SimpleNamespace(regime="trend_up", risk_mode="aggressive")
        normal = SimpleNamespace(regime="trend_up", risk_mode="normal")
        down = SimpleNamespace(regime="trend_down", risk_mode="off")

        self.assertEqual(_allocator_profile("orb_long", trend, None), "trend_up_aggressive")
        self.assertEqual(_allocator_profile("orb_long", normal, None), "trend_up_normal")
        self.assertEqual(_allocator_profile("orb_short", down, None), "short")
        self.assertEqual(
            _allocator_profile("orb_short", down, SimpleNamespace(playbook="no_trade", breakout_quality="fake_risk")),
            "short_fake_risk",
        )
        exhausted = SimpleNamespace(
            regime="trend_down",
            risk_mode="off",
            features=SimpleNamespace(close_position_lookback=0.2, trend_slope_atr=-1.4),
        )
        self.assertEqual(
            _allocator_profile(
                "orb_short",
                exhausted,
                SimpleNamespace(playbook="no_trade", breakout_quality="weak", features=SimpleNamespace(volume_ratio=1.2)),
            ),
            "short_exhaustion",
        )
        self.assertEqual(
            _allocator_profile(
                "orb_short",
                exhausted,
                SimpleNamespace(playbook="no_trade", breakout_quality="strong", features=SimpleNamespace(volume_ratio=1.2)),
            ),
            "short_exhaustion_strong",
        )
        weak_low_atr_short = SimpleNamespace(
            regime="trend_down",
            risk_mode="off",
            features=SimpleNamespace(close_position_lookback=0.45, trend_slope_atr=-0.8),
        )
        self.assertEqual(
            _allocator_profile(
                "orb_short",
                weak_low_atr_short,
                SimpleNamespace(
                    playbook="no_trade",
                    ma20_structure="below_falling",
                    breakout_quality="weak",
                    features=SimpleNamespace(atr_percentile=0.35, volume_ratio=1.2),
                ),
            ),
            "short_weak_low_atr",
        )
        self.assertEqual(_allocator_profile("orb_short", down, SimpleNamespace(playbook="short_breakdown")), "short_breakdown")
        volatile = SimpleNamespace(regime="high_volatility", risk_mode="small")
        self.assertEqual(
            _allocator_profile("orb_short", volatile, SimpleNamespace(playbook="short_breakdown")),
            "volatility_short_breakdown",
        )
        self.assertEqual(_allocator_profile("vwap_long", down, None), "reversion")

    def test_regime_allocator_scales_risk_and_margin(self):
        base = StrategyConfig(symbol="ETHUSDC", equity_usdc=200.0, risk_per_trade_pct=10.0, max_position_margin_pct=50.0)
        decision = SimpleNamespace(regime="trend_up", risk_mode="aggressive")

        routed, allocation = _regime_allocator_adjusted_base(
            base,
            "orb_long",
            decision,
            None,
            None,
            0.0,
            2.0,
            1.5,
            0.45,
            0.65,
            1.15,
            0.9,
            0.35,
            0.45,
            0.85,
            0.25,
            0.20,
            0.30,
            0.10,
            1.10,
            0.45,
            0.75,
            0.45,
            None,
            0.45,
            12.0,
            35.0,
        )

        self.assertEqual(allocation["profile"], "trend_up_aggressive")
        self.assertAlmostEqual(routed.risk_per_trade_pct, 11.5)
        self.assertAlmostEqual(routed.max_position_margin_pct, 35.0)

    def test_regime_allocator_can_block_short_exhaustion_strong(self):
        base = StrategyConfig(symbol="ETHUSDC", equity_usdc=200.0, risk_per_trade_pct=10.0, max_position_margin_pct=50.0)
        decision = SimpleNamespace(
            regime="trend_down",
            risk_mode="off",
            features=SimpleNamespace(close_position_lookback=0.2, trend_slope_atr=-1.4),
        )
        market_decision = SimpleNamespace(
            playbook="no_trade",
            breakout_quality="strong",
            features=SimpleNamespace(volume_ratio=1.2),
        )

        routed, allocation = _regime_allocator_adjusted_base(
            base,
            "orb_short",
            decision,
            market_decision,
            None,
            0.0,
            2.0,
            1.5,
            0.45,
            0.65,
            1.15,
            0.9,
            0.35,
            0.45,
            0.85,
            0.25,
            0.20,
            0.30,
            0.0,
            1.10,
            0.45,
            0.75,
            0.45,
            None,
            0.45,
            12.0,
            35.0,
        )

        self.assertIsNone(routed)
        self.assertEqual(allocation["profile"], "short_exhaustion_strong")
        self.assertEqual(allocation["scale"], 0.0)

    def test_regime_allocator_can_scale_normal_weak_pullback_only(self):
        base = StrategyConfig(symbol="ETHUSDC", equity_usdc=200.0, risk_per_trade_pct=10.0, max_position_margin_pct=50.0)
        decision = SimpleNamespace(regime="trend_up", risk_mode="normal")
        market_decision = SimpleNamespace(
            playbook="long_pullback",
            n_pattern="none",
            breakout_quality="weak",
            pullback_quality="healthy",
        )
        nim_review = SimpleNamespace(playbook="long_pullback", risk_mode="small")

        routed, allocation = _regime_allocator_adjusted_base(
            base,
            "orb_long",
            decision,
            market_decision,
            nim_review,
            0.0,
            2.0,
            1.5,
            0.45,
            0.65,
            1.15,
            0.9,
            0.35,
            0.45,
            0.85,
            0.25,
            0.20,
            0.30,
            0.0,
            1.10,
            0.45,
            0.75,
            0.30,
            0.12,
            0.45,
            12.0,
            35.0,
        )

        self.assertEqual(allocation["profile"], "weak_pullback_small")
        self.assertAlmostEqual(allocation["scale"], 0.12)
        self.assertAlmostEqual(routed.risk_per_trade_pct, 1.2)

    def test_local_ai_risk_rejects_low_atr_mid_structure_short_breakdown(self):
        base = StrategyConfig(symbol="ETHUSDC", risk_per_trade_pct=105.0)
        regime_decision = SimpleNamespace(
            features=SimpleNamespace(close_position_lookback=0.56),
        )
        market_decision = SimpleNamespace(
            features=SimpleNamespace(
                atr_percentile=0.19,
                close_position_20=0.55,
                volume_ratio=1.7,
            ),
        )
        signal = SimpleNamespace(score=106)

        review = _local_ai_risk_review(
            "orb_short",
            regime_decision,
            market_decision,
            signal,
            "normal",
            "short_breakdown",
            base,
        )

        self.assertEqual(review.decision, "reject")
        self.assertEqual(review.risk_level, "extreme")

    def test_local_ai_risk_reduces_protect_aggressive_position_risk(self):
        base = StrategyConfig(symbol="ETHUSDC", risk_per_trade_pct=70.0)
        regime_decision = SimpleNamespace(
            features=SimpleNamespace(close_position_lookback=0.7),
        )
        market_decision = SimpleNamespace(
            features=SimpleNamespace(
                atr_percentile=0.7,
                close_position_20=0.7,
                volume_ratio=1.8,
            ),
        )
        signal = SimpleNamespace(score=98)

        review = _local_ai_risk_review(
            "orb_long",
            regime_decision,
            market_decision,
            signal,
            "protect",
            "trend_up_aggressive",
            base,
        )

        self.assertEqual(review.decision, "reduce")
        self.assertAlmostEqual(review.risk_scale, 0.5)

    def test_local_ai_risk_rejects_high_risk_weak_volume_long_pullback(self):
        base = StrategyConfig(symbol="ETHUSDC", risk_per_trade_pct=65.0)
        regime_decision = SimpleNamespace(
            features=SimpleNamespace(close_position_lookback=0.79),
        )
        market_decision = SimpleNamespace(
            playbook="long_pullback",
            breakout_quality="weak",
            features=SimpleNamespace(
                atr_percentile=0.63,
                close_position_20=0.8,
                volume_ratio=0.93,
            ),
        )
        signal = SimpleNamespace(score=91)

        review = _local_ai_risk_review(
            "orb_long",
            regime_decision,
            market_decision,
            signal,
            "normal",
            "trend_up_normal",
            base,
        )

        self.assertEqual(review.decision, "reject")
        self.assertEqual(review.risk_level, "extreme")

    def test_ai_risk_auto_query_skips_low_risk_trend_up_normal(self):
        base = StrategyConfig(symbol="ETHUSDC", risk_per_trade_pct=8.75)
        market_decision = SimpleNamespace(
            playbook="no_trade",
            breakout_quality="strong",
            features=SimpleNamespace(volume_ratio=1.0, close_position_20=0.5),
        )

        self.assertFalse(
            _ai_risk_judge_ai_query_needed(
                "orb_long",
                market_decision,
                "normal",
                "trend_up_normal",
                base,
            )
        )

    def test_ai_risk_auto_query_keeps_high_risk_gray_long_pullback(self):
        base = StrategyConfig(symbol="ETHUSDC", risk_per_trade_pct=30.0)
        market_decision = SimpleNamespace(
            playbook="long_pullback",
            breakout_quality="weak",
            features=SimpleNamespace(volume_ratio=0.95, close_position_20=0.76),
        )

        self.assertTrue(
            _ai_risk_judge_ai_query_needed(
                "orb_long",
                market_decision,
                "normal",
                "trend_up_normal",
                base,
            )
        )

    def test_external_ai_risk_reject_is_downgraded_to_reduce(self):
        review = SimpleNamespace(
            decision="reject",
            risk_level="extreme",
            risk_scale=0.0,
            confidence=0.9,
            reason_codes=("fallback_minimax", "leverage_too_high"),
        )

        bounded = _bounded_external_ai_risk_review(review)

        self.assertEqual(bounded.decision, "reduce")
        self.assertEqual(bounded.risk_scale, 0.5)
        self.assertIn("external_ai_reject_downgraded_to_reduce", bounded.reason_codes)

    def test_external_ai_risk_reduce_has_scale_floor(self):
        review = SimpleNamespace(
            decision="reduce",
            risk_level="high",
            risk_scale=0.2,
            confidence=0.8,
            reason_codes=("fallback_minimax",),
        )

        bounded = _bounded_external_ai_risk_review(review)

        self.assertEqual(bounded.decision, "reduce")
        self.assertAlmostEqual(bounded.risk_scale, 0.7)

    def test_short_exhaustion_detects_overextended_downtrend(self):
        decision = SimpleNamespace(
            regime="trend_down",
            features=SimpleNamespace(close_position_lookback=0.18, trend_slope_atr=-1.35),
        )
        market_decision = SimpleNamespace(playbook="no_trade", features=SimpleNamespace(volume_ratio=1.2))

        self.assertTrue(_short_exhaustion_confirmed(decision, market_decision))

    def test_short_exhaustion_ignores_mid_range_downtrend(self):
        decision = SimpleNamespace(
            regime="trend_down",
            features=SimpleNamespace(close_position_lookback=0.45, trend_slope_atr=-1.35),
        )
        market_decision = SimpleNamespace(playbook="no_trade", features=SimpleNamespace(volume_ratio=1.2))

        self.assertFalse(_short_exhaustion_confirmed(decision, market_decision))

    def test_short_exhaustion_requires_volume_expansion(self):
        decision = SimpleNamespace(
            regime="trend_down",
            features=SimpleNamespace(close_position_lookback=0.18, trend_slope_atr=-1.35),
        )
        market_decision = SimpleNamespace(playbook="no_trade", features=SimpleNamespace(volume_ratio=0.9))

        self.assertFalse(_short_exhaustion_confirmed(decision, market_decision))

    def test_allocator_profile_splits_weak_trend_up_normal_no_trade(self):
        decision = SimpleNamespace(
            regime="trend_up",
            risk_mode="normal",
            features=SimpleNamespace(close_position_lookback=0.5, trend_slope_atr=0.2),
        )
        market_decision = SimpleNamespace(
            playbook="no_trade",
            features=SimpleNamespace(volume_ratio=1.2),
        )

        self.assertEqual(
            _allocator_profile("orb_long", decision, market_decision, signal_score=91),
            "trend_up_normal",
        )
        self.assertEqual(
            _allocator_profile("orb_long", decision, market_decision, signal_score=86),
            "trend_up_normal_weak",
        )
        low_quality_market = SimpleNamespace(
            playbook="no_trade",
            breakout_quality="weak",
            features=SimpleNamespace(atr_percentile=0.35, volume_ratio=1.2),
        )
        low_quality_decision = SimpleNamespace(
            regime="trend_up",
            risk_mode="normal",
            features=SimpleNamespace(close_position_lookback=0.7, trend_slope_atr=0.2),
        )
        self.assertEqual(
            _allocator_profile("orb_long", low_quality_decision, low_quality_market, signal_score=91),
            "trend_up_normal_low_quality",
        )

    def test_allocator_profile_detects_weak_pullback_small_review(self):
        decision = SimpleNamespace(regime="trend_up", risk_mode="normal")
        market_decision = SimpleNamespace(
            playbook="long_pullback",
            n_pattern="none",
            breakout_quality="weak",
            pullback_quality="healthy",
        )
        review = SimpleNamespace(playbook="long_pullback", risk_mode="small")

        self.assertEqual(
            _allocator_profile("orb_long", decision, market_decision, review),
            "weak_pullback_small",
        )

    def test_allocator_profile_detects_aggressive_no_trade_pullback(self):
        decision = SimpleNamespace(regime="trend_up", risk_mode="aggressive")
        market_decision = SimpleNamespace(
            playbook="no_trade",
            risk_mode="off",
            n_pattern="none",
            breakout_quality="weak",
            pullback_quality="none",
        )
        review = SimpleNamespace(playbook="long_pullback", risk_mode="normal")

        self.assertEqual(
            _allocator_profile("orb_long", decision, market_decision, review),
            "aggressive_no_trade_pullback",
        )

    def test_allocator_profile_keeps_confirmed_no_trade_pullback_aggressive(self):
        decision = SimpleNamespace(
            regime="trend_up",
            risk_mode="aggressive",
            features=SimpleNamespace(close_position_lookback=0.9, trend_slope_atr=1.6),
        )
        market_decision = SimpleNamespace(
            playbook="no_trade",
            risk_mode="off",
            n_pattern="none",
            breakout_quality="weak",
            pullback_quality="none",
        )
        review = SimpleNamespace(playbook="long_pullback", risk_mode="normal")

        self.assertEqual(
            _allocator_profile("orb_long", decision, market_decision, review),
            "trend_up_aggressive",
        )

        market_decision.n_pattern = "bullish"
        decision.features.close_position_lookback = 0.2
        decision.features.trend_slope_atr = 0.2
        self.assertEqual(
            _allocator_profile("orb_long", decision, market_decision, review),
            "trend_up_aggressive",
        )

    def test_signal_journal_allocator_summary_reports_capital_use(self):
        candles = _fillable_orb_candles()
        config = OrbConfig(
            base=StrategyConfig(symbol="ETHUSDC", min_score=44, risk_per_trade_pct=2.0),
            opening_range_bars=9,
            min_volume_ratio=0.8,
            stop_atr=0.6,
        )

        _, rows = run_orb_signal_journal(candles, config, regime_allocator_enabled=True)
        summary = summarize_allocator_journal(rows)

        self.assertEqual(sum(bucket["trades"] for bucket in summary), len(rows))
        self.assertTrue(all("planned_margin_usdc" in bucket for bucket in summary))

    def test_strategy_holding_config_only_overrides_selected_sleeves(self):
        config = OrbConfig(base=StrategyConfig(symbol="ETHUSDC", max_holding_bars=48))

        short_config = _strategy_holding_config(config, "orb_short", 24, 18)
        vwap_config = _strategy_holding_config(config, "vwap_long", 24, 18)
        long_config = _strategy_holding_config(config, "orb_long", 24, 18)

        self.assertEqual(short_config.base.max_holding_bars, 24)
        self.assertEqual(vwap_config.base.max_holding_bars, 18)
        self.assertIs(long_config, config)

    def test_defensive_exit_profile_skips_aggressive_trend_up_longs(self):
        decision = SimpleNamespace(regime="trend_up", risk_mode="aggressive")

        self.assertFalse(_uses_defensive_exit_profile("orb_long", decision))
        self.assertTrue(_uses_defensive_exit_profile("orb_short", decision))
        self.assertFalse(_uses_defensive_exit_profile("orb_long", SimpleNamespace(regime="range", risk_mode="small"), "short_reversion"))

    def test_strategy_trade_config_applies_defensive_exit_profile(self):
        config = OrbConfig(
            base=StrategyConfig(
                symbol="ETHUSDC",
                max_holding_bars=48,
                exit_weights=(0.25, 0.35, 0.40),
            )
        )
        decision = SimpleNamespace(regime="trend_down", risk_mode="off")

        routed = _strategy_trade_config(
            config,
            "orb_short",
            decision,
            0,
            0,
            True,
            (0.55, 0.25, 0.20),
            1,
            0.0,
            24,
            "non_trend",
        )

        self.assertEqual(routed.base.exit_weights, (0.55, 0.25, 0.20))
        self.assertEqual(routed.base.breakeven_after_tp, 1)
        self.assertEqual(routed.base.max_holding_bars, 24)

    def test_strategy_trade_config_keeps_aggressive_trend_up_runner(self):
        config = OrbConfig(
            base=StrategyConfig(
                symbol="ETHUSDC",
                max_holding_bars=48,
                exit_weights=(0.25, 0.35, 0.40),
            )
        )
        decision = SimpleNamespace(regime="trend_up", risk_mode="aggressive")

        routed = _strategy_trade_config(
            config,
            "orb_long",
            decision,
            0,
            0,
            True,
            (0.55, 0.25, 0.20),
            1,
            0.0,
            24,
            "non_trend",
        )

        self.assertIs(routed, config)


if __name__ == "__main__":
    unittest.main()
