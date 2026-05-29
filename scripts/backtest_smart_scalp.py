import asyncio
import os
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

TAIPEI = ZoneInfo("Asia/Taipei")

def calculate_vwap(candles: list[dict]) -> list[float]:
    vwap = []
    current_day = None
    pv_sum = 0.0
    vol_sum = 0.0
    for c in candles:
        day = datetime.fromtimestamp(c["time_ms"]/1000, tz=timezone.utc).astimezone(TAIPEI).date()
        if day != current_day:
            current_day = day
            pv_sum = 0.0
            vol_sum = 0.0
        typical_price = (c["high"] + c["low"] + c["close"]) / 3
        pv_sum += typical_price * c["volume"]
        vol_sum += c["volume"]
        vwap.append(pv_sum / vol_sum if vol_sum > 0 else c["close"])
    return vwap

def calculate_atr(candles: list[dict], period: int = 14) -> list[float]:
    tr_list = []
    for i in range(len(candles)):
        c = candles[i]
        if i == 0:
            tr = c["high"] - c["low"]
        else:
            prev_c = candles[i-1]
            tr = max(c["high"] - c["low"], abs(c["high"] - prev_c["close"]), abs(c["low"] - prev_c["close"]))
        tr_list.append(tr)
        
    atr = [sum(tr_list[:period])/period] * period
    for i in range(period, len(candles)):
        val = (atr[-1] * (period - 1) + tr_list[i]) / period
        atr.append(val)
    return atr

def run_smart_scalp_backtest(candles: list[dict]):
    prices = [c["close"] for c in candles]
    
    # 1. 1m Technical Indicators
    ema_fast_1m = calculate_ema(prices, 5)
    ema_slow_1m = calculate_ema(prices, 20)
    vwap = calculate_vwap(candles)
    atr = calculate_atr(candles, 14)
    
    # 2. 5m Technical Indicators (simulated by downsampling)
    # We will build 5m EMAs on 1m candles by using larger periods (e.g. 5m EMA 12 is approx 1m EMA 60)
    ema_fast_5m = calculate_ema(prices, 60)   # ~12 period on 5m
    ema_slow_5m = calculate_ema(prices, 130)  # ~26 period on 5m
    
    position = None
    entry_price = 0.0
    tp_price = 0.0
    sl_price = 0.0
    qty = 0.5  # ~1000 USDC Notional (10 USDC margin @ 100x leverage)
    
    trades = []
    
    for i in range(130, len(candles)):
        candle = candles[i]
        c_time = datetime.fromtimestamp(candle["time_ms"]/1000, tz=timezone.utc).astimezone(TAIPEI)
        
        # Check active position exits
        if position == "LONG":
            if candle["high"] >= tp_price:
                profit = (tp_price - entry_price) * qty
                trades.append({
                    "type": "LONG_TP",
                    "entry_time": entry_time,
                    "exit_time": c_time,
                    "entry_price": entry_price,
                    "exit_price": tp_price,
                    "pnl": profit
                })
                position = None
            elif candle["low"] <= sl_price:
                loss = (sl_price - entry_price) * qty
                trades.append({
                    "type": "LONG_SL",
                    "entry_time": entry_time,
                    "exit_time": c_time,
                    "entry_price": entry_price,
                    "exit_price": sl_price,
                    "pnl": loss
                })
                position = None
                
        elif position == "SHORT":
            if candle["low"] <= tp_price:
                profit = (entry_price - tp_price) * qty
                trades.append({
                    "type": "SHORT_TP",
                    "entry_time": entry_time,
                    "exit_time": c_time,
                    "entry_price": entry_price,
                    "exit_price": tp_price,
                    "pnl": profit
                })
                position = None
            elif candle["high"] >= sl_price:
                loss = (entry_price - sl_price) * qty
                trades.append({
                    "type": "SHORT_SL",
                    "entry_time": entry_time,
                    "exit_time": c_time,
                    "entry_price": entry_price,
                    "exit_price": sl_price,
                    "pnl": loss
                })
                position = None
        
        # Check entry signal if flat
        if position is None:
            # Check Trend Alignment (Confidence Filter)
            # Long High Confidence: 
            # 1. 1m Fast EMA > Slow EMA
            # 2. 5m Trend is Bullish (Fast 5m EMA > Slow 5m EMA)
            # 3. Price is above daily VWAP
            # 4. Volatility is healthy (ATR > 0.5)
            is_long_aligned = (
                ema_fast_1m[i] > ema_slow_1m[i] and
                ema_fast_5m[i] > ema_slow_5m[i] and
                candle["close"] > vwap[i] and
                atr[i] > 0.5
            )
            
            # Short High Confidence:
            # 1. 1m Fast EMA < Slow EMA
            # 2. 5m Trend is Bearish (Fast 5m EMA < Slow 5m EMA)
            # 3. Price is below daily VWAP
            # 4. Volatility is healthy
            is_short_aligned = (
                ema_fast_1m[i] < ema_slow_1m[i] and
                ema_fast_5m[i] < ema_slow_5m[i] and
                candle["close"] < vwap[i] and
                atr[i] > 0.5
            )
            
            # Trigger Long Entry only on Fast EMA crossover under high confidence
            if is_long_aligned and ema_fast_1m[i-1] <= ema_slow_1m[i-1]:
                position = "LONG"
                entry_price = candle["close"]
                entry_time = c_time
                tp_price = entry_price * 1.0020  # +0.20% TP (2.0 USDC)
                sl_price = entry_price * 0.9985  # -0.15% SL (1.5 USDC) (15% on margin)
                
            # Trigger Short Entry only on Fast EMA crossunder under high confidence
            elif is_short_aligned and ema_fast_1m[i-1] >= ema_slow_1m[i-1]:
                position = "SHORT"
                entry_price = candle["close"]
                entry_time = c_time
                tp_price = entry_price * 0.9980  # +0.20% TP (2.0 USDC)
                sl_price = entry_price * 1.0015  # -0.15% SL (1.5 USDC) (15% on margin)
                
    # Calculate performance metrics
    total_pnl = sum(t["pnl"] for t in trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    
    win_rate = len(wins) / len(trades) if trades else 0
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    
    print("\n==============================================")
    print("   CONFIDENCE-FILTERED SCALP BACKTEST REPORT  ")
    print("==============================================")
    print(f"Strategy: EMA Crossover + Multi-Timeframe Alignment + VWAP")
    print(f"Symbol: ETHUSDC | Sizing: {qty} ETH (~1000 USDC)")
    print(f"Fee Model: Maker TP & SL (0.0% Commission)")
    print(f"TP Offset: +0.20% (2.0 USDC) | SL Offset: -0.15% (1.5 USDC)")
    print("----------------------------------------------")
    print(f"Total Completed Cycles: {len(trades)}")
    print(f"Wins: {len(wins)} (Total: +{gross_profit:.4f} USDC)")
    print(f"Losses: {len(losses)} (Total: -{gross_loss:.4f} USDC)")
    print(f"Win Rate: {win_rate*100:.2f}%")
    print(f"Profit Factor: {profit_factor:.4f}")
    print(f"Total Net PnL: {total_pnl:+.4f} USDC 🟢" if total_pnl > 0 else f"Total Net PnL: {total_pnl:+.4f} USDC 🔴")
    print("==============================================")
    
    # Print sample of last 15 trades
    print("\n--- Last 15 Completed Trades ---")
    for t in trades[-15:]:
        duration = t["exit_time"] - t["entry_time"]
        duration_min = int(duration.total_seconds() / 60)
        print(f"Entry: {t['entry_time'].strftime('%m-%d %H:%M')} | {t['type']:8} | EntryPx: {t['entry_price']:7.2f} | ExitPx: {t['exit_price']:7.2f} | PnL: {t['pnl']:+6.2f} USDC | Hold: {duration_min:2d}m")

if __name__ == "__main__":
    candles = fetch_1m_klines("ETHUSDC", days=7)
    run_smart_scalp_backtest(candles)
