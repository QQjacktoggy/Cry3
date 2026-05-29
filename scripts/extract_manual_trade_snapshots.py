from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_signal import fetch_klines
from scripts.backtest_manual_eth_scalp import (
    Candle,
    ManualScalpConfig,
    TAIPEI,
    ONE_MINUTE_MS,
    aggregate_five_minute,
    aggregate_one_hour,
    anchored_daily_vwap,
    atr_series,
    ema_series,
    hourly_bias,
    volume_sma_series,
)
from src.gridbot.strategy.long_pullback import StrategyConfig as PullbackStrategyConfig
from src.gridbot.strategy.market_state import build_market_state_context, classify_market_state


BINANCE_FAPI_BASE = "https://fapi.binance.com"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Extract feature snapshots around manual mainnet ETH trades.")
    parser.add_argument("--analysis-json", required=True, help="Path to analyze_manual_mainnet JSON output.")
    parser.add_argument("--symbol", default="ETHUSDC")
    parser.add_argument("--start-date", required=True, help="YYYY-MM-DD in Taipei.")
    parser.add_argument("--end-date", required=True, help="YYYY-MM-DD in Taipei.")
    parser.add_argument("--lookback-days", type=int, default=3)
    parser.add_argument("--output-json", required=True)
    return parser.parse_args()


def date_range_to_ms(start_date: str, end_date: str) -> tuple[int, int]:
    start = datetime.fromisoformat(start_date).replace(tzinfo=TAIPEI)
    end = datetime.fromisoformat(end_date).replace(tzinfo=TAIPEI) + timedelta(days=1)
    return int(start.astimezone(timezone.utc).timestamp() * 1000), int(end.astimezone(timezone.utc).timestamp() * 1000)


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(TAIPEI).isoformat()


def day_key(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(TAIPEI).strftime("%Y-%m-%d")


def _directional_sign(side: str) -> int:
    return 1 if side.upper() == "BUY" else -1


def _safe_div(numerator: float, denominator: float) -> float | None:
    if abs(denominator) < 1e-12:
        return None
    return numerator / denominator


def _avg(values: list[float | None]) -> float | None:
    nums = [item for item in values if item is not None]
    if not nums:
        return None
    return mean(nums)


def _find_snapshot_index(candles: list[Candle], order_ms: int) -> int | None:
    order_bucket = order_ms - (order_ms % ONE_MINUTE_MS)
    target_bucket = order_bucket - ONE_MINUTE_MS
    lookup = {candle.open_time_ms: idx for idx, candle in enumerate(candles)}
    return lookup.get(target_bucket)


def _load_analysis_orders(path: Path, symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("order_summaries", [])
    return [
        row
        for row in rows
        if row.get("symbol") == symbol and start_ms <= int(row["time_first_ms"]) < end_ms
    ]


def _snapshot_for_order(
    order: dict,
    candles: list[Candle],
    ema_fast_1m: list[float | None],
    atr_1m: list[float | None],
    volume_sma: list[float | None],
    vwap_1m: list[float | None],
    five: list[Candle],
    five_map: list[int | None],
    ema20_5m: list[float | None],
    ema50_5m: list[float | None],
    one_hour: list[Candle],
    one_hour_map: list[int | None],
    ema_fast_1h: list[float | None],
    ema_slow_1h: list[float | None],
    market_decisions_5m: list[object | None],
    config: ManualScalpConfig,
) -> dict | None:
    index = _find_snapshot_index(candles, int(order["time_first_ms"]))
    if index is None or index < 30:
        return None
    candle = candles[index]
    atr = atr_1m[index]
    ema_fast = ema_fast_1m[index]
    avg_vol = volume_sma[index]
    vwap = vwap_1m[index]
    if atr is None or ema_fast is None or avg_vol is None or vwap is None or atr <= 0:
        return None

    side = str(order["side"]).upper()
    direction = _directional_sign(side)
    five_idx = five_map[index]
    one_hour_bias = hourly_bias(index, one_hour, one_hour_map, ema_fast_1h, ema_slow_1h, config)
    market = market_decisions_5m[five_idx] if five_idx is not None and five_idx >= 0 else None
    ema20 = ema20_5m[five_idx] if five_idx is not None and five_idx >= 0 else None
    ema50 = ema50_5m[five_idx] if five_idx is not None and five_idx >= 0 else None
    prior_3 = candles[index - 3:index]
    prior_5 = candles[index - 5:index]
    prior_15 = candles[index - 15:index]
    prior_30 = candles[index - 30:index]

    breakout_level = max(item.high for item in prior_3) if direction > 0 else min(item.low for item in prior_3)
    breakout_delta = (
        candle.close - breakout_level
        if direction > 0
        else breakout_level - candle.close
    )
    recent_move_5m = candle.close - prior_5[0].close
    recent_move_15m = candle.close - prior_15[0].close
    session_range_30m = max(item.high for item in prior_30) - min(item.low for item in prior_30)

    trend_alignment = None
    if ema20 is not None and ema50 is not None:
        trend_alignment = (
            "up" if ema20 > ema50 else "down" if ema20 < ema50 else "flat"
        )

    pnl = float(order["net_pnl_ex_funding"])
    label = "winner" if pnl > 0 else "loser" if pnl < 0 else "flat"

    return {
        "order_id": int(order["order_id"]),
        "started_at": order["started_at"],
        "snapshot_at": ms_to_iso(candle.open_time_ms),
        "symbol": order["symbol"],
        "side": side,
        "direction": "long" if direction > 0 else "short",
        "fills": int(order["fills"]),
        "qty": float(order["qty"]),
        "avg_price": float(order["avg_price"]),
        "notional_usdc": float(order["qty"]) * float(order["avg_price"]),
        "maker_ratio": float(order["maker_ratio"]),
        "net_pnl_ex_funding": pnl,
        "label": label,
        "minute_close": candle.close,
        "minute_range": candle.high - candle.low,
        "atr_1m": atr,
        "volume_ratio": candle.volume / avg_vol if avg_vol else None,
        "distance_to_ema21_atr": _safe_div(candle.close - ema_fast, atr),
        "distance_to_vwap_atr": _safe_div(candle.close - vwap, atr),
        "directional_distance_to_ema21_atr": _safe_div((candle.close - ema_fast) * direction, atr),
        "directional_distance_to_vwap_atr": _safe_div((candle.close - vwap) * direction, atr),
        "directional_breakout_3bar_atr": _safe_div(breakout_delta, atr),
        "directional_move_5m_atr": _safe_div(recent_move_5m * direction, atr),
        "directional_move_15m_atr": _safe_div(recent_move_15m * direction, atr),
        "session_range_30m_atr": _safe_div(session_range_30m, atr),
        "ema20_5m": ema20,
        "ema50_5m": ema50,
        "trend_alignment_5m": trend_alignment,
        "bias_1h": one_hour_bias,
        "market_trend": getattr(market, "trend", None) if market is not None else None,
        "market_playbook": getattr(market, "playbook", None) if market is not None else None,
        "market_risk_mode": getattr(market, "risk_mode", None) if market is not None else None,
        "market_confidence": float(getattr(market, "confidence", 0.0) or 0.0) if market is not None else None,
        "matches_1h_bias": (
            (direction > 0 and one_hour_bias == "up")
            or (direction < 0 and one_hour_bias == "down")
        ),
        "is_zero_fee_maker": float(order["maker_ratio"]) >= 1.0,
    }


def build_feature_summary(rows: list[dict]) -> dict:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        buckets[row["label"]].append(row)
    numeric_fields = [
        "net_pnl_ex_funding",
        "notional_usdc",
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
    for label, label_rows in buckets.items():
        summary[label] = {
            "count": len(label_rows),
            "maker_zero_fee_ratio": sum(1 for row in label_rows if row["is_zero_fee_maker"]) / len(label_rows),
            "with_1h_bias_ratio": sum(1 for row in label_rows if row["matches_1h_bias"]) / len(label_rows),
        }
        for field in numeric_fields:
            summary[label][field] = _avg([row.get(field) for row in label_rows])
    return summary


def main() -> int:
    args = parse_args()
    start_ms, end_ms = date_range_to_ms(args.start_date, args.end_date)
    orders = _load_analysis_orders(Path(args.analysis_json), args.symbol.upper(), start_ms, end_ms)
    if not orders:
        raise SystemExit(f"No matching manual orders found for {args.symbol} in {args.start_date}..{args.end_date}")

    padded_start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc) - timedelta(days=args.lookback_days)
    padded_start_ms = int(padded_start_dt.timestamp() * 1000)
    candles = fetch_klines(BINANCE_FAPI_BASE, args.symbol.upper(), "1m", padded_start_ms, end_ms)
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

    snapshots = []
    for order in orders:
        row = _snapshot_for_order(
            order,
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
        if row is not None:
            snapshots.append(row)

    payload = {
        "generated_at": datetime.now(tz=TAIPEI).isoformat(),
        "symbol": args.symbol.upper(),
        "start_date": args.start_date,
        "end_date": args.end_date,
        "orders_requested": len(orders),
        "snapshots_generated": len(snapshots),
        "feature_summary": build_feature_summary(snapshots),
        "snapshots": snapshots,
    }
    output_path = Path(args.output_json)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload["feature_summary"], ensure_ascii=False, indent=2))
    print(f"Saved JSON to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
