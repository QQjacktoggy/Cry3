from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export manual trade snapshots into AI-friendly JSON/JSONL.")
    parser.add_argument(
        "--input-json",
        default="reports/manual_eth_entry_snapshots_3d.json",
        help="Source manual entry snapshot JSON.",
    )
    parser.add_argument("--start-date", default="2026-05-25", help="Inclusive Taipei date, YYYY-MM-DD.")
    parser.add_argument("--end-date", default="2026-05-27", help="Inclusive Taipei date, YYYY-MM-DD.")
    parser.add_argument(
        "--output-json",
        default="reports/manual_trades_ai_2026-05-25_2026-05-27.json",
        help="Destination JSON path.",
    )
    parser.add_argument(
        "--output-jsonl",
        default="reports/manual_trades_ai_2026-05-25_2026-05-27.jsonl",
        help="Destination JSONL path.",
    )
    return parser.parse_args()


def _date_only(iso_ts: str) -> str:
    return datetime.fromisoformat(iso_ts).date().isoformat()


def _label(net_pnl: float) -> str:
    if net_pnl > 0:
        return "winner"
    if net_pnl < 0:
        return "loser"
    return "flat"


def _bucket(value: float, *, low: float, high: float) -> str:
    if value <= low:
        return "low"
    if value >= high:
        return "high"
    return "mid"


def _setup_tag(row: dict) -> str:
    breakout = float(row.get("directional_breakout_3bar_atr") or 0.0)
    move_5m = float(row.get("directional_move_5m_atr") or 0.0)
    dist_ema = float(row.get("directional_distance_to_ema21_atr") or 0.0)
    dist_vwap = float(row.get("directional_distance_to_vwap_atr") or 0.0)
    if breakout <= -1.2 and move_5m <= -0.4:
        return "fade_after_push"
    if abs(dist_ema) >= 2.0 or abs(dist_vwap) >= 10.0:
        return "stretched_entry"
    if move_5m >= 1.0:
        return "chase_or_late_entry"
    return "mixed"


def _risk_flags(row: dict) -> list[str]:
    flags: list[str] = []
    if float(row.get("maker_ratio") or 0.0) < 1.0:
        flags.append("has_taker_fill")
    if not bool(row.get("is_zero_fee_maker")):
        flags.append("not_zero_fee_maker")
    if float(row.get("directional_distance_to_ema21_atr") or 0.0) >= 2.0:
        flags.append("far_from_ema")
    if float(row.get("directional_distance_to_vwap_atr") or 0.0) >= 8.0:
        flags.append("far_from_vwap")
    if float(row.get("directional_move_15m_atr") or 0.0) >= 3.0:
        flags.append("overheated_15m_move")
    if float(row.get("volume_ratio") or 0.0) >= 1.8:
        flags.append("crowded_volume")
    return flags


def _narrative(row: dict, result: str, risk_flags: list[str], setup_tag: str) -> str:
    side = row["side"]
    direction = row["direction"]
    pnl = float(row["net_pnl_ex_funding"])
    maker_ratio = float(row.get("maker_ratio") or 0.0)
    return (
        f"{row['started_at']} {side} {direction} entry at {row['avg_price']:.2f}, "
        f"net {pnl:.4f} USDC, result={result}, maker_ratio={maker_ratio:.2f}, "
        f"setup={setup_tag}, risk_flags={','.join(risk_flags) if risk_flags else 'none'}, "
        f"bias_1h={row.get('bias_1h')}, trend_5m={row.get('trend_alignment_5m')}, "
        f"breakout_3bar_atr={float(row.get('directional_breakout_3bar_atr') or 0.0):.2f}, "
        f"move_5m_atr={float(row.get('directional_move_5m_atr') or 0.0):.2f}, "
        f"move_15m_atr={float(row.get('directional_move_15m_atr') or 0.0):.2f}, "
        f"dist_ema_atr={float(row.get('directional_distance_to_ema21_atr') or 0.0):.2f}, "
        f"dist_vwap_atr={float(row.get('directional_distance_to_vwap_atr') or 0.0):.2f}."
    )


def _ai_record(row: dict) -> dict:
    pnl = float(row["net_pnl_ex_funding"])
    result = _label(pnl)
    risk_flags = _risk_flags(row)
    setup_tag = _setup_tag(row)
    return {
        "record_id": f"{row['symbol']}-{row['order_id']}",
        "time": {
            "started_at": row["started_at"],
            "snapshot_at": row["snapshot_at"],
            "date_taipei": _date_only(row["started_at"]),
        },
        "trade": {
            "symbol": row["symbol"],
            "side": row["side"],
            "direction": row["direction"],
            "fills": int(row["fills"]),
            "qty": float(row["qty"]),
            "avg_price": float(row["avg_price"]),
            "notional_usdc": float(row["notional_usdc"]),
            "maker_ratio": float(row["maker_ratio"]),
            "is_zero_fee_maker": bool(row["is_zero_fee_maker"]),
        },
        "result": {
            "label": result,
            "net_pnl_ex_funding": pnl,
            "pnl_bucket": _bucket(pnl, low=-2.0, high=2.0),
        },
        "market_context": {
            "bias_1h": row.get("bias_1h"),
            "trend_alignment_5m": row.get("trend_alignment_5m"),
            "market_trend": row.get("market_trend"),
            "market_playbook": row.get("market_playbook"),
            "market_risk_mode": row.get("market_risk_mode"),
            "market_confidence": float(row.get("market_confidence") or 0.0),
            "matches_1h_bias": bool(row.get("matches_1h_bias")),
        },
        "features": {
            "volume_ratio": float(row.get("volume_ratio") or 0.0),
            "atr_1m": float(row.get("atr_1m") or 0.0),
            "minute_range": float(row.get("minute_range") or 0.0),
            "distance_to_ema21_atr": float(row.get("directional_distance_to_ema21_atr") or 0.0),
            "distance_to_vwap_atr": float(row.get("directional_distance_to_vwap_atr") or 0.0),
            "breakout_3bar_atr": float(row.get("directional_breakout_3bar_atr") or 0.0),
            "move_5m_atr": float(row.get("directional_move_5m_atr") or 0.0),
            "move_15m_atr": float(row.get("directional_move_15m_atr") or 0.0),
            "session_range_30m_atr": float(row.get("session_range_30m_atr") or 0.0),
            "ema20_5m": float(row.get("ema20_5m") or 0.0),
            "ema50_5m": float(row.get("ema50_5m") or 0.0),
        },
        "derived": {
            "setup_tag": setup_tag,
            "risk_flags": risk_flags,
        },
        "ai_text": _narrative(row, result, risk_flags, setup_tag),
    }


def _summary(records: list[dict]) -> dict:
    pnl_values = [record["result"]["net_pnl_ex_funding"] for record in records]
    winners = [value for value in pnl_values if value > 0]
    losers = [value for value in pnl_values if value < 0]
    setup_counts = Counter(record["derived"]["setup_tag"] for record in records)
    risk_counts = Counter(flag for record in records for flag in record["derived"]["risk_flags"])
    by_day: dict[str, dict[str, float | int]] = defaultdict(lambda: {"orders": 0, "net_pnl_ex_funding": 0.0})
    for record in records:
        day = record["time"]["date_taipei"]
        by_day[day]["orders"] += 1
        by_day[day]["net_pnl_ex_funding"] += record["result"]["net_pnl_ex_funding"]
    return {
        "orders": len(records),
        "wins": len(winners),
        "losses": len(losers),
        "win_rate": len(winners) / len(records) if records else 0.0,
        "net_pnl_ex_funding": sum(pnl_values),
        "avg_order_net": mean(pnl_values) if pnl_values else 0.0,
        "best_order_net": max(pnl_values) if pnl_values else 0.0,
        "worst_order_net": min(pnl_values) if pnl_values else 0.0,
        "profit_factor": (sum(winners) / abs(sum(losers))) if losers else None,
        "setup_tag_counts": dict(sorted(setup_counts.items())),
        "risk_flag_counts": dict(sorted(risk_counts.items())),
        "by_day": dict(sorted(by_day.items())),
    }


def main() -> int:
    args = parse_args()
    payload = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
    rows = payload.get("snapshots", [])
    filtered_rows = [
        row
        for row in rows
        if args.start_date <= _date_only(row["started_at"]) <= args.end_date
    ]
    records = [_ai_record(row) for row in filtered_rows]
    output = {
        "generated_from": args.input_json,
        "window": {
            "start_date": args.start_date,
            "end_date": args.end_date,
        },
        "summary": _summary(records),
        "schema_notes": {
            "winner": "net_pnl_ex_funding > 0",
            "loser": "net_pnl_ex_funding < 0",
            "setup_tag.fade_after_push": "price already pushed, then manual entry fades the move",
            "risk_flags": [
                "has_taker_fill",
                "not_zero_fee_maker",
                "far_from_ema",
                "far_from_vwap",
                "overheated_15m_move",
                "crowded_volume",
            ],
        },
        "records": records,
    }

    output_json = Path(args.output_json)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    output_jsonl = Path(args.output_jsonl)
    output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    output_jsonl.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + ("\n" if records else ""),
        encoding="utf-8",
    )

    print(json.dumps(output["summary"], ensure_ascii=False, indent=2))
    print(str(output_json))
    print(str(output_jsonl))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
