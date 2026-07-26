"""Pure v1.4.64 adaptive-promotion policy.

The module has no database, exchange, order, or runtime dependency.  It
canonicalizes an executable candidate profile and evaluates a bounded,
fail-closed lease state machine.  Persistence, atomic lease claims, and order
submission belong to runtime integration code.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from enum import Enum
import hashlib
import json
from math import isfinite
from types import MappingProxyType
from typing import Any, Mapping, Sequence


V1464_VERSION = "v1.4.64"
V1464_POLICY_NAME = "codex-v1.4.64-adaptive-promotion-lease"
_FLOAT_QUANTUM = Decimal("0.000001")
_ABSOLUTE_PRICE_FIELDS = frozenset(
    {
        "entry",
        "entry_price",
        "entry_limit_price",
        "reference_price",
        "signal_price",
        "tp",
        "tp_price",
        "tp1_price",
        "full_tp_price",
        "sl",
        "sl_price",
        "stop",
        "stop_loss",
        "mark_price",
    }
)
_RUNTIME_FIELDS = frozenset(
    {
        "run_id",
        "runtime_id",
        "opportunity_id",
        "sample_id",
        "observed_at_ms",
        "decision_at_ms",
        "recorded_at_ms",
        "resolved_at_ms",
        "event_time_ms",
        "entry_order_id",
        "client_order_id",
    }
)
_ACTION_IDENTIFIER_FIELDS = (
    "profile_id",
    "profile_anchor",
    "profile_patch",
    "action_id",
    "adaptive_tp_engine",
    "entry_model",
)
_ANCHOR_FIELDS = ("full_tp_anchor", "tp_anchor", "sl_anchor")


class PromotionState(str, Enum):
    SHADOW = "SHADOW"
    PROBATION = "PROBATION"
    LIVE = "LIVE"
    COOLDOWN = "COOLDOWN"
    HALTED = "HALTED"


def _canonical_text(value: Any, *, upper: bool = False) -> str:
    rendered = str(value or "").strip()
    return rendered.upper() if upper else rendered


def _source_value(source: Any, *keys: str) -> Any:
    if isinstance(source, Mapping):
        for key in keys:
            value = source.get(key)
            if value is not None:
                return value
        return None
    for key in keys:
        value = getattr(source, key, None)
        if value is not None:
            return value
    return None


def _mapping_value(source: Any, key: str) -> Mapping[str, Any]:
    value = _source_value(source, key)
    return value if isinstance(value, Mapping) else {}


def _canonical_float(value: Any, field: str) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not number.is_finite():
        raise ValueError(f"{field} must be a finite number")
    rounded = number.quantize(_FLOAT_QUANTUM, rounding=ROUND_HALF_EVEN)
    if rounded == 0:
        rounded = Decimal(0)
    return float(rounded)


def _canonical_seconds(value: Any, field: str) -> int | None:
    number = _canonical_float(value, field)
    if number is None:
        return None
    if number < 0 or not float(number).is_integer():
        raise ValueError(f"{field} must be a non-negative whole number of seconds")
    return int(number)


def _first_value(sources: Sequence[Any], keys: Sequence[str]) -> Any:
    for source in sources:
        value = _source_value(source, *keys)
        if value is not None:
            return value
    return None


def canonicalize_stable_profile(
    candidate_ticket: Any,
    frozen_plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the canonical cohort profile for a ticket and frozen plan.

    Only identity and discrete execution geometry survive.  Absolute market
    prices, timestamps, order ids, runtime ids, and opportunity ids are
    intentionally excluded.  Numeric geometry is rounded to six decimals so
    harmless binary-float noise cannot split one cohort.
    """

    if candidate_ticket is None:
        raise TypeError("candidate_ticket is required")
    if not isinstance(frozen_plan, Mapping):
        raise TypeError("frozen_plan must be a mapping")

    ticket_action = _mapping_value(candidate_ticket, "action_parameters")
    plan_action = _mapping_value(frozen_plan, "action_parameters")
    # An actual frozen plan is authoritative, including its nested action
    # profile.  Before that plan exists, however, the ticket's explicit
    # execution fields are the resolved values and must not be masked by a
    # stale/raw alias such as action_parameters.entry_bp.
    sources: tuple[Any, ...] = (
        (frozen_plan, candidate_ticket, plan_action, ticket_action)
        if frozen_plan
        else (candidate_ticket, ticket_action)
    )

    lane = _first_value(
        sources,
        ("effective_lane", "lane_code", "lane", "classifier_lane"),
    )
    effective_side = _first_value(
        sources,
        ("effective_side", "side", "classifier_side"),
    )
    classifier_side = _first_value(
        sources,
        ("classifier_side", "side", "effective_side"),
    )
    strategy = _first_value(sources, ("strategy",))
    market_state = _first_value(
        sources,
        ("market_state", "state", "regime", "v143_market_state"),
    )
    geometry = {
        "entry_offset_bp": _canonical_float(
            _first_value(sources, ("entry_offset_bp", "entry_bp")),
            "entry_offset_bp",
        ),
        "tp1_bp": _canonical_float(
            _first_value(sources, ("tp1_bp", "tp_bp")),
            "tp1_bp",
        ),
        "sl_bp": _canonical_float(
            _first_value(sources, ("sl_bp", "max_sl_bp", "wpr_max_sl_bp")),
            "sl_bp",
        ),
        "full_tp_bp": _canonical_float(
            _first_value(sources, ("full_tp_bp",)),
            "full_tp_bp",
        ),
        "partial_exit_pct": _canonical_float(
            _first_value(
                sources,
                ("partial_exit_pct", "wpr_partial_exit_pct", "wpr_partial_tp_pct"),
            ),
            "partial_exit_pct",
        ),
        "entry_ttl_s": _canonical_seconds(
            _first_value(sources, ("entry_ttl_s", "ttl_s")),
            "entry_ttl_s",
        ),
        "outcome_ttl_s": _canonical_seconds(
            _first_value(
                sources,
                ("outcome_ttl_s", "max_hold_s", "hold_s"),
            ),
            "outcome_ttl_s",
        ),
    }
    anchors = {
        field: _canonical_text(_first_value(sources, (field,)))
        for field in _ANCHOR_FIELDS
        if _canonical_text(_first_value(sources, (field,)))
    }
    action_profile = {
        field: _canonical_text(_first_value(sources, (field,)))
        for field in _ACTION_IDENTIFIER_FIELDS
        if _canonical_text(_first_value(sources, (field,)))
    }

    canonical = {
        "schema": "v1464.stable-profile.1",
        "lane": _canonical_text(lane, upper=True) or "UNKNOWN",
        "classifier_side": _canonical_text(classifier_side, upper=True) or "UNKNOWN",
        "effective_side": _canonical_text(effective_side, upper=True) or "UNKNOWN",
        "strategy": _canonical_text(strategy) or "UNKNOWN",
        "market_state": _canonical_text(market_state) or "UNKNOWN",
        "geometry": geometry,
        "anchors": anchors,
        "action_profile": action_profile,
    }

    # These assertions document the negative contract and protect future edits.
    forbidden = _ABSOLUTE_PRICE_FIELDS | _RUNTIME_FIELDS
    if forbidden.intersection(canonical):
        raise AssertionError("canonical profile contains a forbidden top-level field")
    return canonical


def stable_profile_hash(
    candidate_ticket: Any,
    frozen_plan: Mapping[str, Any],
) -> str:
    payload = canonicalize_stable_profile(candidate_ticket, frozen_plan)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def promotion_cohort_key(profile_hash: str, policy_hash: str) -> str:
    profile = _canonical_text(profile_hash)
    policy = _canonical_text(policy_hash)
    if not profile or not policy:
        raise ValueError("profile_hash and policy_hash must be non-empty")
    encoded = f"{V1464_VERSION}|{profile}|{policy}".encode("utf-8")
    return "v1464_" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class AdaptivePromotionConfig:
    version: str = V1464_VERSION
    policy_name: str = V1464_POLICY_NAME
    profile_schema: str = "v1464.stable-profile.1"
    evidence_contract_version: str = "v1464.sliding-evidence.1"
    evidence_window_seconds: int = 90 * 60
    evidence_max_age_seconds: int = 90 * 60
    lease_ttl_seconds: int = 15 * 60
    cooldown_seconds: int = 15 * 60
    probation_min_evaluable: int = 4
    probation_min_tp_first: int = 3
    live_min_evaluable: int = 6
    live_min_tp_first: int = 4
    live_min_paid_complete: int = 3
    live_min_paid_wins: int = 2
    retain_min_evaluable: int = 4
    retain_min_tp_first: int = 3
    soft_breach_limit: int = 2
    probation_notional_cap_usdc: float = 25.0
    live_notional_cap_usdc: float = 50.0
    consecutive_paid_loss_limit: int = 2
    lane_net_loss_cap_usdc: float = 0.12
    cohort_net_loss_cap_usdc: float = 0.30
    runner_enabled: bool = False
    one_step_reprice_enabled: bool = False
    dca_enabled: bool = False

    def __post_init__(self) -> None:
        for name in (
            "version",
            "policy_name",
            "profile_schema",
            "evidence_contract_version",
        ):
            if not _canonical_text(getattr(self, name)):
                raise ValueError(f"{name} must be non-empty")
        for name in (
            "evidence_window_seconds",
            "evidence_max_age_seconds",
            "lease_ttl_seconds",
            "cooldown_seconds",
            "probation_min_evaluable",
            "probation_min_tp_first",
            "live_min_evaluable",
            "live_min_tp_first",
            "live_min_paid_complete",
            "live_min_paid_wins",
            "retain_min_evaluable",
            "retain_min_tp_first",
            "soft_breach_limit",
            "consecutive_paid_loss_limit",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.probation_min_evaluable < 4 or self.probation_min_tp_first < 3:
            raise ValueError("probation evidence cannot be looser than 4/3")
        if self.live_min_evaluable < 6 or self.live_min_tp_first < 4:
            raise ValueError("live evidence cannot be looser than 6/4")
        if self.live_min_paid_complete < 3 or self.live_min_paid_wins < 2:
            raise ValueError("live paid evidence cannot be looser than 3/2")
        if self.retain_min_evaluable < 4 or self.retain_min_tp_first < 3:
            raise ValueError("retain evidence cannot be looser than 4/3")
        if self.probation_min_tp_first > self.probation_min_evaluable:
            raise ValueError("probation TP threshold exceeds evaluable threshold")
        if self.live_min_tp_first > self.live_min_evaluable:
            raise ValueError("live TP threshold exceeds evaluable threshold")
        if self.live_min_paid_wins > self.live_min_paid_complete:
            raise ValueError("paid wins exceed paid completes")
        if self.retain_min_tp_first > self.retain_min_evaluable:
            raise ValueError("retain TP threshold exceeds evaluable threshold")
        if self.soft_breach_limit < 2:
            raise ValueError("soft_breach_limit must be at least two")
        for name, upper in (
            ("probation_notional_cap_usdc", 25.0),
            ("live_notional_cap_usdc", 50.0),
            ("lane_net_loss_cap_usdc", 0.12),
            ("cohort_net_loss_cap_usdc", 0.30),
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(float(value))
                or not 0.0 < float(value) <= upper
            ):
                raise ValueError(f"{name} must be in (0, {upper}]")
        if self.runner_enabled or self.one_step_reprice_enabled or self.dca_enabled:
            raise ValueError("runner, one-step reprice, and DCA are closed")

    @property
    def policy_hash(self) -> str:
        payload = {
            "config": asdict(self),
            "states": [state.value for state in PromotionState],
            "comparators": {
                "probation_ev": ">0",
                "live_paid_net": ">0",
                "retain_ev": ">=0",
                "fresh": "age<=max",
                "window": "start>=now-window,end<=now",
                "lane_loss": "<=-cap",
                "cohort_loss": "<=-cap",
                "renew": "new_revision_and_newer_snapshot",
                "notional": "min(candidate,stage_cap)",
            },
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class PromotionEvidenceSnapshot:
    evidence_revision: str
    snapshot_at_ms: int
    window_started_at_ms: int
    window_ended_at_ms: int
    last_outcome_at_ms: int | None
    last_outcome: str | None
    opportunities: int
    evaluable: int
    tp_first: int
    sl_first: int = 0
    max_hold: int = 0
    no_fill: int = 0
    fee_net_pnl_usdc: float = 0.0
    paid_complete: int = 0
    paid_wins: int = 0
    paid_net_pnl_usdc: float = 0.0
    data_complete: bool = True
    identity_conflicts: int = 0
    data_conflicts: int = 0
    incomplete: int = 0
    ambiguous: int = 0
    dropped: int = 0
    overdue: int = 0

    def __post_init__(self) -> None:
        if not _canonical_text(self.evidence_revision):
            raise ValueError("evidence_revision must be non-empty")
        for name in (
            "snapshot_at_ms",
            "window_started_at_ms",
            "window_ended_at_ms",
            "opportunities",
            "evaluable",
            "tp_first",
            "sl_first",
            "max_hold",
            "no_fill",
            "paid_complete",
            "paid_wins",
            "identity_conflicts",
            "data_conflicts",
            "incomplete",
            "ambiguous",
            "dropped",
            "overdue",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.last_outcome_at_ms is not None and (
            isinstance(self.last_outcome_at_ms, bool)
            or not isinstance(self.last_outcome_at_ms, int)
            or self.last_outcome_at_ms < 0
        ):
            raise ValueError("last_outcome_at_ms must be a non-negative integer or None")
        if self.tp_first + self.sl_first + self.max_hold > self.evaluable:
            raise ValueError("first-touch outcomes exceed evaluable count")
        if self.paid_wins > self.paid_complete:
            raise ValueError("paid wins exceed paid completes")
        for name in ("fee_net_pnl_usdc", "paid_net_pnl_usdc"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        if not isinstance(self.data_complete, bool):
            raise ValueError("data_complete must be bool")

    @property
    def fee_net_ev_per_opportunity_usdc(self) -> float:
        return self.fee_net_pnl_usdc / self.opportunities if self.opportunities else 0.0


@dataclass(frozen=True, slots=True)
class PromotionRegimeInput:
    supportive: bool = False
    confirmed: bool = False
    fresh: bool = False
    exact_cohort_match: bool = False

    def __post_init__(self) -> None:
        for name in ("supportive", "confirmed", "fresh", "exact_cohort_match"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be bool")


@dataclass(frozen=True, slots=True)
class PromotionRiskInput:
    raw_accepted: bool = True
    pre_gate_accepted: bool = True
    final_incumbent_accepted: bool = True
    reject_lineage: tuple[str, ...] = ()
    identity_valid: bool = True
    integrity_safe: bool = True
    execution_controls_safe: bool = True
    database_healthy: bool = True
    global_halted: bool = False
    consecutive_paid_losses: int = 0
    lane_net_pnl_usdc: float = 0.0
    cohort_net_pnl_usdc: float = 0.0

    def __post_init__(self) -> None:
        for name in (
            "raw_accepted",
            "pre_gate_accepted",
            "final_incumbent_accepted",
            "identity_valid",
            "integrity_safe",
            "execution_controls_safe",
            "database_healthy",
            "global_halted",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be bool")
        if (
            isinstance(self.consecutive_paid_losses, bool)
            or not isinstance(self.consecutive_paid_losses, int)
            or self.consecutive_paid_losses < 0
        ):
            raise ValueError("consecutive_paid_losses must be a non-negative integer")
        for name in ("lane_net_pnl_usdc", "cohort_net_pnl_usdc"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        normalized = tuple(
            _canonical_text(item) for item in self.reject_lineage if _canonical_text(item)
        )
        object.__setattr__(self, "reject_lineage", normalized)


@dataclass(frozen=True, slots=True)
class PromotionLeaseSnapshot:
    state: PromotionState = PromotionState.SHADOW
    lease_id: str | None = None
    cohort_key: str | None = None
    policy_hash: str | None = None
    issued_at_ms: int | None = None
    expires_at_ms: int | None = None
    evidence_revision: str | None = None
    evidence_as_of_ms: int | None = None
    soft_breach_count: int = 0
    cooldown_until_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, PromotionState):
            object.__setattr__(self, "state", PromotionState(str(self.state).upper()))
        for name in (
            "issued_at_ms",
            "expires_at_ms",
            "evidence_as_of_ms",
            "cooldown_until_ms",
        ):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or None")
        if (
            isinstance(self.soft_breach_count, bool)
            or not isinstance(self.soft_breach_count, int)
            or self.soft_breach_count < 0
        ):
            raise ValueError("soft_breach_count must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class AdaptivePromotionDecision:
    state: PromotionState
    permits_order: bool
    max_notional_usdc: float
    applied_notional_usdc: float
    lease_expires_at_ms: int | None
    issue_new_lease: bool
    revoke_existing_lease: bool
    soft_breach_count: int
    reason: str
    profile_hash: str
    cohort_key: str
    policy_hash: str
    evidence_revision: str
    telemetry: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "telemetry", MappingProxyType(dict(self.telemetry)))


def _recent_evidence(
    evidence: PromotionEvidenceSnapshot,
    *,
    now_ms: int,
    config: AdaptivePromotionConfig,
) -> tuple[bool, str]:
    if evidence.snapshot_at_ms > now_ms or evidence.window_ended_at_ms > now_ms:
        return False, "future_evidence_timestamp"
    if evidence.window_started_at_ms > evidence.window_ended_at_ms:
        return False, "invalid_evidence_window"
    cutoff_ms = now_ms - config.evidence_window_seconds * 1000
    if evidence.window_started_at_ms < cutoff_ms:
        return False, "evidence_window_not_recent"
    if evidence.last_outcome_at_ms is None:
        return False, "last_outcome_missing"
    if evidence.last_outcome_at_ms > now_ms:
        return False, "last_outcome_in_future"
    if now_ms - evidence.last_outcome_at_ms > config.evidence_max_age_seconds * 1000:
        return False, "evidence_stale"
    return True, "recent"


def _data_blocker(evidence: PromotionEvidenceSnapshot) -> str | None:
    if not evidence.data_complete:
        return "data_incomplete"
    for name in (
        "identity_conflicts",
        "data_conflicts",
        "incomplete",
        "ambiguous",
        "dropped",
        "overdue",
    ):
        if int(getattr(evidence, name)) > 0:
            return name
    return None


def _probation_pass(
    evidence: PromotionEvidenceSnapshot,
    config: AdaptivePromotionConfig,
) -> bool:
    return bool(
        evidence.evaluable >= config.probation_min_evaluable
        and evidence.tp_first >= config.probation_min_tp_first
        and evidence.fee_net_ev_per_opportunity_usdc > 0.0
        and _canonical_text(evidence.last_outcome).lower()
        not in {"sl", "sl_first", "ambiguous_both"}
    )


def _live_pass(
    evidence: PromotionEvidenceSnapshot,
    config: AdaptivePromotionConfig,
) -> bool:
    return bool(
        evidence.evaluable >= config.live_min_evaluable
        and evidence.tp_first >= config.live_min_tp_first
        and evidence.fee_net_ev_per_opportunity_usdc > 0.0
        and _canonical_text(evidence.last_outcome).lower()
        not in {"sl", "sl_first", "ambiguous_both"}
        and evidence.paid_complete >= config.live_min_paid_complete
        and evidence.paid_wins >= config.live_min_paid_wins
        and evidence.paid_net_pnl_usdc > 0.0
    )


def _retain_pass(
    evidence: PromotionEvidenceSnapshot,
    config: AdaptivePromotionConfig,
) -> bool:
    return bool(
        evidence.evaluable >= config.retain_min_evaluable
        and evidence.tp_first >= config.retain_min_tp_first
        and evidence.fee_net_ev_per_opportunity_usdc >= 0.0
    )


def _fresh_since_lease(
    evidence: PromotionEvidenceSnapshot,
    lease: PromotionLeaseSnapshot,
) -> bool:
    if not lease.evidence_revision or lease.evidence_as_of_ms is None:
        return False
    return bool(
        evidence.evidence_revision != lease.evidence_revision
        and evidence.snapshot_at_ms > lease.evidence_as_of_ms
    )


def _telemetry(
    *,
    state: PromotionState,
    evidence: PromotionEvidenceSnapshot,
    regime: PromotionRegimeInput,
    risk: PromotionRiskInput,
    reason: str,
    probation_pass: bool,
    live_pass: bool,
    retain_pass: bool,
    lease_fresh_evidence: bool,
    prior_state: PromotionState,
    soft_breach_count: int,
) -> dict[str, Any]:
    return {
        "state": state.value,
        "prior_state": prior_state.value,
        "reason": reason,
        "evidence": {
            "revision": evidence.evidence_revision,
            "opportunities": evidence.opportunities,
            "evaluable": evidence.evaluable,
            "tp_first": evidence.tp_first,
            "fee_net_ev_per_opportunity_usdc": (
                evidence.fee_net_ev_per_opportunity_usdc
            ),
            "paid_complete": evidence.paid_complete,
            "paid_wins": evidence.paid_wins,
            "paid_net_pnl_usdc": evidence.paid_net_pnl_usdc,
            "probation_pass": probation_pass,
            "live_pass": live_pass,
            "retain_pass": retain_pass,
            "fresh_since_lease": lease_fresh_evidence,
        },
        "regime": {
            "supportive": regime.supportive,
            "confirmed": regime.confirmed,
            "fresh": regime.fresh,
            "exact_cohort_match": regime.exact_cohort_match,
        },
        "risk": {
            "raw_accepted": risk.raw_accepted,
            "pre_gate_accepted": risk.pre_gate_accepted,
            "final_incumbent_accepted": risk.final_incumbent_accepted,
            "reject_lineage": list(risk.reject_lineage),
            "identity_valid": risk.identity_valid,
            "integrity_safe": risk.integrity_safe,
            "execution_controls_safe": risk.execution_controls_safe,
            "database_healthy": risk.database_healthy,
            "global_halted": risk.global_halted,
            "consecutive_paid_losses": risk.consecutive_paid_losses,
            "lane_net_pnl_usdc": risk.lane_net_pnl_usdc,
            "cohort_net_pnl_usdc": risk.cohort_net_pnl_usdc,
        },
        "soft_breach_count": soft_breach_count,
        "execution_controls": {
            "runner_enabled": False,
            "one_step_reprice_enabled": False,
            "dca_enabled": False,
        },
    }


def select_adaptive_promotion_decision(
    *,
    profile_hash: str,
    cohort_key: str | None = None,
    candidate_notional_usdc: float,
    evidence: PromotionEvidenceSnapshot,
    regime: PromotionRegimeInput,
    risk: PromotionRiskInput,
    now_ms: int,
    existing_lease: PromotionLeaseSnapshot | None = None,
    config: AdaptivePromotionConfig | None = None,
) -> AdaptivePromotionDecision:
    """Evaluate one candidate without mutating runtime or persistence state."""

    active = config or AdaptivePromotionConfig()
    if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
        raise ValueError("now_ms must be a non-negative integer")
    if (
        isinstance(candidate_notional_usdc, bool)
        or not isinstance(candidate_notional_usdc, (int, float))
        or not isfinite(float(candidate_notional_usdc))
    ):
        raise ValueError("candidate_notional_usdc must be finite")
    normalized_profile_hash = _canonical_text(profile_hash)
    if not normalized_profile_hash:
        raise ValueError("profile_hash must be non-empty")

    lease = existing_lease or PromotionLeaseSnapshot()
    policy_hash = active.policy_hash
    resolved_cohort_key = (
        _canonical_text(cohort_key)
        if cohort_key is not None
        else promotion_cohort_key(normalized_profile_hash, policy_hash)
    )
    if not resolved_cohort_key:
        raise ValueError("cohort_key must be non-empty when provided")
    lease_matches = bool(
        lease.cohort_key == resolved_cohort_key and lease.policy_hash == policy_hash
    )
    prior_state = lease.state if lease_matches else PromotionState.SHADOW
    recent, recent_reason = _recent_evidence(evidence, now_ms=now_ms, config=active)
    data_blocker = _data_blocker(evidence)
    probation_pass = _probation_pass(evidence, active)
    live_pass = _live_pass(evidence, active)
    retain_pass = _retain_pass(evidence, active)
    fresh_since_lease = bool(lease_matches and _fresh_since_lease(evidence, lease))

    def decision(
        state: PromotionState,
        reason: str,
        *,
        issue: bool = False,
        revoke: bool = False,
        expires_at_ms: int | None = None,
        soft_breaches: int = 0,
        preserve_authority: bool = False,
    ) -> AdaptivePromotionDecision:
        permits = state in {PromotionState.PROBATION, PromotionState.LIVE}
        if state is PromotionState.PROBATION:
            cap = float(active.probation_notional_cap_usdc)
        elif state is PromotionState.LIVE:
            cap = float(active.live_notional_cap_usdc)
        else:
            cap = 0.0
        applied = min(max(0.0, float(candidate_notional_usdc)), cap) if permits else 0.0
        if permits and applied <= 0.0:
            permits = False
            cap = 0.0
            applied = 0.0
            state = PromotionState.SHADOW
            reason = "candidate_notional_not_positive"
            issue = False
            revoke = bool(lease_matches and lease.state in {PromotionState.PROBATION, PromotionState.LIVE})
            expires_at_ms = None
        if preserve_authority and expires_at_ms is None:
            expires_at_ms = lease.expires_at_ms
        telemetry = _telemetry(
            state=state,
            evidence=evidence,
            regime=regime,
            risk=risk,
            reason=reason,
            probation_pass=probation_pass,
            live_pass=live_pass,
            retain_pass=retain_pass,
            lease_fresh_evidence=fresh_since_lease,
            prior_state=prior_state,
            soft_breach_count=soft_breaches,
        )
        return AdaptivePromotionDecision(
            state=state,
            permits_order=permits,
            max_notional_usdc=cap,
            applied_notional_usdc=applied,
            lease_expires_at_ms=expires_at_ms,
            issue_new_lease=issue,
            revoke_existing_lease=revoke,
            soft_breach_count=soft_breaches,
            reason=reason,
            profile_hash=normalized_profile_hash,
            cohort_key=resolved_cohort_key,
            policy_hash=policy_hash,
            evidence_revision=evidence.evidence_revision,
            telemetry=telemetry,
        )

    # Scope-level failures are sticky halts and never preserve paid authority.
    if lease_matches and lease.state is PromotionState.HALTED:
        return decision(PromotionState.HALTED, "existing_halt", revoke=False)
    if risk.global_halted:
        return decision(PromotionState.HALTED, "global_halt", revoke=True)
    if not risk.database_healthy:
        return decision(PromotionState.HALTED, "database_unhealthy", revoke=True)
    if not risk.integrity_safe:
        return decision(PromotionState.HALTED, "integrity_unsafe", revoke=True)
    if not risk.execution_controls_safe:
        return decision(PromotionState.HALTED, "execution_controls_unsafe", revoke=True)

    # Admission is monotonic: no downstream adaptive fact may reopen a reject.
    for accepted, reason in (
        (risk.raw_accepted, "raw_rejected"),
        (risk.pre_gate_accepted, "pre_gate_rejected"),
        (risk.final_incumbent_accepted, "final_incumbent_rejected"),
        (risk.identity_valid, "identity_invalid"),
    ):
        if not accepted:
            return decision(PromotionState.SHADOW, reason, revoke=True)
    if risk.reject_lineage:
        return decision(PromotionState.SHADOW, "reject_lineage_present", revoke=True)

    if data_blocker is not None:
        return decision(PromotionState.SHADOW, f"evidence_{data_blocker}", revoke=True)
    if not recent:
        return decision(PromotionState.SHADOW, recent_reason, revoke=True)
    for compatible, reason in (
        (regime.supportive, "regime_not_supportive"),
        (regime.confirmed, "regime_unconfirmed"),
        (regime.fresh, "regime_stale"),
        (regime.exact_cohort_match, "regime_cohort_mismatch"),
    ):
        if not compatible:
            return decision(PromotionState.SHADOW, reason, revoke=True)

    if (
        risk.consecutive_paid_losses >= active.consecutive_paid_loss_limit
        or risk.lane_net_pnl_usdc <= -active.lane_net_loss_cap_usdc
        or risk.cohort_net_pnl_usdc <= -active.cohort_net_loss_cap_usdc
    ):
        return decision(
            PromotionState.COOLDOWN,
            "paid_risk_quarantine",
            revoke=True,
            expires_at_ms=now_ms + active.cooldown_seconds * 1000,
        )

    if (
        lease_matches
        and lease.state is PromotionState.COOLDOWN
        and lease.cooldown_until_ms is not None
        and now_ms < lease.cooldown_until_ms
    ):
        return decision(
            PromotionState.COOLDOWN,
            "cooldown_active",
            expires_at_ms=lease.cooldown_until_ms,
        )

    paid_state = prior_state in {PromotionState.PROBATION, PromotionState.LIVE}
    expired = bool(
        paid_state
        and (lease.expires_at_ms is None or now_ms >= lease.expires_at_ms)
    )
    if expired:
        if not fresh_since_lease:
            return decision(
                PromotionState.SHADOW,
                "lease_expired_without_fresh_evidence",
                revoke=True,
            )
        if prior_state is PromotionState.LIVE and live_pass:
            return decision(
                PromotionState.LIVE,
                "live_lease_renewed",
                issue=True,
                revoke=True,
                expires_at_ms=now_ms + active.lease_ttl_seconds * 1000,
            )
        if probation_pass:
            next_state = (
                PromotionState.LIVE
                if prior_state is PromotionState.PROBATION and live_pass
                else PromotionState.PROBATION
            )
            return decision(
                next_state,
                "lease_renewed_with_fresh_evidence",
                issue=True,
                revoke=True,
                expires_at_ms=now_ms + active.lease_ttl_seconds * 1000,
            )
        return decision(
            PromotionState.SHADOW,
            "lease_expired_evidence_below_entry_floor",
            revoke=True,
        )

    if prior_state is PromotionState.PROBATION and live_pass:
        return decision(
            PromotionState.LIVE,
            "paid_probation_passed",
            issue=True,
            revoke=True,
            expires_at_ms=now_ms + active.lease_ttl_seconds * 1000,
        )

    if paid_state:
        if retain_pass:
            return decision(
                prior_state,
                "lease_retained",
                expires_at_ms=lease.expires_at_ms,
                soft_breaches=0,
                preserve_authority=True,
            )
        breaches = lease.soft_breach_count + 1
        if breaches < active.soft_breach_limit:
            return decision(
                prior_state,
                "soft_retain_breach_pending",
                expires_at_ms=lease.expires_at_ms,
                soft_breaches=breaches,
                preserve_authority=True,
            )
        return decision(
            PromotionState.SHADOW,
            "soft_retain_breach_limit",
            revoke=True,
            soft_breaches=breaches,
        )

    if probation_pass:
        return decision(
            PromotionState.PROBATION,
            "recent_shadow_evidence_passed",
            issue=True,
            expires_at_ms=now_ms + active.lease_ttl_seconds * 1000,
        )
    return decision(PromotionState.SHADOW, "shadow_evidence_insufficient")


__all__ = [
    "AdaptivePromotionConfig",
    "AdaptivePromotionDecision",
    "PromotionEvidenceSnapshot",
    "PromotionLeaseSnapshot",
    "PromotionRegimeInput",
    "PromotionRiskInput",
    "PromotionState",
    "V1464_POLICY_NAME",
    "V1464_VERSION",
    "canonicalize_stable_profile",
    "promotion_cohort_key",
    "select_adaptive_promotion_decision",
    "stable_profile_hash",
]
