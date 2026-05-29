import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from config.settings import Settings

TAIPEI = ZoneInfo("Asia/Taipei")

async def main():
    settings = Settings()
    from src.gridbot.binance.client import BinanceFuturesClient
    client = BinanceFuturesClient(settings)
    await client.connect()
    try:
        # Start time 07:00:00 Taipei Time
        start_dt = datetime(2026, 5, 29, 7, 0, 0, tzinfo=TAIPEI)
        start_ms = int(start_dt.timestamp() * 1000)
        
        trades = await client.client.futures_account_trades(symbol="ETHUSDC", startTime=start_ms, limit=1000)
        
        total_realized_pnl = 0.0
        total_commission = 0.0
        wins_count = 0
        losses_count = 0
        wins_val = 0.0
        losses_val = 0.0
        
        print("--- Detailed Trade History (Since 07:00 AM) ---")
        for t in trades:
            t_time = datetime.fromtimestamp(t["time"]/1000, tz=timezone.utc).astimezone(TAIPEI)
            pnl = float(t.get("realizedPnl", 0))
            comm = float(t.get("commission", 0))
            total_realized_pnl += pnl
            total_commission += comm
            
            if pnl > 0:
                wins_count += 1
                wins_val += pnl
            elif pnl < 0:
                losses_count += 1
                losses_val += pnl
                
            print(f"Time: {t_time.strftime('%H:%M:%S')} | Side: {t['side']:4} | Price: {t['price']:8} | Qty: {t['qty']:6} | Realized PnL: {pnl:10.4f} | Comm: {comm:8.6f} | Maker: {t['maker']}")
            
        print("\n--- Summary Statistics ---")
        print(f"Total Trades: {len(trades)}")
        print(f"Wins Count: {wins_count} (Total: {wins_val:+.4f} USDC)")
        print(f"Losses Count: {losses_count} (Total: {losses_val:.4f} USDC)")
        print(f"Total Realized PnL: {total_realized_pnl:+.4f} USDC")
        print(f"Total Commission: {total_commission:.4f} USDC")
        print(f"Net PnL (ex. Funding): {total_realized_pnl - total_commission:+.4f} USDC")
        
    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(main())
