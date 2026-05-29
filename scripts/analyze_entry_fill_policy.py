"""Compare strict trend350 entry limits with small tolerance policies.

The script reads live testnet service logs, fetches 1m Binance Futures klines for
each pending entry window, and reports how many otherwise-missed entries would
have filled under each tolerance.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.gridbot.testnet.fill_policy import entry_limit_price, reward_pct_for_entry


LOG_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+\[[^\]]+\]\s+(?P<event>\S+)\s*(?P<fields>.*)$")
FIELD_RE = re.compile(r"(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>'[^']*'|\"[^\"]*\"|\S+)")


@dataclass(frozen=True)
class EntryEvent:
    created_ms: int
    created_iso: str
    symbol: str
    direction: str
    client_order_id: str
    order_id: str
    entry: float
    order_entry_price: float
    stop: float
    take_profit: float
    score: int
    ttl_bars: int


@dataclass(frozen=True)
class FillPolicyResult:
    tolerance_bps: float
    orders: int
    reward_blocked: int
    filled: int
    fill_rate_pct: float
    avg_extra_bps: float
    avg_miss_bps: float


def parse_log_entry_events(lines: Iterable[str], *, symbol: str, since: datetime | None = None) -> list[EntryEvent]:
    events: list[EntryEvent] = []
    for line in lines:
        match = LOG_RE.match(line.strip())
        if not match or match.group("event") not in {"testnet_trend350_entry_limit_placed", "testnet_router_entry_limit_placed"}:
            continue
        fields = _parse_fields(match.group("fields"))
        if fields.get("symbol") != symbol:
            continue
        created = datetime.strptime(match.group("ts"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if since is not None and created < since:
            continue
        entry = _float(fields.get("entry"))
        order_entry = _float(fields.get("order_entry_price"), entry)
        stop = _float(fields.get("stop"))
        take_profit = _float(fields.get("take_profit"))
        if entry <= 0 or order_entry <= 0:
            continue
        action = fields.get("action", "PLAN_LONG")
        events.append(
            EntryEvent(
                created_ms=int(created.timestamp() * 1000),
                created_iso=created.isoformat(),
                symbol=symbol,
                direction="short" if action == "PLAN_SHORT" else "long",
                client_order_id=fields.get("client_order_id", ""),
                order_id=fields.get("order_id", ""),
                entry=entry,
                order_entry_price=order_entry,
                stop=stop,
                take_profit=take_profit,
                score=int(_float(fields.get("score"))),
                ttl_bars=max(1, int(_float(fields.get("ttl_bars"), 8))),
            )
        )
    return events


def compare_entry_fill_policies(
    events: list[EntryEvent],
    candles_by_order: dict[str, list[list]],
    *,
    tolerance_bps_values: list[float],
    min_reward_pct: float,
) -> list[FillPolicyResult]:
    results: list[FillPolicyResult] = []
    for tolerance_bps in tolerance_bps_values:
        filled = 0
        reward_blocked = 0
        extra_bps_values: list[float] = []
        miss_bps_values: list[float] = []
        for event in events:
            adjusted_entry = entry_limit_price(
                event.direction,
                event.entry,
                event.stop,
                event.take_profit,
                tolerance_bps,
            )
            reward_pct = reward_pct_for_entry(adjusted_entry, event.take_profit, event.direction)
            if reward_pct < min_reward_pct:
                reward_blocked += 1
                continue
            extra_bps_values.append(abs(adjusted_entry - event.entry) / event.entry * 10_000)
            candles = candles_by_order.get(event.client_order_id, [])
            touched, miss_bps = _filled_or_miss_bps(event.direction, adjusted_entry, candles)
            if touched:
                filled += 1
            elif miss_bps is not None:
                miss_bps_values.append(miss_bps)
        usable = max(len(events) - reward_blocked, 0)
        results.append(
            FillPolicyResult(
                tolerance_bps=tolerance_bps,
                orders=len(events),
                reward_blocked=reward_blocked,
                filled=filled,
                fill_rate_pct=round(filled / usable * 100, 2) if usable else 0.0,
                avg_extra_bps=round(sum(extra_bps_values) / len(extra_bps_values), 3) if extra_bps_values else 0.0,
                avg_miss_bps=round(sum(miss_bps_values) / len(miss_bps_values), 3) if miss_bps_values else 0.0,
            )
        )
    return results


def fetch_order_candles(events: list[EntryEvent], *, symbol: str, interval_minutes: int, testnet: bool) -> dict[str, list[list]]:
    try:
        from binance.client import Client
    except ImportError as exc:  # pragma: no cover - exercised by CLI users.
        raise SystemExit("python-binance is required. Install dependencies with pip install -e .") from exc

    client = Client(os.environ.get("BINANCE_API_KEY", ""), os.environ.get("BINANCE_API_SECRET", ""), testnet=testnet)
    candles: dict[str, list[list]] = {}
    interval = Client.KLINE_INTERVAL_1MINUTE
    for event in events:
        end_ms = event.created_ms + event.ttl_bars * interval_minutes * 60_000
        candles[event.client_order_id] = client.futures_klines(
            symbol=symbol,
            interval=interval,
            startTime=event.created_ms,
            endTime=end_ms,
            limit=1000,
        )
    return candles


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare live trend350 entry fill policies from service logs.")
    parser.add_argument("--log-file", default="testnet/logs/service.log")
    parser.add_argument("--symbol", default="ETHUSDC")
    parser.add_argument("--hours", type=float, default=6.0)
    parser.add_argument("--tolerance-bps", default="0,5,10,15,25,50")
    parser.add_argument("--min-reward-pct", type=float, default=0.12)
    parser.add_argument("--kline-interval-minutes", type=int, default=5)
    parser.add_argument("--env-file", default="")
    parser.add_argument("--testnet", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.env_file:
        _load_env_file(Path(args.env_file))

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=args.hours)
    log_path = Path(args.log_file)
    events = parse_log_entry_events(log_path.read_text(encoding="utf-8", errors="ignore").splitlines(), symbol=args.symbol, since=since)
    tolerances = [_float(item.strip()) for item in args.tolerance_bps.split(",") if item.strip()]
    candles = fetch_order_candles(
        events,
        symbol=args.symbol,
        interval_minutes=args.kline_interval_minutes,
        testnet=args.testnet,
    )
    results = compare_entry_fill_policies(
        events,
        candles,
        tolerance_bps_values=tolerances,
        min_reward_pct=args.min_reward_pct,
    )
    payload = {
        "symbol": args.symbol,
        "hours": args.hours,
        "orders": len(events),
        "results": [asdict(result) for result in results],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        _print_table(payload)
    return 0


def _parse_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for match in FIELD_RE.finditer(text):
        value = match.group("value")
        if (value.startswith("'") and value.endswith("'")) or (value.startswith('"') and value.endswith('"')):
            value = value[1:-1]
        fields[match.group("key")] = value
    return fields


def _float(value: str | None, default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _filled_or_miss_bps(direction: str, entry: float, candles: list[list]) -> tuple[bool, float | None]:
    if not candles or entry <= 0:
        return False, None
    highs = [float(candle[2]) for candle in candles]
    lows = [float(candle[3]) for candle in candles]
    if direction == "short":
        max_high = max(highs)
        if max_high >= entry:
            return True, None
        return False, max(0.0, (entry - max_high) / entry * 10_000)
    min_low = min(lows)
    if min_low <= entry:
        return True, None
    return False, max(0.0, (min_low - entry) / entry * 10_000)


def _load_env_file(path: Path) -> None:
    if not path.exists():
        raise SystemExit(f"env file not found: {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _print_table(payload: dict) -> None:
    print(f"symbol={payload['symbol']} hours={payload['hours']} orders={payload['orders']}")
    print("tolerance_bps orders reward_blocked filled fill_rate_pct avg_extra_bps avg_miss_bps")
    for row in payload["results"]:
        print(
            f"{row['tolerance_bps']:>13g} "
            f"{row['orders']:>6} "
            f"{row['reward_blocked']:>14} "
            f"{row['filled']:>6} "
            f"{row['fill_rate_pct']:>13.2f} "
            f"{row['avg_extra_bps']:>13.3f} "
            f"{row['avg_miss_bps']:>12.3f}"
        )


if __name__ == "__main__":
    raise SystemExit(main())
