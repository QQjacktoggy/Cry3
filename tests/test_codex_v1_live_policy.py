import json
from pathlib import Path
import re

import pytest

from src.gridbot.strategy.codex_v1427_tree import V1427_FIVE_WINDOW_TREE

from src.gridbot.strategy.codex_v1_live import (
    CODEX_V1_VERSION,
    V1423_CONSERVATIVE_TREE,
    V1427_FDT_RNG90_BLOCK_TAG,
    V1427_POLICY_TAG,
    V1427_W1D_BLOCK_TAG,
    V1429_STUPS_STALE_SIDE_OVERRIDE_BLOCK_TAG,
    V1433_STUPS_CLEAN_HIGH_OVERRIDE_BLOCK_TAG,
    V1430_BLOCK_TAG,
    V1430_MISSING_FEATURE_BLOCK_TAG,
    V1430_POLICY_TAG,
    V1436_FAST_RECLAIM_DOWNSLOPE_BLOCK_TAG,
    V1449_CNL_WPR_FALLING_TRAP_QUALITY_BLOCK_TAG,
    V1449_CNL_WPR_FAST_RECLAIM_QUALITY_BLOCK_TAG,
    V1450_CNL_WPR_DEEP_LATE_CHASE_BLOCK_TAG,
    V1451_STUPS_CLEAN_EXTENSION_SHADOW_REVIEW_BLOCK_TAG,
    V1452_STUPS_LATE_ADVERSE_REOPEN_BLOCK_TAG,
    V1453_STUPS_CLEAN_EXTENSION_REOPEN_REVIEW_BLOCK_TAG,
    V1455_STUPS_CLEAN_EXTENSION_TP14_BLOCK_TAG,
    V1428_LEGACY_STUPS_REOPEN_REASONS,
    CodexV1Decision,
    apply_v1421_adaptive_decision,
    apply_v1423_conservative_decision,
    apply_v1427_five_window_decision,
    apply_v1430_loss_prune_decision,
    apply_v1436_live_hotfix_decision,
    _v1427_profile_from_action,
    build_stale_upmove_canary_decision,
    build_codex_v1_live_features,
    classify_codex_v133_no_lane_candidate,
    codex_v1_feature_gaps,
    format_codex_v1_signal_overview,
    format_codex_v1_telegram_report,
    lane_code_from_name,
    live_preflight_rejections,
    select_codex_v1_lane,
)


def test_codex_v1_version_is_pinned():
    assert CODEX_V1_VERSION == "_codex_v1.4.69"



def _compact_report_tree(node):
    if "action" in node:
        return node["action"]
    return (
        tuple(node["split"]),
        _compact_report_tree(node["left"]),
        _compact_report_tree(node["right"]),
    )


def test_v1423_embedded_tree_matches_target_1p4_report():
    report_path = Path(__file__).resolve().parents[1] / "reports" / "v1423_four_window_conservative_tree_target1p4_summary.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["target_net_50"] == 1.4
    assert report["fourth_projected_50_net"] == 1.4849
    assert report["pass_all_windows"] is True
    assert _compact_report_tree(report["tree"]) == V1423_CONSERVATIVE_TREE


def test_v1427_embedded_tree_matches_five_window_report():
    report_path = Path(__file__).resolve().parents[1] / "reports" / "v1427_five_window_compact_tree_leaf2_tp14_2026-07-01.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["top"][0]["min_leaf"] == 2
    assert report["top"][0]["depth"] == 8
    assert report["top"][0]["tree"] == V1427_FIVE_WINDOW_TREE


def _v1427_complete_features(**overrides):
    features = {
        "symbol": "ETHUSDC",
        "strategy": "S1_BB_RSI",
        "side": "LONG",
        "score": 71.0,
        "rng15": 28.0,
        "d30": -8.0,
        "adv3": 0.0,
        "rsi": 48.0,
        "vwap_dist_bp": -6.0,
        "range_pos_15": 0.45,
        "pullback_from_recent_high_bp": 18.0,
        "slope30": -1.0,
        "slope60": -2.0,
        "slope120": -3.0,
        "v1421_slope_source": "unit_test_v1427",
    }
    features.update(overrides)
    return features


def test_v1427_blocks_w1d_unvalidated_live_lane():
    baseline = CodexV1Decision(
        accepted=True,
        version=CODEX_V1_VERSION,
        baseline="test",
        lane="w1_lane_s1long_d30neg25_15_vwap4_60_advmax15_e0",
        lane_code="W1D",
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="unit_test_w1d",
        metrics={"policy_tag": "unit_test_w1d"},
        policy_tag="unit_test_w1d",
    )

    decision = apply_v1427_five_window_decision(_v1427_complete_features(), baseline)

    assert decision.accepted is False
    assert decision.reason == V1427_W1D_BLOCK_TAG
    assert decision.requested_notional_usdc == 0.0
    assert decision.metrics["v1427_w1d_blocked"] is True
    assert decision.shadow_lane == "SH_V1427_W1D_BLOCK"


def test_v1427_fdt_rng90_overlay_blocks_submit():
    baseline = _cnl_wpr_baseline("CNL-WPR-L:falling_discount_trap", side="LONG")
    features = _v1427_complete_features(
        rng15=92.0,
        d30=-55.0,
        adv3=-4.0,
        rsi=28.0,
        vwap_dist_bp=-45.0,
        range_pos_15=0.16,
        pullback_from_recent_high_bp=60.0,
        slope30=-4.0,
        slope60=-6.0,
        slope120=-12.0,
    )

    decision = apply_v1427_five_window_decision(features, baseline)

    assert decision.accepted is False
    assert decision.reason == V1427_FDT_RNG90_BLOCK_TAG
    assert decision.metrics["v1427_overlay"] == "fdt_rng90_block"
    assert decision.metrics["v1427_overlay_action"] == "BLOCK"
    assert decision.metrics["v1427_overlay_rng15_threshold_bp"] == 90.0


def test_v1427_action_parser_preserves_time_lock_profile():
    profile = _v1427_profile_from_action("S_E2_TP14_SL8_T90_LOCK90_6_0")

    assert profile is not None
    assert profile["side"] == "SHORT"
    assert profile["entry_bp"] == 2.0
    assert profile["tp1_bp"] == 14.0
    assert profile["full_tp_bp"] == 14.0
    assert profile["sl_bp"] == 8.0
    assert profile["ttl_s"] == 90
    assert profile["partial_exit_pct"] == 1.0
    assert profile["be_bp"] == 0.0
    assert profile["time_profit_lock_enabled"] is True
    assert profile["time_lock_s"] == 90
    assert profile["time_lock_min_bp"] == 6.0
    assert profile["time_lock_slope_max_bp"] == 0.0


def test_v1427_profile_application_sets_tp14_full_exit_and_time_lock():
    baseline = _cnl_wpr_baseline("CNL-WPR-L:discount_mixed", side="LONG")
    features = _v1427_complete_features(
        rng15=40.0,
        d30=12.0,
        adv3=2.0,
        rsi=50.0,
        vwap_dist_bp=-10.0,
        range_pos_15=0.50,
        pullback_from_recent_high_bp=20.0,
        slope30=-4.0,
        slope60=-7.0,
        slope120=-4.0,
    )

    decision = apply_v1427_five_window_decision(features, baseline)

    assert decision.accepted is True
    assert decision.policy_tag == V1427_POLICY_TAG
    assert decision.metrics["v1427_action"].startswith(("L_", "S_"))
    assert decision.metrics["tp1_bp"] <= 14.0
    assert decision.metrics["full_tp_bp"] == decision.metrics["tp1_bp"]
    assert decision.metrics["partial_exit_pct"] == 1.0
    if "LOCK" in decision.metrics["v1427_action"]:
        assert decision.metrics["time_profit_lock_enabled"] is True
        assert decision.metrics["time_lock_reason"] == "CODEX_V1427_TIME_LOCK"


def test_v1428_reopens_legacy_stups_reject_when_tree_has_profile():
    baseline = CodexV1Decision(
        accepted=False,
        version=CODEX_V1_VERSION,
        baseline="test",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=None,
        size_mult=0.0,
        notional_mult=0.0,
        requested_notional_usdc=0.0,
        reason="v1420_stups_mixed_bad_block",
        regime="STUP-S:mixed",
        risk_tags=("legacy_stups_block",),
        metrics={"market_state": "STUP-S:mixed", "policy_tag": "v1420_stups_mixed_bad_block"},
        policy_tag="v1420_stups_mixed_bad_block",
        shadow_lane="SH_SHORT_STALE_UPMOVE_S1",
    )
    features = _v1427_complete_features(
        side="SHORT",
        score=82.0,
        rng15=23.580582,
        d30=-12.706962,
        adv3=10.315281,
        rsi=49.151435,
        vwap_dist_bp=61.383877,
        range_pos_15=0.509383,
        pullback_from_recent_high_bp=12.011556,
        slope30=-1.106083,
        slope60=-2.212166,
        slope120=4.490545,
        v1421_slope_source="candle_close_proxy",
    )

    decision = apply_v1427_five_window_decision(features, baseline)

    assert decision.accepted is True
    assert decision.policy_tag == V1427_POLICY_TAG
    assert decision.requested_notional_usdc == 50.0
    assert decision.metrics["v1428_legacy_reopen"] is True
    assert decision.metrics["v1428_legacy_reopen_reason"] == "v1420_stups_mixed_bad_block"
    assert decision.metrics["v1427_action"] == "S_E2_TP12_SL8_T90_LOCK90_6_0"
    assert decision.side == "SHORT"
    assert decision.entry_offset_bp == 2.0
    assert decision.metrics["tp1_bp"] == 12.0
    assert "v1428_legacy_stups_reopen_for_tree_profile" in decision.risk_tags



def _stups_clean_extension_legacy_reject():
    return CodexV1Decision(
        accepted=False,
        version=CODEX_V1_VERSION,
        baseline="test",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=None,
        size_mult=0.0,
        notional_mult=0.0,
        requested_notional_usdc=0.0,
        reason="v1420_stups_clean_extension_gate_block",
        regime="STUP-S:clean_extension",
        risk_tags=("legacy_stups_block",),
        metrics={"market_state": "STUP-S:clean_extension", "policy_tag": "v1420_stups_clean_extension_gate_block"},
        policy_tag="v1420_stups_clean_extension_gate_block",
        shadow_lane="SH_SHORT_STALE_UPMOVE_S1",
    )


def test_v1452_blocks_stups_late_adverse_clean_extension_reopen():
    features = _v1427_complete_features(
        side="SHORT",
        score=78.0,
        rng15=22.503118,
        d30=16.948192,
        adv3=8.169008,
        d3=8.169008,
        d5=9.304648,
        rsi=63.913315,
        vwap_dist_bp=13.409482,
        range_pos_15=0.95466,
        pullback_from_recent_high_bp=17.061558,
        slope30=-0.510094,
        slope60=-1.020188,
        slope120=7.033625,
        reprice_wait_elapsed_seconds=360.0,
        reprice_favorable_bp=0.454,
        reprice_adverse_bp=6.8674,
        v1421_slope_source="candle_close_proxy",
    )

    decision = apply_v1427_five_window_decision(features, _stups_clean_extension_legacy_reject())

    assert decision.accepted is False
    assert decision.reason == V1452_STUPS_LATE_ADVERSE_REOPEN_BLOCK_TAG
    assert decision.shadow_lane == "SH_V1452_STUPS_LATE_ADVERSE_REOPEN"
    assert decision.metrics["v1452_wait_s"] == 360.0
    assert decision.metrics["v1452_adverse_bp"] == 6.8674
    assert decision.metrics["v1452_favorable_bp"] == 0.454


def test_v1452_allows_fresh_favorable_clean_extension_reopen():
    features = _v1427_complete_features(
        side="SHORT",
        score=78.0,
        rng15=22.503118,
        d30=16.948192,
        adv3=8.169008,
        d3=8.169008,
        d5=9.304648,
        rsi=63.913315,
        vwap_dist_bp=13.409482,
        range_pos_15=0.95466,
        pullback_from_recent_high_bp=17.061558,
        slope30=-0.510094,
        slope60=-1.020188,
        slope120=7.033625,
        reprice_wait_elapsed_seconds=120.0,
        reprice_favorable_bp=4.5,
        reprice_adverse_bp=1.0,
        v1421_slope_source="candle_close_proxy",
    )

    decision = apply_v1427_five_window_decision(features, _stups_clean_extension_legacy_reject())

    assert decision.accepted is True
    assert decision.policy_tag == V1427_POLICY_TAG
    assert decision.metrics["v1428_legacy_reopen"] is True



def test_v1453_blocks_stups_clean_extension_rng_shadow_block_reopen():
    features = _v1427_complete_features(
        side="SHORT",
        score=81.0,
        rng15=29.4659,
        d30=13.3163,
        adv3=1.2468,
        rsi=61.6396,
        vwap_dist_bp=13.5094,
        range_pos_15=0.8173,
        pullback_from_recent_high_bp=24.0827,
        slope30=-1.1614,
        slope60=-2.3228,
        slope120=2.0,
        reprice_wait_elapsed_seconds=110.0,
        reprice_favorable_bp=0.2,
        reprice_adverse_bp=2.0,
        v1421_slope_source="candle_close_proxy",
    )

    decision = apply_v1427_five_window_decision(features, _stups_clean_extension_legacy_reject())

    assert decision.accepted is False
    assert decision.reason == V1453_STUPS_CLEAN_EXTENSION_REOPEN_REVIEW_BLOCK_TAG
    assert decision.shadow_lane == "SH_V1453_STUPS_CLEAN_EXTENSION_REOPEN_REVIEW"
    assert decision.metrics["v1453_block_reason"] == "rng15_shadow_block_candidate"
    assert decision.metrics["v1453_rng15"] == 29.4659


def test_v1453_blocks_stups_clean_extension_high_rsi_vwap_late_reopen():
    features = _v1427_complete_features(
        side="SHORT",
        score=82.0,
        rng15=21.3306,
        d30=24.8386,
        adv3=3.3393,
        rsi=66.9136,
        vwap_dist_bp=24.3989,
        range_pos_15=0.8886,
        pullback_from_recent_high_bp=18.9543,
        slope30=-0.5092,
        slope60=-1.0184,
        slope120=4.0,
        reprice_wait_elapsed_seconds=429.8,
        reprice_favorable_bp=5.3824,
        reprice_adverse_bp=2.5496,
        v1421_slope_source="candle_close_proxy",
    )

    decision = apply_v1427_five_window_decision(features, _stups_clean_extension_legacy_reject())

    assert decision.accepted is False
    assert decision.reason == V1453_STUPS_CLEAN_EXTENSION_REOPEN_REVIEW_BLOCK_TAG
    assert decision.shadow_lane == "SH_V1453_STUPS_CLEAN_EXTENSION_REOPEN_REVIEW"
    assert decision.metrics["v1453_block_reason"] == "high_rsi_vwap_late_review"
    assert decision.metrics["v1453_rsi"] == 66.9136
    assert decision.metrics["v1453_vwap_dist_bp"] == 24.3989
    assert decision.metrics["v1453_wait_s"] == 429.8


def test_v1455_blocks_stups_clean_extension_tp14_route():
    features = _v1427_complete_features(
        side="SHORT",
        rng15=20.0,
        d30=-60.0,
        adv3=-8.0,
        rsi=30.0,
        vwap_dist_bp=-50.0,
        range_pos_15=0.2,
        pullback_from_recent_high_bp=5.0,
        slope30=-8.0,
        slope60=4.0,
        slope120=-8.0,
    )

    decision = apply_v1427_five_window_decision(features, _stups_baseline("STUP-S:clean_extension", side="SHORT"))

    assert decision.accepted is False
    assert decision.reason == V1455_STUPS_CLEAN_EXTENSION_TP14_BLOCK_TAG
    assert decision.requested_notional_usdc == 0.0
    assert decision.metrics["v1455_route"] == "BLOCK"
    assert decision.metrics["v1455_route_reason"] == "stups_clean_extension_tp14_loss_guard"
    assert decision.metrics["v1455_action"] == "L_E0_TP14_SL8_T90_LOCK60_6_0"
    assert decision.metrics["v1455_action_tp_bp"] == 14.0
    assert decision.shadow_lane == "SH_V1455_STUPS_CLEAN_EXTENSION_TP14_BLOCK"


@pytest.mark.parametrize(
    ("expected_tp", "expected_action", "overrides"),
    [
        (
            8.0,
            "L_E0_TP8_SL6_T60_LOCK60_5_0",
            {
                "rng15": 20.0,
                "d30": -60.0,
                "adv3": -8.0,
                "rsi": 30.0,
                "vwap_dist_bp": -50.0,
                "range_pos_15": 0.2,
                "pullback_from_recent_high_bp": 5.0,
                "slope30": -8.0,
                "slope60": -8.0,
                "slope120": -8.0,
            },
        ),
        (
            10.0,
            "S_E2_TP10_SL8_T90_LOCK90_6_0",
            {
                "rng15": 20.0,
                "d30": -60.0,
                "adv3": -8.0,
                "rsi": 30.0,
                "vwap_dist_bp": -5.0,
                "range_pos_15": 0.8,
                "pullback_from_recent_high_bp": 5.0,
                "slope30": -8.0,
                "slope60": -8.0,
                "slope120": 8.0,
            },
        ),
    ],
)
def test_v1455_keeps_stups_clean_extension_tp8_tp10_thin_scalp(expected_tp, expected_action, overrides):
    features = _v1427_complete_features(side="SHORT", **overrides)

    decision = apply_v1427_five_window_decision(features, _stups_baseline("STUP-S:clean_extension", side="SHORT"))

    assert decision.accepted is True
    assert decision.reason == V1427_POLICY_TAG
    assert decision.metrics["v1455_route"] == "THIN_SCALP"
    assert decision.metrics["v1455_route_reason"] == "stups_clean_extension_tp8_tp10_gate_pass"
    assert decision.metrics["v1455_action"] == expected_action
    assert decision.metrics["v1455_action_tp_bp"] == expected_tp
def test_v1429_blocks_stale_stups_side_override_reopen():
    baseline = CodexV1Decision(
        accepted=False,
        version=CODEX_V1_VERSION,
        baseline="test",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=None,
        size_mult=0.0,
        notional_mult=0.0,
        requested_notional_usdc=0.0,
        reason="v1420_stups_mixed_weakzone_block",
        regime="STUP-S:mixed",
        risk_tags=("legacy_stups_block",),
        metrics={"market_state": "STUP-S:mixed", "policy_tag": "v1420_stups_mixed_weakzone_block"},
        policy_tag="v1420_stups_mixed_weakzone_block",
        shadow_lane="SH_SHORT_STALE_UPMOVE_S1",
    )
    features = _v1427_complete_features(
        side="SHORT",
        score=78.0,
        rng15=20.162932,
        d30=29.806073,
        adv3=1.18988,
        rsi=56.572487,
        vwap_dist_bp=113.472949,
        range_pos_15=0.381988,
        pullback_from_recent_high_bp=7.701989,
        slope30=0.688891,
        slope60=1.377781,
        slope120=4.071814,
        reprice_wait_elapsed_seconds=990.0,
        v1421_slope_source="candle_close_proxy",
    )

    decision = apply_v1427_five_window_decision(features, baseline)

    assert decision.accepted is False
    assert decision.reason == V1429_STUPS_STALE_SIDE_OVERRIDE_BLOCK_TAG
    assert decision.shadow_lane == "SH_V1429_STUPS_STALE_SIDE_OVERRIDE"
    assert decision.metrics["v1427_action"] == "L_E2_TP14_SL8_T90_LOCK90_6_0"
    assert decision.metrics["v1429_previous_side"] == "SHORT"
    assert decision.metrics["v1429_target_side"] == "LONG"
    assert decision.metrics["v1429_wait_s"] == 990.0


def test_v1429_allows_fresh_stups_side_override_for_fast_lock_manager():
    baseline = CodexV1Decision(
        accepted=False,
        version=CODEX_V1_VERSION,
        baseline="test",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=None,
        size_mult=0.0,
        notional_mult=0.0,
        requested_notional_usdc=0.0,
        reason="v143_stups_counter_recoil_shadow_only",
        regime="STUP-S:counter_recoil",
        risk_tags=("legacy_stups_block",),
        metrics={"market_state": "STUP-S:counter_recoil", "policy_tag": "v143_stups_counter_recoil_shadow_only"},
        policy_tag="v143_stups_counter_recoil_shadow_only",
        shadow_lane="SH_SHORT_STALE_UPMOVE_S1",
    )
    features = _v1427_complete_features(
        side="SHORT",
        score=78.0,
        rng15=26.484081,
        d30=-27.927526,
        adv3=6.27979,
        rsi=44.588525,
        vwap_dist_bp=84.733912,
        range_pos_15=0.433649,
        pullback_from_recent_high_bp=11.484803,
        slope30=0.219664,
        slope60=0.439329,
        slope120=7.913827,
        reprice_wait_elapsed_seconds=240.0,
        v1421_slope_source="candle_close_proxy",
    )

    decision = apply_v1427_five_window_decision(features, baseline)

    assert decision.accepted is True
    assert decision.policy_tag == V1427_POLICY_TAG
    assert decision.side == "LONG"
    assert decision.metrics["v1427_action"] == "L_E0_TP14_SL8_T90_LOCK60_6_0"
    assert decision.metrics["v1427_previous_side"] == "SHORT"
    assert decision.metrics["target_side"] == "LONG"
def _stups_v143_features(**overrides):
    features = {
        "symbol": "ETHUSDC",
        "strategy": "S1_BB_RSI",
        "side": "SHORT",
        "score": 70.0,
        "rng15": 40.0,
        "d30": 10.0,
        "adv3": 1.0,
        "range_bp": 2.0,
        "rsi": 64.0,
        "vwap_dist_bp": 6.0,
        "range_pos_15": 0.8,
        "pullback_from_recent_high_bp": 18.0,
        "reprice_wait_elapsed_seconds": 120.0,
    }
    features.update(overrides)
    return features


def test_v1421_tree_missing_slope_keeps_baseline_decision():
    baseline = build_stale_upmove_canary_decision(
        _stups_v143_features(rng15=55.0, range_bp=12.0)
    )

    decision = apply_v1421_adaptive_decision(_stups_v143_features(rng15=55.0, range_bp=12.0), baseline)

    assert decision is baseline
    assert decision.policy_tag == "v1420_stups_fixed_regime_exec"
    assert "v1421_action" not in (decision.metrics or {})


def test_v1421_tree_applies_short_runner_e6_profile_when_slopes_present():
    features = _stups_v143_features(
        rng15=25.0,
        d30=0.0,
        adv3=3.0,
        rsi=55.0,
        vwap_dist_bp=6.0,
        range_bp=12.0,
        range_pos_15=0.50,
        pullback_from_recent_high_bp=15.0,
        slope30=0.0,
        slope60=0.0,
        slope120=0.0,
        v1421_slope_source="unit_test",
    )
    baseline = build_stale_upmove_canary_decision(features)

    decision = apply_v1421_adaptive_decision(features, baseline)

    assert decision.accepted
    assert decision.lane_code == "STUP-S"
    assert decision.side == "SHORT"
    assert decision.reason == "v1421_decision_tree_adaptive_exec"
    assert decision.policy_tag == "v1421_decision_tree_adaptive_exec"
    assert decision.metrics["v1421_action"] == "SHORT_RUNNER_E6"
    assert decision.metrics["entry_bp"] == 6.0
    assert decision.metrics["tp1_bp"] == 30.0
    assert decision.metrics["full_tp_bp"] == 100.0
    assert decision.metrics["sl_bp"] == 12.0
    assert decision.metrics["partial_exit_pct"] == 0.3
    assert decision.metrics["ttl_s"] == 180
    assert decision.metrics["v1421_slope_source"] == "unit_test"
    assert "v1421_action_short_runner_e6" in decision.risk_tags


def test_v1421_tree_can_flip_cnl_long_to_short_wide_profile():
    features = {
        "symbol": "ETHUSDC",
        "strategy": "S1_BB_RSI",
        "side": "LONG",
        "rng15": 40.0,
        "d30": 0.0,
        "adv3": 2.0,
        "rsi": 55.0,
        "vwap_dist_bp": -20.0,
        "range_pos_15": 0.70,
        "pullback_from_recent_high_bp": 20.0,
        "slope30": 0.0,
        "slope60": 5.0,
        "slope120": 0.0,
    }
    baseline = CodexV1Decision(
        accepted=True,
        version=CODEX_V1_VERSION,
        baseline="test",
        lane="v139_canary_watch_pre_reprice_long_s1",
        lane_code="CNL-WPR-L",
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=3.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v145_wpr_profit_lock_exec",
        metrics={"market_state": "CNL-WPR-L:discount_mixed", "policy_tag": "v145_wpr_profit_lock_exec"},
        policy_tag="v145_wpr_profit_lock_exec",
    )

    decision = apply_v1421_adaptive_decision(features, baseline)

    assert decision.accepted
    assert decision.lane_code == "CNL-WPR-L"
    assert decision.side == "SHORT"
    assert decision.entry_offset_bp == 8.0
    assert decision.policy_tag == "v1421_decision_tree_adaptive_exec"
    assert decision.metrics["v1421_action"] == "SHORT_WIDE_E8"
    assert decision.metrics["target_side"] == "SHORT"
    assert decision.metrics["tp1_bp"] == 60.0
    assert decision.metrics["full_tp_bp"] == 120.0
    assert decision.metrics["sl_bp"] == 15.0
    assert decision.metrics["partial_exit_pct"] == 0.2
    assert "v1421_side_override_short" in decision.risk_tags

def test_v1421_tree_skips_w6a_even_with_slopes_present():
    baseline = CodexV1Decision(
        accepted=True,
        version=CODEX_V1_VERSION,
        baseline="test",
        lane="w6_lane_s1long_rng38_86_range9_15_e0",
        lane_code="W6A",
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=200.0,
        reason="accepted",
        metrics={"policy_tag": "v137_w6a_200_promo_keep"},
        policy_tag="v137_w6a_200_promo_keep",
    )
    features = {
        "rng15": 40.0,
        "d30": -20.0,
        "adv3": 2.0,
        "rsi": 45.0,
        "vwap_dist_bp": -10.0,
        "range_pos_15": 0.5,
        "pullback_from_recent_high_bp": 15.0,
        "slope30": 2.0,
        "slope60": 5.0,
        "slope120": 3.0,
    }

    decision = apply_v1421_adaptive_decision(features, baseline)

    assert decision is baseline
    assert decision.lane_code == "W6A"
    assert decision.policy_tag == "v137_w6a_200_promo_keep"
    assert "v1421_action" not in (decision.metrics or {})

def test_v1421_tree_skips_s1p_l_even_with_slopes_present():
    baseline = CodexV1Decision(
        accepted=True,
        version=CODEX_V1_VERSION,
        baseline="test",
        lane="codex_v1_s1_bbrsi_ordinary_pullback_long_pre_vwap",
        lane_code="S1P-L",
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=0.0,
        size_mult=0.2,
        notional_mult=0.2,
        requested_notional_usdc=25.0,
        reason="s1p_l_match",
        metrics={"market_state": "S1P-L:ordinary_pullback_pre_vwap", "policy_tag": "v149_s1pl_tiny_profile_fix"},
        policy_tag="v149_s1pl_tiny_profile_fix",
    )
    features = {
        "slope30": 0.0,
        "slope60": 5.0,
        "slope120": 0.0,
        "rng15": 40.0,
        "d30": 0.0,
        "adv3": 2.0,
        "rsi": 55.0,
        "vwap": -20.0,
        "range_pos": 0.7,
        "pullback": 20.0,
    }

    decision = apply_v1421_adaptive_decision(features, baseline)

    assert decision is baseline
    assert decision.lane_code == "S1P-L"
    assert decision.side == "LONG"
    assert decision.policy_tag == "v149_s1pl_tiny_profile_fix"
    assert "v1421_action" not in (decision.metrics or {})


def test_v1421_tree_skips_uncontrolled_lane_even_with_slopes_present():
    baseline = CodexV1Decision(
        accepted=True,
        version=CODEX_V1_VERSION,
        baseline="test",
        lane="w6_lane_s6long_clusterB_vwap45_rp5_03_rp15_07_close08",
        lane_code="W6B",
        strategy="S6_LOW_VOL_VWAP",
        side="LONG",
        entry_offset_bp=1.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="w6b_match",
        metrics={"policy_tag": "w6b_existing_policy"},
        policy_tag="w6b_existing_policy",
    )
    features = {
        "slope30": -5.0,
        "slope60": -10.0,
        "slope120": -20.0,
        "rng15": 55.0,
        "d30": -30.0,
        "adv3": 3.0,
        "rsi": 38.0,
        "vwap": -45.0,
        "range_pos": 0.25,
        "pullback": 35.0,
    }

    decision = apply_v1421_adaptive_decision(features, baseline)

    assert decision is baseline
    assert decision.lane_code == "W6B"
    assert decision.policy_tag == "w6b_existing_policy"
    assert "v1421_action" not in (decision.metrics or {})


def _cnl_wpr_baseline(state="CNL-WPR-L:discount_mixed", side="LONG"):
    return CodexV1Decision(
        accepted=True,
        version=CODEX_V1_VERSION,
        baseline="test",
        lane="v139_canary_watch_pre_reprice_long_s1",
        lane_code="CNL-WPR-L",
        strategy="S1_BB_RSI",
        side=side,
        entry_offset_bp=3.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1421_decision_tree_adaptive_exec",
        metrics={"market_state": state, "policy_tag": "v1421_decision_tree_adaptive_exec"},
        policy_tag="v1421_decision_tree_adaptive_exec",
    )


def test_v1436_fast_reclaim_downslope_blocks_long_entry():
    baseline = _cnl_wpr_baseline("CNL-WPR-L:fast_reclaim", side="LONG")
    features = _v1427_complete_features(
        slope30=-1.8271,
        slope60=-3.6542,
        slope120=-6.8347,
        vwap_dist_bp=-14.2599,
    )

    blocked = apply_v1436_live_hotfix_decision(features, baseline)

    assert blocked.accepted is False
    assert blocked.reason == V1436_FAST_RECLAIM_DOWNSLOPE_BLOCK_TAG
    assert blocked.policy_tag == V1436_FAST_RECLAIM_DOWNSLOPE_BLOCK_TAG
    assert blocked.metrics["v1436_block_reason"] == "fast_reclaim_long_downslope_not_reclaimed"
    assert blocked.shadow_lane == "SH_V1436_FAST_RECLAIM_DOWNSLOPE"


def test_v1436_fast_reclaim_does_not_block_reclaimed_slope():
    baseline = _cnl_wpr_baseline("CNL-WPR-L:fast_reclaim", side="LONG")
    features = _v1427_complete_features(
        slope60=0.5,
        slope120=-1.0,
        vwap_dist_bp=-4.0,
    )

    kept = apply_v1436_live_hotfix_decision(features, baseline)

    assert kept is baseline


def test_v1449_blocks_cnl_wpr_falling_trap_low_rng_false_bounce():
    baseline = _cnl_wpr_baseline("CNL-WPR-L:falling_discount_trap", side="LONG")
    features = _v1427_complete_features(rng15=13.43, d30=-32.78, adv3=24.56)

    blocked = apply_v1436_live_hotfix_decision(features, baseline)

    assert blocked.accepted is False
    assert blocked.reason == V1449_CNL_WPR_FALLING_TRAP_QUALITY_BLOCK_TAG
    assert blocked.policy_tag == V1449_CNL_WPR_FALLING_TRAP_QUALITY_BLOCK_TAG
    assert blocked.metrics["v1449_block_reason"] == "low_rng_false_bounce"
    assert blocked.shadow_lane == "SH_V1449_CNL_WPR_FALLING_TRAP_QUALITY"


def test_v1449_blocks_cnl_wpr_falling_trap_deep_fall_no_reclaim():
    baseline = _cnl_wpr_baseline("CNL-WPR-L:falling_discount_trap", side="LONG")
    features = _v1427_complete_features(rng15=67.32, d30=-42.30, adv3=-3.54)

    blocked = apply_v1436_live_hotfix_decision(features, baseline)

    assert blocked.accepted is False
    assert blocked.reason == V1449_CNL_WPR_FALLING_TRAP_QUALITY_BLOCK_TAG
    assert blocked.metrics["v1449_block_reason"] == "deep_fall_no_reclaim"


def test_v1449_keeps_cnl_wpr_falling_trap_reclaimed_bounce():
    baseline = _cnl_wpr_baseline("CNL-WPR-L:falling_discount_trap", side="LONG")
    features = _v1427_complete_features(rng15=62.27, d30=-51.78, adv3=12.94)

    kept = apply_v1436_live_hotfix_decision(features, baseline)

    assert kept is baseline


def test_v1449_blocks_cnl_wpr_fast_reclaim_short_weak_adv3():
    baseline = _cnl_wpr_baseline("CNL-WPR-L:fast_reclaim", side="SHORT")
    features = _v1427_complete_features(rng15=28.80, d30=7.61, adv3=-0.56)

    blocked = apply_v1436_live_hotfix_decision(features, baseline)

    assert blocked.accepted is False
    assert blocked.reason == V1449_CNL_WPR_FAST_RECLAIM_QUALITY_BLOCK_TAG
    assert blocked.policy_tag == V1449_CNL_WPR_FAST_RECLAIM_QUALITY_BLOCK_TAG
    assert blocked.metrics["v1449_block_reason"] == "fast_reclaim_short_weak_adv3"
    assert blocked.shadow_lane == "SH_V1449_CNL_WPR_FAST_RECLAIM_QUALITY"


def test_v1450_blocks_cnl_wpr_deep_weak_rebound_late_chase():
    baseline = _cnl_wpr_baseline("CNL-WPR-L:deep_discount_stable", side="LONG")
    features = _v1427_complete_features(
        rng15=20.9017,
        d30=20.6745,
        adv3=0.1136,
        rsi=54.2912,
        vwap_dist_bp=-44.5466,
        range_pos_15=0.625,
        close_pos=0.8889,
        pullback_from_recent_high_bp=7.8381,
    )

    blocked = apply_v1436_live_hotfix_decision(features, baseline)

    assert blocked.accepted is False
    assert blocked.reason == V1450_CNL_WPR_DEEP_LATE_CHASE_BLOCK_TAG
    assert blocked.policy_tag == V1450_CNL_WPR_DEEP_LATE_CHASE_BLOCK_TAG
    assert blocked.metrics["v1450_block_reason"] == "weak_rebound_late_chase"
    assert blocked.shadow_lane == "SH_V1450_CNL_WPR_DEEP_LATE_CHASE"


def test_v1450_blocks_cnl_wpr_deep_upper_window_exhaustion():
    baseline = _cnl_wpr_baseline("CNL-WPR-L:deep_discount_stable", side="LONG")
    features = _v1427_complete_features(
        rng15=21.2452,
        d30=3.4651,
        adv3=-2.2159,
        rsi=59.4166,
        vwap_dist_bp=-39.4945,
        range_pos_15=0.9358,
        close_pos=1.0,
        pullback_from_recent_high_bp=1.3633,
    )

    blocked = apply_v1436_live_hotfix_decision(features, baseline)

    assert blocked.accepted is False
    assert blocked.reason == V1450_CNL_WPR_DEEP_LATE_CHASE_BLOCK_TAG
    assert blocked.metrics["v1450_block_reason"] == "upper_window_exhaustion"


def test_v1450_keeps_cnl_wpr_deep_profitable_trail_shape():
    baseline = _cnl_wpr_baseline("CNL-WPR-L:deep_discount_stable", side="LONG")
    features = _v1427_complete_features(
        rng15=24.99,
        d30=-7.38,
        adv3=3.4637,
        rsi=56.7074,
        vwap_dist_bp=-46.6311,
        range_pos_15=0.7182,
        close_pos=0.4605,
        pullback_from_recent_high_bp=7.0434,
    )

    kept = apply_v1436_live_hotfix_decision(features, baseline)

    assert kept is baseline



def test_v1451_blocks_stups_clean_extension_hot_short_trap():
    baseline = _stups_baseline("STUP-S:clean_extension", side="SHORT")
    baseline = baseline.__class__(
        **{**baseline.__dict__, "metrics": {**(baseline.metrics or {}), "v1441_research_selector_action": "SHADOW_REVIEW"}}
    )
    features = _v1427_complete_features(
        d30=48.62,
        adv3=10.4246,
        rsi=69.519,
        vwap_dist_bp=10.2215,
        range_pos_15=1.0572,
        pullback_from_recent_high_bp=20.4879,
        slope120=10.0276,
    )

    blocked = apply_v1436_live_hotfix_decision(features, baseline)

    assert blocked.accepted is False
    assert blocked.reason == V1451_STUPS_CLEAN_EXTENSION_SHADOW_REVIEW_BLOCK_TAG
    assert blocked.metrics["v1451_block_reason"] == "hot_extension_short_trap"
    assert blocked.shadow_lane == "SH_V1451_STUPS_CLEAN_EXTENSION_SHADOW_REVIEW"


def test_v1451_blocks_stups_clean_extension_weak_long_chase():
    baseline = _stups_baseline("STUP-S:clean_extension", side="LONG")
    baseline = baseline.__class__(
        **{**baseline.__dict__, "metrics": {**(baseline.metrics or {}), "v1441_research_selector_action": "SHADOW_REVIEW"}}
    )
    features = _v1427_complete_features(
        d30=33.88,
        adv3=2.3761,
        rsi=63.6901,
        vwap_dist_bp=16.795,
        range_pos_15=0.6072,
        pullback_from_recent_high_bp=23.6986,
        slope120=2.8854,
    )

    blocked = apply_v1436_live_hotfix_decision(features, baseline)

    assert blocked.accepted is False
    assert blocked.reason == V1451_STUPS_CLEAN_EXTENSION_SHADOW_REVIEW_BLOCK_TAG
    assert blocked.metrics["v1451_block_reason"] == "weak_extension_long_chase"


def test_v1451_keeps_stups_clean_extension_fast_scalp_winner():
    baseline = _stups_baseline("STUP-S:clean_extension", side="SHORT")
    baseline = baseline.__class__(
        **{**baseline.__dict__, "metrics": {**(baseline.metrics or {}), "v1441_research_selector_action": "SHADOW_REVIEW"}}
    )
    features = _v1427_complete_features(
        d30=50.18,
        adv3=7.7171,
        rsi=64.9346,
        vwap_dist_bp=26.434,
        range_pos_15=0.5647,
        pullback_from_recent_high_bp=20.5631,
        slope120=0.3353,
    )

    kept = apply_v1436_live_hotfix_decision(features, baseline)

    assert kept is baseline

def _stups_baseline(state="STUP-S:mixed", side="SHORT"):
    return CodexV1Decision(
        accepted=True,
        version=CODEX_V1_VERSION,
        baseline="test",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side=side,
        entry_offset_bp=2.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1427_five_window_tp14_adaptive_exec",
        metrics={"market_state": state, "policy_tag": "v1427_five_window_tp14_adaptive_exec"},
        policy_tag="v1427_five_window_tp14_adaptive_exec",
    )


def test_v1430_discount_mixed_blocks_high_rng15_loss_slice():
    features = _v1427_complete_features(rng15=35.0, vwap_dist_bp=-20.0, pullback_from_recent_high_bp=20.0)

    decision = apply_v1430_loss_prune_decision(features, _cnl_wpr_baseline("CNL-WPR-L:discount_mixed"))

    assert decision.accepted is False
    assert decision.reason == V1430_BLOCK_TAG
    assert decision.requested_notional_usdc == 0.0
    assert decision.metrics["v1430_state_key"] == "CNL-WPR-L|CNL-WPR-L:discount_mixed|LONG"
    assert decision.metrics["v1430_block_reason"] == "block_all"


def test_v1432_discount_mixed_blocks_low_rng15_after_live_max_hold_losses():
    features = _v1427_complete_features(rng15=28.0, vwap_dist_bp=-20.0, pullback_from_recent_high_bp=20.0)

    decision = apply_v1430_loss_prune_decision(features, _cnl_wpr_baseline("CNL-WPR-L:discount_mixed"))

    assert decision.accepted is False
    assert decision.reason == V1430_BLOCK_TAG
    assert decision.requested_notional_usdc == 0.0
    assert decision.metrics["v1430_state_key"] == "CNL-WPR-L|CNL-WPR-L:discount_mixed|LONG"
    assert decision.metrics["v1430_block_reason"] == "block_all"
    assert decision.metrics["policy_tag"] == V1430_BLOCK_TAG


def test_v1430_falling_discount_trap_requires_vwap_and_rsi_keep_branch():
    baseline = _cnl_wpr_baseline("CNL-WPR-L:falling_discount_trap")
    keep_features = _v1427_complete_features(rng15=50.0, vwap_dist_bp=-25.0, rsi=36.0)
    rsi_block_features = _v1427_complete_features(rng15=50.0, vwap_dist_bp=-25.0, rsi=38.0)
    vwap_block_features = _v1427_complete_features(rng15=50.0, vwap_dist_bp=-20.0, rsi=36.0)

    kept = apply_v1430_loss_prune_decision(keep_features, baseline)
    rsi_blocked = apply_v1430_loss_prune_decision(rsi_block_features, baseline)
    vwap_blocked = apply_v1430_loss_prune_decision(vwap_block_features, baseline)

    assert kept.accepted is True
    assert kept.metrics["v1430_final_action"] == "g__nf_e0_t120__trail_arm11_gb5_fl4_sl8"
    assert kept.metrics["sl_bp"] == pytest.approx(8.0)
    assert kept.metrics["trail_giveback_bp"] == pytest.approx(5.0)
    assert rsi_blocked.accepted is False
    assert rsi_blocked.reason == V1430_BLOCK_TAG
    assert rsi_blocked.metrics["v1430_block_reason"] == "targeted_split_block"
    assert vwap_blocked.accepted is False
    assert vwap_blocked.metrics["v1430_block_reason"] == "loss_prune_rejected"


def test_v1430_stups_mixed_short_uses_pullback_prune_and_adv3_split():
    baseline = _stups_baseline("STUP-S:mixed", side="SHORT")
    low_pullback = _v1427_complete_features(side="SHORT", pullback_from_recent_high_bp=14.0, adv3=5.0)
    keep_features = _v1427_complete_features(side="SHORT", pullback_from_recent_high_bp=18.0, adv3=7.0)
    high_adv3 = _v1427_complete_features(side="SHORT", pullback_from_recent_high_bp=18.0, adv3=8.0)

    pruned = apply_v1430_loss_prune_decision(low_pullback, baseline)
    kept = apply_v1430_loss_prune_decision(keep_features, baseline)
    split_blocked = apply_v1430_loss_prune_decision(high_adv3, baseline)

    assert pruned.accepted is False
    assert pruned.metrics["v1430_block_reason"] == "loss_prune_rejected"
    assert kept.accepted is True
    assert kept.side == "SHORT"
    assert kept.metrics["v1430_final_action"] == "p__nf_e0_t120__trail_arm11_gb6_fl5_sl10"
    assert kept.metrics["ttl_s"] == 120
    assert kept.metrics["tp1_bp"] == pytest.approx(6.0)
    assert kept.metrics["partial_exit_pct"] == pytest.approx(0.70)
    assert kept.metrics["sl_bp"] == pytest.approx(10.0)
    assert kept.metrics["trail_arm_bp"] == pytest.approx(6.0)
    assert kept.metrics["trail_giveback_bp"] == pytest.approx(3.0)
    assert kept.metrics["tp_execution_note"] == "TP1_70_RUNNER_TRAIL_ONLY"
    assert kept.metrics["profile_anchor"] == "v1435_tp1_70_runner_trail_only_tp1_floor"
    assert split_blocked.accepted is False
    assert split_blocked.metrics["v1430_block_reason"] == "targeted_split_block"


def test_v1433_stups_weak_chop_short_blocks_live_after_fee_loss():
    baseline = _stups_baseline("STUP-S:weak_chop", side="SHORT")
    keep_features = _v1427_complete_features(side="SHORT", pullback_from_recent_high_bp=18.0, adv3=5.0)
    block_features = _v1427_complete_features(side="SHORT", pullback_from_recent_high_bp=18.0, adv3=5.6)

    kept = apply_v1430_loss_prune_decision(keep_features, baseline)
    blocked = apply_v1430_loss_prune_decision(block_features, baseline)

    assert kept.accepted is False
    assert kept.reason == V1430_BLOCK_TAG
    assert kept.metrics["v1430_state_key"] == "STUP-S|STUP-S:weak_chop|SHORT"
    assert kept.metrics["v1430_block_reason"] == "block_all"
    assert blocked.accepted is False
    assert blocked.metrics["v1430_block_reason"] == "block_all"


def test_v1430_uses_raw_side_after_v1427_side_override_for_weak_chop():
    features = _v1427_complete_features(
        side="SHORT",
        slope30=0.0,
        slope60=0.0,
        slope120=0.0,
        rng15=20.0,
        d30=0.0,
        adv3=3.0,
        rsi=50.0,
        vwap_dist_bp=5.0,
        range_pos_15=0.70,
        pullback_from_recent_high_bp=20.0,
    )
    baseline = _stups_baseline("STUP-S:weak_chop", side="SHORT")

    v1427 = apply_v1427_five_window_decision(features, baseline)
    decision = apply_v1430_loss_prune_decision(features, v1427)

    assert v1427.accepted is True
    assert v1427.side == "LONG"
    assert v1427.metrics["v1427_previous_side"] == "SHORT"
    assert decision.accepted is False
    assert decision.reason == V1430_BLOCK_TAG
    assert decision.metrics["v1430_state_key"] == "STUP-S|STUP-S:weak_chop|SHORT"
    assert decision.metrics["v1430_block_reason"] == "block_all"


def test_v1433_blocks_wpr_deep_discount_stable_short_live():
    features = _v1427_complete_features(side="SHORT", vwap_dist_bp=-22.0, d30=-5.0, rsi=50.0)
    baseline = _cnl_wpr_baseline("CNL-WPR-L:deep_discount_stable", side="SHORT")

    decision = apply_v1430_loss_prune_decision(features, baseline)

    assert decision.accepted is False
    assert decision.reason == V1430_BLOCK_TAG
    assert decision.metrics["v1430_state_key"] == "CNL-WPR-L|CNL-WPR-L:deep_discount_stable|SHORT"
    assert decision.metrics["v1430_block_reason"] == "block_all"




def test_v1433_blocks_wpr_deep_discount_stable_target_short_after_v1427():
    features = _v1427_complete_features(
        side="LONG",
        rng15=20.0,
        d30=-60.0,
        adv3=2.0,
        rsi=55.0,
        vwap_dist_bp=-50.0,
        range_pos_15=0.2,
        pullback_from_recent_high_bp=5.0,
        slope30=0.0,
        slope60=-10.0,
        slope120=-20.0,
    )
    baseline = _cnl_wpr_baseline("CNL-WPR-L:deep_discount_stable", side="LONG")

    v1427 = apply_v1427_five_window_decision(features, baseline)
    decision = apply_v1430_loss_prune_decision(features, v1427)

    assert v1427.accepted is True
    assert v1427.side == "SHORT"
    assert v1427.metrics["v1427_previous_side"] == "LONG"
    assert decision.accepted is False
    assert decision.reason == V1430_BLOCK_TAG
    assert decision.metrics["v1430_state_key"] == "CNL-WPR-L|CNL-WPR-L:deep_discount_stable|SHORT"
    assert decision.metrics["v1430_block_reason"] == "block_all"
def test_v1430_blocks_stups_mixed_long_and_missing_controlled_feature():
    long_block = apply_v1430_loss_prune_decision(
        _v1427_complete_features(side="LONG"),
        _stups_baseline("STUP-S:mixed", side="LONG"),
    )
    missing_features = _v1427_complete_features(side="SHORT", pullback_from_recent_high_bp=18.0, adv3=5.0)
    missing_features.pop("adv3")

    missing = apply_v1430_loss_prune_decision(missing_features, _stups_baseline("STUP-S:mixed", side="SHORT"))

    assert long_block.accepted is False
    assert long_block.reason == V1430_BLOCK_TAG
    assert long_block.metrics["v1430_block_reason"] == "block_all"
    assert missing.accepted is False
    assert missing.reason == V1430_MISSING_FEATURE_BLOCK_TAG
    assert missing.missing_features == ("adv3",)


def test_v1423_conservative_tree_uses_target_1p4_discount_mixed_profile():
    features = {
        "rng15": 26.014305,
        "d30": -22.912843,
        "adv3": -2.595775,
        "rsi": 43.684129,
        "vwap_dist_bp": -31.492543,
        "range_pos_15": 0.265207,
        "pullback_from_recent_high_bp": 19.115134,
        "slope30": 0.284844,
        "slope60": 0.569689,
        "slope120": 2.46912,
        "v1423_slope_source": "unit_test_fourth_window",
    }

    decision = apply_v1423_conservative_decision(features, _cnl_wpr_baseline("CNL-WPR-L:discount_mixed"))

    assert decision.accepted
    assert decision.policy_tag == "v1423_four_window_conservative_tree_exec"
    assert decision.reason == "v1423_four_window_conservative_tree_exec"
    assert decision.side == "LONG"
    assert decision.entry_offset_bp == 4.0
    assert decision.metrics["v1423_action"] == "L_E4_TP20_SL10_T60"
    assert decision.metrics["tp1_bp"] == 20.0
    assert decision.metrics["full_tp_bp"] == 20.0
    assert decision.metrics["sl_bp"] == 10.0
    assert decision.metrics["be_bp"] == 0.0
    assert decision.metrics["partial_exit_pct"] == 1.0
    assert decision.metrics["ttl_s"] == 60
    assert decision.metrics["v1423_target_50_net_usdc"] == 1.4
    assert decision.metrics["v1423_projected_50_net_usdc"] == 1.4849
    assert decision.metrics["v1423_slope_source"] == "unit_test_fourth_window"
    assert "v1423_four_window_conservative_tree" in decision.risk_tags
    assert "v1423_action_l_e4_tp20_sl10_t60" in decision.risk_tags
    assert "be0" in decision.risk_tags


def test_v1423_conservative_tree_pins_fourth_window_fast_reclaim_profile():
    features = {
        "rng15": 46.565701,
        "d30": 31.085922,
        "adv3": 4.610216,
        "rsi": 54.290472,
        "vwap_dist_bp": -13.145462,
        "range_pos_15": 0.762551,
        "pullback_from_recent_high_bp": 11.056985,
        "slope30": -2.557593,
        "slope60": -5.115186,
        "slope120": -8.963911,
    }

    decision = apply_v1423_conservative_decision(features, _cnl_wpr_baseline("CNL-WPR-L:fast_reclaim"))

    assert decision.metrics["v1423_action"] == "L_E0_TP20_SL4_T45"
    assert decision.entry_offset_bp == 0.0
    assert decision.metrics["tp1_bp"] == 20.0
    assert decision.metrics["sl_bp"] == 4.0
    assert decision.metrics["ttl_s"] == 45


def test_v1423_conservative_tree_pins_fourth_window_falling_trap_profile():
    features = {
        "rng15": 72.993,
        "d30": -64.819,
        "adv3": -2.852,
        "rsi": 27.073,
        "vwap_dist_bp": -46.221,
        "range_pos_15": 0.158,
        "pullback_from_recent_high_bp": 61.461,
        "slope30": -7.244,
        "slope60": -14.489,
        "slope120": -5.509,
    }

    decision = apply_v1423_conservative_decision(features, _cnl_wpr_baseline("CNL-WPR-L:falling_discount_trap"))

    assert decision.metrics["v1423_action"] == "L_E2_TP20_SL10_T90"
    assert decision.entry_offset_bp == 2.0
    assert decision.metrics["tp1_bp"] == 20.0
    assert decision.metrics["sl_bp"] == 10.0
    assert decision.metrics["ttl_s"] == 90



def test_v1425_falling_trap_short_e0_uses_deeper_scalp_profit_lock():
    features = {
        "rng15": 74.8714,
        "d30": -5.8873,
        "adv3": 28.3322,
        "rsi": 41.3355,
        "vwap_dist_bp": -4.8027,
        "range_pos_15": 0.0239,
        "pullback_from_recent_high_bp": 73.0796,
        "slope30": -7.8906,
        "slope60": -15.7812,
        "slope120": -20.7544,
        "v1423_slope_source": "live_1782835074370",
    }

    decision = apply_v1423_conservative_decision(features, _cnl_wpr_baseline("CNL-WPR-L:falling_discount_trap", side="SHORT"))

    assert decision.accepted
    assert decision.reason == "v1426_wpr_falling_trap_short_scalp_exec"
    assert decision.policy_tag == "v1426_wpr_falling_trap_short_scalp_exec"
    assert decision.side == "SHORT"
    assert decision.entry_offset_bp == 2.0
    assert decision.metrics["v1423_action"] == "S_E0_TP20_SL6_T45"
    assert decision.metrics["v1425_original_action"] == "S_E0_TP20_SL6_T45"
    assert decision.metrics["v1426_original_action"] == "S_E0_TP20_SL6_T45"
    assert decision.metrics["entry_bp"] == 2.0
    assert decision.metrics["tp1_bp"] == 6.0
    assert decision.metrics["full_tp_bp"] == 6.0
    assert decision.metrics["sl_bp"] == 6.0
    assert decision.metrics["ttl_s"] == 60
    assert decision.metrics["profit_lock_mfe_bp"] == 6.0
    assert decision.metrics["profit_lock_floor_bp"] == 4.0
    assert decision.metrics["profit_lock_giveback_bp"] == 2.0
    assert "v1426_wpr_falling_trap_short_scalp_exec" in decision.risk_tags


def test_v1426_falling_trap_short_e2_uses_scalp_profit_lock():
    features = {
        "rng15": 17.7041,
        "d30": -22.3698,
        "adv3": 9.833,
        "rsi": 39.7399,
        "vwap_dist_bp": -53.4632,
        "range_pos_15": 0.0975,
        "pullback_from_recent_high_bp": 15.9784,
        "slope30": -1.6612,
        "slope60": -3.3224,
        "slope120": -5.8128,
        "v1423_slope_source": "live_1782860452343",
    }

    decision = apply_v1423_conservative_decision(features, _cnl_wpr_baseline("CNL-WPR-L:falling_discount_trap", side="SHORT"))

    assert decision.accepted
    assert decision.reason == "v1426_wpr_falling_trap_short_scalp_exec"
    assert decision.policy_tag == "v1426_wpr_falling_trap_short_scalp_exec"
    assert decision.side == "SHORT"
    assert decision.metrics["v1423_action"] == "S_E2_TP20_SL4_T45"
    assert decision.metrics["v1426_original_action"] == "S_E2_TP20_SL4_T45"
    assert decision.entry_offset_bp == 2.0
    assert decision.metrics["tp1_bp"] == 6.0
    assert decision.metrics["full_tp_bp"] == 6.0
    assert decision.metrics["sl_bp"] == 6.0
    assert decision.metrics["be_bp"] == 0.0
    assert decision.metrics["profit_lock_mfe_bp"] == 6.0
    assert decision.metrics["profit_lock_floor_bp"] == 4.0
    assert decision.metrics["profit_lock_giveback_bp"] == 2.0


def test_v1425_falling_trap_direct_long_e0_blocks_live_entry():
    features = {
        "rng15": 55.3602,
        "d30": -33.3056,
        "adv3": -2.8775,
        "rsi": 45.5154,
        "vwap_dist_bp": -6.9149,
        "range_pos_15": 0.3845,
        "pullback_from_recent_high_bp": 34.0727,
        "slope30": 0.7353,
        "slope60": 1.4705,
        "slope120": 7.485,
        "v1423_slope_source": "live_1782836244488",
    }

    decision = apply_v1423_conservative_decision(features, _cnl_wpr_baseline("CNL-WPR-L:falling_discount_trap"))

    assert not decision.accepted
    assert decision.reason == "v1425_wpr_falling_trap_direct_long_block"
    assert decision.policy_tag == "v1425_wpr_falling_trap_direct_long_block"
    assert decision.requested_notional_usdc == 0.0
    assert decision.metrics["v1423_action"] == "L_E0_TP12_SL10_T45"
    assert decision.metrics["blocked_v1425_action"] == "L_E0_TP12_SL10_T45"
    assert decision.shadow_lane == "SH_V1425_ACTION_BLOCK"


def test_v1425_stups_weak_chop_direct_long_e0_blocks_live_entry():
    features = {
        "rng15": 44.0849,
        "d30": 26.4765,
        "adv3": 16.0392,
        "rsi": 57.2555,
        "vwap_dist_bp": 11.9425,
        "range_pos_15": 0.8582,
        "pullback_from_recent_high_bp": 37.8326,
        "slope30": -2.6781,
        "slope60": -5.3562,
        "slope120": 11.497,
        "v1423_slope_source": "live_1782835444480",
    }
    baseline = CodexV1Decision(
        accepted=True,
        version=CODEX_V1_VERSION,
        baseline="test",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1423_four_window_conservative_tree_exec",
        regime="STUP-S:weak_chop",
        risk_tags=("v1423_four_window_conservative_tree_exec",),
        metrics={"market_state": "STUP-S:weak_chop", "policy_tag": "v1423_four_window_conservative_tree_exec"},
        policy_tag="v1423_four_window_conservative_tree_exec",
    )

    decision = apply_v1423_conservative_decision(features, baseline)

    assert not decision.accepted
    assert decision.reason == "v1425_stups_weak_chop_direct_long_block"
    assert decision.policy_tag == "v1425_stups_weak_chop_direct_long_block"
    assert decision.requested_notional_usdc == 0.0
    assert decision.metrics["v1423_action"] == "L_E0_TP20_SL4_T45"
    assert decision.metrics["blocked_v1425_action"] == "L_E0_TP20_SL4_T45"
    assert decision.shadow_lane == "SH_V1425_ACTION_BLOCK"


def test_v1426_stups_weak_chop_short_blocks_live_entry():
    features = {
        "rng15": 66.4217,
        "d30": 26.4416,
        "adv3": 11.1993,
        "rsi": 55.4553,
        "vwap_dist_bp": 35.6296,
        "range_pos_15": 0.3952,
        "pullback_from_recent_high_bp": 24.5983,
        "slope30": 0.7629,
        "slope60": 1.5257,
        "slope120": 2.9247,
        "v1423_slope_source": "live_1782842741435",
    }
    baseline = CodexV1Decision(
        accepted=True,
        version=CODEX_V1_VERSION,
        baseline="test",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1423_four_window_conservative_tree_exec",
        regime="STUP-S:weak_chop",
        risk_tags=("v1423_four_window_conservative_tree_exec",),
        metrics={"market_state": "STUP-S:weak_chop", "policy_tag": "v1423_four_window_conservative_tree_exec"},
        policy_tag="v1423_four_window_conservative_tree_exec",
    )

    decision = apply_v1423_conservative_decision(features, baseline)

    assert not decision.accepted
    assert decision.reason == "v1426_stups_weak_chop_short_block"
    assert decision.policy_tag == "v1426_stups_weak_chop_short_block"
    assert decision.requested_notional_usdc == 0.0
    assert decision.metrics["v1423_action"] == "S_E0_TP15_SL4_T45"
    assert decision.metrics["blocked_v1426_action"] == "S_E0_TP15_SL4_T45"
    assert decision.shadow_lane == "SH_V1426_STUPS_SHORT_BLOCK"


def test_v1426_stups_mixed_short_blocks_live_entry():
    features = {
        "rng15": 24.4787,
        "d30": 9.307,
        "adv3": 1.0838,
        "rsi": 51.2971,
        "vwap_dist_bp": 15.2859,
        "range_pos_15": 0.6589,
        "pullback_from_recent_high_bp": 16.1279,
        "slope30": -2.5804,
        "slope60": -5.1608,
        "slope120": 0.8925,
        "v1423_slope_source": "live_1782839942525",
    }
    baseline = CodexV1Decision(
        accepted=True,
        version=CODEX_V1_VERSION,
        baseline="test",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1423_four_window_conservative_tree_exec",
        regime="STUP-S:mixed",
        risk_tags=("v1423_four_window_conservative_tree_exec",),
        metrics={"market_state": "STUP-S:mixed", "policy_tag": "v1423_four_window_conservative_tree_exec"},
        policy_tag="v1423_four_window_conservative_tree_exec",
    )

    decision = apply_v1423_conservative_decision(features, baseline)

    assert not decision.accepted
    assert decision.reason == "v1426_stups_mixed_short_block"
    assert decision.policy_tag == "v1426_stups_mixed_short_block"
    assert decision.requested_notional_usdc == 0.0
    assert decision.metrics["v1423_action"] == "S_E2_TP20_SL4_T45"
    assert decision.metrics["blocked_v1426_action"] == "S_E2_TP20_SL4_T45"
    assert decision.shadow_lane == "SH_V1426_STUPS_SHORT_BLOCK"


def test_v1424_stups_base_action_blocks_legacy_profile_fallback():
    features = {
        "rng15": 28.0,
        "d30": 34.0,
        "adv3": 5.5,
        "rsi": 59.0,
        "vwap_dist_bp": 20.0,
        "range_pos_15": 0.54,
        "pullback_from_recent_high_bp": 15.0,
        "slope30": 5.0,
        "slope60": 5.0,
        "slope120": 5.0,
        "v1423_slope_source": "unit_test_live_base_block",
    }
    baseline = CodexV1Decision(
        accepted=True,
        version=CODEX_V1_VERSION,
        baseline="test",
        lane="codex_v1_stale_upmove_short_rng20_canary",
        lane_code="STUP-S",
        strategy="S1_BB_RSI",
        side="SHORT",
        entry_offset_bp=0.0,
        size_mult=1.0,
        notional_mult=1.0,
        requested_notional_usdc=50.0,
        reason="v1420_stups_fixed_regime_exec",
        regime="STUP-S:weak_chop",
        risk_tags=("v1420_stups_fixed_regime_exec",),
        metrics={"market_state": "STUP-S:weak_chop", "policy_tag": "v1420_stups_fixed_regime_exec"},
        policy_tag="v1420_stups_fixed_regime_exec",
    )

    decision = apply_v1423_conservative_decision(features, baseline)

    assert not decision.accepted
    assert decision.reason == "v1424_stups_base_shadow_block"
    assert decision.policy_tag == "v1424_stups_base_shadow_block"
    assert decision.requested_notional_usdc == 0.0
    assert decision.metrics["v1423_action"] == "BASE"
    assert decision.metrics["blocked_base_fallback"] is True
    assert decision.metrics["v1423_state"] == "STUP-S:weak_chop"


def test_v1426_wpr_base_fallback_blocks_legacy_profile():
    features = {
        "rng15": 34.7525,
        "d30": -19.6676,
        "adv3": 5.1529,
        "rsi": 43.1925,
        "vwap_dist_bp": -3.3677,
        "range_pos_15": 0.3736,
        "pullback_from_recent_high_bp": 18.3946,
        "slope30": 0.191,
        "slope60": 0.3819,
        "slope120": 1.0185,
        "v1423_slope_source": "live_1782854172324",
    }

    decision = apply_v1423_conservative_decision(features, _cnl_wpr_baseline("CNL-WPR-L:discount_mixed"))

    assert not decision.accepted
    assert decision.reason == "v1426_wpr_base_fallback_shadow_block"
    assert decision.policy_tag == "v1426_wpr_base_fallback_shadow_block"
    assert decision.requested_notional_usdc == 0.0
    assert decision.metrics["v1423_action"] == "BASE"
    assert decision.metrics["blocked_base_fallback"] is True
    assert decision.metrics["blocked_v1426_state"] == "CNL-WPR-L:discount_mixed"
    assert decision.shadow_lane == "SH_V1426_WPR_BASE_BLOCK"


def test_v1423_conservative_tree_skips_uncontrolled_lane_and_missing_slopes():
    s1p_baseline = CodexV1Decision(
        accepted=True,
        version=CODEX_V1_VERSION,
        baseline="test",
        lane="codex_v1_s1_bbrsi_ordinary_pullback_long_pre_vwap",
        lane_code="S1P-L",
        strategy="S1_BB_RSI",
        side="LONG",
        entry_offset_bp=0.0,
        size_mult=0.2,
        notional_mult=0.2,
        requested_notional_usdc=25.0,
        reason="s1p_l_match",
        metrics={"market_state": "S1P-L:ordinary_pullback_pre_vwap", "policy_tag": "v149_s1pl_tiny_profile_fix"},
        policy_tag="v149_s1pl_tiny_profile_fix",
    )
    complete_features = {
        "slope30": 0.0,
        "slope60": 5.0,
        "slope120": 0.0,
        "rng15": 40.0,
        "d30": 0.0,
        "adv3": 2.0,
        "rsi": 55.0,
        "vwap_dist_bp": -20.0,
        "range_pos_15": 0.7,
        "pullback_from_recent_high_bp": 20.0,
    }

    assert apply_v1423_conservative_decision(complete_features, s1p_baseline) is s1p_baseline

    missing_slope = dict(complete_features)
    missing_slope.pop("slope30")
    cnl_baseline = _cnl_wpr_baseline("CNL-WPR-L:discount_mixed")
    decision = apply_v1423_conservative_decision(missing_slope, cnl_baseline)

    assert decision is cnl_baseline
    assert "v1423_action" not in (decision.metrics or {})


def test_v143_stups_clean_extension_uses_state_profile():
    decision = build_stale_upmove_canary_decision(
        _stups_v143_features(rng15=35.0, d30=12.0, adv3=2.0, vwap_dist_bp=9.0, rsi=64.0, range_pos_15=0.8)
    )

    assert decision is not None
    assert decision.accepted
    assert decision.lane_code == "STUP-S"
    assert decision.requested_notional_usdc == 50.0
    assert decision.entry_offset_bp == 2.0
    assert decision.reason == "v1420_stups_fixed_regime_exec"
    assert decision.regime == "STUP-S:clean_extension"
    assert decision.metrics["tp1_bp"] == 6.0
    assert decision.metrics["full_tp_bp"] == 80.0
    assert decision.metrics["sl_bp"] == 8.0
    assert decision.metrics["be_bp"] == 2.0
    assert decision.metrics["ttl_s"] == 60
    assert decision.metrics["partial_exit_pct"] == 0.7
    assert decision.metrics["adaptive_tp_engine"] == "v1420_stups_runner_after_clean_gate"
    assert "v1420_stups_fixed_regime_exec" in decision.risk_tags
    assert "stups_state_clean_extension" in decision.risk_tags


def test_v1420_stups_clean_extension_blocks_range_top_without_clean_gate():
    decision = build_stale_upmove_canary_decision(
        _stups_v143_features(
            rng15=27.9877,
            d30=7.4888,
            adv3=14.2362,
            range_bp=4.0,
            rsi=62.5731,
            vwap_dist_bp=9.0032,
            range_pos_15=1.0907,
            pullback_from_recent_high_bp=30.5263,
        )
    )

    assert decision is not None
    assert not decision.accepted
    assert decision.regime == "STUP-S:clean_extension"
    assert decision.reason == "v1420_stups_clean_extension_gate_block"
    assert decision.metrics["condition"] == "clean_extension_rng15_le36_vwap_8_13p5_d30_ge10_pullback_ge27_or_rangepos_le0p80"


def test_v1420_stups_clean_extension_blocks_hot_vwap_outside_gate():
    decision = build_stale_upmove_canary_decision(
        _stups_v143_features(
            rng15=43.8071,
            d30=40.1301,
            adv3=3.8687,
            range_bp=4.0,
            rsi=66.2524,
            vwap_dist_bp=14.1232,
            range_pos_15=0.9392,
            pullback_from_recent_high_bp=35.7557,
        )
    )

    assert decision is not None
    assert not decision.accepted
    assert decision.regime == "STUP-S:clean_extension"
    assert decision.reason == "v1420_stups_clean_extension_gate_block"


def test_v1420_stups_mixed_uses_runner_only_outside_bad_weakzone():
    decision = build_stale_upmove_canary_decision(_stups_v143_features(rng15=55.0, range_bp=12.0))

    assert decision is not None
    assert decision.accepted
    assert decision.reason == "v1420_stups_fixed_regime_exec"
    assert decision.regime == "STUP-S:mixed"
    assert decision.entry_offset_bp == 2.0
    assert decision.metrics["tp1_bp"] == 6.0
    assert decision.metrics["full_tp_bp"] == 80.0
    assert decision.metrics["sl_bp"] == 8.0
    assert decision.metrics["be_bp"] == 2.0
    assert decision.metrics["ttl_s"] == 60
    assert decision.metrics["partial_exit_pct"] == 0.7
    assert decision.metrics["adaptive_tp_engine"] == "v1420_stups_runner_after_bad_weakzone_block"
    assert decision.metrics["replay_n"] == 7
    assert decision.metrics["replay_net_usdc"] == 0.32756


def test_v1420_stups_mixed_weakzone_is_blocked():
    decision = build_stale_upmove_canary_decision(_stups_v143_features())

    assert decision is not None
    assert not decision.accepted
    assert decision.regime == "STUP-S:mixed"
    assert decision.reason == "v1420_stups_mixed_weakzone_block"
    assert decision.metrics["condition"] == "mixed_rng15_le50_rangepos_ge0p35_rangebp_le10"


def test_v1420_stups_mixed_bad_is_blocked_before_runner():
    decision = build_stale_upmove_canary_decision(
        _stups_v143_features(d30=-8.0, adv3=8.0, pullback_from_recent_high_bp=20.0, range_pos_15=0.50)
    )

    assert decision is not None
    assert not decision.accepted
    assert decision.regime == "STUP-S:mixed"
    assert decision.reason == "v1420_stups_mixed_bad_block"
    assert decision.metrics["condition"] == "mixed_d30_le_minus7_adv3_ge7_pullback_le23_rangepos_0p35_0p65"


def test_v1420_stups_weak_chop_extreme_is_blocked():
    decision = build_stale_upmove_canary_decision(
        _stups_v143_features(d30=0.0, adv3=3.0, rsi=55.0, vwap_dist_bp=6.0, range_pos_15=0.92)
    )

    assert decision is not None
    assert not decision.accepted
    assert decision.regime == "STUP-S:weak_chop"
    assert decision.reason == "v1420_stups_weak_chop_extreme_block"
    assert decision.metrics["condition"] == "weak_chop_rangepos_ge0p90_or_rangebp_le1p5"


def test_v1416_stups_weak_chop_uses_tp1_runner_profile():
    decision = build_stale_upmove_canary_decision(
        _stups_v143_features(d30=0.0, adv3=3.0, rsi=55.0, vwap_dist_bp=6.0, range_pos_15=0.6)
    )

    assert decision is not None
    assert decision.accepted
    assert decision.regime == "STUP-S:weak_chop"
    assert decision.entry_offset_bp == 0.0
    assert decision.metrics["tp1_bp"] == 5.0
    assert decision.metrics["full_tp_bp"] == 12.0
    assert decision.metrics["sl_bp"] == 10.0
    assert decision.metrics["be_bp"] == 4.0
    assert decision.metrics["partial_exit_pct"] == 0.6
    assert decision.metrics["adaptive_tp_engine"] == "v1416_stups_tp1_runner"


def test_v1417_stups_weak_chop_low_rng_weak_adv_uses_cautious_live_entry():
    decision = build_stale_upmove_canary_decision(
        _stups_v143_features(d30=0.0, adv3=2.5, rng15=25.0, rsi=55.0, vwap_dist_bp=6.0, range_pos_15=0.6)
    )

    assert decision is not None
    assert decision.accepted
    assert decision.reason == "v1420_stups_fixed_regime_exec"
    assert decision.regime == "STUP-S:weak_chop"
    assert decision.requested_notional_usdc == 50.0
    assert decision.entry_offset_bp == 2.0
    assert decision.metrics["entry_bp"] == 2.0
    assert decision.metrics["tp1_bp"] == 5.0
    assert decision.metrics["full_tp_bp"] == 12.0
    assert decision.metrics["adaptive_tp_engine"] == "v1416_stups_tp1_runner"
    assert decision.metrics["profile_patch"] == "v1417_stups_low_rng_weak_adv_cautious_live"
    assert decision.metrics["low_rng_weak_adv_action"] == "live_cautious_maker"
    assert decision.metrics["low_rng_weak_adv_condition"] == "weak_chop_rng15_le30_adv3_lt3"
    assert "v1417_stups_low_rng_weak_adv_cautious_live" in decision.risk_tags


def test_v1448_stale_squeeze_top_is_not_legacy_reopened():
    assert "v143_stups_stale_squeeze_top_shadow_only" not in V1428_LEGACY_STUPS_REOPEN_REASONS


def test_v143_stups_stale_squeeze_top_is_shadow_only():
    decision = build_stale_upmove_canary_decision(
        _stups_v143_features(range_bp=2.0, range_pos_15=0.98, reprice_wait_elapsed_seconds=320.0)
    )

    assert decision is not None
    assert not decision.accepted
    assert decision.lane_code == "STUP-S"
    assert decision.reason == "v143_stups_stale_squeeze_top_shadow_only"
    assert decision.requested_notional_usdc == 0.0
    assert decision.metrics["fixed_notional_usdc"] == 50.0
    assert decision.metrics["applied_notional_cap_usdc"] == 0.0
    assert decision.metrics["market_state"] == "STUP-S:stale_squeeze_top"
    assert decision.shadow_lane == "SH_SHORT_STALE_UPMOVE_S1"


def test_v1414_stups_hot_continuation_is_shadow_only():
    decision = build_stale_upmove_canary_decision(
        _stups_v143_features(
            rng15=72.0,
            d30=35.0,
            adv3=22.0,
            rsi=66.0,
            vwap_dist_bp=18.0,
            range_bp=5.0,
            range_pos_15=0.82,
            reprice_wait_elapsed_seconds=90.0,
        )
    )

    assert decision is not None
    assert not decision.accepted
    assert decision.reason == "v143_stups_hot_continuation_shadow_only"
    assert decision.metrics["market_state"] == "STUP-S:hot_continuation"
    assert "stups_state_hot_continuation" in decision.risk_tags


def test_v147_stups_flat_top_low_range_is_shadow_only():
    decision = build_stale_upmove_canary_decision(
        _stups_v143_features(
            d30=5.0,
            adv3=2.5,
            rsi=60.0,
            vwap_dist_bp=59.0,
            range_bp=1.2,
            range_pos_15=0.94,
            reprice_wait_elapsed_seconds=90.0,
        )
    )

    assert decision is not None
    assert not decision.accepted
    assert decision.reason == "v143_stups_stale_squeeze_top_shadow_only"
    assert decision.metrics["market_state"] == "STUP-S:stale_squeeze_top"


def test_v1420_stups_weak_chop_late_adv_is_not_blocked_without_extreme():
    decision = build_stale_upmove_canary_decision(
        _stups_v143_features(
            d30=28.8,
            adv3=8.7,
            rsi=54.0,
            vwap_dist_bp=59.0,
            range_bp=2.9,
            range_pos_15=0.58,
            reprice_wait_elapsed_seconds=450.0,
        )
    )

    assert decision is not None
    assert decision.accepted
    assert decision.reason == "v1420_stups_fixed_regime_exec"
    assert decision.metrics["market_state"] == "STUP-S:weak_chop"


def test_v1420_stups_late_mixed_reversal_is_not_blocked_outside_bad_weakzone():
    decision = build_stale_upmove_canary_decision(
        _stups_v143_features(
            d30=-25.0,
            adv3=5.5,
            rsi=43.0,
            vwap_dist_bp=12.0,
            range_bp=12.0,
            range_pos_15=0.52,
            reprice_wait_elapsed_seconds=540.0,
        )
    )

    assert decision is not None
    assert decision.accepted
    assert decision.reason == "v1420_stups_fixed_regime_exec"
    assert decision.metrics["market_state"] == "STUP-S:mixed"


def test_v1414_strong_fall_follow_short_canary_accepts_native_short():
    decision = select_codex_v1_lane(
        {
            "symbol": "ETHUSDC",
            "strategy": "S1_BB_RSI",
            "side": "SHORT",
            "score": 72.0,
            "rng15": 96.0,
            "d30": -42.0,
            "adv3": 4.0,
            "range_bp": 8.0,
            "rsi": 44.0,
            "vwap_dist_bp": -22.0,
            "range_pos_15": 0.22,
            "reprice_wait_elapsed_seconds": 30.0,
        }
    )

    assert decision.accepted
    assert decision.lane_code == "SFD-S"
    assert decision.lane == "codex_v1_strong_fall_follow_short_canary"
    assert decision.reason == "v1414_strong_fall_follow_exec"
    assert decision.regime == "SFD-S:strong_down_continuation"
    assert decision.entry_offset_bp == 2.0
    assert decision.metrics["tp1_bp"] == 6.0
    assert decision.metrics["full_tp_bp"] == 8.0
    assert decision.metrics["sl_bp"] == 10.0
    assert decision.metrics["profit_lock_floor_bp"] == 6.0
    assert "strong_fall_follow_canary" in decision.risk_tags


def test_v133_no_lane_candidate_miner_classifies_near_rp1_before_outcome():
    result = classify_codex_v133_no_lane_candidate(
        {
            "symbol": "ETHUSDC",
            "strategy": "S1_BB_RSI",
            "side": "LONG",
            "reprice_favorable_bp": 4.5,
            "reprice_adverse_bp": 1.0,
            "rsi": 45.0,
            "range_bp": 6.0,
            "ret3_bp": -20.0,
        },
        reason="no_codex_v1_lane_match",
    )

    assert result["candidate_bucket"] == "NL_NEAR_RP1_LONG"
    assert result["nearest_lane_code"] == "RP1"
    assert result["nearest_lane_distance"] <= 0.25
    assert result["missing_critical_features"] == 0
    assert "run_id" not in result


def test_v133_no_lane_candidate_miner_classifies_near_s1p_l_before_outcome():
    result = classify_codex_v133_no_lane_candidate(
        {
            "symbol": "ETHUSDC",
            "strategy": "S1_BB_RSI",
            "side": "LONG",
            "score": 68.0,
            "d30": 0.0,
            "adv3": 0.0,
            "d3": 0.0,
            "d5": 0.0,
            "rsi": 46.0,
            "bb_lower_dist_bp": 15.0,
            "vwap_dist_bp": -2.0,
            "pullback_from_recent_high_bp": 15.0,
            "price_above_or_reclaimed_vwap": 0.0,
        },
        reason="no_codex_v1_lane_match",
    )

    assert result["candidate_bucket"] == "NL_NEAR_S1P_L_LONG"
    assert result["nearest_lane_code"] == "S1P-L"
    assert result["nearest_lane_distance"] <= 0.30
    assert result["missing_critical_features"] == 0


def test_v136_no_lane_candidate_miner_classifies_near_w1d_before_live_promotion():
    result = classify_codex_v133_no_lane_candidate(
        {
            "symbol": "ETHUSDC",
            "strategy": "S1_BB_RSI",
            "side": "LONG",
            "score": 66.0,
            "rng15": 90.0,
            "d30": 0.0,
            "adv3": 0.0,
            "range_bp": 4.0,
            "d3": 0.0,
            "d5": 0.0,
            "rsi": 55.0,
            "bb_lower_dist_bp": 20.0,
            "vwap_dist_bp": 4.0,
            "pullback_from_recent_high_bp": 4.0,
            "price_above_or_reclaimed_vwap": 1.0,
        },
        reason="no_codex_v1_lane_match",
    )

    assert result["candidate_bucket"] == "NL_NEAR_W1D_LONG"
    assert result["nearest_lane_code"] == "W1D"
    assert result["nearest_lane_distance"] <= 0.30
    assert result["missing_critical_features"] == 0


def test_v136_s1p_l_routing_respects_wait_gt180_shadow_block():
    features = {
        "symbol": "ETHUSDC",
        "strategy": "S1_BB_RSI",
        "side": "LONG",
        "score": 68.0,
        "d30": 0.0,
        "adv3": 0.0,
        "d3": 0.0,
        "d5": 0.0,
        "rsi": 46.0,
        "bb_lower_dist_bp": 10.0,
        "vwap_dist_bp": -2.0,
        "pullback_from_recent_high_bp": 15.0,
        "price_above_or_reclaimed_vwap": 0.0,
    }

    accepted = select_codex_v1_lane({**features, "reprice_wait_elapsed_seconds": 120.0})
    blocked = select_codex_v1_lane({**features, "reprice_wait_elapsed_seconds": 400.0})

    assert accepted.accepted
    assert accepted.lane_code == "S1P-L"
    assert accepted.reason == "s1p_l_match"
    assert accepted.requested_notional_usdc == 25.0
    assert accepted.policy_tag == "v149_s1pl_tiny_profile_fix"
    assert accepted.metrics["market_state"] == "S1P-L:ordinary_pullback_pre_vwap"
    assert accepted.metrics["applied_notional_cap_usdc"] == 25.0
    assert accepted.metrics["entry_bp"] == 0.0
    assert accepted.metrics["tp1_bp"] == 6.0
    assert accepted.metrics["partial_exit_pct"] == 1.0
    assert accepted.metrics["sl_bp"] == 15.0
    assert accepted.metrics["be_bp"] == 0.0
    assert accepted.metrics["ttl_s"] == 180
    assert "s1p_l_tiny_notional_25" in accepted.risk_tags
    assert "full_tp1" in accepted.risk_tags
    assert not blocked.accepted
    assert blocked.lane_code == "S1P-L"
    assert blocked.reason == "s1p_l_wait_gt180_block"
    assert blocked.shadow_lane == "SH_S1P_L_WAIT_GT180"
    assert blocked.policy_tag == "SH_S1P_L_WAIT_GT180"

def test_anchor_short_sizeup_lane_matches_current_baseline_w1_shape():
    decision = select_codex_v1_lane(
        {
            "symbol": "ETHUSDC",
            "strategy": "S1_BB_RSI",
            "side": "SHORT",
            "score": 60.8,
            "rng15": 36.9,
            "d30": 18.0,
            "adv3": 8.0,
        }
    )

    assert decision.accepted
    assert decision.lane_code == "ANCHOR-S"
    assert decision.lane == "anchor_s1_preblock_broad_su6_exitA"
    assert decision.entry_offset_bp == 3.0
    assert decision.size_mult == 6.0
    assert decision.requested_notional_usdc == 300.0
    assert "sizeup" in decision.risk_tags


def test_short_low_followthrough_block_rejects_w4_w7_bad_shape():
    decision = select_codex_v1_lane(
        {
            "symbol": "ETHUSDC",
            "strategy": "S1_BB_RSI",
            "side": "SHORT",
            "score": 61.2,
            "rng15": 27.2,
            "d30": 5.4,
            "adv3": 5.1,
        }
    )

    assert not decision.accepted
    assert decision.reason == "no_codex_v1_lane_match"


def test_reprice_lane_requires_shadow_features_and_respects_blocker():
    accepted = select_codex_v1_lane(
        {
            "symbol": "ETHUSDC",
            "strategy": "S1_BB_RSI",
            "side": "LONG",
            "score": 67.1,
            "rng15": 21.6,
            "d30": -9.4,
            "adv3": 16.7,
            "reprice_favorable_bp": 2.0,
            "reprice_adverse_bp": 1.0,
            "rsi": 38.0,
            "range_bp": 6.0,
            "ret3_bp": -20.0,
        }
    )
    blocked = select_codex_v1_lane(
        {
            "symbol": "ETHUSDC",
            "strategy": "S1_BB_RSI",
            "side": "LONG",
            "reprice_favorable_bp": 2.0,
            "reprice_adverse_bp": 1.0,
            "rsi": 33.0,
            "range_bp": 12.0,
            "ret3_bp": -12.0,
        }
    )

    assert accepted.accepted
    assert accepted.lane_code == "RP1"
    assert accepted.lane == "reprice_s1_longonly_w1_e1_longblock"
    assert accepted.entry_offset_bp == 1.0
    assert not blocked.accepted


def test_s6_cluster1129_requires_full_feature_filter_and_scales_rng55_75():
    decision = select_codex_v1_lane(
        {
            "symbol": "ETHUSDC",
            "strategy": "S6_TrendPull",
            "side": "SHORT",
            "rng15_bp": 60.0,
            "d30": -60.0,
            "adv3": 0.0,
            "range_bp": 10.0,
            "rsi": 48.0,
            "bb_lower_dist_bp": 30.0,
            "vwap_dist_bp": 50.0,
            "range_pos_15": 0.7,
        }
    )
    missing = select_codex_v1_lane(
        {
            "symbol": "ETHUSDC",
            "strategy": "S6_TrendPull",
            "side": "SHORT",
            "rng15_bp": 60.0,
            "d30": -60.0,
            "adv3": 0.0,
        }
    )

    assert decision.accepted
    assert decision.lane.endswith("cluster1129_rng57_70_d30neg75_neg49_rsi43_53_vwap38_56_rp15_063_080_e0")
    assert decision.notional_mult == 1.2
    assert decision.requested_notional_usdc == 60.0
    assert not missing.accepted


def test_s8_narrow_tail_lane_matches_precise_feature_shape():
    decision = select_codex_v1_lane(
        {
            "symbol": "ETHUSDC",
            "strategy": "S8_TrendSnipe",
            "side": "SHORT",
            "rng15": 33.0,
            "d30": -14.0,
            "adv3": 0.0,
            "range_bp": 6.0,
            "rsi": 48.0,
            "bb_lower_dist_bp": 16.0,
            "vwap_dist_bp": -6.0,
            "range_pos15": 0.6,
        }
    )

    assert decision.accepted
    assert decision.lane_code == "W3C"
    assert decision.lane == "w3_lane_s8short_bar2492_narrow_e0"
    assert decision.entry_offset_bp == 0.0


def test_lane_code_from_name_maps_window_codes_and_special_lanes():
    assert lane_code_from_name("w6_lane_s1long_rng38_86_range9_15_e0", side="LONG") == "W6A"
    assert lane_code_from_name("w6_lane_s6long_clusterB_vwap45_rp5_03_rp15_07_close08", side="LONG") == "W6B"
    assert lane_code_from_name("w6_lane_s8short_bar2432_score72_73_rng35_40_d30neg40_neg35_vwapneg95_neg85_e0", side="SHORT") == "W6C"
    assert lane_code_from_name("anchor_s1_preblock_broad_su6_exitA", side="LONG") == "ANCHOR-L"
    assert lane_code_from_name("anchor_s1_preblock_broad_su6_exitA", side="SHORT") == "ANCHOR-S"
    assert lane_code_from_name("reprice_s1_longonly_w1_e1_longblock", side="LONG") == "RP1"


def test_live_preflight_rejections_cover_known_live_hazards():
    rejects = live_preflight_rejections(
        {
            "spread_bp": 1.4,
            "feature_age_seconds": 90,
            "maker_fee_bp": 0.1,
            "open_position": True,
            "open_entry_order": True,
            "open_reduce_order": True,
            "kill_switch": True,
        }
    )

    assert "spread_gt_1bp" in rejects
    assert "features_stale_gt_75s" in rejects
    assert "maker_fee_not_zero" in rejects
    assert "open_position_exists" in rejects
    assert "open_entry_order_exists" in rejects
    assert "open_reduce_order_exists" in rejects
    assert "kill_switch_enabled" in rejects

    clean = live_preflight_rejections(
        {
            "spread_bp": 0.4,
            "feature_age_seconds": 10,
            "maker_fee_bp": 0,
            "open_position": "false",
            "open_entry_order": "0",
            "open_reduce_order": "",
            "kill_switch": "off",
        }
    )
    assert clean == ()


def test_codex_v1_telegram_report_marks_partial_live_payload_report_only():
    features = build_codex_v1_live_features(
        symbol="ETHUSDC",
        strategy="S6_TrendPull",
        side="SHORT",
        score=80.0,
        rng15=60.0,
        d30=-60.0,
        adv3=0.0,
    )

    gaps = codex_v1_feature_gaps(features)
    report = format_codex_v1_telegram_report(features)

    assert "range_bp" in gaps
    assert "vwap_dist_bp" in gaps
    assert "REJECT" in report
    assert "missing required features" in report
    assert "wired but disabled" in report
    assert "report-only" in report


def test_codex_v1_signal_overview_lists_lanes_and_live_rules():
    report = format_codex_v1_signal_overview(
        execution_wired=True,
        disabled_lane_names=("anchor_s1_preblock_broad_su6_exitA",),
        w6_weak_drift_block_enabled=True,
        w6_weak_drift_threshold_bp=-30.0,
        w2a_tight_block_enabled=True,
        w2a_d30_low_bp=-20.0,
        w2a_d30_high_bp=-5.0,
        w2a_adv3_low_bp=0.0,
        w2a_adv3_high_bp=6.0,
        w2a_bb_lower_dist_low_bp=5.0,
        w2a_bb_lower_dist_high_bp=20.0,
    )

    assert CODEX_V1_VERSION in report
    assert "開單原則" in report
    assert "不再顯示舊版即時判別摘要" in report
    assert "ANCHOR-L" in report
    assert "ANCHOR-S" in report
    assert "W6A" in report
    assert "W2A" in report
    assert "V1.3.7E risk tree" in report
    assert "risk&gt;=4 block" in report
    assert "stale below-VWAP cap50" in report
    assert "shadow-only" in report
    assert "active+block" in report
    assert "drift30" in report
    assert "-30" in report
    assert "bb_lower_dist" in report
    assert _telegram_html_tags(report) <= {"b", "/b", "code", "/code"}


def _telegram_html_tags(text: str) -> set[str]:
    return {match.group(1).split()[0] for match in re.finditer(r"<([^>\n]+)>", text)}
