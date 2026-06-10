#!/usr/bin/env python3
"""Sweep entry-quality filters (A/B/C) against the live preset.

Compares the live mean-reversion preset (wildcat_v2_adverse_guard) under:
  baseline   no entry guard (what the backtest currently models)
  live_0.06  entry_trend_guard_slope=0.06 (current live bot)
  A_0.04     slope guard tightened to 0.04
  A_0.03     slope guard tightened to 0.03
  B_K        slope 0.06 + deep-EMA50 guard (block entries > K*ATR from EMA50)
  C          slope 0.06 + confirmation candle (enter only on a same-direction bar)
  B1.0+C     combined

Goal: cut the falling-knife losses (e.g. the two fast SL runs in the
2026-06-09 morning loop) without killing win rate / total PnL.

Usage (on VM, from repo root, one at a time + nice to respect 964MB RAM):
    nice -n 10 testnet/.venv/bin/python -m scripts.sweep_entry_filter --days 7
    nice -n 10 testnet/.venv/bin/python -m scripts.sweep_entry_filter --days 30
"""

from __future__ import annotations

import argparse
from dataclasses import replace

from scripts.backtest_wildcat_s1s5 import (
    fetch_wildcat_klines,
    preset_params,
    run_backtest,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--symbol", type=str, default="ETHUSDC")
    parser.add_argument("--preset", type=str, default="wildcat_v2_adverse_guard")
    args = parser.parse_args()

    candles = fetch_wildcat_klines(args.symbol, args.days, align_taipei_days=True)
    base = preset_params(args.preset)

    # The backtest baseline preset has no entry trend guard; live runs 0.06.
    variants: list[tuple[str, dict]] = [
        ("baseline", {}),
        ("live_0.06", {"entry_trend_guard_slope": 0.06}),
        ("A_0.04", {"entry_trend_guard_slope": 0.04}),
        ("A_0.03", {"entry_trend_guard_slope": 0.03}),
        ("B_0.8", {"entry_trend_guard_slope": 0.06, "entry_ema50_dist_atr": 0.8}),
        ("B_1.0", {"entry_trend_guard_slope": 0.06, "entry_ema50_dist_atr": 1.0}),
        ("B_1.2", {"entry_trend_guard_slope": 0.06, "entry_ema50_dist_atr": 1.2}),
        ("C", {"entry_trend_guard_slope": 0.06, "entry_confirm_candle": True}),
        (
            "B1.0+C",
            {
                "entry_trend_guard_slope": 0.06,
                "entry_ema50_dist_atr": 1.0,
                "entry_confirm_candle": True,
            },
        ),
    ]

    print(f"\n{'═'*86}")
    print(f"  進場過濾掃描  |  {args.symbol}  |  {args.days}d  |  preset: {args.preset}")
    print(f"{'═'*86}\n")

    header = (
        f"{'variant':>10}  {'trades':>6}  {'PnL':>8}  {'MaxDD':>7}  "
        f"{'WR%':>6}  {'PF':>6}  {'PnL/DD':>7}  {'avg/trd':>8}"
    )
    print(f"  {header}")
    print(f"  {'─'*84}")

    for label, overrides in variants:
        params = replace(base, **overrides)
        result = run_backtest(candles, params, args.symbol, args.days)

        total_pnl = result.get("net_pnl_usdc", 0)
        max_dd = result.get("max_drawdown_usdc", 0)
        total_trades = result.get("trades", 0)
        wr = result.get("win_rate_pct", 0)
        pf = result.get("profit_factor", 0)
        pnl_dd = total_pnl / max_dd if max_dd > 0 else 999
        avg = total_pnl / total_trades if total_trades else 0.0

        print(
            f"  {label:>10}  {total_trades:>6}  {total_pnl:>+8.1f}  {max_dd:>7.1f}  "
            f"{wr:>5.1f}%  {pf:>6.2f}  {pnl_dd:>7.1f}  {avg:>+8.3f}"
        )

    print(f"\n{'═'*86}")
    print("  baseline=回測無進場守門 | live_0.06=現行 live | A=收緊 slope | B=深跌破EMA50 | C=確認K棒")
    print(f"{'═'*86}\n")


if __name__ == "__main__":
    main()
