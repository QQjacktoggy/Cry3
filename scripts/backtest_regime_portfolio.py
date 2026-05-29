import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import requests

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
    calculate_supertrend
)

TAIPEI = ZoneInfo("Asia/Taipei")

def run_regime_portfolio_backtest():
    import argparse
    parser = argparse.ArgumentParser(description="Regime-Switched Backtest")
    parser.add_argument("--days", type=int, default=30, help="Lookback days")
    args = parser.parse_known_args()[0]
    
    candles = fetch_1m_klines("ETHUSDC", days=args.days)
    prices = [c["close"] for c in candles]
    
    print("\n--- Precalculating Indicators for Regime Portfolio ---")
    
    # Precalculate indicators
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
    
    qty = 0.5  # ~1000 USDC notional
    
    # Define Regimes over 1m data (simulating real-time classify_market_state)
    # Volatility: low (ATR percentile < 25%), normal, high (> 80%)
    # Let's compute running ATR percentiles over 288 bars (approx 5 hours on 1m)
    atr_percentiles = [0.5] * len(candles)
    atr_window = 288
    for i in range(len(candles)):
        if i >= atr_window:
            window = [x for x in atr[i - atr_window + 1 : i + 1] if x is not None]
            if window:
                curr = atr[i]
                below = sum(1 for x in window if x < curr)
                atr_percentiles[i] = below / len(window)
                
    # Classify Regimes
    # trend: "up" (prices > ema_trend_50 and slope rising), "down", "range"
    trend_state = ["range"] * len(candles)
    for i in range(20, len(candles)):
        slope = (ema_slow_1m[i] - ema_slow_1m[i-20]) / (atr[i] if atr[i] > 0 else 1)
        if prices[i] > ema_trend_50[i] and slope > 0.08:
            trend_state[i] = "up"
        elif prices[i] < ema_trend_50[i] and slope < -0.08:
            trend_state[i] = "down"
            
    position = None
    entry_price = 0.0
    tp_price = 0.0
    sl_price = 0.0
    entry_time = None
    active_strategy = None
    
    trades = []
    
    for i in range(130, len(candles)):
        candle = candles[i]
        c_time = datetime.fromtimestamp(candle["time_ms"]/1000, tz=timezone.utc).astimezone(TAIPEI)
        
        # Check active position exits
        if position is not None:
            if position == "LONG":
                if candle["high"] >= tp_price:
                    trades.append({
                        "strategy": active_strategy,
                        "type": "LONG_TP",
                        "entry_time": entry_time,
                        "exit_time": c_time,
                        "pnl": (tp_price - entry_price) * qty
                    })
                    position = None
                elif candle["low"] <= sl_price:
                    trades.append({
                        "strategy": active_strategy,
                        "type": "LONG_SL",
                        "entry_time": entry_time,
                        "exit_time": c_time,
                        "pnl": (sl_price - entry_price) * qty
                    })
                    position = None
            elif position == "SHORT":
                if candle["low"] <= tp_price:
                    trades.append({
                        "strategy": active_strategy,
                        "type": "SHORT_TP",
                        "entry_time": entry_time,
                        "exit_time": c_time,
                        "pnl": (entry_price - tp_price) * qty
                    })
                    position = None
                elif candle["high"] >= sl_price:
                    trades.append({
                        "strategy": active_strategy,
                        "type": "SHORT_SL",
                        "entry_time": entry_time,
                        "exit_time": c_time,
                        "pnl": (entry_price - sl_price) * qty
                    })
                    position = None
                    
        # Check entry if flat
        if position is None:
            vol = "normal"
            if atr_percentiles[i] < 0.25:
                vol = "low"
            elif atr_percentiles[i] > 0.80:
                vol = "high"
                
            trend = trend_state[i]
            
            # --- STRATEGY 1: Bollinger Bands + RSI (Low-Vol Range) ---
            if trend == "range" and vol == "low":
                if prices[i] <= bb_lower[i] and rsi[i] < 30:
                    position = "LONG"
                    entry_price = candle["close"]
                    entry_time = c_time
                    tp_price = entry_price * (1 + 0.0005) # TP 0.05%
                    sl_price = entry_price * (1 - 0.0020) # SL 0.20%
                    active_strategy = "S1_Bollinger_RSI"
                elif prices[i] >= bb_upper[i] and rsi[i] > 70:
                    position = "SHORT"
                    entry_price = candle["close"]
                    entry_time = c_time
                    tp_price = entry_price * (1 - 0.0005)
                    sl_price = entry_price * (1 + 0.0020)
                    active_strategy = "S1_Bollinger_RSI"
                    
            # --- STRATEGY 2: SuperTrend + VWAP (High-Confidence Trend Follow) ---
            elif trend in ("up", "down") and vol != "low":
                is_bullish = st_trend[i] == 1 and prices[i] > vwap[i]
                is_bearish = st_trend[i] == -1 and prices[i] < vwap[i]
                
                if is_bullish and ema_fast_1m[i-1] <= ema_slow_1m[i-1] and ema_fast_1m[i] > ema_slow_1m[i]:
                    position = "LONG"
                    entry_price = candle["close"]
                    entry_time = c_time
                    tp_price = entry_price * (1 + 0.0015) # TP 0.15%
                    sl_price = entry_price * (1 - 0.0020) # SL 0.20%
                    active_strategy = "S2_SuperTrend_VWAP"
                elif is_bearish and ema_fast_1m[i-1] >= ema_slow_1m[i-1] and ema_fast_1m[i] < ema_slow_1m[i]:
                    position = "SHORT"
                    entry_price = candle["close"]
                    entry_time = c_time
                    tp_price = entry_price * (1 - 0.0015)
                    sl_price = entry_price * (1 + 0.0020)
                    active_strategy = "S2_SuperTrend_VWAP"
                    
            # --- STRATEGY 3: EMA Pullback + MACD (Trend Pullback) ---
            elif trend in ("up", "down") and vol == "normal":
                is_uptrend = prices[i] > ema_trend_50[i]
                is_downtrend = prices[i] < ema_trend_50[i]
                
                if is_uptrend and candle["low"] <= ema_slow_1m[i] and macd_hist[i-1] <= 0 and macd_hist[i] > 0:
                    position = "LONG"
                    entry_price = candle["close"]
                    entry_time = c_time
                    tp_price = entry_price * (1 + 0.0015)
                    sl_price = entry_price * (1 - 0.0020)
                    active_strategy = "S3_EMA_MACD_Pullback"
                elif is_downtrend and candle["high"] >= ema_slow_1m[i] and macd_hist[i-1] >= 0 and macd_hist[i] < 0:
                    position = "SHORT"
                    entry_price = candle["close"]
                    entry_time = c_time
                    tp_price = entry_price * (1 - 0.0015)
                    sl_price = entry_price * (1 + 0.0020)
                    active_strategy = "S3_EMA_MACD_Pullback"
                    
            # --- STRATEGY 4: Donchian Channel Breakout (Explosive Breakout) ---
            elif vol == "high":
                high_vol = candle["volume"] > 1.5 * volume_sma[i]
                if high_vol and candle["high"] >= donchian_upper[i-1]:
                    position = "LONG"
                    entry_price = candle["close"]
                    entry_time = c_time
                    tp_price = entry_price * (1 + 0.0020) # TP 0.20%
                    sl_price = entry_price * (1 - 0.0010) # SL 0.10%
                    active_strategy = "S4_Donchian_Breakout"
                elif high_vol and candle["low"] <= donchian_lower[i-1]:
                    position = "SHORT"
                    entry_price = candle["close"]
                    entry_time = c_time
                    tp_price = entry_price * (1 - 0.0020)
                    sl_price = entry_price * (1 + 0.0010)
                    active_strategy = "S4_Donchian_Breakout"
                    
            # --- STRATEGY 5: Stochastic Reversion (Wide Normal Range) ---
            elif trend == "range" and vol == "normal":
                if stoch_k[i-1] <= stoch_d[i-1] and stoch_k[i] > stoch_d[i] and stoch_d[i] < 20:
                    position = "LONG"
                    entry_price = candle["close"]
                    entry_time = c_time
                    tp_price = entry_price * (1 + 0.0015)
                    sl_price = entry_price * (1 - 0.0015)
                    active_strategy = "S5_Stoch_Reversion"
                elif stoch_k[i-1] >= stoch_d[i-1] and stoch_k[i] < stoch_d[i] and stoch_d[i] > 80:
                    position = "SHORT"
                    entry_price = candle["close"]
                    entry_time = c_time
                    tp_price = entry_price * (1 - 0.0015)
                    sl_price = entry_price * (1 + 0.0015)
                    active_strategy = "S5_Stoch_Reversion"

    # Analyze combined regime results
    total_pnl = sum(t["pnl"] for t in trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    
    win_rate = len(wins) / len(trades) if trades else 0
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    
    # Counts by strategy
    strategy_counts = {}
    strategy_pnls = {}
    for t in trades:
        s_name = t["strategy"]
        strategy_counts[s_name] = strategy_counts.get(s_name, 0) + 1
        strategy_pnls[s_name] = strategy_pnls.get(s_name, 0.0) + t["pnl"]
        
    print("\n==========================================================================")
    print(f"      DYNAMIC REGIME-SWITCHED PORTFOLIO BACKTEST REPORT ({args.days}-DAY 1M)       ")
    print("==========================================================================")
    print("Market Classifier: Real-time Volatility, Trend, and Playbook classification")
    print("Fee Model: Maker TP & SL (0% Commission)")
    print("--------------------------------------------------------------------------")
    print("Strategy Breakdown in Combined Live Simulation:")
    for s_name in sorted(strategy_counts):
        print(f"  - {s_name:25} | Trades: {strategy_counts[s_name]:3d} | Net PnL: {strategy_pnls[s_name]:+.4f} USDC")
    print("--------------------------------------------------------------------------")
    print(f"Combined Portfolio Total Trades: {len(trades)}")
    print(f"Combined Portfolio Win Rate: {win_rate*100:.2f}%")
    print(f"Combined Portfolio Profit Factor: {profit_factor:.4f}")
    print(f"Combined Portfolio Net PnL: {total_pnl:+.4f} USDC 🌟" if total_pnl >= 70.0 else f"Combined Portfolio Net PnL: {total_pnl:+.4f} USDC 🔴")
    print("==========================================================================")

if __name__ == "__main__":
    run_regime_portfolio_backtest()
