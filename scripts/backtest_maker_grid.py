"""CLI for the maker-first grid scalping backtest."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.backtest_signal import _default_maker_fee, _now_iso, _resolve_timerange, _summary_payload, fetch_klines
from src.gridbot.strategy.long_pullback import StrategyConfig
from src.gridbot.strategy.maker_grid import MakerGridConfig, run_maker_grid_backtest


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest ETHUSDC maker grid scalping.")
    parser.add_argument("--symbol", default="ETHUSDC")
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--equity", type=float, default=200.0)
    parser.add_argument("--compounding", action="store_true")
    parser.add_argument("--risk", type=float, default=4.0)
    parser.add_argument("--max-leverage", type=float, default=35.0)
    parser.add_argument("--max-position-margin-pct", type=float, default=35.0)
    parser.add_argument("--maker-fee", type=float, default=None)
    parser.add_argument("--taker-fee", type=float, default=0.0004)
    parser.add_argument("--daily-soft-loss-pct", type=float, default=8.0)
    parser.add_argument("--daily-max-loss-pct", type=float, default=18.0)
    parser.add_argument("--daily-loss-risk-scale", type=float, default=0.55)
    parser.add_argument("--daily-target-stop-pct", type=float, default=3.0)
    parser.add_argument("--keep-trading-after-target", action="store_true")
    parser.add_argument("--cooldown-bars", type=int, default=2)
    parser.add_argument("--loss-cooldown-after", type=int, default=3)
    parser.add_argument("--loss-cooldown-bars", type=int, default=18)
    parser.add_argument("--side", choices=["long", "short", "both"], default="both")
    parser.add_argument("--lookback-bars", type=int, default=72)
    parser.add_argument("--spacing-atr", type=float, default=0.22)
    parser.add_argument("--take-profit-atr", type=float, default=0.30)
    parser.add_argument("--stop-atr", type=float, default=1.10)
    parser.add_argument("--entry-expiry-bars", type=int, default=6)
    parser.add_argument("--max-holding-bars", type=int, default=24)
    parser.add_argument("--min-range-width-atr", type=float, default=1.20)
    parser.add_argument("--max-range-width-atr", type=float, default=7.50)
    parser.add_argument("--max-ema-spread-atr", type=float, default=1.15)
    parser.add_argument("--min-volume-ratio", type=float, default=0.40)
    parser.add_argument("--base-url", default="https://fapi.binance.com")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    start_ms, end_ms = _resolve_timerange(args.days, args.start_date, args.end_date)
    candles = fetch_klines(args.base_url, args.symbol, args.interval, start_ms, end_ms)
    base = StrategyConfig(
        symbol=args.symbol,
        equity_usdc=args.equity,
        compounding_enabled=args.compounding,
        risk_per_trade_pct=args.risk,
        max_effective_leverage=args.max_leverage,
        maker_fee_rate=args.maker_fee if args.maker_fee is not None else _default_maker_fee(args.symbol),
        taker_fee_rate=args.taker_fee,
        daily_soft_loss_pct=args.daily_soft_loss_pct,
        daily_max_loss_pct=args.daily_max_loss_pct,
        daily_loss_risk_scale=args.daily_loss_risk_scale,
        daily_target_stop_pct=args.daily_target_stop_pct,
        stop_trading_after_daily_target=not args.keep_trading_after_target,
        max_position_margin_pct=args.max_position_margin_pct,
        cooldown_bars=args.cooldown_bars,
        max_consecutive_losses_before_cooldown=args.loss_cooldown_after,
        consecutive_loss_cooldown_bars=args.loss_cooldown_bars,
    )
    config = MakerGridConfig(
        base=base,
        side=args.side,
        lookback_bars=args.lookback_bars,
        spacing_atr=args.spacing_atr,
        take_profit_atr=args.take_profit_atr,
        stop_atr=args.stop_atr,
        entry_expiry_bars=args.entry_expiry_bars,
        max_holding_bars=args.max_holding_bars,
        min_range_width_atr=args.min_range_width_atr,
        max_range_width_atr=args.max_range_width_atr,
        max_ema_spread_atr=args.max_ema_spread_atr,
        min_volume_ratio=args.min_volume_ratio,
    )
    summary = run_maker_grid_backtest(candles, config)
    payload = {
        "mode": "maker_grid",
        "generated_at": _now_iso(),
        "summary": _summary_payload(summary),
        "params": summary.params,
    }
    _emit_grid(payload, args.json)


def _emit_grid(payload: dict, emit_json: bool) -> None:
    if emit_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    summary = payload["summary"]
    print(f"mode=maker_grid generated_at={payload['generated_at']}")
    print(
        "summary "
        f"trades={summary['total_trades']} "
        f"pnl={summary['net_pnl_usdc']} "
        f"return={summary['return_pct']}% "
        f"dd={summary['max_drawdown_pct']}% "
        f"avg_day_pct={summary['avg_daily_return_pct']}% "
        f"target_hit={summary['daily_target_4pct_hit_rate_pct']}% "
        f"pf={summary['profit_factor']}"
    )
    print(f"params={payload['params']}")


if __name__ == "__main__":
    main()
