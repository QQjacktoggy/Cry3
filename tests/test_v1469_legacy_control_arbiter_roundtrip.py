from __future__ import annotations

import hashlib
import json

from src.gridbot.mainnet.v1469_adaptive_identity import (
    BreakevenPolicy,
    DcaPolicy,
    EarlyFailPolicy,
    EXECUTION_PROFILE_SCHEMA,
    MarketStateIdentity,
    RepricePolicy,
    RunnerPolicy,
    TakeProfitLevel,
    TrailPolicy,
    canonical_sha256,
)
from src.gridbot.mainnet.v1469_arm_arbiter import (
    ArbiterRequest,
    RegimeSnapshot,
    evaluate_rolling_arbiter,
)
from src.gridbot.mainnet.v1469_arbiter_evidence_mapper import (
    PAIRED_CONTRACT_SCHEMA,
    expected_profile_ids,
    map_durable_paired_evidence,
    paired_group_identity,
)
from src.gridbot.mainnet.v1469_arm_profiles import get_arm_profile
from src.gridbot.mainnet.v1469_legacy_control import (
    LEGACY_CONTROL,
    LegacyExecutionSnapshot,
)
from src.gridbot.mainnet.v1469_paired_evaluator import TERMINAL_RESULT_SCHEMA


OBSERVED = 2_000_000_000_000


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _legacy_snapshot() -> LegacyExecutionSnapshot:
    return LegacyExecutionSnapshot(
        market_identity=MarketStateIdentity(
            environment="MAINNET",
            symbol="BTCUSDC",
            lane_code="W6A",
            effective_side="LONG",
            strategy="W6A",
            coarse_regime="RANGE",
            market_state="RANGE_STABLE",
        ),
        entry_offset_bp=1.0,
        entry_type="LIMIT",
        entry_ttl_s=90,
        maker_mode="POST_ONLY",
        take_profits=(
            TakeProfitLevel(
                level_id="FULL",
                target_bp=8.0,
                fraction=1.0,
            ),
        ),
        sl_bp=8.0,
        max_hold_s=360,
        reprice=RepricePolicy(),
        breakeven=BreakevenPolicy(),
        trail=TrailPolicy(),
        runner=RunnerPolicy(),
        early_fail=EarlyFailPolicy(),
        dca=DcaPolicy(),
        lane_notional_cap_usdc=25.0,
        global_notional_cap_usdc=50.0,
        risk_policy_hash="risk-a",
        reference_price=100.0,
    )


def _arm_key(profile_id: str, profile_hash: str) -> str:
    return "v1469a_" + canonical_sha256(
        {
            "lane_code": "W6A",
            "effective_side": "LONG",
            "strategy": "W6A",
            "coarse_regime": "RANGE",
            "execution_profile_id": profile_id,
            "execution_profile_schema": EXECUTION_PROFILE_SCHEMA,
            "execution_profile_hash": profile_hash,
        }
    )


def _rehash_evidence(row: dict[str, object]) -> None:
    row["evidence_hash"] = hashlib.sha256(
        _canonical(
            {
                "evidence_id": row["evidence_id"],
                "opportunity_id": row["opportunity_id"],
                "candidate_id": row["candidate_id"],
                "arm_key": row["arm_key"],
                "execution_profile_hash": row["execution_profile_hash"],
                "source_type": row["source_type"],
                "diagnostic_only": row["diagnostic_only"],
                "observed_at_ms": row["observed_at_ms"],
                "status": row["status"],
                "terminal_at_ms": row["terminal_at_ms"],
                "outcome": row["outcome"],
                "fill_status": row["fill_status"],
                "data_complete": row["data_complete"],
                "ambiguous": row["ambiguous"],
                "reward_net_bp": row["reward_net_bp"],
                "mfe_bp": row["mfe_bp"],
                "mae_bp": row["mae_bp"],
                "terminal_reason": row["terminal_reason"],
                "terminal_payload_json": row["terminal_payload_json"],
            }
        ).encode("utf-8")
    ).hexdigest()


def _group_rows(
    opportunity_id: str = "opportunity-1",
    rewards: dict[str, float] | None = None,
) -> list[dict[str, object]]:
    candidate_id = "candidate-w6a"
    snapshot = _legacy_snapshot()
    definitions = {
        profile_id: get_arm_profile(profile_id)
        for profile_id in expected_profile_ids("RANGE")
    }
    definitions[LEGACY_CONTROL] = snapshot.profile_definition
    profile_ids = tuple(sorted(definitions))
    group_id = paired_group_identity(
        opportunity_id,
        candidate_id,
        profile_ids,
    )
    deadline_by_profile = {
        profile_id: OBSERVED
        + (
            definition.execution_profile.entry_ttl_s
            + definition.execution_profile.max_hold_s
        )
        * 1_000
        for profile_id, definition in definitions.items()
    }
    group_deadline = max(deadline_by_profile.values())
    terminal_at = OBSERVED + 30_000
    rows: list[dict[str, object]] = []
    for profile_id in profile_ids:
        definition = definitions[profile_id]
        execution = definition.execution_profile
        assert execution is not None
        reward = float((rewards or {}).get(profile_id, 4.0))
        arm_key = _arm_key(profile_id, execution.profile_hash)
        market_state_hash = (
            snapshot.market_identity.identity_hash
            if profile_id == LEGACY_CONTROL
            else "static-market-state"
        )
        entry_limit = 99.99 if profile_id == LEGACY_CONTROL else 99.0
        payload: dict[str, object] = {
            "schema": TERMINAL_RESULT_SCHEMA,
            "opportunity_id": opportunity_id,
            "profile_id": profile_id,
            "arm_hash": arm_key,
            "market_state_hash": market_state_hash,
            "execution_profile_hash": execution.profile_hash,
            "envelope_hash": "envelope-shared",
            "cost_model_hash": "cost-model",
            "side": "LONG",
            "fill_status": "FILLED",
            "entry_limit_price": entry_limit,
            "entry_price": entry_limit,
            "filled_at_ms": OBSERVED + 1_000,
            "terminal_reason": "TP",
            "terminal_at_ms": terminal_at,
            "terminal_price": 100.0,
            "exits": [],
            "data_complete": True,
            "evaluable": True,
            "gross_reward_bp": reward + 2.0,
            "maker_fee_cost_bp": 2.0,
            "taker_fee_cost_bp": 0.0,
            "slippage_cost_bp": 0.0,
            "reward_net_bp": reward,
            "mfe_bp": reward + 3.0,
            "mae_bp": -1.0,
        }
        payload["terminal_hash"] = canonical_sha256(payload)
        payload["paired_contract"] = {
            "schema": PAIRED_CONTRACT_SCHEMA,
            "paired_group_id": group_id,
            "opportunity_id": opportunity_id,
            "candidate_id": candidate_id,
            "profile_id": profile_id,
            "expected_profile_ids": list(profile_ids),
            "observed_at_ms": OBSERVED,
            "coverage_start_ms": OBSERVED,
            "coverage_through_ms": terminal_at,
            "decision_at_ms": terminal_at,
            "coverage_complete": True,
            "profile_deadline_at_ms": deadline_by_profile[profile_id],
            "group_deadline_at_ms": group_deadline,
            "shared_envelope_hash": "envelope-shared",
        }
        row: dict[str, object] = {
            "evidence_id": f"evidence-{opportunity_id}-{profile_id}",
            "opportunity_id": opportunity_id,
            "candidate_id": candidate_id,
            "arm_key": arm_key,
            "execution_profile_id": profile_id,
            "execution_profile_schema": EXECUTION_PROFILE_SCHEMA,
            "execution_profile_hash": execution.profile_hash,
            "source_type": "SHADOW",
            "diagnostic_only": 0,
            "observed_at_ms": OBSERVED,
            "status": "TERMINAL",
            "terminal_at_ms": terminal_at,
            "outcome": "tp_first",
            "fill_status": "FILLED",
            "data_complete": 1,
            "ambiguous": 0,
            "reward_net_bp": reward,
            "mfe_bp": reward + 3.0,
            "mae_bp": -1.0,
            "terminal_reason": "TP",
            "terminal_payload_json": _canonical(payload),
            "evidence_hash": "",
            "lane_code": "W6A",
            "effective_side": "LONG",
            "strategy": "W6A",
            "coarse_regime": "RANGE",
            "data_quality": "COMPLETE",
            "candidate_status": "SAFE",
        }
        if profile_id == LEGACY_CONTROL:
            row["execution_profile_payload"] = snapshot.to_payload()
            row["feature_snapshot"] = {
                "market_state": "RANGE_STABLE",
                "signal_reference_price": 100.0,
            }
        _rehash_evidence(row)
        rows.append(row)
    return rows


def _legacy_row(rows: list[dict[str, object]]) -> dict[str, object]:
    return next(
        row
        for row in rows
        if row["execution_profile_id"] == LEGACY_CONTROL
    )


def test_exact_legacy_sidecar_maps_as_paired_incumbent_candidate() -> None:
    result = map_durable_paired_evidence(
        _group_rows(),
        ledger_scope_complete=True,
    )

    assert {
        candidate.identity.execution_profile_id
        for candidate in result.candidates
    } == {*expected_profile_ids("RANGE"), LEGACY_CONTROL}
    assert result.trusted_paired_rows == 3
    assert not result.issues
    incumbent = next(
        candidate
        for candidate in result.candidates
        if candidate.identity.execution_profile_id == LEGACY_CONTROL
    )
    assert incumbent.evidence[0].paired


def test_missing_or_corrupt_legacy_sidecar_fails_closed() -> None:
    for mode in ("missing", "corrupt"):
        rows = _group_rows(opportunity_id=f"opportunity-{mode}")
        legacy = _legacy_row(rows)
        if mode == "missing":
            legacy.pop("execution_profile_payload")
        else:
            legacy["execution_profile_payload"] = {"corrupt": True}

        result = map_durable_paired_evidence(
            rows,
            ledger_scope_complete=True,
        )

        assert result.trusted_paired_rows == 0
        assert all(
            candidate.identity.execution_profile_id != LEGACY_CONTROL
            for candidate in result.candidates
        )
        assert any(
            issue.code == "invalid_terminal_evidence"
            and "execution profile sidecar" in issue.detail
            for issue in result.issues
        )


def test_legacy_feature_reference_mismatch_fails_closed() -> None:
    rows = _group_rows()
    legacy = _legacy_row(rows)
    legacy["feature_snapshot"] = {
        "market_state": "RANGE_STABLE",
        "signal_reference_price": 100.01,
    }

    result = map_durable_paired_evidence(
        rows,
        ledger_scope_complete=True,
    )

    assert result.trusted_paired_rows == 0
    assert all(
        candidate.identity.execution_profile_id != LEGACY_CONTROL
        for candidate in result.candidates
    )
    assert any(
        issue.code == "invalid_terminal_evidence"
        and "durable reference price mismatch" in issue.detail
        for issue in result.issues
    )


def test_legacy_candidate_round_trips_into_challenger_comparison() -> None:
    rows: list[dict[str, object]] = []
    rewards = {
        LEGACY_CONTROL: 4.0,
        "PASSIVE_BALANCED": 5.0,
        "RANGE_SCALP": 7.0,
    }
    for index in range(6):
        rows.extend(
            _group_rows(
                opportunity_id=f"opportunity-{index}",
                rewards=rewards,
            )
        )
    mapping = map_durable_paired_evidence(
        rows,
        ledger_scope_complete=True,
    )
    incumbent = next(
        candidate
        for candidate in mapping.candidates
        if candidate.identity.execution_profile_id == LEGACY_CONTROL
    )
    now = OBSERVED + 35_000

    decision = evaluate_rolling_arbiter(
        ArbiterRequest(
            as_of_ms=now,
            regime_snapshot=RegimeSnapshot(
                regime="RANGE",
                observed_at_ms=now - 20_000,
                confirmation_at_ms=(
                    now - 40_000,
                    now - 20_000,
                ),
                direction_valid_sides=frozenset({"LONG"}),
            ),
            submit_snapshot=RegimeSnapshot(
                regime="RANGE",
                observed_at_ms=now - 5_000,
                direction_valid_sides=frozenset({"LONG"}),
            ),
            candidates=mapping.candidates,
            incumbent_arm_key=incumbent.identity.arm_key,
        )
    )

    assert decision.winner is not None
    assert decision.winner.execution_profile_id == "RANGE_SCALP"
    challenger = next(
        evaluation
        for evaluation in decision.evaluations
        if evaluation.identity.execution_profile_id == "RANGE_SCALP"
    )
    assert challenger.paired_vs_incumbent == 6
    assert challenger.paired_wins_vs_incumbent == 6
    assert challenger.paired_ev_delta_vs_incumbent_bp == 3.0
    assert not challenger.selection_blockers
