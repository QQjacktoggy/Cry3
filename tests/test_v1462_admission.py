from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from src.gridbot.mainnet.v1462_admission import (
    AdmissionMode,
    V1462_ALLOWED_CONTROL_RULE_IDS,
    build_pre_reject_candidate_ticket,
    evaluate_strict_admission,
)
from src.gridbot.mainnet.v1462_lane_registry import CNL_SAFE_LINEAGE_KIND


def _control_candidate(rule_id: str):
    specs = {
        "v1460.rp1.control": {
            "lane": "RP1",
            "side": "LONG",
            "state": "UNKNOWN",
            "entry": 1.0,
            "profile": {},
        },
        "v1460.s1p_pullback.control": {
            "lane": "S1P-L",
            "side": "LONG",
            "state": "S1P-L:ordinary_pullback_pre_vwap",
            "entry": 0.0,
            "profile": {
                "entry_bp": 0.0,
                "tp1_bp": 6.0,
                "sl_bp": 15.0,
                "be_bp": 0.0,
                "partial_exit_pct": 1.0,
                "ttl_s": 180,
            },
        },
        "v1460.stup_clean.control": {
            "lane": "STUP-S",
            "side": "SHORT",
            "state": "STUP-S:clean_extension",
            "entry": 2.0,
            "profile": {
                "entry_bp": 2.0,
                "tp1_bp": 6.0,
                "full_tp_bp": 80.0,
                "sl_bp": 8.0,
                "be_bp": 2.0,
                "partial_exit_pct": 0.70,
                "ttl_s": 60,
            },
        },
        "v1460.cnl_reclaim.control": {
            "lane": "CNL-WPR-L",
            "side": "LONG",
            "state": "CNL-WPR-L:fast_reclaim",
            "entry": 0.0,
            "profile": {
                "entry_bp": 0.0,
                "tp1_bp": 6.0,
                "sl_bp": 6.0,
                "be_bp": 0.0,
                "partial_exit_pct": 1.0,
                "ttl_s": 45,
            },
        },
    }
    spec = specs[rule_id]
    raw_lane = None if rule_id == "v1460.cnl_reclaim.control" else spec["lane"]
    raw = {
        "accepted": rule_id != "v1460.cnl_reclaim.control",
        "lane_code": raw_lane,
        "side": spec["side"],
        "strategy": "S1_BB_RSI",
        "entry_offset_bp": spec["entry"],
        "requested_notional_usdc": 50.0,
        "reason": (
            "no_codex_v1_lane_match"
            if rule_id == "v1460.cnl_reclaim.control"
            else "accepted"
        ),
        "metrics": {"market_state": spec["state"]},
    }
    effective = {
        **raw,
        "accepted": True,
        "lane_code": spec["lane"],
        "metrics": {"market_state": spec["state"], **spec["profile"]},
    }
    return build_pre_reject_candidate_ticket(
        raw,
        effective,
        fallback_notional_usdc=25.0,
    )


def _cnl_safe_lineage(candidate):
    return {
        "kind": CNL_SAFE_LINEAGE_KIND,
        "source_reject_reason": "no_codex_v1_lane_match",
        "source_classifier_lane": "UNKNOWN",
        "mapped_shadow_lane": "SH_WPR_L_S1",
        "promotion_source": "no_lane_shadow_reprice_canary",
        "effective_lane": "CNL-WPR-L",
        "market_state": candidate.market_state,
    }


@pytest.mark.parametrize("rule_id", sorted(V1462_ALLOWED_CONTROL_RULE_IDS))
def test_only_explicit_v1460_control_rules_are_live(rule_id: str) -> None:
    candidate = _control_candidate(rule_id)
    decision = evaluate_strict_admission(
        matrix_rule_id=rule_id,
        raw_accepted=rule_id != "v1460.cnl_reclaim.control",
        pre_gate_accepted=True,
        final_incumbent_accepted=True,
        candidate=candidate,
        safe_lineage=(
            _cnl_safe_lineage(candidate)
            if rule_id == "v1460.cnl_reclaim.control"
            else None
        ),
    )
    assert decision.mode is AdmissionMode.LIVE
    assert decision.permits_order is True
    if rule_id == "v1460.cnl_reclaim.control":
        assert decision.raw_accepted is False
        assert decision.reject_lineage == ()
        assert decision.safe_lineage_kind == CNL_SAFE_LINEAGE_KIND


@pytest.mark.parametrize(
    "rule_id",
    [
        "v1460.state.unknown_probation_0_5",
        "v1460.stup_weak.probation_0_5",
        "v1460.other.incumbent_fallback",
        "v1460.incumbent.rejected",
        "UNKNOWN",
    ],
)
def test_probation_fallback_and_unknown_rules_are_shadow(rule_id: str) -> None:
    decision = evaluate_strict_admission(
        matrix_rule_id=rule_id,
        raw_accepted=True,
        pre_gate_accepted=True,
        final_incumbent_accepted=True,
    )
    assert decision.mode is AdmissionMode.SHADOW
    assert decision.permits_order is False
    assert decision.reason == "v1462.shadow.rule_not_allowlisted"


@pytest.mark.parametrize(
    "lineage",
    [
        ("v1436_late_stups_after_veto_edge_block", "v1461_fast_probe_reopen"),
        ("v1445_stups_clean_quality_block", "v1461_probation_reopen"),
    ],
)
def test_stup_reject_then_reopen_can_never_become_live(lineage: tuple[str, ...]) -> None:
    decision = evaluate_strict_admission(
        matrix_rule_id="v1460.stup_clean.control",
        raw_accepted=True,
        pre_gate_accepted=True,
        final_incumbent_accepted=True,
        reject_lineage=lineage,
        candidate=_control_candidate("v1460.stup_clean.control"),
    )
    assert decision.mode is AdmissionMode.SHADOW
    assert decision.reason == "v1462.shadow.reject_reopen_lineage"


@pytest.mark.parametrize(
    ("raw", "pre_gate", "final", "reason"),
    [
        (False, True, True, "v1462.shadow.raw_classifier_rejected"),
        (True, False, True, "v1462.shadow.pre_gate_rejected"),
        (True, True, False, "v1462.shadow.final_incumbent_rejected"),
    ],
)
def test_every_acceptance_stage_is_required(
    raw: bool, pre_gate: bool, final: bool, reason: str
) -> None:
    decision = evaluate_strict_admission(
        matrix_rule_id="v1460.rp1.control",
        raw_accepted=raw,
        pre_gate_accepted=pre_gate,
        final_incumbent_accepted=final,
        candidate=_control_candidate("v1460.rp1.control"),
    )
    assert decision.permits_order is False
    assert decision.reason == reason


def test_promotion_enforcement_configuration_fails_closed() -> None:
    decision = evaluate_strict_admission(
        matrix_rule_id="v1460.rp1.control",
        raw_accepted=True,
        pre_gate_accepted=True,
        final_incumbent_accepted=True,
        promotion_enforcement_enabled=True,
    )
    assert decision.permits_order is False
    assert decision.reason == "v1462.config.promotion_enforcement_must_be_false"


def test_open_execution_controls_fail_closed() -> None:
    decision = evaluate_strict_admission(
        matrix_rule_id="v1460.rp1.control",
        raw_accepted=True,
        pre_gate_accepted=True,
        final_incumbent_accepted=True,
        execution_controls_safe=False,
    )
    assert decision.mode is AdmissionMode.SHADOW
    assert decision.permits_order is False
    assert decision.reason == "v1462.config.execution_controls_not_closed"


def test_zeroed_reject_uses_positive_raw_ticket_with_provenance() -> None:
    raw = {
        "accepted": True,
        "lane_code": "STUP-S",
        "side": "SHORT",
        "strategy": "S1_BB_RSI",
        "entry_offset_bp": 2.0,
        "size_mult": 1.0,
        "notional_mult": 1.0,
        "requested_notional_usdc": 50.0,
        "metrics": {"market_state": "STUP-S:clean_extension", "tp1_bp": 6.0},
    }
    rejected = {
        **raw,
        "accepted": False,
        "size_mult": 0.0,
        "notional_mult": 0.0,
        "requested_notional_usdc": 0.0,
        "reason": "v1445_stups_clean_quality_block",
    }
    ticket = build_pre_reject_candidate_ticket(
        raw,
        rejected,
        fallback_notional_usdc=25.0,
    )
    assert ticket.requested_notional_usdc == pytest.approx(50.0)
    assert ticket.notional_source == "raw_classifier"
    assert ticket.raw_action == "ACCEPT"
    assert ticket.effective_action == "REJECT"
    with pytest.raises(FrozenInstanceError):
        ticket.requested_notional_usdc = 0.0  # type: ignore[misc]
    with pytest.raises(TypeError):
        ticket.action_parameters["tp1_bp"] = 0.0  # type: ignore[index]


def test_unknown_zero_notional_candidate_uses_settings_fallback() -> None:
    raw = {
        "accepted": False,
        "lane_code": None,
        "side": "LONG",
        "strategy": "S1_BB_RSI",
        "requested_notional_usdc": 0.0,
        "reason": "no_codex_v1_lane_match",
    }
    ticket = build_pre_reject_candidate_ticket(
        raw,
        raw,
        fallback_notional_usdc=25.0,
    )
    assert ticket.requested_notional_usdc == pytest.approx(25.0)
    assert ticket.notional_source == "settings_fallback"
    assert ticket.classifier_lane == "UNKNOWN"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("strategy", "S2_SuperTrend", "v1463.shadow.strategy_mismatch"),
        ("classifier_side", "LONG", "v1463.shadow.classifier_side_mismatch"),
        ("effective_lane", "S1P-L", "v1463.shadow.effective_lane_mismatch"),
        ("market_state", "STUP-S:weak_chop", "v1463.shadow.state_not_live"),
    ],
)
def test_registry_identity_mismatch_fails_closed(field: str, value: str, reason: str) -> None:
    from dataclasses import replace

    candidate = replace(_control_candidate("v1460.stup_clean.control"), **{field: value})
    decision = evaluate_strict_admission(
        matrix_rule_id="v1460.stup_clean.control",
        raw_accepted=True,
        pre_gate_accepted=True,
        final_incumbent_accepted=True,
        candidate=candidate,
    )
    assert decision.permits_order is False
    assert decision.reason == reason


def test_cnl_generic_reopen_without_exact_safe_lineage_is_shadow() -> None:
    candidate = _control_candidate("v1460.cnl_reclaim.control")
    decision = evaluate_strict_admission(
        matrix_rule_id="v1460.cnl_reclaim.control",
        raw_accepted=False,
        pre_gate_accepted=True,
        final_incumbent_accepted=True,
        candidate=candidate,
        safe_lineage={**_cnl_safe_lineage(candidate), "mapped_shadow_lane": "SH_OTHER"},
    )
    assert decision.permits_order is False
    assert decision.reason == "v1463.shadow.cnl_safe_lineage_invalid"


def test_registry_profile_geometry_mismatch_fails_closed() -> None:
    from dataclasses import replace

    candidate = _control_candidate("v1460.s1p_pullback.control")
    candidate = replace(
        candidate,
        action_parameters={**candidate.action_parameters, "sl_bp": 8.0},
    )
    decision = evaluate_strict_admission(
        matrix_rule_id="v1460.s1p_pullback.control",
        raw_accepted=True,
        pre_gate_accepted=True,
        final_incumbent_accepted=True,
        candidate=candidate,
    )
    assert decision.permits_order is False
    assert decision.reason == "v1463.shadow.execution_profile_mismatch"
