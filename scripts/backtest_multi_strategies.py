import asyncio
import os
import sys
import time
import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from statistics import mean, stdev
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_maker_scalp import fetch_1m_klines, calculate_ema
from scripts.backtest_smart_scalp import calculate_vwap, calculate_atr

TAIPEI = ZoneInfo("Asia/Taipei")

# ── INDICATORS HELPERS ────────────────────────────────────────────────────────

def calculate_bollinger_bands(prices: list[float], period: int = 20, num_std: float = 2.0):
    upper = []
    lower = []
    mid = []
    for i in range(len(prices)):
        if i < period - 1:
            mid.append(prices[i])
            upper.append(prices[i])
            lower.append(prices[i])
        else:
            window = prices[i - period + 1 : i + 1]
            m = mean(window)
            s = stdev(window) if len(window) > 1 else 0.0
            mid.append(m)
            upper.append(m + num_std * s)
            lower.append(m - num_std * s)
    return upper, mid, lower

def calculate_rsi(prices: list[float], period: int = 14) -> list[float]:
    if not prices:
        return []
    rsi = [50.0] * len(prices)
    if len(prices) < period + 1:
        return rsi
        
    gains = []
    losses = []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i-1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
        
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    
    if avg_loss == 0:
        rsi[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        rsi[period] = 100.0 - (100.0 / (1.0 + rs))
        
    for i in range(period + 1, len(prices)):
        avg_gain = (avg_gain * (period - 1) + gains[i-1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i-1]) / period
        if avg_loss == 0:
            rsi[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            rsi[i] = 100.0 - (100.0 / (1.0 + rs))
    return rsi

def calculate_macd(prices: list[float], fast_period: int = 12, slow_period: int = 26, signal_period: int = 9):
    ema_fast = calculate_ema(prices, fast_period)
    ema_slow = calculate_ema(prices, slow_period)
    macd_line = []
    for f, s in zip(ema_fast, ema_slow):
        macd_line.append(f - s)
    signal_line = calculate_ema(macd_line, signal_period)
    hist = []
    for m, sig in zip(macd_line, signal_line):
        hist.append(m - sig)
    return macd_line, signal_line, hist

def calculate_stochastic(candles: list[dict], period: int = 14, d_period: int = 3):
    pk = []
    pd = []
    for i in range(len(candles)):
        if i < period - 1:
            pk.append(50.0)
        else:
            window = candles[i - period + 1 : i + 1]
            lows = [c["low"] for c in window]
            highs = [c["high"] for c in window]
            min_low = min(lows)
            max_high = max(highs)
            diff = max_high - min_low
            if diff == 0:
                pk.append(50.0)
            else:
                pk.append(100.0 * (candles[i]["close"] - min_low) / diff)
                
    # Calculate %D (SMA of %K)
    pd = [50.0] * len(pk)
    for i in range(len(pk)):
        if i >= d_period - 1:
            window = pk[i - d_period + 1 : i + 1]
            pd[i] = mean(window)
    return pk, pd

def calculate_donchian(candles: list[dict], period: int = 20):
    upper = []
    lower = []
    for i in range(len(candles)):
        if i < period - 1:
            upper.append(candles[i]["high"])
            lower.append(candles[i]["low"])
        else:
            window = candles[i - period + 1 : i + 1]
            upper.append(max(c["high"] for c in window))
            lower.append(min(c["low"] for c in window))
    return upper, lower

def calculate_supertrend(candles: list[dict], period: int = 10, multiplier: float = 3.0):
    atr = calculate_atr(candles, period)
    trend = [1] * len(candles)  # 1 = up, -1 = down
    supertrend = [0.0] * len(candles)
    
    upper_band = [0.0] * len(candles)
    lower_band = [0.0] * len(candles)
    
    for i in range(len(candles)):
        c = candles[i]
        hl2 = (c["high"] + c["low"]) / 2
        
        atr_val = atr[i] if (atr[i] is not None and atr[i] > 0) else 0.0
        basic_upper = hl2 + multiplier * atr_val
        basic_lower = hl2 - multiplier * atr_val
        
        if i == 0:
            upper_band[i] = basic_upper
            lower_band[i] = basic_lower
            supertrend[i] = basic_upper
        else:
            prev_close = candles[i-1]["close"]
            upper_band[i] = basic_upper if basic_upper < upper_band[i-1] or prev_close > upper_band[i-1] else upper_band[i-1]
            lower_band[i] = basic_lower if basic_lower > lower_band[i-1] or prev_close < lower_band[i-1] else lower_band[i-1]
            
            if trend[i-1] == 1:
                trend[i] = 1 if c["close"] > lower_band[i] else -1
            else:
                trend[i] = -1 if c["close"] < upper_band[i] else 1
                
            supertrend[i] = lower_band[i] if trend[i] == 1 else upper_band[i]
            
    return trend, supertrend

# ── STRATEGIES SIMULATIONS ───────────────────────────────────────────────────

def simulate_strategy(candles: list[dict], entry_signals: list[int], tp_pct: float, sl_pct: float, name: str, qty: float = 0.5) -> list[dict]:
    """
    entry_signals: 1 = LONG, -1 = SHORT, 0 = NO SIGNAL
    tp_pct: take profit offset percentage (e.g. 0.0010 = 0.10%)
    sl_pct: stop loss offset percentage
    """
    position = None
    entry_price = 0.0
    tp_price = 0.0
    sl_price = 0.0
    entry_time = None
    trades = []
    
    for i in range(130, len(candles)):
        candle = candles[i]
        c_time = datetime.fromtimestamp(candle["time_ms"]/1000, tz=timezone.utc).astimezone(TAIPEI)
        
        # Check active position exits
        if position == "LONG":
            if candle["high"] >= tp_price:
                trades.append({
                    "strategy": name,
                    "type": "LONG_TP",
                    "entry_time": entry_time,
                    "exit_time": c_time,
                    "entry_price": entry_price,
                    "exit_price": tp_price,
                    "pnl": (tp_price - entry_price) * qty
                })
                position = None
            elif candle["low"] <= sl_price:
                trades.append({
                    "strategy": name,
                    "type": "LONG_SL",
                    "entry_time": entry_time,
                    "exit_time": c_time,
                    "entry_price": entry_price,
                    "exit_price": sl_price,
                    "pnl": (sl_price - entry_price) * qty
                })
                position = None
                
        elif position == "SHORT":
            if candle["low"] <= tp_price:
                trades.append({
                    "strategy": name,
                    "type": "SHORT_TP",
                    "entry_time": entry_time,
                    "exit_time": c_time,
                    "entry_price": entry_price,
                    "exit_price": tp_price,
                    "pnl": (entry_price - tp_price) * qty
                })
                position = None
            elif candle["high"] >= sl_price:
                trades.append({
                    "strategy": name,
                    "type": "SHORT_SL",
                    "entry_time": entry_time,
                    "exit_time": c_time,
                    "entry_price": entry_price,
                    "exit_price": sl_price,
                    "pnl": (entry_price - sl_price) * qty
                })
                position = None
        
        # Check entry if flat
        if position is None:
            sig = entry_signals[i]
            if sig == 1:
                position = "LONG"
                entry_price = candle["close"]
                entry_time = c_time
                tp_price = entry_price * (1 + tp_pct)
                sl_price = entry_price * (1 - sl_pct)
            elif sig == -1:
                position = "SHORT"
                entry_price = candle["close"]
                entry_time = c_time
                tp_price = entry_price * (1 - tp_pct)
                sl_price = entry_price * (1 + sl_pct)
                
    return trades

# ── MAIN SWEEP & OPTIMIZATION ─────────────────────────────────────────────────

def main():
    candles = fetch_1m_klines("ETHUSDC", days=7)
    prices = [c["close"] for c in candles]
    
    print("\n--- Precalculating Indicators ---")
    
    # Precalculate indicators
    ema_fast_1m = calculate_ema(prices, 5)
    ema_slow_1m = calculate_ema(prices, 20)
    ema_trend_50 = calculate_ema(prices, 50)
    
    bb_upper, bb_mid, bb_lower = calculate_bollinger_bands(prices, 20, 2.0)
    rsi = calculate_rsi(prices, 14)
    macd_line, macd_signal, macd_hist = calculate_macd(prices, 12, 26, 9)
    stoch_k, stoch_d = calculate_stochastic(candles, 14, 3)
    donchian_upper, donchian_lower = calculate_donchian(candles, 20)
    st_trend, st_line = calculate_supertrend(candles, 10, 3.0)
    
    vwap = calculate_vwap(candles)
    atr = calculate_atr(candles, 14)
    volume_sma = calculate_ema([c["volume"] for c in candles], 20)
    
    qty = 0.5  # ~1000 USDC Notional (10 USDC margin @ 100x leverage)
    
    print("Precalculation done. Building strategies entry signals...")
    
    # ── Strategy 1: Bollinger Bands + RSI Mean Reversion ──
    s1_signals = [0] * len(candles)
    for i in range(1, len(candles)):
        if prices[i] <= bb_lower[i] and rsi[i] < 30:
            s1_signals[i] = 1
        elif prices[i] >= bb_upper[i] and rsi[i] > 70:
            s1_signals[i] = -1
            
    # ── Strategy 2: SuperTrend + Daily VWAP ──
    s2_signals = [0] * len(candles)
    for i in range(1, len(candles)):
        is_bullish = st_trend[i] == 1 and prices[i] > vwap[i]
        is_bearish = st_trend[i] == -1 and prices[i] < vwap[i]
        
        # Trigger entry on SuperTrend flip or simple EMA fast/slow crossover aligned with filters
        if is_bullish and ema_fast_1m[i-1] <= ema_slow_1m[i-1] and ema_fast_1m[i] > ema_slow_1m[i]:
            s2_signals[i] = 1
        elif is_bearish and ema_fast_1m[i-1] >= ema_slow_1m[i-1] and ema_fast_1m[i] < ema_slow_1m[i]:
            s2_signals[i] = -1
            
    # ── Strategy 3: EMA Pullback + MACD ──
    s3_signals = [0] * len(candles)
    for i in range(1, len(candles)):
        is_uptrend = prices[i] > ema_trend_50[i]
        is_downtrend = prices[i] < ema_trend_50[i]
        
        if is_uptrend and candles[i]["low"] <= ema_slow_1m[i] and macd_hist[i-1] <= 0 and macd_hist[i] > 0:
            s3_signals[i] = 1
        elif is_downtrend and candles[i]["high"] >= ema_slow_1m[i] and macd_hist[i-1] >= 0 and macd_hist[i] < 0:
            s3_signals[i] = -1
            
    # ── Strategy 4: Donchian Channel Breakout ──
    s4_signals = [0] * len(candles)
    for i in range(1, len(candles)):
        high_vol = candles[i]["volume"] > 1.5 * volume_sma[i]
        if high_vol and candles[i]["high"] >= donchian_upper[i-1]:
            s4_signals[i] = 1
        elif high_vol and candles[i]["low"] <= donchian_lower[i-1]:
            s4_signals[i] = -1
            
    # ── Strategy 5: Stochastic Reversion ──
    s5_signals = [0] * len(candles)
    for i in range(1, len(candles)):
        if stoch_k[i-1] <= stoch_d[i-1] and stoch_k[i] > stoch_d[i] and stoch_d[i] < 20:
            s5_signals[i] = 1
        elif stoch_k[i-1] >= stoch_d[i-1] and stoch_k[i] < stoch_d[i] and stoch_d[i] > 80:
            s5_signals[i] = -1

    # ── PARAMETER SWEEPS & PORTFOLIO COMPOSITION ─────────────────────────────
    
    print("\n--- Tuning Individual Strategies to Maximize Profit ---")
    
    # We will sweep TP: 0.05%, 0.10%, 0.15%, 0.20% and SL: 0.10%, 0.15%, 0.20%
    tp_options = [0.0005, 0.0010, 0.0015, 0.0020]
    sl_options = [0.0010, 0.0015, 0.0020]
    
    strategies = [
        {"name": "S1_Bollinger_RSI", "signals": s1_signals},
        {"name": "S2_SuperTrend_VWAP", "signals": s2_signals},
        {"name": "S3_EMA_MACD_Pullback", "signals": s3_signals},
        {"name": "S4_Donchian_Breakout", "signals": s4_signals},
        {"name": "S5_Stoch_Reversion", "signals": s5_signals}
    ]
    
    best_configs = {}
    
    for s in strategies:
        best_pnl = -9999.0
        best_cfg = None
        best_trades = []
        
        for tp in tp_options:
            for sl in sl_options:
                trades = simulate_strategy(candles, s["signals"], tp, sl, s["name"], qty)
                pnl = sum(t["pnl"] for t in trades)
                if pnl > best_pnl:
                    best_pnl = pnl
                    best_cfg = {"tp": tp, "sl": sl, "pnl": pnl, "trades_count": len(trades)}
                    best_trades = trades
                    
        best_configs[s["name"]] = {
            "config": best_cfg,
            "trades": best_trades
        }
        
        print(f"Strategy {s['name']:25} | Best TP: {best_cfg['tp']*100:.2f}% | Best SL: {best_cfg['sl']*100:.2f}% | Trades: {best_cfg['trades_count']:3d} | Net PnL: {best_cfg['pnl']:+.4f} USDC")

    # ── PORTFOLIO COMBINATION ─────────────────────────────────────────────────
    
    print("\n========================================================")
    print("      PORTFOLIO MULTI-STRATEGY BACKTEST SUMMARY         ")
    print("========================================================")
    
    portfolio_pnl = 0.0
    total_trades = 0
    total_wins = 0
    total_losses = 0
    
    for name, data in best_configs.items():
        cfg = data["config"]
        trades = data["trades"]
        wins = [t for t in trades if t["pnl"] > 0]
        losses = [t for t in trades if t["pnl"] < 0]
        
        portfolio_pnl += cfg["pnl"]
        total_trades += len(trades)
        total_wins += len(wins)
        total_losses += len(losses)
        
        win_rate = (len(wins) / len(trades) * 100) if trades else 0.0
        print(f"{name:25} | Config: TP={cfg['tp']*100:.2f}%, SL={cfg['sl']*100:.2f}% | Trades: {len(trades):3d} | WinRate: {win_rate:5.1f}% | Net PnL: {cfg['pnl']:+.2f} USDC")
        
    print("-" * 56)
    combined_win_rate = (total_wins / total_trades * 100) if total_trades else 0.0
    print(f"Portfolio Total Trades: {total_trades}")
    print(f"Portfolio Combined Win Rate: {combined_win_rate:.2f}%")
    print(f"Portfolio Combined Net PnL: {portfolio_pnl:+.4f} USDC 🌟" if portfolio_pnl >= 70.0 else f"Portfolio Combined Net PnL: {portfolio_pnl:+.4f} USDC 🔴")
    print("========================================================")
    
if __name__ == "__main__":
    main()
