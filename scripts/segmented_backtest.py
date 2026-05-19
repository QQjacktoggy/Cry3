from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Sequence


@dataclass(frozen=True)
class Segment:
    index: int
    start_date: date
    end_date: date

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    @property
    def slug(self) -> str:
        return f"{self.index:03d}_{self.start_date.isoformat()}_{self.end_date.isoformat()}"


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    child_args = _clean_child_args(args.backtest_args)
    segments = build_segments(
        args.start_date,
        args.end_date,
        window_days=args.window_days,
        step_days=args.step_days,
        calendar_months=args.calendar_months,
    )
    output_dir = _resolve_output_dir(args, child_args)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        for command in _dry_run_commands(args, child_args, segments, output_dir):
            print(" ".join(command))
        return

    records = run_segments(args, child_args, segments, output_dir)
    write_summaries(records, output_dir)

    failed = [record for record in records if record.get("status") != "ok"]
    if failed and not args.continue_on_error:
        raise SystemExit(1)

    aggregate = aggregate_records(records)
    print(
        "segmented_backtest "
        f"segments={aggregate['completed_segments']}/{aggregate['segments']} "
        f"avg_day_pct={aggregate['avg_daily_return_pct']} "
        f"max_dd_pct={aggregate['max_drawdown_pct']} "
        f"summary={output_dir / 'summary.csv'}"
    )


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run scripts/backtest_signal.py in resumable date segments and aggregate the results."
    )
    parser.add_argument("--start-date", required=True, help="First date to backtest, YYYY-MM-DD.")
    parser.add_argument("--end-date", required=True, help="Last date to backtest, YYYY-MM-DD inclusive.")
    parser.add_argument("--window-days", type=int, default=30, help="Days per segment for fixed/rolling windows.")
    parser.add_argument("--step-days", type=int, default=None, help="Days to advance between windows. Defaults to window-days.")
    parser.add_argument("--calendar-months", action="store_true", help="Use calendar month segments instead of window-days.")
    parser.add_argument("--equity", type=float, default=200.0, help="Starting equity injected into each child run.")
    parser.add_argument(
        "--equity-mode",
        choices=["independent", "chained"],
        default="independent",
        help="independent resets each segment; chained passes ending equity into the next segment.",
    )
    parser.add_argument("--output-dir", help="Directory for segment JSON files and summary tables.")
    parser.add_argument("--label", help="Label used under testnet/results/segmented_backtests when output-dir is omitted.")
    parser.add_argument("--force", action="store_true", help="Re-run segments even if a result JSON already exists.")
    parser.add_argument("--continue-on-error", action="store_true", help="Keep running later segments after a failure.")
    parser.add_argument("--per-segment-timeout", type=int, default=900, help="Timeout per segment in seconds.")
    parser.add_argument("--python-executable", default=sys.executable)
    parser.add_argument(
        "--backtest-script",
        default=str(Path(__file__).with_name("backtest_signal.py")),
        help="Path to the backtest_signal.py script.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print child commands without running them.")
    parser.add_argument("backtest_args", nargs=argparse.REMAINDER, help="Arguments passed to backtest_signal.py after --.")
    return parser.parse_args(argv)


def build_segments(
    start_date: str | date,
    end_date: str | date,
    window_days: int = 30,
    step_days: int | None = None,
    calendar_months: bool = False,
) -> list[Segment]:
    start = _to_date(start_date)
    end = _to_date(end_date)
    if end < start:
        raise ValueError("--end-date must be on or after --start-date")
    if window_days <= 0:
        raise ValueError("--window-days must be positive")
    step = step_days if step_days is not None else window_days
    if step <= 0:
        raise ValueError("--step-days must be positive")

    segments: list[Segment] = []
    cursor = start
    index = 1
    while cursor <= end:
        if calendar_months:
            segment_end = min(_month_end(cursor), end)
            next_cursor = segment_end + timedelta(days=1)
        else:
            segment_end = min(cursor + timedelta(days=window_days - 1), end)
            next_cursor = cursor + timedelta(days=step)
        segments.append(Segment(index, cursor, segment_end))
        cursor = next_cursor
        index += 1
    return segments


def run_segments(
    args: argparse.Namespace,
    child_args: Sequence[str],
    segments: Sequence[Segment],
    output_dir: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[dict]:
    records: list[dict] = []
    next_equity = args.equity
    for segment in segments:
        output_path = output_dir / f"{segment.slug}.json"
        if output_path.exists() and not args.force:
            record = json.loads(output_path.read_text(encoding="utf-8"))
            records.append(record)
            if args.equity_mode == "chained" and record.get("status") == "ok":
                next_equity = float(record["summary"]["equity_end_usdc"])
            continue

        equity = next_equity if args.equity_mode == "chained" else args.equity
        command = build_child_command(args, child_args, segment, equity)
        record = run_one_segment(segment, command, output_path, args.per_segment_timeout, runner)
        records.append(record)
        if record.get("status") != "ok":
            if not args.continue_on_error:
                break
            continue
        if args.equity_mode == "chained":
            next_equity = float(record["summary"]["equity_end_usdc"])
    return records


def build_child_command(
    args: argparse.Namespace,
    child_args: Sequence[str],
    segment: Segment,
    equity: float,
) -> list[str]:
    return [
        args.python_executable,
        args.backtest_script,
        *child_args,
        "--start-date",
        segment.start_date.isoformat(),
        "--end-date",
        segment.end_date.isoformat(),
        "--equity",
        _format_float(equity),
        "--json",
    ]


def run_one_segment(
    segment: Segment,
    command: Sequence[str],
    output_path: Path,
    timeout_seconds: int,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict:
    try:
        completed = runner(
            list(command),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        record = _failure_record(segment, command, "timeout", _exc_text(exc))
        _write_record(output_path, record)
        return record

    if completed.returncode != 0:
        record = _failure_record(segment, command, "failed", completed.stderr or completed.stdout)
        _write_record(output_path, record)
        return record

    try:
        payload = _parse_json_payload(completed.stdout)
        summary = summarize_payload(segment, payload)
        record = {
            "status": "ok",
            "segment": _segment_payload(segment),
            "summary": summary,
            "payload": payload,
            "command": list(command),
            "generated_at": _now_iso(),
        }
    except Exception as exc:  # pragma: no cover - defensive, exercised by integration use.
        record = _failure_record(segment, command, "parse_error", str(exc))
    _write_record(output_path, record)
    return record


def summarize_payload(segment: Segment, payload: dict) -> dict:
    summary = payload.get("summary", {})
    equity_start = float(summary.get("equity_usdc", 0.0))
    net_pnl = float(summary.get("net_pnl_usdc", 0.0))
    return {
        "segment_index": segment.index,
        "start_date": segment.start_date.isoformat(),
        "end_date": segment.end_date.isoformat(),
        "days": segment.days,
        "symbol": summary.get("symbol", ""),
        "equity_start_usdc": round(equity_start, 4),
        "equity_end_usdc": round(equity_start + net_pnl, 4),
        "total_trades": int(summary.get("total_trades", 0)),
        "net_pnl_usdc": round(net_pnl, 4),
        "return_pct": float(summary.get("return_pct", 0.0)),
        "max_drawdown_pct": float(summary.get("max_drawdown_pct", 0.0)),
        "win_rate_pct": float(summary.get("win_rate_pct", 0.0)),
        "profit_factor": summary.get("profit_factor", 0.0),
        "expectancy_usdc": float(summary.get("expectancy_usdc", 0.0)),
        "max_consecutive_losses": int(summary.get("max_consecutive_losses", 0)),
        "avg_daily_return_pct": float(summary.get("avg_daily_return_pct", 0.0)),
        "best_day_usdc": float(summary.get("best_day_usdc", 0.0)),
        "worst_day_usdc": float(summary.get("worst_day_usdc", 0.0)),
        "target_hit_rate_pct": float(summary.get("daily_target_4pct_hit_rate_pct", 0.0)),
    }


def write_summaries(records: Sequence[dict], output_dir: Path) -> None:
    rows = [_summary_row(record) for record in records]
    csv_path = output_dir / "summary.csv"
    fieldnames = [
        "segment_index",
        "start_date",
        "end_date",
        "days",
        "status",
        "symbol",
        "equity_start_usdc",
        "equity_end_usdc",
        "total_trades",
        "net_pnl_usdc",
        "return_pct",
        "max_drawdown_pct",
        "avg_daily_return_pct",
        "target_hit_rate_pct",
        "win_rate_pct",
        "profit_factor",
        "expectancy_usdc",
        "max_consecutive_losses",
        "best_day_usdc",
        "worst_day_usdc",
        "error",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})

    summary_json = {
        "generated_at": _now_iso(),
        "aggregate": aggregate_records(records),
        "segments": rows,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary_json, ensure_ascii=False, indent=2), encoding="utf-8")


def aggregate_records(records: Sequence[dict]) -> dict:
    ok = [record for record in records if record.get("status") == "ok"]
    total_days = sum(int(record["summary"].get("days", 0)) for record in ok)
    if total_days > 0:
        weighted_avg_daily = sum(
            float(record["summary"].get("avg_daily_return_pct", 0.0)) * int(record["summary"].get("days", 0))
            for record in ok
        ) / total_days
    else:
        weighted_avg_daily = 0.0
    drawdowns = [float(record["summary"].get("max_drawdown_pct", 0.0)) for record in ok]
    returns = [float(record["summary"].get("return_pct", 0.0)) for record in ok]
    return {
        "segments": len(records),
        "completed_segments": len(ok),
        "failed_segments": len(records) - len(ok),
        "total_days": total_days,
        "total_net_pnl_usdc": round(sum(float(record["summary"].get("net_pnl_usdc", 0.0)) for record in ok), 4),
        "avg_daily_return_pct": round(weighted_avg_daily, 4),
        "max_drawdown_pct": round(min(drawdowns), 4) if drawdowns else 0.0,
        "best_segment_return_pct": round(max(returns), 4) if returns else 0.0,
        "worst_segment_return_pct": round(min(returns), 4) if returns else 0.0,
        "avg_target_hit_rate_pct": round(
            sum(float(record["summary"].get("target_hit_rate_pct", 0.0)) for record in ok) / len(ok), 4
        )
        if ok
        else 0.0,
    }


def _dry_run_commands(
    args: argparse.Namespace,
    child_args: Sequence[str],
    segments: Sequence[Segment],
    output_dir: Path,
) -> list[list[str]]:
    commands: list[list[str]] = []
    next_equity = args.equity
    for segment in segments:
        output_path = output_dir / f"{segment.slug}.json"
        if output_path.exists() and not args.force and args.equity_mode == "chained":
            record = json.loads(output_path.read_text(encoding="utf-8"))
            if record.get("status") == "ok":
                next_equity = float(record["summary"]["equity_end_usdc"])
        equity = next_equity if args.equity_mode == "chained" else args.equity
        commands.append(build_child_command(args, child_args, segment, equity))
    return commands


def _summary_row(record: dict) -> dict:
    if record.get("status") == "ok":
        return {"status": "ok", **record["summary"], "error": ""}
    segment = record.get("segment", {})
    return {
        "segment_index": segment.get("index", ""),
        "start_date": segment.get("start_date", ""),
        "end_date": segment.get("end_date", ""),
        "days": segment.get("days", ""),
        "status": record.get("status", "failed"),
        "error": record.get("error", ""),
    }


def _failure_record(segment: Segment, command: Sequence[str], status: str, error: str) -> dict:
    return {
        "status": status,
        "segment": _segment_payload(segment),
        "error": error[-4000:],
        "command": list(command),
        "generated_at": _now_iso(),
    }


def _write_record(output_path: Path, record: dict) -> None:
    output_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_json_payload(stdout: str) -> dict:
    text = stdout.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            raise
        return json.loads(text[start : end + 1])


def _resolve_output_dir(args: argparse.Namespace, child_args: Sequence[str]) -> Path:
    if args.output_dir:
        return Path(args.output_dir)
    label = args.label or _default_label(args, child_args)
    return Path("testnet") / "results" / "segmented_backtests" / label


def _default_label(args: argparse.Namespace, child_args: Sequence[str]) -> str:
    symbol = _arg_value(child_args, "--symbol") or _arg_value(child_args, "--symbols") or "symbols"
    mode = "monthly" if args.calendar_months else f"{args.window_days}d"
    step = args.step_days if args.step_days is not None else args.window_days
    return f"{symbol}_{args.start_date}_{args.end_date}_{mode}_step{step}"


def _arg_value(argv: Sequence[str], flag: str) -> str | None:
    for index, token in enumerate(argv):
        if token == flag and index + 1 < len(argv):
            return argv[index + 1]
        prefix = f"{flag}="
        if token.startswith(prefix):
            return token[len(prefix) :]
    return None


def _clean_child_args(argv: Sequence[str]) -> list[str]:
    cleaned = list(argv)
    if cleaned and cleaned[0] == "--":
        cleaned = cleaned[1:]
    return cleaned


def _segment_payload(segment: Segment) -> dict:
    payload = asdict(segment)
    payload["start_date"] = segment.start_date.isoformat()
    payload["end_date"] = segment.end_date.isoformat()
    payload["days"] = segment.days
    return payload


def _to_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _month_end(value: date) -> date:
    if value.month == 12:
        return date(value.year, 12, 31)
    return date(value.year, value.month + 1, 1) - timedelta(days=1)


def _format_float(value: float) -> str:
    text = f"{value:.8f}".rstrip("0").rstrip(".")
    return text if text else "0"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _exc_text(exc: subprocess.TimeoutExpired) -> str:
    parts = [f"Command timed out after {exc.timeout} seconds."]
    if exc.stdout:
        parts.append(str(exc.stdout))
    if exc.stderr:
        parts.append(str(exc.stderr))
    return "\n".join(parts)


if __name__ == "__main__":
    main()
