import pytest

from src.gridbot.strategy.live_next.config import (
    EvaluationStage,
    FrozenReplayProtocol,
    FrozenSplitManifest,
    SplitRole,
    UTC_DAY_MS,
    build_candidate_factory,
)
from src.gridbot.strategy.live_next.contracts import ContractError


def test_default_manifest_is_exact_chronological_60_15_15_days():
    end_ms = 200 * UTC_DAY_MS
    manifest = FrozenSplitManifest.chronological_days(
        dataset_id="binance_ethusdc_90d_v1",
        source_kind="binance_public_aggtrades_klines",
        source_artifact_sha256="source_sha",
        end_ms=end_ms,
        created_at_ms=end_ms,
    )

    assert manifest.train.duration_days == 60
    assert manifest.validation.duration_days == 15
    assert manifest.holdout.duration_days == 15
    assert manifest.train.end_ms == manifest.validation.start_ms
    assert manifest.validation.end_ms == manifest.holdout.start_ms
    assert manifest.holdout.end_ms == end_ms
    assert manifest.to_dict()["manifest_hash"] == manifest.manifest_hash


def test_manifest_rejects_non_utc_boundary():
    with pytest.raises(ContractError, match="UTC day boundary"):
        FrozenSplitManifest.chronological_days(
            dataset_id="dataset",
            source_kind="aggtrades",
            source_artifact_sha256="source_sha",
            end_ms=100 * UTC_DAY_MS + 1,
            created_at_ms=0,
        )


def test_candidate_factory_is_deterministic_and_bounded_at_24():
    candidates = build_candidate_factory(
        expert_families=("impulse_retest", "range_reclaim", "trend_pullback", "shock_fade"),
        execution_profiles=("maker_fast", "maker_patient", "market_guarded"),
        exit_profiles=("tight_t1t2", "wide_t1t2"),
    )
    rerun = build_candidate_factory(
        expert_families=("impulse_retest", "range_reclaim", "trend_pullback", "shock_fade"),
        execution_profiles=("maker_fast", "maker_patient", "market_guarded"),
        exit_profiles=("tight_t1t2", "wide_t1t2"),
    )

    assert len(candidates) == 24
    assert [item.candidate_id for item in candidates] == [
        item.candidate_id for item in rerun
    ]
    assert len({item.candidate_id for item in candidates}) == 24

    with pytest.raises(ContractError, match="bounded limit 4"):
        build_candidate_factory(
            expert_families=("e1", "e2", "e3", "e4", "e5"),
            execution_profiles=("x1",),
            exit_profiles=("z1",),
        )


def test_split_access_is_stage_locked_and_holdout_requires_frozen_portfolio():
    train = FrozenReplayProtocol(
        stage="TRAIN",
        split_manifest_hash="split_sha",
        candidate_ids=("candidate_1",),
        feature_config_hash="feature_sha",
        cost_model_hash="cost_sha",
    )
    train.assert_partition_access(SplitRole.TRAIN)
    with pytest.raises(ContractError, match="cannot evaluate HOLDOUT"):
        train.assert_partition_access(SplitRole.HOLDOUT)

    with pytest.raises(ContractError, match="frozen portfolio"):
        FrozenReplayProtocol(
            stage=EvaluationStage.HOLDOUT,
            split_manifest_hash="split_sha",
            candidate_ids=("candidate_1",),
            feature_config_hash="feature_sha",
            cost_model_hash="cost_sha",
        )

    holdout = FrozenReplayProtocol(
        stage="HOLDOUT",
        split_manifest_hash="split_sha",
        candidate_ids=("candidate_1", "candidate_2"),
        feature_config_hash="feature_sha",
        cost_model_hash="cost_sha",
        portfolio_config_hash="portfolio_sha",
    )
    holdout.assert_partition_access("HOLDOUT")
    assert holdout.protocol_hash == holdout.protocol_hash
