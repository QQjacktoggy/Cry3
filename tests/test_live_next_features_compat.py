from dataclasses import FrozenInstanceError

import pytest

from src.gridbot.strategy.live_next.contracts import (
    ContractError,
    canonical_sha256,
)
from src.gridbot.strategy.live_next.features import (
    CausalityViolation,
    FeatureLeakageError,
    FeatureObservation,
    FeatureRole,
    FeatureSnapshot,
    OutcomeLeakageError,
    build_feature_snapshot,
)


def test_advanced_snapshot_is_deterministic_immutable_and_conservative() -> None:
    observations = {
        "trend_score": FeatureObservation(0.8, 90, 96),
        "range_score": FeatureObservation(0.2, 95, 98),
    }
    snapshot = build_feature_snapshot(
        decision_time_ms=100,
        observations=observations,
        quality_flags=("book_ok", "book_ok"),
        feature_version="features_v2",
    )
    reversed_snapshot = build_feature_snapshot(
        decision_time_ms=100,
        observations=dict(reversed(tuple(observations.items()))),
        quality_flags=("book_ok",),
        feature_version="features_v2",
    )

    observations["trend_score"] = FeatureObservation(-1.0, 100, 100)

    assert tuple(snapshot.features) == ("range_score", "trend_score")
    assert snapshot.values == {"range_score": 0.2, "trend_score": 0.8}
    assert snapshot.observation("trend_score").value == 0.8
    assert snapshot.event_time_ms == 95
    assert snapshot.available_at_ms == 98
    assert snapshot.quality_flags == ("book_ok",)
    assert snapshot.feature_snapshot_id == reversed_snapshot.feature_snapshot_id
    assert snapshot.is_stale(
        9,
        feature_names=("trend_score", "range_score"),
    )
    assert not snapshot.is_stale(
        10,
        feature_names=("trend_score", "range_score"),
    )
    with pytest.raises(TypeError):
        snapshot.features["new"] = FeatureObservation(0.0, 100, 100)  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        snapshot.observation("trend_score").event_time_ms = 100  # type: ignore[misc]


def test_legacy_create_preserves_aliases_and_canonical_hashing() -> None:
    snapshot = FeatureSnapshot.create(
        observed_at_ms=100,
        market_data_max_event_ms=90,
        values={"z": 2.0, "a": {"velocity": 1.5}},
        feature_version="legacy_v1",
    )
    reordered = FeatureSnapshot.create(
        observed_at_ms=100,
        market_data_max_event_ms=90,
        values={"a": {"velocity": 1.5}, "z": 2.0},
        feature_version="legacy_v1",
    )

    assert snapshot.observed_at_ms == snapshot.decision_time_ms == 100
    assert snapshot.market_data_max_event_ms == snapshot.event_time_ms == 90
    assert snapshot.available_at_ms == 100
    assert snapshot.values_json == '{"a":{"velocity":1.5},"z":2.0}'
    assert snapshot.feature_hash == canonical_sha256(snapshot.values)
    assert snapshot.feature_hash == reordered.feature_hash
    assert snapshot.feature_snapshot_id == reordered.feature_snapshot_id
    assert snapshot.data_age_ms == 10
    assert {
        (item.event_time_ms, item.available_at_ms, item.role)
        for item in snapshot.features.values()
    } == {(90, 100, FeatureRole.INPUT)}
    snapshot.assert_usable_at(100)


def test_advanced_and_legacy_paths_fail_closed_on_leakage() -> None:
    with pytest.raises(CausalityViolation, match="event_time_ms"):
        FeatureObservation(1.0, event_time_ms=101, available_at_ms=100)
    with pytest.raises(CausalityViolation, match="after decision time"):
        FeatureSnapshot(
            decision_time_ms=100,
            features={"signal": FeatureObservation(1.0, 99, 101)},
        )
    with pytest.raises(OutcomeLeakageError):
        FeatureSnapshot(
            decision_time_ms=100,
            features={
                "signal": FeatureObservation(
                    1.0,
                    90,
                    95,
                    role=FeatureRole.OUTCOME,
                )
            },
        )
    with pytest.raises(OutcomeLeakageError, match="net_pnl_usdc"):
        FeatureSnapshot.create(
            observed_at_ms=100,
            market_data_max_event_ms=90,
            values={"nested": {"net_pnl_usdc": 1.0}},
            feature_version="legacy_v1",
        )
    with pytest.raises(ContractError, match="non-finite"):
        FeatureObservation(float("nan"), 90, 95)

    allowed = FeatureSnapshot(
        decision_time_ms=100,
        features={"exit_profile_id": FeatureObservation("tp_sl_v1", 90, 95)},
    )
    assert allowed.values["exit_profile_id"] == "tp_sl_v1"
    assert issubclass(CausalityViolation, FeatureLeakageError)
    assert issubclass(OutcomeLeakageError, FeatureLeakageError)
