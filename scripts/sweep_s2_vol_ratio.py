#!/usr/bin/env python3
"""
Sweep S2 vol_ratio threshold to find optimal value.

Usage (on VM, from repo root):
    python -m scripts.sweep_s2_vol_ratio --days 30
    python -m scripts.sweep_s2_vol_ratio --days 7
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
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--symbol", type=str, default="ETHUSDC")
    parser.add_argument("--preset", type=str, default="wildcat_v2_adverse_guard")
    args = parser.parse_args()

    candles = fetch_wildcat_klines(args.symbol, args.days, align_taipei_days=True)
    base = preset_params(args.preset)

    # Baseline: S2 uses shared min_vol_ratio (s2_min_vol_ratio=0 → fallback)
    vol_ratios = [0.0, 0.05, 0.08, 0.10, 0.12, 0.15, 0.18]
    # 0.0 = use shared min_vol_ratio (0.22 in the preset)

    print(f"\n{'═'*80}")
    print(f"  S2 vol_ratio 門檻掃描  |  {args.symbol}  |  {args.days}d  |  preset: {args.preset}")
    print(f"  共用 min_vol_ratio = {base.min_vol_ratio}")
    print(f"{'═'*80}\n")

    header = (
        f"{'s2_vol':>8}  {'trades':>6}  {'S2_cnt':>6}  {'PnL':>8}  "
        f"{'MaxDD':>7}  {'WR%':>6}  {'PF':>6}  {'PnL/DD':>7}  {'S2_PnL':>8}  {'S2_WR%':>6}"
    )
    print(f"  {header}")
    print(f"  {'─'*90}")

    for vr in vol_ratios:
        params = replace(base, s2_min_vol_ratio=vr)
        label = f"{vr:.2f}" if vr > 0 else f"shared({base.min_vol_ratio:.2f})"
        result = run_backtest(candles, params, args.symbol, args.days, include_trades=True)
        all_trades = result.get("all_trades", [])

        s2_trades = [t for t in all_trades if t["strategy"] == "S2_SuperTrend"]
        s2_pnl = sum(t["pnl"] for t in s2_trades)
        s2_wins = sum(1 for t in s2_trades if t["pnl"] > 0)
        s2_wr = s2_wins / max(len(s2_trades), 1) * 100

        total_pnl = result.get("total_pnl", 0)
        max_dd = result.get("max_drawdown", 0)
        total_trades = result.get("total_trades", 0)
        wr = result.get("win_rate", 0) * 100 if result.get("win_rate", 0) <= 1 else result.get("win_rate", 0)
        wins = result.get("wins", 0)
        losses_val = result.get("gross_loss", 0)
        pf = result.get("profit_factor", 0)
        pnl_dd = total_pnl / max_dd if max_dd > 0 else 999

        print(
            f"  {label:>8}  {total_trades:>6}  {len(s2_trades):>6}  "
            f"{total_pnl:>+8.1f}  {max_dd:>7.1f}  {wr:>5.1f}%  {pf:>6.2f}  "
            f"{pnl_dd:>7.1f}  {s2_pnl:>+8.2f}  {s2_wr:>5.1f}%"
        )

    print(f"\n{'═'*80}")
    print("  s2_vol=0 → 用共用 min_vol_ratio (baseline)")
    print("  降低 s2_vol → S2 在低量時段也能出手（更多交易、但可能品質降）")
    print(f"{'═'*80}\n")


if __name__ == "__main__":
    main()
