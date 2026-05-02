"""Integration test: Fetch real Binance data → compute metrics → verify output.

Usage: python scripts/test_integration.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

from config.settings import Settings
from src.gridbot.binance.client import BinanceFuturesClient
from src.gridbot.binance.models import IncomeRecord
from src.gridbot.grid.analyzer import compute_metrics
from src.gridbot.storage.database import Database
from src.gridbot.storage.repositories import (
    AuditLogRepository,
    FuturesTradeRepository,
    GridSessionRepository,
    IncomeRepository,
    MarketSnapshotRepository,
)
from src.gridbot.binance.fetcher import BinanceFetcher


async def main():
    settings = Settings()
    print(f"[*] Symbols: {settings.symbols_list}")
    print(f"[*] Testnet: {settings.binance_testnet}")

    # ── Init database ──
    db = Database("data/test_integration.db")
    await db.initialize()
    print("[OK] Database initialized")

    # ── Init client ──
    client = BinanceFuturesClient(settings)
    await client.connect()
    print("[OK] Binance connected")

    # ── Init repos ──
    trade_repo = FuturesTradeRepository(db)
    income_repo = IncomeRepository(db)
    session_repo = GridSessionRepository(db)
    market_repo = MarketSnapshotRepository(db)
    audit_repo = AuditLogRepository(db)

    # ── Init fetcher ──
    fetcher = BinanceFetcher(
        client=client,
        trade_repo=trade_repo,
        income_repo=income_repo,
        session_repo=session_repo,
        market_repo=market_repo,
        audit_repo=audit_repo,
    )

    # ── Fetch all symbols ──
    print("\n[*] Fetching all symbols...")
    results = await fetcher.fetch_all_symbols(settings.symbols_list)

    for symbol, result in results.items():
        print(f"\n{'='*60}")
        print(f"  {symbol}")
        print(f"{'='*60}")
        print(f"  Trades: {len(result.trades)}")
        print(f"  Income records: {len(result.income_records)}")
        print(f"  Price: ${result.market.current_price:,.2f}")
        print(f"  Mark Price: ${result.market.mark_price:,.2f}")
        print(f"  Funding Rate: {result.market.funding_rate}")

        if result.position:
            p = result.position
            print(f"  Position: {p.position_direction} {abs(p.position_amt)}")
            print(f"  Entry: ${p.entry_price:,.2f}")
            print(f"  Leverage: {p.leverage}x")
            print(f"  Liq Price: ${p.liquidation_price:,.2f}")
            print(f"  Liq Distance: {p.distance_to_liquidation_pct:.1f}%")
            print(f"  Unrealized PnL: ${p.unrealized_pnl:.4f}")
        else:
            print("  Position: None (no open position)")

        if result.account:
            a = result.account
            print(f"  Account Balance: ${a.total_margin_balance:.2f}")
            print(f"  Margin Ratio: {a.margin_ratio:.4f}" if a.margin_ratio else "  Margin Ratio: N/A")

        # ── Compute metrics ──
        # Get active session for this symbol
        active_session = await session_repo.get_active_session()
        session_invested = active_session["invested_amount"] if active_session else None
        session_start = active_session["created_at_ms"] if active_session else None

        income_records = [IncomeRecord.from_api({
            "tranId": r["tran_id"], "symbol": r.get("symbol", ""),
            "incomeType": r["income_type"], "income": str(r["income"]),
            "asset": r["asset"], "time": r["time_ms"],
            "info": r.get("info", ""), "tradeId": r.get("trade_id", ""),
        }) for r in await income_repo.get_records(symbol=symbol)]

        metrics = compute_metrics(
            result=result,
            income_records=income_records if income_records else None,
            session_invested=session_invested,
            session_start_ms=session_start,
        )

        print(f"\n  --- Metrics ---")
        print(f"  Realized PnL: ${metrics.realized_pnl:.4f}")
        print(f"  Unrealized PnL: ${metrics.unrealized_pnl:.4f}")
        print(f"  Funding Cost: ${metrics.funding_cost:.4f}")
        print(f"  Commission: ${metrics.commission_total:.4f}")
        print(f"  Net PnL: ${metrics.net_pnl:.4f}")
        print(f"  Total Trades: {metrics.total_trades}")
        print(f"  Maker/Taker: {metrics.maker_trades}/{metrics.taker_trades} ({metrics.maker_ratio:.0%})")
        print(f"  Fill Rate: {metrics.fill_rate:.0%}")
        print(f"  APR: {metrics.apr_estimate:.1f}%" if metrics.apr_estimate else "  APR: N/A (< 24h)")

    # ── Grid sessions ──
    print(f"\n{'='*60}")
    print("  Grid Sessions")
    print(f"{'='*60}")
    sessions = await session_repo.get_sessions(limit=10)
    for s in sessions:
        status = "ACTIVE" if s["is_active"] else "CLOSED"
        profit = f"${s['net_profit']:.4f}" if s["net_profit"] is not None else "running"
        symbol = s.get("symbol") or "unknown"
        print(f"  [{status}] {symbol} | Invested: ${s['invested_amount']:.2f} | Profit: {profit}")

    total_profit = await session_repo.get_total_profit()
    print(f"\n  Total closed session profit: ${total_profit:.4f}")

    # ── Cleanup ──
    await client.close()
    await db.close()
    print("\n[DONE]")


if __name__ == "__main__":
    asyncio.run(main())
