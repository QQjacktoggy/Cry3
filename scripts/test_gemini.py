"""Test Gemini AI analysis with real market data.

Usage: python scripts/test_gemini.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from config.settings import Settings
from src.gridbot.binance.client import BinanceFuturesClient
from src.gridbot.binance.models import MarketSnapshot, PositionInfo
from src.gridbot.grid.analyzer import compute_metrics
from src.gridbot.grid.models import GridMetrics
from src.gridbot.ai.gemini import GeminiAnalyzer


async def main():
    settings = Settings()

    if not settings.gemini_api_key:
        print("ERROR: GEMINI_API_KEY not set in .env")
        return

    # Connect to Binance
    client = BinanceFuturesClient(settings)
    await client.connect()

    symbols = settings.symbols_list
    print(f"[*] Fetching data for {symbols}...")

    # Collect data
    all_metrics: dict[str, GridMetrics] = {}
    all_markets: dict[str, MarketSnapshot] = {}
    all_positions: dict[str, PositionInfo | None] = {}
    all_funding: dict[str, list[dict]] = {}

    for symbol in symbols:
        result = await client.fetch_symbol_data(symbol)
        all_metrics[symbol] = compute_metrics(result)
        all_markets[symbol] = result.market
        all_positions[symbol] = result.position

        # Get funding rate history
        fr = await client.get_funding_rate_history(symbol, limit=10)
        all_funding[symbol] = fr

        print(f"  {symbol}: price=${result.market.current_price:,.2f}, trades={len(result.trades)}")

    # Get account info
    account = await client.get_account_info()

    # Run Gemini analysis
    print(f"\n[*] Running Gemini analysis (model: {settings.gemini_model})...")
    analyzer = GeminiAnalyzer(settings)

    rec = await analyzer.analyze(
        metrics=all_metrics,
        markets=all_markets,
        positions=all_positions,
        funding_rates=all_funding,
        current_strategy=settings.active_strategy_name,
        account_balance=account.total_margin_balance,
        margin_ratio=account.margin_ratio,
    )

    # Print results
    print(f"\n{'='*60}")
    print(f"  Gemini 分析結果")
    print(f"{'='*60}")
    print(f"  建議策略: {rec.recommended_strategy}")
    print(f"  信心度: {rec.confidence:.0%}")
    print(f"  槓桿建議: {rec.leverage_suggestion}x")
    print(f"  方向建議: {rec.direction_suggestion}")
    print(f"\n  市況摘要:")
    print(f"  {rec.market_condition_summary}")
    print(f"\n  推理:")
    print(f"  {rec.reasoning}")
    print(f"\n  Funding Rate 分析:")
    print(f"  {rec.funding_rate_analysis}")
    print(f"\n  清算風險:")
    print(f"  {rec.liquidation_risk_assessment}")

    if rec.parameter_adjustments:
        print(f"\n  參數調整:")
        for adj in rec.parameter_adjustments:
            print(f"    {adj.parameter}: {adj.current_value} → {adj.suggested_value} ({adj.reason})")

    if rec.risk_warnings:
        print(f"\n  ⚠️ 風險警告:")
        for w in rec.risk_warnings:
            print(f"    - {w}")

    # Test /ask
    print(f"\n{'='*60}")
    print(f"  Free-form /ask Test")
    print(f"{'='*60}")
    answer = await analyzer.ask("目前 BTC 的 funding rate 趨勢如何？適合做多還是做空？")
    print(f"  {answer}")

    await client.close()
    print("\n[DONE]")


if __name__ == "__main__":
    asyncio.run(main())
