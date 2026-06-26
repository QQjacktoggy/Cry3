"""Live policy gate for the accepted Codex V6.8.5 research bundle.

The backtest result this captures is the 21-branch portfolio union ending at
``portfolio_union_21branch_w1s6short_cluster1129``.  This module is deliberately
pure: it decides whether an already-built candidate belongs to one of the
accepted lanes, but it does not place orders or compute indicators from raw
candles.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from html import escape
import json
import re
from typing import Any, Mapping, Sequence


CODEX_V1_VERSION = "_codex_v1.4.0"
CODEX_V1_BASELINE = "portfolio_union_21branch_w1s6short_cluster1129"
BASE_NOTIONAL_USDC = 50.0
STALE_UPMOVE_CANARY_LANE = "codex_v1_stale_upmove_short_rng20_canary"
STALE_UPMOVE_CANARY_LANE_CODE = "STUP-S"
STALE_UPMOVE_CANARY_POLICY_TAG = "v1312_stale_upmove_guarded_canary"
STALE_UPMOVE_CANARY_RNG15_MIN_BP = 20.0
STALE_UPMOVE_CANARY_RNG15_MAX_BP = 100.0
STALE_UPMOVE_CANARY_ADV3_MIN_BP = 0.0
STALE_UPMOVE_CANARY_D30_MAX_BP = 80.0
STALE_UPMOVE_CANARY_NOTIONAL_USDC = 50.0
STALE_UPMOVE_SL19_SHADOW_TAG = "v1315_stups_sl19_shadow_only"
STALE_UPMOVE_SL19_SHADOW_BP = 19.0
STALE_UPMOVE_LOW_RNG_WEAK_ADV_SHADOW_TAG = "v1315_stups_low_rng_weak_adv_shadow_only"
STALE_UPMOVE_LOW_RNG_MAX_BP = 30.0
STALE_UPMOVE_WEAK_ADV3_MAX_BP = 3.0
SUPPORTED_SYMBOLS = ("ETHUSDC",)


_FEATURE_ALIASES: dict[str, tuple[str, ...]] = {
    "strategy": ("strategy",),
    "side": ("side",),
    "score": ("score",),
    "rng15": ("rng15", "rng15_bp", "range15_bp"),
    "d30": ("d30", "drift30", "drift30_bp", "drift_bp"),
    "adv3": ("adv3", "adv3_bp", "adverse3_bp"),
    "range_bp": ("range_bp", "bar_range_bp", "candle_range_bp"),
    "ret3_bp": ("ret3_bp", "ret3"),
    "d3": ("d3", "drift3", "drift3_bp"),
    "d5": ("d5", "drift5", "drift5_bp"),
    "rsi": ("rsi", "rsi14"),
    "bb_lower_dist_bp": ("bb_lower_dist_bp", "bb_lower_dist"),
    "vwap_dist_bp": ("vwap_dist_bp", "vwap_dist"),
    "pullback_from_recent_high_bp": (
        "pullback_from_recent_high_bp",
        "pullback_bp",
        "recent_high_pullback_bp",
    ),
    "price_above_or_reclaimed_vwap": (
        "price_above_or_reclaimed_vwap",
        "reclaimed_vwap",
        "above_or_reclaimed_vwap",
    ),
    "range_pos_5": ("range_pos_5", "range_pos5"),
    "range_pos_15": ("range_pos_15", "range_pos15"),
    "range_pos_30": ("range_pos_30", "range_pos30"),
    "close_pos": ("close_pos", "close_pos_bar"),
    "reprice_favorable_bp": ("reprice_favorable_bp", "favorable_bp"),
    "reprice_adverse_bp": ("reprice_adverse_bp", "adverse_bp"),
    "gap1bp": ("gap1bp", "gap1bp_bp", "one_bp_gap"),
}


@dataclass(frozen=True)
class NumericBand:
    feature: str
    low: float = -10_000.0
    high: float = 10_000.0

    def contains(self, features: Mapping[str, Any]) -> tuple[bool, str | None]:
        value = _feature_value(features, self.feature)
        if value is None:
            return False, self.feature
        return self.low <= value <= self.high, None


@dataclass(frozen=True)
class DenyRule:
    name: str
    bands: tuple[NumericBand, ...]

    def matches(self, features: Mapping[str, Any]) -> bool:
        for band in self.bands:
            ok, _missing = band.contains(features)
            if not ok:
                return False
        return True


@dataclass(frozen=True)
class LaneSpec:
    name: str
    strategies: tuple[str, ...]
    side: str
    entry_offset_bp: float
    bands: tuple[NumericBand, ...]
    feature_bands: tuple[NumericBand, ...] = ()
    deny_rules: tuple[DenyRule, ...] = ()
    base_size_mult: float = 1.0
    scale_rng_low_bp: float | None = 55.0
    scale_rng_high_bp: float | None = 75.0
    scale_factor: float = 1.2
    notes: str = ""

    @property
    def lane_code(self) -> str:
        return lane_code_from_name(self.name, side=self.side) or self.name


@dataclass(frozen=True)
class CodexV1Decision:
    accepted: bool
    version: str
    baseline: str
    lane: str | None
    lane_code: str | None
    strategy: str | None
    side: str | None
    entry_offset_bp: float | None
    size_mult: float
    notional_mult: float
    requested_notional_usdc: float
    reason: str
    regime: str | None = None
    missing_features: tuple[str, ...] = ()
    risk_tags: tuple[str, ...] = ()
    metrics: dict[str, Any] | None = None
    policy_tag: str | None = None
    shadow_lane: str | None = None


def _b(feature: str, low: float = -10_000.0, high: float = 10_000.0) -> NumericBand:
    return NumericBand(feature, low, high)


def _deny(name: str, *bands: NumericBand) -> DenyRule:
    return DenyRule(name, tuple(bands))


S1_LONG_REPRICE_BLOCK = _deny(
    "s1_long_reprice_block",
    _b("rsi", -10_000.0, 34.464),
    _b("range_bp", 9.259, 10_000.0),
    _b("ret3_bp", -16.739, 10_000.0),
)

SHORT_LOW_FOLLOWTHROUGH_BLOCK = _deny(
    "short_low_followthrough_block",
    _b("score", 60.0, 66.0),
    _b("rng15", 26.0, 38.0),
    _b("d30", -3.0, 14.0),
    _b("adv3", 4.5, 7.8),
)


LANES: tuple[LaneSpec, ...] = (
    LaneSpec(
        "codex_v1_hot_up_extension_pullback_long",
        ("S1_BB_RSI", "S5_Stoch", "S6_TrendPull"),
        "LONG",
        0.0,
        (
            _b("score", 75.0, 200.0),
            _b("d30", 25.0, 90.0),
            _b("rsi", 56.0, 66.0),
            _b("vwap_dist_bp", 8.0, 40.0),
            _b("bb_lower_dist_bp", 35.0, 10_000.0),
        ),
        (
            _b("pullback_from_recent_high_bp", 8.0, 35.0),
            _b("d3", -8.0, 10_000.0),
            _b("d5", -15.0, 10_000.0),
            _b("price_above_or_reclaimed_vwap", 1.0, 1.0),
        ),
        base_size_mult=0.35,
        scale_rng_low_bp=None,
        scale_rng_high_bp=None,
        notes="Hot up-extension pullback LONG only; fade-shorts are vetoed while waiting.",
    ),
    LaneSpec(
        "anchor_s1_preblock_broad_su6_exitA",
        ("S1_BB_RSI",),
        "LONG",
        3.0,
        (
            _b("score", 64.0),
            _b("rng15", 26.0, 30.0),
            _b("d30", -20.0, -8.0),
            _b("adv3", 6.0, 10.5),
        ),
        base_size_mult=1.0,
        notes="Anchor LONG side filter.",
    ),
    LaneSpec(
        "anchor_s1_preblock_broad_su6_exitA",
        ("S1_BB_RSI",),
        "SHORT",
        3.0,
        (
            _b("score", 58.0),
            _b("rng15", 26.0, 68.0),
            _b("d30", -28.0, 28.0),
            _b("adv3", -10_000.0, 11.0),
        ),
        deny_rules=(SHORT_LOW_FOLLOWTHROUGH_BLOCK,),
        base_size_mult=1.0,
        notes="Anchor SHORT with low-followthrough block before size-up.",
    ),
    LaneSpec(
        "reprice_s1_longonly_w1_e1_longblock",
        ("S1_BB_RSI",),
        "LONG",
        1.0,
        (
            _b("reprice_favorable_bp", -1.0, 4.0),
            _b("reprice_adverse_bp", -10_000.0, 2.0),
        ),
        deny_rules=(S1_LONG_REPRICE_BLOCK,),
        notes="One-bar 1bp reprice overlay; requires live reprice shadow features.",
    ),
    LaneSpec(
        "w2_lane_s1long_score64_74_rng35_55_e0_block",
        ("S1_BB_RSI",),
        "LONG",
        0.0,
        (_b("score", 64.3912, 74.3912), _b("rng15", 35.1624, 55.1624)),
        deny_rules=(S1_LONG_REPRICE_BLOCK,),
    ),
    LaneSpec(
        "w5_lane_s6short_score79_84_rng0_34_e0",
        ("S6_TrendPull",),
        "SHORT",
        0.0,
        (_b("score", 79.3752, 84.3752), _b("rng15", 0.0, 34.5266)),
    ),
    LaneSpec(
        "w3_lane_s6long_rng39_71_advneg5_14_range5_25_e0",
        ("S6_TrendPull",),
        "LONG",
        0.0,
        (_b("rng15", 39.1319, 71.1319), _b("adv3", -5.311, 14.689)),
        (_b("range_bp", 5.322, 25.322),),
    ),
    LaneSpec(
        "w1_lane_s6short_score69_79_rsi35_43_e0",
        ("S6_TrendPull",),
        "SHORT",
        0.0,
        (_b("score", 69.3497, 79.3497),),
        (_b("rsi", 35.27, 43.27),),
    ),
    LaneSpec(
        "w6_lane_s1long_rng38_86_range9_15_e0",
        ("S1_BB_RSI",),
        "LONG",
        0.0,
        (_b("rng15", 38.243, 86.243),),
        (_b("range_bp", 9.13, 15.13),),
    ),
    LaneSpec(
        "w5_lane_s6short_score72_77_bblower7_35_e0",
        ("S6_TrendPull",),
        "SHORT",
        0.0,
        (_b("score", 72.3908, 77.3908),),
        (_b("bb_lower_dist_bp", 7.305, 35.305),),
    ),
    LaneSpec(
        "w4_lane_s6long_advneg12_neg2_bblower31_47_score86_d30_13_e0",
        ("S6_TrendPull",),
        "LONG",
        0.0,
        (_b("score", 86.0), _b("d30", 13.0, 200.0), _b("adv3", -12.5805, -2.5805)),
        (_b("bb_lower_dist_bp", 31.346, 47.346),),
    ),
    LaneSpec(
        "w7_lane_s6short_range6_12_bblowerneg10_5_e0_advopen",
        ("S6_TrendPull",),
        "SHORT",
        0.0,
        (_b("rng15", 0.0, 200.0), _b("d30", -200.0, 200.0)),
        (_b("range_bp", 6.358, 12.358), _b("bb_lower_dist_bp", -10.908, 5.092)),
    ),
    LaneSpec(
        "w1_lane_s1short_score71_76_range3_9_e0_advopen",
        ("S1_BB_RSI",),
        "SHORT",
        0.0,
        (_b("score", 71.129, 76.129),),
        (_b("range_bp", 3.841, 9.841),),
    ),
    LaneSpec(
        "w3_lane_s6long_d30_6_46_advneg5_14_range9_21_e0",
        ("S6_TrendPull",),
        "LONG",
        0.0,
        (_b("d30", 6.6252, 46.6252), _b("adv3", -5.311, 14.689)),
        (_b("range_bp", 9.322, 21.322),),
    ),
    LaneSpec(
        "w6_lane_s6long_clusterB_vwap45_rp5_03_rp15_07_close08",
        ("S6_TrendPull",),
        "LONG",
        0.0,
        (
            _b("score", 79.0, 86.1),
            _b("rng15", 19.0, 31.0),
            _b("d30", -8.0, 24.0),
            _b("adv3", -14.0, 10.0),
        ),
        (
            _b("range_bp", 1.8, 11.0),
            _b("rsi", 46.0, 64.0),
            _b("bb_lower_dist_bp", 6.0, 26.0),
            _b("vwap_dist_bp", -45.0),
            _b("range_pos_5", 0.3),
            _b("range_pos_15", 0.7),
            _b("close_pos", -10_000.0, 0.8),
        ),
    ),
    LaneSpec(
        "w2_lane_s6short_score86_rng50_75_d30neg122_neg80_rsi32_42_vwap100_155_advneg5_e0",
        ("S6_TrendPull",),
        "SHORT",
        0.0,
        (
            _b("score", 85.9, 86.1),
            _b("rng15", 50.0, 75.0),
            _b("d30", -122.0, -80.0),
            _b("adv3", -5.0),
        ),
        (
            _b("range_bp", 8.0, 20.0),
            _b("rsi", 32.0, 42.0),
            _b("bb_lower_dist_bp", 9.0, 47.0),
            _b("vwap_dist_bp", 100.0, 155.0),
        ),
    ),
    LaneSpec(
        "w4_lane_s6long_score84_86_rng30_45_d30_20_40_advneg20_2_rsi67_82_vwapneg25_neg10_rp15_08_e0",
        ("S6_TrendPull",),
        "LONG",
        0.0,
        (
            _b("score", 84.0, 86.1),
            _b("rng15", 30.0, 45.0),
            _b("d30", 20.0, 40.0),
            _b("adv3", -20.0, 2.0),
        ),
        (
            _b("range_bp", 5.0, 15.0),
            _b("rsi", 67.0, 82.0),
            _b("bb_lower_dist_bp", 35.0, 50.0),
            _b("vwap_dist_bp", -25.0, -10.0),
            _b("range_pos_15", 0.8, 1.4),
        ),
    ),
    LaneSpec(
        "w1_lane_s6long_score86_rng35_42_d30_38_46_advneg3_0_rsi74_78_vwapneg25_neg18_rp15_085_e0",
        ("S6_TrendPull",),
        "LONG",
        0.0,
        (
            _b("score", 85.9, 86.1),
            _b("rng15", 35.0, 42.0),
            _b("d30", 38.0, 46.0),
            _b("adv3", -3.0, 0.0),
        ),
        (
            _b("range_bp", 2.0, 5.0),
            _b("rsi", 74.0, 78.0),
            _b("bb_lower_dist_bp", 40.0, 46.0),
            _b("vwap_dist_bp", -25.0, -18.0),
            _b("range_pos_15", 0.85, 1.0),
        ),
    ),
    LaneSpec(
        "w6_lane_s8short_bar2432_score72_73_rng35_40_d30neg40_neg35_vwapneg95_neg85_e0",
        ("S8_TrendSnipe",),
        "SHORT",
        0.0,
        (
            _b("score", 72.0, 73.0),
            _b("rng15", 35.0, 40.0),
            _b("d30", -40.0, -35.0),
            _b("adv3", -6.0, -4.0),
        ),
        (
            _b("range_bp", 14.0, 16.0),
            _b("rsi", 45.0, 47.0),
            _b("bb_lower_dist_bp", 20.0, 23.0),
            _b("vwap_dist_bp", -95.0, -85.0),
            _b("range_pos_15", 0.45, 0.55),
        ),
    ),
    LaneSpec(
        "w1_lane_s1long_d30neg25_15_vwap4_60_advmax15_e0",
        ("S1_BB_RSI",),
        "LONG",
        0.0,
        (_b("rng15", 0.0, 200.0), _b("d30", -25.3765, 14.6235), _b("adv3", -10_000.0, 15.0)),
        (_b("vwap_dist_bp", 4.201, 60.201),),
    ),
    LaneSpec(
        "codex_v1_s1_bbrsi_ordinary_pullback_long_pre_vwap",
        ("S1_BB_RSI",),
        "LONG",
        0.0,
        (
            _b("score", 64.0, 70.0),
            _b("d30", -8.0, 18.0),
            _b("adv3", -3.0, 7.0),
        ),
        (
            _b("d3", -7.0, 10_000.0),
            _b("d5", -6.0, 10_000.0),
            _b("rsi", 41.5, 50.5),
            _b("bb_lower_dist_bp", 3.0, 14.5),
            _b("vwap_dist_bp", -5.5, 0.75),
            _b("pullback_from_recent_high_bp", 10.0, 24.0),
            _b("price_above_or_reclaimed_vwap", 0.0, 0.0),
        ),
        base_size_mult=0.20,
        scale_rng_low_bp=None,
        scale_rng_high_bp=None,
        notes="Ordinary S1 pullback LONG below VWAP; small probe before reclaim.",
    ),
    LaneSpec(
        "w2_lane_s6short_rng80_112_d30neg56_neg32_e0",
        ("S6_TrendPull",),
        "SHORT",
        0.0,
        (_b("rng15", 80.1343, 112.1343), _b("d30", -56.0366, -32.0366)),
    ),
    LaneSpec(
        "w3_lane_s8short_bar2492_narrow_e0",
        ("S8_TrendSnipe",),
        "SHORT",
        0.0,
        (_b("rng15", 32.0, 35.0), _b("d30", -16.0, -13.0), _b("adv3", -2.0, 1.0)),
        (
            _b("range_bp", 5.0, 7.0),
            _b("rsi", 47.0, 49.0),
            _b("bb_lower_dist_bp", 15.0, 18.0),
            _b("vwap_dist_bp", -8.0, -4.0),
            _b("range_pos_15", 0.55, 0.7),
        ),
    ),
    LaneSpec(
        "w1_lane_s6short_cluster1129_rng57_70_d30neg75_neg49_rsi43_53_vwap38_56_rp15_063_080_e0",
        ("S6_TrendPull",),
        "SHORT",
        0.0,
        (_b("rng15", 57.0, 70.0), _b("d30", -75.0, -49.0), _b("adv3", -5.0, 16.0)),
        (
            _b("range_bp", 7.0, 16.0),
            _b("rsi", 43.0, 53.0),
            _b("bb_lower_dist_bp", 26.0, 43.0),
            _b("vwap_dist_bp", 38.0, 56.0),
            _b("range_pos_15", 0.63, 0.8),
        ),
    ),
)


def select_codex_v1_lane(features: Mapping[str, Any]) -> CodexV1Decision:
    """Return the first live lane accepted by the Codex v1.0.0 policy."""

    symbol = _string_feature(features, "symbol")
    if symbol and symbol not in SUPPORTED_SYMBOLS:
        return _reject(f"unsupported_symbol:{symbol}", features=features)

    strategy = _string_feature(features, "strategy")
    side = _string_feature(features, "side")
    hot_up_lane = _hot_up_extension_pullback_lane()
    if is_hot_up_extension(features):
        if side == "SHORT":
            return _reject(
                "hot_up_extension_short_blocked",
                features=features,
                strategy=strategy,
                side=side,
                regime="hot_up_extension",
                risk_tags=get_short_risk_tags(features),
            )
        if side == "LONG":
            match, missing = _lane_matches(hot_up_lane, features)
            if match:
                size_mult = _size_mult(hot_up_lane, features)
                notional_mult = _notional_mult(hot_up_lane, features, size_mult)
                return CodexV1Decision(
                    accepted=True,
                    version=CODEX_V1_VERSION,
                    baseline=CODEX_V1_BASELINE,
                    lane=hot_up_lane.name,
                    lane_code=hot_up_lane.lane_code,
                    strategy=strategy,
                    side=side,
                    entry_offset_bp=hot_up_lane.entry_offset_bp,
                    size_mult=size_mult,
                    notional_mult=notional_mult,
                    requested_notional_usdc=BASE_NOTIONAL_USDC * notional_mult,
                    reason="hot_up_extension_pullback_long_match",
                    regime="hot_up_extension",
                    missing_features=tuple(sorted(missing)),
                    risk_tags=_risk_tags(hot_up_lane, size_mult),
                )
            return _reject(
                "hot_up_extension_waiting_for_pullback",
                features=features,
                strategy=strategy,
                side=side,
                regime="hot_up_extension",
                risk_tags=get_short_risk_tags(features),
            )
    if is_stale_short_after_upmove(features):
        stale_upmove_canary = build_stale_upmove_canary_decision(
            features,
            strategy=strategy,
            side=side,
        )
        if stale_upmove_canary is not None:
            return stale_upmove_canary
        return _reject(
            "stale_short_after_upmove_blocked",
            features=features,
            strategy=strategy,
            side=side,
            regime="stale_short_after_upmove",
            risk_tags=get_short_risk_tags(features),
        )
    if is_mid_up_extension_short_risk(features):
        return _reject(
            "mid_up_extension_short_blocked",
            features=features,
            strategy=strategy,
            side=side,
            regime="mid_up_extension",
            risk_tags=get_short_risk_tags(features),
        )

    s1p_l_lane = _s1p_l_lane()
    for lane in LANES:
        if lane.name in {hot_up_lane.name, s1p_l_lane.name}:
            continue
        match, missing = _lane_matches(lane, features)
        if not match:
            continue

        size_mult = _size_mult(lane, features)
        notional_mult = _notional_mult(lane, features, size_mult)
        return CodexV1Decision(
            accepted=True,
            version=CODEX_V1_VERSION,
            baseline=CODEX_V1_BASELINE,
            lane=lane.name,
            lane_code=lane.lane_code,
            strategy=strategy,
            side=side,
            entry_offset_bp=lane.entry_offset_bp,
            size_mult=size_mult,
            notional_mult=notional_mult,
            requested_notional_usdc=BASE_NOTIONAL_USDC * notional_mult,
            reason="accepted",
            regime=None,
            missing_features=tuple(sorted(missing)),
            risk_tags=_risk_tags(lane, size_mult),
        )

    if side == "LONG" and match_s1_bbrsi_ordinary_pullback_long_pre_vwap(features):
        return build_s1p_l_enter_decision(features)
    if side == "LONG" and is_s1_bbrsi_pullback_long_family(features):
        return build_s1p_l_wait_decision(features)

    return _reject("no_codex_v1_lane_match", features=features, strategy=strategy, side=side)


def build_codex_v1_live_features(
    *,
    symbol: str,
    strategy: str | None = None,
    side: str | None = None,
    score: float | None = None,
    rng15: float | None = None,
    d30: float | None = None,
    adv3: float | None = None,
    signal: Any | None = None,
    candles: Any | None = None,
    feature_series: Mapping[str, Any] | None = None,
    index: int | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the narrow feature payload used by Telegram/live adapters."""

    features: dict[str, Any] = {"symbol": symbol}
    if strategy:
        features["strategy"] = strategy
    if side:
        features["side"] = side
    if score is not None:
        features["score"] = score
    if rng15 is not None:
        features["rng15"] = rng15
    if d30 is not None:
        features["d30"] = d30
    if adv3 is not None:
        features["adv3"] = adv3

    if signal is not None:
        for attr in ("rsi", "atr", "vwap", "support"):
            value = getattr(signal, attr, None)
            if value is not None:
                features[attr] = value
        for reason in getattr(signal, "reasons", ()) or ():
            text = str(reason)
            if text.startswith("wildcat:") and "strategy" not in features:
                features["strategy"] = text.split(":", 1)[1]
            elif text.startswith("side:") and "side" not in features:
                features["side"] = text.split(":", 1)[1]
            elif text.startswith("score:") and "score" not in features:
                try:
                    features["score"] = float(text.split(":", 1)[1])
                except ValueError:
                    pass

    if candles:
        _populate_candle_derived_features(features, candles, feature_series=feature_series, index=index)

    if extra:
        for key, value in extra.items():
            if value is not None:
                features[key] = value
    return features


def codex_v1_feature_gaps(features: Mapping[str, Any]) -> tuple[str, ...]:
    """Return lane-specific fields that are still missing from a partial live payload."""

    gaps: set[str] = set()
    strategy = _string_feature(features, "strategy")
    side = _string_feature(features, "side")
    if not strategy:
        gaps.add("strategy")
    if not side:
        gaps.add("side")
    if not strategy or not side:
        return tuple(sorted(gaps))

    for lane in LANES:
        if strategy not in lane.strategies or side != lane.side:
            continue
        for band in (*lane.bands, *lane.feature_bands):
            _ok, missing_feature = band.contains(features)
            if missing_feature:
                gaps.add(missing_feature)
    return tuple(sorted(gaps))


def describe_codex_v1_nearest_lanes(
    features: Mapping[str, Any],
    *,
    limit: int = 3,
) -> tuple[str, ...]:
    """Summarize the closest live lanes and the remaining threshold gaps."""

    side = _string_feature(features, "side")
    scoped = _candidate_lanes_for_features(features)
    if not scoped:
        scoped = LANES
    ranked = sorted(
        (
            _lane_gap_summary(lane, features, ordinal)
            for ordinal, lane in enumerate(scoped)
        ),
        key=lambda item: item[0],
    )
    lines: list[str] = []
    side = _string_feature(features, "side")
    for _sort_key, lane, accepted, blockers in ranked[: max(1, limit)]:
        head = f"{lane.lane_code} / {lane.name}"
        if accepted and not blockers:
            lines.append(f"{head}: matched all lane thresholds")
            continue
        detail = ", ".join(blockers[:4]) if blockers else "no gap details"
        lines.append(f"{head}: {detail}")
    if side == "SHORT":
        if is_hot_up_extension(features):
            lines.insert(0, "SHORT veto active: hot_up_extension_short_blocked")
        elif is_stale_short_after_upmove(features):
            if build_stale_upmove_canary_decision(features) is not None:
                lines.insert(0, "SHORT canary active: stale_upmove_guarded_canary")
            else:
                lines.insert(0, "SHORT veto active: stale_short_after_upmove_blocked")
        elif is_mid_up_extension_short_risk(features):
            lines.insert(0, "SHORT veto active: mid_up_extension_short_blocked")
    return tuple(lines)

def classify_codex_v133_no_lane_candidate(
    features: Mapping[str, Any],
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Classify a no-lane row before any outcome is known.

    This is evidence routing only. It does not accept trades or change live
    execution, and promotion must use strict shadow outcomes after collection.
    """

    side = _string_feature(features, "side") or "UNKNOWN"
    strategy = _string_feature(features, "strategy") or "UNKNOWN"
    scoped = _candidate_lanes_for_features(features) or LANES
    ranked = sorted(
        (
            _lane_gap_summary(lane, features, ordinal)
            for ordinal, lane in enumerate(scoped)
        ),
        key=lambda item: item[0],
    )
    if not ranked:
        bucket = f"NL_{side}_UNCLASSIFIED"
        return {
            "candidate_bucket": bucket,
            "candidate_reason": reason or "no_codex_v1_lane_match",
            "nearest_lane_code": None,
            "nearest_lane_name": None,
            "nearest_lane_distance": None,
            "nearest_lane_gaps": {},
            "missing_critical_features": 1,
            "failed_threshold_count": 0,
            "promotion_family": bucket,
            "sampling_family": "NO_LANE",
        }

    sort_key, lane, accepted, blockers = ranked[0]
    missing_count = int(sort_key[0])
    weighted_gap = float(sort_key[1])
    failed_threshold_count = sum(1 for blocker in blockers if "missing" not in blocker)
    threshold_distance = max(0.0, weighted_gap - failed_threshold_count)
    nearest_code = lane.lane_code
    nearest_distance = round(threshold_distance, 4)
    near_threshold_by_code = {
        "RP1": 0.25,
        "S1P-L": 0.30,
        "W1D": 0.30,
        "W6A": 0.30,
    }
    near_threshold = near_threshold_by_code.get(nearest_code, 0.25)
    near_existing_lane = (
        not accepted
        and missing_count == 0
        and failed_threshold_count <= 2
        and threshold_distance <= near_threshold
    )

    if side == "LONG" and near_existing_lane and nearest_code == "RP1":
        bucket = "NL_NEAR_RP1_LONG"
    elif side == "LONG" and near_existing_lane and nearest_code == "S1P-L":
        bucket = "NL_NEAR_S1P_L_LONG"
    elif side == "LONG" and near_existing_lane:
        bucket = f"NL_NEAR_{nearest_code}_LONG"
    elif side == "SHORT" and near_existing_lane:
        bucket = f"NL_NEAR_{nearest_code}_SHORT"
    elif side == "LONG":
        bucket = "NL_LONG_UNCLASSIFIED"
    elif side == "SHORT":
        bucket = "NL_SHORT_UNCLASSIFIED"
    else:
        bucket = "NL_UNKNOWN_UNCLASSIFIED"

    return {
        "candidate_bucket": bucket,
        "candidate_reason": reason or "no_codex_v1_lane_match",
        "nearest_lane_code": nearest_code,
        "nearest_lane_name": lane.name,
        "nearest_lane_distance": nearest_distance,
        "nearest_lane_gaps": {
            "blockers": list(blockers[:8]),
            "accepted": bool(accepted),
            "strategy": strategy,
            "side": side,
        },
        "missing_critical_features": missing_count,
        "failed_threshold_count": failed_threshold_count,
        "promotion_family": bucket,
        "sampling_family": "NO_LANE",
    }

def format_codex_v1_telegram_report(features: Mapping[str, Any], *, execution_wired: bool = False) -> str:
    """HTML-safe Telegram section for the Codex v1.0.0 live policy."""

    decision = select_codex_v1_lane(features)
    preflight = live_preflight_rejections(features)
    gaps = decision.missing_features if decision.accepted else codex_v1_feature_gaps(features)

    lines = [
        f"🧪 <b>Codex Live Policy {escape(CODEX_V1_VERSION)}</b>",
        f"  • baseline: <code>{escape(CODEX_V1_BASELINE)}</code>",
    ]
    if decision.accepted:
        lines.append(f"  • verdict: <b>ACCEPT</b> / lane=<code>{escape(str(decision.lane_code or decision.lane))}</code>")
        lines.append(f"  • lane rule: <code>{escape(str(decision.lane))}</code>")
        if decision.regime:
            lines.append(f"  • regime: <code>{escape(decision.regime)}</code>")
        lines.append(
            "  • order plan: "
            f"side=<code>{escape(str(decision.side))}</code>, "
            f"strategy=<code>{escape(str(decision.strategy))}</code>, "
            f"entry=<code>{decision.entry_offset_bp:g}bp post-only</code>, "
            f"size=<code>{decision.size_mult:.2f}x</code>, "
            f"notional=<code>${decision.requested_notional_usdc:.2f}</code>"
        )
        if decision.risk_tags:
            lines.append(f"  • risk tags: <code>{escape(', '.join(decision.risk_tags))}</code>")
    else:
        lines.append(f"  • verdict: <b>REJECT</b> / reason=<code>{escape(decision.reason)}</code>")
        if decision.regime:
            lines.append(f"  • regime: <code>{escape(decision.regime)}</code>")

    snapshot = _telegram_feature_snapshot(features)
    if snapshot:
        lines.append(f"  • features: <code>{escape(snapshot)}</code>")
    if gaps:
        lines.append(f"  • missing required features: <code>{escape(', '.join(gaps[:12]))}</code>")
    if preflight:
        lines.append(f"  • live preflight blocks: <code>{escape(', '.join(preflight))}</code>")
    if not execution_wired:
        lines.append("  • status: <code>report-only; Codex v1 execution bridge is wired but disabled</code>")
    elif gaps or preflight:
        lines.append("  • status: <code>blocked; do not route mainnet orders through Codex v1 yet</code>")
    return "\n".join(lines)


def format_codex_v1_signal_overview(
    *,
    execution_wired: bool,
    disabled_lane_names: Sequence[str] = (),
    w6_weak_drift_block_enabled: bool = False,
    w6_weak_drift_threshold_bp: float = -30.0,
    w6_deep_pullback_block_enabled: bool = False,
    w6_deep_pullback_d30_max_bp: float = -30.0,
    w6_deep_pullback_adv3_min_bp: float = 6.5,
    w6_deep_pullback_rsi_max: float = 39.0,
    w6_deep_pullback_vwap_dist_max_bp: float = -50.0,
    w6_deep_pullback_pullback_min_bp: float = 30.0,
    w2a_tight_block_enabled: bool = False,
    w2a_d30_low_bp: float = -20.0,
    w2a_d30_high_bp: float = -5.0,
    w2a_adv3_low_bp: float = 0.0,
    w2a_adv3_high_bp: float = 6.0,
    w2a_bb_lower_dist_low_bp: float = 5.0,
    w2a_bb_lower_dist_high_bp: float = 20.0,
    w1b_tight_block_enabled: bool = False,
    w1b_d30_low_bp: float = -45.0,
    w1b_d30_high_bp: float = 5.0,
    w1b_adv3_high_bp: float = 5.0,
    w1b_bb_lower_dist_high_bp: float = 20.0,
    w1b_reprice_wait_max_seconds: float = 60.0,
) -> str:
    """Static Telegram playbook for /signal.

    This intentionally replaces the old real-time indicator dump.  The goal is
    to show the current live Codex lane map and execution principles in a way
    that stays aligned with the production classifier.
    """

    disabled = {name.strip() for name in disabled_lane_names if str(name).strip()}
    strategy_groups: dict[tuple[str, str], list[LaneSpec]] = {}
    for lane in LANES:
        strategy = lane.strategies[0] if lane.strategies else "UNKNOWN"
        strategy_groups.setdefault((strategy, lane.side), []).append(lane)

    lines = [
        f"🧪 <b>Codex Live Playbook {escape(CODEX_V1_VERSION)}</b>",
        f"  • baseline: <code>{escape(CODEX_V1_BASELINE)}</code>",
        f"  • execution: <code>{'LIVE_WIRED' if execution_wired else 'SHADOW_ONLY'}</code>",
        "",
        "📌 <b>開單原則</b>",
        "  • 現在 /signal 只顯示 Codex lane 地圖，不再顯示舊版即時判別摘要。",
        "  • 只要 signal 命中 lane，系統就依 lane 的 side / entry / size 規則決定是否送單。",
        "  • 全部 entry 都是 <code>post-only maker</code>；maker TTL 到期不追價硬吃 taker。",
        "  • lane 依既定順序比對，第一條命中的 lane 取得優先權。",
        "  • 被停用 lane 仍可做 classifier 研究，但 live 不會下單。",
        "  • Telegram 與 run payload 會同時保留 <code>lane_code</code> 與完整 <code>lane</code> rule 名稱。",
        "",
        "🧭 <b>目前 live 特別規則</b>",
        "  • <code>ANCHOR-L</code> / <code>ANCHOR-S</code>：研究保留，但 live 已停用。",
        (
            "  • <code>W6A</code>：V1.3.7E risk tree；risk&gt;=4 block、risk=3 force50、"
            "stale below-VWAP cap50/shadow-only，只有 fresh low-risk setup 才保留 $200。"
        ),
        "  • <code>W2A</code>：V1.3.1 shadow-only；raw classifier 仍記錄，但 live 不送單。",
        (
            "  • <code>W1B</code>："
            + (
                "啟用 tight block，僅允許 "
                f"d30={w1b_d30_low_bp:g}~{w1b_d30_high_bp:g}bp、"
                f"adv3&lt;={w1b_adv3_high_bp:g}bp、"
                f"bb_lower_dist&lt;={w1b_bb_lower_dist_high_bp:g}bp、"
                f"reprice_wait&lt;={w1b_reprice_wait_max_seconds:g}s。"
                if w1b_tight_block_enabled
                else "目前沒有額外 tight block。"
            )
        ),
        "  • <code>HOT-UP</code>：若 <code>d30&gt;=25</code>、<code>rsi&gt;=56</code>、<code>vwap_dist&gt;=20bp</code>、<code>bb_lower_dist&gt;=35bp</code>，SHORT 直接 veto，只等回踩後的 LONG。",
        "  • <code>STALE-SHORT</code>：若 SHORT 等待超過 60s 且盤未轉弱，直接 veto，不再先落到 W1B。",
        "  • <code>MID-UP</code>：若 SHORT 落在中度上行延伸風險，直接 veto，不再先落到 W1B。",
        "",
        "🗂 <b>Lane 清單</b>",
    ]

    display_order = (
        ("S1_BB_RSI", "LONG"),
        ("S1_BB_RSI", "SHORT"),
        ("S6_TrendPull", "LONG"),
        ("S6_TrendPull", "SHORT"),
        ("S8_TrendSnipe", "LONG"),
        ("S8_TrendSnipe", "SHORT"),
    )
    for strategy, side in display_order:
        lanes = strategy_groups.get((strategy, side), [])
        if not lanes:
            continue
        lines.append(f"  • <b>{escape(strategy)}</b> / <code>{escape(side)}</code>")
        for lane in lanes:
            status = "active"
            extras: list[str] = []
            if lane.name in disabled:
                status = "disabled"
            elif lane.lane_code == "W6A" and w6_weak_drift_block_enabled:
                status = "active+block"
                extras.append(f"drift30&lt;={w6_weak_drift_threshold_bp:g}bp")
                if w6_deep_pullback_block_enabled:
                    extras.append(
                        "deeppullback veto"
                        f"(d30&lt;={w6_deep_pullback_d30_max_bp:g},"
                        f"adv3&gt;={w6_deep_pullback_adv3_min_bp:g},"
                        f"rsi&lt;={w6_deep_pullback_rsi_max:g},"
                        f"vwap&lt;={w6_deep_pullback_vwap_dist_max_bp:g},"
                        f"pb&gt;={w6_deep_pullback_pullback_min_bp:g})"
                    )
            elif lane.lane_code == "W2A":
                status = "shadow-only"
                if w2a_tight_block_enabled:
                    extras.append(f"d30={w2a_d30_low_bp:g}..{w2a_d30_high_bp:g}bp")
                    extras.append(f"adv3={w2a_adv3_low_bp:g}..{w2a_adv3_high_bp:g}bp")
                    extras.append(
                        f"bbdist={w2a_bb_lower_dist_low_bp:g}..{w2a_bb_lower_dist_high_bp:g}bp"
                    )
            elif lane.lane_code == "W1B" and w1b_tight_block_enabled:
                status = "active+block"
                extras.append(f"d30={w1b_d30_low_bp:g}..{w1b_d30_high_bp:g}bp")
                extras.append(f"adv3&lt;={w1b_adv3_high_bp:g}bp")
                extras.append(f"bbdist&lt;={w1b_bb_lower_dist_high_bp:g}bp")
                extras.append(f"wait&lt;={w1b_reprice_wait_max_seconds:g}s")
            extras.append(f"entry={lane.entry_offset_bp:g}bp")
            extras.append(f"size={lane.base_size_mult:.2f}x")
            lines.append(
                "    - "
                f"<code>{escape(lane.lane_code)}</code> "
                f"[{escape(status)}] "
                f"<code>{escape(lane.name)}</code> "
                f"({escape(', '.join(extras))})"
            )

    lines.extend(
        [
            "",
            "🧾 <b>解讀方式</b>",
            "  • <code>lane_code</code> 是短代號，例如 <code>W6A</code>、<code>RP1</code>。",
            "  • <code>lane</code> 是完整規則名，方便回測、live log、Telegram 對帳。",
            "  • 若之後有 skip / fill / close 訊息，應以 lane_code 為第一識別，完整 rule 為第二識別。",
        ]
    )
    return "\n".join(lines)


def live_preflight_rejections(features: Mapping[str, Any]) -> tuple[str, ...]:
    """Checks that must pass immediately before a mainnet order is placed."""

    rejects: list[str] = []
    spread_bp = _feature_value(features, "spread_bp")
    if spread_bp is not None and spread_bp > 1.0:
        rejects.append("spread_gt_1bp")

    feature_age_seconds = _feature_value(features, "feature_age_seconds")
    if feature_age_seconds is not None and feature_age_seconds > 75.0:
        rejects.append("features_stale_gt_75s")

    maker_fee_bp = _feature_value(features, "maker_fee_bp")
    if maker_fee_bp is not None and maker_fee_bp > 0.0:
        rejects.append("maker_fee_not_zero")

    if _truthy(features.get("open_position")):
        rejects.append("open_position_exists")
    if _truthy(features.get("open_entry_order")):
        rejects.append("open_entry_order_exists")
    if _truthy(features.get("open_reduce_order")):
        rejects.append("open_reduce_order_exists")
    if _truthy(features.get("kill_switch")):
        rejects.append("kill_switch_enabled")
    return tuple(rejects)


def _lane_matches(lane: LaneSpec, features: Mapping[str, Any]) -> tuple[bool, set[str]]:
    strategy = _string_feature(features, "strategy")
    side = _string_feature(features, "side")
    if lane.name == "codex_v1_hot_up_extension_pullback_long" and not is_hot_up_extension(features):
        return False, set()
    if strategy not in lane.strategies or side != lane.side:
        return False, set()

    missing: set[str] = set()
    for deny_rule in lane.deny_rules:
        if deny_rule.matches(features):
            return False, missing

    for band in (*lane.bands, *lane.feature_bands):
        ok, missing_feature = band.contains(features)
        if missing_feature:
            missing.add(missing_feature)
        if not ok:
            return False, missing
    return True, missing


def _candidate_lanes_for_features(features: Mapping[str, Any]) -> tuple[LaneSpec, ...]:
    strategy = _string_feature(features, "strategy")
    side = _string_feature(features, "side")
    lanes: list[LaneSpec] = []
    for lane in LANES:
        if lane.name == "codex_v1_hot_up_extension_pullback_long" and not is_hot_up_extension(features):
            continue
        if strategy and strategy not in lane.strategies:
            continue
        if side and side != lane.side:
            continue
        lanes.append(lane)
    return tuple(lanes)


def _lane_gap_summary(
    lane: LaneSpec,
    features: Mapping[str, Any],
    ordinal: int,
) -> tuple[tuple[float, float, int], LaneSpec, bool, list[str]]:
    blockers: list[str] = []
    missing_count = 0
    fail_count = 0
    distance = 0.0

    for band in (*lane.bands, *lane.feature_bands):
        blocker, gap_size, is_missing = _band_gap_summary(band, features)
        if blocker is None:
            continue
        blockers.append(blocker)
        distance += gap_size
        if is_missing:
            missing_count += 1
        else:
            fail_count += 1

    for deny_rule in lane.deny_rules:
        if deny_rule.matches(features):
            fail_count += 1
            distance += 0.25
            blockers.append(f"blocked by {deny_rule.name}")

    accepted = missing_count == 0 and fail_count == 0
    return ((missing_count, fail_count + distance, ordinal), lane, accepted, blockers)


def _band_gap_summary(band: NumericBand, features: Mapping[str, Any]) -> tuple[str | None, float, bool]:
    value = _feature_value(features, band.feature)
    need = _format_band_requirement(band)
    if value is None:
        return (f"{band.feature} missing (need {need})", 10.0, True)
    if value < band.low:
        gap = band.low - value
        return (
            f"{band.feature} {value:.2f} < {band.low:.2f} (need +{gap:.2f})",
            _normalized_gap(gap, band),
            False,
        )
    if value > band.high:
        gap = value - band.high
        return (
            f"{band.feature} {value:.2f} > {band.high:.2f} (need -{gap:.2f})",
            _normalized_gap(gap, band),
            False,
        )
    return (None, 0.0, False)


def _format_band_requirement(band: NumericBand) -> str:
    if band.low <= -10_000.0 and band.high >= 10_000.0:
        return "any"
    if band.low <= -10_000.0:
        return f"<= {band.high:.2f}"
    if band.high >= 10_000.0:
        return f">= {band.low:.2f}"
    return f"{band.low:.2f}..{band.high:.2f}"


def _normalized_gap(gap: float, band: NumericBand) -> float:
    width = band.high - band.low
    scale = width if width > 0 and width < 20_000 else max(abs(band.low), abs(band.high), 10.0)
    return min(abs(gap) / max(scale, 1.0), 1.0)


def _feature_value(features: Mapping[str, Any], feature: str) -> float | None:
    for key in _FEATURE_ALIASES.get(feature, (feature,)):
        if key in features and features[key] is not None:
            try:
                value = float(features[key])
            except (TypeError, ValueError):
                return None
            if isfinite(value):
                return value
    return None


def is_hot_up_extension(features: Mapping[str, Any]) -> bool:
    d30 = _feature_value(features, "d30")
    rsi = _feature_value(features, "rsi")
    vwap_dist = _feature_value(features, "vwap_dist_bp")
    bb_lower_dist = _feature_value(features, "bb_lower_dist_bp")
    return bool(
        d30 is not None
        and rsi is not None
        and vwap_dist is not None
        and bb_lower_dist is not None
        and d30 >= 25.0
        and rsi >= 56.0
        and vwap_dist >= 20.0
        and bb_lower_dist >= 35.0
    )


def is_stale_short_after_upmove(features: Mapping[str, Any]) -> bool:
    side = _string_feature(features, "side")
    wait_s = _feature_value(features, "reprice_wait_elapsed_seconds")
    adv3 = _feature_value(features, "adv3")
    d30 = _feature_value(features, "d30")
    rsi = _feature_value(features, "rsi")
    if side != "SHORT" or wait_s is None or wait_s <= 60.0:
        return False
    return bool(
        (adv3 is not None and adv3 > 5.0)
        or (d30 is not None and d30 > 5.0)
        or (rsi is not None and rsi >= 58.0)
    )



def evaluate_stups_loss_breaker(
    completed_runs: Sequence[Mapping[str, Any]],
    *,
    min_net_loss_usdc: float = 0.04,
    loss_count: int = 2,
    window_seconds: int | None = None,
) -> dict[str, Any]:
    """Return whether recent completed STUP-S losses should pause new entries."""
    threshold = max(1, int(loss_count or 1))
    min_loss = abs(float(min_net_loss_usdc or 0.0))
    completed_count = 0
    net_pnl = 0.0
    loss_rows: list[dict[str, Any]] = []

    def _row_float(row: Mapping[str, Any], key: str) -> float:
        try:
            return float(row.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    for row in completed_runs or ():
        if not isinstance(row, Mapping):
            continue
        signal_raw = row.get("signal_json")
        if isinstance(signal_raw, str):
            try:
                signal = json.loads(signal_raw) if signal_raw else {}
            except json.JSONDecodeError:
                signal = {}
        elif isinstance(signal_raw, Mapping):
            signal = signal_raw
        else:
            signal = {}
        codex = signal.get("codex_v1") if isinstance(signal, Mapping) else {}
        if not isinstance(codex, Mapping):
            codex = {}
        lane_code = str(codex.get("lane_code") or "")
        if lane_code != "STUP-S":
            continue

        completed_count += 1
        realized = _row_float(row, "realized_pnl_usdc")
        commission = _row_float(row, "commission_usdc")
        net = realized - commission
        net_pnl += net
        exit_reason = str(row.get("exit_reason") or row.get("status") or "")
        if net <= -min_loss:
            loss_rows.append(
                {
                    "run_id": str(row.get("run_id") or ""),
                    "exit_reason": exit_reason,
                    "net_pnl_usdc": round(net, 8),
                }
            )

    return {
        "should_block": len(loss_rows) >= threshold and net_pnl < 0.0,
        "lane_code": "STUP-S",
        "completed_count": completed_count,
        "loss_count": len(loss_rows),
        "loss_threshold": threshold,
        "min_net_loss_usdc": round(min_loss, 8),
        "window_seconds": window_seconds,
        "net_pnl_usdc": round(net_pnl, 8),
        "loss_net_pnl_usdc": round(sum(float(x["net_pnl_usdc"]) for x in loss_rows), 8),
        "loss_run_ids": [x["run_id"] for x in loss_rows],
        "loss_reasons": [x["exit_reason"] for x in loss_rows],
    }
def build_stale_upmove_canary_decision(
    features: Mapping[str, Any],
    *,
    strategy: str | None = None,
    side: str | None = None,
) -> CodexV1Decision | None:
    strategy_value = strategy if strategy is not None else _string_feature(features, "strategy")
    side_value = side if side is not None else _string_feature(features, "side")
    if strategy_value != "S1_BB_RSI" or side_value != "SHORT":
        return None

    rng15 = _feature_value(features, "rng15")
    if rng15 is None or rng15 <= STALE_UPMOVE_CANARY_RNG15_MIN_BP:
        return None
    if rng15 > STALE_UPMOVE_CANARY_RNG15_MAX_BP:
        return None

    adv3 = _feature_value(features, "adv3")
    if adv3 is None or adv3 <= STALE_UPMOVE_CANARY_ADV3_MIN_BP:
        return None

    d30 = _feature_value(features, "d30")
    if d30 is None or d30 > STALE_UPMOVE_CANARY_D30_MAX_BP:
        return None

    notional_mult = STALE_UPMOVE_CANARY_NOTIONAL_USDC / BASE_NOTIONAL_USDC
    low_rng_weak_adv_shadow = (
        rng15 <= STALE_UPMOVE_LOW_RNG_MAX_BP
        and adv3 < STALE_UPMOVE_WEAK_ADV3_MAX_BP
    )
    shadow_guards = [STALE_UPMOVE_SL19_SHADOW_TAG]
    base_tags = (
        "post_only_entry",
        "dca_disabled",
        "trail_required",
        *get_short_risk_tags(features),
        "stale_upmove_canary",
        "rng15_gt20",
        "rng15_le100",
        "adv3_gt0",
        "d30_le80",
        "canary_notional_50",
        "tp_no_runner",
        "original_sl_kept",
        STALE_UPMOVE_SL19_SHADOW_TAG,
    )
    if low_rng_weak_adv_shadow:
        base_tags = (*base_tags, STALE_UPMOVE_LOW_RNG_WEAK_ADV_SHADOW_TAG)
        shadow_guards.append(STALE_UPMOVE_LOW_RNG_WEAK_ADV_SHADOW_TAG)
    risk_tags = tuple(dict.fromkeys(base_tags))
    metrics = {
        "policy_note": STALE_UPMOVE_CANARY_POLICY_TAG,
        "policy_tag": STALE_UPMOVE_CANARY_POLICY_TAG,
        "shadow_lane": "SH_SHORT_STALE_UPMOVE_S1",
        "admitted_from_shadow_lane": "SH_SHORT_STALE_UPMOVE_S1",
        "rng15": round(float(rng15), 4),
        "rng15_min_bp": STALE_UPMOVE_CANARY_RNG15_MIN_BP,
        "rng15_max_bp": STALE_UPMOVE_CANARY_RNG15_MAX_BP,
        "adv3": round(float(adv3), 4),
        "adv3_min_bp": STALE_UPMOVE_CANARY_ADV3_MIN_BP,
        "d30": round(float(d30), 4),
        "d30_max_bp": STALE_UPMOVE_CANARY_D30_MAX_BP,
        "admission_guard": "rng15_gt20_adv3_gt0_rng15_le100_d30_le80",
        "applied_notional_cap_usdc": STALE_UPMOVE_CANARY_NOTIONAL_USDC,
        "tp_policy_override": "tp1_40_then_trail_runner",
        "sl_policy": "hard_sl_25bp",
        "sl_policy_shadow": STALE_UPMOVE_SL19_SHADOW_TAG,
        "sl_policy_shadow_bp": STALE_UPMOVE_SL19_SHADOW_BP,
        "sl_policy_shadow_action": "observe_only",
        "sl_policy_shadow_replay_note": (
            "rng15_gt20 subset: 19bp +11.4064bp vs 17.1bp; not live"
        ),
        "shadow_guards": shadow_guards,
        "replay_report": "reports/SH_SHORT_STALE_UPMOVE_S1_FULL_TP_SL_REPLAY_2026-06-24.md",
    }
    if low_rng_weak_adv_shadow:
        metrics.update(
            {
                STALE_UPMOVE_LOW_RNG_WEAK_ADV_SHADOW_TAG: True,
                "low_rng_weak_adv_shadow_action": "observe_only",
                "low_rng_weak_adv_shadow_condition": "rng15_le30_adv3_lt3",
                "low_rng_weak_adv_rng15_max_bp": STALE_UPMOVE_LOW_RNG_MAX_BP,
                "low_rng_weak_adv_adv3_max_bp": STALE_UPMOVE_WEAK_ADV3_MAX_BP,
            }
        )
    return CodexV1Decision(
        accepted=True,
        version=CODEX_V1_VERSION,
        baseline=CODEX_V1_BASELINE,
        lane=STALE_UPMOVE_CANARY_LANE,
        lane_code=STALE_UPMOVE_CANARY_LANE_CODE,
        strategy=strategy_value,
        side=side_value,
        entry_offset_bp=1.0,
        size_mult=notional_mult,
        notional_mult=notional_mult,
        requested_notional_usdc=STALE_UPMOVE_CANARY_NOTIONAL_USDC,
        reason="stale_upmove_guarded_canary",
        regime="stale_short_after_upmove",
        risk_tags=risk_tags,
        metrics=metrics,
        policy_tag=STALE_UPMOVE_CANARY_POLICY_TAG,
        shadow_lane="SH_SHORT_STALE_UPMOVE_S1",
    )
def is_mid_up_extension_short_risk(features: Mapping[str, Any]) -> bool:
    side = _string_feature(features, "side")
    rsi = _feature_value(features, "rsi")
    vwap_dist = _feature_value(features, "vwap_dist_bp")
    adv3 = _feature_value(features, "adv3")
    d30 = _feature_value(features, "d30")
    bb_lower_dist = _feature_value(features, "bb_lower_dist_bp")
    pullback = _feature_value(features, "pullback_from_recent_high_bp")
    if side != "SHORT" or rsi is None or vwap_dist is None:
        return False
    if rsi < 58.0 or vwap_dist < 7.0:
        return False
    return bool(
        (adv3 is not None and adv3 >= 7.0)
        or (d30 is not None and d30 >= 15.0)
        or (bb_lower_dist is not None and bb_lower_dist >= 20.0)
        or (pullback is not None and pullback >= 20.0)
    )


def is_s1_bbrsi_pullback_long_family(features: Mapping[str, Any]) -> bool:
    strategy = _string_feature(features, "strategy")
    side = _string_feature(features, "side")
    if strategy != "S1_BB_RSI" or side != "LONG":
        return False
    score = _feature_value(features, "score")
    d30 = _feature_value(features, "d30")
    adv3 = _feature_value(features, "adv3")
    d3 = _feature_value(features, "d3")
    d5 = _feature_value(features, "d5")
    rsi = _feature_value(features, "rsi")
    bb_lower_dist = _feature_value(features, "bb_lower_dist_bp")
    vwap_dist = _feature_value(features, "vwap_dist_bp")
    pullback = _feature_value(features, "pullback_from_recent_high_bp")
    reclaimed = _feature_value(features, "price_above_or_reclaimed_vwap")
    return bool(
        score is not None
        and d30 is not None
        and adv3 is not None
        and d3 is not None
        and d5 is not None
        and rsi is not None
        and bb_lower_dist is not None
        and vwap_dist is not None
        and pullback is not None
        and reclaimed is not None
        and 62.0 <= score <= 72.0
        and -10.0 <= d30 <= 20.0
        and -5.0 <= adv3 <= 9.0
        and d3 >= -10.0
        and d5 >= -8.0
        and 40.0 <= rsi <= 53.0
        and 2.0 <= bb_lower_dist <= 16.0
        and -7.0 <= vwap_dist <= 2.0
        and 8.0 <= pullback <= 28.0
        and reclaimed == 0.0
    )


def match_s1_bbrsi_ordinary_pullback_long_pre_vwap(features: Mapping[str, Any]) -> bool:
    lane = _s1p_l_lane()
    matched, _missing = _lane_matches(lane, features)
    if not matched:
        return False
    wait_s = _feature_value(features, "reprice_wait_elapsed_seconds")
    return wait_s is None or wait_s <= 180.0


def build_s1p_l_enter_decision(features: Mapping[str, Any]) -> CodexV1Decision:
    lane = _s1p_l_lane()
    _matched, missing = _lane_matches(lane, features)
    size_mult = _size_mult(lane, features)
    notional_mult = _notional_mult(lane, features, size_mult)
    return CodexV1Decision(
        accepted=True,
        version=CODEX_V1_VERSION,
        baseline=CODEX_V1_BASELINE,
        lane=lane.name,
        lane_code=lane.lane_code,
        strategy=_string_feature(features, "strategy"),
        side=_string_feature(features, "side"),
        entry_offset_bp=lane.entry_offset_bp,
        size_mult=size_mult,
        notional_mult=notional_mult,
        requested_notional_usdc=BASE_NOTIONAL_USDC * notional_mult,
        reason="s1p_l_match",
        regime="ordinary_pullback_long_pre_vwap",
        missing_features=tuple(sorted(missing)),
        risk_tags=_risk_tags(lane, size_mult),
    )


def build_s1p_l_wait_decision(features: Mapping[str, Any]) -> CodexV1Decision:
    lane = _s1p_l_lane()
    wait_s = _feature_value(features, "reprice_wait_elapsed_seconds")
    wait_gt180 = wait_s is not None and wait_s > 180.0
    metrics = None
    policy_tag = None
    shadow_lane = None
    reason = "s1p_l_waiting_for_completion"
    risk_tags = ["pre_vwap_pullback_family"]
    if wait_gt180:
        reason = "s1p_l_wait_gt180_block"
        policy_tag = "SH_S1P_L_WAIT_GT180"
        shadow_lane = "SH_S1P_L_WAIT_GT180"
        risk_tags.append("s1p_l_wait_gt180")
        metrics = {
            "policy_note": policy_tag,
            "policy_tag": policy_tag,
            "shadow_lane": shadow_lane,
            "reprice_wait_elapsed_seconds": round(float(wait_s), 4),
        }
    return CodexV1Decision(
        accepted=False,
        version=CODEX_V1_VERSION,
        baseline=CODEX_V1_BASELINE,
        lane=lane.name,
        lane_code=lane.lane_code,
        strategy=_string_feature(features, "strategy"),
        side=_string_feature(features, "side"),
        entry_offset_bp=None,
        size_mult=0.0,
        notional_mult=0.0,
        requested_notional_usdc=0.0,
        reason=reason,
        regime="ordinary_pullback_long_pre_vwap",
        risk_tags=tuple(risk_tags),
        metrics=metrics,
        policy_tag=policy_tag,
        shadow_lane=shadow_lane,
    )


def get_short_risk_tags(features: Mapping[str, Any]) -> tuple[str, ...]:
    side = _string_feature(features, "side")
    if side != "SHORT":
        return ()
    tags: list[str] = []
    wait_s = _feature_value(features, "reprice_wait_elapsed_seconds")
    adv3 = _feature_value(features, "adv3")
    d30 = _feature_value(features, "d30")
    rsi = _feature_value(features, "rsi")
    bb_lower_dist = _feature_value(features, "bb_lower_dist_bp")
    pullback = _feature_value(features, "pullback_from_recent_high_bp")

    if wait_s is not None and wait_s > 60.0:
        tags.append("wait_gt_60")
    if adv3 is not None and adv3 > 5.0:
        tags.append("adv3_gt_5")
    if d30 is not None and d30 > 5.0:
        tags.append("d30_positive_gt_5")
    if d30 is not None and d30 >= 15.0:
        tags.append("d30_mid_up_extension")
    if rsi is not None and rsi >= 62.0:
        tags.append("rsi_hot")
    if bb_lower_dist is not None and bb_lower_dist >= 20.0:
        tags.append("bbdist_extended")
    if bb_lower_dist is not None and bb_lower_dist >= 35.0:
        tags.append("bbdist_very_extended")
    if pullback is not None and pullback >= 20.0:
        tags.append("deep_pullback_from_high_but_still_up_structure")
    return tuple(tags)


def _hot_up_extension_pullback_lane() -> LaneSpec:
    for lane in LANES:
        if lane.name == "codex_v1_hot_up_extension_pullback_long":
            return lane
    raise KeyError("codex_v1_hot_up_extension_pullback_long")


def _s1p_l_lane() -> LaneSpec:
    for lane in LANES:
        if lane.name == "codex_v1_s1_bbrsi_ordinary_pullback_long_pre_vwap":
            return lane
    raise KeyError("codex_v1_s1_bbrsi_ordinary_pullback_long_pre_vwap")


def _string_feature(features: Mapping[str, Any], feature: str) -> str | None:
    for key in _FEATURE_ALIASES.get(feature, (feature,)):
        value = features.get(key)
        if value is not None:
            return str(value).upper() if feature == "side" else str(value)
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _size_mult(lane: LaneSpec, features: Mapping[str, Any]) -> float:
    if lane.name != "anchor_s1_preblock_broad_su6_exitA":
        return lane.base_size_mult

    sizeup_bands = (
        _b("score", 60.0),
        _b("rng15", 26.0, 46.0),
        _b("d30", -4.0, 22.0),
        _b("adv3", 3.0, 10.5),
    )
    if all(band.contains(features)[0] for band in sizeup_bands):
        return 6.0
    return lane.base_size_mult


def _notional_mult(lane: LaneSpec, features: Mapping[str, Any], size_mult: float) -> float:
    rng15 = _feature_value(features, "rng15")
    if (
        rng15 is not None
        and lane.scale_rng_low_bp is not None
        and lane.scale_rng_high_bp is not None
        and lane.scale_rng_low_bp <= rng15 <= lane.scale_rng_high_bp
    ):
        return size_mult * lane.scale_factor
    return size_mult


def _risk_tags(lane: LaneSpec, size_mult: float) -> tuple[str, ...]:
    tags = ["post_only_entry", "dca_disabled", "trail_required"]
    if lane.entry_offset_bp == 0:
        tags.append("zero_bp_entry")
    if size_mult > 1:
        tags.append("sizeup")
    if "reprice" in lane.name:
        tags.append("reprice_shadow_required")
    return tuple(tags)


def _telegram_feature_snapshot(features: Mapping[str, Any]) -> str:
    parts: list[str] = []
    text_features = ("symbol", "strategy", "side")
    for key in text_features:
        value = _string_feature(features, key)
        if value:
            parts.append(f"{key}={value}")
    numeric_features = (
        "score",
        "rng15",
        "d30",
        "adv3",
        "d3",
        "d5",
        "range_bp",
        "rsi",
        "bb_lower_dist_bp",
        "vwap_dist_bp",
        "pullback_from_recent_high_bp",
        "price_above_or_reclaimed_vwap",
    )
    for key in numeric_features:
        value = _feature_value(features, key)
        if value is None:
            continue
        parts.append(f"{key}={value:.2f}")
    return ", ".join(parts)


def _populate_candle_derived_features(
    features: dict[str, Any],
    candles: Any,
    *,
    feature_series: Mapping[str, Any] | None = None,
    index: int | None = None,
) -> None:
    try:
        n = len(candles)
    except TypeError:
        return
    if n <= 0:
        return
    i = n - 1 if index is None else index
    if i < 0:
        i = n + i
    if i < 0 or i >= n:
        return

    close = _candle_value(candles[i], "close")
    high = _candle_value(candles[i], "high")
    low = _candle_value(candles[i], "low")
    if close is None or close <= 0:
        return

    side = _string_feature(features, "side")
    if "range_bp" not in features and high is not None and low is not None:
        features["range_bp"] = _bp(high - low, close)
    if "close_pos" not in features and high is not None and low is not None:
        features["close_pos"] = (close - low) / (high - low) if high > low else 0.5
    if "ret3_bp" not in features:
        features["ret3_bp"] = _ret_bp(candles, i, 3)
    if "d3" not in features:
        features["d3"] = _ret_bp(candles, i, 3)
    if "d5" not in features:
        features["d5"] = _ret_bp(candles, i, 5)
    if "rng15" not in features and i >= 15:
        window = candles[i - 15 : i]
        hi = max((_candle_value(c, "high") or close) for c in window)
        lo = min((_candle_value(c, "low") or close) for c in window)
        features["rng15"] = _bp(hi - lo, close)
    if "pullback_from_recent_high_bp" not in features:
        features["pullback_from_recent_high_bp"] = _pullback_from_recent_extreme_bp(
            candles,
            i,
            side or "LONG",
        )
    if "d30" not in features and i >= 30:
        prev = _candle_value(candles[i - 30], "close")
        if prev and prev > 0:
            features["d30"] = _bp(close - prev, prev)
    if "adv3" not in features and side and i >= 3:
        prev = _candle_value(candles[i - 3], "close")
        if prev and prev > 0:
            features["adv3"] = _bp(prev - close, prev) if side == "LONG" else _bp(close - prev, prev)
    for bars in (5, 15, 30):
        key = f"range_pos_{bars}"
        if key not in features:
            features[key] = _range_position(candles, i, bars)

    if feature_series:
        for key in ("rsi", "bb_lower", "vwap"):
            value = _series_value(feature_series.get(key), i)
            if value is None:
                continue
            if key == "rsi" and "rsi" not in features:
                features["rsi"] = value
            elif key == "bb_lower" and "bb_lower_dist_bp" not in features:
                features["bb_lower_dist_bp"] = _bp(close - value, close)
            elif key == "vwap" and "vwap_dist_bp" not in features:
                features["vwap_dist_bp"] = _bp(close - value, close)
        vwap_now = _series_value(feature_series.get("vwap"), i)
        vwap_prev = _series_value(feature_series.get("vwap"), i - 1) if i > 0 else None
        prev_close = _candle_value(candles[i - 1], "close") if i > 0 else None
        if "price_above_or_reclaimed_vwap" not in features and vwap_now is not None:
            reclaimed = bool(close >= vwap_now)
            if not reclaimed and prev_close is not None and vwap_prev is not None:
                reclaimed = prev_close < vwap_prev and close >= vwap_now
            features["price_above_or_reclaimed_vwap"] = 1.0 if reclaimed else 0.0


def _candle_value(candle: Any, key: str) -> float | None:
    if isinstance(candle, Mapping):
        value = candle.get(key)
    else:
        value = getattr(candle, key, None)
    try:
        value_f = float(value)
    except (TypeError, ValueError):
        return None
    return value_f if isfinite(value_f) else None


def _series_value(series: Any, index: int) -> float | None:
    try:
        value = series[index]
    except (IndexError, KeyError, TypeError):
        return None
    try:
        value_f = float(value)
    except (TypeError, ValueError):
        return None
    return value_f if isfinite(value_f) else None


def _bp(num: float, den: float) -> float:
    return num / den * 1e4 if den else 0.0


def _ret_bp(candles: Any, index: int, bars: int) -> float:
    if index < bars:
        return 0.0
    close = _candle_value(candles[index], "close")
    prev = _candle_value(candles[index - bars], "close")
    if close is None or prev is None or prev <= 0:
        return 0.0
    return _bp(close - prev, prev)


def _range_position(candles: Any, index: int, bars: int) -> float:
    start = max(0, index - bars)
    window = candles[start:index]
    if not window:
        return 0.5
    close = _candle_value(candles[index], "close")
    if close is None:
        return 0.5
    hi = max((_candle_value(c, "high") or close) for c in window)
    lo = min((_candle_value(c, "low") or close) for c in window)
    return (close - lo) / (hi - lo) if hi > lo else 0.5


def _pullback_from_recent_extreme_bp(candles: Any, index: int, side: str, lookback_bars: int = 15) -> float:
    close = _candle_value(candles[index], "close")
    if close is None or close <= 0:
        return 0.0
    start = max(0, index - lookback_bars + 1)
    window = candles[start : index + 1]
    if not window:
        return 0.0
    side_upper = str(side or "").upper()
    if side_upper == "LONG":
        extreme = max((_candle_value(c, "high") or close) for c in window)
        return max(0.0, _bp(extreme - close, close))
    extreme = min((_candle_value(c, "low") or close) for c in window)
    return max(0.0, _bp(close - extreme, close))


def _reject(
    reason: str,
    *,
    features: Mapping[str, Any] | None = None,
    strategy: str | None = None,
    side: str | None = None,
    regime: str | None = None,
    risk_tags: Sequence[str] = (),
) -> CodexV1Decision:
    return CodexV1Decision(
        accepted=False,
        version=CODEX_V1_VERSION,
        baseline=CODEX_V1_BASELINE,
        lane=None,
        lane_code=None,
        strategy=strategy if strategy is not None else (_string_feature(features or {}, "strategy") if features else None),
        side=side if side is not None else (_string_feature(features or {}, "side") if features else None),
        entry_offset_bp=None,
        size_mult=0.0,
        notional_mult=0.0,
        requested_notional_usdc=0.0,
        reason=reason,
        regime=regime,
        risk_tags=tuple(risk_tags),
    )


_LANE_CODE_BY_KEY: dict[tuple[str, str | None], str] = {
    ("anchor_s1_preblock_broad_su6_exitA", "LONG"): "ANCHOR-L",
    ("anchor_s1_preblock_broad_su6_exitA", "SHORT"): "ANCHOR-S",
    ("reprice_s1_longonly_w1_e1_longblock", "LONG"): "RP1",
    ("w2_lane_s1long_score64_74_rng35_55_e0_block", "LONG"): "W2A",
    ("w5_lane_s6short_score79_84_rng0_34_e0", "SHORT"): "W5A",
    ("w3_lane_s6long_rng39_71_advneg5_14_range5_25_e0", "LONG"): "W3A",
    ("w1_lane_s6short_score69_79_rsi35_43_e0", "SHORT"): "W1A",
    ("w6_lane_s1long_rng38_86_range9_15_e0", "LONG"): "W6A",
    ("w5_lane_s6short_score72_77_bblower7_35_e0", "SHORT"): "W5B",
    ("w4_lane_s6long_advneg12_neg2_bblower31_47_score86_d30_13_e0", "LONG"): "W4A",
    ("w7_lane_s6short_range6_12_bblowerneg10_5_e0_advopen", "SHORT"): "W7A",
    ("w1_lane_s1short_score71_76_range3_9_e0_advopen", "SHORT"): "W1B",
    ("w3_lane_s6long_d30_6_46_advneg5_14_range9_21_e0", "LONG"): "W3B",
    ("w6_lane_s6long_clusterB_vwap45_rp5_03_rp15_07_close08", "LONG"): "W6B",
    ("w2_lane_s6short_score86_rng50_75_d30neg122_neg80_rsi32_42_vwap100_155_advneg5_e0", "SHORT"): "W2B",
    ("w4_lane_s6long_score84_86_rng30_45_d30_20_40_advneg20_2_rsi67_82_vwapneg25_neg10_rp15_08_e0", "LONG"): "W4B",
    ("w1_lane_s6long_score86_rng35_42_d30_38_46_advneg3_0_rsi74_78_vwapneg25_neg18_rp15_085_e0", "LONG"): "W1C",
    ("w6_lane_s8short_bar2432_score72_73_rng35_40_d30neg40_neg35_vwapneg95_neg85_e0", "SHORT"): "W6C",
    ("w1_lane_s1long_d30neg25_15_vwap4_60_advmax15_e0", "LONG"): "W1D",
    ("codex_v1_s1_bbrsi_ordinary_pullback_long_pre_vwap", "LONG"): "S1P-L",
    ("w2_lane_s6short_rng80_112_d30neg56_neg32_e0", "SHORT"): "W2C",
    ("w3_lane_s8short_bar2492_narrow_e0", "SHORT"): "W3C",
    ("w1_lane_s6short_cluster1129_rng57_70_d30neg75_neg49_rsi43_53_vwap38_56_rp15_063_080_e0", "SHORT"): "W1E",
    ("codex_v1_hot_up_extension_pullback_long", "LONG"): "HUE-L",
    (STALE_UPMOVE_CANARY_LANE, "SHORT"): STALE_UPMOVE_CANARY_LANE_CODE,
}


def lane_code_from_name(name: str | None, side: str | None = None) -> str | None:
    if not name:
        return None
    keyed = _LANE_CODE_BY_KEY.get((name, side.upper() if isinstance(side, str) else side))
    if keyed:
        return keyed
    keyed = _LANE_CODE_BY_KEY.get((name, None))
    if keyed:
        return keyed
    match = re.match(r"^(w\d+)_lane_", name, flags=re.IGNORECASE)
    if match:
        return match.group(1).upper()
    if name.startswith("reprice_"):
        return "RP1"
    if name.startswith("anchor_"):
        return "ANCHOR"
    return name.split("_", 1)[0].upper()

