"""Sweep conservative micro-only live replay parameters on recent candles."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import product
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_signal import fetch_klines
from scripts.live_replay_backtest import live_base, run_live_replay
from src.gridbot.replay.live_replay import ReplayConfig

DEFAULT_BASE_URL = "https://testnet.binancefuture.com"

MICRO_CANDIDATES: tuple[dict[str, float], ...] = (
    {
        "micro_margin_pct": 8.0,
        "micro_leverage_cap": 8.0,
        "micro_max_extension_atr": 2.8,
    },
    {
        "micro_margin_pct": 10.0,
        "micro_leverage_cap": 10.0,
        "micro_max_extension_atr": 3.0,
    },
    {
        "micro_margin_pct": 12.0,
        "micro_leverage_cap": 12.0,
        "micro_max_extension_atr": 3.2,
    },
)

RECLAIM_CANDIDATES: tuple[dict[str, float], ...] = (
    {
        "micro_reversion_margin_pct": 6.0,
        "micro_reversion_leverage_cap": 8.0,
        "micro_reversion_min_dip_atr": 0.95,
        "micro_reversion_stop_atr": 0.75,
        "micro_reversion_take_profit_atr": 2.2,
    },
    {
        "micro_reversion_margin_pct": 8.0,
        "micro_reversion_leverage_cap": 10.0,
        "micro_reversion_min_dip_atr": 0.9,
        "micro_reversion_stop_atr": 0.8,
        "micro_reversion_take_profit_atr": 2.4,
    },
    {
        "micro_reversion_margin_pct": 10.0,
        "micro_reversion_leverage_cap": 12.0,
        "micro_reversion_min_dip_atr": 0.85,
        "micro_reversion_stop_atr": 0.85,
        "micro_reversion_take_profit_atr": 2.6,
    },
)

VWAP_CANDIDATES: tuple[dict[str, float], ...] = (
    {
        "micro_vwap_margin_pct": 6.0,
        "micro_vwap_leverage_cap": 10.0,
        "micro_vwap_min_sweep_atr": 0.65,
        "micro_vwap_stop_atr": 0.7,
        "micro_vwap_take_profit_atr": 1.8,
    },
    {
        "micro_vwap_margin_pct": 8.0,
        "micro_vwap_leverage_cap": 12.0,
        "micro_vwap_min_sweep_atr": 0.6,
        "micro_vwap_stop_atr": 0.75,
        "micro_vwap_take_profit_atr": 2.0,
    },
    {
        "micro_vwap_margin_pct": 10.0,
        "micro_vwap_leverage_cap": 14.0,
        "micro_vwap_min_sweep_atr": 0.55,
        "micro_vwap_stop_atr": 0.8,
        "micro_vwap_take_profit_atr": 2.1,
    },
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sweep recent micro-only live replay parameters.",
    )
    parser.add_argument("--hours", type=float, default=24.0)
    parser.add_argument("--warmup-hours", type=float, default=30.0)
    parser.add_argument("--symbol", default="ETHUSDC")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--maker-fee-rate", type=float, default=0.0002)
    parser.add_argument("--taker-fee-rate", type=float, default=0.0004)
    parser.add_argument(
        "--mainnet-usdc-fees",
        action="store_true",
        help="Use mainnet USDC-pair economics: maker 0, taker 0.0004.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    _validate_args(parser, args)

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=args.hours)
    fetch_start = start - timedelta(hours=args.warmup_hours)
    symbol = args.symbol.upper()
    candles = fetch_klines(
        DEFAULT_BASE_URL,
        symbol,
        "1m",
        int(fetch_start.timestamp() * 1000),
        int(end.timestamp() * 1000),
    )
    if not candles:
        raise RuntimeError(f"No 1m candles fetched for {symbol}.")

    rows = _run_sweep(
        candles,
        start_time_ms=int(start.timestamp() * 1000),
        symbol=symbol,
        maker_fee_rate=0.0 if args.mainnet_usdc_fees else args.maker_fee_rate,
        taker_fee_rate=args.taker_fee_rate,
    )
    rows.sort(key=_rank_key, reverse=True)
    top_rows = [_ranked(row, rank) for rank, row in enumerate(rows[: args.top], start=1)]

    payload = {
        "symbol": symbol,
        "window_utc": {
            "warmup_start": fetch_start.isoformat(),
            "start": start.isoformat(),
            "end": end.isoformat(),
        },
        "candles": len(candles),
        "sweep_count": len(rows),
        "top": args.top,
        "results": top_rows,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _print_payload(payload)
    return 0


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.hours <= 0:
        parser.error("--hours must be greater than 0")
    if args.warmup_hours < 0:
        parser.error("--warmup-hours must be 0 or greater")
    if args.top <= 0:
        parser.error("--top must be greater than 0")


def _run_sweep(
    candles: list[Any],
    *,
    start_time_ms: int,
    symbol: str,
    maker_fee_rate: float,
    taker_fee_rate: float,
) -> list[dict[str, Any]]:
    base = live_base(symbol, maker_fee_rate=maker_fee_rate, taker_fee_rate=taker_fee_rate)
    rows: list[dict[str, Any]] = []
    for micro, reclaim, vwap in product(MICRO_CANDIDATES, RECLAIM_CANDIDATES, VWAP_CANDIDATES):
        overrides = {
            "legacy_5m_enabled": False,
            "micro_enabled": True,
            **micro,
            **reclaim,
            **vwap,
        }
        config = replace(ReplayConfig(), **overrides)
        config = replace(
            config,
            micro_entry_taker_fee_rate=taker_fee_rate,
            micro_take_profit_fee_rate=maker_fee_rate,
            micro_stop_taker_fee_rate=taker_fee_rate,
        )
        result = run_live_replay(
            candles,
            start_time_ms=start_time_ms,
            base=base,
            config=config,
        )
        rows.append(_row_from_result(result, overrides))
    return rows


def _row_from_result(result: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    summary = result["summary"]
    return {
        "avg_daily_pct": summary["avg_daily_pct"],
        "worst_day_pct": summary["worst_day_pct"],
        "best_day_pct": summary["best_day_pct"],
        "trades": summary["total_trades"],
        "pf": summary["profit_factor"],
        "target_hit_rate": summary["target_hit_rate_pct"],
        "mode_pnl": summary["mode_pnl_usdc"],
        "config": config,
    }


def _rank_key(row: dict[str, Any]) -> tuple[float, float, float, float, float, int]:
    pf = row["pf"] if row["pf"] is not None else -1.0
    return (
        row["avg_daily_pct"],
        row["worst_day_pct"],
        pf,
        row["target_hit_rate"],
        row["best_day_pct"],
        row["trades"],
    )


def _ranked(row: dict[str, Any], rank: int) -> dict[str, Any]:
    return {"rank": rank, **row}


def _print_payload(payload: dict[str, Any]) -> None:
    window = payload["window_utc"]
    print(
        f"symbol={payload['symbol']} "
        f"window={window['start']}..{window['end']} "
        f"warmup_start={window['warmup_start']}"
    )
    print(f"candles={payload['candles']} sweep_count={payload['sweep_count']} top={payload['top']}")
    for row in payload["results"]:
        print(
            f"#{row['rank']} "
            f"avg_daily_pct={_fmt(row['avg_daily_pct'])} "
            f"worst_day_pct={_fmt(row['worst_day_pct'])} "
            f"best_day_pct={_fmt(row['best_day_pct'])} "
            f"trades={row['trades']} "
            f"pf={_fmt(row['pf'])} "
            f"target_hit_rate={_fmt(row['target_hit_rate'])} "
            f"mode_pnl={row['mode_pnl']}"
        )
        print(f"   config={_compact_config(row['config'])}")


def _compact_config(config: dict[str, Any]) -> str:
    return ", ".join(f"{key}={value}" for key, value in sorted(config.items()))


def _fmt(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int):
        return str(value)
    return f"{value:.4f}"


if __name__ == "__main__":
    raise SystemExit(main())
