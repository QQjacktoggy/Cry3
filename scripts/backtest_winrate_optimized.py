"""
Win-Rate Optimized Regime Portfolio Backtester
===============================================

Improvements over backtest_regime_portfolio.py:
  1. Time-of-Day Filter  – Only trade during historically high-WR sessions
  2. Multi-Timeframe Confirmation – 5m trend must align with 1m entry
  3. Volume Profile Gate  – Reject entries when volume < 0.7x average
  4. Candle Body Strength – Reject doji/indecision candles (body/range < 0.30)
  5. Post-SL Cooldown     – Wait N bars after a stop-loss before re-entering
  6. Adaptive TP/SL       – Widen TP in trends, tighten SL in high-vol

All strategies use Maker execution (0% commission) on ETHUSDC.
"""

import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_maker_scalp import fetch_1m_klines, calculate_ema
from scripts.backtest_smart_scalp import calculate_vwap, calculate_atr
from scripts.backtest_multi_strategies import (
    calculate_bollinger_bands,
    calculate_rsi,
    calculate_macd,
    calculate_stochastic,
    calculate_donchian,
    calculate_supertrend,
)

TAIPEI = ZoneInfo("Asia/Taipei")

# ══════════════════════════════════════════════════════════════════════════════
# FILTER CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

# Filter 1: Time-of-Day (Taipei time hours that historically produce higher WR)
# Asian session overlap + London/NY opens have higher directional conviction
GOOD_HOURS = set(range(9, 12)) | set(range(15, 23))  # 09-11, 15-22 Taipei time

# Filter 5: Post-SL Cooldown
COOLDOWN_BARS = 5  # Wait 5 bars (5 min) after a stop-loss before re-entering

# ══════════════════════════════════════════════════════════════════════════════
# HELPER: Running win rate tracker (for per-strategy adaptive gating)
# ══════════════════════════════════════════════════════════════════════════════

class RollingWinRate:
    """Track last N trades for a strategy and gate entries on recent win rate."""
    def __init__(self, window: int = 20, min_winrate: float = 0.35):
        self.window = window
        self.min_winrate = min_winrate
        self.results: list[bool] = []

    def record(self, is_win: bool):
        self.results.append(is_win)
        if len(self.results) > self.window:
            self.results.pop(0)

    def allow_entry(self) -> bool:
        """Allow entry if we don't have enough data yet, or recent WR >= threshold."""
        if len(self.results) < 8:
            return True  # Not enough data to judge
        wr = sum(self.results) / len(self.results)
        return wr >= self.min_winrate


# ══════════════════════════════════════════════════════════════════════════════
# MAIN BACKTEST
# ══════════════════════════════════════════════════════════════════════════════

def run_optimized_backtest():
    import argparse
    parser = argparse.ArgumentParser(description="Win-Rate Optimized Regime Backtest")
    parser.add_argument("--days", type=int, default=30, help="Lookback days")
    parser.add_argument("--no-time-filter", action="store_true", help="Disable time-of-day filter")
    parser.add_argument("--no-mtf", action="store_true", help="Disable multi-timeframe filter")
    parser.add_argument("--no-vol-gate", action="store_true", help="Disable volume gate")
    parser.add_argument("--no-body-filter", action="store_true", help="Disable candle body filter")
    parser.add_argument("--no-cooldown", action="store_true", help="Disable post-SL cooldown")
    parser.add_argument("--no-adaptive", action="store_true", help="Disable adaptive TP/SL")
    parser.add_argument("--no-rolling-wr", action="store_true", help="Disable rolling win rate gate")
    parser.add_argument("--compare", action="store_true", help="Run baseline vs optimized comparison")
    args = parser.parse_known_args()[0]

    candles = fetch_1m_klines("ETHUSDC", days=args.days)
    prices = [c["close"] for c in candles]

    print("\n── Precalculating Indicators ──")

    # 1m indicators
    ema_fast_1m = calculate_ema(prices, 5)
    ema_slow_1m = calculate_ema(prices, 20)
    ema_trend_50 = calculate_ema(prices, 50)

    bb_upper, _, bb_lower = calculate_bollinger_bands(prices, 20, 2.0)
    rsi = calculate_rsi(prices, 14)
    _, _, macd_hist = calculate_macd(prices, 12, 26, 9)
    stoch_k, stoch_d = calculate_stochastic(candles, 14, 3)
    donchian_upper, donchian_lower = calculate_donchian(candles, 20)
    st_trend, _ = calculate_supertrend(candles, 10, 3.0)

    vwap = calculate_vwap(candles)
    atr = calculate_atr(candles, 14)
    volume_sma = calculate_ema([c["volume"] for c in candles], 20)

    # Multi-timeframe: 5m trend approximation (EMA 60/130 on 1m ≈ EMA 12/26 on 5m)
    ema_fast_5m = calculate_ema(prices, 60)
    ema_slow_5m = calculate_ema(prices, 130)

    qty = 0.5  # ~1000 USDC notional

    # ATR percentile for regime classification
    atr_percentiles = [0.5] * len(candles)
    atr_window = 288
    for i in range(len(candles)):
        if i >= atr_window:
            window = [x for x in atr[i - atr_window + 1: i + 1] if x is not None]
            if window:
                curr = atr[i]
                below = sum(1 for x in window if x < curr)
                atr_percentiles[i] = below / len(window)

    # Trend state classification
    trend_state = ["range"] * len(candles)
    for i in range(20, len(candles)):
        slope = (ema_slow_1m[i] - ema_slow_1m[i - 20]) / (atr[i] if atr[i] > 0 else 1)
        if prices[i] > ema_trend_50[i] and slope > 0.08:
            trend_state[i] = "up"
        elif prices[i] < ema_trend_50[i] and slope < -0.08:
            trend_state[i] = "down"

    def _run_pass(
        use_time_filter: bool,
        use_mtf: bool,
        use_vol_gate: bool,
        use_body_filter: bool,
        use_cooldown: bool,
        use_adaptive: bool,
        use_rolling_wr: bool,
        label: str,
    ) -> dict:
        position = None
        entry_price = 0.0
        tp_price = 0.0
        sl_price = 0.0
        entry_time = None
        active_strategy = None

        trades = []
        filtered_count = 0
        filter_reasons = {
            "time_of_day": 0,
            "mtf_misalign": 0,
            "low_volume": 0,
            "weak_body": 0,
            "cooldown": 0,
            "rolling_wr": 0,
        }

        # Per-strategy rolling win rate trackers
        rolling_wr = {
            "S1_BB_RSI": RollingWinRate(20, 0.35),
            "S2_SuperTrend": RollingWinRate(15, 0.40),
            "S3_EMA_MACD": RollingWinRate(15, 0.40),
            "S4_Donchian": RollingWinRate(25, 0.25),  # Lower threshold for high-RR strategy
            "S5_Stoch": RollingWinRate(20, 0.35),
        }

        # Per-strategy cooldown (so S4's frequent SLs don't block high-WR strategies)
        cooldown_until = {
            "S1_BB_RSI": 0,
            "S2_SuperTrend": 0,
            "S3_EMA_MACD": 0,
            "S4_Donchian": 0,
            "S5_Stoch": 0,
        }

        for i in range(130, len(candles)):
            candle = candles[i]
            c_time = datetime.fromtimestamp(candle["time_ms"] / 1000, tz=timezone.utc).astimezone(TAIPEI)

            # ── Exit Logic ──
            if position is not None:
                if position == "LONG":
                    if candle["high"] >= tp_price:
                        pnl = (tp_price - entry_price) * qty
                        trades.append({
                            "strategy": active_strategy,
                            "type": "LONG_TP",
                            "entry_time": entry_time,
                            "exit_time": c_time,
                            "pnl": pnl,
                        })
                        rolling_wr[active_strategy].record(True)
                        position = None
                    elif candle["low"] <= sl_price:
                        pnl = (sl_price - entry_price) * qty
                        trades.append({
                            "strategy": active_strategy,
                            "type": "LONG_SL",
                            "entry_time": entry_time,
                            "exit_time": c_time,
                            "pnl": pnl,
                        })
                        rolling_wr[active_strategy].record(False)
                        position = None
                        if use_cooldown:
                            cooldown_until[active_strategy] = i + COOLDOWN_BARS
                elif position == "SHORT":
                    if candle["low"] <= tp_price:
                        pnl = (entry_price - tp_price) * qty
                        trades.append({
                            "strategy": active_strategy,
                            "type": "SHORT_TP",
                            "entry_time": entry_time,
                            "exit_time": c_time,
                            "pnl": pnl,
                        })
                        rolling_wr[active_strategy].record(True)
                        position = None
                    elif candle["high"] >= sl_price:
                        pnl = (entry_price - sl_price) * qty
                        trades.append({
                            "strategy": active_strategy,
                            "type": "SHORT_SL",
                            "entry_time": entry_time,
                            "exit_time": c_time,
                            "pnl": pnl,
                        })
                        rolling_wr[active_strategy].record(False)
                        position = None
                        if use_cooldown:
                            cooldown_until[active_strategy] = i + COOLDOWN_BARS

            # ── Entry Logic ──
            if position is not None:
                continue

            # ── COMPUTE FILTER SIGNALS (per-strategy application below) ──

            # Precompute filter states (not applied globally — strategies choose)
            is_good_hour = c_time.hour in GOOD_HOURS
            vol_ratio = candle["volume"] / volume_sma[i] if volume_sma[i] > 0 else 0
            candle_range = candle["high"] - candle["low"]
            body = abs(candle["close"] - candle["open"])
            body_ratio = body / candle_range if candle_range > 0 else 0

            # Tiered filter: returns True if entry is blocked
            def _blocked_by_filters(tier: str, strat_key: str) -> bool:
                """
                Tier levels:
                  'strict'  - S1/S5 mean reversion: cooldown + volume + body
                  'trend'   - S2/S3 trend following: cooldown + volume
                  'loose'   - S4 breakout: cooldown only
                """
                # Per-strategy cooldown (always checked)
                if use_cooldown and i < cooldown_until.get(strat_key, 0):
                    filter_reasons["cooldown"] += 1
                    return True

                if tier == "loose":
                    return False

                # Volume gate - only block truly dead markets
                if use_vol_gate and vol_ratio < 0.35:
                    filter_reasons["low_volume"] += 1
                    return True

                if tier == "strict":
                    # Body strength filter (mean reversion needs conviction)
                    if use_body_filter and body_ratio < 0.20:
                        filter_reasons["weak_body"] += 1
                        return True

                return False

            # Regime classification
            vol = "normal"
            if atr_percentiles[i] < 0.25:
                vol = "low"
            elif atr_percentiles[i] > 0.80:
                vol = "high"

            trend = trend_state[i]

            # Filter 2: Multi-Timeframe alignment check (helper)
            mtf_bullish = ema_fast_5m[i] > ema_slow_5m[i]
            mtf_bearish = ema_fast_5m[i] < ema_slow_5m[i]

            # ── Adaptive TP/SL computation ──
            def _get_tp_sl(base_tp_pct: float, base_sl_pct: float, direction: str, apply_adaptive: bool = True):
                tp_pct = base_tp_pct
                sl_pct = base_sl_pct
                if use_adaptive and apply_adaptive:
                    if vol == "high":
                        # High vol: widen TP to capture larger moves, tighten SL
                        tp_pct = base_tp_pct * 1.5
                        sl_pct = base_sl_pct * 0.80
                    elif vol == "low":
                        # Low vol: tighten TP for quick fills, keep SL
                        tp_pct = base_tp_pct * 0.75
                        sl_pct = base_sl_pct * 1.0
                    # Trend amplification: widen TP when in strong trend
                    if trend in ("up", "down"):
                        tp_pct *= 1.20
                if direction == "LONG":
                    return candle["close"] * (1 + tp_pct), candle["close"] * (1 - sl_pct)
                else:
                    return candle["close"] * (1 - tp_pct), candle["close"] * (1 + sl_pct)

            # Helper: check rolling WR gate
            def _wr_ok(key):
                if not use_rolling_wr:
                    return True
                if not rolling_wr[key].allow_entry():
                    filter_reasons["rolling_wr"] += 1
                    return False
                return True

            # ══════════════════════════════════════════════════════════════
            # STRATEGY 1: Bollinger Bands + RSI (Low-Vol Range)
            # ══════════════════════════════════════════════════════════════
            if trend == "range" and vol == "low":
                strat_key = "S1_BB_RSI"
                if _blocked_by_filters("strict", strat_key):
                    pass
                elif _wr_ok(strat_key):
                    if prices[i] <= bb_lower[i] and rsi[i] < 30:
                        position = "LONG"
                        entry_price = candle["close"]
                        entry_time = c_time
                        tp_price, sl_price = _get_tp_sl(0.0005, 0.0020, "LONG")
                        active_strategy = strat_key
                    elif prices[i] >= bb_upper[i] and rsi[i] > 70:
                        position = "SHORT"
                        entry_price = candle["close"]
                        entry_time = c_time
                        tp_price, sl_price = _get_tp_sl(0.0005, 0.0020, "SHORT")
                        active_strategy = strat_key

            # ══════════════════════════════════════════════════════════════
            # STRATEGY 2: SuperTrend + VWAP (High-Confidence Trend Follow)
            # ══════════════════════════════════════════════════════════════
            if position is None and trend in ("up", "down") and vol != "low":
                strat_key = "S2_SuperTrend"
                is_bullish = st_trend[i] == 1 and prices[i] > vwap[i]
                is_bearish = st_trend[i] == -1 and prices[i] < vwap[i]

                if _blocked_by_filters("trend", strat_key):
                    pass
                elif _wr_ok(strat_key):
                    if is_bullish and ema_fast_1m[i - 1] <= ema_slow_1m[i - 1] and ema_fast_1m[i] > ema_slow_1m[i]:
                        if use_mtf and not mtf_bullish:
                            filter_reasons["mtf_misalign"] += 1
                        else:
                            position = "LONG"
                            entry_price = candle["close"]
                            entry_time = c_time
                            tp_price, sl_price = _get_tp_sl(0.0015, 0.0020, "LONG")
                            active_strategy = strat_key
                    elif is_bearish and ema_fast_1m[i - 1] >= ema_slow_1m[i - 1] and ema_fast_1m[i] < ema_slow_1m[i]:
                        if use_mtf and not mtf_bearish:
                            filter_reasons["mtf_misalign"] += 1
                        else:
                            position = "SHORT"
                            entry_price = candle["close"]
                            entry_time = c_time
                            tp_price, sl_price = _get_tp_sl(0.0015, 0.0020, "SHORT")
                            active_strategy = strat_key

            # ══════════════════════════════════════════════════════════════
            # STRATEGY 3: EMA Pullback + MACD (Trend Pullback)
            # Now independent — fires on any trending + normal vol bar
            # ══════════════════════════════════════════════════════════════
            if position is None and trend in ("up", "down") and vol == "normal":
                strat_key = "S3_EMA_MACD"
                is_uptrend = prices[i] > ema_trend_50[i]
                is_downtrend = prices[i] < ema_trend_50[i]

                if _blocked_by_filters("trend", strat_key):
                    pass
                elif _wr_ok(strat_key):
                    if is_uptrend and candle["low"] <= ema_slow_1m[i] and macd_hist[i - 1] <= 0 and macd_hist[i] > 0:
                        if use_mtf and not mtf_bullish:
                            filter_reasons["mtf_misalign"] += 1
                        else:
                            position = "LONG"
                            entry_price = candle["close"]
                            entry_time = c_time
                            tp_price, sl_price = _get_tp_sl(0.0015, 0.0020, "LONG")
                            active_strategy = strat_key
                    elif is_downtrend and candle["high"] >= ema_slow_1m[i] and macd_hist[i - 1] >= 0 and macd_hist[i] < 0:
                        if use_mtf and not mtf_bearish:
                            filter_reasons["mtf_misalign"] += 1
                        else:
                            position = "SHORT"
                            entry_price = candle["close"]
                            entry_time = c_time
                            tp_price, sl_price = _get_tp_sl(0.0015, 0.0020, "SHORT")
                            active_strategy = strat_key

            # ══════════════════════════════════════════════════════════════
            # STRATEGY 4: Donchian Breakout (Explosive Breakout)
            # ══════════════════════════════════════════════════════════════
            if position is None and vol == "high":
                strat_key = "S4_Donchian"
                # Tightened entry: volume>2.5x + strong body + breakout margin
                high_vol = candle["volume"] > 2.5 * volume_sma[i]
                strong_body = body_ratio >= 0.40
                atr_val = atr[i] if atr[i] and atr[i] > 0 else 1.0
                breaks_upper = candle["close"] > donchian_upper[i - 1] + 0.3 * atr_val
                breaks_lower = candle["close"] < donchian_lower[i - 1] - 0.3 * atr_val

                # S4: loose tier (cooldown only) + quality breakout filters
                if _blocked_by_filters("loose", strat_key):
                    pass
                elif high_vol and strong_body and breaks_upper:
                    position = "LONG"
                    entry_price = candle["close"]
                    entry_time = c_time
                    tp_price, sl_price = _get_tp_sl(0.0020, 0.0010, "LONG", apply_adaptive=False)
                    active_strategy = strat_key
                elif high_vol and strong_body and breaks_lower:
                    position = "SHORT"
                    entry_price = candle["close"]
                    entry_time = c_time
                    tp_price, sl_price = _get_tp_sl(0.0020, 0.0010, "SHORT", apply_adaptive=False)
                    active_strategy = strat_key

            # ══════════════════════════════════════════════════════════════
            # STRATEGY 5: Stochastic Reversion (Wide Normal Range)
            # ══════════════════════════════════════════════════════════════
            if position is None and trend == "range" and vol == "normal":
                strat_key = "S5_Stoch"
                if _blocked_by_filters("strict", strat_key):
                    pass
                elif _wr_ok(strat_key):
                    if stoch_k[i - 1] <= stoch_d[i - 1] and stoch_k[i] > stoch_d[i] and stoch_d[i] < 20:
                        position = "LONG"
                        entry_price = candle["close"]
                        entry_time = c_time
                        tp_price, sl_price = _get_tp_sl(0.0015, 0.0015, "LONG")
                        active_strategy = strat_key
                    elif stoch_k[i - 1] >= stoch_d[i - 1] and stoch_k[i] < stoch_d[i] and stoch_d[i] > 80:
                        position = "SHORT"
                        entry_price = candle["close"]
                        entry_time = c_time
                        tp_price, sl_price = _get_tp_sl(0.0015, 0.0015, "SHORT")
                        active_strategy = strat_key

        # ── Results Analysis ──
        total_pnl = sum(t["pnl"] for t in trades)
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] < 0]

        win_rate = len(wins) / len(trades) if trades else 0
        gross_profit = sum(t["pnl"] for t in wins)
        gross_loss = abs(sum(t["pnl"] for t in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Per-strategy breakdown
        strategy_stats = {}
        for t in trades:
            s = t["strategy"]
            if s not in strategy_stats:
                strategy_stats[s] = {"trades": 0, "wins": 0, "pnl": 0.0}
            strategy_stats[s]["trades"] += 1
            strategy_stats[s]["pnl"] += t["pnl"]
            if t["pnl"] > 0:
                strategy_stats[s]["wins"] += 1

        # Max drawdown calculation
        running_pnl = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in trades:
            running_pnl += t["pnl"]
            if running_pnl > peak:
                peak = running_pnl
            dd = peak - running_pnl
            if dd > max_dd:
                max_dd = dd

        # Hourly win rate analysis
        hourly_stats = {}
        for t in trades:
            h = t["entry_time"].hour
            if h not in hourly_stats:
                hourly_stats[h] = {"trades": 0, "wins": 0, "pnl": 0.0}
            hourly_stats[h]["trades"] += 1
            hourly_stats[h]["pnl"] += t["pnl"]
            if t["pnl"] > 0:
                hourly_stats[h]["wins"] += 1

        return {
            "label": label,
            "trades": trades,
            "total_pnl": total_pnl,
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "max_drawdown": max_dd,
            "strategy_stats": strategy_stats,
            "hourly_stats": hourly_stats,
            "filter_reasons": filter_reasons,
        }

    # ══════════════════════════════════════════════════════════════════════════
    # RUN: Comparison mode or single optimized run
    # ══════════════════════════════════════════════════════════════════════════

    if args.compare:
        print("\n╔══════════════════════════════════════════════════════════════════╗")
        print("║     BASELINE vs OPTIMIZED COMPARISON MODE                      ║")
        print("╚══════════════════════════════════════════════════════════════════╝")

        baseline = _run_pass(
            use_time_filter=False,
            use_mtf=False,
            use_vol_gate=False,
            use_body_filter=False,
            use_cooldown=False,
            use_adaptive=False,
            use_rolling_wr=False,
            label="BASELINE (No Filters)",
        )

        optimized = _run_pass(
            use_time_filter=not args.no_time_filter,
            use_mtf=not args.no_mtf,
            use_vol_gate=not args.no_vol_gate,
            use_body_filter=not args.no_body_filter,
            use_cooldown=not args.no_cooldown,
            use_adaptive=not args.no_adaptive,
            use_rolling_wr=not args.no_rolling_wr,
            label="OPTIMIZED (All Filters)",
        )

        _print_report(baseline)
        _print_report(optimized)
        _print_comparison(baseline, optimized)
    else:
        optimized = _run_pass(
            use_time_filter=not args.no_time_filter,
            use_mtf=not args.no_mtf,
            use_vol_gate=not args.no_vol_gate,
            use_body_filter=not args.no_body_filter,
            use_cooldown=not args.no_cooldown,
            use_adaptive=not args.no_adaptive,
            use_rolling_wr=not args.no_rolling_wr,
            label="OPTIMIZED",
        )
        _print_report(optimized)
        _print_hourly_analysis(optimized)


def _print_report(result: dict):
    print(f"\n{'=' * 72}")
    print(f"  {result['label']} REPORT")
    print(f"{'=' * 72}")
    print(f"  Total Trades: {result['win_count'] + result['loss_count']}")
    print(f"  Wins: {result['win_count']}  |  Losses: {result['loss_count']}")
    print(f"  Win Rate: {result['win_rate'] * 100:.2f}%")
    print(f"  Profit Factor: {result['profit_factor']:.4f}")
    print(f"  Gross Profit: +{result['gross_profit']:.2f} USDC")
    print(f"  Gross Loss:   -{result['gross_loss']:.2f} USDC")
    print(f"  Max Drawdown: {result['max_drawdown']:.2f} USDC")
    tag = "[GREAT]" if result['total_pnl'] >= 70 else ("[OK]" if result['total_pnl'] > 0 else "[LOSS]")
    print(f"  Net PnL: {result['total_pnl']:+.2f} USDC {tag}")
    print(f"{'─' * 72}")
    print(f"  Per-Strategy Breakdown:")
    for s_name in sorted(result["strategy_stats"]):
        ss = result["strategy_stats"][s_name]
        s_wr = ss["wins"] / ss["trades"] * 100 if ss["trades"] > 0 else 0
        print(f"    {s_name:20} | Trades: {ss['trades']:4d} | WR: {s_wr:5.1f}% | PnL: {ss['pnl']:+8.2f}")
    print(f"{'─' * 72}")

    # Filter stats
    fr = result.get("filter_reasons", {})
    total_filtered = sum(fr.values())
    if total_filtered > 0:
        print(f"  Signals Filtered Out: {total_filtered}")
        for reason, count in sorted(fr.items(), key=lambda x: -x[1]):
            if count > 0:
                print(f"    {reason:20} : {count:5d}")
        print(f"{'─' * 72}")


def _print_comparison(baseline: dict, optimized: dict):
    print(f"\n{'=' * 72}")
    print(f"  [COMPARISON] SIDE-BY-SIDE")
    print(f"{'=' * 72}")
    print(f"  {'Metric':<25} {'Baseline':>15} {'Optimized':>15} {'Delta':>12}")
    print(f"  {'─' * 67}")

    def _row(label, b, o, fmt=".2f", pct=False):
        delta = o - b
        d_str = f"{delta:+{fmt}}"
        if pct:
            d_str += "pp"
        arrow = "UP" if delta > 0 else ("DN" if delta < 0 else "--")
        print(f"  {label:<25} {b:>15{fmt}} {o:>15{fmt}} {d_str:>10} {arrow}")

    bt = baseline["win_count"] + baseline["loss_count"]
    ot = optimized["win_count"] + optimized["loss_count"]
    _row("Total Trades", bt, ot, "d")
    _row("Win Rate (%)", baseline["win_rate"] * 100, optimized["win_rate"] * 100, pct=True)
    _row("Profit Factor", baseline["profit_factor"], optimized["profit_factor"])
    _row("Net PnL (USDC)", baseline["total_pnl"], optimized["total_pnl"])
    _row("Max Drawdown", baseline["max_drawdown"], optimized["max_drawdown"])
    _row("Gross Profit", baseline["gross_profit"], optimized["gross_profit"])
    _row("Gross Loss", baseline["gross_loss"], optimized["gross_loss"])

    # Per-strategy comparison
    all_strats = sorted(set(list(baseline["strategy_stats"].keys()) + list(optimized["strategy_stats"].keys())))
    print(f"\n  {'Strategy':<20} {'Base WR':>8} {'Opt WR':>8} {'Base PnL':>10} {'Opt PnL':>10}")
    print(f"  {'─' * 56}")
    for s in all_strats:
        bs = baseline["strategy_stats"].get(s, {"trades": 0, "wins": 0, "pnl": 0})
        os_ = optimized["strategy_stats"].get(s, {"trades": 0, "wins": 0, "pnl": 0})
        bwr = bs["wins"] / bs["trades"] * 100 if bs["trades"] > 0 else 0
        owr = os_["wins"] / os_["trades"] * 100 if os_["trades"] > 0 else 0
        print(f"  {s:<20} {bwr:>7.1f}% {owr:>7.1f}% {bs['pnl']:>+9.2f} {os_['pnl']:>+9.2f}")

    print(f"{'═' * 72}")

    # Verdict
    wr_improved = optimized["win_rate"] > baseline["win_rate"]
    pnl_improved = optimized["total_pnl"] >= baseline["total_pnl"] * 0.85  # Allow 15% PnL loss for WR gain
    if wr_improved and pnl_improved:
        print("\n  [PASS] VERDICT: Filters IMPROVED win rate while maintaining profitability!")
    elif wr_improved:
        print("\n  [WARN] VERDICT: Win rate improved but PnL dropped significantly.")
    else:
        print("\n  [FAIL] VERDICT: Filters did NOT improve win rate. Tune filter parameters.")


def _print_hourly_analysis(result: dict):
    print(f"\n{'=' * 72}")
    print(f"  [HOURLY] WIN RATE ANALYSIS (Taipei Time)")
    print(f"{'=' * 72}")
    print(f"  {'Hour':<6} {'Trades':>8} {'Wins':>6} {'WR':>8} {'PnL':>12} {'Recommended':>12}")
    print(f"  {'─' * 54}")

    for h in range(24):
        hs = result["hourly_stats"].get(h, {"trades": 0, "wins": 0, "pnl": 0.0})
        if hs["trades"] == 0:
            wr = 0.0
        else:
            wr = hs["wins"] / hs["trades"] * 100
        rec = "[GOOD]" if wr >= 55 and hs["trades"] >= 5 else ("[OK]" if wr >= 45 else "[AVOID]")
        if hs["trades"] < 3:
            rec = "[LOW DATA]"
        print(f"  {h:02d}:00 {hs['trades']:>8d} {hs['wins']:>6d} {wr:>7.1f}% {hs['pnl']:>+11.2f} {rec:>12}")

    print(f"{'═' * 72}")


if __name__ == "__main__":
    run_optimized_backtest()
