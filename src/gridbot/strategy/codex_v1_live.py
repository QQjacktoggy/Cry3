"""Live policy gate for the accepted Codex V6.8.5 research bundle.

The backtest result this captures is the 21-branch portfolio union ending at
``portfolio_union_21branch_w1s6short_cluster1129``.  This module is deliberately
pure: it decides whether an already-built candidate belongs to one of the
accepted lanes, but it does not place orders or compute indicators from raw
candles.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite
from html import escape
import json
import re
from typing import Any, Mapping, Sequence

from src.gridbot.strategy.codex_v1427_tree import (
    V1427_FDT_RNG90_BLOCK_BY_WINDOW,
    V1427_FIVE_WINDOW_TREE,
    V1427_FIVE_WINDOW_TREE_SOURCE,
    V1427_OVERLAY_SOURCE,
)


CODEX_V1_VERSION = "_codex_v1.4.69"
CODEX_V1_BASELINE = "portfolio_union_21branch_w1s6short_cluster1129"
BASE_NOTIONAL_USDC = 50.0
V1421_DECISION_TREE_SOURCE = "reports/v1421_decision_tree_three_window_success.md"
V1421_POLICY_TAG = "v1421_decision_tree_adaptive_exec"
V1421_REQUIRED_FEATURES = (
    "slope30",
    "slope60",
    "slope120",
    "rng15",
    "d30",
    "adv3",
    "rsi",
    "vwap",
    "range_pos",
    "pullback",
)
V1421_LIVE_LANE_CODES = frozenset({"CNL-WPR-L", "STUP-S", "SFD-S"})
V1421_ACTION_PROFILES: dict[str, dict[str, Any]] = {
    "LONG_DEEP_TP4_SL6": {
        "side": "LONG",
        "entry_bp": 2.0,
        "tp1_bp": 4.0,
        "full_tp_bp": 8.0,
        "sl_bp": 6.0,
        "be_bp": 2.0,
        "partial_exit_pct": 1.0,
        "ttl_s": 180,
        "profile_anchor": "long_deep_e2_tp4_full8_sl6_p100_ttl180",
    },
    "LONG_FALL_TP8_SL6": {
        "side": "LONG",
        "entry_bp": 4.0,
        "tp1_bp": 8.0,
        "full_tp_bp": 20.0,
        "sl_bp": 6.0,
        "be_bp": 4.0,
        "partial_exit_pct": 0.5,
        "ttl_s": 180,
        "profile_anchor": "long_fall_e4_tp8_full20_sl6_p50_ttl180",
    },
    "LONG_OVERSOLD_E8_TP8": {
        "side": "LONG",
        "entry_bp": 8.0,
        "tp1_bp": 8.0,
        "full_tp_bp": 30.0,
        "sl_bp": 6.0,
        "be_bp": 2.0,
        "partial_exit_pct": 0.5,
        "ttl_s": 180,
        "profile_anchor": "long_oversold_e8_tp8_full30_sl6_p50_ttl180",
    },
    "SHORT_FAST30_E2": {
        "side": "SHORT",
        "entry_bp": 2.0,
        "tp1_bp": 30.0,
        "full_tp_bp": 30.0,
        "sl_bp": 8.0,
        "be_bp": 0.0,
        "partial_exit_pct": 1.0,
        "ttl_s": 60,
        "hold_s": 2100,
        "profile_anchor": "short_fast_e2_tp30_full30_sl8_p100_ttl60",
    },
    "SHORT_RUNNER_E2": {
        "side": "SHORT",
        "entry_bp": 2.0,
        "tp1_bp": 30.0,
        "full_tp_bp": 100.0,
        "sl_bp": 12.0,
        "be_bp": 2.0,
        "partial_exit_pct": 0.3,
        "ttl_s": 60,
        "hold_s": 2100,
        "profile_anchor": "short_runner_e2_tp30_full100_sl12_p30_ttl60",
    },
    "SHORT_RUNNER_E6": {
        "side": "SHORT",
        "entry_bp": 6.0,
        "tp1_bp": 30.0,
        "full_tp_bp": 100.0,
        "sl_bp": 12.0,
        "be_bp": 2.0,
        "partial_exit_pct": 0.3,
        "ttl_s": 180,
        "hold_s": 2100,
        "profile_anchor": "short_runner_e6_tp30_full100_sl12_p30_ttl180",
    },
    "SHORT_WIDE_E8": {
        "side": "SHORT",
        "entry_bp": 8.0,
        "tp1_bp": 60.0,
        "full_tp_bp": 120.0,
        "sl_bp": 15.0,
        "be_bp": 2.0,
        "partial_exit_pct": 0.2,
        "ttl_s": 60,
        "hold_s": 2100,
        "profile_anchor": "short_wide_e8_tp60_full120_sl15_p20_ttl60",
    },
}
V1423_DECISION_TREE_SOURCE = "reports/v1423_four_window_conservative_tree_target1p4_summary.md"
V1423_POLICY_TAG = "v1423_four_window_conservative_tree_exec"
V1424_STUPS_BASE_BLOCK_TAG = "v1424_stups_base_shadow_block"
V1425_STUPS_WEAK_CHOP_DIRECT_LONG_BLOCK_TAG = "v1425_stups_weak_chop_direct_long_block"
V1425_WPR_FALLING_TRAP_DIRECT_LONG_BLOCK_TAG = "v1425_wpr_falling_trap_direct_long_block"
V1425_WPR_FALLING_TRAP_SHORT_SCALP_TAG = "v1425_wpr_falling_trap_short_scalp_exec"
V1426_WPR_BASE_FALLBACK_BLOCK_TAG = "v1426_wpr_base_fallback_shadow_block"
V1426_WPR_FALLING_TRAP_SHORT_SCALP_TAG = "v1426_wpr_falling_trap_short_scalp_exec"
V1426_STUPS_WEAK_CHOP_SHORT_BLOCK_TAG = "v1426_stups_weak_chop_short_block"
V1426_STUPS_MIXED_SHORT_BLOCK_TAG = "v1426_stups_mixed_short_block"
V1423_PROJECTED_50_NET_USDC = 1.4849
V1423_TARGET_50_NET_USDC = 1.4
V1423_TARGET_WR = 0.75
V1423_MAX_TP_BP = 20.0
V1423_LIVE_LANE_CODES = V1421_LIVE_LANE_CODES
V1423_REQUIRED_FEATURES = V1421_REQUIRED_FEATURES
V1427_DECISION_TREE_SOURCE = V1427_FIVE_WINDOW_TREE_SOURCE
V1427_POLICY_TAG = "v1427_five_window_tp14_adaptive_exec"
V1427_BASE_PASSTHROUGH_TAG = "v1427_base_passthrough"
V1427_TREE_BLOCK_TAG = "v1427_five_window_tree_block"
V1427_W1D_BLOCK_TAG = "v1427_w1d_unvalidated_block"
V1427_FDT_RNG90_BLOCK_TAG = "v1427_fdt_rng90_block"
V1427_MISSING_FEATURE_BLOCK_TAG = "v1427_missing_features_block"
V1428_LEGACY_STUPS_REOPEN_TAG = "v1428_legacy_stups_reopen_for_tree_profile"
V1429_STUPS_STALE_SIDE_OVERRIDE_BLOCK_TAG = "v1429_stups_stale_side_override_block"
V1429_STUPS_STALE_SIDE_OVERRIDE_STATES = frozenset({"STUP-S:mixed", "STUP-S:counter_recoil"})
V1429_STUPS_STALE_SIDE_OVERRIDE_WAIT_S = 300.0
V1433_STUPS_CLEAN_HIGH_OVERRIDE_BLOCK_TAG = "v1433_stups_clean_high_override_block"
V1433_STUPS_CLEAN_HIGH_OVERRIDE_RANGE_POS_MIN = 0.95
V1433_STUPS_CLEAN_HIGH_OVERRIDE_VWAP_MIN_BP = 50.0
V1428_LEGACY_STUPS_REOPEN_REASONS = frozenset(
    {
        "v1420_stups_clean_extension_gate_block",
        "v1420_stups_mixed_bad_block",
        "v1420_stups_mixed_weakzone_block",
        "v1420_stups_weak_chop_extreme_block",
        "v143_stups_counter_recoil_shadow_only",
        "v143_stups_hot_continuation_shadow_only",
        "v143_stups_near_vwap_flat_shadow_only",
    }
)
V1427_LIVE_LANE_CODES = V1421_LIVE_LANE_CODES
V1427_REQUIRED_FEATURES = V1421_REQUIRED_FEATURES
V1427_TARGET_50_NET_USDC = 1.0
V1427_TARGET_WR = 0.82
V1427_MAX_TP_BP = 14.0
V1427_MIN_PROJECTED_50_NET_USDC = min(
    float(row["projected_50_net"])
    for row in V1427_FDT_RNG90_BLOCK_BY_WINDOW.values()
)
V1430_POLICY_TAG = "v1430_loss_prune_exec"
V1430_BLOCK_TAG = "v1430_loss_prune_block"
V1430_MISSING_FEATURE_BLOCK_TAG = "v1430_missing_features_block"
V1436_FAST_RECLAIM_DOWNSLOPE_BLOCK_TAG = "v1436_fast_reclaim_downslope_block"
V1449_CNL_WPR_FALLING_TRAP_QUALITY_BLOCK_TAG = "v1449_cnl_wpr_falling_trap_quality_block"
V1449_CNL_WPR_FAST_RECLAIM_QUALITY_BLOCK_TAG = "v1449_cnl_wpr_fast_reclaim_quality_block"
V1450_CNL_WPR_DEEP_LATE_CHASE_BLOCK_TAG = "v1450_cnl_wpr_deep_late_chase_block"
V1451_STUPS_CLEAN_EXTENSION_SHADOW_REVIEW_BLOCK_TAG = "v1451_stups_clean_extension_shadow_review_block"
V1452_STUPS_LATE_ADVERSE_REOPEN_BLOCK_TAG = "v1452_stups_late_adverse_reopen_block"
V1453_STUPS_CLEAN_EXTENSION_REOPEN_REVIEW_BLOCK_TAG = "v1453_stups_clean_extension_reopen_review_block"
V1455_STUPS_CLEAN_EXTENSION_TP14_BLOCK_TAG = "v1455_stups_clean_extension_tp14_block"
V1455_ADAPTIVE_ROUTE_TAG = "v1455_adaptive_route"
V1455_THIN_SCALP_ROUTE_TP_MAX_BP = 10.0
V1455_BLOCKED_TP_MIN_BP = 14.0
V1430_SOURCE = "reports/v1430_selective_hybrid_loss_prune_outcomefee.json"
V1430_TARGETED_SPLIT_SOURCE = "reports/v1430_selective_hybrid_targeted_split_outcomefee.json"
V1430_TOTAL_METRICS = {
    "attempts": 268,
    "filled": 130,
    "fee_wr": 0.8846153846153846,
    "fee_adjusted_net": 4.95528107,
    "fee_adjusted_p50": 0.92449274,
    "fee_losses": 15,
}
V1430_ACTION_PROFILES: dict[str, dict[str, Any]] = {
    "pnlfirst__p__nf_e0_t180__trail_arm11_gb6_fl5_sl10": {
        "entry_bp": 0.0,
        "tp1_bp": 11.0,
        "full_tp_bp": 11.0,
        "sl_bp": 10.0,
        "be_bp": 0.0,
        "partial_exit_pct": 1.0,
        "ttl_s": 180,
        "hold_s": 30,
        "trail_arm_bp": 11.0,
        "trail_giveback_bp": 6.0,
        "trail_floor_bp": 5.0,
        "profile_anchor": "v1430_trail_arm11_gb6_fl5_sl10",
    },
    "g__nf_e0_t120__trail_arm11_gb5_fl4_sl8": {
        "entry_bp": 0.0,
        "tp1_bp": 11.0,
        "full_tp_bp": 11.0,
        "sl_bp": 8.0,
        "be_bp": 0.0,
        "partial_exit_pct": 1.0,
        "ttl_s": 120,
        "hold_s": 30,
        "trail_arm_bp": 11.0,
        "trail_giveback_bp": 5.0,
        "trail_floor_bp": 4.0,
        "profile_anchor": "v1430_trail_arm11_gb5_fl4_sl8",
    },
    "p__nf_e0_t120__trail_arm11_gb6_fl5_sl10": {
        "entry_bp": 0.0,
        "tp1_bp": 6.0,
        "full_tp_bp": 11.0,
        "sl_bp": 10.0,
        "be_bp": 0.0,
        "partial_exit_pct": 0.70,
        "ttl_s": 120,
        "hold_s": 30,
        "trail_arm_bp": 6.0,
        "trail_giveback_bp": 3.0,
        "trail_floor_bp": 5.0,
        "tp_execution_note": "TP1_70_RUNNER_TRAIL_ONLY",
        "profile_anchor": "v1435_tp1_70_runner_trail_only_tp1_floor",
    },
    "p__nf_e1_t120__trail_arm8_gb4_fl3_sl6": {
        "entry_bp": 1.0,
        "tp1_bp": 8.0,
        "full_tp_bp": 8.0,
        "sl_bp": 6.0,
        "be_bp": 0.0,
        "partial_exit_pct": 1.0,
        "ttl_s": 120,
        "hold_s": 30,
        "trail_arm_bp": 8.0,
        "trail_giveback_bp": 4.0,
        "trail_floor_bp": 3.0,
        "profile_anchor": "v1430_trail_arm8_gb4_fl3_sl6",
    },
}
V1430_RULES: dict[str, dict[str, Any]] = {
    "CNL-WPR-L|CNL-WPR-L:discount_mixed|LONG": {
        "action_id": "v1432_live_block_discount_mixed_long",
        "baseline_action": "pnlfirst__p__nf_e0_t180__trail_arm11_gb6_fl5_sl10",
        "block_all": True,
        "live_hotfix": "v1432_block_after_2x_live_max_hold_loss",
        "counts": {"BLOCK": 40},
        "summary": {"attempts": 40, "filled": 0, "fee_wr": None, "fee_adjusted_net": 0.0},
    },
    "CNL-WPR-L|CNL-WPR-L:falling_discount_trap|LONG": {
        "action_id": "loss_prune__vwaplem23p818283__keep_match",
        "baseline_action": "split__rsile37p129257__Lpnl__Rwr",
        "loss_condition": {"feature": "vwap", "op": "<=", "threshold": -23.818283},
        "keep_when": "match",
        "split": {
            "condition": {"feature": "rsi", "op": "<=", "threshold": 37.129257},
            "match_action": "g__nf_e0_t120__trail_arm11_gb5_fl4_sl8",
            "not_match_action": "block_all",
        },
        "counts": {"BLOCK": 42, "SL": 1, "TRAIL": 10},
        "summary": {"attempts": 53, "filled": 11, "fee_wr": 0.9090909090909091, "fee_adjusted_net": 0.61608026},
    },
    "CNL-WPR-L|CNL-WPR-L:deep_discount_stable|SHORT": {
        "action_id": "v1433_block_live_deep_discount_short",
        "baseline_action": "v1427_base_passthrough",
        "block_all": True,
        "live_hotfix": "v1433_block_after_live_short_sl_in_deep_discount_stable",
        "counts": {"BLOCK": 1},
        "summary": {"attempts": 1, "filled": 0, "fee_wr": None, "fee_adjusted_net": 0.0},
    },
    "STUP-S|STUP-S:mixed|LONG": {
        "action_id": "block_all",
        "baseline_action": "fixed_pnlfirst__p__nf_none__fixed_tp10_sl6_h20",
        "block_all": True,
        "counts": {"BLOCK": 3},
        "summary": {"attempts": 3, "filled": 0, "fee_wr": None, "fee_adjusted_net": 0.0},
    },
    "STUP-S|STUP-S:mixed|SHORT": {
        "action_id": "loss_prune__pullbackle14p572457__block_match",
        "baseline_action": "split__adv3le7p877217__Lpnl__Rwr",
        "loss_condition": {"feature": "pullback", "op": "<=", "threshold": 14.572457},
        "keep_when": "not_match",
        "split": {
            "condition": {"feature": "adv3", "op": "<=", "threshold": 7.877217},
            "match_action": "p__nf_e0_t120__trail_arm11_gb6_fl5_sl10",
            "not_match_action": "block_all",
        },
        "counts": {"BLOCK": 21, "NO_FILL": 1, "SL": 2, "TRAIL": 9},
        "summary": {"attempts": 33, "filled": 11, "fee_wr": 0.8181818181818182, "fee_adjusted_net": 0.58772948},
    },
    "STUP-S|STUP-S:weak_chop|SHORT": {
        "action_id": "v1433_live_block_weak_chop_short",
        "baseline_action": "split__adv3ge5p575973__Lwr__Rpnl",
        "block_all": True,
        "live_hotfix": "v1433_block_after_live_max_hold_loss_and_low_fill",
        "counts": {"BLOCK": 21},
        "summary": {"attempts": 21, "filled": 0, "fee_wr": None, "fee_adjusted_net": 0.0},
    },
    "W1D|W1D:mixed|LONG": {
        "action_id": "block_all",
        "baseline_action": "fixed_pnlfirst__p__nf_none__runner_tp6_q50_run11_fl5_sl8",
        "block_all": True,
        "counts": {"BLOCK": 4},
        "summary": {"attempts": 4, "filled": 0, "fee_wr": None, "fee_adjusted_net": 0.0},
    },
}
V1423_CONSERVATIVE_TREE: tuple[Any, Any, Any] = (
    ("state_eq", "CNL-WPR-L:deep_discount_stable"),
    (
        ("adv3", -2.5958),
        (
            ("slope60", -4.6678),
            "L_E2_TP4_SL4_T45",
            (
                ("rng15", 26.0143),
                (
                    ("slope120", 2.2255),
                    "S_E4_TP10_SL10_T45",
                    (("slope30", 2.2357), "L_E0_TP4_SL4_T60", "S_E4_TP20_SL8_T45"),
                ),
                (
                    ("rsi", 53.3715),
                    (("slope30", -0.2515), "L_E0_TP8_SL6_T45", "L_E0_TP15_SL8_T120"),
                    "S_E4_TP5_SL6_T45",
                ),
            ),
        ),
        (
            ("slope60", 1.8632),
            (
                ("slope120", -1.665),
                (("rsi", 46.2201), "L_E0_TP20_SL4_T45", "L_E4_TP20_SL6_T120"),
                "BASE",
            ),
            (("slope120", 2.2255), "L_E2_TP4_SL4_T60", "L_E4_TP20_SL6_T90"),
        ),
    ),
    (
        ("d30", 24.7563),
        (
            ("pullback", 16.2208),
            (
                ("adv3", -8.6025),
                "BASE",
                (
                    ("slope30", -1.3823),
                    (("slope60", 0.8231), "S_E2_TP20_SL4_T45", "L_E2_TP12_SL8_T45"),
                    (("vwap", -35.1889), "S_E0_TP20_SL4_T45", "L_E0_TP8_SL8_T120"),
                ),
            ),
            (
                ("adv3", 0.5118),
                (
                    ("d30", -54.4452),
                    (("range_pos", 0.2815), "L_E2_TP20_SL10_T90", "S_E4_TP20_SL6_T45"),
                    (("vwap", -21.4824), "L_E4_TP20_SL10_T60", "L_E0_TP12_SL10_T45"),
                ),
                (
                    ("vwap", -21.4824),
                    (("vwap", -35.1889), "L_E2_TP6_SL10_T90", "S_E4_TP4_SL4_T45"),
                    (("rng15", 59.7921), "BASE", "S_E0_TP20_SL6_T45"),
                ),
            ),
        ),
        (
            ("range_pos", 0.5566),
            (
                ("slope30", 3.5636),
                (("state_eq", "STUP-S:mixed"), "S_E0_TP20_SL4_T45", "S_E0_TP15_SL4_T45"),
                "BASE",
            ),
            (
                ("state_eq", "STUP-S:clean_extension"),
                (("rng15", 26.0143), "S_E0_TP20_SL4_T45", "L_E4_TP6_SL10_T90"),
                (("state_eq", "STUP-S:mixed"), "L_E4_TP20_SL4_T60", "L_E0_TP20_SL4_T45"),
            ),
        ),
    ),
)
V1423_ACTION_RE = re.compile(
    r"^(?P<side>[LS])_E(?P<entry>\d+(?:\.\d+)?)_TP(?P<tp>\d+(?:\.\d+)?)_SL(?P<sl>\d+(?:\.\d+)?)_T(?P<ttl>\d+)$"
)
V1427_ACTION_RE = re.compile(
    r"^(?P<side>[LS])_E(?P<entry>\d+(?:\.\d+)?)_TP(?P<tp>\d+(?:\.\d+)?)_SL(?P<sl>\d+(?:\.\d+)?)_T(?P<ttl>\d+)"
    r"(?:_LOCK(?P<lock_s>\d+)_(?P<lock_min>\d+(?:\.\d+)?)_(?P<lock_slope>-?\d+(?:\.\d+)?))?$"
)
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
STALE_UPMOVE_LOW_RNG_WEAK_ADV_LIVE_TAG = "v1417_stups_low_rng_weak_adv_cautious_live"
STALE_UPMOVE_LOW_RNG_MAX_BP = 30.0
STALE_UPMOVE_WEAK_ADV3_MAX_BP = 3.0
STALE_UPMOVE_LOW_RNG_WEAK_ADV_ENTRY_BP = 2.0
STALE_UPMOVE_HOT_CLEAN_ENTRY_TAG = "v1418_stups_clean_extension_hot_entry_band"
STALE_UPMOVE_HOT_CLEAN_ENTRY_BP = 6.0
STALE_UPMOVE_HOT_CLEAN_TTL_S = 75
STALE_UPMOVE_HOT_CLEAN_RSI_MIN = 62.0
STALE_UPMOVE_HOT_CLEAN_VWAP_MIN_BP = 8.0
STALE_UPMOVE_HOT_CLEAN_PULLBACK_MIN_BP = 30.0
STALE_UPMOVE_HOT_CLEAN_RANGE_POS_MIN = 0.90
STALE_UPMOVE_HOT_CLEAN_ADV3_MIN_BP = 10.0
STALE_UPMOVE_HOT_CLEAN_D30_MIN_BP = 30.0
STRONG_FALL_FOLLOW_LANE = "codex_v1_strong_fall_follow_short_canary"
STRONG_FALL_FOLLOW_LANE_CODE = "SFD-S"
STRONG_FALL_FOLLOW_POLICY_TAG = "v1414_strong_fall_follow_exec"
STRONG_FALL_FOLLOW_NOTIONAL_USDC = 50.0
STRONG_FALL_FOLLOW_PROFILE: dict[str, Any] = {
    "entry_bp": 2.0,
    "tp1_bp": 6.0,
    "full_tp_bp": 8.0,
    "sl_bp": 10.0,
    "be_bp": 2.0,
    "partial_exit_pct": 0.40,
    "ttl_s": 90,
    "profit_lock_mfe_bp": 6.0,
    "profit_lock_floor_bp": 6.0,
    "profit_lock_giveback_bp": 2.0,
    "small_n_forward_watch": True,
}
V143_PROFILE_SOURCE = "reports/v1420_profile_explorer_2026-06-28_29.md"
STUPS_V143_PROFILE_POLICY_TAG = "v1420_stups_fixed_regime_exec"
STUPS_V1420_CLEAN_GATE_BLOCK_REASON = "v1420_stups_clean_extension_gate_block"
STUPS_V1420_MIXED_BAD_BLOCK_REASON = "v1420_stups_mixed_bad_block"
STUPS_V1420_MIXED_WEAKZONE_BLOCK_REASON = "v1420_stups_mixed_weakzone_block"
STUPS_V1420_WEAK_CHOP_EXTREME_BLOCK_REASON = "v1420_stups_weak_chop_extreme_block"
STUPS_V143_PROFILES: dict[str, dict[str, Any]] = {
    "STUP-S:clean_extension": {
        "entry_bp": 2.0,
        "tp1_bp": 6.0,
        "full_tp_bp": 80.0,
        "sl_bp": 8.0,
        "be_bp": 2.0,
        "partial_exit_pct": 0.70,
        "ttl_s": 60,
        "replay_n": 2,
        "replay_wr": 1.0,
        "replay_net_usdc": 0.159,
        "adaptive_tp_engine": "v1420_stups_runner_after_clean_gate",
        "live_observation": "v1420_clean_gate_runner_cross_day",
        "no_fill_recovery_tp": 0,
        "no_fill_recovery_loss": 0,
        "no_fill_recovery_still_no_fill": 1,
    },
    "STUP-S:mixed": {
        "entry_bp": 2.0,
        "tp1_bp": 6.0,
        "full_tp_bp": 80.0,
        "sl_bp": 8.0,
        "be_bp": 2.0,
        "partial_exit_pct": 0.70,
        "ttl_s": 60,
        "replay_n": 7,
        "replay_wr": 1.0,
        "replay_net_usdc": 0.32756,
        "adaptive_tp_engine": "v1420_stups_runner_after_bad_weakzone_block",
        "live_observation": "v1420_mixed_bad_weakzone_block_runner",
        "no_fill_recovery_tp": 4,
        "no_fill_recovery_loss": 1,
        "no_fill_recovery_still_no_fill": 3,
    },
    "STUP-S:weak_chop": {
        "entry_bp": 0.0,
        "tp1_bp": 5.0,
        "full_tp_bp": 12.0,
        "sl_bp": 10.0,
        "be_bp": 4.0,
        "partial_exit_pct": 0.60,
        "ttl_s": 90,
        "replay_n": 7,
        "replay_wr": 1.0,
        "replay_net_usdc": 0.30000000,
        "adaptive_tp_engine": "v1416_stups_tp1_runner",
        "live_observation": "weak_chop_mfe5p5_missed_tp12",
        "no_fill_recovery_tp": 0,
        "no_fill_recovery_loss": 0,
        "no_fill_recovery_still_no_fill": 2,
    },
    "STUP-S:no_momentum_edge": {
        "entry_bp": 1.0,
        "tp1_bp": 8.0,
        "sl_bp": 15.0,
        "be_bp": 0.0,
        "partial_exit_pct": 1.0,
        "ttl_s": 90,
        "replay_n": 2,
        "replay_wr": 1.0,
        "replay_net_usdc": 0.08000000,
        "small_n_forward_watch": True,
        "no_fill_recovery_tp": 0,
        "no_fill_recovery_loss": 0,
        "no_fill_recovery_still_no_fill": 0,
    },
    "STUP-S:hot_continuation": {
        "entry_bp": 0.0,
        "tp1_bp": 4.0,
        "sl_bp": 4.0,
        "be_bp": 0.0,
        "partial_exit_pct": 0.40,
        "ttl_s": 45,
        "replay_n": 0,
        "replay_wr": None,
        "replay_net_usdc": 0.0,
        "shadow_only": True,
        "small_n_forward_watch": True,
        "no_fill_recovery_tp": 0,
        "no_fill_recovery_loss": 0,
        "no_fill_recovery_still_no_fill": 0,
    },
    "STUP-S:stale_squeeze_top": {
        "entry_bp": 0.0,
        "tp1_bp": 4.0,
        "sl_bp": 4.0,
        "be_bp": 0.0,
        "partial_exit_pct": 0.40,
        "ttl_s": 45,
        "replay_n": 2,
        "replay_wr": None,
        "replay_net_usdc": 0.0,
        "shadow_only": True,
        "no_fill_recovery_tp": 0,
        "no_fill_recovery_loss": 0,
        "no_fill_recovery_still_no_fill": 2,
    },
}
STUPS_V143_SHADOW_STATES = {
    "STUP-S:counter_recoil",
    "STUP-S:hot_continuation",
    "STUP-S:near_vwap_flat",
    "STUP-S:stale_squeeze_top",
    "STUP-S:missing_features",
}
S1P_L_V149_PROFILE_POLICY_TAG = "v149_s1pl_tiny_profile_fix"
S1P_L_V149_NOTIONAL_USDC = 25.0
S1P_L_V149_MARKET_STATE = "S1P-L:ordinary_pullback_pre_vwap"
S1P_L_V149_PROFILE_SOURCE = "reports/v141_aggtick_optimization_2026-06-27.md"
S1P_L_V149_PROFILE: dict[str, Any] = {
    "entry_bp": 0.0,
    "tp1_bp": 6.0,
    "partial_exit_pct": 1.0,
    "sl_bp": 15.0,
    "be_bp": 0.0,
    "ttl_s": 180,
    "replay_n": 3,
    "replay_wr": 1.0,
    "replay_net_usdc": 0.072,
    "small_n_forward_watch": True,
}
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
    "slope30": ("slope30", "slope30_bp", "price_slope30_bp", "slope_30_bp"),
    "slope60": ("slope60", "slope60_bp", "price_slope60_bp", "slope_60_bp"),
    "slope120": ("slope120", "slope120_bp", "price_slope120_bp", "slope_120_bp"),
    "close_pos": ("close_pos", "close_pos_bar"),
    "reprice_favorable_bp": ("reprice_favorable_bp", "favorable_bp"),
    "reprice_adverse_bp": ("reprice_adverse_bp", "adverse_bp"),
    "reprice_wait_elapsed_seconds": (
        "reprice_wait_elapsed_seconds",
        "setup_age_sec",
        "reprice_wait_s",
    ),
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
class CodexV1LaneMatch:
    """A predicate-only base-lane match with no paid-routing authority."""

    lane: str
    lane_code: str
    side: str
    strategy: str
    regime: str | None = None
    annotations: tuple[str, ...] = ()


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

    strong_fall_follow = build_strong_fall_follow_short_decision(
        features,
        strategy=strategy,
        side=side,
    )
    if strong_fall_follow is not None:
        return strong_fall_follow

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


def match_all_codex_v1_lanes(
    features: Mapping[str, Any],
    *,
    lanes: Sequence[LaneSpec] | None = None,
) -> tuple[CodexV1LaneMatch, ...]:
    """Return every independently discoverable lane without paid authority.

    The supplied registry controls output order only.  Each lane predicate is
    evaluated independently, so reordering the registry cannot change the
    returned match set.  Selector-local positive STUP-S and SFD-S builders are
    also evaluated, but stale/mid-extension vetoes alone are never lane matches.

    CNL-WPR-L is intentionally not inferred here: its current positive routing
    is stateful in ``one_run`` and cannot be established from this feature
    mapping alone without guessing.
    """

    symbol = _string_feature(features, "symbol")
    if symbol and symbol not in SUPPORTED_SYMBOLS:
        return ()

    strategy = _string_feature(features, "strategy")
    if strategy is None:
        return ()

    registry = LANES if lanes is None else tuple(lanes)
    matches: list[CodexV1LaneMatch] = []
    seen: set[tuple[str, str]] = set()
    for lane in registry:
        matched, _missing = _lane_matches(lane, features)
        if not matched:
            continue

        identity = (lane.lane_code, lane.side)
        if identity in seen:
            continue
        seen.add(identity)

        regime: str | None = None
        annotations = [f"deny_rule_passed:{rule.name}" for rule in lane.deny_rules]
        if lane.name == "codex_v1_hot_up_extension_pullback_long":
            regime = "hot_up_extension"
            annotations.append("paid_selector_special_branch:hot_up_extension")
        elif lane.name == "codex_v1_s1_bbrsi_ordinary_pullback_long_pre_vwap":
            regime = "S1P-L:ordinary_pullback_pre_vwap"
            annotations.append("paid_selector_special_branch:s1p_l")

        matches.append(
            CodexV1LaneMatch(
                lane=lane.name,
                lane_code=lane.lane_code,
                side=lane.side,
                strategy=strategy,
                regime=regime,
                annotations=tuple(annotations),
            )
        )

    def append_special(
        decision: CodexV1Decision | None,
        *,
        builder_name: str,
    ) -> None:
        if (
            decision is None
            or not decision.lane
            or not decision.lane_code
            or not decision.side
            or not decision.strategy
        ):
            return
        identity = (decision.lane_code, decision.side)
        if identity in seen:
            return
        seen.add(identity)
        matches.append(
            CodexV1LaneMatch(
                lane=decision.lane,
                lane_code=decision.lane_code,
                side=decision.side,
                strategy=decision.strategy,
                regime=decision.regime,
                annotations=(
                    f"positive_special_builder:{builder_name}",
                    f"special_builder_outcome:{'accepted' if decision.accepted else 'shadow'}",
                ),
            )
        )

    # The stale predicate is only a branch guard.  It becomes a discovery match
    # only when the pure positive builder assigns the STUP-S family.
    if is_stale_short_after_upmove(features):
        append_special(
            build_stale_upmove_canary_decision(
                features,
                strategy=strategy,
                side=_string_feature(features, "side"),
            ),
            builder_name="build_stale_upmove_canary_decision",
        )

    append_special(
        build_strong_fall_follow_short_decision(
            features,
            strategy=strategy,
            side=_string_feature(features, "side"),
        ),
        builder_name="build_strong_fall_follow_short_decision",
    )
    return tuple(matches)


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


def _v1421_feature_value(features: Mapping[str, Any], feature: str) -> float | None:
    if feature == "vwap":
        return _feature_value(features, "vwap_dist_bp")
    if feature == "range_pos":
        return _feature_value(features, "range_pos_15")
    if feature == "pullback":
        return _feature_value(features, "pullback_from_recent_high_bp")
    return _feature_value(features, feature)


def _v1421_market_state(features: Mapping[str, Any], decision: CodexV1Decision) -> str:
    metrics = decision.metrics if isinstance(decision.metrics, Mapping) else {}
    state = (
        metrics.get("market_state")
        or metrics.get("v143_market_state")
        or metrics.get("wpr_profile")
        or decision.regime
    )
    if state:
        return str(state)
    lane_code = str(decision.lane_code or "").upper()
    if lane_code == STALE_UPMOVE_CANARY_LANE_CODE:
        return _stups_v143_market_state(features)
    return lane_code or "UNKNOWN"


def _v1421_feature_row(features: Mapping[str, Any], decision: CodexV1Decision) -> dict[str, Any]:
    row = {key: _v1421_feature_value(features, key) for key in V1421_REQUIRED_FEATURES}
    row["state"] = _v1421_market_state(features, decision)
    return row


def _v1421_le(row: Mapping[str, Any], key: str, threshold: float) -> bool:
    value = row.get(key)
    return value is not None and float(value) <= threshold


def _v1421_predict_action(row: Mapping[str, Any]) -> str:
    state = str(row.get("state") or "")
    if _v1421_le(row, "slope60", 3.1212):
        if _v1421_le(row, "slope30", -4.0823):
            if _v1421_le(row, "vwap", -13.2579):
                if _v1421_le(row, "d30", 14.8377):
                    if _v1421_le(row, "pullback", 9.4382):
                        return "SHORT_RUNNER_E2"
                    if _v1421_le(row, "adv3", 10.7038):
                        return "SHORT_FAST30_E2"
                    return "LONG_FALL_TP8_SL6"
                return "LONG_DEEP_TP4_SL6"
            if _v1421_le(row, "range_pos", 0.5605):
                if _v1421_le(row, "slope120", -0.8691):
                    if _v1421_le(row, "slope120", -10.5179):
                        return "LONG_OVERSOLD_E8_TP8"
                    return "SHORT_FAST30_E2"
                return "BASE"
            if state == "STUP-S:mixed":
                return "BASE"
            return "LONG_DEEP_TP4_SL6"
        if _v1421_le(row, "range_pos", 0.6489):
            if _v1421_le(row, "rng15", 29.9224):
                if _v1421_le(row, "slope120", -3.6124):
                    return "BASE"
                if _v1421_le(row, "slope30", 2.2292):
                    return "SHORT_RUNNER_E6"
                return "BASE"
            if _v1421_le(row, "range_pos", 0.4924):
                return "BASE"
            if _v1421_le(row, "slope120", 2.3419):
                return "SHORT_FAST30_E2"
            return "SHORT_WIDE_E8"
        if state == "STUP-S:mixed":
            return "SHORT_RUNNER_E2"
        if _v1421_le(row, "d30", -28.3865):
            return "SHORT_FAST30_E2"
        if _v1421_le(row, "rng15", 26.6487):
            return "SHORT_WIDE_E8"
        return "BASE"

    if _v1421_le(row, "rng15", 33.5211):
        if _v1421_le(row, "range_pos", 0.5605):
            if state == "STUP-S:mixed":
                return "SHORT_RUNNER_E6"
            if _v1421_le(row, "slope120", 4.1008):
                return "SHORT_RUNNER_E6"
            return "BASE"
        if _v1421_le(row, "pullback", 24.1866):
            if state == "CNL-WPR-L:deep_discount_stable":
                if _v1421_le(row, "vwap", -36.4502):
                    return "LONG_DEEP_TP4_SL6"
                return "SHORT_RUNNER_E2"
            if _v1421_le(row, "slope30", -1.3714):
                return "BASE"
            return "SHORT_RUNNER_E2"
        if _v1421_le(row, "slope30", -1.3714):
            return "LONG_DEEP_TP4_SL6"
        return "SHORT_WIDE_E8"

    if state == "STUP-S:weak_chop":
        if _v1421_le(row, "range_pos", 0.5605):
            return "BASE"
        return "SHORT_RUNNER_E6"
    if state == "CNL-WPR-L:deep_discount_stable":
        return "BASE"
    if _v1421_le(row, "d30", -28.3865):
        if _v1421_le(row, "slope30", 2.2292):
            return "BASE"
        return "LONG_FALL_TP8_SL6"
    if _v1421_le(row, "rsi", 49.1795):
        return "BASE"
    return "SHORT_WIDE_E8"


def apply_v1421_adaptive_decision(
    features: Mapping[str, Any],
    decision: CodexV1Decision,
) -> CodexV1Decision:
    if not decision.accepted:
        return decision
    lane_code = str(decision.lane_code or "").upper()
    lane = str(decision.lane or "")
    if lane_code not in V1421_LIVE_LANE_CODES:
        return decision
    if lane_code == "W6A" or lane == "w6_lane_s1long_rng38_86_range9_15_e0":
        return decision
    metrics = decision.metrics if isinstance(decision.metrics, Mapping) else {}
    if metrics.get("v1421_action"):
        return decision

    row = _v1421_feature_row(features, decision)
    missing = tuple(key for key in V1421_REQUIRED_FEATURES if row.get(key) is None)
    if missing:
        return decision

    action = _v1421_predict_action(row)
    base_metrics = dict(metrics)
    previous_policy_tag = decision.policy_tag or base_metrics.get("policy_tag") or base_metrics.get("policy_note")
    feature_snapshot = {
        key: round(float(row[key]), 4)
        for key in V1421_REQUIRED_FEATURES
        if row.get(key) is not None
    }
    base_metrics.update(
        {
            "v1421_action": action,
            "v1421_state": row.get("state"),
            "v1421_policy_tag": V1421_POLICY_TAG,
            "v1421_policy_source": V1421_DECISION_TREE_SOURCE,
            "v1421_previous_policy_tag": previous_policy_tag,
            "v1421_previous_side": decision.side,
            "v1421_features": feature_snapshot,
            "v1421_slope_source": features.get("v1421_slope_source") or features.get("slope_source"),
        }
    )
    if action == "BASE":
        return replace(decision, metrics=base_metrics)
    if action == "BLOCK":
        return replace(
            decision,
            accepted=False,
            reason="v1421_decision_tree_block",
            size_mult=0.0,
            notional_mult=0.0,
            requested_notional_usdc=0.0,
            risk_tags=tuple(dict.fromkeys((*decision.risk_tags, "v1421_decision_tree_block"))),
            metrics={**base_metrics, "policy_note": "v1421_decision_tree_block", "policy_tag": "v1421_decision_tree_block"},
            policy_tag="v1421_decision_tree_block",
            shadow_lane="SH_V1421_TREE_BLOCK",
        )

    profile = V1421_ACTION_PROFILES.get(action)
    if profile is None:
        return replace(decision, metrics=base_metrics)

    target_side = str(profile["side"]).upper()
    action_tag = f"v1421_action_{action.lower()}"
    side_override_tag = f"v1421_side_override_{target_side.lower()}" if target_side != str(decision.side or "").upper() else None
    profile_metrics = {
        **base_metrics,
        "policy_note": V1421_POLICY_TAG,
        "policy_tag": V1421_POLICY_TAG,
        "live_action": "live_decision_tree_adaptive_profile",
        "source": "v1421_decision_tree_three_window_search",
        "profile_source": V1421_DECISION_TREE_SOURCE,
        "market_state": row.get("state"),
        "v143_market_state": row.get("state"),
        "adaptive_tp_engine": "v1421_decision_tree_adaptive_runner",
        "entry_bp": float(profile["entry_bp"]),
        "tp1_bp": float(profile["tp1_bp"]),
        "full_tp_bp": float(profile["full_tp_bp"]),
        "sl_bp": float(profile["sl_bp"]),
        "be_bp": float(profile["be_bp"]),
        "partial_exit_pct": float(profile["partial_exit_pct"]),
        "ttl_s": int(profile["ttl_s"]),
        "hold_s": int(profile.get("hold_s", 0) or 0) or None,
        "profile_anchor": profile.get("profile_anchor"),
        "profile_patch": V1421_POLICY_TAG,
        "target_side": target_side,
        "applied_notional_cap_usdc": decision.requested_notional_usdc,
    }
    risk_tags = tuple(
        dict.fromkeys(
            tag
            for tag in (
                *decision.risk_tags,
                "v1421_decision_tree_adaptive",
                action_tag,
                side_override_tag,
                f"entry{float(profile['entry_bp']):g}",
                f"tp{float(profile['tp1_bp']):g}",
                f"fulltp{float(profile['full_tp_bp']):g}",
                f"sl{float(profile['sl_bp']):g}",
                f"be{float(profile['be_bp']):g}",
                f"ttl{int(profile['ttl_s'])}s",
            )
            if tag
        )
    )
    return replace(
        decision,
        side=target_side,
        entry_offset_bp=float(profile["entry_bp"]),
        reason=V1421_POLICY_TAG,
        regime=str(row.get("state") or decision.regime or ""),
        risk_tags=risk_tags,
        metrics=profile_metrics,
        policy_tag=V1421_POLICY_TAG,
    )

def _v1423_split_left(row: Mapping[str, Any], split: Sequence[Any]) -> bool:
    key = str(split[0])
    threshold = split[1]
    if key == "state_eq":
        return str(row.get("state") or "") == str(threshold)
    value = row.get(key)
    if value is None:
        return False
    try:
        return float(value) <= float(threshold)
    except (TypeError, ValueError):
        return False


def _v1423_predict_action(row: Mapping[str, Any], node: Any = V1423_CONSERVATIVE_TREE) -> str:
    current = node
    while not isinstance(current, str):
        split, left, right = current
        current = left if _v1423_split_left(row, split) else right
    return current


def _v1423_profile_from_action(action: str) -> dict[str, Any] | None:
    match = V1423_ACTION_RE.match(action)
    if not match:
        return None
    side = "LONG" if match.group("side") == "L" else "SHORT"
    entry_bp = float(match.group("entry"))
    tp_bp = min(float(match.group("tp")), V1423_MAX_TP_BP)
    sl_bp = float(match.group("sl"))
    ttl_s = int(match.group("ttl"))
    return {
        "side": side,
        "entry_bp": entry_bp,
        "tp1_bp": tp_bp,
        "full_tp_bp": tp_bp,
        "sl_bp": sl_bp,
        "be_bp": 0.0,
        "partial_exit_pct": 1.0,
        "ttl_s": ttl_s,
        "profile_anchor": f"v1423_{action.lower()}",
    }


def _v1427_predict_action(row: Mapping[str, Any], node: Mapping[str, Any] = V1427_FIVE_WINDOW_TREE) -> str:
    current: Any = node
    while isinstance(current, Mapping) and "action" not in current:
        split = current.get("split")
        if not isinstance(split, Sequence):
            return "BASE"
        current = current["left"] if _v1423_split_left(row, split) else current["right"]
    if isinstance(current, Mapping):
        return str(current.get("action") or "BASE")
    return str(current or "BASE")


def _v1427_profile_from_action(action: str) -> dict[str, Any] | None:
    match = V1427_ACTION_RE.match(action)
    if not match:
        return None
    side = "LONG" if match.group("side") == "L" else "SHORT"
    entry_bp = float(match.group("entry"))
    tp_bp = min(float(match.group("tp")), V1427_MAX_TP_BP)
    sl_bp = float(match.group("sl"))
    ttl_s = int(match.group("ttl"))
    profile: dict[str, Any] = {
        "side": side,
        "entry_bp": entry_bp,
        "tp1_bp": tp_bp,
        "full_tp_bp": tp_bp,
        "sl_bp": sl_bp,
        "be_bp": 0.0,
        "partial_exit_pct": 1.0,
        "ttl_s": ttl_s,
        "hold_s": 900,
        "profile_anchor": f"v1427_{action.lower()}",
    }
    if match.group("lock_s") is not None:
        lock_s = int(match.group("lock_s"))
        lock_min = float(match.group("lock_min"))
        lock_slope = float(match.group("lock_slope"))
        profile.update(
            {
                "time_profit_lock_enabled": True,
                "time_lock_enabled": True,
                "time_lock_s": lock_s,
                "time_lock_min_bp": lock_min,
                "time_lock_slope_max_bp": lock_slope,
                "time_lock_lookback_s": 30,
                "time_lock_reason": "CODEX_V1427_TIME_LOCK",
            }
        )
    return profile


def _v1427_projected_50_net_by_window() -> dict[str, float]:
    return {
        window: round(float(row["projected_50_net"]), 6)
        for window, row in V1427_FDT_RNG90_BLOCK_BY_WINDOW.items()
    }


def _v1427_feature_snapshot(row: Mapping[str, Any]) -> dict[str, float]:
    return {
        key: round(float(row[key]), 4)
        for key in V1427_REQUIRED_FEATURES
        if row.get(key) is not None
    }


def _v1427_base_metrics(
    features: Mapping[str, Any],
    decision: CodexV1Decision,
    row: Mapping[str, Any],
    action: str,
) -> dict[str, Any]:
    metrics = decision.metrics if isinstance(decision.metrics, Mapping) else {}
    previous_policy_tag = decision.policy_tag or metrics.get("policy_tag") or metrics.get("policy_note")
    return {
        **metrics,
        "v1427_action": action,
        "v1427_state": row.get("state"),
        "v1427_policy_tag": V1427_POLICY_TAG,
        "v1427_policy_source": V1427_DECISION_TREE_SOURCE,
        "v1427_overlay_source": V1427_OVERLAY_SOURCE,
        "v1427_previous_policy_tag": previous_policy_tag,
        "v1427_previous_side": decision.side,
        "v1427_features": _v1427_feature_snapshot(row),
        "v1427_slope_source": features.get("v1423_slope_source") or features.get("v1421_slope_source") or features.get("slope_source"),
        "v1427_target_50_net_usdc": V1427_TARGET_50_NET_USDC,
        "v1427_target_wr": V1427_TARGET_WR,
        "v1427_max_tp_bp": V1427_MAX_TP_BP,
        "v1427_min_projected_50_net_usdc": round(V1427_MIN_PROJECTED_50_NET_USDC, 6),
        "v1427_projected_50_net_by_window": _v1427_projected_50_net_by_window(),
    }


def _v1427_block_decision(
    decision: CodexV1Decision,
    *,
    reason: str,
    metrics: Mapping[str, Any],
    shadow_lane: str,
    extra_tags: Sequence[str] = (),
) -> CodexV1Decision:
    blocked_metrics = {
        **dict(metrics),
        "policy_note": reason,
        "policy_tag": reason,
        "live_action": "blocked_no_submit",
    }
    return replace(
        decision,
        accepted=False,
        reason=reason,
        size_mult=0.0,
        notional_mult=0.0,
        requested_notional_usdc=0.0,
        risk_tags=tuple(dict.fromkeys((*decision.risk_tags, reason, *extra_tags))),
        metrics=blocked_metrics,
        policy_tag=reason,
        shadow_lane=shadow_lane,
    )


def _v1429_stale_side_override_details(
    features: Mapping[str, Any],
    *,
    lane_code: str,
    state: str,
    target_side: str,
    previous_side: str | None,
) -> dict[str, Any] | None:
    if lane_code != "STUP-S" or state not in V1429_STUPS_STALE_SIDE_OVERRIDE_STATES:
        return None
    raw_side = str(previous_side or features.get("side") or "").upper()
    target = str(target_side or "").upper()
    if target not in {"LONG", "SHORT"} or raw_side not in {"LONG", "SHORT"}:
        return None
    if target == raw_side:
        return None
    wait_s = _feature_value(features, "reprice_wait_elapsed_seconds")
    if wait_s is None or wait_s < V1429_STUPS_STALE_SIDE_OVERRIDE_WAIT_S:
        return None
    return {
        "v1429_block_reason": V1429_STUPS_STALE_SIDE_OVERRIDE_BLOCK_TAG,
        "v1429_stale_side_override_blocked": True,
        "v1429_previous_side": raw_side,
        "v1429_target_side": target,
        "v1429_wait_s": round(float(wait_s), 4),
        "v1429_wait_threshold_s": V1429_STUPS_STALE_SIDE_OVERRIDE_WAIT_S,
        "v1429_state": state,
    }

def _v1433_stups_clean_high_override_details(
    features: Mapping[str, Any],
    *,
    lane_code: str,
    state: str,
    target_side: str,
    previous_side: str | None,
) -> dict[str, Any] | None:
    if lane_code != "STUP-S" or state != "STUP-S:clean_extension":
        return None
    raw_side = str(previous_side or features.get("side") or "").upper()
    target = str(target_side or "").upper()
    if raw_side != "SHORT" or target != "LONG":
        return None
    range_pos = _feature_value(features, "range_pos_15")
    if range_pos is None:
        range_pos = _feature_value(features, "range_pos")
    vwap_dist_bp = _feature_value(features, "vwap_dist_bp")

    if range_pos is None or vwap_dist_bp is None:
        return None
    if (
        float(range_pos) < V1433_STUPS_CLEAN_HIGH_OVERRIDE_RANGE_POS_MIN
        or float(vwap_dist_bp) < V1433_STUPS_CLEAN_HIGH_OVERRIDE_VWAP_MIN_BP
    ):
        return None
    return {
        "v1433_block_reason": V1433_STUPS_CLEAN_HIGH_OVERRIDE_BLOCK_TAG,
        "v1433_clean_high_override_blocked": True,
        "v1433_previous_side": raw_side,
        "v1433_target_side": target,
        "v1433_range_pos_15": round(float(range_pos), 4),
        "v1433_range_pos_min": V1433_STUPS_CLEAN_HIGH_OVERRIDE_RANGE_POS_MIN,
        "v1433_vwap_dist_bp": round(float(vwap_dist_bp), 4),
        "v1433_vwap_min_bp": V1433_STUPS_CLEAN_HIGH_OVERRIDE_VWAP_MIN_BP,
        "v1433_state": state,
    }


def _v1452_stups_late_adverse_reopen_details(
    features: Mapping[str, Any],
    *,
    lane_code: str,
    state: str,
    target_side: str,
    previous_side: str | None,
    legacy_reason: str,
) -> dict[str, Any] | None:
    if lane_code != "STUP-S" or state != "STUP-S:clean_extension":
        return None
    target = str(target_side or "").upper()
    raw_side = str(previous_side or features.get("side") or "").upper()
    if target not in {"LONG", "SHORT"} or raw_side not in {"LONG", "SHORT"}:
        return None
    wait_s = _feature_value(features, "reprice_wait_elapsed_seconds")
    adverse_bp = _feature_value(features, "reprice_adverse_bp")
    favorable_bp = _feature_value(features, "reprice_favorable_bp")
    if wait_s is None or adverse_bp is None or favorable_bp is None:
        return None
    if float(wait_s) <= 180.0 or float(adverse_bp) < 5.0 or float(favorable_bp) > 1.0:
        return None
    return {
        "v1452_block_reason": V1452_STUPS_LATE_ADVERSE_REOPEN_BLOCK_TAG,
        "v1452_late_adverse_reopen_blocked": True,
        "v1452_previous_side": raw_side,
        "v1452_target_side": target,
        "v1452_legacy_reason": legacy_reason,
        "v1452_state": state,
        "v1452_wait_s": round(float(wait_s), 4),
        "v1452_wait_threshold_s": 180.0,
        "v1452_adverse_bp": round(float(adverse_bp), 4),
        "v1452_adverse_threshold_bp": 5.0,
        "v1452_favorable_bp": round(float(favorable_bp), 4),
        "v1452_favorable_max_bp": 1.0,
    }



def _v1453_stups_clean_extension_reopen_review_details(
    features: Mapping[str, Any],
    *,
    lane_code: str,
    state: str,
    target_side: str,
    previous_side: str | None,
    legacy_reason: str,
    action: str,
) -> dict[str, Any] | None:
    if lane_code != "STUP-S" or state != "STUP-S:clean_extension":
        return None
    target = str(target_side or "").upper()
    raw_side = str(previous_side or features.get("side") or "").upper()
    if target != "SHORT" or raw_side not in {"LONG", "SHORT"}:
        return None
    rng15 = _feature_value(features, "rng15")
    rsi = _feature_value(features, "rsi")
    vwap_dist_bp = _feature_value(features, "vwap_dist_bp")
    wait_s = _feature_value(features, "reprice_wait_elapsed_seconds")

    high_rng_shadow_block = rng15 is not None and float(rng15) >= 28.91
    high_rsi_vwap_late_review = (
        rsi is not None
        and vwap_dist_bp is not None
        and wait_s is not None
        and float(rsi) >= 65.0
        and float(vwap_dist_bp) >= 20.0
        and float(wait_s) > 180.0
    )
    if high_rng_shadow_block:
        block_shape = "rng15_shadow_block_candidate"
    elif high_rsi_vwap_late_review:
        block_shape = "high_rsi_vwap_late_review"
    else:
        return None

    return {
        "v1453_block_reason": block_shape,
        "v1453_clean_extension_reopen_review_blocked": True,
        "v1453_previous_side": raw_side,
        "v1453_target_side": target,
        "v1453_legacy_reason": legacy_reason,
        "v1453_state": state,
        "v1453_action": action,
        "v1453_rng15": round(float(rng15), 4) if rng15 is not None else None,
        "v1453_rng15_shadow_block_min": 28.91,
        "v1453_rsi": round(float(rsi), 4) if rsi is not None else None,
        "v1453_rsi_min": 65.0,
        "v1453_vwap_dist_bp": round(float(vwap_dist_bp), 4) if vwap_dist_bp is not None else None,
        "v1453_vwap_dist_min_bp": 20.0,
        "v1453_wait_s": round(float(wait_s), 4) if wait_s is not None else None,
        "v1453_wait_min_s": 180.0,
    }

def _v1455_action_tp_bp(action: str | None) -> float | None:
    if not action:
        return None
    profile = _v1427_profile_from_action(str(action))
    if profile is None:
        profile = _v1423_profile_from_action(str(action))
    if profile is None:
        return None
    try:
        value = float(profile.get("tp1_bp") or profile.get("full_tp_bp") or 0.0)
    except (TypeError, ValueError):
        return None
    return value if isfinite(value) and value > 0 else None


def _v1455_route_for_action(
    row: Mapping[str, Any],
    decision: CodexV1Decision,
    action: str | None,
) -> tuple[str, str, float | None]:
    lane_code = str(decision.lane_code or "").upper()
    state = str(row.get("state") or decision.regime or "")
    tp_bp = _v1455_action_tp_bp(action)
    if lane_code == "STUP-S" and state == "STUP-S:clean_extension":
        if tp_bp is not None and tp_bp >= V1455_BLOCKED_TP_MIN_BP:
            return "BLOCK", "stups_clean_extension_tp14_loss_guard", tp_bp
        if tp_bp is not None and tp_bp <= V1455_THIN_SCALP_ROUTE_TP_MAX_BP:
            return "THIN_SCALP", "stups_clean_extension_tp8_tp10_gate_pass", tp_bp
        return "NORMAL", "stups_clean_extension_non_tp14_profile", tp_bp
    if lane_code == "CNL-WPR-L":
        return "RECOVERY_CANARY", "codex_recovery_lane_whitelist_canary", tp_bp
    if not decision.accepted:
        return "OBSERVE_ONLY", "not_live_accepted", tp_bp
    return "NORMAL", "default_live_profile", tp_bp


def _v1455_route_metrics(route: str, route_reason: str, action: str | None, tp_bp: float | None) -> dict[str, Any]:
    return {
        "v1455_route": route,
        "v1455_route_reason": route_reason,
        "v1455_action": action,
        "v1455_action_tp_bp": round(float(tp_bp), 4) if tp_bp is not None else None,
        "v1455_policy_tag": V1455_ADAPTIVE_ROUTE_TAG,
    }

def apply_v1427_five_window_decision(
    features: Mapping[str, Any],
    decision: CodexV1Decision,
) -> CodexV1Decision:
    lane_code = str(decision.lane_code or "").upper()
    lane = str(decision.lane or "")
    row = _v1421_feature_row(features, decision)

    metrics = decision.metrics if isinstance(decision.metrics, Mapping) else {}
    if not decision.accepted:
        legacy_reason = str(decision.reason or "")
        missing = tuple(key for key in V1427_REQUIRED_FEATURES if row.get(key) is None)
        action = _v1427_predict_action(row) if not missing else "BLOCK"
        profile = _v1427_profile_from_action(action)
        if (
            lane_code != "STUP-S"
            or legacy_reason not in V1428_LEGACY_STUPS_REOPEN_REASONS
            or action in {"BASE", "BLOCK"}
            or profile is None
        ):
            return decision
        target_side = str(profile["side"]).upper()
        state = str(row.get("state") or "")
        stale_override_details = _v1429_stale_side_override_details(
            features,
            lane_code=lane_code,
            state=state,
            target_side=target_side,
            previous_side=decision.side,
        )
        if stale_override_details:
            block_metrics = _v1427_base_metrics(features, decision, row, action)
            block_metrics.update(stale_override_details)
            return _v1427_block_decision(
                decision,
                reason=V1429_STUPS_STALE_SIDE_OVERRIDE_BLOCK_TAG,
                metrics=block_metrics,
                shadow_lane="SH_V1429_STUPS_STALE_SIDE_OVERRIDE",
                extra_tags=("v1429_stale_side_override_block", f"v1429_blocked_{action.lower()}"),
            )
        clean_high_details = _v1433_stups_clean_high_override_details(
            features,
            lane_code=lane_code,
            state=state,
            target_side=target_side,
            previous_side=decision.side,
        )
        if clean_high_details:
            block_metrics = _v1427_base_metrics(features, decision, row, action)
            block_metrics.update(clean_high_details)
            return _v1427_block_decision(
                decision,
                reason=V1433_STUPS_CLEAN_HIGH_OVERRIDE_BLOCK_TAG,
                metrics=block_metrics,
                shadow_lane="SH_V1433_STUPS_CLEAN_HIGH_OVERRIDE",
                extra_tags=("v1433_clean_high_override_block", f"v1433_blocked_{action.lower()}"),
            )
        late_adverse_details = _v1452_stups_late_adverse_reopen_details(
            features,
            lane_code=lane_code,
            state=state,
            target_side=target_side,
            previous_side=decision.side,
            legacy_reason=legacy_reason,
        )
        if late_adverse_details:
            block_metrics = _v1427_base_metrics(features, decision, row, action)
            block_metrics.update(late_adverse_details)
            return _v1427_block_decision(
                decision,
                reason=V1452_STUPS_LATE_ADVERSE_REOPEN_BLOCK_TAG,
                metrics=block_metrics,
                shadow_lane="SH_V1452_STUPS_LATE_ADVERSE_REOPEN",
                extra_tags=("v1452_stups_late_adverse_reopen_block", f"v1452_blocked_{action.lower()}"),
            )
        reopen_review_details = _v1453_stups_clean_extension_reopen_review_details(
            features,
            lane_code=lane_code,
            state=state,
            target_side=target_side,
            previous_side=decision.side,
            legacy_reason=legacy_reason,
            action=action,
        )
        if reopen_review_details:
            block_metrics = _v1427_base_metrics(features, decision, row, action)
            block_metrics.update(reopen_review_details)
            return _v1427_block_decision(
                decision,
                reason=V1453_STUPS_CLEAN_EXTENSION_REOPEN_REVIEW_BLOCK_TAG,
                metrics=block_metrics,
                shadow_lane="SH_V1453_STUPS_CLEAN_EXTENSION_REOPEN_REVIEW",
                extra_tags=("v1453_stups_clean_extension_reopen_review_block", f"v1453_blocked_{action.lower()}"),
            )
        v1455_route, v1455_route_reason, v1455_tp_bp = _v1455_route_for_action(row, decision, action)
        if v1455_route == "BLOCK":
            block_metrics = _v1427_base_metrics(features, decision, row, action)
            block_metrics.update(_v1455_route_metrics(v1455_route, v1455_route_reason, action, v1455_tp_bp))
            return _v1427_block_decision(
                decision,
                reason=V1455_STUPS_CLEAN_EXTENSION_TP14_BLOCK_TAG,
                metrics=block_metrics,
                shadow_lane="SH_V1455_STUPS_CLEAN_EXTENSION_TP14_BLOCK",
                extra_tags=("v1455_stups_clean_extension_tp14_block", f"v1455_blocked_{action.lower()}"),
            )
        reopened_metrics = {
            **dict(metrics),
            "v1428_legacy_reopen": True,
            "v1428_legacy_reopen_reason": legacy_reason,
            "v1428_legacy_reopen_action": action,
        }
        decision = replace(
            decision,
            accepted=True,
            reason=f"{V1427_POLICY_TAG}_legacy_reopen",
            size_mult=max(float(decision.size_mult or 0.0), 1.0),
            notional_mult=max(float(decision.notional_mult or 0.0), 1.0),
            requested_notional_usdc=max(float(decision.requested_notional_usdc or 0.0), BASE_NOTIONAL_USDC),
            risk_tags=tuple(
                dict.fromkeys(
                    (
                        *decision.risk_tags,
                        V1428_LEGACY_STUPS_REOPEN_TAG,
                        f"v1428_reopened_{legacy_reason}",
                    )
                )
            ),
            metrics=reopened_metrics,
            policy_tag=V1427_POLICY_TAG,
        )
        metrics = reopened_metrics

    if lane_code == "W1D":
        metrics = _v1427_base_metrics(features, decision, row, "BLOCK")
        metrics.update({"v1427_block_reason": V1427_W1D_BLOCK_TAG, "v1427_w1d_blocked": True})
        return _v1427_block_decision(
            decision,
            reason=V1427_W1D_BLOCK_TAG,
            metrics=metrics,
            shadow_lane="SH_V1427_W1D_BLOCK",
            extra_tags=("v1427_unvalidated_lane_block",),
        )

    if lane_code not in V1427_LIVE_LANE_CODES:
        return decision
    if lane_code == "W6A" or lane == "w6_lane_s1long_rng38_86_range9_15_e0":
        return decision
    if metrics.get("v1427_action"):
        return decision

    missing = tuple(key for key in V1427_REQUIRED_FEATURES if row.get(key) is None)
    if missing:
        missing_metrics = dict(metrics)
        missing_metrics.update(
            {
                "v1427_action": "BLOCK",
                "v1427_state": row.get("state"),
                "v1427_missing_features": missing,
                "v1427_policy_tag": V1427_POLICY_TAG,
                "v1427_policy_source": V1427_DECISION_TREE_SOURCE,
            }
        )
        wpr_state = str(
            metrics.get("wpr_profile")
            or metrics.get("market_state")
            or row.get("state")
            or decision.regime
            or ""
        )
        wpr_shadow_lane = str(decision.shadow_lane or metrics.get("shadow_lane") or "").upper()
        wpr_policy_tag = str(
            metrics.get("profile_patch")
            or metrics.get("policy_tag")
            or metrics.get("policy_note")
            or decision.policy_tag
            or ""
        )
        has_wpr_execution_profile = any(
            key in metrics
            for key in (
                "entry_bp",
                "tp1_bp",
                "full_tp_bp",
                "sl_bp",
                "partial_exit_pct",
                "ttl_s",
            )
        )
        if (
            lane_code == "CNL-WPR-L"
            and wpr_state.startswith("CNL-WPR-L:")
            and (
                wpr_shadow_lane == "SH_WPR_L_S1"
                or has_wpr_execution_profile
                or wpr_policy_tag.startswith(("v139", "v141", "v142", "v145"))
            )
        ):
            passthrough_metrics = {
                **missing_metrics,
                "v1427_action": "MISSING_FEATURES_PROFILE_PASSTHROUGH",
                "v1427_missing_features_passthrough": True,
                "v1427_missing_features_policy": "keep_existing_wpr_profile",
            }
            return replace(
                decision,
                risk_tags=tuple(
                    dict.fromkeys(
                        (
                            *decision.risk_tags,
                            "v1427_missing_features_profile_passthrough",
                        )
                    )
                ),
                metrics=passthrough_metrics,
            )
        return _v1427_block_decision(
            decision,
            reason=V1427_MISSING_FEATURE_BLOCK_TAG,
            metrics=missing_metrics,
            shadow_lane="SH_V1427_MISSING_FEATURES",
            extra_tags=("v1427_missing_features",),
        )

    action = _v1427_predict_action(row)
    base_metrics = _v1427_base_metrics(features, decision, row, action)
    state = str(row.get("state") or "")
    rng15 = row.get("rng15")
    if state == "CNL-WPR-L:falling_discount_trap" and rng15 is not None and float(rng15) >= 90.0:
        base_metrics.update(
            {
                "v1427_overlay": "fdt_rng90_block",
                "v1427_overlay_action": "BLOCK",
                "v1427_overlay_old_action": action,
                "v1427_overlay_rng15_threshold_bp": 90.0,
            }
        )
        return _v1427_block_decision(
            decision,
            reason=V1427_FDT_RNG90_BLOCK_TAG,
            metrics=base_metrics,
            shadow_lane="SH_V1427_FDT_RNG90_BLOCK",
            extra_tags=("v1427_overlay_fdt_rng90_block", f"v1427_old_action_{action.lower()}"),
        )

    if action == "BASE":
        passthrough_metrics = {
            **base_metrics,
            "policy_note": V1427_BASE_PASSTHROUGH_TAG,
            "policy_tag": V1427_BASE_PASSTHROUGH_TAG,
            "live_action": "live_v1427_base_passthrough",
            "source": "v1427_five_window_compact_tree_base_passthrough",
            "profile_source": V1427_DECISION_TREE_SOURCE,
            "market_state": row.get("state"),
            "v143_market_state": row.get("state"),
        }
        return replace(
            decision,
            reason=V1427_BASE_PASSTHROUGH_TAG,
            regime=str(row.get("state") or decision.regime or ""),
            risk_tags=tuple(dict.fromkeys((*decision.risk_tags, "v1427_five_window_compact_tree", "v1427_base_passthrough"))),
            metrics=passthrough_metrics,
            policy_tag=V1427_BASE_PASSTHROUGH_TAG,
        )

    if action == "BLOCK":
        return _v1427_block_decision(
            decision,
            reason=V1427_TREE_BLOCK_TAG,
            metrics=base_metrics,
            shadow_lane="SH_V1427_TREE_BLOCK",
            extra_tags=("v1427_tree_block",),
        )

    profile = _v1427_profile_from_action(action)
    if profile is None:
        return replace(decision, metrics=base_metrics)

    target_side = str(profile["side"]).upper()
    stale_override_details = _v1429_stale_side_override_details(
        features,
        lane_code=lane_code,
        state=state,
        target_side=target_side,
        previous_side=decision.side,
    )
    if stale_override_details:
        base_metrics.update(stale_override_details)
        return _v1427_block_decision(
            decision,
            reason=V1429_STUPS_STALE_SIDE_OVERRIDE_BLOCK_TAG,
            metrics=base_metrics,
            shadow_lane="SH_V1429_STUPS_STALE_SIDE_OVERRIDE",
            extra_tags=("v1429_stale_side_override_block", f"v1429_blocked_{action.lower()}"),
        )
    clean_high_details = _v1433_stups_clean_high_override_details(
        features,
        lane_code=lane_code,
        state=state,
        target_side=target_side,
        previous_side=decision.side,
    )
    if clean_high_details:
        base_metrics.update(clean_high_details)
        return _v1427_block_decision(
            decision,
            reason=V1433_STUPS_CLEAN_HIGH_OVERRIDE_BLOCK_TAG,
            metrics=base_metrics,
            shadow_lane="SH_V1433_STUPS_CLEAN_HIGH_OVERRIDE",
            extra_tags=("v1433_clean_high_override_block", f"v1433_blocked_{action.lower()}"),
        )
    v1455_route, v1455_route_reason, v1455_tp_bp = _v1455_route_for_action(row, decision, action)
    if v1455_route == "BLOCK":
        base_metrics.update(_v1455_route_metrics(v1455_route, v1455_route_reason, action, v1455_tp_bp))
        return _v1427_block_decision(
            decision,
            reason=V1455_STUPS_CLEAN_EXTENSION_TP14_BLOCK_TAG,
            metrics=base_metrics,
            shadow_lane="SH_V1455_STUPS_CLEAN_EXTENSION_TP14_BLOCK",
            extra_tags=("v1455_stups_clean_extension_tp14_block", f"v1455_blocked_{action.lower()}"),
        )
    action_tag = f"v1427_action_{action.lower()}"
    side_override_tag = f"v1427_side_override_{target_side.lower()}" if target_side != str(decision.side or "").upper() else None
    profile_metrics = {
        **base_metrics,
        "policy_note": V1427_POLICY_TAG,
        "policy_tag": V1427_POLICY_TAG,
        "live_action": "live_v1427_five_window_adaptive_profile",
        "source": "v1427_five_window_compact_tree_leaf2_tp14",
        "profile_source": V1427_DECISION_TREE_SOURCE,
        "overlay_source": V1427_OVERLAY_SOURCE,
        "market_state": row.get("state"),
        "v143_market_state": row.get("state"),
        "adaptive_tp_engine": "v1427_five_window_tp14_time_lock" if profile.get("time_lock_enabled") else "v1427_five_window_tp14_full_exit",
        "entry_bp": float(profile["entry_bp"]),
        "tp1_bp": float(profile["tp1_bp"]),
        "full_tp_bp": float(profile["full_tp_bp"]),
        "sl_bp": float(profile["sl_bp"]),
        "be_bp": float(profile["be_bp"]),
        "partial_exit_pct": float(profile["partial_exit_pct"]),
        "ttl_s": int(profile["ttl_s"]),
        "hold_s": int(profile["hold_s"]),
        "profile_anchor": profile.get("profile_anchor"),
        "profile_patch": V1427_POLICY_TAG,
        "target_side": target_side,
        "applied_notional_cap_usdc": decision.requested_notional_usdc,
    }
    profile_metrics.update(_v1455_route_metrics(v1455_route, v1455_route_reason, action, v1455_tp_bp))
    for optional_key in (
        "time_profit_lock_enabled",
        "time_lock_enabled",
        "time_lock_s",
        "time_lock_min_bp",
        "time_lock_slope_max_bp",
        "time_lock_lookback_s",
        "time_lock_reason",
    ):
        if optional_key in profile:
            profile_metrics[optional_key] = profile[optional_key]
    risk_tags = tuple(
        dict.fromkeys(
            tag
            for tag in (
                *decision.risk_tags,
                "v1427_five_window_compact_tree",
                V1427_POLICY_TAG,
                action_tag,
                side_override_tag,
                f"entry{float(profile['entry_bp']):g}",
                f"tp{float(profile['tp1_bp']):g}",
                f"fulltp{float(profile['full_tp_bp']):g}",
                f"sl{float(profile['sl_bp']):g}",
                "be0",
                f"ttl{int(profile['ttl_s'])}s",
                (
                    f"lock{int(profile['time_lock_s'])}_{float(profile['time_lock_min_bp']):g}_{float(profile['time_lock_slope_max_bp']):g}"
                    if profile.get("time_lock_enabled")
                    else None
                ),
            )
            if tag
        )
    )
    return replace(
        decision,
        side=target_side,
        entry_offset_bp=float(profile["entry_bp"]),
        reason=V1427_POLICY_TAG,
        regime=str(row.get("state") or decision.regime or ""),
        risk_tags=risk_tags,
        metrics=profile_metrics,
        policy_tag=V1427_POLICY_TAG,
    )


def _v1430_state_key(row: Mapping[str, Any], decision: CodexV1Decision) -> str:
    lane_code = str(decision.lane_code or "").upper()
    state = str(row.get("state") or decision.regime or lane_code or "UNKNOWN")
    if lane_code == "W1D" and state == "W1D":
        state = "W1D:mixed"
    metrics = decision.metrics if isinstance(decision.metrics, Mapping) else {}
    previous_side = str(metrics.get("v1427_previous_side") or "").upper()
    current_side = str(decision.side or "").upper()
    if lane_code == "CNL-WPR-L" and state == "CNL-WPR-L:deep_discount_stable" and current_side in {"LONG", "SHORT"}:
        side = current_side
    else:
        side = previous_side if previous_side in {"LONG", "SHORT"} else current_side
    return f"{lane_code}|{state}|{side}"


def _v1430_condition_matches(row: Mapping[str, Any], condition: Mapping[str, Any]) -> tuple[bool | None, str | None]:
    feature = str(condition.get("feature") or "")
    op = str(condition.get("op") or "")
    value = row.get(feature)
    if value is None:
        return None, feature or None
    try:
        parsed = float(value)
        threshold = float(condition.get("threshold"))
    except (TypeError, ValueError):
        return None, feature or None
    if not isfinite(parsed) or not isfinite(threshold):
        return None, feature or None
    if op == "<=":
        return parsed <= threshold, None
    if op == ">=":
        return parsed >= threshold, None
    if op == "<":
        return parsed < threshold, None
    if op == ">":
        return parsed > threshold, None
    return None, feature or None


def _v1430_profile_from_action(action: str | None) -> dict[str, Any] | None:
    if not action or action == "block_all":
        return None
    profile = V1430_ACTION_PROFILES.get(str(action))
    return dict(profile) if profile is not None else None


def _v1430_resolve_action(row: Mapping[str, Any], rule: Mapping[str, Any]) -> tuple[str, str | None, str | None]:
    if bool(rule.get("block_all")):
        return "BLOCK", None, "block_all"

    loss_condition = rule.get("loss_condition")
    if isinstance(loss_condition, Mapping):
        matched, missing = _v1430_condition_matches(row, loss_condition)
        if missing:
            return "MISSING", missing, "missing_loss_prune_feature"
        keep_when = str(rule.get("keep_when") or "match")
        keep = bool(matched) if keep_when == "match" else not bool(matched)
        if not keep:
            return "BLOCK", None, "loss_prune_rejected"

    split = rule.get("split")
    if isinstance(split, Mapping):
        condition = split.get("condition")
        if not isinstance(condition, Mapping):
            return "BLOCK", None, "malformed_split"
        matched, missing = _v1430_condition_matches(row, condition)
        if missing:
            return "MISSING", missing, "missing_split_feature"
        action = str(split.get("match_action") if matched else split.get("not_match_action") or "block_all")
        if action == "block_all":
            return "BLOCK", None, "targeted_split_block"
        if _v1430_profile_from_action(action) is None:
            return "BLOCK", None, "unknown_profile_action"
        return "PROFILE", action, "targeted_split_profile"

    action = str(rule.get("baseline_action") or "")
    if _v1430_profile_from_action(action) is None:
        return "BLOCK", None, "unknown_baseline_action"
    return "PROFILE", action, "baseline_profile"


def _v1430_base_metrics(
    features: Mapping[str, Any],
    decision: CodexV1Decision,
    row: Mapping[str, Any],
    *,
    state_key: str,
    rule: Mapping[str, Any],
    final_action: str | None,
    resolution: str,
) -> dict[str, Any]:
    metrics = decision.metrics if isinstance(decision.metrics, Mapping) else {}
    previous_policy_tag = decision.policy_tag or metrics.get("policy_tag") or metrics.get("policy_note")
    return {
        **dict(metrics),
        "v1430_policy_tag": V1430_POLICY_TAG,
        "v1430_policy_source": V1430_SOURCE,
        "v1430_targeted_split_source": V1430_TARGETED_SPLIT_SOURCE,
        "v1430_previous_policy_tag": previous_policy_tag,
        "v1430_state_key": state_key,
        "v1430_state": row.get("state"),
        "v1430_action_id": rule.get("action_id"),
        "v1430_baseline_action": rule.get("baseline_action"),
        "v1430_final_action": final_action,
        "v1430_resolution": resolution,
        "v1430_counts": rule.get("counts"),
        "v1430_summary": rule.get("summary"),
        "v1430_total": V1430_TOTAL_METRICS,
        "v1430_features": _v1427_feature_snapshot(row),
        "v1430_slope_source": features.get("v1423_slope_source") or features.get("v1421_slope_source") or features.get("slope_source"),
    }


def _v1430_block_decision(
    decision: CodexV1Decision,
    *,
    reason: str,
    metrics: Mapping[str, Any],
    shadow_lane: str,
    missing_features: Sequence[str] = (),
    extra_tags: Sequence[str] = (),
) -> CodexV1Decision:
    blocked = _v1427_block_decision(
        decision,
        reason=reason,
        metrics=metrics,
        shadow_lane=shadow_lane,
        extra_tags=extra_tags,
    )
    if missing_features:
        return replace(blocked, missing_features=tuple(missing_features))
    return blocked


def apply_v1430_loss_prune_decision(
    features: Mapping[str, Any],
    decision: CodexV1Decision,
) -> CodexV1Decision:
    row = _v1421_feature_row(features, decision)
    state_key = _v1430_state_key(row, decision)
    rule = V1430_RULES.get(state_key)
    if rule is None:
        return decision

    action_kind, action, resolution = _v1430_resolve_action(row, rule)
    base_metrics = _v1430_base_metrics(
        features,
        decision,
        row,
        state_key=state_key,
        rule=rule,
        final_action=action,
        resolution=resolution or action_kind.lower(),
    )

    if not decision.accepted:
        return decision

    if action_kind == "MISSING":
        missing = (str(action),) if action else ()
        missing_metrics = {
            **base_metrics,
            "v1430_block_reason": V1430_MISSING_FEATURE_BLOCK_TAG,
            "v1430_missing_features": missing,
            "policy_note": V1430_MISSING_FEATURE_BLOCK_TAG,
            "policy_tag": V1430_MISSING_FEATURE_BLOCK_TAG,
        }
        return _v1430_block_decision(
            decision,
            reason=V1430_MISSING_FEATURE_BLOCK_TAG,
            metrics=missing_metrics,
            shadow_lane="SH_V1430_MISSING_FEATURES",
            missing_features=missing,
            extra_tags=("v1430_missing_features",),
        )

    if action_kind == "BLOCK":
        block_metrics = {
            **base_metrics,
            "v1430_block_reason": resolution,
            "policy_note": V1430_BLOCK_TAG,
            "policy_tag": V1430_BLOCK_TAG,
        }
        return _v1430_block_decision(
            decision,
            reason=V1430_BLOCK_TAG,
            metrics=block_metrics,
            shadow_lane="SH_V1430_LOSS_PRUNE_BLOCK",
            extra_tags=("v1430_loss_prune_block", f"v1430_{state_key.lower().replace('|', '_').replace(':', '_')}"),
        )

    profile = _v1430_profile_from_action(action)
    if profile is None:
        return decision

    target_side = state_key.rsplit("|", 1)[-1]
    profile_metrics = {
        **base_metrics,
        "policy_note": V1430_POLICY_TAG,
        "policy_tag": V1430_POLICY_TAG,
        "live_action": "live_v1430_loss_prune_profile",
        "source": "v1430_selective_hybrid_loss_prune_median_exit",
        "profile_source": V1430_SOURCE,
        "targeted_split_source": V1430_TARGETED_SPLIT_SOURCE,
        "market_state": row.get("state"),
        "v143_market_state": row.get("state"),
        "adaptive_tp_engine": "v1430_loss_prune_trail",
        "entry_bp": float(profile["entry_bp"]),
        "tp1_bp": float(profile["tp1_bp"]),
        "full_tp_bp": float(profile["full_tp_bp"]),
        "sl_bp": float(profile["sl_bp"]),
        "be_bp": float(profile["be_bp"]),
        "partial_exit_pct": float(profile["partial_exit_pct"]),
        "ttl_s": int(profile["ttl_s"]),
        "hold_s": int(profile["hold_s"]),
        "trail_arm_bp": float(profile["trail_arm_bp"]),
        "trail_giveback_bp": float(profile["trail_giveback_bp"]),
        "trail_floor_bp": float(profile["trail_floor_bp"]),
        "profile_anchor": profile.get("profile_anchor"),
        "tp_execution_note": profile.get("tp_execution_note"),
        "profile_patch": V1430_POLICY_TAG,
        "target_side": target_side,
        "applied_notional_cap_usdc": decision.requested_notional_usdc,
    }
    action_tag = f"v1430_action_{str(action).lower()}"
    side_override_tag = f"v1430_side_override_{target_side.lower()}" if target_side != str(decision.side or "").upper() else None
    risk_tags = tuple(
        dict.fromkeys(
            tag
            for tag in (
                *decision.risk_tags,
                "v1430_loss_prune",
                V1430_POLICY_TAG,
                action_tag,
                side_override_tag,
                f"entry{float(profile['entry_bp']):g}",
                f"tp{float(profile['tp1_bp']):g}",
                f"fulltp{float(profile['full_tp_bp']):g}",
                f"sl{float(profile['sl_bp']):g}",
                f"trailarm{float(profile['trail_arm_bp']):g}",
                f"trailgb{float(profile['trail_giveback_bp']):g}",
                f"trailfloor{float(profile['trail_floor_bp']):g}",
                f"ttl{int(profile['ttl_s'])}s",
            )
            if tag
        )
    )
    return replace(
        decision,
        side=target_side,
        entry_offset_bp=float(profile["entry_bp"]),
        reason=V1430_POLICY_TAG,
        regime=str(row.get("state") or decision.regime or ""),
        risk_tags=risk_tags,
        metrics=profile_metrics,
        policy_tag=V1430_POLICY_TAG,
    )



def apply_v1436_live_hotfix_decision(
    features: Mapping[str, Any],
    decision: CodexV1Decision,
) -> CodexV1Decision:
    if not decision.accepted:
        return decision
    row = _v1421_feature_row(features, decision)
    lane_code = str(decision.lane_code or "").upper()
    state = str(row.get("state") or decision.regime or "")
    side = str(decision.side or "").upper()
    if lane_code == "CNL-WPR-L" and state == "CNL-WPR-L:fast_reclaim" and side == "LONG":
        try:
            slope60 = float(row.get("slope60"))
            slope120 = float(row.get("slope120"))
            vwap = float(row.get("vwap"))
        except (TypeError, ValueError):
            return decision
        if isfinite(slope60) and isfinite(slope120) and isfinite(vwap):
            if slope60 <= -2.0 and slope120 <= -4.0 and vwap <= -8.0:
                metrics = dict(decision.metrics or {})
                metrics.update(
                    {
                        "policy_note": V1436_FAST_RECLAIM_DOWNSLOPE_BLOCK_TAG,
                        "policy_tag": V1436_FAST_RECLAIM_DOWNSLOPE_BLOCK_TAG,
                        "v1436_block_reason": "fast_reclaim_long_downslope_not_reclaimed",
                        "v1436_features": {
                            "slope60": round(slope60, 4),
                            "slope120": round(slope120, 4),
                            "vwap": round(vwap, 4),
                        },
                        "v1436_thresholds": {
                            "slope60_max": -2.0,
                            "slope120_max": -4.0,
                            "vwap_max": -8.0,
                        },
                    }
                )
                return replace(
                    decision,
                    accepted=False,
                    reason=V1436_FAST_RECLAIM_DOWNSLOPE_BLOCK_TAG,
                    size_mult=0.0,
                    notional_mult=0.0,
                    requested_notional_usdc=0.0,
                    risk_tags=tuple(dict.fromkeys((*decision.risk_tags, V1436_FAST_RECLAIM_DOWNSLOPE_BLOCK_TAG))),
                    metrics=metrics,
                    policy_tag=V1436_FAST_RECLAIM_DOWNSLOPE_BLOCK_TAG,
                    shadow_lane="SH_V1436_FAST_RECLAIM_DOWNSLOPE",
                )
    if lane_code == "STUP-S" and state == "STUP-S:clean_extension" and side in {"LONG", "SHORT"}:
        selector_action = str((decision.metrics or {}).get("v1441_research_selector_action") or "")
        try:
            d30 = float(row.get("d30"))
            adv3 = float(row.get("adv3"))
            rsi = float(row.get("rsi"))
            vwap_dist_bp = float(row.get("vwap_dist_bp", row.get("vwap")))
            range_pos_15 = float(row.get("range_pos_15", row.get("range_pos")))
            pullback = float(row.get("pullback_from_recent_high_bp", row.get("pullback")))
            slope120 = float(row.get("slope120"))
        except (TypeError, ValueError):
            return decision
        values = (d30, adv3, rsi, vwap_dist_bp, range_pos_15, pullback, slope120)
        if selector_action == "SHADOW_REVIEW" and all(isfinite(value) for value in values):
            hot_extension_short_trap = (
                side == "SHORT"
                and d30 >= 40.0
                and adv3 >= 8.0
                and slope120 >= 8.0
                and rsi >= 67.0
                and vwap_dist_bp >= 8.0
                and range_pos_15 >= 0.95
            )
            weak_extension_long_chase = (
                side == "LONG"
                and d30 >= 30.0
                and adv3 <= 3.0
                and rsi >= 63.0
                and vwap_dist_bp >= 15.0
                and pullback >= 20.0
            )
            if hot_extension_short_trap or weak_extension_long_chase:
                block_shape = "hot_extension_short_trap" if hot_extension_short_trap else "weak_extension_long_chase"
                metrics = dict(decision.metrics or {})
                metrics.update(
                    {
                        "policy_note": V1451_STUPS_CLEAN_EXTENSION_SHADOW_REVIEW_BLOCK_TAG,
                        "policy_tag": V1451_STUPS_CLEAN_EXTENSION_SHADOW_REVIEW_BLOCK_TAG,
                        "v1451_block_reason": block_shape,
                        "v1451_live_sample_runs": [
                            "cry3mn_1783225643617",
                            "cry3mn_1783226163122",
                        ],
                        "v1451_features": {
                            "d30": round(d30, 4),
                            "adv3": round(adv3, 4),
                            "rsi": round(rsi, 4),
                            "vwap_dist_bp": round(vwap_dist_bp, 4),
                            "range_pos_15": round(range_pos_15, 4),
                            "pullback_from_recent_high_bp": round(pullback, 4),
                            "slope120": round(slope120, 4),
                            "selector_action": selector_action,
                            "state": state,
                            "side": side,
                        },
                        "v1451_thresholds": {
                            "hot_short_d30_min": 40.0,
                            "hot_short_adv3_min": 8.0,
                            "hot_short_slope120_min": 8.0,
                            "hot_short_rsi_min": 67.0,
                            "hot_short_vwap_min": 8.0,
                            "hot_short_range_pos_min": 0.95,
                            "weak_long_d30_min": 30.0,
                            "weak_long_adv3_max": 3.0,
                            "weak_long_rsi_min": 63.0,
                            "weak_long_vwap_min": 15.0,
                            "weak_long_pullback_min": 20.0,
                        },
                    }
                )
                return replace(
                    decision,
                    accepted=False,
                    reason=V1451_STUPS_CLEAN_EXTENSION_SHADOW_REVIEW_BLOCK_TAG,
                    size_mult=0.0,
                    notional_mult=0.0,
                    requested_notional_usdc=0.0,
                    risk_tags=tuple(dict.fromkeys((*decision.risk_tags, V1451_STUPS_CLEAN_EXTENSION_SHADOW_REVIEW_BLOCK_TAG, f"v1451_{block_shape}"))),
                    metrics=metrics,
                    policy_tag=V1451_STUPS_CLEAN_EXTENSION_SHADOW_REVIEW_BLOCK_TAG,
                    shadow_lane="SH_V1451_STUPS_CLEAN_EXTENSION_SHADOW_REVIEW",
                )
    if lane_code == "CNL-WPR-L" and state == "CNL-WPR-L:falling_discount_trap" and side == "LONG":
        try:
            rng15 = float(row.get("rng15"))
            d30 = float(row.get("d30"))
            adv3 = float(row.get("adv3"))
        except (TypeError, ValueError):
            return decision
        if isfinite(rng15) and isfinite(d30) and isfinite(adv3):
            false_bounce = rng15 <= 20.0 and d30 <= -25.0 and adv3 >= 20.0
            no_reclaim = d30 <= -35.0 and adv3 <= 0.0
            if false_bounce or no_reclaim:
                block_shape = "low_rng_false_bounce" if false_bounce else "deep_fall_no_reclaim"
                metrics = dict(decision.metrics or {})
                metrics.update(
                    {
                        "policy_note": V1449_CNL_WPR_FALLING_TRAP_QUALITY_BLOCK_TAG,
                        "policy_tag": V1449_CNL_WPR_FALLING_TRAP_QUALITY_BLOCK_TAG,
                        "v1449_block_reason": block_shape,
                        "v1449_live_sample_runs": [
                            "cry3mn_1783195853863",
                            "cry3mn_1783196733860",
                        ],
                        "v1449_features": {
                            "rng15": round(rng15, 4),
                            "d30": round(d30, 4),
                            "adv3": round(adv3, 4),
                            "state": state,
                            "side": side,
                        },
                        "v1449_thresholds": {
                            "false_bounce_rng15_max": 20.0,
                            "false_bounce_d30_max": -25.0,
                            "false_bounce_adv3_min": 20.0,
                            "no_reclaim_d30_max": -35.0,
                            "no_reclaim_adv3_max": 0.0,
                        },
                    }
                )
                return replace(
                    decision,
                    accepted=False,
                    reason=V1449_CNL_WPR_FALLING_TRAP_QUALITY_BLOCK_TAG,
                    size_mult=0.0,
                    notional_mult=0.0,
                    requested_notional_usdc=0.0,
                    risk_tags=tuple(dict.fromkeys((*decision.risk_tags, V1449_CNL_WPR_FALLING_TRAP_QUALITY_BLOCK_TAG, f"v1449_{block_shape}"))),
                    metrics=metrics,
                    policy_tag=V1449_CNL_WPR_FALLING_TRAP_QUALITY_BLOCK_TAG,
                    shadow_lane="SH_V1449_CNL_WPR_FALLING_TRAP_QUALITY",
                )

    if lane_code == "CNL-WPR-L" and state == "CNL-WPR-L:deep_discount_stable" and side == "LONG":
        try:
            rng15 = float(row.get("rng15"))
            d30 = float(row.get("d30"))
            adv3 = float(row.get("adv3"))
            rsi = float(row.get("rsi"))
            vwap_dist_bp = float(row.get("vwap_dist_bp", row.get("vwap")))
            range_pos_15 = float(row.get("range_pos_15", row.get("range_pos")))
            pullback = float(row.get("pullback_from_recent_high_bp", row.get("pullback")))
            close_pos_raw = row.get("close_pos", features.get("close_pos"))
            close_pos = float(close_pos_raw) if close_pos_raw is not None else float("nan")
        except (TypeError, ValueError):
            return decision
        values = (rng15, d30, adv3, rsi, vwap_dist_bp, range_pos_15, pullback)
        if all(isfinite(value) for value in values):
            weak_rebound_late_chase = (
                d30 >= 10.0
                and adv3 <= 1.0
                and range_pos_15 >= 0.60
                and (not isfinite(close_pos) or close_pos >= 0.80)
                and vwap_dist_bp <= -25.0
                and pullback <= 10.0
            )
            upper_window_exhaustion = (
                range_pos_15 >= 0.85
                and pullback <= 3.5
                and rsi >= 58.0
                and vwap_dist_bp <= -25.0
                and adv3 <= 0.0
            )
            if weak_rebound_late_chase or upper_window_exhaustion:
                block_shape = "weak_rebound_late_chase" if weak_rebound_late_chase else "upper_window_exhaustion"
                metrics = dict(decision.metrics or {})
                metrics.update(
                    {
                        "policy_note": V1450_CNL_WPR_DEEP_LATE_CHASE_BLOCK_TAG,
                        "policy_tag": V1450_CNL_WPR_DEEP_LATE_CHASE_BLOCK_TAG,
                        "v1450_block_reason": block_shape,
                        "v1450_live_sample_runs": [
                            "cry3mn_1783221548047",
                            "cry3mn_1783222767945",
                            "cry3mn_1783223348090",
                        ],
                        "v1450_features": {
                            "rng15": round(rng15, 4),
                            "d30": round(d30, 4),
                            "adv3": round(adv3, 4),
                            "rsi": round(rsi, 4),
                            "vwap_dist_bp": round(vwap_dist_bp, 4),
                            "range_pos_15": round(range_pos_15, 4),
                            "close_pos": round(close_pos, 4) if isfinite(close_pos) else None,
                            "pullback_from_recent_high_bp": round(pullback, 4),
                            "state": state,
                            "side": side,
                        },
                        "v1450_thresholds": {
                            "weak_rebound_d30_min": 10.0,
                            "weak_rebound_adv3_max": 1.0,
                            "weak_rebound_range_pos_min": 0.60,
                            "weak_rebound_close_pos_min": 0.80,
                            "weak_rebound_vwap_max": -25.0,
                            "weak_rebound_pullback_max": 10.0,
                            "upper_window_range_pos_min": 0.85,
                            "upper_window_pullback_max": 3.5,
                            "upper_window_rsi_min": 58.0,
                            "upper_window_vwap_max": -25.0,
                            "upper_window_adv3_max": 0.0,
                        },
                    }
                )
                return replace(
                    decision,
                    accepted=False,
                    reason=V1450_CNL_WPR_DEEP_LATE_CHASE_BLOCK_TAG,
                    size_mult=0.0,
                    notional_mult=0.0,
                    requested_notional_usdc=0.0,
                    risk_tags=tuple(dict.fromkeys((*decision.risk_tags, V1450_CNL_WPR_DEEP_LATE_CHASE_BLOCK_TAG, f"v1450_{block_shape}"))),
                    metrics=metrics,
                    policy_tag=V1450_CNL_WPR_DEEP_LATE_CHASE_BLOCK_TAG,
                    shadow_lane="SH_V1450_CNL_WPR_DEEP_LATE_CHASE",
                )
    if lane_code == "CNL-WPR-L" and state == "CNL-WPR-L:fast_reclaim" and side == "SHORT":
        try:
            rng15 = float(row.get("rng15"))
            adv3 = float(row.get("adv3"))
            d30 = float(row.get("d30"))
        except (TypeError, ValueError):
            return decision
        if isfinite(rng15) and isfinite(adv3) and isfinite(d30):
            weak_reclaim = rng15 <= 35.0 and abs(adv3) <= 2.0
            if weak_reclaim:
                metrics = dict(decision.metrics or {})
                metrics.update(
                    {
                        "policy_note": V1449_CNL_WPR_FAST_RECLAIM_QUALITY_BLOCK_TAG,
                        "policy_tag": V1449_CNL_WPR_FAST_RECLAIM_QUALITY_BLOCK_TAG,
                        "v1449_block_reason": "fast_reclaim_short_weak_adv3",
                        "v1449_live_sample_runs": ["cry3mn_1783197093854"],
                        "v1449_features": {
                            "rng15": round(rng15, 4),
                            "d30": round(d30, 4),
                            "adv3": round(adv3, 4),
                            "state": state,
                            "side": side,
                        },
                        "v1449_thresholds": {
                            "rng15_max": 35.0,
                            "abs_adv3_max": 2.0,
                        },
                    }
                )
                return replace(
                    decision,
                    accepted=False,
                    reason=V1449_CNL_WPR_FAST_RECLAIM_QUALITY_BLOCK_TAG,
                    size_mult=0.0,
                    notional_mult=0.0,
                    requested_notional_usdc=0.0,
                    risk_tags=tuple(dict.fromkeys((*decision.risk_tags, V1449_CNL_WPR_FAST_RECLAIM_QUALITY_BLOCK_TAG, "v1449_fast_reclaim_short_weak_adv3"))),
                    metrics=metrics,
                    policy_tag=V1449_CNL_WPR_FAST_RECLAIM_QUALITY_BLOCK_TAG,
                    shadow_lane="SH_V1449_CNL_WPR_FAST_RECLAIM_QUALITY",
                )

    return decision

def apply_v1423_conservative_decision(
    features: Mapping[str, Any],
    decision: CodexV1Decision,
) -> CodexV1Decision:
    if not decision.accepted:
        return decision
    lane_code = str(decision.lane_code or "").upper()
    lane = str(decision.lane or "")
    if lane_code not in V1423_LIVE_LANE_CODES:
        return decision
    if lane_code == "W6A" or lane == "w6_lane_s1long_rng38_86_range9_15_e0":
        return decision
    metrics = decision.metrics if isinstance(decision.metrics, Mapping) else {}
    if metrics.get("v1423_action"):
        return decision

    row = _v1421_feature_row(features, decision)
    missing = tuple(key for key in V1423_REQUIRED_FEATURES if row.get(key) is None)
    if missing:
        return decision

    action = _v1423_predict_action(row)
    base_metrics = dict(metrics)
    previous_policy_tag = decision.policy_tag or base_metrics.get("policy_tag") or base_metrics.get("policy_note")
    feature_snapshot = {
        key: round(float(row[key]), 4)
        for key in V1423_REQUIRED_FEATURES
        if row.get(key) is not None
    }
    base_metrics.update(
        {
            "v1423_action": action,
            "v1423_state": row.get("state"),
            "v1423_policy_tag": V1423_POLICY_TAG,
            "v1423_policy_source": V1423_DECISION_TREE_SOURCE,
            "v1423_previous_policy_tag": previous_policy_tag,
            "v1423_previous_side": decision.side,
            "v1423_features": feature_snapshot,
            "v1423_slope_source": features.get("v1423_slope_source") or features.get("v1421_slope_source") or features.get("slope_source"),
            "v1423_projected_50_net_usdc": V1423_PROJECTED_50_NET_USDC,
            "v1423_target_50_net_usdc": V1423_TARGET_50_NET_USDC,
            "v1423_target_wr": V1423_TARGET_WR,
            "v1423_max_tp_bp": V1423_MAX_TP_BP,
        }
    )
    if action == "BASE":
        state = str(row.get("state") or "")
        if lane_code == "STUP-S" or state.startswith("STUP-S:"):
            return replace(
                decision,
                accepted=False,
                reason=V1424_STUPS_BASE_BLOCK_TAG,
                size_mult=0.0,
                notional_mult=0.0,
                requested_notional_usdc=0.0,
                risk_tags=tuple(dict.fromkeys((*decision.risk_tags, V1424_STUPS_BASE_BLOCK_TAG))),
                metrics={
                    **base_metrics,
                    "policy_note": V1424_STUPS_BASE_BLOCK_TAG,
                    "policy_tag": V1424_STUPS_BASE_BLOCK_TAG,
                    "blocked_base_fallback": True,
                },
                policy_tag=V1424_STUPS_BASE_BLOCK_TAG,
                shadow_lane="SH_V1424_STUPS_BASE_BLOCK",
            )
        if lane_code == "CNL-WPR-L" or state.startswith("CNL-WPR-L:"):
            return replace(
                decision,
                accepted=False,
                reason=V1426_WPR_BASE_FALLBACK_BLOCK_TAG,
                size_mult=0.0,
                notional_mult=0.0,
                requested_notional_usdc=0.0,
                risk_tags=tuple(dict.fromkeys((*decision.risk_tags, V1426_WPR_BASE_FALLBACK_BLOCK_TAG))),
                metrics={
                    **base_metrics,
                    "policy_note": V1426_WPR_BASE_FALLBACK_BLOCK_TAG,
                    "policy_tag": V1426_WPR_BASE_FALLBACK_BLOCK_TAG,
                    "blocked_base_fallback": True,
                    "blocked_v1426_state": state,
                },
                policy_tag=V1426_WPR_BASE_FALLBACK_BLOCK_TAG,
                shadow_lane="SH_V1426_WPR_BASE_BLOCK",
            )
        return replace(decision, metrics=base_metrics)
    if action == "BLOCK":
        return replace(
            decision,
            accepted=False,
            reason="v1423_conservative_tree_block",
            size_mult=0.0,
            notional_mult=0.0,
            requested_notional_usdc=0.0,
            risk_tags=tuple(dict.fromkeys((*decision.risk_tags, "v1423_conservative_tree_block"))),
            metrics={**base_metrics, "policy_note": "v1423_conservative_tree_block", "policy_tag": "v1423_conservative_tree_block"},
            policy_tag="v1423_conservative_tree_block",
            shadow_lane="SH_V1423_TREE_BLOCK",
        )

    profile = _v1423_profile_from_action(action)
    if profile is None:
        return replace(decision, metrics=base_metrics)

    state = str(row.get("state") or "")
    action_block_reason: str | None = None
    action_block_shadow_lane = "SH_V1425_ACTION_BLOCK"
    action_block_tag_prefix = "v1425"
    if lane_code == "STUP-S" and state == "STUP-S:weak_chop" and action.startswith("L_E0_"):
        action_block_reason = V1425_STUPS_WEAK_CHOP_DIRECT_LONG_BLOCK_TAG
    elif lane_code == "CNL-WPR-L" and state == "CNL-WPR-L:falling_discount_trap" and action.startswith("L_E0_"):
        action_block_reason = V1425_WPR_FALLING_TRAP_DIRECT_LONG_BLOCK_TAG
    elif lane_code == "STUP-S" and state == "STUP-S:weak_chop" and action.startswith("S_E"):
        action_block_reason = V1426_STUPS_WEAK_CHOP_SHORT_BLOCK_TAG
        action_block_shadow_lane = "SH_V1426_STUPS_SHORT_BLOCK"
        action_block_tag_prefix = "v1426"
    elif lane_code == "STUP-S" and state == "STUP-S:mixed" and action.startswith("S_E"):
        action_block_reason = V1426_STUPS_MIXED_SHORT_BLOCK_TAG
        action_block_shadow_lane = "SH_V1426_STUPS_SHORT_BLOCK"
        action_block_tag_prefix = "v1426"
    if action_block_reason:
        return replace(
            decision,
            accepted=False,
            reason=action_block_reason,
            size_mult=0.0,
            notional_mult=0.0,
            requested_notional_usdc=0.0,
            risk_tags=tuple(dict.fromkeys((*decision.risk_tags, action_block_reason, f"{action_block_tag_prefix}_blocked_action_{action.lower()}"))),
            metrics={
                **base_metrics,
                "policy_note": action_block_reason,
                "policy_tag": action_block_reason,
                "blocked_v1425_action": action,
                "blocked_v1425_state": state,
                "blocked_v1426_action": action,
                "blocked_v1426_state": state,
            },
            policy_tag=action_block_reason,
            shadow_lane=action_block_shadow_lane,
        )

    if lane_code == "CNL-WPR-L" and state == "CNL-WPR-L:falling_discount_trap" and action.startswith(("S_E0_", "S_E2_")):
        profile = {
            **profile,
            "entry_bp": 2.0,
            "tp1_bp": 6.0,
            "full_tp_bp": 6.0,
            "sl_bp": 6.0,
            "be_bp": 0.0,
            "partial_exit_pct": 1.0,
            "ttl_s": max(int(profile.get("ttl_s") or 45), 60),
            "profit_lock_mfe_bp": 6.0,
            "profit_lock_floor_bp": 4.0,
            "profit_lock_giveback_bp": 2.0,
            "profile_patch": V1426_WPR_FALLING_TRAP_SHORT_SCALP_TAG,
            "profile_anchor": f"v1426_wpr_falling_trap_short_scalp_from_{action.lower()}",
            "v1425_original_action": action,
            "v1426_original_action": action,
        }

    target_side = str(profile["side"]).upper()
    action_tag = f"v1423_action_{action.lower()}"
    profile_policy_tag = str(profile.get("profile_patch") or V1423_POLICY_TAG)
    side_override_tag = f"v1423_side_override_{target_side.lower()}" if target_side != str(decision.side or "").upper() else None
    profile_metrics = {
        **base_metrics,
        "policy_note": profile_policy_tag,
        "policy_tag": profile_policy_tag,
        "live_action": "live_conservative_tree_adaptive_profile",
        "source": "v1423_four_window_conservative_tree_search",
        "profile_source": V1423_DECISION_TREE_SOURCE,
        "market_state": row.get("state"),
        "v143_market_state": row.get("state"),
        "adaptive_tp_engine": "v1423_four_window_conservative_tree",
        "entry_bp": float(profile["entry_bp"]),
        "tp1_bp": float(profile["tp1_bp"]),
        "full_tp_bp": float(profile["full_tp_bp"]),
        "sl_bp": float(profile["sl_bp"]),
        "be_bp": float(profile["be_bp"]),
        "partial_exit_pct": float(profile["partial_exit_pct"]),
        "ttl_s": int(profile["ttl_s"]),
        "hold_s": None,
        "profile_anchor": profile.get("profile_anchor"),
        "profile_patch": profile_policy_tag,
        "target_side": target_side,
        "applied_notional_cap_usdc": decision.requested_notional_usdc,
    }
    for optional_key in (
        "profit_lock_mfe_bp",
        "profit_lock_floor_bp",
        "profit_lock_giveback_bp",
        "pre_tp_profit_lock_enabled",
        "pre_tp_profit_lock_mfe_bp",
        "pre_tp_profit_lock_floor_bp",
        "pre_tp_profit_lock_method",
        "v1425_original_action",
        "v1426_original_action",
    ):
        if optional_key in profile:
            profile_metrics[optional_key] = profile[optional_key]
    risk_tags = tuple(
        dict.fromkeys(
            tag
            for tag in (
                *decision.risk_tags,
                "v1423_four_window_conservative_tree",
                profile_policy_tag,
                action_tag,
                side_override_tag,
                f"entry{float(profile['entry_bp']):g}",
                f"tp{float(profile['tp1_bp']):g}",
                f"fulltp{float(profile['full_tp_bp']):g}",
                f"sl{float(profile['sl_bp']):g}",
                "be0",
                f"ttl{int(profile['ttl_s'])}s",
            )
            if tag
        )
    )
    return replace(
        decision,
        side=target_side,
        entry_offset_bp=float(profile["entry_bp"]),
        reason=profile_policy_tag,
        regime=str(row.get("state") or decision.regime or ""),
        risk_tags=risk_tags,
        metrics=profile_metrics,
        policy_tag=profile_policy_tag,
    )

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
def _stups_v143_market_state(features: Mapping[str, Any]) -> str:
    rng15 = _feature_value(features, "rng15")
    d30 = _feature_value(features, "d30")
    adv3 = _feature_value(features, "adv3")
    rsi = _feature_value(features, "rsi")
    vwap = _feature_value(features, "vwap_dist_bp")
    range_bp = _feature_value(features, "range_bp")
    range_pos = _feature_value(features, "range_pos_15")
    pull = _feature_value(features, "pullback_from_recent_high_bp")
    wait = _feature_value(features, "reprice_wait_elapsed_seconds") or 0.0
    if any(value is None for value in (rng15, d30, adv3, rsi, vwap, range_bp, range_pos)):
        return "STUP-S:missing_features"

    pull = pull if pull is not None else 0.0
    if wait >= 300.0 and range_bp < 3.0 and range_pos >= 0.95:
        return "STUP-S:stale_squeeze_top"
    if range_bp <= 2.0 and range_pos >= 0.90 and vwap >= 8.0:
        return "STUP-S:stale_squeeze_top"
    if d30 < -15.0 and range_pos < 0.45:
        return "STUP-S:counter_recoil"
    if vwap < 5.0 and range_bp < 2.0:
        return "STUP-S:near_vwap_flat"
    if adv3 < 1.0 and range_pos < 0.35 and pull < 8.0:
        return "STUP-S:no_momentum_edge"
    if rng15 >= 65.0 and d30 >= 30.0 and adv3 >= 20.0 and rsi >= 64.0 and vwap >= 15.0 and range_pos >= 0.55:
        return "STUP-S:hot_continuation"
    if rsi >= 58.0 and vwap >= 8.0 and range_pos >= 0.55 and adv3 >= 1.0:
        return "STUP-S:clean_extension"
    if d30 >= -5.0 and vwap >= 5.0 and adv3 >= 2.0 and 0.35 <= range_pos <= 0.95:
        return "STUP-S:weak_chop"
    return "STUP-S:mixed"


def _stups_v1420_clean_extension_good(features: Mapping[str, Any]) -> bool:
    rng15 = _feature_value(features, "rng15")
    d30 = _feature_value(features, "d30")
    vwap = _feature_value(features, "vwap_dist_bp")
    pullback = _feature_value(features, "pullback_from_recent_high_bp")
    range_pos = _feature_value(features, "range_pos_15")
    if any(value is None for value in (rng15, d30, vwap, pullback, range_pos)):
        return False
    return bool(rng15 <= 36.0 and 8.0 <= vwap <= 13.5 and d30 >= 10.0 and (pullback >= 27.0 or range_pos <= 0.80))


def _stups_v1420_mixed_bad(features: Mapping[str, Any]) -> bool:
    d30 = _feature_value(features, "d30")
    adv3 = _feature_value(features, "adv3")
    pullback = _feature_value(features, "pullback_from_recent_high_bp")
    range_pos = _feature_value(features, "range_pos_15")
    if any(value is None for value in (d30, adv3, pullback, range_pos)):
        return False
    return bool(d30 <= -7.0 and adv3 >= 7.0 and pullback <= 23.0 and 0.35 <= range_pos <= 0.65)


def _stups_v1420_mixed_weakzone(features: Mapping[str, Any]) -> bool:
    rng15 = _feature_value(features, "rng15")
    range_pos = _feature_value(features, "range_pos_15")
    range_bp = _feature_value(features, "range_bp")
    if any(value is None for value in (rng15, range_pos, range_bp)):
        return False
    return bool(rng15 <= 50.0 and range_pos >= 0.35 and range_bp <= 10.0)


def _stups_v1420_weak_chop_extreme(features: Mapping[str, Any]) -> bool:
    range_pos = _feature_value(features, "range_pos_15")
    range_bp = _feature_value(features, "range_bp")
    return bool((range_pos is not None and range_pos >= 0.90) or (range_bp is not None and range_bp <= 1.5))


def build_strong_fall_follow_short_decision(
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
    d30 = _feature_value(features, "d30")
    rsi = _feature_value(features, "rsi")
    vwap_dist_bp = _feature_value(features, "vwap_dist_bp")
    range_pos_15 = _feature_value(features, "range_pos_15")
    close_pos = _feature_value(features, "close_pos")
    adv3 = _feature_value(features, "adv3")
    range_pos = range_pos_15 if range_pos_15 is not None else close_pos
    if any(value is None for value in (rng15, d30, rsi, vwap_dist_bp, range_pos)):
        return None
    if not (
        rng15 >= 70.0
        and d30 <= -35.0
        and vwap_dist_bp <= -8.0
        and rsi <= 55.0
        and range_pos <= 0.45
    ):
        return None
    if adv3 is not None and adv3 > 12.0:
        return None

    entry_bp = float(STRONG_FALL_FOLLOW_PROFILE["entry_bp"])
    tp1_bp = float(STRONG_FALL_FOLLOW_PROFILE["tp1_bp"])
    full_tp_bp = float(STRONG_FALL_FOLLOW_PROFILE["full_tp_bp"])
    sl_bp = float(STRONG_FALL_FOLLOW_PROFILE["sl_bp"])
    be_bp = float(STRONG_FALL_FOLLOW_PROFILE["be_bp"])
    partial_exit_pct = float(STRONG_FALL_FOLLOW_PROFILE["partial_exit_pct"])
    ttl_s = int(STRONG_FALL_FOLLOW_PROFILE["ttl_s"])
    notional_mult = STRONG_FALL_FOLLOW_NOTIONAL_USDC / BASE_NOTIONAL_USDC
    market_state = "SFD-S:strong_down_continuation"
    state_tag = market_state.split(":", 1)[-1]
    risk_tags = tuple(
        dict.fromkeys(
            (
                "post_only_entry",
                "dca_disabled",
                "strong_fall_follow_canary",
                "rng15_ge70",
                "d30_le_minus35",
                "vwap_dist_le_minus8",
                "rsi_le55",
                f"sfd_state_{state_tag}",
                STRONG_FALL_FOLLOW_POLICY_TAG,
                "fixed_50_usdc",
                "small_n_forward_watch",
            )
        )
    )
    metrics = {
        "policy_note": STRONG_FALL_FOLLOW_POLICY_TAG,
        "policy_tag": STRONG_FALL_FOLLOW_POLICY_TAG,
        "market_state": market_state,
        "v143_market_state": market_state,
        "live_action": "live_canary_strong_down_follow",
        "source": "v1414_live_market_state_hotfix",
        "profile_source": "reports/CODEX_V1_4_14_MARKET_STATE_EXIT_HOTFIX_2026-06-29.md",
        "rng15": round(float(rng15), 4),
        "d30": round(float(d30), 4),
        "rsi": round(float(rsi), 4),
        "vwap_dist_bp": round(float(vwap_dist_bp), 4),
        "range_pos_15": round(float(range_pos_15), 4) if range_pos_15 is not None else None,
        "close_pos": round(float(close_pos), 4) if close_pos is not None else None,
        "adv3": round(float(adv3), 4) if adv3 is not None else None,
        "admission_guard": "s1_short_rng15_ge70_d30_le_minus35_vwap_le_minus8_rsi_le55_pos_le045",
        "applied_notional_cap_usdc": STRONG_FALL_FOLLOW_NOTIONAL_USDC,
        "fixed_notional_usdc": STRONG_FALL_FOLLOW_NOTIONAL_USDC,
        "entry_bp": entry_bp,
        "tp1_bp": tp1_bp,
        "full_tp_bp": full_tp_bp,
        "partial_exit_pct": partial_exit_pct,
        "sl_bp": sl_bp,
        "be_bp": be_bp,
        "ttl_s": ttl_s,
        "profit_lock_mfe_bp": STRONG_FALL_FOLLOW_PROFILE["profit_lock_mfe_bp"],
        "profit_lock_floor_bp": STRONG_FALL_FOLLOW_PROFILE["profit_lock_floor_bp"],
        "profit_lock_giveback_bp": STRONG_FALL_FOLLOW_PROFILE["profit_lock_giveback_bp"],
        "small_n_forward_watch": True,
    }
    return CodexV1Decision(
        accepted=True,
        version=CODEX_V1_VERSION,
        baseline=CODEX_V1_BASELINE,
        lane=STRONG_FALL_FOLLOW_LANE,
        lane_code=STRONG_FALL_FOLLOW_LANE_CODE,
        strategy=strategy_value,
        side=side_value,
        entry_offset_bp=entry_bp,
        size_mult=notional_mult,
        notional_mult=notional_mult,
        requested_notional_usdc=STRONG_FALL_FOLLOW_NOTIONAL_USDC,
        reason=STRONG_FALL_FOLLOW_POLICY_TAG,
        regime=market_state,
        risk_tags=risk_tags,
        metrics=metrics,
        policy_tag=STRONG_FALL_FOLLOW_POLICY_TAG,
    )
def _stups_v143_shadow_block_decision(
    features: Mapping[str, Any],
    *,
    strategy: str,
    side: str,
    reason: str,
    condition: str,
    trigger_metrics: Mapping[str, Any],
    market_state: str,
    profile: Mapping[str, Any] | None = None,
) -> CodexV1Decision:
    profile = profile or {}
    metrics: dict[str, Any] = {
        "policy_note": reason,
        "policy_tag": reason,
        "shadow_lane": "SH_SHORT_STALE_UPMOVE_S1",
        "admitted_from_shadow_lane": "SH_SHORT_STALE_UPMOVE_S1",
        "condition": condition,
        "market_state": market_state,
        "v143_market_state": market_state,
        "live_action": "shadow_only",
        "source": "v143_strategy_market_profile_sweep",
        "profile_source": V143_PROFILE_SOURCE,
        "fixed_notional_usdc": STALE_UPMOVE_CANARY_NOTIONAL_USDC,
        "applied_notional_cap_usdc": 0.0,
        "would_live_notional_usdc": STALE_UPMOVE_CANARY_NOTIONAL_USDC,
    }
    for key in ("entry_bp", "tp1_bp", "full_tp_bp", "partial_exit_pct", "sl_bp", "be_bp", "ttl_s"):
        if key in profile:
            metrics[key] = profile[key]
    for key in (
        "replay_n",
        "replay_wr",
        "replay_net_usdc",
        "small_n_forward_watch",
        "no_fill_recovery_tp",
        "no_fill_recovery_loss",
        "no_fill_recovery_still_no_fill",
        "adaptive_tp_engine",
        "live_observation",
    ):
        if key in profile:
            metrics[key] = profile[key]
    for key, value in trigger_metrics.items():
        if value is None:
            continue
        try:
            metrics[key] = round(float(value), 4)
        except (TypeError, ValueError):
            metrics[key] = value

    state_tag = market_state.split(":", 1)[-1].replace("-", "_")
    risk_tags = tuple(
        dict.fromkeys(
            (
                "post_only_entry",
                "dca_disabled",
                *get_short_risk_tags(features),
                "stale_upmove_canary",
                "v143_stups_shadow_only",
                f"stups_state_{state_tag}",
                reason,
                "fixed_50_usdc",
            )
        )
    )
    return CodexV1Decision(
        accepted=False,
        version=CODEX_V1_VERSION,
        baseline=CODEX_V1_BASELINE,
        lane=STALE_UPMOVE_CANARY_LANE,
        lane_code=STALE_UPMOVE_CANARY_LANE_CODE,
        strategy=strategy,
        side=side,
        entry_offset_bp=None,
        size_mult=0.0,
        notional_mult=0.0,
        requested_notional_usdc=0.0,
        reason=reason,
        regime=market_state,
        risk_tags=risk_tags,
        metrics=metrics,
        policy_tag=reason,
        shadow_lane="SH_SHORT_STALE_UPMOVE_S1",
    )


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

    range_pos_15 = _feature_value(features, "range_pos_15")
    range_pos_30 = _feature_value(features, "range_pos_30")
    rsi = _feature_value(features, "rsi")
    wait_s = _feature_value(features, "reprice_wait_elapsed_seconds")
    pullback = _feature_value(features, "pullback_from_recent_high_bp")
    range_bp = _feature_value(features, "range_bp")
    vwap_dist_bp = _feature_value(features, "vwap_dist_bp")

    market_state = _stups_v143_market_state(features)
    profile = STUPS_V143_PROFILES.get(market_state)
    trigger_metrics = {
        "rng15": rng15,
        "adv3": adv3,
        "d30": d30,
        "range_pos_15": range_pos_15,
        "range_pos_30": range_pos_30,
        "rsi": rsi,
        "vwap_dist_bp": vwap_dist_bp,
        "reprice_wait_elapsed_seconds": wait_s,
        "pullback_from_recent_high_bp": pullback,
        "range_bp": range_bp,
    }
    if market_state in STUPS_V143_SHADOW_STATES or profile is None or bool(profile.get("shadow_only")):
        state_suffix = market_state.split(":", 1)[-1]
        return _stups_v143_shadow_block_decision(
            features,
            strategy=strategy_value,
            side=side_value,
            reason=f"v143_stups_{state_suffix}_shadow_only",
            condition=state_suffix,
            trigger_metrics=trigger_metrics,
            market_state=market_state,
            profile=profile,
        )


    if market_state == "STUP-S:clean_extension" and not _stups_v1420_clean_extension_good(features):
        return _stups_v143_shadow_block_decision(
            features,
            strategy=strategy_value,
            side=side_value,
            reason=STUPS_V1420_CLEAN_GATE_BLOCK_REASON,
            condition="clean_extension_rng15_le36_vwap_8_13p5_d30_ge10_pullback_ge27_or_rangepos_le0p80",
            trigger_metrics=trigger_metrics,
            market_state=market_state,
            profile=profile,
        )

    if market_state == "STUP-S:mixed" and _stups_v1420_mixed_bad(features):
        return _stups_v143_shadow_block_decision(
            features,
            strategy=strategy_value,
            side=side_value,
            reason=STUPS_V1420_MIXED_BAD_BLOCK_REASON,
            condition="mixed_d30_le_minus7_adv3_ge7_pullback_le23_rangepos_0p35_0p65",
            trigger_metrics=trigger_metrics,
            market_state=market_state,
            profile=profile,
        )

    if market_state == "STUP-S:mixed" and _stups_v1420_mixed_weakzone(features):
        return _stups_v143_shadow_block_decision(
            features,
            strategy=strategy_value,
            side=side_value,
            reason=STUPS_V1420_MIXED_WEAKZONE_BLOCK_REASON,
            condition="mixed_rng15_le50_rangepos_ge0p35_rangebp_le10",
            trigger_metrics=trigger_metrics,
            market_state=market_state,
            profile=profile,
        )

    if market_state == "STUP-S:weak_chop" and _stups_v1420_weak_chop_extreme(features):
        return _stups_v143_shadow_block_decision(
            features,
            strategy=strategy_value,
            side=side_value,
            reason=STUPS_V1420_WEAK_CHOP_EXTREME_BLOCK_REASON,
            condition="weak_chop_rangepos_ge0p90_or_rangebp_le1p5",
            trigger_metrics=trigger_metrics,
            market_state=market_state,
            profile=profile,
        )


    profile_entry_bp = float(profile["entry_bp"])
    low_rng_weak_adv_cautious_live = (
        market_state == "STUP-S:weak_chop"
        and rng15 <= STALE_UPMOVE_LOW_RNG_MAX_BP
        and adv3 < STALE_UPMOVE_WEAK_ADV3_MAX_BP
    )
    hot_clean_extension_entry_band = (
        str(profile.get("adaptive_tp_engine") or "") not in {"v1419_stups_runner", "v1420_stups_runner_after_clean_gate"}
        and market_state == "STUP-S:clean_extension"
        and rsi is not None
        and vwap_dist_bp is not None
        and pullback is not None
        and range_pos_15 is not None
        and adv3 is not None
        and d30 is not None
        and rsi >= STALE_UPMOVE_HOT_CLEAN_RSI_MIN
        and vwap_dist_bp >= STALE_UPMOVE_HOT_CLEAN_VWAP_MIN_BP
        and pullback >= STALE_UPMOVE_HOT_CLEAN_PULLBACK_MIN_BP
        and (
            range_pos_15 >= STALE_UPMOVE_HOT_CLEAN_RANGE_POS_MIN
            or adv3 >= STALE_UPMOVE_HOT_CLEAN_ADV3_MIN_BP
            or d30 >= STALE_UPMOVE_HOT_CLEAN_D30_MIN_BP
        )
    )
    entry_bp = profile_entry_bp
    if low_rng_weak_adv_cautious_live:
        entry_bp = max(entry_bp, STALE_UPMOVE_LOW_RNG_WEAK_ADV_ENTRY_BP)
    if hot_clean_extension_entry_band:
        entry_bp = max(entry_bp, STALE_UPMOVE_HOT_CLEAN_ENTRY_BP)
    tp1_bp = float(profile["tp1_bp"])
    full_tp_bp = float(profile.get("full_tp_bp", tp1_bp) or tp1_bp)
    sl_bp = float(profile["sl_bp"])
    be_bp = float(profile["be_bp"])
    partial_exit_pct = float(profile["partial_exit_pct"])
    ttl_s = int(profile["ttl_s"])
    if hot_clean_extension_entry_band:
        ttl_s = min(ttl_s, STALE_UPMOVE_HOT_CLEAN_TTL_S)
    notional_mult = STALE_UPMOVE_CANARY_NOTIONAL_USDC / BASE_NOTIONAL_USDC
    shadow_guards = [STALE_UPMOVE_SL19_SHADOW_TAG]
    state_tag = market_state.split(":", 1)[-1].replace("-", "_")
    base_tags = (
        "post_only_entry",
        "dca_disabled",
        *get_short_risk_tags(features),
        "stale_upmove_canary",
        "rng15_gt20",
        "rng15_le100",
        "adv3_gt0",
        "d30_le80",
        "canary_notional_50",
        STUPS_V143_PROFILE_POLICY_TAG,
        f"stups_state_{state_tag}",
        f"tp{tp1_bp:g}",
        f"fulltp{full_tp_bp:g}",
        f"sl{sl_bp:g}",
        f"be{be_bp:g}",
        f"ttl{ttl_s}s",
    )
    if profile.get("small_n_forward_watch"):
        base_tags = (*base_tags, "small_n_forward_watch")
    if low_rng_weak_adv_cautious_live:
        base_tags = (*base_tags, STALE_UPMOVE_LOW_RNG_WEAK_ADV_LIVE_TAG, f"entry{entry_bp:g}")
    if hot_clean_extension_entry_band:
        base_tags = (*base_tags, STALE_UPMOVE_HOT_CLEAN_ENTRY_TAG, f"entry{entry_bp:g}", f"ttl{ttl_s}s")
    risk_tags = tuple(dict.fromkeys(base_tags))
    metrics = {
        "policy_note": STUPS_V143_PROFILE_POLICY_TAG,
        "policy_tag": STUPS_V143_PROFILE_POLICY_TAG,
        "shadow_lane": "SH_SHORT_STALE_UPMOVE_S1",
        "admitted_from_shadow_lane": "SH_SHORT_STALE_UPMOVE_S1",
        "market_state": market_state,
        "v143_market_state": market_state,
        "live_action": "live_canary_adaptive_profile",
        "source": "v143_strategy_market_profile_sweep",
        "profile_source": V143_PROFILE_SOURCE,
        "rng15": round(float(rng15), 4),
        "rng15_min_bp": STALE_UPMOVE_CANARY_RNG15_MIN_BP,
        "rng15_max_bp": STALE_UPMOVE_CANARY_RNG15_MAX_BP,
        "adv3": round(float(adv3), 4),
        "adv3_min_bp": STALE_UPMOVE_CANARY_ADV3_MIN_BP,
        "d30": round(float(d30), 4),
        "d30_max_bp": STALE_UPMOVE_CANARY_D30_MAX_BP,
        "range_pos_15": round(float(range_pos_15), 4) if range_pos_15 is not None else None,
        "rsi": round(float(rsi), 4) if rsi is not None else None,
        "vwap_dist_bp": round(float(vwap_dist_bp), 4) if vwap_dist_bp is not None else None,
        "range_bp": round(float(range_bp), 4) if range_bp is not None else None,
        "pullback_from_recent_high_bp": round(float(pullback), 4) if pullback is not None else None,
        "reprice_wait_elapsed_seconds": round(float(wait_s), 4) if wait_s is not None else None,
        "admission_guard": "rng15_gt20_adv3_gt0_rng15_le100_d30_le80",
        "applied_notional_cap_usdc": STALE_UPMOVE_CANARY_NOTIONAL_USDC,
        "fixed_notional_usdc": STALE_UPMOVE_CANARY_NOTIONAL_USDC,
        "entry_bp": entry_bp,
        "tp1_bp": tp1_bp,
        "full_tp_bp": full_tp_bp,
        "partial_exit_pct": partial_exit_pct,
        "sl_bp": sl_bp,
        "be_bp": be_bp,
        "ttl_s": ttl_s,
        "tp_policy_override": f"tp{tp1_bp:g}_profile",
        "sl_policy": f"hard_sl_{sl_bp:g}bp_profile",
        "be_policy": f"post_tp_be_{be_bp:g}bp" if be_bp > 0 else "be_disabled_for_state",
        "ttl_policy": f"entry_ttl_{ttl_s}s_profile",
        "shadow_guards": shadow_guards,
        "replay_report": V143_PROFILE_SOURCE,
    }
    for key in (
        "replay_n",
        "replay_wr",
        "replay_net_usdc",
        "small_n_forward_watch",
        "no_fill_recovery_tp",
        "no_fill_recovery_loss",
        "no_fill_recovery_still_no_fill",
        "adaptive_tp_engine",
        "live_observation",
    ):
        if key in profile:
            metrics[key] = profile[key]
    if low_rng_weak_adv_cautious_live:
        metrics.update(
            {
                STALE_UPMOVE_LOW_RNG_WEAK_ADV_LIVE_TAG: True,
                "profile_patch": STALE_UPMOVE_LOW_RNG_WEAK_ADV_LIVE_TAG,
                "low_rng_weak_adv_action": "live_cautious_maker",
                "low_rng_weak_adv_condition": "weak_chop_rng15_le30_adv3_lt3",
                "low_rng_weak_adv_profile_entry_bp": profile_entry_bp,
                "low_rng_weak_adv_entry_bp": entry_bp,
                "low_rng_weak_adv_rng15_max_bp": STALE_UPMOVE_LOW_RNG_MAX_BP,
                "low_rng_weak_adv_adv3_max_bp": STALE_UPMOVE_WEAK_ADV3_MAX_BP,
            }
        )
    if hot_clean_extension_entry_band:
        metrics.update(
            {
                STALE_UPMOVE_HOT_CLEAN_ENTRY_TAG: True,
                "profile_patch": STALE_UPMOVE_HOT_CLEAN_ENTRY_TAG,
                "hot_clean_entry_action": "bounded_deep_maker",
                "hot_clean_entry_condition": "clean_extension_rsi_ge62_vwap_ge8_pullback_ge30_and_rangepos_ge0p9_or_adv3_ge10_or_d30_ge30",
                "hot_clean_profile_entry_bp": profile_entry_bp,
                "hot_clean_entry_bp": entry_bp,
                "hot_clean_ttl_s": ttl_s,
                "hot_clean_rsi_min": STALE_UPMOVE_HOT_CLEAN_RSI_MIN,
                "hot_clean_vwap_min_bp": STALE_UPMOVE_HOT_CLEAN_VWAP_MIN_BP,
                "hot_clean_pullback_min_bp": STALE_UPMOVE_HOT_CLEAN_PULLBACK_MIN_BP,
                "hot_clean_range_pos_min": STALE_UPMOVE_HOT_CLEAN_RANGE_POS_MIN,
                "hot_clean_adv3_min_bp": STALE_UPMOVE_HOT_CLEAN_ADV3_MIN_BP,
                "hot_clean_d30_min_bp": STALE_UPMOVE_HOT_CLEAN_D30_MIN_BP,
                "hot_clean_observed_rsi": rsi,
                "hot_clean_observed_vwap_dist_bp": vwap_dist_bp,
                "hot_clean_observed_pullback_from_recent_high_bp": pullback,
                "hot_clean_observed_range_pos_15": range_pos_15,
                "hot_clean_observed_adv3": adv3,
                "hot_clean_observed_d30": d30,
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
        entry_offset_bp=entry_bp,
        size_mult=notional_mult,
        notional_mult=notional_mult,
        requested_notional_usdc=STALE_UPMOVE_CANARY_NOTIONAL_USDC,
        reason=STUPS_V143_PROFILE_POLICY_TAG,
        regime=market_state,
        risk_tags=risk_tags,
        metrics=metrics,
        policy_tag=STUPS_V143_PROFILE_POLICY_TAG,
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


def _s1p_l_v149_metrics(features: Mapping[str, Any], notional_mult: float) -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "policy_note": S1P_L_V149_PROFILE_POLICY_TAG,
        "policy_tag": S1P_L_V149_PROFILE_POLICY_TAG,
        "market_state": S1P_L_V149_MARKET_STATE,
        "v143_market_state": S1P_L_V149_MARKET_STATE,
        "live_action": "live_tiny_profile",
        "source": "v149_s1pl_hotfix",
        "profile_source": S1P_L_V149_PROFILE_SOURCE,
        "fixed_notional_usdc": S1P_L_V149_NOTIONAL_USDC,
        "applied_notional_cap_usdc": S1P_L_V149_NOTIONAL_USDC,
        "would_live_notional_usdc": S1P_L_V149_NOTIONAL_USDC,
        "entry_model": "post_only_maker_0bp",
        "base_notional_mult": round(float(notional_mult), 4),
    }
    metrics.update(S1P_L_V149_PROFILE)
    for key in (
        "score",
        "rng15",
        "d30",
        "adv3",
        "d3",
        "d5",
        "rsi",
        "bb_lower_dist_bp",
        "vwap_dist_bp",
        "pullback_from_recent_high_bp",
        "price_above_or_reclaimed_vwap",
        "reprice_wait_elapsed_seconds",
    ):
        value = _feature_value(features, key)
        if value is not None:
            metrics[key] = round(float(value), 4)
    return metrics


def build_s1p_l_enter_decision(features: Mapping[str, Any]) -> CodexV1Decision:
    lane = _s1p_l_lane()
    _matched, missing = _lane_matches(lane, features)
    size_mult = _size_mult(lane, features)
    notional_mult = _notional_mult(lane, features, size_mult)
    metrics = _s1p_l_v149_metrics(features, notional_mult)
    risk_tags = tuple(
        dict.fromkeys(
            (
                *_risk_tags(lane, size_mult),
                S1P_L_V149_PROFILE_POLICY_TAG,
                "s1p_l_tiny_notional_25",
                "tp6",
                "sl15",
                "be0",
                "full_tp1",
                "ttl180s",
                "small_n_forward_watch",
            )
        )
    )
    return CodexV1Decision(
        accepted=True,
        version=CODEX_V1_VERSION,
        baseline=CODEX_V1_BASELINE,
        lane=lane.name,
        lane_code=lane.lane_code,
        strategy=_string_feature(features, "strategy"),
        side=_string_feature(features, "side"),
        entry_offset_bp=float(S1P_L_V149_PROFILE["entry_bp"]),
        size_mult=size_mult,
        notional_mult=notional_mult,
        requested_notional_usdc=S1P_L_V149_NOTIONAL_USDC,
        reason="s1p_l_match",
        regime="ordinary_pullback_long_pre_vwap",
        missing_features=tuple(sorted(missing)),
        risk_tags=risk_tags,
        metrics=metrics,
        policy_tag=S1P_L_V149_PROFILE_POLICY_TAG,
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
        "slope30",
        "slope60",
        "slope120",
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
    slope_proxy_applied = False
    if i >= 1:
        slope60 = _ret_bp(candles, i, 1)
        if "slope60" not in features and "slope60_bp" not in features:
            features["slope60"] = slope60
            slope_proxy_applied = True
        if "slope30" not in features and "slope30_bp" not in features:
            features["slope30"] = slope60 * 0.5
            slope_proxy_applied = True
    if i >= 2 and "slope120" not in features and "slope120_bp" not in features:
        features["slope120"] = _ret_bp(candles, i, 2)
        slope_proxy_applied = True
    if slope_proxy_applied:
        features.setdefault("v1421_slope_source", "candle_close_proxy")
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
