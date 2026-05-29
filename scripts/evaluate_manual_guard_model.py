from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay manual ETHUSDC order snapshots through a lightweight guard model."
    )
    parser.add_argument(
        "--snapshots-json",
        default="reports/manual_eth_entry_snapshots_3d.json",
        help="Snapshot file produced by extract_manual_trade_snapshots.py.",
    )
    parser.add_argument("--output-json", default="")
    parser.add_argument("--max-directional-ema-atr", type=float, default=2.0)
    parser.add_argument("--max-directional-move-15m-atr", type=float, default=3.0)
    parser.add_argument("--max-notional-usdc", type=float, default=8000.0)
    return parser.parse_args()


def _day(value: str) -> str:
    return datetime.fromisoformat(value).date().isoformat()


def _guard_reasons(row: dict[str, Any], args: argparse.Namespace) -> list[str]:
    reasons: list[str] = []
    if float(row.get("notional_usdc") or 0.0) > args.max_notional_usdc:
        reasons.append("notional_cap")
    if float(row.get("directional_distance_to_ema21_atr") or 0.0) > args.max_directional_ema_atr:
        reasons.append("ema_overheat")
    if float(row.get("directional_move_15m_atr") or 0.0) > args.max_directional_move_15m_atr:
        reasons.append("move15_overheat")
    return reasons


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pnl_values = [float(row["net_pnl_ex_funding"]) for row in rows]
    winners = [value for value in pnl_values if value > 0]
    losers = [value for value in pnl_values if value < 0]
    by_day: dict[str, dict[str, Any]] = {}
    day_rows: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        day_rows[_day(row["started_at"])].append(row)
    for day, items in sorted(day_rows.items()):
        by_day[day] = {
            "orders": len(items),
            "net_pnl_ex_funding": sum(float(item["net_pnl_ex_funding"]) for item in items),
            "maker_ratio": mean(float(item["maker_ratio"]) for item in items) if items else 0.0,
            "worst_order_net": min((float(item["net_pnl_ex_funding"]) for item in items), default=0.0),
        }
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    return {
        "orders": len(rows),
        "wins": len(winners),
        "losses": len(losers),
        "win_rate": len(winners) / len(rows) if rows else 0.0,
        "net_pnl_ex_funding": sum(pnl_values),
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "maker_ratio": mean(float(row["maker_ratio"]) for row in rows) if rows else 0.0,
        "avg_order_net": mean(pnl_values) if pnl_values else 0.0,
        "best_order_net": max(pnl_values, default=0.0),
        "worst_order_net": min(pnl_values, default=0.0),
        "by_day": by_day,
    }


def evaluate(rows: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    kept: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    blocked_reason_counts: defaultdict[str, int] = defaultdict(int)
    blocked_reason_pnl: defaultdict[str, float] = defaultdict(float)
    for row in sorted(rows, key=lambda item: item["started_at"]):
        reasons = _guard_reasons(row, args)
        if reasons:
            blocked_row = dict(row)
            blocked_row["guard_reasons"] = reasons
            blocked.append(blocked_row)
            for reason in reasons:
                blocked_reason_counts[reason] += 1
                blocked_reason_pnl[reason] += float(row["net_pnl_ex_funding"])
        else:
            kept.append(row)
    return {
        "guard": {
            "max_directional_ema_atr": args.max_directional_ema_atr,
            "max_directional_move_15m_atr": args.max_directional_move_15m_atr,
            "max_notional_usdc": args.max_notional_usdc,
        },
        "baseline": _summarize(rows),
        "kept": _summarize(kept),
        "blocked": _summarize(blocked),
        "blocked_reason_counts": dict(sorted(blocked_reason_counts.items())),
        "blocked_reason_pnl": dict(sorted(blocked_reason_pnl.items())),
        "largest_blocked_losses": sorted(
            blocked,
            key=lambda item: float(item["net_pnl_ex_funding"]),
        )[:10],
        "largest_blocked_winners": sorted(
            [row for row in blocked if float(row["net_pnl_ex_funding"]) > 0],
            key=lambda item: float(item["net_pnl_ex_funding"]),
            reverse=True,
        )[:10],
    }


def main() -> int:
    args = parse_args()
    payload = json.loads(Path(args.snapshots_json).read_text(encoding="utf-8"))
    rows = payload.get("snapshots", [])
    result = evaluate(rows, args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
