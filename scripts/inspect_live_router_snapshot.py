from __future__ import annotations

import argparse
import asyncio
from pprint import pprint

from config.settings import Settings
from src.gridbot.binance.client import BinanceFuturesClient
from src.gridbot.strategy.long_orb import OrbConfig, build_orb_context, generate_orb_short_signal_at, generate_orb_signal_at
from src.gridbot.strategy.long_pullback import Candle, StrategyConfig, _risk_adjusted_config
from src.gridbot.strategy.market_state import build_market_state_context, classify_market_state
from src.gridbot.strategy.regime import build_regime_context, classify_regime
from src.gridbot.strategy.signal_journal import (
    explain_router_allocator_high_return_live_block,
    generate_router_allocator_high_return_live_decision,
)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect the current live router/orb decision snapshot.")
    parser.add_argument("--env-file", default="testnet/.env.testnet")
    parser.add_argument("--symbol", default="ETHUSDC")
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--limit", type=int, default=300)
    args = parser.parse_args()

    settings = Settings(_env_file=args.env_file)
    client = BinanceFuturesClient(settings)
    await client.connect()
    try:
        rows = await client.get_klines(args.symbol, interval=args.interval, limit=args.limit)
    finally:
        await client.close()

    candles = [Candle.from_binance_kline(row) for row in rows]
    base = StrategyConfig(
        symbol=args.symbol,
        equity_usdc=settings.testnet_equity_usdc,
        compounding_enabled=True,
        daily_target_min_pct=settings.testnet_daily_target_pct,
        daily_target_max_pct=settings.testnet_daily_target_pct,
        risk_per_trade_pct=100.0,
        min_score=60,
        max_effective_leverage=settings.max_effective_leverage,
        maker_fee_rate=settings.testnet_maker_fee_rate,
        taker_fee_rate=settings.testnet_taker_fee_rate,
        daily_soft_loss_pct=settings.daily_soft_loss_pct,
        daily_max_loss_pct=settings.max_daily_loss_pct,
        daily_loss_risk_scale=0.55,
        daily_target_stop_pct=10.0,
        max_open_positions=1,
        max_position_margin_pct=100.0,
        cooldown_bars=4,
        max_consecutive_losses_before_cooldown=3,
        consecutive_loss_cooldown_bars=18,
        max_holding_bars=48,
        take_profit_r=(0.55, 1.1, 2.2),
        exit_weights=(0.25, 0.35, 0.40),
    )

    runtime_base = _risk_adjusted_config(base, 0.0)
    config = OrbConfig(
        base=runtime_base,
        session_start_bar=0,
        opening_range_bars=9,
        min_volume_ratio=0.8,
        stop_atr=0.6,
    )
    context = build_orb_context(candles, config)
    regime_context = build_regime_context(candles, config.base)
    market_context = build_market_state_context(candles, config.base)
    index = len(candles) - 1

    regime = classify_regime(candles, index, regime_context, runtime_base)
    market = classify_market_state(candles, index, market_context, runtime_base)
    long_signal = generate_orb_signal_at(candles, index, config, context)
    short_signal = generate_orb_short_signal_at(candles, index, config, context)
    live_decision = generate_router_allocator_high_return_live_decision(candles, base, 0.0)
    block_reason = explain_router_allocator_high_return_live_block(candles, base, 0.0)
    candle = candles[index]

    print("=== Snapshot ===")
    print(
        {
            "symbol": args.symbol,
            "interval": args.interval,
            "open_time_ms": candle.open_time_ms,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
        }
    )
    print("\n=== Regime ===")
    pprint(
        {
            "regime": regime.regime,
            "risk_mode": regime.risk_mode,
            "confidence": regime.confidence,
            "features": getattr(regime, "features", None),
        }
    )
    print("\n=== Market State ===")
    pprint(
        {
            "playbook": market.playbook,
            "risk_mode": market.risk_mode,
            "confidence": market.confidence,
            "trend": market.trend,
            "ma20_structure": market.ma20_structure,
            "n_pattern": market.n_pattern,
            "breakout_quality": market.breakout_quality,
            "pullback_quality": market.pullback_quality,
            "features": getattr(market, "features", None),
        }
    )
    print("\n=== ORB Long ===")
    pprint(
        {
            "action": long_signal.action,
            "score": long_signal.score,
            "confidence": long_signal.confidence,
            "entries": long_signal.entries,
            "stop_loss": long_signal.stop_loss,
            "take_profits": long_signal.take_profits,
            "reasons": long_signal.reasons,
            "risk_notes": long_signal.risk_notes,
        }
    )
    print("\n=== ORB Short ===")
    pprint(
        {
            "action": short_signal.action,
            "score": short_signal.score,
            "confidence": short_signal.confidence,
            "entries": short_signal.entries,
            "stop_loss": short_signal.stop_loss,
            "take_profits": short_signal.take_profits,
            "reasons": short_signal.reasons,
            "risk_notes": short_signal.risk_notes,
        }
    )
    print("\n=== Router Live ===")
    pprint(
        {
            "decision": None
            if live_decision is None
            else {
                "strategy": live_decision.strategy,
                "regime": live_decision.regime,
                "risk_mode": live_decision.risk_mode,
                "market_playbook": live_decision.market_playbook,
                "allocator_state": live_decision.allocator_state,
                "allocator_profile": live_decision.allocator_profile,
                "allocator_scale": live_decision.allocator_scale,
                "signal_action": live_decision.signal.action,
                "signal_score": live_decision.signal.score,
                "signal_reasons": live_decision.signal.reasons,
            },
            "block_reason": block_reason,
        }
    )


if __name__ == "__main__":
    asyncio.run(main())
