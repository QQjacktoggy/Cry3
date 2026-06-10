#!/usr/bin/env python3
"""
Compare a completed mainnet loop against backtest simulation on the same candles.

Usage (run from repo root on VM):
    python -m scripts.compare_live_vs_backtest              # last 24h
    python -m scripts.compare_live_vs_backtest --days 1
    python -m scripts.compare_live_vs_backtest --since "2026-06-09 10:00" --until "2026-06-09 22:00"
    python -m scripts.compare_live_vs_backtest --days 0.5 --preset wildcat_v2_adverse_guard
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.backtest_wildcat_s1s5 import (
    build_features,
    candle_time,
    preset_params,
    run_backtest,
)
from scripts.backtest_maker_scalp import fetch_1m_klines

TAIPEI = timezone(timedelta(hours=8))
DB_PATH = Path("/home/jack_shih/testnet/data/gridbot_testnet.db")
SYMBOL = "ETHUSDC"
WARMUP_BARS = 150   # extra margin on top of the 130 run_backtest needs
MATCH_WINDOW_S = 600  # ±10 min to match live entry → backtest entry


# ──────────────────────────────────────────────────────────────────────────────
# DB helpers (sync sqlite3 — no async needed for a CLI script)
# ──────────────────────────────────────────────────────────────────────────────

def load_live_runs(db_path: Path, since_ms: int, until_ms: int) -> list[dict]:
    """Return completed mainnet_runs with entry_filled event time merged in."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT
            r.run_id,
            r.symbol,
            r.strategy_label,
            r.status,
            r.side,
            r.entry_price,
            r.avg_entry_price,
            r.qty,
            r.realized_pnl_usdc,
            r.commission_usdc,
            r.exit_reason,
            r.armed_at_ms,
            r.completed_at_ms,
            r.signal_json,
            MIN(e.event_time_ms) AS entry_filled_ms,
            json_extract(MIN(e.details_json), '$.price') AS fill_price_str
        FROM mainnet_runs r
        LEFT JOIN mainnet_run_events e
            ON e.run_id = r.run_id AND e.event_type = 'entry_filled'
        WHERE r.armed_at_ms BETWEEN ? AND ?
        GROUP BY r.run_id
        ORDER BY r.armed_at_ms
        """,
        (since_ms, until_ms),
    ).fetchall()
    conn.close()

    result = []
    for row in rows:
        d = dict(row)
        # Parse signal_json for rescue/catchup flag
        try:
            sig = json.loads(d.get("signal_json") or "{}")
            d["_rescue"] = sig.get("rescue", False) or sig.get("catchup", False)
            d["_signal_side"] = sig.get("side", d.get("side", "?"))
        except Exception:
            d["_rescue"] = False
            d["_signal_side"] = d.get("side", "?")
        result.append(d)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ──────────────────────────────────────────────────────────────────────────────

def ts_str(ms: int | None) -> str:
    if ms is None:
        return "  —   "
    return datetime.fromtimestamp(ms / 1000, tz=TAIPEI).strftime("%H:%M")


def pnl_str(pnl: float | None) -> str:
    if pnl is None:
        return "   —   "
    sign = "+" if pnl >= 0 else ""
    return f"{sign}{pnl:.3f}"


def side_icon(side: str) -> str:
    return "▲ L" if side == "LONG" else "▼ S"


# ──────────────────────────────────────────────────────────────────────────────
# Matching logic
# ──────────────────────────────────────────────────────────────────────────────

def match_runs_to_backtest(
    live_runs: list[dict],
    bt_trades: list[dict],
    window_s: int = MATCH_WINDOW_S,
) -> tuple[list[tuple], list[dict], list[dict]]:
    """
    Returns:
        matched  : list of (live_run, bt_trade) pairs
        live_only: live runs with no matching backtest trade
        bt_only  : backtest trades with no matching live run
    """
    used_bt: set[int] = set()

    matched: list[tuple] = []
    live_only: list[dict] = []

    for run in live_runs:
        entry_ms = run.get("entry_filled_ms")
        if entry_ms is None:
            live_only.append(run)
            continue

        entry_s = entry_ms / 1000
        side = run.get("side") or run.get("_signal_side", "")

        best_idx: int | None = None
        best_diff: float = float("inf")

        for idx, bt in enumerate(bt_trades):
            if idx in used_bt:
                continue
            if bt.get("side") != side:
                continue
            bt_entry_s = datetime.fromisoformat(bt["entry_time"]).timestamp()
            diff = abs(bt_entry_s - entry_s)
            if diff <= window_s and diff < best_diff:
                best_diff = diff
                best_idx = idx

        if best_idx is not None:
            used_bt.add(best_idx)
            matched.append((run, bt_trades[best_idx]))
        else:
            live_only.append(run)

    bt_only = [bt for idx, bt in enumerate(bt_trades) if idx not in used_bt]
    return matched, live_only, bt_only


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Compare live mainnet runs vs backtest")
    parser.add_argument("--days", type=float, default=1.0, help="Look-back window in days (default 1)")
    parser.add_argument("--since", type=str, default=None, help="Start time UTC+8 'YYYY-MM-DD HH:MM'")
    parser.add_argument("--until", type=str, default=None, help="End time UTC+8 'YYYY-MM-DD HH:MM'")
    parser.add_argument("--symbol", type=str, default=SYMBOL)
    parser.add_argument("--preset", type=str, default="wildcat_v2_adverse_guard")
    parser.add_argument("--db", type=str, default=str(DB_PATH))
    parser.add_argument("--match-window", type=int, default=MATCH_WINDOW_S,
                        help="Seconds window for matching live→backtest entry (default 600)")
    args = parser.parse_args()

    # Time window
    now = datetime.now(timezone.utc)
    if args.until:
        until_dt = datetime.strptime(args.until, "%Y-%m-%d %H:%M").replace(tzinfo=TAIPEI).astimezone(timezone.utc)
    else:
        until_dt = now
    if args.since:
        since_dt = datetime.strptime(args.since, "%Y-%m-%d %H:%M").replace(tzinfo=TAIPEI).astimezone(timezone.utc)
    else:
        since_dt = until_dt - timedelta(days=args.days)

    since_ms = int(since_dt.timestamp() * 1000)
    until_ms = int(until_dt.timestamp() * 1000)

    since_str = since_dt.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M")
    until_str = until_dt.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M")

    print(f"\n{'═'*66}")
    print(f"  LIVE vs BACKTEST 比對  |  {args.symbol}  |  {since_str} → {until_str} UTC+8")
    print(f"  Preset: {args.preset}")
    print(f"{'═'*66}\n")

    # ── 1. Live runs ──────────────────────────────────────────────────────────
    db_path = Path(args.db)
    live_runs = load_live_runs(db_path, since_ms, until_ms)
    print(f"  DB: {db_path}")
    print(f"  Live runs loaded: {len(live_runs)}")

    # ── 2. Candles (with warmup) ───────────────────────────────────────────────
    warmup_start = since_dt - timedelta(minutes=WARMUP_BARS)
    print(f"  Fetching 1m candles from {warmup_start.astimezone(TAIPEI).strftime('%H:%M')} UTC+8 (includes {WARMUP_BARS}m warmup)...")
    candles = fetch_1m_klines(args.symbol, days=0, start_dt=warmup_start, end_dt=until_dt)
    print(f"  Candles fetched: {len(candles)}")

    # Filter candles to window for display
    since_iso = since_dt.isoformat()
    until_iso = until_dt.isoformat()

    # ── 3. Backtest ────────────────────────────────────────────────────────────
    params = preset_params(args.preset)
    days_float = (until_dt - since_dt).total_seconds() / 86400
    features = build_features(candles)
    bt_result = run_backtest(candles, params, args.symbol, days=max(days_float, 0.1), features=features, include_trades=True)
    bt_all = bt_result.get("all_trades", [])

    # Filter to window
    bt_trades = [
        t for t in bt_all
        if since_iso <= t["entry_time"] <= until_iso
    ]
    print(f"  Backtest trades in window: {len(bt_trades)}\n")

    # ── 4. Match ───────────────────────────────────────────────────────────────
    # Only consider live runs that got an entry fill (have entry_filled_ms)
    live_entered = [r for r in live_runs if r.get("entry_filled_ms")]
    live_no_entry = [r for r in live_runs if not r.get("entry_filled_ms")]

    matched, live_only, bt_only = match_runs_to_backtest(live_entered, bt_trades, window_s=args.match_window)

    # ── 5. Print tables ────────────────────────────────────────────────────────
    W = 66
    HDR = f"{'#':>3}  {'進場':^5}  {'策略':<12}  {'方':^3}  {'Live入':>8}  {'BT入':>8}  {'LivePnL':>8}  {'BT PnL':>8}  {'L出因':<12}  {'BT出因'}"

    # ── MATCHED ───────────────────────────────────────────────────────────────
    print(f"{'─'*W}")
    print(f"  ✅ MATCHED（live + backtest 均有進場）  [{len(matched)} 筆]")
    print(f"{'─'*W}")
    if matched:
        print(f"  {HDR}")
        for n, (run, bt) in enumerate(matched, 1):
            live_entry_t = ts_str(run.get("entry_filled_ms"))
            bt_entry_t = datetime.fromisoformat(bt["entry_time"]).astimezone(TAIPEI).strftime("%H:%M")
            live_pnl = run.get("realized_pnl_usdc")
            bt_pnl = bt.get("pnl")
            strat = run.get("strategy_label") or bt.get("strategy", "?")
            side = run.get("side") or "?"
            live_price = f'{run.get("avg_entry_price") or run.get("entry_price") or 0:.1f}'
            bt_price = f'{bt.get("entry_price", 0):.1f}'
            live_reason = (run.get("exit_reason") or "?")[:12]
            bt_reason = (bt.get("exit_reason") or "?")[:12]
            print(
                f"  {n:>3}  {live_entry_t}  {strat:<12}  {side_icon(side)}  "
                f"{live_price:>8}  {bt_price:>8}  {pnl_str(live_pnl):>8}  {pnl_str(bt_pnl):>8}  "
                f"{live_reason:<12}  {bt_reason}"
            )
    else:
        print("  （無）")

    # ── LIVE ONLY ─────────────────────────────────────────────────────────────
    print(f"\n{'─'*W}")
    print(f"  🟡 LIVE ONLY（live 有進場，backtest 無對應訊號）  [{len(live_only)} 筆]")
    print(f"{'─'*W}")
    if live_only:
        print(f"  {'#':>3}  {'進場':^5}  {'策略':<12}  {'方':^3}  {'Live入':>8}  {'LivePnL':>8}  {'出因':<12}  {'備註'}")
        for n, run in enumerate(live_only, 1):
            live_entry_t = ts_str(run.get("entry_filled_ms"))
            strat = run.get("strategy_label") or "?"
            side = run.get("side") or "?"
            live_price = f'{run.get("avg_entry_price") or run.get("entry_price") or 0:.1f}'
            live_pnl = run.get("realized_pnl_usdc")
            live_reason = (run.get("exit_reason") or "?")[:12]
            note = "rescue/catchup" if run.get("_rescue") else ""
            print(
                f"  {n:>3}  {live_entry_t}  {strat:<12}  {side_icon(side)}  "
                f"{live_price:>8}  {pnl_str(live_pnl):>8}  {live_reason:<12}  {note}"
            )
    else:
        print("  （無）")

    # ── NO ENTRY (live armed but never filled) ────────────────────────────────
    if live_no_entry:
        print(f"\n{'─'*W}")
        print(f"  ⚪ LIVE ARM 但未成交（signal_timeout / entry_ttl / GTX 全拒）  [{len(live_no_entry)} 筆]")
        print(f"{'─'*W}")
        print(f"  {'#':>3}  {'Armed':^5}  {'策略':<12}  {'狀態':<16}  {'出因'}")
        for n, run in enumerate(live_no_entry, 1):
            armed_t = ts_str(run.get("armed_at_ms"))
            strat = run.get("strategy_label") or "?"
            status = run.get("status") or "?"
            reason = run.get("exit_reason") or "—"
            print(f"  {n:>3}  {armed_t}  {strat:<12}  {status:<16}  {reason}")

    # ── BACKTEST ONLY ─────────────────────────────────────────────────────────
    print(f"\n{'─'*W}")
    print(f"  🔵 BACKTEST ONLY（backtest 有訊號，live 未進場）  [{len(bt_only)} 筆]")
    print(f"{'─'*W}")
    if bt_only:
        print(f"  {'#':>3}  {'進場':^5}  {'策略':<12}  {'方':^3}  {'BT入':>8}  {'BT PnL':>8}  {'BT出因'}")
        for n, bt in enumerate(bt_only, 1):
            bt_entry_t = datetime.fromisoformat(bt["entry_time"]).astimezone(TAIPEI).strftime("%H:%M")
            strat = bt.get("strategy") or "?"
            side = bt.get("side") or "?"
            bt_price = f'{bt.get("entry_price", 0):.1f}'
            bt_pnl = bt.get("pnl")
            bt_reason = (bt.get("exit_reason") or "?")[:12]
            print(
                f"  {n:>3}  {bt_entry_t}  {strat:<12}  {side_icon(side)}  "
                f"{bt_price:>8}  {pnl_str(bt_pnl):>8}  {bt_reason}"
            )
    else:
        print("  （無）")

    # ── SUMMARY ───────────────────────────────────────────────────────────────
    print(f"\n{'═'*W}")
    print(f"  SUMMARY")
    print(f"{'─'*W}")

    live_pnls = [r.get("realized_pnl_usdc") or 0.0 for r in live_entered]
    bt_pnls_matched = [bt.get("pnl", 0.0) for _, bt in matched]
    bt_pnls_all = [t.get("pnl", 0.0) for t in bt_trades]

    live_wr = sum(1 for p in live_pnls if p > 0) / max(len(live_pnls), 1) * 100
    bt_wr_window = sum(1 for p in bt_pnls_all if p > 0) / max(len(bt_pnls_all), 1) * 100

    print(f"  {'':30}  {'Live':>10}  {'Backtest':>10}")
    print(f"  {'有進場 runs':<30}  {len(live_entered):>10}  {len(bt_trades):>10}")
    print(f"  {'Win Rate':<30}  {live_wr:>9.1f}%  {bt_wr_window:>9.1f}%")
    print(f"  {'Total PnL (USDC)':<30}  {pnl_str(sum(live_pnls)):>10}  {pnl_str(sum(bt_pnls_all)):>10}")
    if live_entered:
        print(f"  {'Avg PnL/trade':<30}  {pnl_str(sum(live_pnls)/len(live_pnls)):>10}  {pnl_str(sum(bt_pnls_all)/max(len(bt_pnls_all),1)):>10}")
    print(f"  {'─'*46}")
    print(f"  {'Matched 筆數':<30}  {len(matched):>10}")
    print(f"  {'Live-only（含 rescue）':<30}  {len(live_only):>10}")
    print(f"  {'Live arm 未成交':<30}  {len(live_no_entry):>10}")
    print(f"  {'BT-only（live 漏掉）':<30}  {len(bt_only):>10}")

    if matched:
        pnl_deltas = [(r.get("realized_pnl_usdc") or 0) - (bt.get("pnl") or 0) for r, bt in matched]
        avg_delta = sum(pnl_deltas) / len(pnl_deltas)
        print(f"  {'Matched avg (Live−BT) PnL diff':<30}  {pnl_str(avg_delta):>10}")

    # Exit reason breakdown
    live_reasons: dict[str, int] = {}
    for r in live_entered:
        k = r.get("exit_reason") or "?"
        live_reasons[k] = live_reasons.get(k, 0) + 1
    bt_reasons: dict[str, int] = {}
    for t in bt_trades:
        k = t.get("exit_reason") or "?"
        bt_reasons[k] = bt_reasons.get(k, 0) + 1

    all_reasons = sorted(set(live_reasons) | set(bt_reasons))
    print(f"\n  {'出場原因分布':<30}  {'Live':>10}  {'Backtest':>10}")
    for r in all_reasons:
        lc = live_reasons.get(r, 0)
        bc = bt_reasons.get(r, 0)
        print(f"  {'  ' + r:<30}  {lc:>10}  {bc:>10}")

    print(f"\n{'═'*W}\n")


if __name__ == "__main__":
    main()
