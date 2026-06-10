"""Compare rescue/catchup configurations on the live mainnet preset.

Runs the SAME candles through wildcat_v2_adverse_guard with different
catchup/rescue start hours (and fully off) so we can see, on real data,
whether the catch-up "chase the daily target" mode is net positive.

Usage:
    python scripts/compare_rescue.py --days 30
"""
from __future__ import annotations

import argparse
from dataclasses import replace

from scripts.backtest_wildcat_s1s5 import (
    build_features,
    candle_time,
    fetch_wildcat_klines,
    preset_params,
    run_backtest,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ETHUSDC")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--leverage", type=int, default=75)
    args = ap.parse_args()

    print(f"抓取 {args.symbol} 最近 {args.days} 天 1m K 線 ...")
    candles = fetch_wildcat_klines(args.symbol, days=args.days, align_taipei_days=True)
    features = build_features(candles)
    print(f"K 線數：{len(candles)}  範圍：{candle_time(candles[0])} ~ {candle_time(candles[-1])}\n")

    base = preset_params(
        "wildcat_v2_adverse_guard",
        target_daily_usdc=20.0,
        leverage_options=(args.leverage,),
    )

    variants = {
        "rescue 關閉":        replace(base, catchup_enabled=False),
        "18:18 (現 VM)":      replace(base, catchup_enabled=True, catchup_start_hour=18, rescue_hour=18),
        "12:14 (git原值/昨天)": replace(base, catchup_enabled=True, catchup_start_hour=12, rescue_hour=14),
        "全天 00:00":         replace(base, catchup_enabled=True, catchup_start_hour=0, rescue_hour=0),
    }

    rows = []
    for name, params in variants.items():
        r = run_backtest(candles, params, args.symbol, args.days, features=features, include_trades=True)
        rows.append((name, r))

    header = f"{'設定':<22}{'淨PnL':>10}{'MaxDD':>9}{'勝率%':>8}{'筆數':>6}{'PF':>7}"
    print(header)
    print("-" * len(header))
    for name, r in rows:
        pf = r["profit_factor"]
        pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
        print(
            f"{name:<22}{r['net_pnl_usdc']:>10.2f}{r['max_drawdown_usdc']:>9.2f}"
            f"{r['win_rate_pct']:>8.1f}{r['trades']:>6}{pf_s:>7}"
        )

    # rescue/catchup 的淨貢獻 = 相對 "rescue 關閉" baseline 的增量。
    base_r = rows[0][1]
    print("\n— 相對『rescue 關閉』的增量(= catchup/rescue 的淨貢獻) —")
    print(f"{'設定':<22}{'增量PnL':>10}{'增量筆數':>9}{'每筆均益':>10}")
    print("-" * 51)
    for name, r in rows[1:]:
        d_pnl = r["net_pnl_usdc"] - base_r["net_pnl_usdc"]
        d_n = r["trades"] - base_r["trades"]
        per = d_pnl / d_n if d_n else 0.0
        print(f"{name:<22}{d_pnl:>10.2f}{d_n:>9}{per:>10.4f}")


if __name__ == "__main__":
    main()
