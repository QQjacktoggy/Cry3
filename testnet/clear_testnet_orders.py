"""Script to clear all open orders and algo/conditional orders on Binance Futures Testnet."""

import asyncio
import os
import sys

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

async def clear_orders() -> None:
    settings = Settings()
    
    print("\n=== Initializing Testnet Client for Clearing Orders ===")
    print(f"BINANCE_TESTNET: {settings.binance_testnet}")
    print(f"API Key Prefix: {settings.binance_api_key[:6]}...")
    
    client = BinanceFuturesClient(settings)
    await client.connect()
    
    try:
        # 1. Fetch all open orders globally (without specifying a symbol)
        print("\n1. Fetching all open orders globally...")
        # Since client.get_open_orders(symbol) in our client wrapper requires a symbol parameter,
        # we can directly call the underlying python-binance client's futures_get_open_orders()
        open_orders = await client.client.futures_get_open_orders()
        print(f"   Found {len(open_orders)} open orders.")
        
        # 2. Cancel all open orders
        if open_orders:
            print("\n2. Cancelling open orders...")
            for order in open_orders:
                symbol = order.get("symbol")
                order_id = order.get("orderId")
                client_order_id = order.get("clientOrderId")
                price = order.get("price")
                side = order.get("side")
                print(f"   Cancelling {side} order {order_id} ({client_order_id}) on {symbol} at ${price}...")
                try:
                    res = await client.cancel_order(symbol, order_id=order_id)
                    print(f"   ✅ Cancelled! Status: {res.get('status')}")
                except Exception as e:
                    print(f"   ❌ Error cancelling order {order_id}: {e}")
        else:
            print("   No open orders found.")

        # 3. Handle open algo/conditional orders (which require querying symbol-by-symbol)
        # We will check the configured symbols + other common ones to be comprehensive.
        symbols_to_check = [s.strip() for s in settings.trading_symbols.split(",") if s.strip()]
        for fallback in ["ETHUSDC", "BTCUSDC", "SOLUSDC", "XRPUSDC", "DOGEUSDC"]:
            if fallback not in symbols_to_check:
                symbols_to_check.append(fallback)
        
        print(f"\n3. Fetching open algo/conditional orders for symbols: {symbols_to_check}...")
        for symbol in symbols_to_check:
            try:
                algo_orders = await client.get_open_algo_orders(symbol)
                if algo_orders:
                    print(f"   Found {len(algo_orders)} open algo orders on {symbol}.")
                    for order in algo_orders:
                        algo_id = order.get("algoId")
                        client_algo_id = order.get("clientAlgoId")
                        order_type = order.get("algoType")
                        print(f"   Cancelling {order_type} algo order {algo_id} ({client_algo_id}) on {symbol}...")
                        try:
                            res = await client.cancel_algo_order(symbol, algo_id=algo_id)
                            print(f"   ✅ Cancelled algo! Status: {res.get('status')}")
                        except Exception as e:
                            print(f"   ❌ Error cancelling algo order {algo_id}: {e}")
                else:
                    # Keep quiet if no algo orders
                    pass
            except Exception as e:
                # Some symbols might not be active or supported on testnet, ignore them gracefully
                print(f"   ⚠️ Could not fetch algo orders for {symbol}: {e}")

        # 4. Final verification
        print("\n4. Final Verification...")
        final_orders = await client.client.futures_get_open_orders()
        print(f"   Total remaining open orders: {len(final_orders)}")
        
        # Verify algo orders for the configured symbols
        for symbol in symbols_to_check:
            try:
                remaining_algos = await client.get_open_algo_orders(symbol)
                if remaining_algos:
                    print(f"   ⚠️ Remaining algo orders on {symbol}: {len(remaining_algos)}")
            except Exception:
                pass
                
        print("\n=== Finished Clearing Orders ===")
        
    except Exception as e:
        print(f"\n❌ ERROR during order clearing: {str(e)}")
        import traceback
        traceback.print_exc()
    finally:
        await client.close()

if __name__ == "__main__":
    asyncio.run(clear_orders())
