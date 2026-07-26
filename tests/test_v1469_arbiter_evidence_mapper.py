from __future__ import annotations

import hashlib
import json

from src.gridbot.mainnet.v1469_adaptive_identity import (
    EXECUTION_PROFILE_SCHEMA,
    canonical_sha256,
)
from src.gridbot.mainnet.v1469_arbiter_evidence_mapper import (
    PAIRED_CONTRACT_SCHEMA,
    expected_profile_ids,
    map_durable_paired_evidence,
    paired_group_identity,
)
from src.gridbot.mainnet.v1469_arm_profiles import get_arm_profile
from src.gridbot.mainnet.v1469_paired_evaluator import TERMINAL_RESULT_SCHEMA


OBSERVED = 2_000_000_000_000


def _canonical(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _arm_key(profile_id: str, *, lane_code: str = "W6A") -> str:
    profile = get_arm_profile(profile_id)
    return "v1469a_" + canonical_sha256(
        {
            "lane_code": lane_code,
            "effective_side": "LONG",
            "strategy": "W6A",
            "coarse_regime": "RANGE",
            "execution_profile_id": profile_id,
            "execution_profile_schema": EXECUTION_PROFILE_SCHEMA,
            "execution_profile_hash": profile.execution_profile_hash,
        }
    )


def _rehash_row(row: dict) -> None:
    terminal = {
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
                **terminal,
            }
        ).encode("utf-8")
    ).hexdigest()


def _row(
    profile_id: str,
    *,
    lane_code: str = "W6A",
    candidate_id: str = "candidate-w6a",
    opportunity_id: str = "opportunity-1",
    envelope_hash: str = "envelope-shared",
    include_contract: bool = True,
    coverage_through_ms: int | None = None,
    status: str = "TERMINAL",
    outcome: str = "tp_first",
) -> dict:
    profile = get_arm_profile(profile_id)
    execution = profile.execution_profile
    assert execution is not None
    profile_ids = expected_profile_ids("RANGE")
    profile_deadline = OBSERVED + (
        execution.entry_ttl_s + execution.max_hold_s
    ) * 1_000
    group_deadline = max(
        OBSERVED
        + (
            get_arm_profile(item).execution_profile.entry_ttl_s
            + get_arm_profile(item).execution_profile.max_hold_s
        )
        * 1_000
        for item in profile_ids
    )
    terminal_at = OBSERVED + 30_000
    through = coverage_through_ms or terminal_at
    arm_key = _arm_key(profile_id, lane_code=lane_code)
    terminal_reason = "TP"
    reward = 4.0
    payload = {
        "schema": TERMINAL_RESULT_SCHEMA,
        "opportunity_id": opportunity_id,
        "profile_id": profile_id,
        "arm_hash": arm_key,
        "market_state_hash": "market-state",
        "execution_profile_hash": profile.execution_profile_hash,
        "envelope_hash": envelope_hash,
        "cost_model_hash": "cost-model",
        "side": "LONG",
        "fill_status": "FILLED",
        "entry_limit_price": 99.0,
        "entry_price": 99.0,
        "filled_at_ms": OBSERVED + 1_000,
        "terminal_reason": terminal_reason,
        "terminal_at_ms": terminal_at,
        "terminal_price": 100.0,
        "exits": [],
        "data_complete": True,
        "evaluable": True,
        "gross_reward_bp": 6.0,
        "maker_fee_cost_bp": 2.0,
        "taker_fee_cost_bp": 0.0,
        "slippage_cost_bp": 0.0,
        "reward_net_bp": reward,
        "mfe_bp": 7.0,
        "mae_bp": -1.0,
    }
    payload["terminal_hash"] = canonical_sha256(payload)
    if include_contract:
        payload["paired_contract"] = {
            "schema": PAIRED_CONTRACT_SCHEMA,
            "paired_group_id": paired_group_identity(
                opportunity_id, candidate_id, profile_ids
            ),
            "opportunity_id": opportunity_id,
            "candidate_id": candidate_id,
            "profile_id": profile_id,
            "expected_profile_ids": list(profile_ids),
            "observed_at_ms": OBSERVED,
            "coverage_start_ms": OBSERVED,
            "coverage_through_ms": through,
            "decision_at_ms": through,
            "coverage_complete": True,
            "profile_deadline_at_ms": profile_deadline,
            "group_deadline_at_ms": group_deadline,
            "shared_envelope_hash": envelope_hash,
        }
    payload_json = _canonical(payload)
    row = {
        "evidence_id": f"evidence-{candidate_id}-{profile_id}",
        "opportunity_id": opportunity_id,
        "candidate_id": candidate_id,
        "arm_key": arm_key,
        "execution_profile_id": profile_id,
        "execution_profile_schema": EXECUTION_PROFILE_SCHEMA,
        "execution_profile_hash": profile.execution_profile_hash,
        "source_type": "SHADOW",
        "diagnostic_only": 0,
        "observed_at_ms": OBSERVED,
        "status": status,
        "terminal_at_ms": terminal_at,
        "outcome": outcome,
        "fill_status": "FILLED",
        "data_complete": 1,
        "ambiguous": 0,
        "reward_net_bp": reward,
        "mfe_bp": 7.0,
        "mae_bp": -1.0,
        "terminal_reason": terminal_reason,
        "terminal_payload_json": payload_json,
        "evidence_hash": "",
        "lane_code": lane_code,
        "effective_side": "LONG",
        "strategy": "W6A",
        "coarse_regime": "RANGE",
        "data_quality": "COMPLETE",
        "candidate_status": "SAFE",
    }
    _rehash_row(row)
    return row


def test_complete_contract_maps_all_profiles_as_trusted_paired_evidence() -> None:
    rows = [_row(profile_id) for profile_id in expected_profile_ids("RANGE")]

    result = map_durable_paired_evidence(
        rows, ledger_scope_complete=True
    )

    assert len(result.candidates) == 2
    assert result.trusted_paired_rows == 2
    assert not result.issues
    assert result.ledger_revision.startswith("v1469l_")
    assert all(
        candidate.evidence[0].paired for candidate in result.candidates
    )
    assert all(
        candidate.source_evidence_revision.startswith("v1469r_")
        for candidate in result.candidates
    )


def test_only_explicit_boolean_marker_trips_hard_loss() -> None:
    row = _row(
        "RANGE_SCALP",
        include_contract=False,
        outcome="sl_first",
    )
    payload = json.loads(row["terminal_payload_json"])
    payload.pop("terminal_hash")
    payload["terminal_reason"] = "SL"
    payload["hard_loss"] = "false"
    payload["terminal_hash"] = canonical_sha256(payload)
    row["terminal_reason"] = "SL"
    row["terminal_payload_json"] = _canonical(payload)
    _rehash_row(row)

    result = map_durable_paired_evidence([row], ledger_scope_complete=True)

    evidence = result.candidates[0].evidence[0]
    assert evidence.hard_loss is False

    payload.pop("terminal_hash")
    payload["hard_loss"] = True
    payload["terminal_hash"] = canonical_sha256(payload)
    row["terminal_payload_json"] = _canonical(payload)
    _rehash_row(row)

    result = map_durable_paired_evidence([row], ledger_scope_complete=True)

    assert result.candidates[0].evidence[0].hard_loss is True


def test_mapping_and_revisions_are_deterministic_under_row_reordering() -> None:
    rows = [_row(profile_id) for profile_id in expected_profile_ids("RANGE")]
    first = map_durable_paired_evidence(
        rows, ledger_scope_complete=True
    )
    second = map_durable_paired_evidence(
        list(reversed(rows)), ledger_scope_complete=True
    )

    assert first == second


def test_missing_profile_never_claims_pairing() -> None:
    result = map_durable_paired_evidence(
        [_row("RANGE_SCALP")], ledger_scope_complete=True
    )

    assert len(result.candidates) == 1
    assert not result.candidates[0].evidence[0].paired
    assert result.trusted_paired_rows == 0
    assert any(
        issue.code == "incomplete_paired_profile_group"
        for issue in result.issues
    )


def test_missing_contract_ignores_untrusted_boolean() -> None:
    rows = [
        _row(profile_id, include_contract=False)
        for profile_id in expected_profile_ids("RANGE")
    ]
    for row in rows:
        row["paired"] = True

    result = map_durable_paired_evidence(
        rows, ledger_scope_complete=True
    )

    assert result.trusted_paired_rows == 0
    assert all(
        not candidate.evidence[0].paired for candidate in result.candidates
    )
    assert any(
        issue.code == "invalid_paired_contract" for issue in result.issues
    )


def test_profile_envelope_mismatch_fails_closed() -> None:
    profile_ids = expected_profile_ids("RANGE")
    rows = [
        _row(profile_ids[0], envelope_hash="envelope-a"),
        _row(profile_ids[1], envelope_hash="envelope-b"),
    ]

    result = map_durable_paired_evidence(
        rows, ledger_scope_complete=True
    )

    assert result.trusted_paired_rows == 0
    assert any(
        issue.code == "invalid_paired_contract" for issue in result.issues
    )


def test_coverage_before_terminal_fails_closed() -> None:
    rows = [
        _row(
            profile_id,
            coverage_through_ms=OBSERVED + 10_000,
        )
        for profile_id in expected_profile_ids("RANGE")
    ]

    result = map_durable_paired_evidence(
        rows, ledger_scope_complete=True
    )

    assert result.trusted_paired_rows == 0
    assert any(
        issue.code == "invalid_paired_contract"
        and "coverage ordering" in issue.detail
        for issue in result.issues
    )


def test_dropped_member_is_revision_only_and_cannot_complete_pair() -> None:
    rows = [_row("RANGE_SCALP"), _row("PASSIVE_BALANCED")]
    dropped = rows[1]
    dropped["status"] = "DROPPED"
    dropped["outcome"] = "data_incomplete"
    dropped["fill_status"] = "UNKNOWN"
    dropped["data_complete"] = 0
    dropped["reward_net_bp"] = None
    dropped["terminal_reason"] = "PAIRED_ENVELOPE_INCOMPLETE"
    dropped["terminal_payload_json"] = _canonical(
        {
            "schema": "v1469.paired-envelope-drop.1",
            "profile_id": "PASSIVE_BALANCED",
        }
    )
    _rehash_row(dropped)

    result = map_durable_paired_evidence(
        rows, ledger_scope_complete=True
    )

    assert len(result.candidates) == 1
    assert not result.candidates[0].evidence[0].paired
    assert result.trusted_paired_rows == 0
    assert any(
        issue.code == "non_evaluable_durable_row"
        for issue in result.issues
    )


def test_same_opportunity_cross_lane_envelope_conflict_fails_both_groups() -> None:
    rows = []
    for profile_id in expected_profile_ids("RANGE"):
        rows.append(
            _row(
                profile_id,
                lane_code="W6A",
                candidate_id="candidate-w6a",
                envelope_hash="envelope-a",
            )
        )
        rows.append(
            _row(
                profile_id,
                lane_code="T3L",
                candidate_id="candidate-t3l",
                envelope_hash="envelope-b",
            )
        )

    result = map_durable_paired_evidence(
        rows, ledger_scope_complete=True
    )

    assert result.trusted_paired_rows == 0
    assert any(
        issue.code == "opportunity_envelope_conflict"
        for issue in result.issues
    )


def test_tampered_evidence_hash_blocks_the_entire_arm() -> None:
    rows = [_row(profile_id) for profile_id in expected_profile_ids("RANGE")]
    rows[0]["evidence_hash"] = "0" * 64
    corrupted_arm = rows[0]["arm_key"]

    result = map_durable_paired_evidence(
        rows, ledger_scope_complete=True
    )

    assert all(
        candidate.identity.arm_key != corrupted_arm
        for candidate in result.candidates
    )
    assert any(
        issue.code == "invalid_durable_row" for issue in result.issues
    )


def test_bounded_or_rolling_scope_cannot_create_a_revision_or_candidates() -> None:
    result = map_durable_paired_evidence(
        [_row("RANGE_SCALP")], ledger_scope_complete=False
    )

    assert not result.candidates
    assert result.ledger_revision is None
    assert result.issues[0].code == "incomplete_ledger_scope"
