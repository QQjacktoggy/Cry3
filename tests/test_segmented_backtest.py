import json
import subprocess
import tempfile
import unittest
from argparse import Namespace
from datetime import date
from pathlib import Path

from scripts.segmented_backtest import (
    aggregate_records,
    build_child_command,
    build_segments,
    run_segments,
    write_summaries,
)


class TestSegmentedBacktest(unittest.TestCase):
    def test_builds_non_overlapping_segments(self):
        segments = build_segments("2026-01-01", "2026-01-20", window_days=7)

        self.assertEqual([(s.start_date, s.end_date) for s in segments], [
            (date(2026, 1, 1), date(2026, 1, 7)),
            (date(2026, 1, 8), date(2026, 1, 14)),
            (date(2026, 1, 15), date(2026, 1, 20)),
        ])

    def test_builds_rolling_segments(self):
        segments = build_segments("2026-01-01", "2026-01-15", window_days=7, step_days=3)

        self.assertEqual([(s.start_date, s.end_date) for s in segments[:4]], [
            (date(2026, 1, 1), date(2026, 1, 7)),
            (date(2026, 1, 4), date(2026, 1, 10)),
            (date(2026, 1, 7), date(2026, 1, 13)),
            (date(2026, 1, 10), date(2026, 1, 15)),
        ])

    def test_builds_calendar_month_segments(self):
        segments = build_segments("2026-01-15", "2026-03-03", calendar_months=True)

        self.assertEqual([(s.start_date, s.end_date) for s in segments], [
            (date(2026, 1, 15), date(2026, 1, 31)),
            (date(2026, 2, 1), date(2026, 2, 28)),
            (date(2026, 3, 1), date(2026, 3, 3)),
        ])

    def test_build_child_command_appends_segment_dates_equity_and_json(self):
        segment = build_segments("2026-01-01", "2026-01-07", window_days=7)[0]
        args = Namespace(python_executable="python", backtest_script="scripts/backtest_signal.py")

        command = build_child_command(args, ["--mode", "signal_journal", "--symbol", "ETHUSDC"], segment, 200.0)

        self.assertEqual(command[-7:], [
            "--start-date",
            "2026-01-01",
            "--end-date",
            "2026-01-07",
            "--equity",
            "200",
            "--json",
        ])

    def test_run_segments_writes_summary_files(self):
        payloads = [
            _payload("ETHUSDC", 200.0, 10.0, 5.0, -2.0, 1.0),
            _payload("ETHUSDC", 200.0, -4.0, -2.0, -6.0, -0.4),
        ]

        def runner(command, capture_output, text, timeout, check):
            del command, capture_output, text, timeout, check
            return subprocess.CompletedProcess([], 0, stdout=json.dumps(payloads.pop(0)), stderr="")

        args = Namespace(
            equity=200.0,
            equity_mode="independent",
            force=False,
            continue_on_error=False,
            per_segment_timeout=10,
            python_executable="python",
            backtest_script="scripts/backtest_signal.py",
        )
        segments = build_segments("2026-01-01", "2026-01-14", window_days=7)
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            records = run_segments(args, ["--mode", "signal_journal"], segments, output_dir, runner=runner)
            write_summaries(records, output_dir)

            self.assertEqual(len(records), 2)
            self.assertTrue((output_dir / "summary.csv").exists())
            self.assertTrue((output_dir / "summary.json").exists())
            aggregate = aggregate_records(records)

        self.assertEqual(aggregate["completed_segments"], 2)
        self.assertEqual(aggregate["total_net_pnl_usdc"], 6.0)
        self.assertEqual(aggregate["max_drawdown_pct"], -6.0)


def _payload(symbol, equity, pnl, return_pct, drawdown_pct, avg_daily_pct):
    return {
        "mode": "signal_journal",
        "summary": {
            "symbol": symbol,
            "equity_usdc": equity,
            "total_trades": 3,
            "net_pnl_usdc": pnl,
            "return_pct": return_pct,
            "max_drawdown_pct": drawdown_pct,
            "win_rate_pct": 66.67,
            "profit_factor": 2.0,
            "expectancy_usdc": pnl / 3,
            "max_consecutive_losses": 1,
            "avg_daily_return_pct": avg_daily_pct,
            "best_day_usdc": max(pnl, 0.0),
            "worst_day_usdc": min(pnl, 0.0),
            "daily_target_4pct_hit_rate_pct": 33.33,
        },
    }


if __name__ == "__main__":
    unittest.main()
