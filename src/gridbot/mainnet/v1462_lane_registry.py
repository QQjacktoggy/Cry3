"""Canonical v1.4.63 registry for legacy Codex lane families.

This module is deliberately data-only.  Admission, shadow collection, promotion
review, and Telegram monitoring can import the same immutable definitions without
importing the live strategy module or silently inheriting its mutable runtime
state.  Values for the frozen lanes are transcribed from
``strategy/codex_v1_live.py`` (the v1.4.61 baseline); later families retain their
original classifier side separately from sides an adaptive tree may emit.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import hashlib
import json
from typing import Any


REGISTRY_VERSION = "v1.4.63"
REGISTRY_SOURCE_VERSION = "_codex_v1.4.61"
LIVE_CONTROL_RULE_IDS = frozenset(
    {
        "v1460.rp1.control",
        "v1460.s1p_pullback.control",
        "v1460.stup_clean.control",
        "v1460.cnl_reclaim.control",
    }
)


class LaneMode(str, Enum):
    LIVE_ALLOWLIST = "LIVE_ALLOWLIST"
    SHADOW_ONLY = "SHADOW_ONLY"
    MIXED = "MIXED"


@dataclass(frozen=True, slots=True)
class NumericBand:
    feature: str
    low: float = -10_000.0
    high: float = 10_000.0


@dataclass(frozen=True, slots=True)
class DenyRule:
    name: str
    bands: tuple[NumericBand, ...]


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    profile_id: str
    entry_bp: float
    tp1_bp: float | None = None
    full_tp_bp: float | None = None
    sl_bp: float | None = None
    be_bp: float | None = None
    partial_exit_pct: float | None = None
    ttl_s: int | None = None
    max_hold_s: int | None = None
    source: str = ""


@dataclass(frozen=True, slots=True)
class StateProfile:
    state: str
    mode: LaneMode
    profile: ExecutionProfile | None
    gate_note: str = ""


@dataclass(frozen=True, slots=True)
class LiveControlContract:
    """Exact registry identity permitted to consume one v1.4.60 CONTROL rule."""

    rule_id: str
    lane_code: str
    safe_lineage_kind: str | None = None


@dataclass(frozen=True, slots=True)
class LegacyLane:
    lane_code: str
    rule_name: str
    source_version: str
    strategies: tuple[str, ...]
    classifier_side: str
    effective_sides: tuple[str, ...]
    entry_offset_bp: float
    bands: tuple[NumericBand, ...] = ()
    feature_bands: tuple[NumericBand, ...] = ()
    deny_rules: tuple[DenyRule, ...] = ()
    base_size_mult: float = 1.0
    scale_rng_low_bp: float | None = 55.0
    scale_rng_high_bp: float | None = 75.0
    scale_factor: float = 1.2
    notional_policy: str = "base_notional_x_size_x_optional_rng_scale"
    intended_mode: LaneMode = LaneMode.SHADOW_ONLY
    default_profile: ExecutionProfile | None = None
    state_profiles: tuple[StateProfile, ...] = ()
    matrix_rule_id: str = "v1460.unknown.shadow"
    promotion_family: str = ""
    notes: str = ""

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-safe payload for monitors and evidence envelopes."""

        payload = _canonical(asdict(self))
        # These aliases keep Telegram/evidence consumers on the same frozen
        # policy identity without asking them to reconstruct hashes.
        payload["mode"] = self.intended_mode.value
        payload["definition_hash"] = lane_definition_hash(self)
        return payload


def _b(feature: str, low: float = -10_000.0, high: float = 10_000.0) -> NumericBand:
    return NumericBand(feature, low, high)


S1_LONG_REPRICE_BLOCK = DenyRule(
    "s1_long_reprice_block",
    (_b("rsi", -10_000.0, 34.464), _b("range_bp", 9.259), _b("ret3_bp", -16.739)),
)
SHORT_LOW_FOLLOWTHROUGH_BLOCK = DenyRule(
    "short_low_followthrough_block",
    (
        _b("score", 60.0, 66.0),
        _b("rng15", 26.0, 38.0),
        _b("d30", -3.0, 14.0),
        _b("adv3", 4.5, 7.8),
    ),
)


def _lane(
    code: str,
    name: str,
    strategies: tuple[str, ...],
    side: str,
    entry: float,
    bands: tuple[NumericBand, ...],
    feature_bands: tuple[NumericBand, ...] = (),
    *,
    deny_rules: tuple[DenyRule, ...] = (),
    base_size_mult: float = 1.0,
    scale_rng_low_bp: float | None = 55.0,
    scale_rng_high_bp: float | None = 75.0,
    intended_mode: LaneMode = LaneMode.SHADOW_ONLY,
    default_profile: ExecutionProfile | None = None,
    state_profiles: tuple[StateProfile, ...] = (),
    matrix_rule_id: str = "v1460.legacy.shadow",
    effective_sides: tuple[str, ...] | None = None,
    notional_policy: str = "base_notional_x_size_x_optional_rng_scale",
    notes: str = "",
) -> LegacyLane:
    return LegacyLane(
        lane_code=code,
        rule_name=name,
        source_version=REGISTRY_SOURCE_VERSION,
        strategies=strategies,
        classifier_side=side,
        effective_sides=effective_sides or (side,),
        entry_offset_bp=entry,
        bands=bands,
        feature_bands=feature_bands,
        deny_rules=deny_rules,
        base_size_mult=base_size_mult,
        scale_rng_low_bp=scale_rng_low_bp,
        scale_rng_high_bp=scale_rng_high_bp,
        notional_policy=notional_policy,
        intended_mode=intended_mode,
        default_profile=default_profile,
        state_profiles=state_profiles,
        matrix_rule_id=matrix_rule_id,
        promotion_family=code,
        notes=notes,
    )


# The 24 frozen base definitions, kept in their original evaluation order.
_FROZEN_BASE_LANES: tuple[LegacyLane, ...] = (
    _lane(
        "HUE-L", "codex_v1_hot_up_extension_pullback_long", ("S1_BB_RSI", "S5_Stoch", "S6_TrendPull"), "LONG", 0.0,
        (_b("score", 75.0, 200.0), _b("d30", 25.0, 90.0), _b("rsi", 56.0, 66.0), _b("vwap_dist_bp", 8.0, 40.0), _b("bb_lower_dist_bp", 35.0)),
        (_b("pullback_from_recent_high_bp", 8.0, 35.0), _b("d3", -8.0), _b("d5", -15.0), _b("price_above_or_reclaimed_vwap", 1.0, 1.0)),
        base_size_mult=0.35, scale_rng_low_bp=None, scale_rng_high_bp=None,
        notes="Hot extension pullback classifier retained for research; live disabled.",
    ),
    _lane("ANCHOR-L", "anchor_s1_preblock_broad_su6_exitA", ("S1_BB_RSI",), "LONG", 3.0,
          (_b("score", 64.0), _b("rng15", 26.0, 30.0), _b("d30", -20.0, -8.0), _b("adv3", 6.0, 10.5)),
          notional_policy="base 1x; anchor size-up to 6x only when score>=60,rng15=26..46,d30=-4..22,adv3=3..10.5; then optional rng scale"),
    _lane("ANCHOR-S", "anchor_s1_preblock_broad_su6_exitA", ("S1_BB_RSI",), "SHORT", 3.0,
          (_b("score", 58.0), _b("rng15", 26.0, 68.0), _b("d30", -28.0, 28.0), _b("adv3", -10_000.0, 11.0)),
          deny_rules=(SHORT_LOW_FOLLOWTHROUGH_BLOCK,),
          notional_policy="base 1x; anchor size-up to 6x only when score>=60,rng15=26..46,d30=-4..22,adv3=3..10.5; then optional rng scale"),
    _lane("RP1", "reprice_s1_longonly_w1_e1_longblock", ("S1_BB_RSI",), "LONG", 1.0,
          (_b("reprice_favorable_bp", -1.0, 4.0), _b("reprice_adverse_bp", -10_000.0, 2.0)),
          deny_rules=(S1_LONG_REPRICE_BLOCK,), intended_mode=LaneMode.LIVE_ALLOWLIST,
          matrix_rule_id="v1460.rp1.control", notes="Live only after raw/final acceptance and all hard risk gates; no downstream reopen."),
    _lane("W2A", "w2_lane_s1long_score64_74_rng35_55_e0_block", ("S1_BB_RSI",), "LONG", 0.0,
          (_b("score", 64.3912, 74.3912), _b("rng15", 35.1624, 55.1624)), deny_rules=(S1_LONG_REPRICE_BLOCK,)),
    _lane("W5A", "w5_lane_s6short_score79_84_rng0_34_e0", ("S6_TrendPull",), "SHORT", 0.0,
          (_b("score", 79.3752, 84.3752), _b("rng15", 0.0, 34.5266))),
    _lane("W3A", "w3_lane_s6long_rng39_71_advneg5_14_range5_25_e0", ("S6_TrendPull",), "LONG", 0.0,
          (_b("rng15", 39.1319, 71.1319), _b("adv3", -5.311, 14.689)), (_b("range_bp", 5.322, 25.322),)),
    _lane("W1A", "w1_lane_s6short_score69_79_rsi35_43_e0", ("S6_TrendPull",), "SHORT", 0.0,
          (_b("score", 69.3497, 79.3497),), (_b("rsi", 35.27, 43.27),)),
    _lane("W6A", "w6_lane_s1long_rng38_86_range9_15_e0", ("S1_BB_RSI",), "LONG", 0.0,
          (_b("rng15", 38.243, 86.243),), (_b("range_bp", 9.13, 15.13),)),
    _lane("W5B", "w5_lane_s6short_score72_77_bblower7_35_e0", ("S6_TrendPull",), "SHORT", 0.0,
          (_b("score", 72.3908, 77.3908),), (_b("bb_lower_dist_bp", 7.305, 35.305),)),
    _lane("W4A", "w4_lane_s6long_advneg12_neg2_bblower31_47_score86_d30_13_e0", ("S6_TrendPull",), "LONG", 0.0,
          (_b("score", 86.0), _b("d30", 13.0, 200.0), _b("adv3", -12.5805, -2.5805)), (_b("bb_lower_dist_bp", 31.346, 47.346),)),
    _lane("W7A", "w7_lane_s6short_range6_12_bblowerneg10_5_e0_advopen", ("S6_TrendPull",), "SHORT", 0.0,
          (_b("rng15", 0.0, 200.0), _b("d30", -200.0, 200.0)), (_b("range_bp", 6.358, 12.358), _b("bb_lower_dist_bp", -10.908, 5.092))),
    _lane("W1B", "w1_lane_s1short_score71_76_range3_9_e0_advopen", ("S1_BB_RSI",), "SHORT", 0.0,
          (_b("score", 71.129, 76.129),), (_b("range_bp", 3.841, 9.841),)),
    _lane("W3B", "w3_lane_s6long_d30_6_46_advneg5_14_range9_21_e0", ("S6_TrendPull",), "LONG", 0.0,
          (_b("d30", 6.6252, 46.6252), _b("adv3", -5.311, 14.689)), (_b("range_bp", 9.322, 21.322),)),
    _lane("W6B", "w6_lane_s6long_clusterB_vwap45_rp5_03_rp15_07_close08", ("S6_TrendPull",), "LONG", 0.0,
          (_b("score", 79.0, 86.1), _b("rng15", 19.0, 31.0), _b("d30", -8.0, 24.0), _b("adv3", -14.0, 10.0)),
          (_b("range_bp", 1.8, 11.0), _b("rsi", 46.0, 64.0), _b("bb_lower_dist_bp", 6.0, 26.0), _b("vwap_dist_bp", -45.0), _b("range_pos_5", 0.3), _b("range_pos_15", 0.7), _b("close_pos", -10_000.0, 0.8))),
    _lane("W2B", "w2_lane_s6short_score86_rng50_75_d30neg122_neg80_rsi32_42_vwap100_155_advneg5_e0", ("S6_TrendPull",), "SHORT", 0.0,
          (_b("score", 85.9, 86.1), _b("rng15", 50.0, 75.0), _b("d30", -122.0, -80.0), _b("adv3", -5.0)),
          (_b("range_bp", 8.0, 20.0), _b("rsi", 32.0, 42.0), _b("bb_lower_dist_bp", 9.0, 47.0), _b("vwap_dist_bp", 100.0, 155.0))),
    _lane("W4B", "w4_lane_s6long_score84_86_rng30_45_d30_20_40_advneg20_2_rsi67_82_vwapneg25_neg10_rp15_08_e0", ("S6_TrendPull",), "LONG", 0.0,
          (_b("score", 84.0, 86.1), _b("rng15", 30.0, 45.0), _b("d30", 20.0, 40.0), _b("adv3", -20.0, 2.0)),
          (_b("range_bp", 5.0, 15.0), _b("rsi", 67.0, 82.0), _b("bb_lower_dist_bp", 35.0, 50.0), _b("vwap_dist_bp", -25.0, -10.0), _b("range_pos_15", 0.8, 1.4))),
    _lane("W1C", "w1_lane_s6long_score86_rng35_42_d30_38_46_advneg3_0_rsi74_78_vwapneg25_neg18_rp15_085_e0", ("S6_TrendPull",), "LONG", 0.0,
          (_b("score", 85.9, 86.1), _b("rng15", 35.0, 42.0), _b("d30", 38.0, 46.0), _b("adv3", -3.0, 0.0)),
          (_b("range_bp", 2.0, 5.0), _b("rsi", 74.0, 78.0), _b("bb_lower_dist_bp", 40.0, 46.0), _b("vwap_dist_bp", -25.0, -18.0), _b("range_pos_15", 0.85, 1.0))),
    _lane("W6C", "w6_lane_s8short_bar2432_score72_73_rng35_40_d30neg40_neg35_vwapneg95_neg85_e0", ("S8_TrendSnipe",), "SHORT", 0.0,
          (_b("score", 72.0, 73.0), _b("rng15", 35.0, 40.0), _b("d30", -40.0, -35.0), _b("adv3", -6.0, -4.0)),
          (_b("range_bp", 14.0, 16.0), _b("rsi", 45.0, 47.0), _b("bb_lower_dist_bp", 20.0, 23.0), _b("vwap_dist_bp", -95.0, -85.0), _b("range_pos_15", 0.45, 0.55))),
    _lane("W1D", "w1_lane_s1long_d30neg25_15_vwap4_60_advmax15_e0", ("S1_BB_RSI",), "LONG", 0.0,
          (_b("rng15", 0.0, 200.0), _b("d30", -25.3765, 14.6235), _b("adv3", -10_000.0, 15.0)), (_b("vwap_dist_bp", 4.201, 60.201),)),
    _lane("S1P-L", "codex_v1_s1_bbrsi_ordinary_pullback_long_pre_vwap", ("S1_BB_RSI",), "LONG", 0.0,
          (_b("score", 64.0, 70.0), _b("d30", -8.0, 18.0), _b("adv3", -3.0, 7.0)),
          (_b("d3", -7.0), _b("d5", -6.0), _b("rsi", 41.5, 50.5), _b("bb_lower_dist_bp", 3.0, 14.5), _b("vwap_dist_bp", -5.5, 0.75), _b("pullback_from_recent_high_bp", 10.0, 24.0), _b("price_above_or_reclaimed_vwap", 0.0, 0.0)),
          base_size_mult=0.20, scale_rng_low_bp=None, scale_rng_high_bp=None,
          intended_mode=LaneMode.LIVE_ALLOWLIST,
          default_profile=ExecutionProfile("v149_s1pl_tiny_profile_fix", 0.0, 6.0, None, 15.0, 0.0, 1.0, 180, source="codex_v1_live.S1P_L_V149_PROFILE"),
          state_profiles=tuple(StateProfile(s, LaneMode.LIVE_ALLOWLIST, None, "raw and final accept required") for s in ("ordinary_pullback", "pre_vwap", "ordinary_pullback_pre_vwap")),
          matrix_rule_id="v1460.s1p_pullback.control"),
    _lane("W2C", "w2_lane_s6short_rng80_112_d30neg56_neg32_e0", ("S6_TrendPull",), "SHORT", 0.0,
          (_b("rng15", 80.1343, 112.1343), _b("d30", -56.0366, -32.0366))),
    _lane("W3C", "w3_lane_s8short_bar2492_narrow_e0", ("S8_TrendSnipe",), "SHORT", 0.0,
          (_b("rng15", 32.0, 35.0), _b("d30", -16.0, -13.0), _b("adv3", -2.0, 1.0)),
          (_b("range_bp", 5.0, 7.0), _b("rsi", 47.0, 49.0), _b("bb_lower_dist_bp", 15.0, 18.0), _b("vwap_dist_bp", -8.0, -4.0), _b("range_pos_15", 0.55, 0.7))),
    _lane("W1E", "w1_lane_s6short_cluster1129_rng57_70_d30neg75_neg49_rsi43_53_vwap38_56_rp15_063_080_e0", ("S6_TrendPull",), "SHORT", 0.0,
          (_b("rng15", 57.0, 70.0), _b("d30", -75.0, -49.0), _b("adv3", -5.0, 16.0)),
          (_b("range_bp", 7.0, 16.0), _b("rsi", 43.0, 53.0), _b("bb_lower_dist_bp", 26.0, 43.0), _b("vwap_dist_bp", 38.0, 56.0), _b("range_pos_15", 0.63, 0.8))),
)


def _profile(profile_id: str, entry: float, tp1: float, full: float | None, sl: float, be: float, pct: float, ttl: int, source: str) -> ExecutionProfile:
    return ExecutionProfile(profile_id, entry, tp1, full, sl, be, pct, ttl, source=source)


_STUP_SOURCE = "codex_v1_live.STUPS_V143_PROFILES"
_STUP_PROFILES = (
    StateProfile("clean_extension", LaneMode.LIVE_ALLOWLIST, _profile("v1420_stups_clean_extension", 2, 6, 80, 8, 2, .70, 60, _STUP_SOURCE), "only clean legacy gate pass; rejected/reopened cases stay shadow"),
    StateProfile("strong_up_continuation", LaneMode.LIVE_ALLOWLIST, None, "v1.4.60 clean-family control; all raw/final gates required"),
    StateProfile("mixed", LaneMode.SHADOW_ONLY, _profile("v1420_stups_mixed", 2, 6, 80, 8, 2, .70, 60, _STUP_SOURCE)),
    StateProfile("weak_chop", LaneMode.SHADOW_ONLY, _profile("v1416_stups_weak_chop", 0, 5, 12, 10, 4, .60, 90, _STUP_SOURCE)),
    StateProfile("no_momentum_edge", LaneMode.SHADOW_ONLY, _profile("v143_stups_no_momentum", 1, 8, None, 15, 0, 1, 90, _STUP_SOURCE)),
    StateProfile(
        "hot_continuation",
        LaneMode.LIVE_ALLOWLIST,
        _profile("v143_stups_hot_continuation", 0, 4, None, 4, 0, .40, 45, _STUP_SOURCE),
        "v1.4.60 clean-control matrix state; raw legacy reject, any gate reject, or reject->reopen lineage is still SHADOW_ONLY",
    ),
    StateProfile("stale_squeeze_top", LaneMode.SHADOW_ONLY, _profile("v143_stups_stale_squeeze_top", 0, 4, None, 4, 0, .40, 45, _STUP_SOURCE)),
    StateProfile("counter_recoil", LaneMode.SHADOW_ONLY, None),
    StateProfile("near_vwap_flat", LaneMode.SHADOW_ONLY, None),
    StateProfile("missing_features", LaneMode.SHADOW_ONLY, None),
)

_CNL_SOURCE = "mainnet.one_run.WPR_V143_PROFILES"
_CNL_PROFILES = (
    StateProfile("fast_reclaim", LaneMode.LIVE_ALLOWLIST, _profile("v143_wpr_fast_reclaim", 0, 6, None, 6, 0, 1, 45, _CNL_SOURCE), "final gate decides; no reopen"),
    StateProfile("discount_mixed", LaneMode.LIVE_ALLOWLIST, _profile("v1420_wpr_discount_mixed_runner", 0, 5, 16, 15, 2, .45, 150, _CNL_SOURCE), "final gate decides; no reopen"),
    StateProfile("discount_delayed_reclaim", LaneMode.LIVE_ALLOWLIST, _profile("v1420_wpr_delayed_reclaim", 3, 5, 8, 8, 2, .70, 75, _CNL_SOURCE), "final gate decides; no reopen"),
    StateProfile("deep_discount_stable", LaneMode.SHADOW_ONLY, _profile("v143_wpr_deep_discount", 2, 8, None, 8, 2, .40, 180, _CNL_SOURCE)),
    StateProfile("falling_discount_trap", LaneMode.SHADOW_ONLY, _profile("v1416_wpr_falling_trap", 0, 4, 10, 15, 4, .60, 90, _CNL_SOURCE)),
    StateProfile("falling_continuation_probe", LaneMode.SHADOW_ONLY, _profile("v1420_wpr_falling_continuation", 3, 6, 8, 10, 2, .40, 60, _CNL_SOURCE)),
    StateProfile("ambiguous", LaneMode.SHADOW_ONLY, None),
    StateProfile("missing_features", LaneMode.SHADOW_ONLY, None),
)

_LATER_FAMILIES: tuple[LegacyLane, ...] = (
    _lane(
        "STUP-S", "codex_v1_stale_upmove_short_rng20_canary", ("S1_BB_RSI",), "SHORT", 2.0,
        (_b("rng15", 20.0, 100.0), _b("adv3", 0.0), _b("d30", -10_000.0, 80.0)),
        intended_mode=LaneMode.MIXED, state_profiles=_STUP_PROFILES,
        matrix_rule_id="v1460.stup_clean.control+v1460.stup_weak.shadow",
        effective_sides=("SHORT", "LONG"), notional_policy="fixed 50 USDC cap before adaptive profile/risk scaling",
        notes="Classifier is SHORT; adaptive trees may emit LONG. Preserve classifier_side, effective_side, raw_action, and effective_action. v1.4.60 matrix control never overrides a raw/pre-gate reject or reject->reopen lineage.",
    ),
    _lane(
        "SFD-S", "codex_v1_strong_fall_follow_short_canary", ("S1_BB_RSI",), "SHORT", 2.0,
        (_b("rng15", 70.0), _b("d30", -10_000.0, -35.0), _b("vwap_dist_bp", -10_000.0, -8.0), _b("rsi", -10_000.0, 55.0), _b("range_pos_15_or_close_pos", -10_000.0, .45)),
        (_b("adv3", -10_000.0, 12.0),),
        default_profile=_profile("v1414_strong_fall_follow_exec", 2, 6, 8, 10, 2, .40, 90, "codex_v1_live.STRONG_FALL_FOLLOW_PROFILE"),
        matrix_rule_id="v1460.other_incumbent_fallback.shadow", notional_policy="fixed 50 USDC cap",
        notes="Explicitly shadow-only in v1.4.63 despite earlier canary execution history.",
    ),
    _lane(
        "CNL-WPR-L", "v139_canary_watch_pre_reprice_long_s1", ("S1_BB_RSI",), "LONG", 3.0, (),
        intended_mode=LaneMode.MIXED, state_profiles=_CNL_PROFILES,
        matrix_rule_id="v1460.cnl_reclaim.control+v1460.cnl_risk.shadow",
        effective_sides=("LONG", "SHORT"), notional_policy="fixed 50 USDC canary cap before adaptive profile/risk scaling",
        notes="Promoted from SH_WPR_L_S1; classifier is LONG while adaptive trees may emit either side. Raw rejects cannot be reopened.",
    ),
)


LANES: tuple[LegacyLane, ...] = _FROZEN_BASE_LANES + _LATER_FAMILIES
LANE_BY_CODE: dict[str, LegacyLane] = {lane.lane_code: lane for lane in LANES}

CNL_SAFE_LINEAGE_KIND = "v1463.cnl_wpr_no_lane_reprice_control"
LIVE_CONTROL_CONTRACTS: tuple[LiveControlContract, ...] = (
    LiveControlContract("v1460.rp1.control", "RP1"),
    LiveControlContract("v1460.s1p_pullback.control", "S1P-L"),
    LiveControlContract("v1460.stup_clean.control", "STUP-S"),
    LiveControlContract(
        "v1460.cnl_reclaim.control",
        "CNL-WPR-L",
        safe_lineage_kind=CNL_SAFE_LINEAGE_KIND,
    ),
)
LIVE_CONTROL_CONTRACT_BY_RULE_ID: dict[str, LiveControlContract] = {
    item.rule_id: item for item in LIVE_CONTROL_CONTRACTS
}


def _canonical(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_canonical(item) for item in value]
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in value.items()}
    return value


def lane_definition_hash(lane: LegacyLane) -> str:
    payload = json.dumps(_canonical(asdict(lane)), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def registry_hash() -> str:
    payload = {
        "registry_version": REGISTRY_VERSION,
        "lanes": [_canonical(asdict(lane)) for lane in LANES],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


REGISTRY_HASH = registry_hash()
LANE_REGISTRY: tuple[LegacyLane, ...] = LANES
LEGACY_LANE_REGISTRY: tuple[dict[str, Any], ...] = tuple(lane.to_payload() for lane in LANES)
# Explicit monitor aliases.  Keep the object and JSON-safe forms separate so
# callers cannot accidentally mutate the policy object while rendering it.
MONITOR_LANE_REGISTRY: tuple[dict[str, Any], ...] = LEGACY_LANE_REGISTRY
V1462_LANE_REGISTRY: tuple[LegacyLane, ...] = LANE_REGISTRY


def registry_payload() -> tuple[dict[str, Any], ...]:
    """Return the immutable JSON-safe registry contract used by monitors."""

    return LEGACY_LANE_REGISTRY


def lane_for(code: str) -> LegacyLane:
    """Return a lane by case-insensitive code; unknown lanes fail closed."""

    normalized = str(code or "").strip().upper()
    try:
        return LANE_BY_CODE[normalized]
    except KeyError as exc:
        raise KeyError(f"unknown v1.4.63 lane: {code!r}") from exc


def state_mode(code: str, state: str | None) -> LaneMode:
    """Resolve intended mode, failing closed for absent or unknown states."""

    lane = lane_for(code)
    if not lane.state_profiles:
        return lane.intended_mode
    normalized = str(state or "").strip()
    if ":" in normalized:
        normalized = normalized.split(":", 1)[1]
    for item in lane.state_profiles:
        if item.state == normalized:
            return item.mode
    return LaneMode.SHADOW_ONLY


def state_profile_for(code: str, state: str | None) -> StateProfile | None:
    """Return the exact state profile, never a nearby/default state."""

    lane = lane_for(code)
    normalized = str(state or "").strip()
    if ":" in normalized:
        normalized = normalized.split(":", 1)[1]
    for item in lane.state_profiles:
        if item.state == normalized:
            return item
    return None


def live_control_contract(rule_id: str | None) -> LiveControlContract:
    """Resolve an allowlisted control rule; unknown rules fail closed."""

    normalized = str(rule_id or "").strip()
    try:
        return LIVE_CONTROL_CONTRACT_BY_RULE_ID[normalized]
    except KeyError as exc:
        raise KeyError(f"unknown v1.4.63 live control rule: {rule_id!r}") from exc


def monitor_rows() -> tuple[dict[str, Any], ...]:
    """Return the fixed left side of the Telegram lane-monitor join."""

    return tuple(
        {
            "lane_code": lane.lane_code,
            "mode": lane.intended_mode.value,
            "classifier_side": lane.classifier_side,
            "definition_hash": lane_definition_hash(lane),
            "captured": 0,
            "complete": 0,
            "incomplete": 0,
        }
        for lane in LANES
    )


if len(LANES) != 27 or len(LANE_BY_CODE) != 27:  # import-time integrity guard
    raise RuntimeError("v1.4.63 registry must contain exactly 27 unique lanes")


__all__ = [
    "DenyRule",
    "ExecutionProfile",
    "CNL_SAFE_LINEAGE_KIND",
    "LANES",
    "LANE_BY_CODE",
    "LANE_REGISTRY",
    "LEGACY_LANE_REGISTRY",
    "MONITOR_LANE_REGISTRY",
    "LIVE_CONTROL_RULE_IDS",
    "LIVE_CONTROL_CONTRACTS",
    "LIVE_CONTROL_CONTRACT_BY_RULE_ID",
    "LiveControlContract",
    "LaneMode",
    "LegacyLane",
    "NumericBand",
    "REGISTRY_HASH",
    "REGISTRY_SOURCE_VERSION",
    "REGISTRY_VERSION",
    "StateProfile",
    "V1462_LANE_REGISTRY",
    "lane_definition_hash",
    "lane_for",
    "live_control_contract",
    "monitor_rows",
    "registry_hash",
    "registry_payload",
    "state_mode",
    "state_profile_for",
]
