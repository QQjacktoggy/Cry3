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
        # Start time 12:55:00 Taipei Time to 13:10:00
        start_dt = datetime(2026, 5, 29, 12, 55, 0, tzinfo=TAIPEI)
        start_ms = int(start_dt.timestamp() * 1000)
        
        trades = await client.client.futures_account_trades(symbol="ETHUSDC", startTime=start_ms, limit=1000)
        orders = await client.client.futures_get_all_orders(symbol="ETHUSDC", startTime=start_ms, limit=1000)
        
        order_dict = {o["orderId"]: o for o in orders}
        
        print("--- Trades around 13:00 ---")
        for t in trades:
            t_time = datetime.fromtimestamp(t["time"]/1000, tz=timezone.utc).astimezone(TAIPEI)
            o_id = t["orderId"]
            order_info = order_dict.get(o_id, {})
            client_order_id = order_info.get("clientOrderId", "UNKNOWN")
            print(f"Trade Time: {t_time.strftime('%H:%M:%S')} | Side: {t['side']} | Price: {t['price']} | Qty: {t['qty']} | Realized PnL: {t['realizedPnl']} | OrderId: {o_id} | ClientOrderId: {client_order_id}")
            
    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(main())
