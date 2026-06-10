#!/usr/bin/env python3
"""Sweep trend-coverage strategies for WR > 80%.

Three approaches:
1. S1/S5 with-trend: allow proven mean-reversion signals to fire in trends
   when direction matches (BB lower + RSI oversold + uptrend = buy the dip
   with the wind behind you).
2. S8_TrendSnipe: EMA20 bounce in trends (already tested, WR ~60%, weak).
3. Combinations of the above.

Usage (VM):
    nice -n 10 testnet/.venv/bin/python -m scripts.sweep_trend_pull --days 7
    nice -n 10 testnet/.venv/bin/python -m scripts.sweep_trend_pull --days 30
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
    args = parser.parse_args()

    candles = fetch_wildcat_klines(args.symbol, args.days, align_taipei_days=True)
    base = preset_params("wildcat_v2_adverse_guard")

    guard = {"entry_trend_guard_slope": 0.06}

    variants: list[tuple[str, dict]] = [
        # Baseline: S1+S5 range only
        ("baseline", {
            "enabled_strategies": ("S1_BB_RSI", "S5_Stoch"),
            **guard,
        }),
        # Live preset (S1+S5+S2)
        ("live S1S5S2", {
            **guard,
        }),
        # S1 with-trend only
        ("S1 w/trend", {
            "enabled_strategies": ("S1_BB_RSI", "S5_Stoch"),
            "s1_allow_with_trend": True,
            **guard,
        }),
        # S5 with-trend only
        ("S5 w/trend", {
            "enabled_strategies": ("S1_BB_RSI", "S5_Stoch"),
            "s5_allow_with_trend": True,
            **guard,
        }),
        # Both S1+S5 with-trend
        ("S1S5 w/trend", {
            "enabled_strategies": ("S1_BB_RSI", "S5_Stoch"),
            "s1_allow_with_trend": True,
            "s5_allow_with_trend": True,
            **guard,
        }),
        # S1+S5 with-trend + S2
        ("all w/trend", {
            "s1_allow_with_trend": True,
            "s5_allow_with_trend": True,
            **guard,
        }),
        # S1 with-trend + tighter TP/SL for trend entries
        ("S1 wt tp10", {
            "enabled_strategies": ("S1_BB_RSI", "S5_Stoch"),
            "s1_allow_with_trend": True,
            "s1_tp": 0.0010,
            **guard,
        }),
        ("S1 wt tp8", {
            "enabled_strategies": ("S1_BB_RSI", "S5_Stoch"),
            "s1_allow_with_trend": True,
            "s1_tp": 0.0008,
            **guard,
        }),
        # S1+S5 with-trend, no DCA in trend entries (can't distinguish, so
        # just block S1/S5 DCA entirely when with_trend is on)
        ("S1S5 wt noD", {
            "enabled_strategies": ("S1_BB_RSI", "S5_Stoch"),
            "s1_allow_with_trend": True,
            "s5_allow_with_trend": True,
            "no_dca_strategies": ("S1_BB_RSI", "S5_Stoch"),
            **guard,
        }),
    ]

    print(f"\n{'═'*105}")
    print(f"  趨勢覆蓋掃描  |  {args.symbol}  |  {args.days}d")
    print(f"  S1/S5 順勢進場 — 用現有高 WR 指標覆蓋 trend 段")
    print(f"{'═'*105}\n")

    header = (
        f"{'variant':>14}  {'total':>5}  {'PnL':>8}  {'MaxDD':>7}  "
        f"{'WR%':>6}  {'PF':>6}  {'PnL/DD':>7}  {'avg/trd':>8}  "
        f"{'S1':>4}  {'S5':>4}  {'S2':>4}  {'trend_PnL':>10}"
    )
    print(f"  {header}")
    print(f"  {'─'*103}")

    for label, overrides in variants:
        params = replace(base, **overrides)
        result = run_backtest(candles, params, args.symbol, args.days, include_trades=True)

        all_trades = result.get("all_trades", [])
        by_strat = {}
        for t in all_trades:
            s = t["strategy"]
            by_strat.setdefault(s, []).append(t)

        s1_count = len(by_strat.get("S1_BB_RSI", []))
        s5_count = len(by_strat.get("S5_Stoch", []))
        s2_count = len(by_strat.get("S2_SuperTrend", []))

        # "trend PnL" = PnL from trades entered during up/down (not range)
        trend_pnl = sum(t["pnl"] for t in all_trades if t.get("trend", "range") != "range")

        total_pnl = result.get("net_pnl_usdc", 0)
        max_dd = result.get("max_drawdown_usdc", 0)
        total_trades_count = result.get("trades", 0)
        wr = result.get("win_rate_pct", 0)
        pf = result.get("profit_factor", 0)
        pnl_dd = total_pnl / max_dd if max_dd > 0 else 999
        avg = total_pnl / total_trades_count if total_trades_count else 0.0

        print(
            f"  {label:>14}  {total_trades_count:>5}  {total_pnl:>+8.1f}  {max_dd:>7.1f}  "
            f"{wr:>5.1f}%  {pf:>6.2f}  {pnl_dd:>7.1f}  {avg:>+8.3f}  "
            f"{s1_count:>4}  {s5_count:>4}  {s2_count:>4}  {trend_pnl:>+10.1f}"
        )

    print(f"\n{'═'*105}")
    print("  w/trend = S1/S5 允許在趨勢中順勢做單 | noD = 禁 DCA | tp = S1 TP 調整")
    print(f"{'═'*105}\n")


if __name__ == "__main__":
    main()
