"""
探索腳本 v3：修正時間戳問題，專注抓 SAPI algo 端點
"""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
import aiohttp

load_dotenv()

API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

SAPI_BASE = "https://api.binance.com"
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_responses_algo.json")


async def get_server_time(session: aiohttp.ClientSession) -> int:
    """Get Binance server time to avoid timestamp issues"""
    async with session.get(f"{SAPI_BASE}/api/v3/time") as resp:
        data = await resp.json()
        return data["serverTime"]


def sign_params(params: dict) -> str:
    query_string = urllib.parse.urlencode(params)
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return query_string + "&signature=" + signature


async def sapi_get(session: aiohttp.ClientSession, path: str, params: dict | None = None, server_time: int | None = None) -> dict | list:
    if params is None:
        params = {}
    params["timestamp"] = server_time or int(time.time() * 1000)
    params["recvWindow"] = 60000
    signed_qs = sign_params(params)
    url = f"{SAPI_BASE}{path}?{signed_qs}"
    headers = {"X-MBX-APIKEY": API_KEY}
    async with session.get(url, headers=headers) as resp:
        text = await resp.text()
        if resp.status != 200:
            return {"_error": f"HTTP {resp.status}: {text[:500]}"}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"_error": f"Invalid JSON: {text[:500]}"}


async def fapi_get(session: aiohttp.ClientSession, path: str, params: dict | None = None, server_time: int | None = None) -> dict | list:
    """Call FAPI (futures) endpoints directly"""
    FAPI_BASE = "https://fapi.binance.com"
    if params is None:
        params = {}
    params["timestamp"] = server_time or int(time.time() * 1000)
    params["recvWindow"] = 60000
    signed_qs = sign_params(params)
    url = f"{FAPI_BASE}{path}?{signed_qs}"
    headers = {"X-MBX-APIKEY": API_KEY}
    async with session.get(url, headers=headers) as resp:
        text = await resp.text()
        if resp.status != 200:
            return {"_error": f"HTTP {resp.status}: {text[:500]}"}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"_error": f"Invalid JSON: {text[:500]}"}


async def main():
    if not API_KEY or not API_SECRET:
        print("ERROR: set BINANCE_API_KEY and BINANCE_API_SECRET in .env")
        return

    session = aiohttp.ClientSession()
    results = {}

    # Get server time first
    print("[*] Getting Binance server time...")
    server_time = await get_server_time(session)
    local_time = int(time.time() * 1000)
    diff = local_time - server_time
    print(f"    Server: {server_time}, Local: {local_time}, Diff: {diff}ms")

    # ── 1. SAPI: Futures Grid Open Orders ──
    print("\n[1] /sapi/v1/algo/futures/openOrders")
    open_orders = await sapi_get(session, "/sapi/v1/algo/futures/openOrders", server_time=server_time)
    results["futures_grid_open_orders"] = open_orders
    print(f"    Result: {json.dumps(open_orders, indent=2)[:500]}")

    # ── 2. SAPI: Futures Grid Historical Orders ──
    print("\n[2] /sapi/v1/algo/futures/historicalOrders")
    hist_orders = await sapi_get(session, "/sapi/v1/algo/futures/historicalOrders", server_time=server_time)
    results["futures_grid_historical_orders"] = hist_orders
    print(f"    Result keys/length: {type(hist_orders)} ", end="")
    if isinstance(hist_orders, dict):
        print(list(hist_orders.keys())[:5])
    elif isinstance(hist_orders, list):
        print(f"len={len(hist_orders)}")

    # ── 3. Sub Orders ──
    algo_ids = []
    for source in [open_orders, hist_orders]:
        if isinstance(source, dict) and "_error" not in source:
            orders_list = source.get("orders", [])
            if isinstance(orders_list, list):
                for o in orders_list[:10]:
                    aid = o.get("algoId")
                    if aid and aid not in algo_ids:
                        algo_ids.append(aid)

    print(f"\n[3] Sub Orders (found {len(algo_ids)} algo IDs: {algo_ids[:5]})")
    results["futures_grid_sub_orders"] = {}
    for algo_id in algo_ids[:5]:
        server_time = await get_server_time(session)
        sub = await sapi_get(
            session, "/sapi/v1/algo/futures/subOrders",
            {"algoId": algo_id, "pageSize": 30},
            server_time=server_time
        )
        results["futures_grid_sub_orders"][str(algo_id)] = sub
        print(f"    algoId={algo_id}: ", end="")
        if isinstance(sub, dict) and "_error" not in sub:
            sub_list = sub.get("subOrders", [])
            print(f"{len(sub_list)} sub orders")
            if sub_list:
                print(f"    Sample: {json.dumps(sub_list[0], indent=2)}")
        else:
            print(sub)

    # ── 4. Try FAPI endpoints too (some grid APIs might be here) ──
    print("\n[4] FAPI: /fapi/v1/openOrders (all open futures orders)")
    for symbol in ["BTCUSDC", "ETHUSDC", "SOLUSDC"]:
        server_time = await get_server_time(session)
        open_f = await fapi_get(session, "/fapi/v1/openOrders", {"symbol": symbol}, server_time=server_time)
        count = len(open_f) if isinstance(open_f, list) else "error"
        print(f"    {symbol}: {count} open orders")
        results[f"fapi_open_orders_{symbol}"] = open_f if isinstance(open_f, list) and len(open_f) <= 10 else (
            {"_count": len(open_f), "sample": open_f[:3]} if isinstance(open_f, list) else open_f
        )

    # ── 5. FAPI: /fapi/v1/userTrades (recent trades) ──
    print("\n[5] FAPI: /fapi/v1/userTrades (last 20)")
    for symbol in ["BTCUSDC", "ETHUSDC", "SOLUSDC"]:
        server_time = await get_server_time(session)
        trades = await fapi_get(session, "/fapi/v1/userTrades", {"symbol": symbol, "limit": 20}, server_time=server_time)
        count = len(trades) if isinstance(trades, list) else "error"
        print(f"    {symbol}: {count} trades")
        if isinstance(trades, list) and trades:
            results[f"fapi_user_trades_{symbol}"] = {
                "_count": len(trades),
                "sample": trades[:3],
                "last": trades[-1] if trades else None,
            }
        else:
            results[f"fapi_user_trades_{symbol}"] = trades

    # ── 6. FAPI: /fapi/v1/allOrders (order history) ──
    print("\n[6] FAPI: /fapi/v1/allOrders (last 20)")
    for symbol in ["BTCUSDC"]:  # Just one to check format
        server_time = await get_server_time(session)
        all_orders = await fapi_get(session, "/fapi/v1/allOrders", {"symbol": symbol, "limit": 20}, server_time=server_time)
        count = len(all_orders) if isinstance(all_orders, list) else "error"
        print(f"    {symbol}: {count} orders")
        if isinstance(all_orders, list) and all_orders:
            results[f"fapi_all_orders_{symbol}"] = {
                "_count": len(all_orders),
                "sample": all_orders[:3],
            }
        else:
            results[f"fapi_all_orders_{symbol}"] = all_orders

    # ── 7. Income history with more records ──
    print("\n[7] FAPI: Income history (last 200)")
    server_time = await get_server_time(session)
    income = await fapi_get(session, "/fapi/v1/income", {"limit": 200}, server_time=server_time)
    if isinstance(income, list):
        # Summarize by type
        by_type = {}
        for entry in income:
            t = entry.get("incomeType", "UNKNOWN")
            if t not in by_type:
                by_type[t] = {"count": 0, "total": 0.0, "sample": entry}
            by_type[t]["count"] += 1
            by_type[t]["total"] += float(entry.get("income", 0))

        print("    Summary by type:")
        for t, info in by_type.items():
            print(f"      {t}: {info['count']} entries, total={info['total']:.4f}")

        results["futures_income_summary"] = {t: {"count": v["count"], "total": v["total"], "sample": v["sample"]} for t, v in by_type.items()}
        results["futures_income_raw_last_10"] = income[-10:]
    else:
        results["futures_income_summary"] = income

    await session.close()

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)

    print(f"\n[DONE] Output: {OUTPUT_FILE}")
    print(f"       Size: {os.path.getsize(OUTPUT_FILE) / 1024:.1f} KB")


if __name__ == "__main__":
    asyncio.run(main())
