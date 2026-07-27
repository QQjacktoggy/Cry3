from __future__ import annotations

import dataclasses

import pytest

from src.gridbot.mainnet.v1462_lane_registry import (
    LANES,
    LANE_BY_CODE,
    LEGACY_LANE_REGISTRY,
    MONITOR_LANE_REGISTRY,
    REGISTRY_HASH,
    LaneMode,
    lane_definition_hash,
    lane_for,
    monitor_rows,
    registry_hash,
    registry_payload,
    state_mode,
)


EXPECTED_CODES = {
    "HUE-L",
    "ANCHOR-L",
    "ANCHOR-S",
    "RP1",
    "S1P-L",
    "W2A",
    "W3A",
    "W6A",
    "W4A",
    "W3B",
    "W6B",
    "W4B",
    "W1C",
    "W1D",
    "W5A",
    "W1A",
    "W5B",
    "W7A",
    "W1B",
    "W2B",
    "W6C",
    "W2C",
    "W3C",
    "W1E",
    "STUP-S",
    "SFD-S",
    "CNL-WPR-L",
}


def test_registry_has_all_27_unique_legacy_lanes() -> None:
    assert len(LANES) == 27
    assert len(LANE_BY_CODE) == 27
    assert set(LANE_BY_CODE) == EXPECTED_CODES


def test_frozen_metadata_preserves_exact_strategy_side_entry_and_guards() -> None:
    hue = lane_for("hue-l")
    assert hue.rule_name == "codex_v1_hot_up_extension_pullback_long"
    assert hue.strategies == ("S1_BB_RSI", "S5_Stoch", "S6_TrendPull")
    assert hue.classifier_side == "LONG"
    assert hue.entry_offset_bp == 0.0
    assert hue.base_size_mult == 0.35
    assert hue.scale_rng_low_bp is None

    anchor_short = lane_for("ANCHOR-S")
    assert anchor_short.rule_name == "anchor_s1_preblock_broad_su6_exitA"
    assert anchor_short.entry_offset_bp == 3.0
    assert anchor_short.deny_rules[0].name == "short_low_followthrough_block"

    rp1 = lane_for("RP1")
    assert rp1.strategies == ("S1_BB_RSI",)
    assert rp1.classifier_side == "LONG"
    assert rp1.entry_offset_bp == 1.0
    assert rp1.deny_rules[0].name == "s1_long_reprice_block"

    w6c = lane_for("W6C")
    assert w6c.strategies == ("S8_TrendSnipe",)
    assert w6c.classifier_side == "SHORT"
    assert {band.feature for band in (*w6c.bands, *w6c.feature_bands)} == {
        "score",
        "rng15",
        "d30",
        "adv3",
        "range_bp",
        "rsi",
        "bb_lower_dist_bp",
        "vwap_dist_bp",
        "range_pos_15",
    }


def test_initial_allowlist_is_narrow_and_state_gated_families_are_mixed() -> None:
    live = {lane.lane_code for lane in LANES if lane.intended_mode is LaneMode.LIVE_ALLOWLIST}
    mixed = {lane.lane_code for lane in LANES if lane.intended_mode is LaneMode.MIXED}
    assert live == {"RP1", "S1P-L"}
    assert mixed == {"STUP-S", "CNL-WPR-L"}
    assert lane_for("SFD-S").intended_mode is LaneMode.SHADOW_ONLY
    assert all(lane.intended_mode is LaneMode.SHADOW_ONLY for lane in LANES if lane.lane_code.startswith("W"))


def test_state_mode_preserves_v1460_control_but_admission_must_still_fail_closed() -> None:
    assert state_mode("STUP-S", "STUP-S:clean_extension") is LaneMode.LIVE_ALLOWLIST
    assert state_mode("STUP-S", "mixed") is LaneMode.SHADOW_ONLY
    # v1.4.60 includes hot_continuation in STUP_CLEAN_STATES.  This registry
    # preserves that matrix fact; v1.4.62 admission additionally requires raw
    # acceptance and an unbroken reject lineage before routing LIVE.
    assert state_mode("STUP-S", "hot_continuation") is LaneMode.LIVE_ALLOWLIST
    hot = next(item for item in lane_for("STUP-S").state_profiles if item.state == "hot_continuation")
    assert "reject->reopen" in hot.gate_note
    assert state_mode("STUP-S", "new_unreviewed_state") is LaneMode.SHADOW_ONLY

    assert state_mode("CNL-WPR-L", "fast_reclaim") is LaneMode.LIVE_ALLOWLIST
    assert state_mode("CNL-WPR-L", "discount_delayed_reclaim") is LaneMode.LIVE_ALLOWLIST
    assert state_mode("CNL-WPR-L", "falling_discount_trap") is LaneMode.SHADOW_ONLY
    assert state_mode("CNL-WPR-L", None) is LaneMode.SHADOW_ONLY


def test_classifier_and_effective_sides_are_not_conflated() -> None:
    stup = lane_for("STUP-S")
    cnl = lane_for("CNL-WPR-L")
    assert stup.classifier_side == "SHORT"
    assert stup.effective_sides == ("SHORT", "LONG")
    assert cnl.classifier_side == "LONG"
    assert cnl.effective_sides == ("LONG", "SHORT")


def test_profiles_preserve_known_base_execution_parameters() -> None:
    s1p = lane_for("S1P-L").default_profile
    assert s1p is not None
    assert (s1p.entry_bp, s1p.tp1_bp, s1p.sl_bp, s1p.ttl_s) == (0.0, 6.0, 15.0, 180)

    sfd = lane_for("SFD-S").default_profile
    assert sfd is not None
    assert (sfd.entry_bp, sfd.tp1_bp, sfd.full_tp_bp, sfd.sl_bp, sfd.ttl_s) == (2, 6, 8, 10, 90)

    cnl = {item.state: item.profile for item in lane_for("CNL-WPR-L").state_profiles}
    assert cnl["fast_reclaim"] is not None
    assert (cnl["fast_reclaim"].entry_bp, cnl["fast_reclaim"].tp1_bp, cnl["fast_reclaim"].sl_bp) == (0, 6, 6)


def test_registry_hash_is_deterministic_and_definitions_are_immutable() -> None:
    assert registry_hash() == REGISTRY_HASH
    assert len(REGISTRY_HASH) == 64
    assert len({lane_definition_hash(lane) for lane in LANES}) == 27
    with pytest.raises(dataclasses.FrozenInstanceError):
        lane_for("RP1").entry_offset_bp = 0.0  # type: ignore[misc]


def test_monitor_left_join_always_starts_with_zero_rows_for_all_lanes() -> None:
    rows = monitor_rows()
    assert len(rows) == 27
    assert {row["lane_code"] for row in rows} == EXPECTED_CODES
    assert all(row["captured"] == row["complete"] == row["incomplete"] == 0 for row in rows)
    assert all(len(row["definition_hash"]) == 64 for row in rows)
    assert lane_for("STUP-S").intended_mode is LaneMode.MIXED


def test_monitor_payload_alias_is_json_safe() -> None:
    assert len(LEGACY_LANE_REGISTRY) == 27
    assert MONITOR_LANE_REGISTRY is LEGACY_LANE_REGISTRY
    assert registry_payload() is LEGACY_LANE_REGISTRY
    stup = next(row for row in LEGACY_LANE_REGISTRY if row["lane_code"] == "STUP-S")
    assert stup["intended_mode"] == "MIXED"
    assert stup["state_profiles"][0]["mode"] == "LIVE_ALLOWLIST"


def test_unknown_lane_fails_closed() -> None:
    with pytest.raises(KeyError, match="unknown v1.4.63 lane"):
        lane_for("NEW-LANE")
