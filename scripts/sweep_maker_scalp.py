import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_maker_scalp import fetch_1m_klines, calculate_ema

TAIPEI = ZoneInfo("Asia/Taipei")

def run_single_backtest(candles: list[dict], ema_fast: list[float], ema_slow: list[float], tp_pct: float, sl_pct: float) -> dict:
    position = None
    entry_price = 0.0
    tp_price = 0.0
    sl_price = 0.0
    qty = 0.5 # ~1000 USDC notional (10 USDC margin * 100x leverage when ETH is ~2000)
    
    trades = []
    
    for i in range(20, len(candles)):
        candle = candles[i]
        c_time = datetime.fromtimestamp(candle["time_ms"]/1000, tz=timezone.utc).astimezone(TAIPEI)
        
        # Check active position exits
        if position == "LONG":
            if candle["high"] >= tp_price:
                profit = (tp_price - entry_price) * qty
                trades.append({"pnl": profit})
                position = None
            elif candle["low"] <= sl_price:
                loss = (sl_price - entry_price) * qty
                trades.append({"pnl": loss})
                position = None
                
        elif position == "SHORT":
            if candle["low"] <= tp_price:
                profit = (entry_price - tp_price) * qty
                trades.append({"pnl": profit})
                position = None
            elif candle["high"] >= sl_price:
                loss = (entry_price - sl_price) * qty
                trades.append({"pnl": loss})
                position = None
        
        # Check entry signal if flat
        if position is None:
            prev_fast = ema_fast[i-1]
            prev_slow = ema_slow[i-1]
            curr_fast = ema_fast[i]
            curr_slow = ema_slow[i]
            
            if prev_fast <= prev_slow and curr_fast > curr_slow:
                position = "LONG"
                entry_price = candle["close"]
                entry_time = c_time
                tp_price = entry_price * (1 + tp_pct)
                sl_price = entry_price * (1 - sl_pct)
                
            elif prev_fast >= prev_slow and curr_fast < curr_slow:
                position = "SHORT"
                entry_price = candle["close"]
                entry_time = c_time
                tp_price = entry_price * (1 - tp_pct)
                sl_price = entry_price * (1 + sl_pct)
                
    # Metrics
    total_pnl = sum(t["pnl"] for t in trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    
    win_rate = len(wins) / len(trades) if trades else 0
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    
    return {
        "trades_count": len(trades),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "net_pnl": total_pnl
    }

def main():
    candles = fetch_1m_klines("ETHUSDC", days=7)
    prices = [c["close"] for c in candles]
    ema_fast = calculate_ema(prices, 5)
    ema_slow = calculate_ema(prices, 20)
    
    # Sweep parameters
    # Margin = 10 USDC, Leverage = 100x -> Notional = 1000 USDC
    # TP: 0.5 USDC (0.05%), 1.0 USDC (0.10%), 1.5 USDC (0.15%), 2.0 USDC (0.20%)
    # SL: 1.0 USDC (10% of margin = 0.10%), 1.5 USDC (15% = 0.15%), 2.0 USDC (20% = 0.20%)
    tp_options = [0.0005, 0.0010, 0.0015, 0.0020]
    sl_options = [0.0010, 0.0015, 0.0020]
    
    results = []
    for tp in tp_options:
        for sl in sl_options:
            res = run_single_backtest(candles, ema_fast, ema_slow, tp, sl)
            results.append({
                "tp_pct": tp,
                "sl_pct": sl,
                "tp_desc": f"{tp*100:.2f}% ({tp*1000:.1f} USDC)",
                "sl_desc": f"{sl*100:.2f}% ({sl*1000:.1f} USDC)",
                **res
            })
            
    # Print results sorted by PnL descending
    results.sort(key=lambda x: x["net_pnl"], reverse=True)
    
    print("\n=======================================================================")
    print("      MAKER SCALP PARAMETER SWEEP REPORT (10 USDC Margin / 100x Lev)   ")
    print("=======================================================================")
    print(f"{'TP (Offset)':25} | {'SL (Offset)':25} | {'Trades':6} | {'WinRate':7} | {'PF':6} | {'Net PnL (USDC)':15}")
    print("-" * 92)
    for r in results:
        pnl_str = f"{r['net_pnl']:+.2f}"
        print(f"{r['tp_desc']:25} | {r['sl_desc']:25} | {r['trades_count']:6d} | {r['win_rate']*100:6.2f}% | {r['profit_factor']:6.4f} | {pnl_str:15}")
    print("=======================================================================")

if __name__ == "__main__":
    main()
