"""Fail-closed v1.4.63 live admission and immutable Shadow ticket helpers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from math import isclose, isfinite
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from src.gridbot.mainnet.v1462_lane_registry import (
    CNL_SAFE_LINEAGE_KIND,
    LIVE_CONTROL_RULE_IDS,
    LaneMode,
    lane_for,
    live_control_contract,
    state_mode,
    state_profile_for,
)


V1462_VERSION = "v1.4.63"
V1462_POLICY_NAME = "codex-v1.4.63-strict-registry-live-allowlist"

V1462_ALLOWED_CONTROL_RULE_IDS = LIVE_CONTROL_RULE_IDS


class AdmissionMode(str, Enum):
    LIVE = "LIVE"
    SHADOW = "SHADOW"


def _finite_positive(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not isfinite(parsed) or parsed <= 0.0:
        return None
    return parsed


def _text(value: Any, fallback: str = "") -> str:
    rendered = str(value or "").strip()
    return rendered or fallback


@dataclass(frozen=True, slots=True)
class PreRejectCandidateTicket:
    """Immutable candidate identity and executable counterfactual before a reject.

    A rejected ``CodexV1Decision`` normally has zeroed sizing.  The ticket keeps
    the effective identity/action parameters while selecting a strictly positive
    notional from the last usable source and records that provenance.
    """

    classifier_lane: str
    effective_lane: str
    classifier_side: str
    effective_side: str
    strategy: str
    raw_action: str
    effective_action: str
    entry_offset_bp: float
    size_mult: float
    notional_mult: float
    requested_notional_usdc: float
    notional_source: str
    market_state: str
    policy_tag: str
    reason: str
    action_parameters: Mapping[str, Any]

    def __post_init__(self) -> None:
        if _finite_positive(self.requested_notional_usdc) is None:
            raise ValueError("requested_notional_usdc must remain positive")
        object.__setattr__(
            self,
            "action_parameters",
            MappingProxyType(dict(self.action_parameters)),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "classifier_lane": self.classifier_lane,
            "effective_lane": self.effective_lane,
            "classifier_side": self.classifier_side,
            "effective_side": self.effective_side,
            "strategy": self.strategy,
            "raw_action": self.raw_action,
            "effective_action": self.effective_action,
            "entry_offset_bp": self.entry_offset_bp,
            "size_mult": self.size_mult,
            "notional_mult": self.notional_mult,
            "requested_notional_usdc": self.requested_notional_usdc,
            "notional_source": self.notional_source,
            "market_state": self.market_state,
            "policy_tag": self.policy_tag,
            "reason": self.reason,
            "action_parameters": dict(self.action_parameters),
        }


def build_pre_reject_candidate_ticket(
    raw: Mapping[str, Any],
    effective: Mapping[str, Any],
    *,
    fallback_notional_usdc: float,
) -> PreRejectCandidateTicket:
    """Freeze candidate facts and repair zero-notional Shadow provenance."""

    if not isinstance(raw, Mapping) or not isinstance(effective, Mapping):
        raise TypeError("raw and effective decisions must be mappings")
    raw_metrics = raw.get("metrics") if isinstance(raw.get("metrics"), Mapping) else {}
    effective_metrics = (
        effective.get("metrics")
        if isinstance(effective.get("metrics"), Mapping)
        else {}
    )

    notional_candidates = (
        ("effective_pre_gate", effective.get("requested_notional_usdc")),
        ("raw_classifier", raw.get("requested_notional_usdc")),
        ("settings_fallback", fallback_notional_usdc),
    )
    notional_source = ""
    requested_notional = None
    for source, value in notional_candidates:
        parsed = _finite_positive(value)
        if parsed is not None:
            notional_source = source
            requested_notional = parsed
            break
    if requested_notional is None:
        raise ValueError("a positive candidate notional or fallback is required")

    def numeric(name: str, fallback: float) -> float:
        for source in (effective, raw):
            try:
                value = float(source.get(name))
            except (TypeError, ValueError, OverflowError):
                continue
            if isfinite(value) and (value > 0.0 or name == "entry_offset_bp"):
                return value
        return fallback

    market_state = _text(
        effective_metrics.get("market_state")
        or effective_metrics.get("v143_market_state")
        or effective.get("regime")
        or raw_metrics.get("market_state")
        or raw.get("regime"),
        "UNKNOWN",
    )
    # This is the frozen execution-profile surface, not an arbitrary subset of
    # strategy metrics.  Every field below can change entry, fill eligibility,
    # position sizing, exit geometry, or the observation horizon.
    action_keys = (
        "entry_bp",
        "tp1_bp",
        "full_tp_bp",
        "sl_bp",
        "be_bp",
        "ttl_s",
        "hold_s",
        "max_hold_s",
        "partial_exit_pct",
        "profit_lock_mfe_bp",
        "profit_lock_floor_bp",
        "profit_lock_giveback_bp",
        "pre_tp_profit_lock_enabled",
        "pre_tp_profit_lock_mfe_bp",
        "pre_tp_profit_lock_floor_bp",
        "pre_tp_profit_lock_method",
        "staged_entry_reprice_enabled",
        "staged_entry_bps",
        "staged_entry_delay_s",
        "adaptive_tp_engine",
        "profile_patch",
        "profile_anchor",
        "entry_model",
        "wpr_partial_tp_pct",
        "wpr_partial_exit_pct",
        "wpr_max_sl_bp",
        "applied_notional_cap_usdc",
        "fixed_notional_usdc",
        "target_side",
        "live_action",
        "v1455_action",
    )
    action_parameters = {
        key: effective_metrics.get(key, raw_metrics.get(key))
        for key in action_keys
        if effective_metrics.get(key, raw_metrics.get(key)) is not None
    }
    return PreRejectCandidateTicket(
        classifier_lane=_text(raw.get("lane_code") or raw.get("lane"), "UNKNOWN"),
        effective_lane=_text(
            effective.get("lane_code")
            or effective.get("lane")
            or raw.get("lane_code")
            or raw.get("lane"),
            "UNKNOWN",
        ),
        classifier_side=_text(raw.get("side"), "UNKNOWN").upper(),
        effective_side=_text(effective.get("side") or raw.get("side"), "UNKNOWN").upper(),
        strategy=_text(effective.get("strategy") or raw.get("strategy"), "UNKNOWN"),
        raw_action="ACCEPT" if bool(raw.get("accepted")) else "REJECT",
        effective_action="ACCEPT" if bool(effective.get("accepted")) else "REJECT",
        entry_offset_bp=numeric("entry_offset_bp", 0.0),
        size_mult=numeric("size_mult", 1.0),
        notional_mult=numeric("notional_mult", 1.0),
        requested_notional_usdc=requested_notional,
        notional_source=notional_source,
        market_state=market_state,
        policy_tag=_text(
            effective.get("policy_tag")
            or effective_metrics.get("policy_tag")
            or effective_metrics.get("policy_note")
            or raw.get("policy_tag")
        ),
        reason=_text(effective.get("reason") or raw.get("reason"), "UNKNOWN"),
        action_parameters=action_parameters,
    )


_POLICY_PAYLOAD = {
    "version": V1462_VERSION,
    "policy_name": V1462_POLICY_NAME,
    "allowed_control_rule_ids": sorted(V1462_ALLOWED_CONTROL_RULE_IDS),
    "requirements": [
        "raw_accepted_or_exact_cnl_safe_lineage",
        "pre_gate_accepted",
        "no_reject_or_reopen_lineage",
        "final_incumbent_accepted",
        "execution_controls_safe",
        "promotion_enforcement_disabled",
        "registry_lane_strategy_side_state_profile_match",
    ],
}
V1462_POLICY_HASH = hashlib.sha256(
    json.dumps(_POLICY_PAYLOAD, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


@dataclass(frozen=True, slots=True)
class StrictAdmissionDecision:
    mode: AdmissionMode
    reason: str
    matrix_rule_id: str
    permits_order: bool
    raw_accepted: bool
    pre_gate_accepted: bool
    final_incumbent_accepted: bool
    reject_lineage: tuple[str, ...]
    registry_identity_valid: bool = False
    registry_lane_code: str | None = None
    registry_profile_id: str | None = None
    safe_lineage_kind: str | None = None
    policy_hash: str = V1462_POLICY_HASH

    def to_payload(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "reason": self.reason,
            "matrix_rule_id": self.matrix_rule_id,
            "permits_order": self.permits_order,
            "raw_accepted": self.raw_accepted,
            "pre_gate_accepted": self.pre_gate_accepted,
            "final_incumbent_accepted": self.final_incumbent_accepted,
            "reject_lineage": list(self.reject_lineage),
            "registry_identity_valid": self.registry_identity_valid,
            "registry_lane_code": self.registry_lane_code,
            "registry_profile_id": self.registry_profile_id,
            "safe_lineage_kind": self.safe_lineage_kind,
            "policy_hash": self.policy_hash,
        }


def _normalize_lane(value: Any) -> str:
    return _text(value, "UNKNOWN").upper()


def _normalize_state(value: Any) -> str:
    rendered = _text(value, "UNKNOWN")
    return rendered.split(":", 1)[-1]


def _safe_cnl_lineage_valid(
    lineage: Mapping[str, Any] | None,
    candidate: PreRejectCandidateTicket,
) -> bool:
    if not isinstance(lineage, Mapping):
        return False
    return bool(
        _text(lineage.get("kind")) == CNL_SAFE_LINEAGE_KIND
        and _text(lineage.get("source_reject_reason")) == "no_codex_v1_lane_match"
        and _normalize_lane(lineage.get("source_classifier_lane")) == "UNKNOWN"
        and _normalize_lane(lineage.get("mapped_shadow_lane")) == "SH_WPR_L_S1"
        and _text(lineage.get("promotion_source"))
        == "no_lane_shadow_reprice_canary"
        and _normalize_lane(lineage.get("effective_lane")) == "CNL-WPR-L"
        and _normalize_lane(candidate.effective_lane) == "CNL-WPR-L"
        and _normalize_lane(candidate.classifier_lane) == "UNKNOWN"
        and _normalize_state(lineage.get("market_state"))
        == _normalize_state(candidate.market_state)
    )


def _numbers_match(expected: Any, actual: Any) -> bool:
    try:
        expected_f = float(expected)
        actual_f = float(actual)
    except (TypeError, ValueError, OverflowError):
        return False
    return bool(
        isfinite(expected_f)
        and isfinite(actual_f)
        and isclose(expected_f, actual_f, rel_tol=0.0, abs_tol=1e-9)
    )


def _validate_registry_identity(
    *,
    matrix_rule_id: str,
    candidate: PreRejectCandidateTicket | None,
    safe_lineage: Mapping[str, Any] | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (failure reason, lane, profile id, safe-lineage kind)."""

    if candidate is None:
        return "v1463.shadow.candidate_ticket_missing", None, None, None
    try:
        contract = live_control_contract(matrix_rule_id)
        lane = lane_for(contract.lane_code)
    except KeyError:
        return "v1463.shadow.rule_not_allowlisted", None, None, None

    effective_lane = _normalize_lane(candidate.effective_lane)
    classifier_lane = _normalize_lane(candidate.classifier_lane)
    if effective_lane != lane.lane_code:
        return "v1463.shadow.effective_lane_mismatch", lane.lane_code, None, None

    safe_kind: str | None = None
    if contract.safe_lineage_kind is None:
        if classifier_lane != lane.lane_code:
            return "v1463.shadow.classifier_lane_mismatch", lane.lane_code, None, None
    else:
        if not _safe_cnl_lineage_valid(safe_lineage, candidate):
            return "v1463.shadow.cnl_safe_lineage_invalid", lane.lane_code, None, None
        safe_kind = contract.safe_lineage_kind

    if _text(candidate.strategy) not in lane.strategies:
        return "v1463.shadow.strategy_mismatch", lane.lane_code, None, safe_kind
    if _normalize_lane(candidate.classifier_side) != lane.classifier_side:
        return "v1463.shadow.classifier_side_mismatch", lane.lane_code, None, safe_kind
    if _normalize_lane(candidate.effective_side) not in lane.effective_sides:
        return "v1463.shadow.effective_side_mismatch", lane.lane_code, None, safe_kind
    if state_mode(lane.lane_code, candidate.market_state) is not LaneMode.LIVE_ALLOWLIST:
        return "v1463.shadow.state_not_live", lane.lane_code, None, safe_kind

    state_profile = state_profile_for(lane.lane_code, candidate.market_state)
    profile = (state_profile.profile if state_profile is not None else None) or lane.default_profile
    expected_entry = profile.entry_bp if profile is not None else lane.entry_offset_bp
    if not _numbers_match(expected_entry, candidate.entry_offset_bp):
        return "v1463.shadow.entry_profile_mismatch", lane.lane_code, getattr(profile, "profile_id", None), safe_kind

    profile_id = profile.profile_id if profile is not None else f"{lane.lane_code}.incumbent"
    if profile is not None:
        values = dict(candidate.action_parameters)
        checks = (
            ("tp1_bp", profile.tp1_bp),
            ("full_tp_bp", profile.full_tp_bp),
            ("sl_bp", profile.sl_bp),
            ("be_bp", profile.be_bp),
            ("partial_exit_pct", profile.partial_exit_pct),
            ("ttl_s", profile.ttl_s),
            ("max_hold_s", profile.max_hold_s),
        )
        for key, expected in checks:
            if expected is None:
                continue
            actual = values.get(key)
            if key == "max_hold_s" and actual is None:
                actual = values.get("hold_s")
            if not _numbers_match(expected, actual):
                return "v1463.shadow.execution_profile_mismatch", lane.lane_code, profile_id, safe_kind
    return None, lane.lane_code, profile_id, safe_kind


def evaluate_strict_admission(
    *,
    matrix_rule_id: str | None,
    raw_accepted: bool,
    pre_gate_accepted: bool,
    final_incumbent_accepted: bool,
    reject_lineage: Sequence[str] = (),
    execution_controls_safe: bool = True,
    promotion_enforcement_enabled: bool = False,
    candidate: PreRejectCandidateTicket | None = None,
    safe_lineage: Mapping[str, Any] | None = None,
) -> StrictAdmissionDecision:
    """Allow only reviewed v1.4.60 CONTROL incumbents; everything else shadows."""

    rule = _text(matrix_rule_id, "UNKNOWN")
    lineage = tuple(_text(item) for item in reject_lineage if _text(item))
    identity_failure, registry_lane, registry_profile, safe_kind = (
        _validate_registry_identity(
            matrix_rule_id=rule,
            candidate=candidate,
            safe_lineage=safe_lineage,
        )
    )
    cnl_raw_exception = bool(
        safe_kind == CNL_SAFE_LINEAGE_KIND and candidate is not None
    )
    reason = "v1462.live_allowlist_control"
    if promotion_enforcement_enabled:
        reason = "v1462.config.promotion_enforcement_must_be_false"
    elif not execution_controls_safe:
        reason = "v1462.config.execution_controls_not_closed"
    elif rule not in V1462_ALLOWED_CONTROL_RULE_IDS:
        reason = "v1462.shadow.rule_not_allowlisted"
    elif identity_failure == "v1463.shadow.cnl_safe_lineage_invalid":
        reason = identity_failure
    elif not raw_accepted and not cnl_raw_exception:
        reason = "v1462.shadow.raw_classifier_rejected"
    elif not pre_gate_accepted:
        reason = "v1462.shadow.pre_gate_rejected"
    elif lineage:
        reason = "v1462.shadow.reject_reopen_lineage"
    elif not final_incumbent_accepted:
        reason = "v1462.shadow.final_incumbent_rejected"
    elif identity_failure:
        reason = identity_failure
    else:
        return StrictAdmissionDecision(
            mode=AdmissionMode.LIVE,
            reason=reason,
            matrix_rule_id=rule,
            permits_order=True,
            raw_accepted=bool(raw_accepted),
            pre_gate_accepted=bool(pre_gate_accepted),
            final_incumbent_accepted=bool(final_incumbent_accepted),
            reject_lineage=(),
            registry_identity_valid=True,
            registry_lane_code=registry_lane,
            registry_profile_id=registry_profile,
            safe_lineage_kind=safe_kind,
        )
    return StrictAdmissionDecision(
        mode=AdmissionMode.SHADOW,
        reason=reason,
        matrix_rule_id=rule,
        permits_order=False,
        raw_accepted=bool(raw_accepted),
        pre_gate_accepted=bool(pre_gate_accepted),
        final_incumbent_accepted=bool(final_incumbent_accepted),
        reject_lineage=lineage,
        registry_identity_valid=False,
        registry_lane_code=registry_lane,
        registry_profile_id=registry_profile,
        safe_lineage_kind=safe_kind,
    )


__all__ = [
    "AdmissionMode",
    "PreRejectCandidateTicket",
    "StrictAdmissionDecision",
    "V1462_ALLOWED_CONTROL_RULE_IDS",
    "V1462_POLICY_HASH",
    "V1462_POLICY_NAME",
    "V1462_VERSION",
    "build_pre_reject_candidate_ticket",
    "evaluate_strict_admission",
]
