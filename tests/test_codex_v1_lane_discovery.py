import pytest

from src.gridbot.strategy.codex_v1_live import (
    LANES,
    match_all_codex_v1_lanes,
    select_codex_v1_lane,
)


def _lane_codes(matches):
    return tuple(match.lane_code for match in matches)


def _band_witness_value(band):
    if band.low <= -10_000.0 and band.high >= 10_000.0:
        return 0.0
    if band.low <= -10_000.0:
        return band.high - 1.0
    if band.high >= 10_000.0:
        return band.low + 1.0
    return (band.low + band.high) / 2.0


def _lane_accepts_feature_value(lane, feature, value):
    required = (
        band
        for band in (*lane.bands, *lane.feature_bands)
        if band.feature == feature
    )
    return all(band.low <= value <= band.high for band in required)


def _move_outside_deny_rule(lane, features, deny_rule):
    if not deny_rule.matches(features):
        return

    required_features = {
        band.feature for band in (*lane.bands, *lane.feature_bands)
    }
    for band in deny_rule.bands:
        if band.feature not in required_features:
            features.pop(band.feature, None)
            return

        epsilon = max(1e-6, abs(band.low) * 1e-6)
        candidates = []
        if band.low > -10_000.0:
            candidates.append(band.low - epsilon)
        if band.high < 10_000.0:
            candidates.append(band.high + max(1e-6, abs(band.high) * 1e-6))
        for candidate in candidates:
            if _lane_accepts_feature_value(lane, band.feature, candidate):
                features[band.feature] = candidate
                return

    raise AssertionError(
        f"{lane.lane_code} predicate is fully contained by deny rule "
        f"{deny_rule.name}"
    )


def _synthetic_lane_witness(lane):
    features = {
        "symbol": "ETHUSDC",
        "strategy": lane.strategies[0],
        "side": lane.side,
    }
    for band in (*lane.bands, *lane.feature_bands):
        features[band.feature] = _band_witness_value(band)

    for deny_rule in lane.deny_rules:
        _move_outside_deny_rule(lane, features, deny_rule)
    assert not any(rule.matches(features) for rule in lane.deny_rules)

    if lane.lane_code == "HUE-L":
        features["d30"] = max(25.0, features["d30"])
        features["rsi"] = max(56.0, features["rsi"])
        features["vwap_dist_bp"] = max(20.0, features["vwap_dist_bp"])
        features["bb_lower_dist_bp"] = max(
            35.0,
            features["bb_lower_dist_bp"],
        )
    elif lane.lane_code == "S1P-L":
        features["reprice_wait_elapsed_seconds"] = 0.0
    return features


def test_base_lane_reachability_scope_is_the_frozen_24_lane_registry():
    assert len(LANES) == 24
    assert len({(lane.lane_code, lane.side) for lane in LANES}) == 24


@pytest.mark.parametrize("lane", LANES, ids=lambda lane: lane.lane_code)
def test_every_base_lane_has_a_synthetic_reachability_witness(lane):
    features = _synthetic_lane_witness(lane)

    matches = match_all_codex_v1_lanes(features)

    assert lane.lane_code in _lane_codes(matches), (
        f"{lane.lane_code} was unreachable from its own predicate midpoint; "
        f"matches={_lane_codes(matches)}, features={features}"
    )


def test_discovers_w2a_and_w6a_without_changing_paid_first_match():
    features = {
        "symbol": "ETHUSDC",
        "strategy": "S1_BB_RSI",
        "side": "LONG",
        "score": 70.0,
        "rng15": 40.0,
        "range_bp": 10.0,
    }

    matches = match_all_codex_v1_lanes(features)

    assert _lane_codes(matches) == ("W2A", "W6A")
    assert matches[0].side == "LONG"
    assert matches[0].strategy == "S1_BB_RSI"
    assert matches[0].lane == "w2_lane_s1long_score64_74_rng35_55_e0_block"
    assert matches[0].annotations == ("deny_rule_passed:s1_long_reprice_block",)

    paid = select_codex_v1_lane(features)
    assert paid.accepted
    assert paid.lane_code == "W2A"


def test_discovers_anchor_s_and_w1b_without_changing_paid_first_match():
    features = {
        "symbol": "ETHUSDC",
        "strategy": "S1_BB_RSI",
        "side": "SHORT",
        "score": 72.0,
        "rng15": 40.0,
        "d30": 0.0,
        "adv3": 0.0,
        "range_bp": 5.0,
    }

    matches = match_all_codex_v1_lanes(features)

    assert _lane_codes(matches) == ("ANCHOR-S", "W1B")
    assert matches[0].annotations == (
        "deny_rule_passed:short_low_followthrough_block",
    )

    paid = select_codex_v1_lane(features)
    assert paid.accepted
    assert paid.lane_code == "ANCHOR-S"


def test_match_set_is_registry_order_independent():
    features = {
        "symbol": "ETHUSDC",
        "strategy": "S1_BB_RSI",
        "side": "LONG",
        "score": 70.0,
        "rng15": 40.0,
        "range_bp": 10.0,
    }

    forward = match_all_codex_v1_lanes(features, lanes=LANES)
    reversed_order = match_all_codex_v1_lanes(
        features,
        lanes=tuple(reversed(LANES)),
    )

    assert _lane_codes(forward) == ("W2A", "W6A")
    assert _lane_codes(reversed_order) == ("W6A", "W2A")
    assert set(forward) == set(reversed_order)


def test_discovers_positive_sfd_special_builder_and_is_reorder_invariant():
    features = {
        "symbol": "ETHUSDC",
        "strategy": "S1_BB_RSI",
        "side": "SHORT",
        "rng15": 75.0,
        "d30": -40.0,
        "adv3": 4.0,
        "rsi": 45.0,
        "vwap_dist_bp": -12.0,
        "range_pos_15": 0.35,
    }

    forward = match_all_codex_v1_lanes(features, lanes=LANES)
    reversed_order = match_all_codex_v1_lanes(
        features,
        lanes=tuple(reversed(LANES)),
    )

    assert _lane_codes(forward) == ("SFD-S",)
    assert set(forward) == set(reversed_order)
    assert forward[0].regime == "SFD-S:strong_down_continuation"
    assert forward[0].annotations == (
        "positive_special_builder:build_strong_fall_follow_short_decision",
        "special_builder_outcome:accepted",
    )

    paid = select_codex_v1_lane(features)
    assert paid.accepted
    assert paid.lane_code == "SFD-S"


def test_discovers_positive_stup_special_builder_not_stale_veto_alone():
    features = {
        "symbol": "ETHUSDC",
        "strategy": "S1_BB_RSI",
        "side": "SHORT",
        "score": 50.0,
        "rng15": 30.0,
        "d30": 12.0,
        "adv3": 5.0,
        "rsi": 60.0,
        "vwap_dist_bp": 10.0,
        "range_bp": 4.0,
        "range_pos_15": 0.70,
        "pullback_from_recent_high_bp": 30.0,
        "reprice_wait_elapsed_seconds": 120.0,
    }

    matches = match_all_codex_v1_lanes(features)

    assert _lane_codes(matches) == ("STUP-S",)
    assert matches[0].regime == "STUP-S:clean_extension"
    assert matches[0].annotations == (
        "positive_special_builder:build_stale_upmove_canary_decision",
        "special_builder_outcome:accepted",
    )

    paid = select_codex_v1_lane(features)
    assert paid.accepted
    assert paid.lane_code == "STUP-S"

    veto_only = {
        "symbol": "ETHUSDC",
        "strategy": "S1_BB_RSI",
        "side": "SHORT",
        "adv3": 6.0,
        "reprice_wait_elapsed_seconds": 120.0,
    }
    assert match_all_codex_v1_lanes(veto_only) == ()
