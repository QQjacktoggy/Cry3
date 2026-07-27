from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest

from src.gridbot.mainnet.v1462_lane_registry import (
    REGISTRY_HASH,
    REGISTRY_VERSION,
    lane_definition_hash,
    lane_for,
)
from src.gridbot.mainnet.v1464_adaptive_promotion import (
    AdaptivePromotionConfig,
    PromotionRiskInput,
    PromotionState,
)
from src.gridbot.mainnet.v1464_promotion_runtime import (
    PromotionRegimeSnapshot,
    V1464PromotionRuntime,
    adaptive_promotion_config_from_settings,
    aggregate_promotion_evidence,
    derive_regime_input,
    project_paid_terminal_evidence,
    promotion_cohort_from_identity,
)
from src.gridbot.storage.v1464_promotion_repository import (
    AdmissionClaimError,
    LeaseConflictError,
    PromotionCohort,
    promotion_cohort_key,
)


NOW = 20_000_000
CONFIG = AdaptivePromotionConfig()


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _cohort(**changes: object) -> PromotionCohort:
    values = {
        "environment": "MAINNET",
        "symbol": "ETHUSDC",
        "lane_code": "W6A",
        "market_state": "supportive_range",
        "effective_side": "LONG",
        "strategy": "S1_BB_RSI",
        "resolved_profile_hash": "profile-a",
        "profile_identity_schema": CONFIG.profile_schema,
        "registry_version": REGISTRY_VERSION,
        "registry_hash": REGISTRY_HASH,
        "lane_definition_hash": lane_definition_hash(lane_for("W6A")),
        "admission_policy_hash": "admission-a",
        "promotion_policy_hash": CONFIG.policy_hash,
    }
    values.update(changes)
    return PromotionCohort(**values)


def _identity(cohort: PromotionCohort | None = None) -> dict:
    active = cohort or _cohort()
    return {
        "environment": active.environment,
        "symbol": active.symbol,
        "lane_code": active.lane_code,
        "market_state": active.market_state,
        "effective_side": active.effective_side,
        "strategy": active.strategy,
        "resolved_profile_hash": active.resolved_profile_hash,
        "profile_identity_schema": active.profile_identity_schema,
        "registry_version": active.registry_version,
        "registry_hash": active.registry_hash,
        "lane_definition_hash": active.lane_definition_hash,
        "admission_policy_hash": active.admission_policy_hash,
    }


def _row(
    index: int,
    outcome: str,
    *,
    cohort: PromotionCohort | None = None,
    source_type: str = "SHADOW",
    complete: bool = True,
    diagnostic: bool = False,
    pnl: float | None = None,
    source_payload: dict | None = None,
) -> dict:
    active = cohort or _cohort()
    if pnl is None and complete:
        pnl = 0.08 if outcome in {"tp1_first", "tp_first", "tp"} else -0.04
    observed = NOW - 60_000 + index * 1_000
    payload = {
        "opportunity_id": f"{source_type.lower()}-{index}",
        **_identity(active),
        "evidence_schema_version": CONFIG.evidence_contract_version,
        "observed_at_ms": observed,
        "terminal_at_ms": observed + 500,
        "outcome": outcome,
        "data_complete": complete,
        "ambiguous": outcome == "ambiguous_both",
        "diagnostic_only": diagnostic,
        "net_pnl_usdc": pnl,
        "source_type": source_type,
        "source_id": f"{source_type.lower()}:{index}",
        "source_payload": dict(source_payload or {}),
    }
    payload["evidence_hash"] = _canonical_hash(payload)
    return payload


def _shadow_floor_rows() -> list[dict]:
    return [
        _row(1, "tp1_first"),
        _row(2, "sl_first"),
        _row(3, "tp1_first"),
        _row(4, "tp1_first"),
    ]


def _regime(
    *,
    cohort: PromotionCohort | None = None,
    confirmations: int = 2,
    observed_at_ms: int = NOW - 100,
    supportive: bool = True,
    **changes: object,
) -> PromotionRegimeSnapshot:
    active = cohort or _cohort()
    times = tuple(observed_at_ms - (confirmations - index - 1) * 1_000 for index in range(confirmations))
    values = {
        **_identity(active),
        "observed_at_ms": observed_at_ms,
        "supportive": supportive,
        "confirmations": confirmations,
        "confirmation_observed_at_ms": times,
        "confirmation_cohort_keys": tuple(active.key for _ in times),
    }
    values.update(changes)
    return PromotionRegimeSnapshot(**values)


def _details(**changes: object) -> dict:
    values = {
        **_identity(),
        "registry_version": REGISTRY_VERSION,
        "classifier_side": "LONG",
        "opportunity_id": "opp-1",
        "sample_id": "sample-1",
        "observed_at_ms": NOW - 2_000,
        "terminal_at_ms": NOW - 1_000,
        "shadow_outcome": "tp1_first",
        "data_complete": True,
        "evidence_source": "binance_aggTrade",
        "fill_model": "limit_touch",
        "paper_pnl_usdc_after_fee": 0.08,
        "promotion_eligible": True,
        "promotion_counts_as": "tp_success",
    }
    values.update(changes)
    return values


class FakeRepository:
    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = list(rows or [])
        self.lease: dict | None = None
        self.calls: list[tuple[str, object]] = []
        self.fail_reads = False
        self.fail_mutations = False
        self.claims: dict[str, dict] = {}

    async def upsert_evidence(self, payload):
        self.calls.append(("upsert_evidence", payload))
        if self.fail_mutations:
            raise LeaseConflictError("evidence conflict")
        for existing in self.rows:
            if existing.get("opportunity_id") == payload.get("opportunity_id"):
                if existing == dict(payload):
                    return False
                raise LeaseConflictError("conflicting immutable evidence")
        self.rows.append(dict(payload))
        return True

    async def list_sliding_evidence(self, cohort, **kwargs):
        self.calls.append(("list_sliding_evidence", kwargs))
        if self.fail_reads:
            raise RuntimeError("database unavailable")
        expected = _identity(cohort)
        return [
            dict(row)
            for row in self.rows
            if all(row.get(name) == value for name, value in expected.items())
        ]

    async def list_lane_paid_evidence(
        self,
        *,
        environment,
        symbol,
        lane_code,
        **kwargs,
    ):
        self.calls.append(("list_lane_paid_evidence", lane_code))
        if self.fail_reads:
            raise RuntimeError("database unavailable")
        return [
            dict(row)
            for row in self.rows
            if row.get("environment") == environment
            and row.get("symbol") == symbol
            and row.get("lane_code") == lane_code
            and row.get("source_type") == "PAID"
            and row.get("data_complete") is True
            and not row.get("ambiguous")
            and not row.get("diagnostic_only")
            and row.get("net_pnl_usdc") is not None
        ]

    async def get_lease(self, cohort):
        self.calls.append(("get_lease", cohort))
        if self.fail_reads:
            raise RuntimeError("database unavailable")
        return dict(self.lease) if self.lease else None

    async def upsert_lease(
        self,
        lease,
        *,
        expected_generation,
        event_type,
        event_time_ms,
        **kwargs,
    ):
        self.calls.append(("upsert_lease", event_type))
        if self.fail_mutations:
            raise LeaseConflictError("lost CAS")
        actual = int(self.lease["generation"]) if self.lease else None
        if actual != expected_generation:
            raise LeaseConflictError("generation mismatch")
        snapshot = dict(lease["evidence_snapshot"])
        row = {
            **dict(lease),
            "cohort_key": promotion_cohort_key(lease),
            "generation": 1 if actual is None else actual + 1,
            "evidence_snapshot": snapshot,
            "evidence_snapshot_hash": _canonical_hash(snapshot),
            "created_at_ms": (
                event_time_ms if self.lease is None else self.lease["created_at_ms"]
            ),
            "updated_at_ms": event_time_ms,
        }
        self.lease = row
        return dict(row)

    async def demote_lease(
        self,
        cohort_key,
        *,
        expected_generation,
        reason,
        event_time_ms,
        **kwargs,
    ):
        self.calls.append(("demote_lease", reason))
        if self.fail_mutations:
            raise LeaseConflictError("lost CAS")
        assert self.lease is not None
        if self.lease["generation"] != expected_generation:
            raise LeaseConflictError("generation mismatch")
        self.lease = {
            **self.lease,
            "status": "DEMOTED",
            "generation": expected_generation + 1,
            "demotion_reason": reason,
            "demoted_at_ms": event_time_ms,
        }
        return dict(self.lease)

    async def expire_lease(
        self,
        cohort_key,
        *,
        expected_generation,
        now_ms,
        **kwargs,
    ):
        self.calls.append(("expire_lease", cohort_key))
        if self.fail_mutations:
            raise LeaseConflictError("lost CAS")
        assert self.lease is not None
        if self.lease["generation"] != expected_generation:
            raise LeaseConflictError("generation mismatch")
        self.lease = {
            **self.lease,
            "status": "EXPIRED",
            "generation": expected_generation + 1,
            "demotion_reason": "lease_expired",
            "demoted_at_ms": now_ms,
        }
        return dict(self.lease)

    async def cooldown_lease(
        self,
        cohort_key,
        *,
        expected_generation,
        reason,
        event_time_ms,
        cooldown_until_ms,
        **kwargs,
    ):
        assert self.lease is not None
        if self.lease["generation"] != expected_generation:
            raise LeaseConflictError("generation mismatch")
        self.calls.append(("cooldown_lease", reason))
        self.lease = {
            **self.lease,
            "status": "COOLDOWN",
            "generation": expected_generation + 1,
            "demotion_reason": reason,
            "demoted_at_ms": event_time_ms,
            "cooldown_until_ms": cooldown_until_ms,
        }
        return dict(self.lease)

    async def halt_lease(
        self,
        cohort_key,
        *,
        expected_generation,
        reason,
        event_time_ms,
        **kwargs,
    ):
        assert self.lease is not None
        if self.lease["generation"] != expected_generation:
            raise LeaseConflictError("generation mismatch")
        self.calls.append(("halt_lease", reason))
        self.lease = {
            **self.lease,
            "status": "HALTED",
            "generation": expected_generation + 1,
            "demotion_reason": reason,
            "demoted_at_ms": event_time_ms,
            "cooldown_until_ms": None,
        }
        return dict(self.lease)

    async def upsert_guard_state(
        self,
        lease,
        *,
        expected_generation,
        status,
        reason,
        event_time_ms,
        cooldown_until_ms,
        **kwargs,
    ):
        self.calls.append(("upsert_guard_state", status))
        row = await self.upsert_lease(
            lease,
            expected_generation=expected_generation,
            event_type=status,
            event_time_ms=event_time_ms,
        )
        self.lease = {
            **row,
            "status": status,
            "demotion_reason": reason,
            "demoted_at_ms": event_time_ms,
            "cooldown_until_ms": cooldown_until_ms,
        }
        return dict(self.lease)

    async def claim_admission(
        self,
        cohort_key,
        *,
        lease_id,
        expected_generation,
        current_identity,
        now_ms,
        actual_notional_usdc,
        idempotency_key,
        actor,
    ):
        self.calls.append(("claim_admission", idempotency_key))
        if idempotency_key in self.claims:
            return {
                **self.claims[idempotency_key],
                "claim_granted": False,
                "claim_replayed": True,
            }
        if self.lease is None:
            raise AdmissionClaimError("lease_missing")
        if (
            self.lease["cohort_key"] != cohort_key
            or self.lease["lease_id"] != lease_id
            or self.lease["generation"] != expected_generation
            or self.lease["status"] != "ACTIVE"
            or self.lease["expires_at_ms"] <= now_ms
            or self.lease["notional_cap_usdc"] < actual_notional_usdc
        ):
            raise AdmissionClaimError("admission_claim_cas_lost")
        self.lease = {
            **self.lease,
            "generation": expected_generation + 1,
            "updated_at_ms": now_ms,
        }
        claimed = {
            **self.lease,
            "claim_granted": True,
            "claim_replayed": False,
            "claim_generation": expected_generation + 1,
        }
        self.claims[idempotency_key] = dict(claimed)
        return claimed


def _runtime(repository: FakeRepository, **changes: object) -> V1464PromotionRuntime:
    values = {
        "config": CONFIG,
        "boot_id": "boot-1",
        "owner_id": "worker-1",
        "enabled": True,
        "regime_confirmations": 2,
    }
    values.update(changes)
    return V1464PromotionRuntime(repository, **values)


def test_config_and_cohort_are_built_from_settings() -> None:
    settings = SimpleNamespace(
        mainnet_codex_v1464_evidence_window_seconds=5_000,
        mainnet_codex_v1464_evidence_max_age_seconds=4_000,
        mainnet_codex_v1464_lease_ttl_seconds=600,
        mainnet_codex_v1464_probation_notional_usdc=20.0,
        mainnet_codex_v1464_live_notional_usdc=40.0,
    )
    config = adaptive_promotion_config_from_settings(settings)
    cohort = promotion_cohort_from_identity(
        {
            **_identity(),
            "environment": "mainnet",
            "v1462_policy_hash": "admission-b",
        },
        config=config,
    )
    assert config.evidence_window_seconds == 5_000
    assert config.lease_ttl_seconds == 600
    assert config.probation_notional_cap_usdc == 20.0
    assert cohort.environment == "MAINNET"
    assert cohort.admission_policy_hash == "admission-a"
    assert cohort.promotion_policy_hash == config.policy_hash


def test_runtime_reads_independent_regime_and_terminal_latency_settings() -> None:
    settings = SimpleNamespace(
        mainnet_codex_v1464_auto_promotion_enabled=True,
        mainnet_codex_v1464_regime_confirmations=3,
        mainnet_codex_v1464_regime_max_age_seconds=60,
        mainnet_codex_v1464_regime_confirmation_window_seconds=45,
        mainnet_codex_v1464_max_terminal_latency_seconds=360,
    )
    runtime = V1464PromotionRuntime(
        FakeRepository(),
        settings=settings,
        config=CONFIG,
        boot_id="boot-1",
        owner_id="worker-1",
    )
    assert runtime.regime_confirmations == 3
    assert runtime.regime_max_age_seconds == 60
    assert runtime.regime_confirmation_window_seconds == 45
    assert runtime.max_terminal_latency_ms == 360_000


@pytest.mark.asyncio
async def test_shadow_projection_requires_explicit_environment_and_exact_registry() -> None:
    repository = FakeRepository()
    runtime = _runtime(repository)

    missing_environment = _details()
    missing_environment.pop("environment")
    missing = await runtime.project_shadow_outcome(missing_environment, NOW)
    outside = await runtime.project_shadow_outcome(
        _details(
            opportunity_id="opp-2",
            sample_id="sample-2",
            registry_status="OUT_OF_REGISTRY",
        ),
        NOW,
    )

    assert missing.persist is False
    assert missing.reason == "identity_incomplete"
    assert outside.persist is False
    assert outside.reason == "out_of_registry"
    assert repository.rows == []


@pytest.mark.asyncio
async def test_shadow_projection_preserves_non_authoritative_supported_rows() -> None:
    repository = FakeRepository()
    runtime = _runtime(repository)

    exact = await runtime.project_shadow_outcome(_details(), NOW)
    diagnostic = await runtime.project_shadow_outcome(
        _details(
            opportunity_id="opp-2",
            sample_id="sample-2",
            diagnostic_only=True,
        ),
        NOW,
    )
    incomplete = await runtime.project_shadow_outcome(
        _details(
            opportunity_id="opp-3",
            sample_id="sample-3",
            shadow_outcome="max_hold",
            data_complete=False,
            paper_pnl_usdc_after_fee=None,
        ),
        NOW,
    )

    assert exact.authoritative is True
    assert exact.payload["source_type"] == "SHADOW"
    assert diagnostic.persist is True and diagnostic.authoritative is False
    assert diagnostic.payload["diagnostic_only"] is True
    assert incomplete.persist is True and incomplete.authoritative is False
    assert incomplete.payload["outcome"] == "max_hold"
    assert incomplete.payload["data_complete"] is False
    assert len(repository.rows) == 3


@pytest.mark.asyncio
async def test_formal_v1462_evidence_uses_durable_id_and_legacy_false_is_not_diagnostic() -> None:
    repository = FakeRepository()
    runtime = _runtime(repository)
    formal = _details(
        opportunity_id="legacy-sample-1",
        v1462_opportunity_id="v1462-durable-1",
        sample_id="sample-1",
        promotion_eligible=False,
        evidence_evaluator_eligible=True,
        promotion_counts_as="diagnostic_only",
    )

    first = await runtime.project_shadow_outcome(formal, NOW)
    retry = await runtime.project_shadow_outcome(
        {
            **formal,
            "opportunity_id": "legacy-sample-2",
            "sample_id": "sample-2",
        },
        NOW,
    )

    assert first.authoritative is True
    assert first.payload["diagnostic_only"] is False
    assert first.payload["opportunity_id"] == "v1462-durable-1"
    assert first.payload["source_id"] == "v1462-durable-1"
    assert retry.authoritative is True
    assert len(repository.rows) == 1
    with pytest.raises(LeaseConflictError):
        await runtime.project_shadow_outcome(
            {
                **formal,
                "shadow_outcome": "sl_first",
                "paper_pnl_usdc_after_fee": -0.04,
            },
            NOW,
        )
    assert runtime.database_healthy is False


@pytest.mark.asyncio
async def test_true_drop_is_visible_but_pending_dedupe_is_not_written() -> None:
    repository = FakeRepository()
    runtime = _runtime(repository)

    dropped = await runtime.project_shadow_drop(
        _details(terminal_reason="queue_evicted"),
        NOW,
    )
    benign = await runtime.project_shadow_drop(
        _details(
            opportunity_id="opp-2",
            sample_id="sample-2",
            terminal_reason="active_opportunity_pending",
        ),
        NOW,
    )

    assert dropped.persist is True
    assert dropped.payload["outcome"] == "no_fill"
    assert dropped.payload["source_type"] == "SHADOW_DROP"
    assert dropped.payload["data_complete"] is False
    assert dropped.payload["source_payload"]["dropped"] is True
    assert benign.persist is False
    assert benign.reason == "benign_pending_dedupe"
    aggregate = aggregate_promotion_evidence(
        repository.rows,
        cohort=_cohort(),
        now_ms=NOW,
        config=CONFIG,
    ).snapshot
    assert aggregate.dropped == 1
    assert aggregate.incomplete == 1
    assert aggregate.data_complete is False


def test_paid_rows_are_separate_and_non_authority_does_not_change_revision() -> None:
    shadow = _shadow_floor_rows()
    paid = [
        _row(10, "tp1_first", source_type="PAID"),
        _row(11, "sl_first", source_type="PAID"),
        _row(12, "tp1_first", source_type="PAID"),
    ]
    base = aggregate_promotion_evidence(
        shadow + paid,
        cohort=_cohort(),
        now_ms=NOW,
        config=CONFIG,
    )
    diagnostic = _row(
        20,
        "tp1_first",
        diagnostic=True,
        source_type="SHADOW",
    )
    incomplete = _row(
        21,
        "no_fill",
        complete=False,
        pnl=None,
        source_type="SHADOW_DROP",
        source_payload={"dropped": True},
    )
    with_non_authority = aggregate_promotion_evidence(
        shadow + paid + [diagnostic, incomplete],
        cohort=_cohort(),
        now_ms=NOW,
        config=CONFIG,
    )

    assert base.snapshot.opportunities == 4
    assert base.snapshot.evaluable == 4
    assert base.snapshot.tp_first == 3
    assert base.snapshot.paid_complete == 3
    assert base.snapshot.paid_wins == 2
    assert base.snapshot.paid_net_pnl_usdc > 0
    assert base.consecutive_paid_losses == 0
    assert with_non_authority.snapshot.evidence_revision == base.snapshot.evidence_revision
    assert with_non_authority.snapshot.dropped == 1


def test_paid_only_evidence_cannot_satisfy_shadow_probation_floor() -> None:
    aggregate = aggregate_promotion_evidence(
        [
            _row(1, "tp1_first", source_type="PAID"),
            _row(2, "tp1_first", source_type="PAID"),
            _row(3, "tp1_first", source_type="PAID"),
            _row(4, "tp1_first", source_type="PAID"),
        ],
        cohort=_cohort(),
        now_ms=NOW,
        config=CONFIG,
    ).snapshot
    assert aggregate.opportunities == 0
    assert aggregate.evaluable == 0
    assert aggregate.tp_first == 0
    assert aggregate.paid_complete == 4


@pytest.mark.asyncio
async def test_two_exact_paid_losses_immediately_persist_cooldown() -> None:
    repository = FakeRepository(
        _shadow_floor_rows()
        + [
            _row(10, "sl_first", source_type="PAID", pnl=-0.03),
            _row(11, "sl_first", source_type="PAID", pnl=-0.03),
        ]
    )
    runtime = _runtime(repository)

    result = await runtime.evaluate_candidate(
        cohort=_cohort(),
        candidate_notional_usdc=25.0,
        regime=_regime(),
        risk=PromotionRiskInput(),
        now_ms=NOW,
        evaluation_id="eval-loss-quarantine",
    )

    assert result.decision.state is PromotionState.COOLDOWN
    assert result.metadata.adaptive_authorized is False
    assert repository.lease["status"] == "COOLDOWN"
    assert repository.lease["cooldown_until_ms"] == NOW + 15 * 60 * 1_000
    assert ("upsert_guard_state", "COOLDOWN") in repository.calls


@pytest.mark.asyncio
async def test_lane_wide_paid_streak_crosses_state_and_profile_cohorts() -> None:
    other = _cohort(
        market_state="other_supportive_state",
        resolved_profile_hash="profile-b",
    )
    repository = FakeRepository(
        _shadow_floor_rows()
        + [
            _row(10, "sl_first", source_type="PAID", pnl=-0.03),
            _row(
                11,
                "sl_first",
                cohort=other,
                source_type="PAID",
                pnl=-0.03,
            ),
        ]
    )
    runtime = _runtime(repository)

    result = await runtime.evaluate_candidate(
        cohort=_cohort(),
        candidate_notional_usdc=25.0,
        regime=_regime(),
        risk=PromotionRiskInput(),
        now_ms=NOW,
        evaluation_id="eval-lane-loss-quarantine",
    )

    assert result.decision.state is PromotionState.COOLDOWN
    assert result.metadata.adaptive_authorized is False
    assert repository.lease["status"] == "COOLDOWN"
    assert any(call[0] == "list_lane_paid_evidence" for call in repository.calls)


@pytest.mark.asyncio
async def test_global_halt_is_durable_without_prior_active_lease() -> None:
    repository = FakeRepository(_shadow_floor_rows())
    runtime = _runtime(repository)

    result = await runtime.evaluate_candidate(
        cohort=_cohort(),
        candidate_notional_usdc=25.0,
        regime=_regime(),
        risk=PromotionRiskInput(global_halted=True),
        now_ms=NOW,
        evaluation_id="eval-halt",
    )

    assert result.decision.state is PromotionState.HALTED
    assert result.metadata.adaptive_authorized is False
    assert repository.lease["status"] == "HALTED"
    assert ("upsert_guard_state", "HALTED") in repository.calls


def test_regime_requires_fresh_exact_timestamped_confirmation_chain() -> None:
    cohort = _cohort()
    good = derive_regime_input(
        _regime(cohort=cohort),
        cohort=cohort,
        now_ms=NOW,
        minimum_confirmations=2,
        max_age_seconds=90,
        confirmation_window_seconds=45,
    )
    invented_count = derive_regime_input(
        _regime(
            cohort=cohort,
            confirmation_observed_at_ms=(),
            confirmation_cohort_keys=(),
        ),
        cohort=cohort,
        now_ms=NOW,
        minimum_confirmations=2,
        max_age_seconds=90,
        confirmation_window_seconds=45,
    )
    mismatch = derive_regime_input(
        _regime(
            cohort=cohort,
            confirmation_cohort_keys=("wrong", "wrong"),
        ),
        cohort=cohort,
        now_ms=NOW,
        minimum_confirmations=2,
        max_age_seconds=90,
        confirmation_window_seconds=45,
    )
    overwide = derive_regime_input(
        _regime(
            cohort=cohort,
            confirmation_observed_at_ms=(NOW - 50_000, NOW - 100),
        ),
        cohort=cohort,
        now_ms=NOW,
        minimum_confirmations=2,
        max_age_seconds=90,
        confirmation_window_seconds=45,
    )
    assert good == replace(good, supportive=True, confirmed=True, fresh=True, exact_cohort_match=True)
    assert invented_count.confirmed is False
    assert mismatch.confirmed is False
    assert overwide.confirmed is False


@pytest.mark.asyncio
async def test_probation_grant_materializes_exact_25u_lease_and_metadata() -> None:
    repository = FakeRepository(_shadow_floor_rows())
    runtime = _runtime(repository)

    result = await runtime.evaluate_candidate(
        cohort=_cohort(),
        candidate_notional_usdc=50.0,
        regime=_regime(),
        risk=PromotionRiskInput(),
        now_ms=NOW,
        evaluation_id="eval-1",
    )

    assert result.decision.state is PromotionState.PROBATION
    assert result.metadata.adaptive_authorized is True
    assert result.metadata.incumbent_control_unchanged is True
    assert result.metadata.notional_cap_usdc == 25.0
    assert result.metadata.applied_notional_usdc == 25.0
    assert repository.lease["phase"] == "PROBATION"
    assert ("upsert_lease", "PROBATION_GRANTED") in repository.calls
    list_call = next(call for call in repository.calls if call[0] == "list_sliding_evidence")
    assert list_call[1]["eligible_only"] is False
    assert list_call[1]["max_terminal_latency_ms"] == 360 * 1_000


@pytest.mark.asyncio
async def test_new_paid_row_does_not_invalidate_retained_active_lease() -> None:
    repository = FakeRepository(_shadow_floor_rows())
    runtime = _runtime(repository)
    first = await runtime.evaluate_candidate(
        cohort=_cohort(),
        candidate_notional_usdc=25.0,
        regime=_regime(),
        risk=PromotionRiskInput(),
        now_ms=NOW,
        evaluation_id="eval-1",
    )
    prior_hash = first.metadata.evidence_snapshot_hash
    repository.rows.append(_row(10, "tp1_first", source_type="PAID"))

    retained = await runtime.evaluate_candidate(
        cohort=_cohort(),
        candidate_notional_usdc=25.0,
        regime=_regime(observed_at_ms=NOW + 1_000),
        risk=PromotionRiskInput(),
        now_ms=NOW + 1_000,
        evaluation_id="eval-2",
    )

    assert retained.decision.reason == "lease_retained"
    assert retained.metadata.adaptive_authorized is True
    assert retained.metadata.evidence_snapshot_hash == prior_hash


@pytest.mark.asyncio
async def test_shadow_six_four_plus_paid_three_two_promotes_control() -> None:
    repository = FakeRepository(_shadow_floor_rows())
    runtime = _runtime(repository)
    await runtime.evaluate_candidate(
        cohort=_cohort(),
        candidate_notional_usdc=50.0,
        regime=_regime(),
        risk=PromotionRiskInput(),
        now_ms=NOW,
        evaluation_id="eval-1",
    )
    repository.rows.extend(
        [
            _row(5, "sl_first"),
            _row(6, "tp1_first"),
            _row(10, "tp1_first", source_type="PAID"),
            _row(11, "sl_first", source_type="PAID"),
            _row(12, "tp1_first", source_type="PAID"),
        ]
    )

    result = await runtime.evaluate_candidate(
        cohort=_cohort(),
        candidate_notional_usdc=75.0,
        regime=_regime(observed_at_ms=NOW + 1_000),
        risk=PromotionRiskInput(),
        now_ms=NOW + 1_000,
        evaluation_id="eval-2",
    )

    assert result.decision.state is PromotionState.LIVE
    assert result.metadata.adaptive_authorized is True
    assert result.metadata.notional_cap_usdc == 50.0
    assert result.metadata.applied_notional_usdc == 50.0
    assert repository.lease["phase"] == "CONTROL"
    assert ("upsert_lease", "CONTROL_GRANTED") in repository.calls


@pytest.mark.asyncio
async def test_expired_lease_without_new_authoritative_rows_is_atomically_expired() -> None:
    repository = FakeRepository(_shadow_floor_rows())
    runtime = _runtime(repository)
    first = await runtime.evaluate_candidate(
        cohort=_cohort(),
        candidate_notional_usdc=25.0,
        regime=_regime(),
        risk=PromotionRiskInput(),
        now_ms=NOW,
        evaluation_id="eval-1",
    )
    expires = first.metadata.expires_at_ms
    assert expires is not None

    result = await runtime.evaluate_candidate(
        cohort=_cohort(),
        candidate_notional_usdc=25.0,
        regime=_regime(observed_at_ms=expires),
        risk=PromotionRiskInput(),
        now_ms=expires,
        evaluation_id="eval-expire",
    )

    assert result.metadata.adaptive_authorized is False
    assert result.decision.reason == "lease_expired_without_fresh_evidence"
    assert repository.lease["status"] == "EXPIRED"
    assert any(call[0] == "expire_lease" for call in repository.calls)


@pytest.mark.asyncio
async def test_cas_conflict_latches_database_health_and_blocks_only_adaptive() -> None:
    repository = FakeRepository(_shadow_floor_rows())
    repository.fail_mutations = True
    runtime = _runtime(repository)

    result = await runtime.evaluate_candidate(
        cohort=_cohort(),
        candidate_notional_usdc=25.0,
        regime=_regime(),
        risk=PromotionRiskInput(),
        now_ms=NOW,
        evaluation_id="eval-conflict",
    )

    assert result.persistence_healthy is False
    assert result.metadata.adaptive_authorized is False
    assert result.metadata.incumbent_control_unchanged is True
    assert result.metadata.reason == "adaptive_database_unhealthy"
    assert runtime.database_healthy is False
    second = await runtime.evaluate_candidate(
        cohort=_cohort(),
        candidate_notional_usdc=25.0,
        regime=_regime(),
        risk=PromotionRiskInput(),
        now_ms=NOW + 1,
        evaluation_id="eval-latched",
    )
    assert second.persistence_healthy is False
    assert second.metadata.adaptive_authorized is False


@pytest.mark.asyncio
async def test_paid_terminal_projection_uses_admitted_identity_and_is_recordable() -> None:
    repository = FakeRepository(_shadow_floor_rows())
    runtime = _runtime(repository)
    granted = await runtime.evaluate_candidate(
        cohort=_cohort(),
        candidate_notional_usdc=25.0,
        regime=_regime(),
        risk=PromotionRiskInput(),
        now_ms=NOW,
        evaluation_id="eval-1",
    )

    payload = project_paid_terminal_evidence(
        granted.metadata,
        run_id="run-1",
        terminal_at_ms=NOW + 500,
        net_pnl_usdc=0.09,
        reason="take_profit_filled",
        now_ms=NOW + 1_000,
    )
    inserted = await runtime.record_paid_terminal(
        granted.metadata,
        run_id="run-2",
        terminal_at_ms=NOW + 500,
        net_pnl_usdc=-0.04,
        reason="stop_loss_filled",
        now_ms=NOW + 1_000,
    )

    assert payload["source_type"] == "PAID"
    assert payload["outcome"] == "tp1_first"
    assert payload["resolved_profile_hash"] == _cohort().resolved_profile_hash
    assert inserted is True
    assert repository.rows[-1]["outcome"] == "sl_first"


@pytest.mark.asyncio
async def test_second_paid_terminal_loss_immediately_cas_cools_active_lease() -> None:
    repository = FakeRepository(_shadow_floor_rows())
    runtime = _runtime(repository)
    granted = await runtime.evaluate_candidate(
        cohort=_cohort(),
        candidate_notional_usdc=25.0,
        regime=_regime(),
        risk=PromotionRiskInput(),
        now_ms=NOW,
        evaluation_id="eval-terminal-risk",
    )

    await runtime.record_paid_terminal(
        granted.metadata,
        run_id="loss-run-1",
        terminal_at_ms=NOW + 500,
        net_pnl_usdc=-0.03,
        reason="TRAIL",
        now_ms=NOW + 1_000,
    )
    assert repository.lease["status"] == "ACTIVE"
    await runtime.record_paid_terminal(
        granted.metadata,
        run_id="loss-run-2",
        terminal_at_ms=NOW + 1_500,
        net_pnl_usdc=-0.03,
        reason="BE",
        now_ms=NOW + 2_000,
    )

    assert repository.lease["status"] == "COOLDOWN"
    assert repository.lease["demotion_reason"] == "paid_risk_quarantine"
    assert repository.lease["cooldown_until_ms"] == NOW + 2_000 + 15 * 60 * 1_000
    assert ("cooldown_lease", "paid_risk_quarantine") in repository.calls


@pytest.mark.parametrize(
    ("net_pnl_usdc", "reason", "expected"),
    [
        (0.03, "TRAIL", "tp1_first"),
        (-0.01, "BE", "sl_first"),
        (0.0, "flat_detected", "max_hold"),
    ],
)
@pytest.mark.asyncio
async def test_paid_outcome_authority_comes_from_reconciled_net_not_reason(
    net_pnl_usdc: float,
    reason: str,
    expected: str,
) -> None:
    repository = FakeRepository(_shadow_floor_rows())
    runtime = _runtime(repository)
    granted = await runtime.evaluate_candidate(
        cohort=_cohort(),
        candidate_notional_usdc=25.0,
        regime=_regime(),
        risk=PromotionRiskInput(),
        now_ms=NOW,
        evaluation_id=f"eval-{reason}",
    )

    payload = project_paid_terminal_evidence(
        granted.metadata,
        run_id=f"run-{reason}",
        terminal_at_ms=NOW + 500,
        net_pnl_usdc=net_pnl_usdc,
        reason=reason,
        now_ms=NOW + 1_000,
    )

    assert payload["outcome"] == expected
    assert payload["source_payload"]["terminal_reason"] == reason


@pytest.mark.asyncio
async def test_pre_submit_revalidation_atomically_claims_generation_and_blocks_replay() -> None:
    repository = FakeRepository(_shadow_floor_rows())
    runtime = _runtime(repository)
    granted = await runtime.evaluate_candidate(
        cohort=_cohort(),
        candidate_notional_usdc=25.0,
        regime=_regime(),
        risk=PromotionRiskInput(),
        now_ms=NOW,
        evaluation_id="eval-1",
    )

    claimed = await runtime.revalidate_before_submit(
        granted.metadata,
        NOW + 1_000,
        current_cohort=_cohort(),
        actual_notional_usdc=20.0,
        risk=PromotionRiskInput(),
        regime=_regime(observed_at_ms=NOW + 1_000),
        consume_id="submit-1",
    )
    replay = await runtime.revalidate_before_submit(
        granted.metadata,
        NOW + 1_000,
        current_cohort=_cohort(),
        actual_notional_usdc=20.0,
        risk=PromotionRiskInput(),
        regime=_regime(observed_at_ms=NOW + 1_000),
        consume_id="submit-1",
    )

    assert claimed.allowed is True
    assert claimed.reason == "active_lease_claimed"
    assert claimed.claim_generation == granted.metadata.generation + 1
    assert claimed.metadata.generation == claimed.claim_generation
    assert claimed.metadata.applied_notional_usdc == 20.0
    assert replay.allowed is False
    assert replay.reason == "admission_claim_replayed"


@pytest.mark.asyncio
async def test_pre_submit_revalidation_checks_final_notional_regime_and_paid_risk() -> None:
    repository = FakeRepository(_shadow_floor_rows())
    runtime = _runtime(repository)
    granted = await runtime.evaluate_candidate(
        cohort=_cohort(),
        candidate_notional_usdc=25.0,
        regime=_regime(),
        risk=PromotionRiskInput(),
        now_ms=NOW,
        evaluation_id="eval-1",
    )

    oversized = await runtime.revalidate_before_submit(
        granted.metadata,
        NOW + 1_000,
        current_cohort=_cohort(),
        actual_notional_usdc=30.0,
        risk=PromotionRiskInput(),
        regime=_regime(observed_at_ms=NOW + 1_000),
        consume_id="submit-oversized",
    )
    stale_regime = await runtime.revalidate_before_submit(
        granted.metadata,
        NOW + 1_000,
        current_cohort=_cohort(),
        actual_notional_usdc=20.0,
        risk=PromotionRiskInput(),
        regime=_regime(
            observed_at_ms=NOW - CONFIG.evidence_max_age_seconds * 1_000 - 1
        ),
        consume_id="submit-stale",
    )
    repository.rows.extend(
        [
            _row(20, "sl_first", source_type="PAID", pnl=-0.03),
            _row(21, "sl_first", source_type="PAID", pnl=-0.03),
        ]
    )
    loss_quarantine = await runtime.revalidate_before_submit(
        granted.metadata,
        NOW + 1_000,
        current_cohort=_cohort(),
        actual_notional_usdc=20.0,
        risk=PromotionRiskInput(),
        regime=_regime(observed_at_ms=NOW + 1_000),
        consume_id="submit-loss",
    )

    assert oversized.reason == "actual_notional_exceeds_admission"
    assert stale_regime.reason == "regime_stale"
    assert loss_quarantine.reason == "consecutive_paid_loss_limit"
    assert not any(call[0] == "claim_admission" for call in repository.calls)
