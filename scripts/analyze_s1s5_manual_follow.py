"""Analyze S1-S5 Telegram signals matched to manual mainnet follow trades.

This script intentionally ignores old router/orb/manual_scout records.  It
keeps the live evaluation focused on the deployed winrate_optimized_portfolio
family: S1_BB_RSI, S2_SuperTrend, S3_EMA_MACD, S4_Donchian, and S5_Stoch.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any


S1S5_STRATEGIES = {
    "S1_BB_RSI",
    "S2_SuperTrend",
    "S3_EMA_MACD",
    "S4_Donchian",
    "S5_Stoch",
}


@dataclass
class Bucket:
    signals: int = 0
    closed: int = 0
    open: int = 0
    expired: int = 0
    wins: int = 0
    gross_pnl: float = 0.0
    fees: float = 0.0
    entry_notional: float = 0.0
    maker_fills: int = 0
    taker_fills: int = 0
    hold_seconds: list[float] = field(default_factory=list)
    entry_delay_seconds: list[float] = field(default_factory=list)
    entry_slippage_bps: list[float] = field(default_factory=list)

    def add_cycle(self, cycle: dict[str, Any]) -> None:
        self.signals += 1
        status = cycle.get("cycle_status")
        if status == "closed":
            self.closed += 1
        elif status == "open":
            self.open += 1
        elif status == "expired":
            self.expired += 1

        if status != "closed":
            return

        gross = float(cycle.get("cycle_realized_pnl") or 0.0)
        fee = float(cycle.get("cycle_commission") or 0.0)
        self.gross_pnl += gross
        self.fees += fee
        self.entry_notional += float(cycle.get("entry_notional_usdc") or 0.0)
        self.wins += int(gross > 0)
        self.maker_fills += int(cycle.get("maker_count") or 0)
        self.taker_fills += int(cycle.get("taker_count") or 0)

        for key, target in (
            ("holding_seconds", self.hold_seconds),
            ("first_entry_delay_seconds", self.entry_delay_seconds),
            ("entry_slippage_bps", self.entry_slippage_bps),
        ):
            value = cycle.get(key)
            if isinstance(value, (int, float)):
                target.append(float(value))

    def as_dict(self) -> dict[str, Any]:
        net_pnl = self.gross_pnl - self.fees
        return {
            "signals": self.signals,
            "closed": self.closed,
            "open": self.open,
            "expired": self.expired,
            "win_rate_pct": round(self.wins / self.closed * 100, 2) if self.closed else None,
            "gross_pnl_usdc": round(self.gross_pnl, 4),
            "fees_usdc": round(self.fees, 4),
            "net_pnl_usdc": round(net_pnl, 4),
            "avg_net_pnl_usdc": round(net_pnl / self.closed, 4) if self.closed else None,
            "avg_entry_notional_usdc": round(self.entry_notional / self.closed, 2) if self.closed else None,
            "maker_fills": self.maker_fills,
            "taker_fills": self.taker_fills,
            "taker_fill_ratio_pct": round(
                self.taker_fills / (self.taker_fills + self.maker_fills) * 100,
                2,
            )
            if (self.taker_fills + self.maker_fills)
            else None,
            "avg_hold_seconds": round(mean(self.hold_seconds), 2) if self.hold_seconds else None,
            "avg_entry_delay_seconds": round(mean(self.entry_delay_seconds), 2)
            if self.entry_delay_seconds
            else None,
            "avg_entry_slippage_bps": round(mean(self.entry_slippage_bps), 2)
            if self.entry_slippage_bps
            else None,
        }


def _tw_iso(ms: int | float | None) -> str | None:
    if not ms:
        return None
    tz = timezone.utc
    dt = datetime.fromtimestamp(float(ms) / 1000, tz=tz).astimezone(
        timezone.utc
    )
    # Keep the offset explicit while avoiding a non-stdlib timezone dependency.
    taipei = dt.timestamp() + 8 * 3600
    return datetime.fromtimestamp(taipei, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "+08:00")


def _load_latest_s1s5_cycles(db_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        select id, event_time_ms, event_type, details_json
        from audit_log
        where event_type in (
            'manual_signal_sent',
            'manual_signal_auto_matched',
            'manual_signal_auto_match_expired'
        )
        order by id asc
        """
    ).fetchall()

    sent: list[dict[str, Any]] = []
    latest_by_exec: dict[str, dict[str, Any]] = {}

    for row in rows:
        details = json.loads(row["details_json"])
        event_type = row["event_type"]
        if event_type == "manual_signal_sent":
            strategy = details.get("strategy")
            if strategy not in S1S5_STRATEGIES:
                continue
            details["_audit_id"] = row["id"]
            details["_event_time_ms"] = row["event_time_ms"]
            details["time_taipei"] = _tw_iso(row["event_time_ms"])
            sent.append(details)
            continue

        signal = details.get("signal") or {}
        strategy = signal.get("strategy") or details.get("strategy")
        if strategy not in S1S5_STRATEGIES:
            continue
        execution_id = details.get("execution_id")
        if not execution_id:
            continue
        if execution_id not in latest_by_exec or row["id"] > latest_by_exec[execution_id]["_audit_id"]:
            details["_audit_id"] = row["id"]
            details["_event_time_ms"] = row["event_time_ms"]
            details["_event_type"] = event_type
            details["time_taipei"] = _tw_iso(row["event_time_ms"])
            latest_by_exec[execution_id] = details

    return sent, sorted(latest_by_exec.values(), key=lambda row: row["_audit_id"])


def _summarize_cycles(cycles: list[dict[str, Any]]) -> dict[str, Any]:
    by_strategy: dict[str, Bucket] = defaultdict(Bucket)
    by_strategy_direction: dict[str, Bucket] = defaultdict(Bucket)
    overall = Bucket()
    latest_closed: list[dict[str, Any]] = []

    for cycle in cycles:
        signal = cycle.get("signal") or {}
        strategy = signal.get("strategy") or cycle.get("strategy") or "UNKNOWN"
        direction = cycle.get("direction") or signal.get("direction") or "UNKNOWN"
        overall.add_cycle(cycle)
        by_strategy[strategy].add_cycle(cycle)
        by_strategy_direction[f"{strategy}:{direction}"].add_cycle(cycle)
        if cycle.get("cycle_status") == "closed":
            latest_closed.append(cycle)

    def compact_trade(cycle: dict[str, Any]) -> dict[str, Any]:
        signal = cycle.get("signal") or {}
        return {
            "audit_id": cycle.get("_audit_id"),
            "time_taipei": cycle.get("time_taipei"),
            "execution_id": cycle.get("execution_id"),
            "strategy": signal.get("strategy") or cycle.get("strategy"),
            "direction": cycle.get("direction") or signal.get("direction"),
            "planned_entry": signal.get("planned_entry") or cycle.get("planned_entry"),
            "entry_avg_price": cycle.get("entry_avg_price"),
            "exit_avg_price": cycle.get("exit_avg_price"),
            "entry_qty": cycle.get("entry_qty"),
            "gross_pnl_usdc": cycle.get("cycle_realized_pnl"),
            "fees_usdc": cycle.get("cycle_commission"),
            "net_pnl_usdc": round(
                float(cycle.get("cycle_realized_pnl") or 0.0)
                - float(cycle.get("cycle_commission") or 0.0),
                4,
            ),
            "maker_count": cycle.get("maker_count"),
            "taker_count": cycle.get("taker_count"),
            "holding_seconds": cycle.get("holding_seconds"),
            "entry_slippage_bps": cycle.get("entry_slippage_bps"),
        }

    return {
        "overall": overall.as_dict(),
        "by_strategy": {key: value.as_dict() for key, value in sorted(by_strategy.items())},
        "by_strategy_direction": {
            key: value.as_dict() for key, value in sorted(by_strategy_direction.items())
        },
        "latest_closed_cycles": [
            compact_trade(row)
            for row in sorted(latest_closed, key=lambda item: item["_audit_id"], reverse=True)[:20]
        ],
    }


def _load_manual_style(path: Path | None) -> dict[str, Any] | None:
    if not path or not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    records = data.get("records") or []
    setup_counts: Counter[str] = Counter()
    risk_counts: Counter[str] = Counter()
    by_direction: dict[str, Bucket] = defaultdict(Bucket)
    for record in records:
        setup = (record.get("derived") or {}).get("setup_tag")
        if setup:
            setup_counts[setup] += 1
        for flag in (record.get("derived") or {}).get("risk_flags") or []:
            risk_counts[flag] += 1

        direction = ((record.get("trade") or {}).get("direction") or "UNKNOWN").lower()
        bucket = by_direction[direction]
        bucket.signals += 1
        bucket.closed += 1
        pnl = float((record.get("result") or {}).get("net_pnl_ex_funding") or 0.0)
        bucket.gross_pnl += pnl
        bucket.wins += int(pnl > 0)
        bucket.entry_notional += float((record.get("trade") or {}).get("notional_usdc") or 0.0)
        maker_ratio = (record.get("trade") or {}).get("maker_ratio")
        if isinstance(maker_ratio, (int, float)):
            if maker_ratio >= 1.0:
                bucket.maker_fills += 1
            else:
                bucket.taker_fills += 1

    return {
        "source": str(path),
        "summary": data.get("summary") or {},
        "top_setup_tags": dict(setup_counts.most_common(8)),
        "top_risk_flags": dict(risk_counts.most_common(10)),
        "by_direction": {key: value.as_dict() for key, value in sorted(by_direction.items())},
    }


def _recommendations(live: dict[str, Any], manual: dict[str, Any] | None) -> list[str]:
    rows: list[str] = []
    by_strategy = live.get("by_strategy", {})
    s2 = by_strategy.get("S2_SuperTrend", {})
    if s2.get("closed", 0) > 0:
        rows.append(
            "Keep S2_SuperTrend active as the main S1-S5 live candidate until at least 30 closed follow cycles are collected."
        )
        if (s2.get("taker_fill_ratio_pct") or 0) > 10:
            rows.append(
                "Prefer maker exits for S2 when possible; taker fills are already visible in net PnL compression."
            )
    for weak in ("S3_EMA_MACD", "S4_Donchian", "S5_Stoch"):
        stats = by_strategy.get(weak)
        if not stats or stats.get("closed", 0) == 0:
            rows.append(f"Do not upsize {weak} yet; wait for live manual-follow evidence.")

    if manual:
        risk_flags = manual.get("top_risk_flags", {})
        if risk_flags.get("has_taker_fill") or risk_flags.get("not_zero_fee_maker"):
            rows.append(
                "Track maker/taker separately; manual history shows fees are a major difference between good small wins and weak net results."
            )
        if risk_flags.get("far_from_vwap") or risk_flags.get("far_from_ema"):
            rows.append(
                "Add a distance-to-VWAP/EMA tag to every S1-S5 notification so your follow trades can be compared against the manual loss patterns."
            )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze S1-S5 manual follow performance")
    parser.add_argument("--db", type=Path, default=Path("data/gridbot.db"))
    parser.add_argument("--manual-ai", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    sent, cycles = _load_latest_s1s5_cycles(args.db)
    live = _summarize_cycles(cycles)
    manual = _load_manual_style(args.manual_ai)
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "allowed_strategies": sorted(S1S5_STRATEGIES),
        "source_db": str(args.db),
        "s1s5_signal_count": len(sent),
        "s1s5_cycle_count": len(cycles),
        "live_manual_follow": live,
        "manual_style_reference": manual,
        "recommendations": _recommendations(live, manual),
    }

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
