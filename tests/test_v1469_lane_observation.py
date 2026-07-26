from dataclasses import replace

from src.gridbot.mainnet.v1469_lane_observation import (
    V1469_MATCHER_HASH,
    _matcher_contract,
    build_v1469_lane_observation,
)
from src.gridbot.strategy.codex_v1_live import (
    CODEX_V1_BASELINE,
    CODEX_V1_VERSION,
    CodexV1Decision,
    select_codex_v1_lane,
)


def _features() -> dict:
    return {
        "symbol": "ETHUSDC",
        "strategy": "S1_BB_RSI",
        "side": "LONG",
        "score": 70.0,
        "rng15": 40.0,
        "range_bp": 10.0,
        "feature_age_seconds": 0.0,
    }


def _decision(
    *,
    lane_code: str | None,
    lane: str | None,
    accepted: bool,
    reason: str,
    side: str = "LONG",
    strategy: str = "S1_BB_RSI",
    regime: str | None = None,
) -> CodexV1Decision:
    return CodexV1Decision(
        accepted=accepted,
        version=CODEX_V1_VERSION,
        baseline=CODEX_V1_BASELINE,
        lane=lane,
        lane_code=lane_code,
        strategy=strategy,
        side=side,
        entry_offset_bp=0.0,
        size_mult=1.0 if accepted else 0.0,
        notional_mult=1.0 if accepted else 0.0,
        requested_notional_usdc=50.0 if accepted else 0.0,
        reason=reason,
        regime=regime,
    )


def test_matcher_identity_contains_only_sorted_predicate_semantics() -> None:
    contract = _matcher_contract()
    lane_rows = contract["base_lanes"]

    assert [row["lane_code"] for row in lane_rows] == sorted(
        row["lane_code"] for row in lane_rows
    )
    assert all("notes" not in row for row in lane_rows)
    assert all("entry_offset_bp" not in row for row in lane_rows)
    assert all("base_size_mult" not in row for row in lane_rows)
    assert "codex_version" not in contract


def test_selected_then_blocked_lane_remains_visible_and_suppresses_overlap() -> None:
    features = _features()
    selector = select_codex_v1_lane(features)
    effective = replace(
        selector,
        accepted=False,
        reason="codex_v1_lane_disabled",
        size_mult=0.0,
        notional_mult=0.0,
        requested_notional_usdc=0.0,
    )

    batch = build_v1469_lane_observation(
        environment="MAINNET",
        run_id="cry3mn-1",
        observed_at_ms=120_001,
        bucket_seconds=120,
        features=features,
        feature_snapshot=features,
        selector_decision=selector,
        effective_decision=effective,
    )

    by_lane = {item["lane_code"]: item for item in batch.candidates}
    assert set(by_lane) == {"W2A", "W6A"}
    assert by_lane["W2A"]["is_selected"] is True
    assert by_lane["W2A"]["safety_status"] == "NOT_EVALUATED"
    assert by_lane["W6A"]["is_selected"] is False
    assert by_lane["W6A"]["safety_status"] == "NOT_EVALUATED"
    assert by_lane["W6A"]["suppression_reason"] == "LEGACY_FIRST_MATCH"
    assert by_lane["W6A"]["suppressed_by_lane_code"] == "W2A"
    assert batch.opportunity["feature_snapshot"]["matcher_hash"] == V1469_MATCHER_HASH


def test_async_cnl_selector_owner_is_recorded_outside_pure_matcher() -> None:
    features = {
        "symbol": "ETHUSDC",
        "strategy": "S1_BB_RSI",
        "side": "LONG",
        "score": 61.0,
    }
    cnl = _decision(
        lane_code="CNL-WPR-L",
        lane="codex_v139_wpr_reclaim_long",
        accepted=True,
        reason="v139_wpr_reclaim_canary",
        regime="CNL-WPR-L:fast_reclaim",
    )

    batch = build_v1469_lane_observation(
        environment="MAINNET",
        run_id="cry3mn-cnl",
        observed_at_ms=240_001,
        bucket_seconds=120,
        features=features,
        feature_snapshot=features,
        selector_decision=cnl,
        effective_decision=cnl,
        feature_gaps=("rng15",),
    )

    assert len(batch.candidates) == 1
    candidate = batch.candidates[0]
    assert candidate["lane_code"] == "CNL-WPR-L"
    assert candidate["is_selected"] is True
    assert "async_route_boundary:cnl_wpr" in candidate["annotations"][
        "matcher_annotations"
    ]
    assert batch.opportunity["coarse_regime"] == "TREND_UP"


def test_bucket_dedup_key_is_stable_but_snapshot_ids_remain_immutable() -> None:
    features = _features()
    selector = select_codex_v1_lane(features)
    first = build_v1469_lane_observation(
        environment="MAINNET",
        run_id="cry3mn-bucket",
        observed_at_ms=360_001,
        bucket_seconds=120,
        features=features,
        feature_snapshot=features,
        selector_decision=selector,
        effective_decision=selector,
    )
    second = build_v1469_lane_observation(
        environment="MAINNET",
        run_id="cry3mn-bucket",
        observed_at_ms=479_999,
        bucket_seconds=120,
        features=features,
        feature_snapshot=features,
        selector_decision=selector,
        effective_decision=selector,
    )

    assert first.opportunity_id != second.opportunity_id
    assert first.dedup_key == second.dedup_key


def test_legacy_owner_change_does_not_change_market_opportunity_identity() -> None:
    features = _features()
    first_owner = select_codex_v1_lane(features)
    second_owner = replace(
        first_owner,
        lane_code="W6A",
        lane="w6_lane_s1long_rng38_86_range9_15_e0",
    )
    common = {
        "environment": "MAINNET",
        "run_id": "cry3mn-owner-reorder",
        "observed_at_ms": 500_001,
        "bucket_seconds": 120,
        "features": features,
        "feature_snapshot": features,
    }

    first = build_v1469_lane_observation(
        **common,
        selector_decision=first_owner,
        effective_decision=first_owner,
    )
    second = build_v1469_lane_observation(
        **common,
        selector_decision=second_owner,
        effective_decision=second_owner,
    )

    assert first.dedup_key == second.dedup_key
    assert first.opportunity_id == second.opportunity_id
    assert [
        (item["lane_code"], item["selection_rank"])
        for item in first.candidates
    ] == [
        (item["lane_code"], item["selection_rank"])
        for item in second.candidates
    ]


def test_feature_gaps_fail_closed_even_when_a_lane_matches() -> None:
    features = _features()
    selector = select_codex_v1_lane(features)

    batch = build_v1469_lane_observation(
        environment="MAINNET",
        run_id="cry3mn-incomplete",
        observed_at_ms=600_001,
        bucket_seconds=120,
        features=features,
        feature_snapshot=features,
        selector_decision=selector,
        effective_decision=selector,
        feature_gaps=("rsi", "d30"),
    )

    assert batch.opportunity["data_quality"] == "DATA_INCOMPLETE"
    assert batch.opportunity["feature_snapshot"]["feature_gaps"] == [
        "d30",
        "rsi",
    ]
    assert all(candidate["data_complete"] is False for candidate in batch.candidates)


def test_zero_match_opportunity_keeps_a_diagnostic_reason() -> None:
    features = {
        "symbol": "ETHUSDC",
        "strategy": "S1_BB_RSI",
        "side": "LONG",
        "score": 1.0,
    }
    no_match = _decision(
        lane_code=None,
        lane=None,
        accepted=False,
        reason="no_codex_v1_lane_match",
    )

    batch = build_v1469_lane_observation(
        environment="MAINNET",
        run_id="cry3mn-no-match",
        observed_at_ms=720_001,
        bucket_seconds=120,
        features=features,
        feature_snapshot=features,
        selector_decision=no_match,
        effective_decision=no_match,
        feature_gaps=("rng15",),
    )

    assert batch.candidates == ()
    assert batch.opportunity["data_quality"] == "DATA_INCOMPLETE"
    assert (
        batch.opportunity["feature_snapshot"]["zero_match_reason"]
        == "FEATURES_INCOMPLETE"
    )
