"""CLI for opening-range fakeout reversal backtests."""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.backtest_signal import _default_maker_fee, _now_iso, _resolve_timerange, _summary_payload, fetch_klines
from src.gridbot.strategy.fakeout_reversal import FakeoutReversalConfig, run_fakeout_reversal_backtest
from src.gridbot.strategy.long_orb import OrbConfig
from src.gridbot.strategy.long_pullback import StrategyConfig


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest opening-range fakeout reversal strategy.")
    parser.add_argument("--symbol", default="ETHUSDC")
    parser.add_argument("--interval", default="5m")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--equity", type=float, default=200.0)
    parser.add_argument("--compounding", action="store_true")
    parser.add_argument("--risk", type=float, default=4.0)
    parser.add_argument("--min-score", type=int, default=58)
    parser.add_argument("--max-leverage", type=float, default=35.0)
    parser.add_argument("--max-position-margin-pct", type=float, default=35.0)
    parser.add_argument("--maker-fee", type=float, default=None)
    parser.add_argument("--taker-fee", type=float, default=0.0004)
    parser.add_argument("--daily-soft-loss-pct", type=float, default=8.0)
    parser.add_argument("--daily-max-loss-pct", type=float, default=18.0)
    parser.add_argument("--daily-loss-risk-scale", type=float, default=0.55)
    parser.add_argument("--daily-target-stop-pct", type=float, default=3.0)
    parser.add_argument("--keep-trading-after-target", action="store_true")
    parser.add_argument("--cooldown-bars", type=int, default=4)
    parser.add_argument("--loss-cooldown-after", type=int, default=3)
    parser.add_argument("--loss-cooldown-bars", type=int, default=18)
    parser.add_argument("--max-holding-bars", type=int, default=24)
    parser.add_argument("--take-profit-r", default="0.45,0.9,1.6")
    parser.add_argument("--exit-weights", default="0.35,0.35,0.30")
    parser.add_argument("--orb-session-start-bar", type=int, default=0)
    parser.add_argument("--orb-opening-range-bars", type=int, default=9)
    parser.add_argument("--orb-min-volume-ratio", type=float, default=0.65)
    parser.add_argument("--orb-stop-atr", type=float, default=0.9)
    parser.add_argument("--side", choices=["both", "long", "short"], default="both")
    parser.add_argument("--min-probe-atr", type=float, default=0.10)
    parser.add_argument("--max-close-outside-atr", type=float, default=0.03)
    parser.add_argument("--min-wick-ratio", type=float, default=0.38)
    parser.add_argument("--min-volume-ratio", type=float, default=0.65)
    parser.add_argument("--min-orb-width-atr", type=float, default=0.45)
    parser.add_argument("--max-orb-width-atr", type=float, default=4.5)
    parser.add_argument("--allow-strong-trend", action="store_true")
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
        max_holding_bars=args.max_holding_bars,
        cooldown_bars=args.cooldown_bars,
        max_consecutive_losses_before_cooldown=args.loss_cooldown_after,
        consecutive_loss_cooldown_bars=args.loss_cooldown_bars,
        take_profit_r=_parse_float_tuple(args.take_profit_r),
        exit_weights=_parse_float_tuple(args.exit_weights),
        min_score=args.min_score,
    )
    orb = OrbConfig(
        base=base,
        session_start_bar=args.orb_session_start_bar,
        opening_range_bars=args.orb_opening_range_bars,
        min_volume_ratio=args.orb_min_volume_ratio,
        stop_atr=args.orb_stop_atr,
    )
    config = FakeoutReversalConfig(
        orb=orb,
        side=args.side,
        min_probe_atr=args.min_probe_atr,
        max_close_outside_atr=args.max_close_outside_atr,
        min_wick_ratio=args.min_wick_ratio,
        min_volume_ratio=args.min_volume_ratio,
        min_orb_width_atr=args.min_orb_width_atr,
        max_orb_width_atr=args.max_orb_width_atr,
        reject_strong_trend=not args.allow_strong_trend,
    )
    summary = run_fakeout_reversal_backtest(candles, config)
    payload = {
        "mode": "fakeout_reversal",
        "generated_at": _now_iso(),
        "summary": _summary_payload(summary),
        "params": summary.params,
    }
    _emit_fakeout(payload, args.json)


def _parse_float_tuple(value: str) -> tuple[float, float, float]:
    parts = tuple(float(part.strip()) for part in value.split(",") if part.strip())
    if len(parts) != 3:
        raise ValueError("expected three comma-separated floats")
    return parts


def _emit_fakeout(payload: dict, emit_json: bool) -> None:
    if emit_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    summary = payload["summary"]
    print(f"mode=fakeout_reversal generated_at={payload['generated_at']}")
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
