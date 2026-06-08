"""Wildcat S1-S5 research sidecar backtest.

This script is intentionally scoped to research/backtest usage. It keeps the
live S1-S5 strategy concepts from winrate_optimized_portfolio, but tests a
candidate-scored variant with tunable TP/SL, gates, cooldowns, and fee
assumptions.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.backtest_maker_scalp import calculate_ema, fetch_1m_klines
from scripts.backtest_multi_strategies import (
    calculate_bollinger_bands,
    calculate_donchian,
    calculate_macd,
    calculate_rsi,
    calculate_stochastic,
    calculate_supertrend,
)
from scripts.backtest_smart_scalp import calculate_atr, calculate_vwap

TAIPEI = ZoneInfo("Asia/Taipei")
STRATEGIES = ("S1_BB_RSI", "S2_SuperTrend", "S3_EMA_MACD", "S4_Donchian", "S5_Stoch")


@dataclass(frozen=True)
class WildcatParams:
    label: str
    s1_tp: float = 0.0007
    s1_sl: float = 0.0016
    s2_tp: float = 0.0018
    s2_sl: float = 0.0014
    s3_tp: float = 0.0016
    s3_sl: float = 0.0015
    s4_tp: float = 0.0024
    s4_sl: float = 0.0012
    s5_tp: float = 0.0012
    s5_sl: float = 0.0013
    s1_rsi_long_max: float = 32.0
    s1_rsi_short_min: float = 68.0
    s5_long_d_max: float = 24.0
    s5_short_d_min: float = 76.0
    range_edge_atr_margin: float = 0.0
    min_vol_ratio: float = 0.35
    strict_body_ratio: float = 0.18
    breakout_vol_ratio: float = 1.8
    breakout_body_ratio: float = 0.35
    breakout_atr_margin: float = 0.18
    cooldown_bars: int = 7
    max_holding_bars: int = 36
    entry_fee_rate: float = 0.0
    tp_exit_fee_rate: float = 0.0
    sl_exit_fee_rate: float = 0.0004
    time_bias: bool = True
    rolling_gate: bool = True
    adaptive_tp_sl: bool = True
    notional_usdc: float = 1000.0
    target_daily_usdc: float = 20.0
    leverage_options: tuple[int, ...] = (75, 100)
    enabled_strategies: tuple[str, ...] = STRATEGIES
    score_floor: float = 0.0
    max_open_positions: int = 1
    recovery_enabled: bool = False
    recovery_steps: int = 0
    recovery_trigger_pct: float = 0.0010
    recovery_notional_scale: float = 1.0
    recovery_tp_shrink: float = 0.65
    partial_exit_pct: float = 0.0
    partial_tp_pct: float = 0.0008
    daily_target_stop: bool = False
    daily_profit_target_usdc: float = 20.0
    daily_floor_lock_usdc: float = 20.0
    daily_giveback_usdc: float = 4.0
    catchup_enabled: bool = False
    catchup_start_hour: int = 0
    catchup_vwap_atr: float = 0.25
    catchup_rsi_long_max: float = 48.0
    catchup_rsi_short_min: float = 52.0
    rescue_hour: int = 16
    rescue_vwap_atr: float = 0.10
    rescue_rsi_long_max: float = 56.0
    rescue_rsi_short_min: float = 44.0
    allow_duplicate_layers: bool = False
    max_duplicate_layers: int = 1
    regime_guard_enabled: bool = False
    s1_max_trend_share_60: float = 0.62
    s1_max_vwap_slope_atr: float = 0.18
    s1_max_ema_spread_atr: float = 0.85
    loss_cluster_guard_enabled: bool = False
    loss_cluster_window_bars: int = 240
    loss_cluster_limit: int = 2
    loss_cluster_cooldown_bars: int = 90
    adverse_exit_enabled: bool = False
    adverse_exit_bars: int = 10
    adverse_exit_loss_pct: float = 0.0009
    # S2_SuperTrend strength gates (2026-06-08). Defaults 0/False = no extra
    # gate (existing presets unchanged). SuperTrend whipsaws in chop that the
    # loose ±0.06 slope classifier still labels up/down; these require a
    # *sustained, separated* trend and (optionally) a fresh cross instead of
    # the permissive every-pullback "continuation" entry.
    s2_min_trend_share_60: float = 0.0   # last-60-bar trending fraction floor
    s2_min_ema_spread_atr: float = 0.0   # |emaFast-emaSlow|/ATR floor
    s2_require_cross: bool = False        # drop continuation entries
    # Live-accuracy guards (2026-06-08). Defaults off so existing presets are
    # unchanged.  Set both to match the A-layer live bot behaviour.
    entry_trend_guard_slope: float = 0.0  # >0 blocks counter-trend entries (live uses 0.03)
    dca_regime_guard: bool = False        # True blocks DCA outside range or on opposing stoch cross


@dataclass
class Candidate:
    strategy: str
    side: str
    score: float
    tp_pct: float
    sl_pct: float
    reasons: list[str]


@dataclass
class Position:
    strategy: str
    side: str
    entry_time: datetime
    entry_index: int
    entry_price: float
    tp_price: float
    sl_price: float
    tp_pct: float
    sl_pct: float
    qty: float
    entry_fee: float
    notional_usdc: float
    dca_count: int = 0
    partial_taken: bool = False
    realized_pnl: float = 0.0
    realized_fees: float = 0.0


class RollingWinRate:
    def __init__(self, window: int = 18, floor: float = 0.36) -> None:
        self.window = window
        self.floor = floor
        self.results: list[bool] = []

    def record(self, win: bool) -> None:
        self.results.append(win)
        if len(self.results) > self.window:
            self.results.pop(0)

    def allow(self) -> bool:
        if len(self.results) < 8:
            return True
        return sum(self.results) / len(self.results) >= self.floor


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest/tune wildcat S1-S5 sidecar.")
    parser.add_argument("--symbol", default="ETHUSDC")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--run-30", action="store_true", help="Also run a 30 day validation with the best 7 day params.")
    parser.add_argument("--top-n", type=int, default=8)
    parser.add_argument("--target-daily-usdc", type=float, default=20.0)
    parser.add_argument("--leverage-options", default="75,100")
    parser.add_argument("--fee-profile", default="maker_all", choices=["maker_all", "maker_tp_taker_sl", "taker_entry_exit", "all"])
    parser.add_argument("--quick", action="store_true", help="Use a smaller recovery-focused search grid for fast iteration.")
    parser.add_argument(
        "--align-taipei-days",
        action="store_true",
        help="Backtest the last N complete Taiwan calendar days instead of a rolling now-minus-N-days window.",
    )
    parser.add_argument(
        "--focused-preset",
        choices=["wildcat_converged_v1", "wildcat_30d_balanced_v1", "wildcat_v2_regime_guard", "wildcat_v2_adverse_guard"],
        default=None,
        help="Search only local DD/weak-day refinements around a named preset.",
    )
    parser.add_argument("--preset", choices=["wildcat_converged_v1", "wildcat_30d_balanced_v1", "wildcat_v2_regime_guard", "wildcat_v2_adverse_guard", "wildcat_v2ag_fees", "wildcat_v3_trend", "wildcat_v3_trend_rr", "wildcat_v3_trend_filt", "wildcat_v3_trend_filt2", "wildcat_v3_trend_cross", "wildcat_v3_trend_cont", "wildcat_v3_s3", "wildcat_v3_s4", "wildcat_v3_s3s4", "wildcat_v2ag_guarded", "wildcat_v3_cross_guarded"], default=None, help="Run a fixed named wildcat preset instead of searching variants.")
    parser.add_argument("--json-output", default=None, help="Optional report path. Defaults to reports/wildcat_s1s5_<days>d.json")
    parser.add_argument("--dump-trades", action="store_true", help="Include all trades in the JSON artifact for deeper analysis.")
    args = parser.parse_args()

    leverages = tuple(int(x.strip()) for x in args.leverage_options.split(",") if x.strip())
    if args.preset:
        params = preset_params(args.preset, target_daily_usdc=args.target_daily_usdc, leverage_options=leverages)
        seven = run_single(
            args.symbol,
            args.days,
            params,
            fetch=True,
            align_taipei_days=args.align_taipei_days,
            include_trades=args.dump_trades,
        )
    elif args.focused_preset:
        seven = run_focused_research(
            args.symbol,
            args.days,
            args.focused_preset,
            top_n=args.top_n,
            target_daily_usdc=args.target_daily_usdc,
            leverage_options=leverages,
            align_taipei_days=args.align_taipei_days,
            include_trades=args.dump_trades,
        )
    else:
        seven = run_research(
            args.symbol,
            args.days,
            top_n=args.top_n,
            target_daily_usdc=args.target_daily_usdc,
            leverage_options=leverages,
            fee_profile=args.fee_profile,
            quick=args.quick,
            align_taipei_days=args.align_taipei_days,
            include_trades=args.dump_trades,
        )
    payload = {"mode": "wildcat_s1s5", "symbol": args.symbol, "runs": [seven]}

    if args.run_30:
        best = WildcatParams(**seven["best"]["params"])
        try:
            thirty = run_single(
                args.symbol,
                30,
                best,
                fetch=True,
                align_taipei_days=args.align_taipei_days,
                include_trades=args.dump_trades,
            )
            payload["runs"].append(thirty)
        except Exception as exc:  # noqa: BLE001 - keep the 7d research artifact.
            payload["thirty_day_error"] = f"{type(exc).__name__}: {exc}"

    output = Path(args.json_output or f"reports/wildcat_s1s5_{args.days}d.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print_report(payload, output)


def run_research(
    symbol: str,
    days: int,
    top_n: int = 8,
    target_daily_usdc: float = 20.0,
    leverage_options: tuple[int, ...] = (75, 100),
    fee_profile: str = "maker_all",
    quick: bool = False,
    align_taipei_days: bool = False,
    include_trades: bool = False,
) -> dict:
    candles = fetch_wildcat_klines(symbol, days=days, align_taipei_days=align_taipei_days)
    features = build_features(candles)
    variants = build_variants(
        target_daily_usdc=target_daily_usdc,
        leverage_options=leverage_options,
        fee_profile=fee_profile,
        quick=quick,
    )
    ranked = []
    for params in variants:
        result = run_backtest(candles, params, symbol, days, features=features)
        ranked.append(result)
    ranked.sort(key=lambda row: row["rank_score"], reverse=True)
    return {
        "days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "variant_count": len(variants),
        "best": ranked[0],
        "top": ranked[:top_n],
    }


def run_single(
    symbol: str,
    days: int,
    params: WildcatParams,
    fetch: bool = True,
    align_taipei_days: bool = False,
    include_trades: bool = False,
) -> dict:
    candles = fetch_wildcat_klines(symbol, days=days, align_taipei_days=align_taipei_days) if fetch else []
    features = build_features(candles) if candles else None
    return {
        "days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "variant_count": 1,
        "best": run_backtest(candles, params, symbol, days, features=features, include_trades=include_trades),
        "top": [],
    }


def run_focused_research(
    symbol: str,
    days: int,
    preset_name: str,
    top_n: int = 8,
    target_daily_usdc: float = 20.0,
    leverage_options: tuple[int, ...] = (75, 100),
    align_taipei_days: bool = False,
    include_trades: bool = False,
) -> dict:
    candles = fetch_wildcat_klines(symbol, days=days, align_taipei_days=align_taipei_days)
    features = build_features(candles)
    base = preset_params(preset_name, target_daily_usdc=target_daily_usdc, leverage_options=leverage_options)
    variants = build_local_variants(base)
    ranked = []
    for params in variants:
        result = run_backtest(candles, params, symbol, days, features=features, include_trades=include_trades)
        ranked.append(result)
    ranked.sort(key=lambda row: row["rank_score"], reverse=True)
    return {
        "days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "variant_count": len(variants),
        "focused_preset": preset_name,
        "best": ranked[0],
        "top": ranked[:top_n],
    }


def fetch_wildcat_klines(symbol: str, days: int, align_taipei_days: bool = False) -> list[dict]:
    if not align_taipei_days:
        return fetch_1m_klines(symbol, days=days)
    end_local = datetime.now(TAIPEI).replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = end_local - timedelta(days=days)
    return fetch_1m_klines(
        symbol,
        days=days,
        start_dt=start_local.astimezone(timezone.utc),
        end_dt=end_local.astimezone(timezone.utc),
    )


def preset_params(
    name: str,
    target_daily_usdc: float = 20.0,
    leverage_options: tuple[int, ...] = (75, 100),
) -> WildcatParams:
    presets = {
        "wildcat_converged_v1": WildcatParams(
            label="wildcat_converged_v1",
            s1_tp=0.0012,
            s1_sl=0.0018,
            s2_tp=0.0028,
            s2_sl=0.0015,
            s3_tp=0.0026,
            s3_sl=0.0016,
            s4_tp=0.0035,
            s4_sl=0.0013,
            s5_tp=0.0018,
            s5_sl=0.0014,
            s1_rsi_long_max=38.0,
            s1_rsi_short_min=62.0,
            s5_long_d_max=36.0,
            s5_short_d_min=64.0,
            range_edge_atr_margin=0.25,
            min_vol_ratio=0.22,
            strict_body_ratio=0.12,
            breakout_vol_ratio=1.25,
            breakout_body_ratio=0.24,
            breakout_atr_margin=0.08,
            cooldown_bars=3,
            max_holding_bars=24,
            entry_fee_rate=0.0,
            tp_exit_fee_rate=0.0,
            sl_exit_fee_rate=0.0,
            target_daily_usdc=target_daily_usdc,
            leverage_options=leverage_options,
            enabled_strategies=("S1_BB_RSI", "S5_Stoch"),
            score_floor=0.0,
            max_open_positions=2,
            recovery_enabled=True,
            recovery_steps=3,
            recovery_trigger_pct=0.0009,
            recovery_notional_scale=1.0,
            recovery_tp_shrink=0.45,
            partial_exit_pct=0.35,
            partial_tp_pct=0.0006,
            daily_target_stop=True,
            daily_profit_target_usdc=40.0,
            daily_floor_lock_usdc=22.0,
            daily_giveback_usdc=6.0,
            catchup_enabled=True,
            catchup_start_hour=12,
            catchup_vwap_atr=0.18,
            catchup_rsi_long_max=52.0,
            catchup_rsi_short_min=48.0,
            rescue_hour=14,
            rescue_vwap_atr=0.06,
            rescue_rsi_long_max=60.0,
            rescue_rsi_short_min=40.0,
            allow_duplicate_layers=True,
            max_duplicate_layers=2,
        ),
        "wildcat_30d_balanced_v1": WildcatParams(
            label="wildcat_30d_balanced_v1",
            s1_tp=0.0012,
            s1_sl=0.0018,
            s2_tp=0.0028,
            s2_sl=0.0015,
            s3_tp=0.0026,
            s3_sl=0.0016,
            s4_tp=0.0035,
            s4_sl=0.0013,
            s5_tp=0.0018,
            s5_sl=0.0014,
            s1_rsi_long_max=38.0,
            s1_rsi_short_min=62.0,
            s5_long_d_max=31.0,
            s5_short_d_min=69.0,
            range_edge_atr_margin=0.20,
            min_vol_ratio=0.22,
            strict_body_ratio=0.12,
            breakout_vol_ratio=1.25,
            breakout_body_ratio=0.24,
            breakout_atr_margin=0.08,
            cooldown_bars=5,
            max_holding_bars=20,
            entry_fee_rate=0.0,
            tp_exit_fee_rate=0.0,
            sl_exit_fee_rate=0.0,
            target_daily_usdc=target_daily_usdc,
            leverage_options=leverage_options,
            enabled_strategies=("S1_BB_RSI", "S5_Stoch"),
            score_floor=0.0,
            max_open_positions=2,
            recovery_enabled=True,
            recovery_steps=3,
            recovery_trigger_pct=0.0009,
            recovery_notional_scale=1.0,
            recovery_tp_shrink=0.45,
            partial_exit_pct=0.40,
            partial_tp_pct=0.0005,
            daily_target_stop=True,
            daily_profit_target_usdc=40.0,
            daily_floor_lock_usdc=24.0,
            daily_giveback_usdc=4.0,
            catchup_enabled=True,
            catchup_start_hour=12,
            catchup_vwap_atr=0.18,
            catchup_rsi_long_max=52.0,
            catchup_rsi_short_min=48.0,
            rescue_hour=14,
            rescue_vwap_atr=0.06,
            rescue_rsi_long_max=60.0,
            rescue_rsi_short_min=40.0,
            allow_duplicate_layers=False,
            max_duplicate_layers=1,
        ),
        "wildcat_v2_regime_guard": WildcatParams(
            label="wildcat_v2_regime_guard",
            s1_tp=0.0012,
            s1_sl=0.0018,
            s2_tp=0.0028,
            s2_sl=0.0015,
            s3_tp=0.0026,
            s3_sl=0.0016,
            s4_tp=0.0035,
            s4_sl=0.0013,
            s5_tp=0.0018,
            s5_sl=0.0014,
            s1_rsi_long_max=38.0,
            s1_rsi_short_min=62.0,
            s5_long_d_max=31.0,
            s5_short_d_min=69.0,
            range_edge_atr_margin=0.20,
            min_vol_ratio=0.22,
            strict_body_ratio=0.12,
            breakout_vol_ratio=1.25,
            breakout_body_ratio=0.24,
            breakout_atr_margin=0.08,
            cooldown_bars=5,
            max_holding_bars=20,
            entry_fee_rate=0.0,
            tp_exit_fee_rate=0.0,
            sl_exit_fee_rate=0.0,
            target_daily_usdc=target_daily_usdc,
            leverage_options=leverage_options,
            enabled_strategies=("S1_BB_RSI", "S5_Stoch"),
            score_floor=0.0,
            max_open_positions=2,
            recovery_enabled=True,
            recovery_steps=3,
            recovery_trigger_pct=0.0009,
            recovery_notional_scale=1.0,
            recovery_tp_shrink=0.45,
            partial_exit_pct=0.40,
            partial_tp_pct=0.0005,
            daily_target_stop=True,
            daily_profit_target_usdc=40.0,
            daily_floor_lock_usdc=24.0,
            daily_giveback_usdc=4.0,
            catchup_enabled=True,
            catchup_start_hour=12,
            catchup_vwap_atr=0.18,
            catchup_rsi_long_max=52.0,
            catchup_rsi_short_min=48.0,
            rescue_hour=14,
            rescue_vwap_atr=0.06,
            rescue_rsi_long_max=60.0,
            rescue_rsi_short_min=40.0,
            allow_duplicate_layers=False,
            max_duplicate_layers=1,
            regime_guard_enabled=True,
            s1_max_trend_share_60=0.58,
            s1_max_vwap_slope_atr=0.14,
            s1_max_ema_spread_atr=0.72,
            loss_cluster_guard_enabled=True,
            loss_cluster_window_bars=240,
            loss_cluster_limit=2,
            loss_cluster_cooldown_bars=100,
            adverse_exit_enabled=True,
            adverse_exit_bars=10,
            adverse_exit_loss_pct=0.0008,
        ),
        "wildcat_v2_adverse_guard": WildcatParams(
            label="wildcat_v2_adverse_guard",
            s1_tp=0.0012,
            s1_sl=0.0018,
            s2_tp=0.0028,
            s2_sl=0.0015,
            s3_tp=0.0026,
            s3_sl=0.0016,
            s4_tp=0.0035,
            s4_sl=0.0013,
            s5_tp=0.0018,
            s5_sl=0.0014,
            s1_rsi_long_max=38.0,
            s1_rsi_short_min=62.0,
            s5_long_d_max=31.0,
            s5_short_d_min=69.0,
            range_edge_atr_margin=0.20,
            min_vol_ratio=0.22,
            strict_body_ratio=0.12,
            breakout_vol_ratio=1.25,
            breakout_body_ratio=0.24,
            breakout_atr_margin=0.08,
            cooldown_bars=5,
            max_holding_bars=20,
            entry_fee_rate=0.0,
            tp_exit_fee_rate=0.0,
            sl_exit_fee_rate=0.0,
            target_daily_usdc=target_daily_usdc,
            leverage_options=leverage_options,
            enabled_strategies=("S1_BB_RSI", "S5_Stoch"),
            score_floor=0.0,
            max_open_positions=2,
            recovery_enabled=True,
            recovery_steps=3,
            recovery_trigger_pct=0.0009,
            recovery_notional_scale=1.0,
            recovery_tp_shrink=0.45,
            partial_exit_pct=0.40,
            partial_tp_pct=0.0005,
            daily_target_stop=True,
            daily_profit_target_usdc=40.0,
            daily_floor_lock_usdc=24.0,
            daily_giveback_usdc=4.0,
            catchup_enabled=True,
            catchup_start_hour=12,
            catchup_vwap_atr=0.18,
            catchup_rsi_long_max=52.0,
            catchup_rsi_short_min=48.0,
            rescue_hour=14,
            rescue_vwap_atr=0.06,
            rescue_rsi_long_max=60.0,
            rescue_rsi_short_min=40.0,
            allow_duplicate_layers=False,
            max_duplicate_layers=1,
            adverse_exit_enabled=True,
            adverse_exit_bars=10,
            adverse_exit_loss_pct=0.0007,
        ),
    }
    # --- B/C experiment presets (2026-06-08) -------------------------------
    # Derived from the live wildcat_v2_adverse_guard so the live preset is left
    # untouched. They model REALISTIC fees (maker entry/TP = 0, taker SL =
    # 0.0004) because the live bleed was fee/slippage-driven, which the
    # zero-fee baseline hides.
    _v2ag = presets["wildcat_v2_adverse_guard"]
    _realistic_fees = {"entry_fee_rate": 0.0, "tp_exit_fee_rate": 0.0, "sl_exit_fee_rate": 0.0004}
    # Baseline with fees (apples-to-apples control for the variants below).
    presets["wildcat_v2ag_fees"] = replace(
        _v2ag, label="wildcat_v2ag_fees", **_realistic_fees
    )
    # C1: enable S2_SuperTrend so a sustained drop is traded WITH the trend
    # (short) instead of only mean-reverted (the falling-knife longs of Run 4/5).
    presets["wildcat_v3_trend"] = replace(
        _v2ag,
        label="wildcat_v3_trend",
        enabled_strategies=("S1_BB_RSI", "S5_Stoch", "S2_SuperTrend"),
        **_realistic_fees,
    )
    # B1: as v3_trend, plus rebalance S1 risk/reward to >= 1:1 (tp 0.0012 ->
    # 0.0018 to match the 0.0018 sl) so a win covers a loss after taker SL fee.
    presets["wildcat_v3_trend_rr"] = replace(
        _v2ag,
        label="wildcat_v3_trend_rr",
        enabled_strategies=("S1_BB_RSI", "S5_Stoch", "S2_SuperTrend"),
        s1_tp=0.0018,
        **_realistic_fees,
    )
    # C1 salvage: S2 with strength gates so it only fires in sustained, separated
    # trends (cuts the chop whipsaw that made naive S2 lose over 30d).
    presets["wildcat_v3_trend_filt"] = replace(
        _v2ag,
        label="wildcat_v3_trend_filt",
        enabled_strategies=("S1_BB_RSI", "S5_Stoch", "S2_SuperTrend"),
        s2_min_trend_share_60=0.5,
        s2_min_ema_spread_atr=0.5,
        s2_require_cross=True,
        **_realistic_fees,
    )
    # Cross-only (drop the permissive continuation entries) with light gates —
    # isolates whether the whipsaw was mostly the continuation branch.
    presets["wildcat_v3_trend_cross"] = replace(
        _v2ag,
        label="wildcat_v3_trend_cross",
        enabled_strategies=("S1_BB_RSI", "S5_Stoch", "S2_SuperTrend"),
        s2_min_trend_share_60=0.4,
        s2_min_ema_spread_atr=0.3,
        s2_require_cross=True,
        **_realistic_fees,
    )
    # Continuation allowed but only in strong/separated trends (no cross req).
    presets["wildcat_v3_trend_cont"] = replace(
        _v2ag,
        label="wildcat_v3_trend_cont",
        enabled_strategies=("S1_BB_RSI", "S5_Stoch", "S2_SuperTrend"),
        s2_min_trend_share_60=0.55,
        s2_min_ema_spread_atr=0.7,
        s2_require_cross=False,
        **_realistic_fees,
    )
    # Stricter still — fresh cross + very strong/separated trend only.
    presets["wildcat_v3_trend_filt2"] = replace(
        _v2ag,
        label="wildcat_v3_trend_filt2",
        enabled_strategies=("S1_BB_RSI", "S5_Stoch", "S2_SuperTrend"),
        s2_min_trend_share_60=0.65,
        s2_min_ema_spread_atr=0.9,
        s2_require_cross=True,
        **_realistic_fees,
    )
    # --- Live-accuracy guarded presets (2026-06-08) --------------------------
    # Adds A1 entry-trend guard (slope_block=0.03) to block counter-trend
    # entries, mirroring the live bot's evaluate_entry_trend_guard.
    # NOTE: dca_regime_guard is intentionally NOT enabled here. The live bot
    # calls evaluate_dca_guard only ONCE per DCA event (single-shot decision),
    # but the backtest's maybe_recover_position runs on EVERY bar — enabling
    # the guard in the backtest blocks virtually all DCA and cascades into a
    # catastrophic performance drop via cooldown buildup. Until the backtest
    # has single-shot DCA timing, only the entry guard is modelled.
    presets["wildcat_v2ag_guarded"] = replace(
        _v2ag,
        label="wildcat_v2ag_guarded",
        entry_trend_guard_slope=0.03,
        **_realistic_fees,
    )
    # Same guard applied to the best S2 rescue preset.
    presets["wildcat_v3_cross_guarded"] = replace(
        presets["wildcat_v3_trend_cross"],
        label="wildcat_v3_cross_guarded",
        entry_trend_guard_slope=0.03,
    )
    # --- S3/S4 evaluation presets (2026-06-08) --------------------------------
    # S3_EMA_MACD: EMA pullback + MACD flip in trending regime (trend pullback
    #   entry, fires when trend != range and price touches EMA slow + MACD
    #   crosses zero). Complements S1/S5 by capturing trend-pullback entries.
    # S4_Donchian: Donchian channel breakout with high volume / body confirmation
    #   (fires on momentum breakout, independent of trend regime).
    presets["wildcat_v3_s3"] = replace(
        _v2ag,
        label="wildcat_v3_s3",
        enabled_strategies=("S1_BB_RSI", "S5_Stoch", "S3_EMA_MACD"),
        **_realistic_fees,
    )
    presets["wildcat_v3_s4"] = replace(
        _v2ag,
        label="wildcat_v3_s4",
        enabled_strategies=("S1_BB_RSI", "S5_Stoch", "S4_Donchian"),
        **_realistic_fees,
    )
    presets["wildcat_v3_s3s4"] = replace(
        _v2ag,
        label="wildcat_v3_s3s4",
        enabled_strategies=("S1_BB_RSI", "S5_Stoch", "S3_EMA_MACD", "S4_Donchian"),
        **_realistic_fees,
    )
    if name not in presets:
        raise ValueError(f"Unknown wildcat preset: {name}")
    preset = presets[name]
    return replace(preset, target_daily_usdc=target_daily_usdc, leverage_options=leverage_options)


def build_local_variants(base: WildcatParams) -> list[WildcatParams]:
    variants: list[WildcatParams] = [base]

    def add_variant(label: str, **changes: object) -> None:
        variants.append(
            WildcatParams(
                **{
                    **asdict(base),
                    **changes,
                    "label": f"{base.label}_{label}",
                }
            )
        )

    # DD tightening set.
    for recovery_steps, recovery_scale, recovery_tp_shrink in (
        (2, 0.85, 0.50),
        (2, 0.75, 0.55),
        (1, 0.75, 0.60),
    ):
        for partial_exit_pct, partial_tp_pct in ((0.45, 0.0005), (0.55, 0.0005), (0.50, 0.0006)):
            add_variant(
                f"ddtight_rec{recovery_steps}_scale{int(recovery_scale * 100)}_pt{int(partial_exit_pct * 100)}",
                recovery_steps=recovery_steps,
                recovery_notional_scale=recovery_scale,
                recovery_tp_shrink=recovery_tp_shrink,
                partial_exit_pct=partial_exit_pct,
                partial_tp_pct=partial_tp_pct,
                allow_duplicate_layers=False,
                max_duplicate_layers=1,
                daily_profit_target_usdc=36.0,
                daily_floor_lock_usdc=24.0,
                daily_giveback_usdc=4.0,
            )

    # Preserve avg while trimming giveback and opening catch-up a bit earlier.
    for catchup_start_hour, catchup_vwap_atr, rescue_hour in ((10, 0.16, 13), (11, 0.16, 13), (10, 0.18, 14)):
        for floor_lock, giveback in ((24.0, 4.0), (25.0, 4.0), (24.0, 5.0)):
            add_variant(
                f"floor{int(floor_lock)}_gb{int(giveback)}_catch{catchup_start_hour}_rescue{rescue_hour}",
                catchup_start_hour=catchup_start_hour,
                catchup_vwap_atr=catchup_vwap_atr,
                rescue_hour=rescue_hour,
                daily_profit_target_usdc=38.0,
                daily_floor_lock_usdc=floor_lock,
                daily_giveback_usdc=giveback,
                recovery_notional_scale=0.85,
                partial_exit_pct=0.45,
            )

    # Weak-day rescue set: a little more permissive catch-up, but keep DCA contained.
    for catchup_rsi_long_max, catchup_rsi_short_min, rescue_vwap_atr in ((54.0, 46.0, 0.05), (56.0, 44.0, 0.05)):
        add_variant(
            f"rescue_soft_rsi{int(catchup_rsi_long_max)}",
            recovery_steps=2,
            recovery_notional_scale=0.85,
            partial_exit_pct=0.50,
            partial_tp_pct=0.0005,
            daily_profit_target_usdc=36.0,
            daily_floor_lock_usdc=23.0,
            daily_giveback_usdc=4.0,
            catchup_start_hour=10,
            catchup_vwap_atr=0.16,
            catchup_rsi_long_max=catchup_rsi_long_max,
            catchup_rsi_short_min=catchup_rsi_short_min,
            rescue_hour=13,
            rescue_vwap_atr=rescue_vwap_atr,
            rescue_rsi_long_max=62.0,
            rescue_rsi_short_min=38.0,
            allow_duplicate_layers=False,
            max_duplicate_layers=1,
        )

    # A narrower-hold family can cut deep intraday slips without killing frequency too hard.
    for hold_bars, cooldown_bars in ((18, 4), (20, 4), (18, 5)):
        add_variant(
            f"hold{hold_bars}_cool{cooldown_bars}",
            max_holding_bars=hold_bars,
            cooldown_bars=cooldown_bars,
            recovery_steps=2,
            recovery_notional_scale=0.85,
            partial_exit_pct=0.45,
            daily_profit_target_usdc=36.0,
            daily_floor_lock_usdc=24.0,
            daily_giveback_usdc=4.0,
        )

    # Hybrid balanced set keeps duplicate layers but softens size and floor lock.
    for duplicate_layers, dup_cap, recovery_scale in ((True, 2, 0.85), (True, 2, 0.75), (False, 1, 1.0)):
        add_variant(
            f"hybrid_dup{dup_cap if duplicate_layers else 0}_scale{int(recovery_scale * 100)}",
            recovery_steps=2,
            recovery_notional_scale=recovery_scale,
            recovery_tp_shrink=0.50,
            partial_exit_pct=0.45,
            partial_tp_pct=0.0005,
            allow_duplicate_layers=duplicate_layers,
            max_duplicate_layers=dup_cap,
            daily_profit_target_usdc=38.0,
            daily_floor_lock_usdc=24.0,
            daily_giveback_usdc=5.0,
            catchup_start_hour=11,
            rescue_hour=13,
        )

    # Weak-day repair set: encourage a few more S5 reversion fills without opening the floodgates.
    for s5_long_d_max, s5_short_d_min, range_edge_atr_margin in ((38.0, 62.0, 0.18), (40.0, 60.0, 0.15)):
        for s1_sl, recovery_trigger_pct in ((0.0017, 0.0010), (0.00165, 0.0011)):
            add_variant(
                f"weakfix_s5_{int(s5_long_d_max)}_{int(s5_short_d_min)}_sl{int(s1_sl * 10000)}",
                s1_sl=s1_sl,
                s5_long_d_max=s5_long_d_max,
                s5_short_d_min=s5_short_d_min,
                range_edge_atr_margin=range_edge_atr_margin,
                strict_body_ratio=0.10,
                recovery_steps=2,
                recovery_trigger_pct=recovery_trigger_pct,
                recovery_notional_scale=0.85,
                recovery_tp_shrink=0.50,
                partial_exit_pct=0.40,
                partial_tp_pct=0.0005,
                daily_profit_target_usdc=38.0,
                daily_floor_lock_usdc=23.0,
                daily_giveback_usdc=5.0,
            )

    deduped: dict[str, WildcatParams] = {}
    for variant in variants:
        deduped[variant.label] = variant
    return list(deduped.values())


def build_variants(
    target_daily_usdc: float = 20.0,
    leverage_options: tuple[int, ...] = (75, 100),
    fee_profile: str = "maker_all",
    quick: bool = False,
) -> list[WildcatParams]:
    variants: list[WildcatParams] = []
    fee_profiles = {
        "maker_tp_taker_sl": (0.0, 0.0, 0.0004),
        "maker_all": (0.0, 0.0, 0.0),
        "taker_entry_exit": (0.0004, 0.0004, 0.0004),
    }
    if fee_profile != "all":
        fee_profiles = {fee_profile: fee_profiles[fee_profile]}
    tp_sl_sets = [
        (0.0006, 0.0015, 0.0016, 0.0014, 0.0015, 0.0015, 0.0022, 0.0011, 0.0011, 0.0013),
        (0.0007, 0.0016, 0.0018, 0.0014, 0.0016, 0.0015, 0.0024, 0.0012, 0.0012, 0.0013),
        (0.0008, 0.0018, 0.0020, 0.0015, 0.0018, 0.0016, 0.0026, 0.0013, 0.0013, 0.0014),
        (0.0010, 0.0017, 0.0024, 0.0014, 0.0022, 0.0015, 0.0030, 0.0012, 0.0016, 0.0013),
        (0.0012, 0.0018, 0.0028, 0.0015, 0.0026, 0.0016, 0.0035, 0.0013, 0.0018, 0.0014),
    ]
    if quick:
        tp_sl_sets = tp_sl_sets[3:]
    gate_sets = [
        (0.22, 0.12, 1.25, 0.24, 0.08, 3, 24),
        (0.30, 0.16, 1.6, 0.32, 0.15, 5, 30),
        (0.35, 0.18, 1.8, 0.35, 0.18, 7, 36),
        (0.45, 0.22, 2.1, 0.40, 0.24, 10, 48),
    ]
    if quick:
        gate_sets = [gate_sets[0], gate_sets[2]]
    strategy_sets = [
        STRATEGIES,
        ("S1_BB_RSI", "S5_Stoch"),
        ("S1_BB_RSI", "S3_EMA_MACD", "S4_Donchian"),
        ("S1_BB_RSI", "S4_Donchian"),
        ("S2_SuperTrend", "S4_Donchian"),
    ]
    if quick:
        strategy_sets = [
            ("S1_BB_RSI", "S5_Stoch"),
            ("S1_BB_RSI", "S4_Donchian"),
            ("S2_SuperTrend", "S4_Donchian"),
        ]
    score_floors = [0.0, 72.0, 78.0]
    if quick:
        score_floors = [0.0, 72.0]
    max_open_positions = [1, 2, 3, 4]
    if quick:
        max_open_positions = [1, 2, 3]
    recovery_sets = [
        (False, 0, 0.0010, 1.0, 0.65, 0.0, 0.0008),
        (True, 1, 0.0008, 1.0, 0.55, 0.50, 0.0006),
        (True, 1, 0.0012, 1.0, 0.65, 0.50, 0.0008),
        (True, 2, 0.0010, 1.0, 0.55, 0.50, 0.0007),
        (True, 2, 0.0014, 0.75, 0.65, 0.40, 0.0009),
        (True, 3, 0.0009, 1.0, 0.45, 0.35, 0.0006),
        (True, 3, 0.0012, 1.0, 0.55, 0.50, 0.0008),
    ]
    catchup_sets = [
        (False, False, 0, 0.25, 48.0, 52.0, 20.0, 20.0, 4.0, 16, 0.10, 56.0, 44.0),
        (True, True, 0, 0.20, 50.0, 50.0, 30.0, 20.0, 4.0, 16, 0.10, 56.0, 44.0),
        (True, True, 0, 0.20, 50.0, 50.0, 35.0, 20.0, 5.0, 15, 0.08, 58.0, 42.0),
        (True, True, 8, 0.25, 48.0, 52.0, 35.0, 20.0, 5.0, 14, 0.10, 56.0, 44.0),
        (True, True, 12, 0.18, 52.0, 48.0, 40.0, 22.0, 6.0, 14, 0.06, 60.0, 40.0),
    ]
    if quick:
        catchup_sets = catchup_sets[1:]
    duplicate_layer_sets = [(False, 1), (True, 2), (True, 3)]
    if not quick:
        duplicate_layer_sets = [(False, 1), (True, 2)]
    range_relax_sets = [
        (32.0, 68.0, 24.0, 76.0, 0.0),
        (35.0, 65.0, 30.0, 70.0, 0.12),
        (38.0, 62.0, 36.0, 64.0, 0.25),
    ]
    if quick:
        range_relax_sets = range_relax_sets[1:]

    for fee_label, fees in fee_profiles.items():
        for idx, tp_sl in enumerate(tp_sl_sets, start=1):
            for gate_idx, gates in enumerate(gate_sets, start=1):
                for strategies in strategy_sets:
                    set_label = "all" if strategies == STRATEGIES else "_".join(s.split("_", 1)[0].lower() for s in strategies)
                    for relax_idx, relax in enumerate(range_relax_sets, start=1):
                        for score_floor in score_floors:
                            for max_open in max_open_positions:
                                if max_open > 1 and strategies == STRATEGIES:
                                    # Keep the broad all-strategy router from becoming an unmanaged exposure bucket.
                                    continue
                                for rec_idx, recovery in enumerate(recovery_sets):
                                    if recovery[0] and max_open > 2:
                                        # Recovery already increases same-idea exposure; cap concurrent routes.
                                        continue
                                    for catch_idx, catchup in enumerate(catchup_sets):
                                        if catchup[0] and strategies not in {
                                            ("S1_BB_RSI", "S5_Stoch"),
                                            ("S1_BB_RSI", "S4_Donchian"),
                                        }:
                                            continue
                                        for dup_idx, duplicate_layers in enumerate(duplicate_layer_sets):
                                            if duplicate_layers[0] and max_open < duplicate_layers[1]:
                                                continue
                                            if duplicate_layers[0] and not catchup[0]:
                                                continue
                                            rec_label = "base" if not recovery[0] else f"rec{rec_idx}"
                                            catch_label = "plain" if not catchup[0] else f"catch{catch_idx}"
                                            dup_label = "dup0" if not duplicate_layers[0] else f"dup{duplicate_layers[1]}"
                                            variants.append(
                                                WildcatParams(
                                            label=(
                                                f"wildcat_{fee_label}_tp{idx}_gate{gate_idx}_relax{relax_idx}_"
                                                f"{set_label}_score{int(score_floor)}_max{max_open}_{rec_label}_{catch_label}_{dup_label}"
                                            ),
                                            s1_tp=tp_sl[0],
                                            s1_sl=tp_sl[1],
                                            s2_tp=tp_sl[2],
                                            s2_sl=tp_sl[3],
                                            s3_tp=tp_sl[4],
                                            s3_sl=tp_sl[5],
                                            s4_tp=tp_sl[6],
                                            s4_sl=tp_sl[7],
                                            s5_tp=tp_sl[8],
                                            s5_sl=tp_sl[9],
                                            s1_rsi_long_max=relax[0],
                                            s1_rsi_short_min=relax[1],
                                            s5_long_d_max=relax[2],
                                            s5_short_d_min=relax[3],
                                            range_edge_atr_margin=relax[4],
                                            min_vol_ratio=gates[0],
                                            strict_body_ratio=gates[1],
                                            breakout_vol_ratio=gates[2],
                                            breakout_body_ratio=gates[3],
                                            breakout_atr_margin=gates[4],
                                            cooldown_bars=gates[5],
                                            max_holding_bars=gates[6],
                                            entry_fee_rate=fees[0],
                                            tp_exit_fee_rate=fees[1],
                                            sl_exit_fee_rate=fees[2],
                                            target_daily_usdc=target_daily_usdc,
                                            leverage_options=leverage_options,
                                            enabled_strategies=strategies,
                                            score_floor=score_floor,
                                            max_open_positions=max_open,
                                            recovery_enabled=recovery[0],
                                            recovery_steps=recovery[1],
                                            recovery_trigger_pct=recovery[2],
                                            recovery_notional_scale=recovery[3],
                                            recovery_tp_shrink=recovery[4],
                                            partial_exit_pct=recovery[5],
                                            partial_tp_pct=recovery[6],
                                            daily_target_stop=catchup[1],
                                            daily_profit_target_usdc=catchup[6],
                                            daily_floor_lock_usdc=catchup[7],
                                            daily_giveback_usdc=catchup[8],
                                            catchup_enabled=catchup[0],
                                            catchup_start_hour=catchup[2],
                                            catchup_vwap_atr=catchup[3],
                                            catchup_rsi_long_max=catchup[4],
                                            catchup_rsi_short_min=catchup[5],
                                            rescue_hour=catchup[9],
                                            rescue_vwap_atr=catchup[10],
                                            rescue_rsi_long_max=catchup[11],
                                            rescue_rsi_short_min=catchup[12],
                                            allow_duplicate_layers=duplicate_layers[0],
                                            max_duplicate_layers=duplicate_layers[1],
                                        )
                                    )
    return variants


def run_backtest(
    candles: list[dict],
    params: WildcatParams,
    symbol: str,
    days: int,
    features: dict | None = None,
    include_trades: bool = False,
) -> dict:
    if len(candles) < 160:
        raise ValueError("Need at least 160 1m candles for wildcat S1-S5 backtest.")

    features = features or build_features(candles)
    rolling = {
        "S1_BB_RSI": RollingWinRate(20, 0.34),
        "S2_SuperTrend": RollingWinRate(16, 0.38),
        "S3_EMA_MACD": RollingWinRate(16, 0.38),
        "S4_Donchian": RollingWinRate(24, 0.30),
        "S5_Stoch": RollingWinRate(20, 0.34),
    }
    cooldown_until = {key: 0 for key in STRATEGIES}
    side_cooldown_until = {(strategy, side): 0 for strategy in STRATEGIES for side in ("LONG", "SHORT")}
    bad_exit_indices: dict[tuple[str, str], list[int]] = {(strategy, side): [] for strategy in STRATEGIES for side in ("LONG", "SHORT")}
    trades: list[dict] = []
    positions: list[Position] = []
    rejected: dict[str, int] = {}
    daily_pnl_state: dict[str, float] = {}
    daily_peak_state: dict[str, float] = {}

    for i in range(130, len(candles)):
        candle = candles[i]
        c_time = candle_time(candle)
        day_key = c_time.date().isoformat()

        next_positions: list[Position] = []
        for position in positions:
            exit_trade = maybe_exit(candle, c_time, i, position, params, features)
            if exit_trade is not None:
                trades.append(exit_trade)
                daily_pnl_state[day_key] = daily_pnl_state.get(day_key, 0.0) + exit_trade["pnl"]
                daily_peak_state[day_key] = max(daily_peak_state.get(day_key, 0.0), daily_pnl_state[day_key])
                rolling[position.strategy].record(exit_trade["pnl"] > 0)
                if exit_trade["exit_reason"] in {"SL", "MAX_HOLD_LOSS", "ADVERSE_EXIT"}:
                    cooldown_until[position.strategy] = i + params.cooldown_bars
                    if params.loss_cluster_guard_enabled:
                        key = (position.strategy, position.side)
                        window_start = i - params.loss_cluster_window_bars
                        bad_exit_indices[key] = [idx for idx in bad_exit_indices[key] if idx >= window_start]
                        bad_exit_indices[key].append(i)
                        if len(bad_exit_indices[key]) >= params.loss_cluster_limit:
                            side_cooldown_until[key] = i + params.loss_cluster_cooldown_bars
                            rejected["loss_cluster_cooldown_set"] = rejected.get("loss_cluster_cooldown_set", 0) + 1
            else:
                next_positions.append(position)
        positions = next_positions

        if len(positions) >= params.max_open_positions:
            continue
        day_pnl = daily_pnl_state.get(day_key, 0.0)
        day_peak = daily_peak_state.get(day_key, day_pnl)
        target_hit_stop = params.daily_target_stop and day_pnl >= params.daily_profit_target_usdc
        floor_lock_stop = (
            params.daily_target_stop
            and day_peak >= params.daily_floor_lock_usdc
            and day_pnl <= max(params.target_daily_usdc, day_peak - params.daily_giveback_usdc)
        )
        if target_hit_stop or floor_lock_stop:
            rejected["daily_target_stop"] = rejected.get("daily_target_stop", 0) + 1
            continue

        catchup = (
            params.catchup_enabled
            and day_pnl < params.daily_profit_target_usdc
            and c_time.hour >= params.catchup_start_hour
        )
        rescue = params.catchup_enabled and day_pnl < params.target_daily_usdc and c_time.hour >= params.rescue_hour
        candidates = build_candidates(candles, features, i, params, catchup=catchup, rescue=rescue)
        candidates = [row for row in candidates if row.strategy in params.enabled_strategies and row.score >= params.score_floor]
        candidates.sort(key=lambda row: row.score, reverse=True)
        for candidate in candidates:
            same_layer_count = sum(1 for pos in positions if pos.strategy == candidate.strategy and pos.side == candidate.side)
            max_same_layers = params.max_duplicate_layers if params.allow_duplicate_layers else 1
            if same_layer_count >= max_same_layers:
                rejected["duplicate_open"] = rejected.get("duplicate_open", 0) + 1
                continue
            if i < cooldown_until[candidate.strategy]:
                rejected["cooldown"] = rejected.get("cooldown", 0) + 1
                continue
            if i < side_cooldown_until[(candidate.strategy, candidate.side)]:
                rejected["loss_cluster_cooldown"] = rejected.get("loss_cluster_cooldown", 0) + 1
                continue
            if params.rolling_gate and not rolling[candidate.strategy].allow():
                rejected["rolling_wr"] = rejected.get("rolling_wr", 0) + 1
                continue
            positions.append(open_position(candle, c_time, i, candidate, params))
            break

    final = candles[-1]
    for position in positions:
        trades.append(force_exit(final, candle_time(final), len(candles) - 1, position, params, "EOD"))

    summary = summarize(trades, days, params)
    result = {
        "symbol": symbol,
        "days": days,
        "params": asdict(params),
        **summary,
        "rank_score": rank_score(summary),
        "rejected": rejected,
        "sample_trades": trades[-12:],
    }
    if include_trades:
        result["all_trades"] = trades
    return result


def build_features(candles: list[dict]) -> dict:
    prices = [c["close"] for c in candles]
    atr = calculate_atr(candles, 14)
    ema_fast = calculate_ema(prices, 5)
    ema_slow = calculate_ema(prices, 20)
    ema_trend = calculate_ema(prices, 50)
    ema_fast_5m = calculate_ema(prices, 60)
    ema_slow_5m = calculate_ema(prices, 130)
    volume_sma = calculate_ema([c["volume"] for c in candles], 20)
    bb_upper, _, bb_lower = calculate_bollinger_bands(prices, 20, 2.0)
    _, _, macd_hist = calculate_macd(prices, 12, 26, 9)
    stoch_k, stoch_d = calculate_stochastic(candles, 14, 3)
    donchian_upper, donchian_lower = calculate_donchian(candles, 20)
    supertrend, _ = calculate_supertrend(candles, 10, 3.0)
    vwap = calculate_vwap(candles)
    rsi = calculate_rsi(prices, 14)

    atr_pct = [0.5] * len(candles)
    for i in range(288, len(candles)):
        window = [x for x in atr[i - 287 : i + 1] if x is not None]
        current = atr[i]
        if window and current is not None:
            atr_pct[i] = sum(1 for x in window if x < current) / len(window)

    trend = ["range"] * len(candles)
    for i in range(50, len(candles)):
        atr_val = atr[i] if atr[i] and atr[i] > 0 else 1.0
        slope = (ema_slow[i] - ema_slow[i - 20]) / atr_val
        if prices[i] > ema_trend[i] and slope > 0.06:
            trend[i] = "up"
        elif prices[i] < ema_trend[i] and slope < -0.06:
            trend[i] = "down"

    vwap_slope_atr = [0.0] * len(candles)
    ema_spread_atr = [0.0] * len(candles)
    trend_share_60 = [0.0] * len(candles)
    for i in range(60, len(candles)):
        atr_val = atr[i] if atr[i] and atr[i] > 0 else 1.0
        vwap_slope_atr[i] = abs(vwap[i] - vwap[i - 20]) / atr_val
        ema_spread_atr[i] = abs(ema_fast[i] - ema_slow[i]) / atr_val
        recent_trends = trend[i - 59 : i + 1]
        trend_share_60[i] = sum(1 for state in recent_trends if state in {"up", "down"}) / len(recent_trends)

    return {
        "prices": prices,
        "atr": atr,
        "atr_pct": atr_pct,
        "trend": trend,
        "vwap_slope_atr": vwap_slope_atr,
        "ema_spread_atr": ema_spread_atr,
        "trend_share_60": trend_share_60,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "ema_trend": ema_trend,
        "ema_fast_5m": ema_fast_5m,
        "ema_slow_5m": ema_slow_5m,
        "volume_sma": volume_sma,
        "bb_upper": bb_upper,
        "bb_lower": bb_lower,
        "macd_hist": macd_hist,
        "stoch_k": stoch_k,
        "stoch_d": stoch_d,
        "donchian_upper": donchian_upper,
        "donchian_lower": donchian_lower,
        "supertrend": supertrend,
        "vwap": vwap,
        "rsi": rsi,
    }


def build_candidates(
    candles: list[dict],
    f: dict,
    i: int,
    params: WildcatParams,
    catchup: bool = False,
    rescue: bool = False,
) -> list[Candidate]:
    c = candles[i]
    price = c["close"]
    atr_val = f["atr"][i] if f["atr"][i] and f["atr"][i] > 0 else max(c["high"] - c["low"], 1.0)
    vol_ratio = c["volume"] / f["volume_sma"][i] if f["volume_sma"][i] > 0 else 0.0
    body_ratio = abs(c["close"] - c["open"]) / (c["high"] - c["low"]) if c["high"] > c["low"] else 0.0
    vol_state = volatility_state(f["atr_pct"][i])
    trend = f["trend"][i]
    mtf_bull = f["ema_fast_5m"][i] > f["ema_slow_5m"][i]
    mtf_bear = f["ema_fast_5m"][i] < f["ema_slow_5m"][i]
    hour_boost = 4 if params.time_bias and candle_time(c).hour in good_hours() else 0
    candidates: list[Candidate] = []

    def add(strategy: str, side: str, score: float, tp: float, sl: float, reasons: list[str]) -> None:
        tp_adj, sl_adj = adaptive_offsets(tp, sl, side, vol_state, trend, params)
        candidates.append(Candidate(strategy, side, score + hour_boost, tp_adj, sl_adj, reasons))

    s1_allowed = s1_regime_allowed(f, i, params)

    if s1_allowed and trend == "range" and vol_state in {"low", "normal"} and vol_ratio >= params.min_vol_ratio and body_ratio >= params.strict_body_ratio:
        if price <= f["bb_lower"][i] + params.range_edge_atr_margin * atr_val and f["rsi"][i] <= params.s1_rsi_long_max:
            stretch = max(0.0, (f["bb_lower"][i] - price) / atr_val)
            relax_penalty = max(0.0, f["rsi"][i] - 32) * 0.35 + params.range_edge_atr_margin * 14
            add("S1_BB_RSI", "LONG", 65 + min(18, stretch * 12) + (32 - min(f["rsi"][i], 32)) * 0.4 - relax_penalty, params.s1_tp, params.s1_sl, ["bb_lower", "rsi_oversold"])
        if price >= f["bb_upper"][i] - params.range_edge_atr_margin * atr_val and f["rsi"][i] >= params.s1_rsi_short_min:
            stretch = max(0.0, (price - f["bb_upper"][i]) / atr_val)
            relax_penalty = max(0.0, 68 - f["rsi"][i]) * 0.35 + params.range_edge_atr_margin * 14
            add("S1_BB_RSI", "SHORT", 65 + min(18, stretch * 12) + (max(f["rsi"][i], 68) - 68) * 0.4 - relax_penalty, params.s1_tp, params.s1_sl, ["bb_upper", "rsi_overbought"])

    s2_strong = (
        f["trend_share_60"][i] >= params.s2_min_trend_share_60
        and f["ema_spread_atr"][i] >= params.s2_min_ema_spread_atr
    )
    if trend in {"up", "down"} and vol_state != "low" and vol_ratio >= params.min_vol_ratio and s2_strong:
        cross_up = f["ema_fast"][i - 1] <= f["ema_slow"][i - 1] and f["ema_fast"][i] > f["ema_slow"][i]
        cross_dn = f["ema_fast"][i - 1] >= f["ema_slow"][i - 1] and f["ema_fast"][i] < f["ema_slow"][i]
        continuation_up = (not params.s2_require_cross) and f["ema_fast"][i] > f["ema_slow"][i] and c["low"] <= max(f["vwap"][i], f["ema_slow"][i])
        continuation_dn = (not params.s2_require_cross) and f["ema_fast"][i] < f["ema_slow"][i] and c["high"] >= min(f["vwap"][i], f["ema_slow"][i])
        if f["supertrend"][i] == 1 and price > f["vwap"][i] and mtf_bull and (cross_up or continuation_up):
            add("S2_SuperTrend", "LONG", 78 + vol_ratio * 5 + (8 if cross_up else 0), params.s2_tp, params.s2_sl, ["supertrend_up", "vwap_above", "mtf_bull"])
        if f["supertrend"][i] == -1 and price < f["vwap"][i] and mtf_bear and (cross_dn or continuation_dn):
            add("S2_SuperTrend", "SHORT", 78 + vol_ratio * 5 + (8 if cross_dn else 0), params.s2_tp, params.s2_sl, ["supertrend_down", "vwap_below", "mtf_bear"])

    if trend in {"up", "down"} and vol_state == "normal" and vol_ratio >= params.min_vol_ratio:
        macd_up = f["macd_hist"][i - 1] <= 0 and f["macd_hist"][i] > 0
        macd_dn = f["macd_hist"][i - 1] >= 0 and f["macd_hist"][i] < 0
        if trend == "up" and mtf_bull and c["low"] <= f["ema_slow"][i] and macd_up:
            add("S3_EMA_MACD", "LONG", 74 + body_ratio * 8, params.s3_tp, params.s3_sl, ["ema_pullback", "macd_flip_up"])
        if trend == "down" and mtf_bear and c["high"] >= f["ema_slow"][i] and macd_dn:
            add("S3_EMA_MACD", "SHORT", 74 + body_ratio * 8, params.s3_tp, params.s3_sl, ["ema_pullback", "macd_flip_down"])

    if vol_state == "high" and vol_ratio >= params.breakout_vol_ratio and body_ratio >= params.breakout_body_ratio:
        upper = f["donchian_upper"][i - 1]
        lower = f["donchian_lower"][i - 1]
        if price > upper + params.breakout_atr_margin * atr_val:
            add("S4_Donchian", "LONG", 70 + min(20, vol_ratio * 4), params.s4_tp, params.s4_sl, ["donchian_upper", "high_volume"])
        if price < lower - params.breakout_atr_margin * atr_val:
            add("S4_Donchian", "SHORT", 70 + min(20, vol_ratio * 4), params.s4_tp, params.s4_sl, ["donchian_lower", "high_volume"])

    if trend == "range" and vol_state == "normal" and vol_ratio >= params.min_vol_ratio and body_ratio >= params.strict_body_ratio:
        stoch_up = f["stoch_k"][i - 1] <= f["stoch_d"][i - 1] and f["stoch_k"][i] > f["stoch_d"][i]
        stoch_dn = f["stoch_k"][i - 1] >= f["stoch_d"][i - 1] and f["stoch_k"][i] < f["stoch_d"][i]
        if stoch_up and f["stoch_d"][i] < params.s5_long_d_max and price < f["vwap"][i] + (0.25 + params.range_edge_atr_margin) * atr_val:
            relax_penalty = max(0.0, f["stoch_d"][i] - 24) * 0.3 + params.range_edge_atr_margin * 10
            add("S5_Stoch", "LONG", 66 + (24 - min(f["stoch_d"][i], 24)) * 0.5 - relax_penalty, params.s5_tp, params.s5_sl, ["stoch_cross_up", "range_reversion"])
        if stoch_dn and f["stoch_d"][i] > params.s5_short_d_min and price > f["vwap"][i] - (0.25 + params.range_edge_atr_margin) * atr_val:
            relax_penalty = max(0.0, 76 - f["stoch_d"][i]) * 0.3 + params.range_edge_atr_margin * 10
            add("S5_Stoch", "SHORT", 66 + (max(f["stoch_d"][i], 76) - 76) * 0.5 - relax_penalty, params.s5_tp, params.s5_sl, ["stoch_cross_down", "range_reversion"])

    if catchup and s1_allowed and vol_ratio >= params.min_vol_ratio * 0.45 and body_ratio >= max(0.04, params.strict_body_ratio * 0.45):
        vwap_atr = params.rescue_vwap_atr if rescue else params.catchup_vwap_atr
        rsi_long_max = params.rescue_rsi_long_max if rescue else params.catchup_rsi_long_max
        rsi_short_min = params.rescue_rsi_short_min if rescue else params.catchup_rsi_short_min
        long_edge = price < f["vwap"][i] - vwap_atr * atr_val and f["rsi"][i] <= rsi_long_max
        short_edge = price > f["vwap"][i] + vwap_atr * atr_val and f["rsi"][i] >= rsi_short_min
        if long_edge:
            add(
                "S1_BB_RSI",
                "LONG",
                61 + min(14, (f["vwap"][i] - price) / atr_val * 5) + (3 if rescue else 0),
                min(params.s1_tp, params.s5_tp) * (0.62 if rescue else 0.72),
                max(params.s1_sl, params.s5_sl) * 0.95,
                ["rescue_vwap_reversion" if rescue else "catchup_vwap_reversion", "rsi_soft_long"],
            )
        if short_edge:
            add(
                "S1_BB_RSI",
                "SHORT",
                61 + min(14, (price - f["vwap"][i]) / atr_val * 5) + (3 if rescue else 0),
                min(params.s1_tp, params.s5_tp) * (0.62 if rescue else 0.72),
                max(params.s1_sl, params.s5_sl) * 0.95,
                ["rescue_vwap_reversion" if rescue else "catchup_vwap_reversion", "rsi_soft_short"],
            )

    # A1 entry trend guard: block counter-trend entries (mirrors live evaluate_entry_trend_guard).
    # slope_block=0.03 is half the regime classifier's 0.06, catching the "soft trend" band
    # that still gets labelled "range".  Default 0.0 = disabled (old preset behaviour unchanged).
    if params.entry_trend_guard_slope > 0 and i >= 20:
        sg = params.entry_trend_guard_slope
        ema_t = f["ema_trend"][i]
        bt_slope = (f["ema_slow"][i] - f["ema_slow"][i - 20]) / max(atr_val, 1e-8)
        candidates = [
            c for c in candidates
            if not (c.side == "LONG" and price < ema_t and bt_slope < -sg)
            and not (c.side == "SHORT" and price > ema_t and bt_slope > sg)
        ]

    return candidates


def s1_regime_allowed(f: dict, i: int, params: WildcatParams) -> bool:
    if not params.regime_guard_enabled:
        return True
    return (
        f["trend_share_60"][i] <= params.s1_max_trend_share_60
        and f["vwap_slope_atr"][i] <= params.s1_max_vwap_slope_atr
        and f["ema_spread_atr"][i] <= params.s1_max_ema_spread_atr
    )


def adaptive_offsets(tp: float, sl: float, side: str, vol: str, trend: str, params: WildcatParams) -> tuple[float, float]:
    if not params.adaptive_tp_sl:
        return tp, sl
    if vol == "high":
        tp *= 1.20
        sl *= 0.92
    elif vol == "low":
        tp *= 0.82
    if trend in {"up", "down"}:
        tp *= 1.08
    return tp, sl


def open_position(candle: dict, c_time: datetime, i: int, candidate: Candidate, params: WildcatParams) -> Position:
    entry = candle["close"]
    qty = params.notional_usdc / entry
    if candidate.side == "LONG":
        tp = entry * (1 + candidate.tp_pct)
        sl = entry * (1 - candidate.sl_pct)
    else:
        tp = entry * (1 - candidate.tp_pct)
        sl = entry * (1 + candidate.sl_pct)
    return Position(
        strategy=candidate.strategy,
        side=candidate.side,
        entry_time=c_time,
        entry_index=i,
        entry_price=entry,
        tp_price=tp,
        sl_price=sl,
        tp_pct=candidate.tp_pct,
        sl_pct=candidate.sl_pct,
        qty=qty,
        entry_fee=params.notional_usdc * params.entry_fee_rate,
        notional_usdc=params.notional_usdc,
    )


def maybe_exit(candle: dict, c_time: datetime, i: int, pos: Position, params: WildcatParams, f: dict) -> dict | None:
    maybe_recover_position(candle, pos, params, f, i)
    partial_exit_position(candle, pos, params)
    if pos.side == "LONG":
        if candle["low"] <= pos.sl_price:
            return close_trade(c_time, i, pos, pos.sl_price, params.sl_exit_fee_rate, "SL")
        if candle["high"] >= pos.tp_price:
            return close_trade(c_time, i, pos, pos.tp_price, params.tp_exit_fee_rate, "TP")
    else:
        if candle["high"] >= pos.sl_price:
            return close_trade(c_time, i, pos, pos.sl_price, params.sl_exit_fee_rate, "SL")
        if candle["low"] <= pos.tp_price:
            return close_trade(c_time, i, pos, pos.tp_price, params.tp_exit_fee_rate, "TP")
    if (
        params.adverse_exit_enabled
        and i - pos.entry_index >= params.adverse_exit_bars
        and unrealized_pnl(candle["close"], pos) <= -pos.notional_usdc * params.adverse_exit_loss_pct
    ):
        return close_trade(c_time, i, pos, candle["close"], params.sl_exit_fee_rate, "ADVERSE_EXIT")
    if i - pos.entry_index >= params.max_holding_bars:
        reason = "MAX_HOLD_WIN" if unrealized_pnl(candle["close"], pos) >= 0 else "MAX_HOLD_LOSS"
        return close_trade(c_time, i, pos, candle["close"], params.tp_exit_fee_rate, reason)
    return None


def maybe_recover_position(candle: dict, pos: Position, params: WildcatParams, f: dict, i: int) -> None:
    if not params.recovery_enabled or pos.dca_count >= params.recovery_steps:
        return
    # DCA regime guard: mirrors live evaluate_dca_guard.  Only average-down
    # when the regime is still "range" and stochastic momentum is not crossing
    # against the position.
    if params.dca_regime_guard:
        if f["trend"][i] != "range":
            return
        k, d = f["stoch_k"][i], f["stoch_d"][i]
        k_p, d_p = f["stoch_k"][i - 1], f["stoch_d"][i - 1]
        if pos.side == "SHORT" and k_p <= d_p and k > d:
            return
        if pos.side == "LONG" and k_p >= d_p and k < d:
            return
    trigger_pct = params.recovery_trigger_pct * (pos.dca_count + 1)
    if pos.side == "LONG":
        trigger_price = pos.entry_price * (1 - trigger_pct)
        hit = candle["low"] <= trigger_price
    else:
        trigger_price = pos.entry_price * (1 + trigger_pct)
        hit = candle["high"] >= trigger_price
    if not hit:
        return

    added_notional = params.notional_usdc * params.recovery_notional_scale
    added_qty = added_notional / trigger_price
    new_qty = pos.qty + added_qty
    pos.entry_price = (pos.entry_price * pos.qty + trigger_price * added_qty) / new_qty
    pos.qty = new_qty
    pos.notional_usdc += added_notional
    pos.entry_fee += added_notional * params.entry_fee_rate
    pos.dca_count += 1
    tp_pct = pos.tp_pct * (params.recovery_tp_shrink if pos.dca_count > 0 else 1.0)
    if pos.side == "LONG":
        pos.tp_price = pos.entry_price * (1 + tp_pct)
        pos.sl_price = pos.entry_price * (1 - pos.sl_pct * (1 + 0.25 * pos.dca_count))
    else:
        pos.tp_price = pos.entry_price * (1 - tp_pct)
        pos.sl_price = pos.entry_price * (1 + pos.sl_pct * (1 + 0.25 * pos.dca_count))


def partial_exit_position(candle: dict, pos: Position, params: WildcatParams) -> None:
    if pos.partial_taken or params.partial_exit_pct <= 0:
        return
    if pos.side == "LONG":
        partial_price = pos.entry_price * (1 + params.partial_tp_pct)
        hit = candle["high"] >= partial_price
    else:
        partial_price = pos.entry_price * (1 - params.partial_tp_pct)
        hit = candle["low"] <= partial_price
    if not hit:
        return

    close_ratio = min(max(params.partial_exit_pct, 0.0), 0.8)
    close_qty = pos.qty * close_ratio
    gross = (partial_price - pos.entry_price) * close_qty if pos.side == "LONG" else (pos.entry_price - partial_price) * close_qty
    entry_fee_alloc = pos.entry_fee * close_ratio
    exit_fee = close_qty * partial_price * params.tp_exit_fee_rate
    pos.realized_pnl += gross - entry_fee_alloc - exit_fee
    pos.realized_fees += entry_fee_alloc + exit_fee
    pos.entry_fee -= entry_fee_alloc
    pos.qty -= close_qty
    pos.notional_usdc *= 1 - close_ratio
    pos.partial_taken = True


def force_exit(candle: dict, c_time: datetime, i: int, pos: Position, params: WildcatParams, reason: str) -> dict:
    return close_trade(c_time, i, pos, candle["close"], params.tp_exit_fee_rate, reason)


def close_trade(c_time: datetime, i: int, pos: Position, exit_price: float, exit_fee_rate: float, reason: str) -> dict:
    gross = (exit_price - pos.entry_price) * pos.qty if pos.side == "LONG" else (pos.entry_price - exit_price) * pos.qty
    exit_fee = pos.qty * exit_price * exit_fee_rate
    pnl = pos.realized_pnl + gross - pos.entry_fee - exit_fee
    return {
        "strategy": pos.strategy,
        "side": pos.side,
        "entry_time": pos.entry_time.isoformat(),
        "exit_time": c_time.isoformat(),
        "entry_index": pos.entry_index,
        "exit_index": i,
        "entry_price": round(pos.entry_price, 4),
        "exit_price": round(exit_price, 4),
        "exit_reason": reason,
        "gross_pnl": round(pos.realized_pnl + gross + pos.realized_fees, 6),
        "fees": round(pos.realized_fees + pos.entry_fee + exit_fee, 6),
        "pnl": round(pnl, 6),
        "holding_bars": i - pos.entry_index,
        "dca_count": pos.dca_count,
        "partial_taken": pos.partial_taken,
        "final_notional_usdc": round(pos.notional_usdc, 4),
    }


def summarize(trades: list[dict], days: int, params: WildcatParams) -> dict:
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    by_strategy = {key: strategy_summary([t for t in trades if t["strategy"] == key]) for key in STRATEGIES}
    daily = daily_summary(trades, days, params)
    net_pnl = sum(t["pnl"] for t in trades)
    dd = max_drawdown(trades)
    avg_daily = net_pnl / max(days, 1)
    strict_met = (
        daily["all_days_met_target"]
        and avg_daily > 30.0
        and dd < 20.0
    )
    near_met = (
        daily["daily_target_hits"] >= max(1, math.ceil(daily["daily_target_day_count"] * 0.875))
        and daily["worst_day_usdc"] >= params.target_daily_usdc * 0.9
        and avg_daily >= 27.0
        and dd <= 22.0
    )
    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "net_pnl_usdc": round(net_pnl, 4),
        "gross_profit_usdc": round(gross_profit, 4),
        "gross_loss_usdc": round(gross_loss, 4),
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else math.inf,
        "max_drawdown_usdc": round(dd, 4),
        "strict_objective": {
            "daily_min_target_usdc": params.target_daily_usdc,
            "avg_daily_target_usdc": 30.0,
            "max_drawdown_limit_usdc": 20.0,
            "met": strict_met,
        },
        "near_10pct_objective": {
            "daily_min_floor_usdc": round(params.target_daily_usdc * 0.9, 4),
            "avg_daily_floor_usdc": 27.0,
            "max_drawdown_ceiling_usdc": 22.0,
            "met": near_met,
        },
        **daily,
        "by_strategy": by_strategy,
    }


def daily_summary(trades: list[dict], days: int, params: WildcatParams) -> dict:
    buckets: dict[str, float] = {}
    for trade in trades:
        day = str(trade["exit_time"])[:10]
        buckets[day] = buckets.get(day, 0.0) + trade["pnl"]

    observed_days = sorted(buckets)
    daily_rows = [{"date": day, "pnl_usdc": round(buckets[day], 4), "met_target": buckets[day] >= params.target_daily_usdc} for day in observed_days]
    active_days = len(observed_days)
    calendar_days = max(days, 1)
    net = sum(t["pnl"] for t in trades)
    avg_calendar = net / calendar_days
    target_hits = sum(1 for row in daily_rows if row["met_target"])
    target_day_count = max(active_days, 1)
    worst_observed = min((row["pnl_usdc"] for row in daily_rows), default=0.0)
    best_observed = max((row["pnl_usdc"] for row in daily_rows), default=0.0)
    required_notional = math.inf if avg_calendar <= 0 else params.notional_usdc * params.target_daily_usdc / avg_calendar
    leverage_rows = []
    for leverage in params.leverage_options:
        margin = params.notional_usdc / leverage if leverage > 0 else math.inf
        leverage_rows.append(
            {
                "leverage": leverage,
                "margin_per_trade_usdc": round(margin, 4),
                "avg_daily_return_on_margin_pct": round(avg_calendar / margin * 100, 2) if margin > 0 and math.isfinite(margin) else 0.0,
            }
        )

    return {
        "target_daily_usdc": params.target_daily_usdc,
        "active_days": active_days,
        "avg_daily_pnl_usdc": round(avg_calendar, 4),
        "avg_active_day_pnl_usdc": round(net / active_days, 4) if active_days else 0.0,
        "daily_target_hits": target_hits,
        "daily_target_day_count": active_days,
        "daily_target_hit_rate_pct": round(target_hits / target_day_count * 100, 2),
        "all_days_met_target": active_days > 0 and target_hits >= active_days,
        "best_day_usdc": round(best_observed, 4),
        "worst_day_usdc": round(worst_observed, 4),
        "required_notional_for_target_usdc": round(required_notional, 2) if math.isfinite(required_notional) else math.inf,
        "leverage_report": leverage_rows,
        "daily_pnl": daily_rows,
    }


def strategy_summary(trades: list[dict]) -> dict:
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    return {
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else 0.0,
        "net_pnl_usdc": round(sum(t["pnl"] for t in trades), 4),
        "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else (math.inf if wins else 0.0),
        "max_drawdown_usdc": round(max_drawdown(trades), 4),
    }


def max_drawdown(trades: list[dict]) -> float:
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for trade in trades:
        running += trade["pnl"]
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
    return max_dd


def rank_score(summary: dict) -> float:
    trades = summary["trades"]
    if trades < 5:
        return -10000.0 + trades
    pf = summary["profit_factor"]
    pf_score = min(pf if math.isfinite(pf) else 5.0, 5.0) * 8
    target_gap = max(0.0, summary["target_daily_usdc"] - summary["avg_daily_pnl_usdc"])
    avg30_gap = max(0.0, 30.01 - summary["avg_daily_pnl_usdc"])
    dd_over = max(0.0, summary["max_drawdown_usdc"] - 19.99)
    worst_day_penalty = max(0.0, summary["target_daily_usdc"] - summary["worst_day_usdc"]) * 2.0
    missed_days = max(0, summary["daily_target_day_count"] - summary["daily_target_hits"])
    return (
        summary["daily_target_hit_rate_pct"] * 8.0
        + summary["avg_daily_pnl_usdc"] * 16.0
        + summary["net_pnl_usdc"] * 0.4
        + pf_score
        + summary["win_rate_pct"] * 0.12
        - summary["max_drawdown_usdc"] * 1.2
        - missed_days * 180.0
        - target_gap * 6.0
        - avg30_gap * 25.0
        - dd_over * 80.0
        - worst_day_penalty * 8.0
    )


def volatility_state(percentile: float) -> str:
    if percentile < 0.25:
        return "low"
    if percentile > 0.80:
        return "high"
    return "normal"


def unrealized_pnl(price: float, pos: Position) -> float:
    return (price - pos.entry_price) * pos.qty if pos.side == "LONG" else (pos.entry_price - price) * pos.qty


def candle_time(candle: dict) -> datetime:
    return datetime.fromtimestamp(candle["time_ms"] / 1000, tz=timezone.utc).astimezone(TAIPEI)


def good_hours() -> set[int]:
    return set(range(9, 12)) | set(range(15, 23)) | {1, 2}


def print_report(payload: dict, output: Path) -> None:
    print("\n" + "=" * 78)
    print("WILDCAT S1-S5 RESEARCH SIDECAR")
    print("=" * 78)
    for run in payload["runs"]:
        best = run["best"]
        params = best["params"]
        print(f"\nDays: {run['days']} | Variants: {run['variant_count']} | Best: {params['label']}")
        print(
            f"Trades={best['trades']} WR={best['win_rate_pct']:.2f}% "
            f"PnL={best['net_pnl_usdc']:+.4f} PF={best['profit_factor']} "
            f"MaxDD={best['max_drawdown_usdc']:.4f}"
        )
        print(
            f"Daily avg={best['avg_daily_pnl_usdc']:+.4f} target={best['target_daily_usdc']:.2f} "
            f"hits={best['daily_target_hits']}/{best.get('daily_target_day_count', run['days'])} ({best['daily_target_hit_rate_pct']:.2f}%) "
            f"worst={best['worst_day_usdc']:+.4f} best={best['best_day_usdc']:+.4f} "
            f"need_notional~{best['required_notional_for_target_usdc']}"
        )
        strict = best.get("strict_objective") or {}
        near = best.get("near_10pct_objective") or {}
        print(f"Objective: strict={strict.get('met')} near_10pct={near.get('met')}")
        if best.get("leverage_report"):
            leverage_bits = [
                f"{row['leverage']}x margin={row['margin_per_trade_usdc']} avg/day={row['avg_daily_return_on_margin_pct']}%"
                for row in best["leverage_report"]
            ]
            print("Leverage report: " + " | ".join(leverage_bits))
        if best.get("daily_pnl"):
            print("Daily PnL:")
            for row in best["daily_pnl"]:
                flag = "OK" if row["met_target"] else "MISS"
                print(f"  {row['date']} {row['pnl_usdc']:+9.4f} {flag}")
        print("Per strategy:")
        for name, stats in best["by_strategy"].items():
            print(
                f"  {name:15} trades={stats['trades']:3d} "
                f"WR={stats['win_rate_pct']:6.2f}% PnL={stats['net_pnl_usdc']:+9.4f} "
                f"PF={stats['profit_factor']} DD={stats['max_drawdown_usdc']:.4f}"
            )
        print(
            "Recommended params: "
            f"min_vol={params['min_vol_ratio']} body={params['strict_body_ratio']} "
            f"breakout_vol={params['breakout_vol_ratio']} cooldown={params['cooldown_bars']} "
            f"hold={params['max_holding_bars']} fees=entry{params['entry_fee_rate']}/"
            f"tp{params['tp_exit_fee_rate']}/sl{params['sl_exit_fee_rate']}"
        )
    print(f"\nWrote JSON report: {output}")


if __name__ == "__main__":
    main()
