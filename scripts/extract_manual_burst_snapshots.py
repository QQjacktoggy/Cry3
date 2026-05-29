from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_manual_eth_scalp import (
    BINANCE_FAPI_BASE,
    ManualScalpConfig,
    TAIPEI,
    aggregate_five_minute,
    aggregate_one_hour,
    anchored_daily_vwap,
    atr_series,
    ema_series,
    fetch_klines,
    hourly_bias,
    volume_sma_series,
)
from scripts.extract_manual_trade_snapshots import _snapshot_for_order
from src.gridbot.strategy.long_pullback import StrategyConfig as PullbackStrategyConfig
from src.gridbot.strategy.market_state import build_market_state_context, classify_market_state


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract feature snapshots at manual burst starts.")
    parser.add_argument("--bursts-json", required=True)
    parser.add_argument("--symbol", default="ETHUSDC")
    parser.add_argument("--lookback-days", type=int, default=3)
    parser.add_argument("--output-json", required=True)
    return parser.parse_args()


def _avg(values: list[float | None]) -> float | None:
    nums = [value for value in values if value is not None]
    return mean(nums) if nums else None


def _side_for_burst(burst: dict) -> str | None:
    side = str(burst.get("dominant_side", "")).upper()
    if side in {"BUY", "SELL"}:
        return side
    details = burst.get("fills_detail", [])
    if not details:
        return None
    return str(details[0].get("side", "")).upper() or None


def _pseudo_order_from_burst(burst: dict, symbol: str) -> dict | None:
    side = _side_for_burst(burst)
    if side not in {"BUY", "SELL"}:
        return None
    qty = max(float(burst.get("buy_qty") or 0.0), float(burst.get("sell_qty") or 0.0))
    if qty <= 0:
        qty = sum(float(item.get("qty") or 0.0) for item in burst.get("fills_detail", []))
    first_price = float(burst.get("first_price") or 0.0)
    if qty <= 0 or first_price <= 0:
        return None
    return {
        "order_id": int(burst.get("burst_id") or 0),
        "symbol": symbol,
        "side": side,
        "time_first_ms": int(burst["started_at_ms"]),
        "started_at": burst["started_at"],
        "fills": int(burst.get("fills") or 0),
        "qty": qty,
        "avg_price": first_price,
        "maker_ratio": float(burst.get("maker_ratio") or 0.0),
        "net_pnl_ex_funding": float(burst.get("net_pnl_ex_funding") or 0.0),
    }


def _date_window(bursts: list[dict], lookback_days: int) -> tuple[int, int]:
    start_ms = min(int(burst["started_at_ms"]) for burst in bursts)
    end_ms = max(int(burst["ended_at_ms"]) for burst in bursts)
    padded_start = int((datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc) - timedelta(days=lookback_days)).timestamp() * 1000)
    padded_end = int((datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc) + timedelta(days=1)).timestamp() * 1000)
    return padded_start, padded_end


def build_feature_summary(rows: list[dict]) -> dict:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        buckets[row["label"]].append(row)
    fields = [
        "net_pnl_ex_funding",
        "burst_turnover_usdc",
        "burst_duration_minutes",
        "burst_orders",
        "burst_fills",
        "maker_ratio",
        "volume_ratio",
        "directional_distance_to_ema21_atr",
        "directional_distance_to_vwap_atr",
        "directional_breakout_3bar_atr",
        "directional_move_5m_atr",
        "directional_move_15m_atr",
        "session_range_30m_atr",
        "market_confidence",
    ]
    summary: dict[str, dict] = {}
    for label, items in buckets.items():
        summary[label] = {
            "count": len(items),
            "maker_only_ratio": sum(1 for item in items if item["maker_ratio"] >= 1.0) / len(items),
            "with_1h_bias_ratio": sum(1 for item in items if item["matches_1h_bias"]) / len(items),
        }
        for field in fields:
            summary[label][field] = _avg([item.get(field) for item in items])
    return summary


def main() -> int:
    args = parse_args()
    payload = json.loads(Path(args.bursts_json).read_text(encoding="utf-8"))
    bursts = payload.get("bursts", [])
    if not bursts:
        raise SystemExit("No bursts found in input JSON.")

    start_ms, end_ms = _date_window(bursts, args.lookback_days)
    candles = fetch_klines(BINANCE_FAPI_BASE, args.symbol.upper(), "1m", start_ms, end_ms)
    closes_1m = [item.close for item in candles]
    ema_fast_1m = ema_series(closes_1m, 21)
    atr_1m = atr_series(candles, 14)
    volume_sma = volume_sma_series(candles, 20)
    vwap_1m = anchored_daily_vwap(candles)
    five, five_map = aggregate_five_minute(candles)
    one_hour, one_hour_map = aggregate_one_hour(candles)
    closes_5m = [item.close for item in five]
    closes_1h = [item.close for item in one_hour]
    ema20_5m = ema_series(closes_5m, 20)
    ema50_5m = ema_series(closes_5m, 50)
    ema_fast_1h = ema_series(closes_1h, 8)
    ema_slow_1h = ema_series(closes_1h, 21)
    runtime = PullbackStrategyConfig(symbol=args.symbol.upper())
    market_context_5m = build_market_state_context(five, runtime)
    market_decisions_5m = [
        classify_market_state(five, idx, market_context_5m, runtime)
        for idx in range(len(five))
    ]
    config = ManualScalpConfig(symbol=args.symbol.upper())

    rows: list[dict] = []
    for burst in bursts:
        pseudo_order = _pseudo_order_from_burst(burst, args.symbol.upper())
        if pseudo_order is None:
            continue
        row = _snapshot_for_order(
            pseudo_order,
            candles,
            ema_fast_1m,
            atr_1m,
            volume_sma,
            vwap_1m,
            five,
            five_map,
            ema20_5m,
            ema50_5m,
            one_hour,
            one_hour_map,
            ema_fast_1h,
            ema_slow_1h,
            market_decisions_5m,
            config,
        )
        if row is None:
            continue
        row["burst_id"] = int(burst.get("burst_id") or 0)
        row["burst_started_at"] = burst["started_at"]
        row["burst_ended_at"] = burst["ended_at"]
        row["burst_duration_minutes"] = float(burst.get("duration_minutes") or 0.0)
        row["burst_fills"] = int(burst.get("fills") or 0)
        row["burst_orders"] = int(burst.get("orders") or 0)
        row["burst_turnover_usdc"] = float(burst.get("turnover_usdc") or 0.0)
        row["burst_maker_ratio"] = float(burst.get("maker_ratio") or 0.0)
        row["burst_dominant_side"] = str(burst.get("dominant_side", ""))
        rows.append(row)

    output = {
        "generated_at": datetime.now(tz=timezone.utc).astimezone(TAIPEI).isoformat(),
        "symbol": args.symbol.upper(),
        "source": args.bursts_json,
        "bursts_requested": len(bursts),
        "snapshots_generated": len(rows),
        "feature_summary": build_feature_summary(rows),
        "snapshots": rows,
    }
    output_path = Path(args.output_json)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output["feature_summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
