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

from config.settings import Settings

TAIPEI = ZoneInfo("Asia/Taipei")

def fetch_1m_klines(
    symbol: str,
    days: int = 7,
    start_dt: datetime | None = None,
    end_dt: datetime | None = None,
) -> list[dict]:
    """Fetch recent 1m candles from Binance Futures API."""
    base_url = "https://fapi.binance.com"
    if end_dt is None:
        end_dt = datetime.now(timezone.utc)
    if start_dt is None:
        start_dt = end_dt - timedelta(days=days)
    
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    
    print(f"Fetching 1m klines for {symbol} from {start_dt} to {end_dt}...")
    
    rows = []
    cursor = start_ms
    while cursor < end_ms:
        url = f"{base_url}/fapi/v1/klines"
        params = {
            "symbol": symbol,
            "interval": "1m",
            "startTime": cursor,
            "endTime": end_ms,
            "limit": 1500
        }
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        rows.extend(batch)
        next_cursor = int(batch[-1][0]) + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(batch) < 1500:
            break
        time.sleep(0.2)
        
    print(f"Fetched {len(rows)} raw klines.")
    
    # Map to clean dictionary format
    candles = []
    for r in rows:
        candles.append({
            "time_ms": int(r[0]),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
            "volume": float(r[5])
        })
    return candles

def calculate_ema(prices: list[float], period: int) -> list[float]:
    if not prices:
        return []
    ema = []
    multiplier = 2 / (period + 1)
    
    # Start with simple moving average
    sma = sum(prices[:period]) / period
    ema.extend([sma] * period)
    
    for i in range(period, len(prices)):
        val = (prices[i] - ema[-1]) * multiplier + ema[-1]
        ema.append(val)
    return ema

def run_scalp_backtest(candles: list[dict]):
    prices = [c["close"] for c in candles]
    ema_fast = calculate_ema(prices, 5)
    ema_slow = calculate_ema(prices, 20)
    
    position = None # None, "LONG", or "SHORT"
    entry_price = 0.0
    tp_price = 0.0
    sl_price = 0.0
    qty = 0.5 # 0.5 ETH per trade (~1000 USDC notional)
    
    trades = []
    
    for i in range(20, len(candles)):
        candle = candles[i]
        c_time = datetime.fromtimestamp(candle["time_ms"]/1000, tz=timezone.utc).astimezone(TAIPEI)
        
        # Check active position exits
        if position == "LONG":
            if candle["high"] >= tp_price:
                # Maker Take Profit
                profit = (tp_price - entry_price) * qty
                trades.append({
                    "type": "LONG_TP",
                    "entry_time": entry_time,
                    "exit_time": c_time,
                    "entry_price": entry_price,
                    "exit_price": tp_price,
                    "pnl": profit,
                    "fee": 0.0
                })
                position = None
            elif candle["low"] <= sl_price:
                # Maker Stop Loss (simulated as filled at exactly SL level with 0 fee)
                loss = (sl_price - entry_price) * qty
                trades.append({
                    "type": "LONG_SL",
                    "entry_time": entry_time,
                    "exit_time": c_time,
                    "entry_price": entry_price,
                    "exit_price": sl_price,
                    "pnl": loss,
                    "fee": 0.0
                })
                position = None
                
        elif position == "SHORT":
            if candle["low"] <= tp_price:
                # Maker Take Profit
                profit = (entry_price - tp_price) * qty
                trades.append({
                    "type": "SHORT_TP",
                    "entry_time": entry_time,
                    "exit_time": c_time,
                    "entry_price": entry_price,
                    "exit_price": tp_price,
                    "pnl": profit,
                    "fee": 0.0
                })
                position = None
            elif candle["high"] >= sl_price:
                # Maker Stop Loss
                loss = (entry_price - sl_price) * qty
                trades.append({
                    "type": "SHORT_SL",
                    "entry_time": entry_time,
                    "exit_time": c_time,
                    "entry_price": entry_price,
                    "exit_price": sl_price,
                    "pnl": loss,
                    "fee": 0.0
                })
                position = None
        
        # Check entry signal if flat
        if position is None:
            prev_fast = ema_fast[i-1]
            prev_slow = ema_slow[i-1]
            curr_fast = ema_fast[i]
            curr_slow = ema_slow[i]
            
            # Crossover Long
            if prev_fast <= prev_slow and curr_fast > curr_slow:
                position = "LONG"
                entry_price = candle["close"]
                entry_time = c_time
                tp_price = entry_price * 1.0010  # +0.10% TP
                sl_price = entry_price * 0.9990  # -0.10% SL
                
            # Crossunder Short
            elif prev_fast >= prev_slow and curr_fast < curr_slow:
                position = "SHORT"
                entry_price = candle["close"]
                entry_time = c_time
                tp_price = entry_price * 0.9990  # +0.10% TP
                sl_price = entry_price * 1.0010  # -0.10% SL
                
    # Calculate performance metrics
    total_pnl = sum(t["pnl"] for t in trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    
    win_rate = len(wins) / len(trades) if trades else 0
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
    
    print("\n==============================================")
    print("      MAKER SCALP 1-WEEK 1M BACKTEST REPORT   ")
    print("==============================================")
    print(f"Strategy: EMA 5/20 Crossover (1m chart)")
    print(f"Symbol: ETHUSDC | Sizing: {qty} ETH (~1000 USDC)")
    print(f"Fee Model: Maker TP & SL (0.0% Commission)")
    print(f"TP Offset: +0.10% | SL Offset: -0.10% (1:1 Ratio)")
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
    run_scalp_backtest(candles)
