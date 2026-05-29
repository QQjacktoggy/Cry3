"""Binance Futures Testnet Order Placement Verification Script."""

import asyncio
import os
import sys
import time

# Ensure project root is in path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from dotenv import load_dotenv

# Load testnet environment variables
env_file = os.path.join(ROOT, "testnet", ".env.testnet")
if os.path.exists(env_file):
    print(f"Loading env from {env_file}")
    load_dotenv(env_file, override=True)
else:
    print(f"Missing {env_file}, loading default .env")
    load_dotenv()

from config.settings import Settings
from src.gridbot.binance.client import BinanceFuturesClient

async def test_order() -> None:
    settings = Settings()
    
    print("\n=== Initializing Testnet Client ===")
    print(f"BINANCE_TESTNET: {settings.binance_testnet}")
    print(f"DB_PATH: {settings.db_path}")
    print(f"API Key Prefix: {settings.binance_api_key[:6]}...")
    
    client = BinanceFuturesClient(settings)
    await client.connect()
    
    symbol = "ETHUSDC"
    try:
        # 1. Fetch current price
        print(f"\n1. Fetching current mark price for {symbol}...")
        mark = await client.get_mark_price(symbol)
        mark_price = float(mark.get("markPrice") or 0)
        print(f"   Current Mark Price: ${mark_price:,.2f}")
        
        # 2. Place a small limit buy order far below the market (e.g. mark_price * 0.8)
        test_price = round(mark_price * 0.8, 2)
        qty = "0.02"  # 0.02 ETH * ~1600 USD = ~32 USDC notional (exceeds the 20 USDC minimum)
        client_oid = f"cry3test_{int(time.time() * 1000)}"
        
        print(f"\n2. Placing test limit BUY order on {symbol}...")
        print(f"   Price: ${test_price:,.2f} | Quantity: {qty} | ClientOrderId: {client_oid}")
        
        # Ensure leverage is set first
        print("   Setting leverage to 5x...")
        await client.set_leverage(symbol, 5)
        
        order = await client.create_limit_order(
            symbol=symbol,
            side="BUY",
            quantity=qty,
            price=test_price,
            reduce_only=False,
            client_order_id=client_oid
        )
        
        order_id = order.get("orderId")
        print(f"   ✅ SUCCESS! Order placed successfully on Binance Testnet!")
        print(f"   OrderID: {order_id} | Status: {order.get('status')}")
        
        # 3. Fetch open orders to verify it is active
        print(f"\n3. Verifying order {order_id} in open orders list...")
        open_orders = await client.get_open_orders(symbol)
        matched = [o for o in open_orders if str(o.get("orderId")) == str(order_id)]
        if matched:
            print(f"   ✅ Verified! Order {order_id} is active in the order book.")
        else:
            print(f"   ❌ WARNING: Order {order_id} not found in open orders list!")
            
        # 4. Cancel the test order immediately
        print(f"\n4. Cancelling test order {order_id}...")
        cancel_res = await client.cancel_order(symbol, order_id=order_id)
        print(f"   ✅ SUCCESS! Order cancelled successfully! Status: {cancel_res.get('status')}")
        
    except Exception as e:
        print(f"\n❌ ERROR during order verification: {str(e)}")
        import traceback
        traceback.print_exc()
    finally:
        await client.close()
        print("\n=== Testnet Order Verification Finished ===")

if __name__ == "__main__":
    asyncio.run(test_order())
