#!/usr/bin/env python3
"""
逐筆 review mainnet runs：進場 / DCA / TP 成交 / 出場
Usage (on VM, from repo root):
    python -m scripts.review_runs           # last 10 completed runs
    python -m scripts.review_runs --n 20
    python -m scripts.review_runs --hours 6
    python -m scripts.review_runs --loop    # only show loop-chained runs (latest loop)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

TAIPEI = timezone(timedelta(hours=8))
DB_PATH = Path("/home/jack_shih/testnet/data/gridbot_testnet.db")


def ts(ms: int | None, fmt: str = "%m/%d %H:%M:%S") -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(ms / 1000, tz=TAIPEI).strftime(fmt)


def pnl(v: float | None) -> str:
    if v is None:
        return "    —   "
    s = "+" if v >= 0 else ""
    return f"{s}{v:.4f}"


def price(v: float | None) -> str:
    return f"{v:.2f}" if v else "—"


def load_runs(db: sqlite3.Connection, n: int, since_ms: int) -> list[dict]:
    rows = db.execute(
        """
        SELECT * FROM mainnet_runs
        WHERE armed_at_ms >= ?
        ORDER BY armed_at_ms DESC
        LIMIT ?
        """,
        (since_ms, n),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


def load_events(db: sqlite3.Connection, run_id: str) -> list[dict]:
    rows = db.execute(
        """
        SELECT event_time_ms, event_type, details_json
        FROM mainnet_run_events
        WHERE run_id = ?
        ORDER BY event_time_ms
        """,
        (run_id,),
    ).fetchall()
    result = []
    for r in rows:
        d = json.loads(r["details_json"] or "{}")
        result.append({"ms": r["event_time_ms"], "type": r["event_type"], **d})
    return result


def summarize_run(run: dict, events: list[dict]) -> None:
    run_id = run["run_id"]
    short_id = run_id[-10:]
    strategy = run.get("strategy_label") or "?"
    side = run.get("side") or "?"
    status = run.get("status") or "?"
    exit_reason = run.get("exit_reason") or "—"
    realized = run.get("realized_pnl_usdc")
    commission = run.get("commission_usdc") or 0.0
    armed_ms = run.get("armed_at_ms")
    completed_ms = run.get("completed_at_ms")

    # Collect key events
    entry_ev = next((e for e in events if e["type"] == "entry_filled"), None)
    dca_placed = [e for e in events if e["type"] == "recovery_entry_placed"]
    dca_filled = [e for e in events if e["type"] == "recovery_entry_filled"]
    tp_syncs = [e for e in events if e["type"] == "take_profit_synced"]
    partial_exit_ev = next((e for e in events if e["type"] == "partial_exit"), None)
    mid_exit_ev = next((e for e in events if e["type"] == "mid_exit"), None)
    sl_placed_ev = next((e for e in events if e["type"] == "sl_stop_market_placed"), None)
    trail_armed_ev = next((e for e in events if e["type"] == "trail_maker_order_placed"), None)
    trail_filled_ev = next((e for e in events if e["type"] == "trail_maker_filled"), None)
    trail_fallback_ev = next((e for e in events if e["type"] == "trail_maker_timeout_fallback_market"), None)
    dca_guard_blocked = [e for e in events if e["type"] in ("dca_blocked_guard_cooldown", "dca_blocked_partial_exit")]

    side_icon = "▲ L" if side == "LONG" else "▼ S"
    hold_s = ((completed_ms or 0) - (entry_ev["ms"] if entry_ev else armed_ms or 0)) / 1000 if (completed_ms and (entry_ev or armed_ms)) else 0
    hold_str = f"{int(hold_s//60)}m{int(hold_s%60):02d}s" if hold_s > 0 else "—"

    # ── Header ────────────────────────────────────────────────────────────────
    pnl_str = pnl(realized)
    fee_str = f"-{commission:.4f}" if commission else ""
    print(f"\n  ┌─ {short_id}  {strategy:<12} {side_icon}  armed={ts(armed_ms,'%H:%M:%S')}  hold={hold_str}  PnL={pnl_str} fee={fee_str}  [{exit_reason}]")

    # ── Entry ──────────────────────────────────────────────────────────────────
    if entry_ev:
        ep = entry_ev.get("price") or run.get("entry_price")
        eq = entry_ev.get("qty") or run.get("qty")
        print(f"  │  ENTRY   {ts(entry_ev['ms'],'%H:%M:%S')}  price={price(ep)}  qty={eq}")
    else:
        # No entry fill: show why
        print(f"  │  ENTRY   —  status={status}")

    # ── DCA ───────────────────────────────────────────────────────────────────
    for i, (placed, filled) in enumerate(zip(dca_placed, dca_filled + [None] * len(dca_placed)), 1):
        dca_limit = price(placed.get("price") or placed.get("limit_price"))
        if filled:
            new_avg = price(filled.get("avg_price"))
            new_qty = filled.get("qty")
            added = filled.get("added_qty")
            print(f"  │  DCA #{i}  {ts(filled['ms'],'%H:%M:%S')}  fill=avg_entry→{new_avg}  added={added}  total_qty={new_qty}  (limit was {dca_limit})")
        else:
            print(f"  │  DCA #{i}  {ts(placed['ms'],'%H:%M:%S')}  PLACED at {dca_limit}  (not yet filled)")
    for e in dca_guard_blocked:
        reason = "partial_exit" if e["type"] == "dca_blocked_partial_exit" else "guard_cooldown"
        print(f"  │  DCA blocked ({reason})  {ts(e['ms'],'%H:%M:%S')}")

    # ── TP snapshot: first vs last sync ───────────────────────────────────────
    if tp_syncs:
        def fmt_orders(ev: dict) -> str:
            orders = ev.get("orders", [])
            parts = []
            for o in orders:
                cid = str(o.get("client_order_id", "")).split("_")[-1]
                parts.append(f"{cid}@{price(o.get('price'))}×{o.get('qty')}")
            return "  ".join(parts)

        first_sync = tp_syncs[0]
        last_sync = tp_syncs[-1]
        print(f"  │  TP[1st] {ts(first_sync['ms'],'%H:%M:%S')}  {fmt_orders(first_sync)}")
        if len(tp_syncs) > 1:
            print(f"  │  TP[lst] {ts(last_sync['ms'],'%H:%M:%S')}  {fmt_orders(last_sync)}  ({len(tp_syncs)} syncs total)")

    # ── SL placed ─────────────────────────────────────────────────────────────
    if sl_placed_ev:
        sl_p = price(sl_placed_ev.get("stop_price") or sl_placed_ev.get("sl_price"))
        print(f"  │  SL      {ts(sl_placed_ev['ms'],'%H:%M:%S')}  stop={sl_p}")

    # ── TP fills ──────────────────────────────────────────────────────────────
    if partial_exit_ev:
        pq = partial_exit_ev.get("qty")
        print(f"  │  TP1✅   {ts(partial_exit_ev['ms'],'%H:%M:%S')}  qty={pq}  (partial exit)")
    if mid_exit_ev:
        mq = mid_exit_ev.get("qty")
        print(f"  │  TP2✅   {ts(mid_exit_ev['ms'],'%H:%M:%S')}  qty={mq}  (mid exit)")

    # ── Trailing ──────────────────────────────────────────────────────────────
    if trail_armed_ev:
        tp = price(trail_armed_ev.get("price"))
        print(f"  │  TRAIL   {ts(trail_armed_ev['ms'],'%H:%M:%S')}  maker@{tp}")
    if trail_filled_ev:
        print(f"  │  TRAIL✅ {ts(trail_filled_ev['ms'],'%H:%M:%S')}  maker filled")
    if trail_fallback_ev:
        print(f"  │  TRAIL⚠  {ts(trail_fallback_ev['ms'],'%H:%M:%S')}  maker timeout → market")

    # ── Exit ──────────────────────────────────────────────────────────────────
    print(f"  └─ EXIT   {ts(completed_ms,'%H:%M:%S')}  reason={exit_reason}  PnL={pnl_str}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=10, help="Number of runs to show (default 10)")
    parser.add_argument("--hours", type=float, default=24.0, help="Look-back window in hours (default 24)")
    parser.add_argument("--db", type=str, default=str(DB_PATH))
    args = parser.parse_args()

    since_ms = int((datetime.now(timezone.utc) - timedelta(hours=args.hours)).timestamp() * 1000)

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row

    runs = load_runs(db, args.n, since_ms)
    if not runs:
        print(f"No runs found in last {args.hours}h")
        db.close()
        return

    # Summary header
    total_pnl = sum((r.get("realized_pnl_usdc") or 0.0) for r in runs if r.get("status") == "COMPLETED")
    entered = [r for r in runs if r.get("entry_price")]
    wins = [r for r in entered if (r.get("realized_pnl_usdc") or 0) > 0]

    print(f"\n{'═'*62}")
    print(f"  最近 {len(runs)} 個 runs  |  {ts(since_ms)} → now")
    print(f"  有進場: {len(entered)}  |  獲利: {len(wins)}  |  勝率: {len(wins)/max(len(entered),1)*100:.0f}%  |  總 PnL: {pnl(total_pnl)}")
    print(f"{'═'*62}")

    for run in runs:
        events = load_events(db, run["run_id"])
        summarize_run(run, events)

    db.close()
    print()


if __name__ == "__main__":
    main()
