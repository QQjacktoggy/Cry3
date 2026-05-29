import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from config.settings import Settings
from src.gridbot.binance.client import BinanceFuturesClient, is_grid_order

TAIPEI = ZoneInfo("Asia/Taipei")

async def main():
    settings = Settings()
    client = BinanceFuturesClient(settings)
    await client.connect()
    try:
        # 5/29 07:00:00 Taipei Time
        start_dt = datetime(2026, 5, 29, 7, 0, 0, tzinfo=TAIPEI)
        start_ms = int(start_dt.timestamp() * 1000)
        print(f"Checking trades since: {start_dt.isoformat()} ({start_ms})")

        # Let's check BTCUSDC, ETHUSDC, SOLUSDC, and other symbols if possible
        symbols = ["BTCUSDC", "ETHUSDC", "SOLUSDC"]
        
        # Let's also fetch recent income to see if there are other symbols
        income = await client.client.futures_income_history(startTime=start_ms, limit=1000)
        discovered_symbols = {item["symbol"] for item in income if item.get("symbol")}
        all_symbols = sorted(set(symbols) | discovered_symbols)
        print(f"Symbols to check: {all_symbols}")

        for symbol in all_symbols:
            trades = await client.client.futures_account_trades(symbol=symbol, startTime=start_ms, limit=1000)
            if not trades:
                continue
            
            print(f"\n--- {symbol} Trades ({len(trades)}) ---")
            for t in trades:
                trade_time = datetime.fromtimestamp(t["time"]/1000, tz=timezone.utc).astimezone(TAIPEI)
                client_order_id = t.get("clientOrderId", "")
                is_bot = is_grid_order(client_order_id)
                bot_label = "[BOT]" if is_bot else "[MANUAL]"
                print(f"Time: {trade_time.strftime('%Y-%m-%d %H:%M:%S')} | {bot_label} | Side: {t['side']} | Price: {t['price']} | Qty: {t['qty']} | RealizedPnL: {t.get('realizedPnl', '0')} | ClientOrderId: {client_order_id}")
                
        # Print all income records since 7:00 AM
        print("\n--- Income Records (REALIZED_PNL) ---")
        for item in income:
            if item["incomeType"] == "REALIZED_PNL":
                item_time = datetime.fromtimestamp(int(item["time"])/1000, tz=timezone.utc).astimezone(TAIPEI)
                print(f"Time: {item_time.strftime('%Y-%m-%d %H:%M:%S')} | Symbol: {item['symbol']} | Income: {item['income']} | TradeId: {item.get('tradeId')}")

    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(main())
