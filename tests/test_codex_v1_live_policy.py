import re

from src.gridbot.strategy.codex_v1_live import (
    CODEX_V1_VERSION,
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
    assert CODEX_V1_VERSION == "_codex_v1.4.0"



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
