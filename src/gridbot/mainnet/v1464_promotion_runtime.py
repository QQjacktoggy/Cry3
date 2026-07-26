"""Runtime adapter for the v1.4.64 adaptive-promotion policy.

The adapter translates immutable mainnet evidence and repository rows into the
pure lifecycle engine.  It deliberately does not own admission, order
submission, settings, or database schema.  Repository failures and compare-
and-swap conflicts remove only adaptive authority; an incumbent allowlisted
CONTROL route remains the caller's independent decision.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
from math import isfinite
from typing import Any, Mapping, Sequence

from src.gridbot.mainnet.v1462_lane_registry import (
    REGISTRY_HASH,
    REGISTRY_VERSION,
    lane_definition_hash,
    lane_for,
    state_profile_for,
)
from src.gridbot.mainnet.v1464_adaptive_promotion import (
    AdaptivePromotionConfig,
    AdaptivePromotionDecision,
    PromotionEvidenceSnapshot,
    PromotionLeaseSnapshot,
    PromotionRegimeInput,
    PromotionRiskInput,
    PromotionState,
    select_adaptive_promotion_decision,
)
from src.gridbot.storage.v1464_promotion_repository import (
    AdmissionClaimError,
    PromotionCohort,
    V1464_EVIDENCE_SCHEMA_VERSION,
    V1464_PROFILE_IDENTITY_SCHEMA,
    V1464PromotionRepository,
    lease_row_to_engine_snapshot,
)


_EVIDENCE_IDENTITY_FIELDS = (
    "environment",
    "symbol",
    "lane_code",
    "market_state",
    "effective_side",
    "strategy",
    "resolved_profile_hash",
    "profile_identity_schema",
    "registry_version",
    "registry_hash",
    "lane_definition_hash",
    "admission_policy_hash",
)
_SUPPORTED_OUTCOMES = frozenset(
    {
        "tp1_first",
        "tp_first",
        "tp",
        "sl_first",
        "sl",
        "max_hold",
        "no_fill",
        "ambiguous_both",
    }
)
_TP_OUTCOMES = frozenset({"tp1_first", "tp_first", "tp"})
_SL_OUTCOMES = frozenset({"sl_first", "sl"})
_EXCLUDED_COUNTS_AS = frozenset(
    {
        "diagnostic_only",
        "excluded_terminal",
        "out_of_registry",
    }
)
_BENIGN_DROP_REASONS = frozenset({"active_opportunity_pending"})


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any, *, upper: bool = False) -> str:
    result = str(value or "").strip()
    return result.upper() if upper else result


def _first(payload: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = payload.get(name)
        if value is not None:
            return value
    return None


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        parsed = int(value)
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not isfinite(numeric) or numeric != parsed or parsed < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return parsed


def _finite(
    value: Any,
    name: str,
    *,
    allow_none: bool = False,
) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    return parsed


def _setting(settings: Any, name: str, default: Any) -> Any:
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)


def adaptive_promotion_config_from_settings(settings: Any) -> AdaptivePromotionConfig:
    """Build the pure policy config from the current mainnet setting names."""

    return AdaptivePromotionConfig(
        evidence_window_seconds=int(
            _setting(
                settings,
                "mainnet_codex_v1464_evidence_window_seconds",
                90 * 60,
            )
        ),
        evidence_max_age_seconds=int(
            _setting(
                settings,
                "mainnet_codex_v1464_evidence_max_age_seconds",
                90 * 60,
            )
        ),
        lease_ttl_seconds=int(
            _setting(settings, "mainnet_codex_v1464_lease_ttl_seconds", 15 * 60)
        ),
        cooldown_seconds=int(
            _setting(settings, "mainnet_codex_v1464_cooldown_seconds", 15 * 60)
        ),
        probation_min_evaluable=int(
            _setting(
                settings,
                "mainnet_codex_v1464_probation_min_evaluable",
                4,
            )
        ),
        probation_min_tp_first=int(
            _setting(
                settings,
                "mainnet_codex_v1464_probation_min_tp_first",
                3,
            )
        ),
        live_min_evaluable=int(
            _setting(settings, "mainnet_codex_v1464_live_min_evaluable", 6)
        ),
        live_min_tp_first=int(
            _setting(settings, "mainnet_codex_v1464_live_min_tp_first", 4)
        ),
        live_min_paid_complete=int(
            _setting(
                settings,
                "mainnet_codex_v1464_live_min_paid_complete",
                3,
            )
        ),
        live_min_paid_wins=int(
            _setting(settings, "mainnet_codex_v1464_live_min_paid_wins", 2)
        ),
        retain_min_evaluable=int(
            _setting(settings, "mainnet_codex_v1464_retain_min_evaluable", 4)
        ),
        retain_min_tp_first=int(
            _setting(settings, "mainnet_codex_v1464_retain_min_tp_first", 3)
        ),
        soft_breach_limit=int(
            _setting(settings, "mainnet_codex_v1464_soft_breach_limit", 2)
        ),
        probation_notional_cap_usdc=float(
            _setting(
                settings,
                "mainnet_codex_v1464_probation_notional_usdc",
                25.0,
            )
        ),
        live_notional_cap_usdc=float(
            _setting(
                settings,
                "mainnet_codex_v1464_live_notional_usdc",
                50.0,
            )
        ),
        consecutive_paid_loss_limit=int(
            _setting(
                settings,
                "mainnet_codex_v1464_consecutive_paid_loss_limit",
                2,
            )
        ),
        lane_net_loss_cap_usdc=float(
            _setting(
                settings,
                "mainnet_codex_v1464_lane_net_loss_cap_usdc",
                0.12,
            )
        ),
        cohort_net_loss_cap_usdc=float(
            _setting(
                settings,
                "mainnet_codex_v1464_cohort_net_loss_cap_usdc",
                0.30,
            )
        ),
    )


def promotion_cohort_from_identity(
    identity: Mapping[str, Any],
    *,
    config: AdaptivePromotionConfig,
) -> PromotionCohort:
    """Create the repository's full exact cohort with the active policy hash."""

    return PromotionCohort(
        environment=_text(identity.get("environment"), upper=True),
        symbol=_text(identity.get("symbol"), upper=True),
        lane_code=_text(
            _first(identity, "lane_code", "effective_lane", "classifier_lane"),
            upper=True,
        ),
        market_state=_text(identity.get("market_state")),
        effective_side=_text(identity.get("effective_side"), upper=True),
        strategy=_text(identity.get("strategy")),
        resolved_profile_hash=_text(identity.get("resolved_profile_hash")),
        profile_identity_schema=_text(
            identity.get("profile_identity_schema")
            or config.profile_schema
        ),
        registry_version=_text(identity.get("registry_version")),
        registry_hash=_text(identity.get("registry_hash")),
        lane_definition_hash=_text(identity.get("lane_definition_hash")),
        admission_policy_hash=_text(
            _first(identity, "admission_policy_hash", "v1462_policy_hash")
        ),
        promotion_policy_hash=config.policy_hash,
    )


def _cohort_identity(cohort: PromotionCohort) -> dict[str, str]:
    payload = asdict(cohort)
    return {name: _text(payload[name]) for name in _EVIDENCE_IDENTITY_FIELDS}


def _identity_from_details(details: Mapping[str, Any]) -> dict[str, str]:
    return {
        "environment": _text(details.get("environment"), upper=True),
        "symbol": _text(details.get("symbol"), upper=True),
        "lane_code": _text(
            _first(details, "lane_code", "effective_lane", "classifier_lane"),
            upper=True,
        ),
        "market_state": _text(details.get("market_state")),
        "effective_side": _text(details.get("effective_side"), upper=True),
        "strategy": _text(details.get("strategy")),
        "resolved_profile_hash": _text(details.get("resolved_profile_hash")),
        "profile_identity_schema": _text(
            details.get("profile_identity_schema")
        ),
        "registry_version": _text(details.get("registry_version")),
        "registry_hash": _text(details.get("registry_hash")),
        "lane_definition_hash": _text(details.get("lane_definition_hash")),
        "admission_policy_hash": _text(
            _first(details, "admission_policy_hash", "v1462_policy_hash")
        ),
    }


def validate_exact_registry_identity(payload: Mapping[str, Any]) -> str | None:
    """Return a fail-closed reason unless identity is in the exact registry."""

    identity = _identity_from_details(payload)
    if any(not value for value in identity.values()):
        return "identity_incomplete"
    if identity["registry_hash"] != REGISTRY_HASH:
        return "registry_hash_mismatch"
    if identity["registry_version"] != REGISTRY_VERSION:
        return "registry_version_mismatch"
    if identity["profile_identity_schema"] != V1464_PROFILE_IDENTITY_SCHEMA:
        return "profile_identity_schema_mismatch"
    try:
        lane = lane_for(identity["lane_code"])
    except KeyError:
        return "out_of_registry"
    if identity["effective_side"] not in lane.effective_sides:
        return "effective_side_out_of_registry"
    if identity["strategy"] not in lane.strategies:
        return "strategy_out_of_registry"
    classifier_side = _text(payload.get("classifier_side"), upper=True)
    if classifier_side and classifier_side != lane.classifier_side:
        return "classifier_side_mismatch"
    if identity["lane_definition_hash"] != lane_definition_hash(lane):
        return "lane_definition_hash_mismatch"
    if lane.state_profiles and state_profile_for(
        lane.lane_code, identity["market_state"]
    ) is None:
        return "market_state_out_of_registry"
    return None


def _known_exact_registry_reason(details: Mapping[str, Any]) -> str | None:
    registry_marker = _text(
        _first(details, "registry_status", "cohort_status", "lane_resolution"),
        upper=True,
    )
    if registry_marker == "OUT_OF_REGISTRY":
        return "out_of_registry"
    return validate_exact_registry_identity(details)


def _normalize_outcome(value: Any) -> str | None:
    outcome = _text(value).lower()
    aliases = {
        "take_profit": "tp1_first",
        "tp_success": "tp1_first",
        "stop_loss": "sl_first",
        "sl_failure": "sl_first",
    }
    outcome = aliases.get(outcome, outcome)
    return outcome if outcome in _SUPPORTED_OUTCOMES else None


@dataclass(frozen=True, slots=True)
class EvidenceProjection:
    persist: bool
    authoritative: bool
    reason: str
    payload: Mapping[str, Any] | None = None


def _shadow_projection(
    details: Mapping[str, Any],
    *,
    now_ms: int,
    config: AdaptivePromotionConfig,
    forced_drop: bool,
) -> EvidenceProjection:
    if not isinstance(details, Mapping):
        raise TypeError("details must be a mapping")
    now = _integer(now_ms, "now_ms")
    drop_reason = _text(
        _first(details, "drop_reason", "terminal_reason", "reason")
    ).lower()
    if forced_drop and drop_reason in _BENIGN_DROP_REASONS:
        return EvidenceProjection(False, False, "benign_pending_dedupe")
    registry_reason = _known_exact_registry_reason(details)
    if registry_reason is not None:
        return EvidenceProjection(False, False, registry_reason)

    outcome = (
        "no_fill"
        if forced_drop
        else _normalize_outcome(
            _first(details, "shadow_outcome", "outcome", "promotion_counts_as")
        )
    )
    if outcome is None:
        return EvidenceProjection(False, False, "unsupported_outcome")
    formal_v1462 = bool(
        details.get("v1462_opportunity_id")
        and (
            details.get("evidence_evaluator_eligible") is True
            or forced_drop
        )
    )
    opportunity_id = _text(
        _first(details, "v1462_opportunity_id", "opportunity_id")
        if formal_v1462
        else _first(details, "opportunity_id", "v1462_opportunity_id")
    )
    if not opportunity_id:
        return EvidenceProjection(False, False, "opportunity_id_missing")
    observed_raw = _first(details, "observed_at_ms", "start_ms", "first_seen_at_ms")
    terminal_raw = (
        now
        if forced_drop
        else _first(
            details,
            "terminal_at_ms",
            "resolved_at_ms",
            "resolved_ts",
            "hit_time_ms",
        )
    )
    try:
        observed_at_ms = _integer(observed_raw, "observed_at_ms")
        terminal_at_ms = _integer(terminal_raw, "terminal_at_ms")
    except ValueError:
        return EvidenceProjection(False, False, "evidence_timestamp_invalid")
    if terminal_at_ms < observed_at_ms or terminal_at_ms > now:
        return EvidenceProjection(False, False, "evidence_timestamp_invalid")

    counts_as = _text(details.get("promotion_counts_as")).lower()
    diagnostic = bool(
        forced_drop is False
        and (
            bool(details.get("diagnostic_only"))
            or bool(details.get("excluded_from_promotion"))
            or (
                not formal_v1462
                and (
                    details.get("promotion_eligible") is False
                    or counts_as in _EXCLUDED_COUNTS_AS
                )
            )
        )
    )
    ambiguous = bool(
        outcome == "ambiguous_both" or details.get("ambiguity_flag")
    )
    raw_pnl = _first(
        details,
        "net_pnl_usdc",
        "paper_pnl_usdc_after_fee",
        "fee_net_pnl_usdc",
    )
    try:
        net_pnl = _finite(raw_pnl, "net_pnl_usdc", allow_none=True)
    except ValueError:
        net_pnl = None
    if outcome == "no_fill" and raw_pnl is None and not forced_drop:
        net_pnl = 0.0
    complete = bool(
        not forced_drop
        and details.get("data_complete") is True
        and _text(details.get("evidence_source")) == "binance_aggTrade"
        and _text(details.get("fill_model")).lower() == "limit_touch"
        and not diagnostic
        and not ambiguous
        and net_pnl is not None
    )
    source_type = "SHADOW_DROP" if forced_drop else "SHADOW"
    source_id = _text(
        _first(details, "source_id", "sample_id", "strict_sample_id")
    )
    if formal_v1462:
        source_id = opportunity_id
    if not source_id:
        source_id = opportunity_id
    identity = _identity_from_details(details)
    payload = {
        "opportunity_id": opportunity_id,
        **identity,
        "evidence_schema_version": V1464_EVIDENCE_SCHEMA_VERSION,
        "observed_at_ms": observed_at_ms,
        "terminal_at_ms": terminal_at_ms,
        "outcome": outcome,
        "data_complete": complete,
        "ambiguous": ambiguous,
        "diagnostic_only": diagnostic,
        "net_pnl_usdc": net_pnl,
        "source_type": source_type,
        "source_id": source_id,
        "source_payload": {
            "promotion_counts_as": counts_as or None,
            "terminal_reason": drop_reason or None,
            "dropped": bool(forced_drop),
            "overdue": bool(details.get("overdue")),
            "evidence_source": _text(details.get("evidence_source")) or None,
        },
        "created_at_ms": now,
    }
    return EvidenceProjection(
        persist=True,
        authoritative=complete and not diagnostic,
        reason=(
            "shadow_drop_incomplete"
            if forced_drop
            else "authoritative"
            if complete and not diagnostic
            else "non_authoritative"
        ),
        payload=payload,
    )


@dataclass(frozen=True, slots=True)
class AggregatedPromotionEvidence:
    snapshot: PromotionEvidenceSnapshot
    revision_payload: Mapping[str, Any]
    evidence_watermark: int
    consecutive_paid_losses: int


@dataclass(frozen=True, slots=True)
class PaidRiskSummary:
    net_pnl_usdc: float
    consecutive_losses: int
    complete: int


def summarize_paid_risk(
    rows: Sequence[Mapping[str, Any]],
) -> PaidRiskSummary:
    """Summarize repository-filtered paid rows in terminal-time order."""

    ordered = sorted(
        (dict(row) for row in rows),
        key=lambda row: (
            int(row.get("terminal_at_ms") or 0),
            int(row.get("observed_at_ms") or 0),
            _text(row.get("opportunity_id")),
        ),
    )
    net = 0.0
    streak = 0
    for row in ordered:
        if _text(row.get("source_type"), upper=True) != "PAID":
            raise ValueError("lane paid evidence contains a non-PAID source")
        if _text(row.get("evidence_schema_version")) != V1464_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("lane paid evidence schema mismatch")
        if (
            not bool(row.get("data_complete"))
            or bool(row.get("ambiguous"))
            or bool(row.get("diagnostic_only"))
        ):
            raise ValueError("lane paid evidence is not authoritative")
        pnl = _finite(row.get("net_pnl_usdc"), "net_pnl_usdc")
        assert pnl is not None
        net += pnl
        if pnl < 0.0:
            streak += 1
        else:
            streak = 0
    return PaidRiskSummary(net, streak, len(ordered))


def aggregate_promotion_evidence(
    rows: Sequence[Mapping[str, Any]],
    *,
    cohort: PromotionCohort,
    now_ms: int,
    config: AdaptivePromotionConfig,
    activation_cutoff_ms: int = 0,
) -> AggregatedPromotionEvidence:
    """Aggregate exact-cohort rows without giving diagnostic rows authority."""

    now = _integer(now_ms, "now_ms")
    cutoff = max(
        0,
        now - config.evidence_window_seconds * 1000,
        _integer(activation_cutoff_ms, "activation_cutoff_ms"),
    )
    ordered = sorted(
        (dict(row) for row in rows),
        key=lambda row: (
            int(row.get("observed_at_ms") or 0),
            int(row.get("terminal_at_ms") or 0),
            str(row.get("opportunity_id") or ""),
        ),
    )
    opportunities = 0
    evaluable = 0
    tp_first = 0
    sl_first = 0
    max_hold = 0
    no_fill = 0
    fee_net = 0.0
    paid_complete = 0
    paid_wins = 0
    paid_net = 0.0
    consecutive_paid_losses = 0
    identity_conflicts = 0
    data_conflicts = 0
    incomplete = 0
    ambiguous_count = 0
    dropped = 0
    overdue = 0
    last_outcome: str | None = None
    last_outcome_at_ms: int | None = None
    authoritative_hashes: list[str] = []
    watermark = 0

    expected_identity = _cohort_identity(cohort)
    for row in ordered:
        if any(_text(row.get(name)) != expected_identity[name] for name in _EVIDENCE_IDENTITY_FIELDS):
            identity_conflicts += 1
            continue
        terminal_at = int(row.get("terminal_at_ms") or 0)
        watermark = max(watermark, terminal_at)
        source_payload = row.get("source_payload")
        if not isinstance(source_payload, Mapping):
            source_payload = {}
        evidence_hash = _text(row.get("evidence_hash")) or _hash_json(
            {
                name: row.get(name)
                for name in (
                    "opportunity_id",
                    *_EVIDENCE_IDENTITY_FIELDS,
                    "observed_at_ms",
                    "terminal_at_ms",
                    "outcome",
                    "data_complete",
                    "ambiguous",
                    "diagnostic_only",
                    "net_pnl_usdc",
                    "source_type",
                    "source_id",
                )
            }
        )
        if bool(row.get("diagnostic_only")):
            continue

        source_type = _text(row.get("source_type"), upper=True)
        if source_type not in {"SHADOW", "SHADOW_DROP", "PAID"}:
            data_conflicts += 1
            continue
        if _text(row.get("evidence_schema_version")) != config.evidence_contract_version:
            data_conflicts += 1
            continue
        outcome = _normalize_outcome(row.get("outcome"))
        is_ambiguous = bool(row.get("ambiguous")) or outcome == "ambiguous_both"
        pnl: float | None
        try:
            pnl = _finite(row.get("net_pnl_usdc"), "net_pnl_usdc", allow_none=True)
        except ValueError:
            pnl = None
        complete = bool(
            row.get("data_complete")
            and not is_ambiguous
            and outcome is not None
            and pnl is not None
        )
        row_dropped = bool(source_payload.get("dropped")) or _text(
            row.get("source_type"), upper=True
        ) == "SHADOW_DROP"
        row_overdue = bool(source_payload.get("overdue"))
        if row_dropped:
            dropped += 1
        if row_overdue:
            overdue += 1
        if is_ambiguous:
            ambiguous_count += 1
        if not complete:
            incomplete += 1
            if source_type in {"SHADOW", "SHADOW_DROP"}:
                opportunities += 1
            continue

        authoritative_hashes.append(evidence_hash)
        assert outcome is not None and pnl is not None
        if source_type == "PAID":
            if outcome == "no_fill":
                data_conflicts += 1
                continue
            paid_complete += 1
            paid_net += pnl
            if outcome in _TP_OUTCOMES:
                paid_wins += 1
            if pnl < 0.0:
                consecutive_paid_losses += 1
            else:
                consecutive_paid_losses = 0
            continue
        opportunities += 1
        if outcome in _TP_OUTCOMES:
            evaluable += 1
            tp_first += 1
        elif outcome in _SL_OUTCOMES:
            evaluable += 1
            sl_first += 1
        elif outcome == "max_hold":
            evaluable += 1
            max_hold += 1
        elif outcome == "no_fill":
            no_fill += 1
        else:
            data_conflicts += 1
            continue
        fee_net += pnl
        if last_outcome_at_ms is None or terminal_at >= last_outcome_at_ms:
            last_outcome_at_ms = terminal_at
            last_outcome = outcome

    revision_payload = {
        "schema": config.evidence_contract_version,
        "cohort_key": cohort.key,
        "authoritative_evidence_hashes": authoritative_hashes,
    }
    revision = _hash_json(revision_payload)
    snapshot = PromotionEvidenceSnapshot(
        evidence_revision=revision,
        snapshot_at_ms=now,
        window_started_at_ms=cutoff,
        window_ended_at_ms=now,
        last_outcome_at_ms=last_outcome_at_ms,
        last_outcome=last_outcome,
        opportunities=opportunities,
        evaluable=evaluable,
        tp_first=tp_first,
        sl_first=sl_first,
        max_hold=max_hold,
        no_fill=no_fill,
        fee_net_pnl_usdc=fee_net,
        paid_complete=paid_complete,
        paid_wins=paid_wins,
        paid_net_pnl_usdc=paid_net,
        data_complete=not (
            identity_conflicts
            or data_conflicts
            or incomplete
            or ambiguous_count
            or dropped
            or overdue
        ),
        identity_conflicts=identity_conflicts,
        data_conflicts=data_conflicts,
        incomplete=incomplete,
        ambiguous=ambiguous_count,
        dropped=dropped,
        overdue=overdue,
    )
    return AggregatedPromotionEvidence(
        snapshot,
        revision_payload,
        watermark,
        consecutive_paid_losses,
    )


@dataclass(frozen=True, slots=True)
class PromotionRegimeSnapshot:
    environment: str
    symbol: str
    lane_code: str
    market_state: str
    effective_side: str
    strategy: str
    resolved_profile_hash: str
    profile_identity_schema: str
    registry_version: str
    registry_hash: str
    lane_definition_hash: str
    admission_policy_hash: str
    observed_at_ms: int
    supportive: bool
    confirmations: int
    confirmation_observed_at_ms: tuple[int, ...] = ()
    confirmation_cohort_keys: tuple[str, ...] = ()


def derive_regime_input(
    regime: PromotionRegimeSnapshot,
    *,
    cohort: PromotionCohort,
    now_ms: int,
    minimum_confirmations: int,
    max_age_seconds: int,
    confirmation_window_seconds: int,
) -> PromotionRegimeInput:
    now = _integer(now_ms, "now_ms")
    confirmations = _integer(
        regime.confirmations, "regime.confirmations"
    )
    observed = _integer(regime.observed_at_ms, "regime.observed_at_ms")
    exact = all(
        _text(getattr(regime, name)) == _text(getattr(cohort, name))
        for name in _EVIDENCE_IDENTITY_FIELDS
    )
    fresh = bool(
        observed <= now and now - observed <= int(max_age_seconds) * 1000
    )
    confirmation_times = tuple(
        _integer(value, "regime.confirmation_observed_at_ms")
        for value in regime.confirmation_observed_at_ms
    )
    confirmation_keys = tuple(
        _text(value) for value in regime.confirmation_cohort_keys
    )
    confirmation_chain_valid = bool(
        confirmations == len(confirmation_times)
        and len(confirmation_times) == len(confirmation_keys)
        and confirmation_times == tuple(sorted(set(confirmation_times)))
        and (not confirmation_times or confirmation_times[-1] == observed)
        and all(
            timestamp <= now
            and now - timestamp <= int(max_age_seconds) * 1000
            for timestamp in confirmation_times
        )
        and all(key == cohort.key for key in confirmation_keys)
        and (
            not confirmation_times
            or confirmation_times[-1] - confirmation_times[0]
            <= int(confirmation_window_seconds) * 1000
        )
        and all(
            later - earlier <= int(confirmation_window_seconds) * 1000
            for earlier, later in zip(
                confirmation_times,
                confirmation_times[1:],
            )
        )
    )
    return PromotionRegimeInput(
        supportive=bool(regime.supportive),
        confirmed=bool(
            confirmation_chain_valid
            and confirmations >= int(minimum_confirmations)
        ),
        fresh=fresh,
        exact_cohort_match=exact,
    )


@dataclass(frozen=True, slots=True)
class PromotionAdmissionMetadata:
    adaptive_authorized: bool
    incumbent_control_unchanged: bool
    state: str
    reason: str
    evaluation_id: str
    evaluated_at_ms: int
    environment: str
    symbol: str
    lane_code: str
    market_state: str
    effective_side: str
    strategy: str
    resolved_profile_hash: str
    profile_identity_schema: str
    registry_version: str
    registry_hash: str
    lane_definition_hash: str
    admission_policy_hash: str
    promotion_policy_hash: str
    cohort_key: str
    lease_id: str | None
    generation: int | None
    lease_status: str | None
    lease_phase: str | None
    evidence_revision: str
    evidence_snapshot_hash: str | None
    expires_at_ms: int | None
    notional_cap_usdc: float
    applied_notional_usdc: float

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any]
    ) -> "PromotionAdmissionMetadata":
        return cls(**{name: payload.get(name) for name in cls.__dataclass_fields__})


@dataclass(frozen=True, slots=True)
class PromotionRuntimeResult:
    decision: AdaptivePromotionDecision
    metadata: PromotionAdmissionMetadata
    persistence_healthy: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PromotionRevalidation:
    allowed: bool
    reason: str
    notional_cap_usdc: float = 0.0
    claim_generation: int | None = None
    metadata: PromotionAdmissionMetadata | None = None


def _risk_block_reason(
    risk: PromotionRiskInput,
    config: AdaptivePromotionConfig,
) -> str | None:
    for accepted, reason in (
        (risk.raw_accepted, "raw_rejected"),
        (risk.pre_gate_accepted, "pre_gate_rejected"),
        (risk.final_incumbent_accepted, "final_incumbent_rejected"),
        (risk.identity_valid, "identity_invalid"),
        (risk.integrity_safe, "integrity_unsafe"),
        (risk.execution_controls_safe, "execution_controls_unsafe"),
        (risk.database_healthy, "database_unhealthy"),
        (not risk.global_halted, "global_halt"),
    ):
        if not accepted:
            return reason
    if risk.reject_lineage:
        return "reject_lineage_present"
    if risk.consecutive_paid_losses >= config.consecutive_paid_loss_limit:
        return "consecutive_paid_loss_limit"
    if risk.lane_net_pnl_usdc <= -config.lane_net_loss_cap_usdc:
        return "lane_loss_cap"
    if risk.cohort_net_pnl_usdc <= -config.cohort_net_loss_cap_usdc:
        return "cohort_loss_cap"
    return None


def _empty_evidence(
    *,
    cohort: PromotionCohort,
    now_ms: int,
    config: AdaptivePromotionConfig,
) -> PromotionEvidenceSnapshot:
    return aggregate_promotion_evidence(
        (),
        cohort=cohort,
        now_ms=now_ms,
        config=config,
    ).snapshot


def _metadata(
    *,
    cohort: PromotionCohort,
    decision: AdaptivePromotionDecision,
    evaluation_id: str,
    now_ms: int,
    lease_row: Mapping[str, Any] | None,
    adaptive_authorized: bool,
    reason: str | None = None,
) -> PromotionAdmissionMetadata:
    identity = _cohort_identity(cohort)
    return PromotionAdmissionMetadata(
        adaptive_authorized=adaptive_authorized,
        incumbent_control_unchanged=True,
        state=decision.state.value,
        reason=reason or decision.reason,
        evaluation_id=evaluation_id,
        evaluated_at_ms=now_ms,
        **identity,
        promotion_policy_hash=cohort.promotion_policy_hash,
        cohort_key=cohort.key,
        lease_id=(
            _text(lease_row.get("lease_id")) or None if lease_row else None
        ),
        generation=(
            int(lease_row["generation"])
            if lease_row and lease_row.get("generation") is not None
            else None
        ),
        lease_status=(
            _text(lease_row.get("status"), upper=True) or None
            if lease_row
            else None
        ),
        lease_phase=(
            _text(lease_row.get("phase"), upper=True) or None
            if lease_row
            else None
        ),
        evidence_revision=decision.evidence_revision,
        evidence_snapshot_hash=(
            _text(lease_row.get("evidence_snapshot_hash")) or None
            if lease_row
            else None
        ),
        expires_at_ms=(
            int(lease_row["expires_at_ms"])
            if lease_row and lease_row.get("expires_at_ms") is not None
            else None
        ),
        notional_cap_usdc=(
            float(lease_row["notional_cap_usdc"])
            if adaptive_authorized and lease_row
            else 0.0
        ),
        applied_notional_usdc=(
            float(decision.applied_notional_usdc)
            if adaptive_authorized
            else 0.0
        ),
    )


def _lease_id(cohort_key: str, evaluation_id: str) -> str:
    encoded = f"{cohort_key}|{evaluation_id}".encode("utf-8")
    return "v1464_lease_" + hashlib.sha256(encoded).hexdigest()


def _idempotency_key(
    cohort_key: str,
    evaluation_id: str,
    action: str,
) -> str:
    encoded = f"{cohort_key}|{evaluation_id}|{action}".encode("utf-8")
    return "v1464_event_" + hashlib.sha256(encoded).hexdigest()


def _phase(state: PromotionState) -> str:
    if state is PromotionState.PROBATION:
        return "PROBATION"
    if state is PromotionState.LIVE:
        return "CONTROL"
    raise ValueError(f"{state.value} has no active storage phase")


def _paid_outcome(net_pnl_usdc: float) -> str:
    """Classify paid authority from reconciled fee-net PnL, never reason text."""

    if net_pnl_usdc > 0.0:
        return "tp1_first"
    if net_pnl_usdc < 0.0:
        return "sl_first"
    return "max_hold"


def project_paid_terminal_evidence(
    metadata: PromotionAdmissionMetadata | Mapping[str, Any],
    *,
    run_id: str,
    terminal_at_ms: int,
    net_pnl_usdc: float,
    reason: str,
    now_ms: int,
    evidence_schema_version: str = V1464_EVIDENCE_SCHEMA_VERSION,
) -> Mapping[str, Any]:
    """Project one paid terminal result onto the admitted immutable identity."""

    meta = (
        metadata
        if isinstance(metadata, PromotionAdmissionMetadata)
        else PromotionAdmissionMetadata.from_payload(metadata)
    )
    run = _text(run_id)
    if not run:
        raise ValueError("run_id must be non-empty")
    if not meta.adaptive_authorized or not meta.lease_id:
        raise ValueError("paid evidence requires authorized admission metadata")
    terminal = _integer(terminal_at_ms, "terminal_at_ms")
    now = _integer(now_ms, "now_ms")
    if terminal < meta.evaluated_at_ms or terminal > now:
        raise ValueError("paid terminal time is outside the admitted lifecycle")
    pnl = _finite(net_pnl_usdc, "net_pnl_usdc")
    assert pnl is not None
    return {
        "opportunity_id": f"paid:{run}",
        **{
            name: getattr(meta, name)
            for name in _EVIDENCE_IDENTITY_FIELDS
        },
        "evidence_schema_version": _text(evidence_schema_version),
        "observed_at_ms": meta.evaluated_at_ms,
        "terminal_at_ms": terminal,
        "outcome": _paid_outcome(pnl),
        "data_complete": True,
        "ambiguous": False,
        "diagnostic_only": False,
        "net_pnl_usdc": pnl,
        "source_type": "PAID",
        "source_id": run,
        "source_payload": {
            "run_id": run,
            "terminal_reason": _text(reason),
            "lease_id": meta.lease_id,
            "generation": meta.generation,
            "promotion_policy_hash": meta.promotion_policy_hash,
        },
        "created_at_ms": now,
    }


class V1464PromotionRuntime:
    """Small async facade intended for one-run admission and terminal hooks."""

    def __init__(
        self,
        repository: V1464PromotionRepository,
        *,
        settings: Any | None = None,
        config: AdaptivePromotionConfig | None = None,
        boot_id: str,
        owner_id: str,
        actor: str = "v1464_promotion_runtime",
        enabled: bool | None = None,
        activation_cutoff_ms: int | None = None,
        regime_confirmations: int | None = None,
        max_terminal_latency_seconds: int | None = None,
        regime_max_age_seconds: int | None = None,
        regime_confirmation_window_seconds: int | None = None,
    ) -> None:
        if config is None and settings is None:
            config = AdaptivePromotionConfig()
        self.repository = repository
        self.config = config or adaptive_promotion_config_from_settings(settings)
        if self.config.profile_schema != V1464_PROFILE_IDENTITY_SCHEMA:
            raise ValueError("unsupported profile identity schema")
        if self.config.evidence_contract_version != V1464_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("unsupported evidence schema")
        self.boot_id = _text(boot_id)
        self.owner_id = _text(owner_id)
        self.actor = _text(actor)
        if not self.boot_id or not self.owner_id or not self.actor:
            raise ValueError("boot_id, owner_id, and actor must be non-empty")
        self.enabled = bool(
            enabled
            if enabled is not None
            else _setting(
                settings,
                "mainnet_codex_v1464_auto_promotion_enabled",
                True,
            )
        )
        self.activation_cutoff_ms = _integer(
            activation_cutoff_ms
            if activation_cutoff_ms is not None
            else _setting(
                settings,
                "mainnet_codex_v1464_activation_cutoff_ms",
                0,
            ),
            "activation_cutoff_ms",
        )
        self.regime_confirmations = _integer(
            regime_confirmations
            if regime_confirmations is not None
            else _setting(
                settings,
                "mainnet_codex_v1464_regime_confirmations",
                2,
            ),
            "regime_confirmations",
            minimum=1,
        )
        self.max_terminal_latency_ms = (
            _integer(
                max_terminal_latency_seconds
                if max_terminal_latency_seconds is not None
                else _setting(
                    settings,
                    "mainnet_codex_v1464_max_terminal_latency_seconds",
                    360,
                ),
                "max_terminal_latency_seconds",
                minimum=1,
            )
            * 1000
        )
        self.regime_max_age_seconds = _integer(
            regime_max_age_seconds
            if regime_max_age_seconds is not None
            else _setting(
                settings,
                "mainnet_codex_v1464_regime_max_age_seconds",
                60,
            ),
            "regime_max_age_seconds",
            minimum=1,
        )
        self.regime_confirmation_window_seconds = _integer(
            regime_confirmation_window_seconds
            if regime_confirmation_window_seconds is not None
            else _setting(
                settings,
                "mainnet_codex_v1464_regime_confirmation_window_seconds",
                45,
            ),
            "regime_confirmation_window_seconds",
            minimum=1,
        )
        self._database_healthy = True

    @property
    def database_healthy(self) -> bool:
        return self._database_healthy

    def mark_database_healthy(self) -> None:
        """Clear the latch only after an external database health check passes."""

        self._database_healthy = True

    async def project_shadow_outcome(
        self,
        details: Mapping[str, Any],
        now_ms: int,
    ) -> EvidenceProjection:
        projection = _shadow_projection(
            details,
            now_ms=now_ms,
            config=self.config,
            forced_drop=False,
        )
        if projection.persist and projection.payload is not None:
            try:
                await self.repository.upsert_evidence(projection.payload)
            except Exception:
                self._database_healthy = False
                raise
        return projection

    async def project_shadow_drop(
        self,
        details: Mapping[str, Any],
        now_ms: int,
    ) -> EvidenceProjection:
        """Persist a true known-cohort drop as visible incomplete evidence."""

        projection = _shadow_projection(
            details,
            now_ms=now_ms,
            config=self.config,
            forced_drop=True,
        )
        if projection.persist and projection.payload is not None:
            try:
                await self.repository.upsert_evidence(projection.payload)
            except Exception:
                self._database_healthy = False
                raise
        return projection

    async def record_paid_terminal(
        self,
        metadata: PromotionAdmissionMetadata | Mapping[str, Any],
        *,
        run_id: str,
        terminal_at_ms: int,
        net_pnl_usdc: float,
        reason: str,
        now_ms: int,
    ) -> bool:
        meta = (
            metadata
            if isinstance(metadata, PromotionAdmissionMetadata)
            else PromotionAdmissionMetadata.from_payload(metadata)
        )
        payload = project_paid_terminal_evidence(
            meta,
            run_id=run_id,
            terminal_at_ms=terminal_at_ms,
            net_pnl_usdc=net_pnl_usdc,
            reason=reason,
            now_ms=now_ms,
            evidence_schema_version=self.config.evidence_contract_version,
        )
        try:
            inserted = await self.repository.upsert_evidence(payload)
            await self._enforce_paid_risk_after_terminal(
                meta,
                now_ms=now_ms,
                source_id=_text(run_id),
            )
            return inserted
        except Exception:
            self._database_healthy = False
            raise

    async def _enforce_paid_risk_after_terminal(
        self,
        metadata: PromotionAdmissionMetadata,
        *,
        now_ms: int,
        source_id: str,
    ) -> None:
        cohort = PromotionCohort(
            **{
                name: getattr(metadata, name)
                for name in _EVIDENCE_IDENTITY_FIELDS
            },
            promotion_policy_hash=metadata.promotion_policy_hash,
        )
        now = _integer(now_ms, "now_ms")
        window_start = max(
            0,
            now - self.config.evidence_window_seconds * 1000,
        )
        exact_rows = await self.repository.list_sliding_evidence(
            cohort,
            window_start_ms=window_start,
            as_of_ms=now,
            activation_cutoff_ms=self.activation_cutoff_ms,
            max_terminal_latency_ms=self.max_terminal_latency_ms,
            eligible_only=False,
        )
        lane_rows = await self.repository.list_lane_paid_evidence(
            environment=cohort.environment,
            symbol=cohort.symbol,
            lane_code=cohort.lane_code,
            window_start_ms=window_start,
            as_of_ms=now,
            activation_cutoff_ms=self.activation_cutoff_ms,
        )
        aggregate = aggregate_promotion_evidence(
            exact_rows,
            cohort=cohort,
            now_ms=now,
            config=self.config,
            activation_cutoff_ms=self.activation_cutoff_ms,
        )
        lane_risk = summarize_paid_risk(lane_rows)
        quarantined = bool(
            lane_risk.consecutive_losses
            >= self.config.consecutive_paid_loss_limit
            or lane_risk.net_pnl_usdc <= -self.config.lane_net_loss_cap_usdc
            or aggregate.snapshot.paid_net_pnl_usdc
            <= -self.config.cohort_net_loss_cap_usdc
        )
        if not quarantined:
            return

        current = await self.repository.get_lease(cohort.key)
        generation = (
            int(current["generation"])
            if current and current.get("generation") is not None
            else None
        )
        cooldown_until = now + self.config.cooldown_seconds * 1000
        idempotency_key = _idempotency_key(
            cohort.key,
            f"paid:{source_id}",
            "COOLDOWN",
        )
        if current and _text(current.get("status"), upper=True) == "ACTIVE":
            await self.repository.cooldown_lease(
                cohort.key,
                expected_generation=int(generation),
                reason="paid_risk_quarantine",
                event_time_ms=now,
                cooldown_until_ms=cooldown_until,
                idempotency_key=idempotency_key,
                actor=self.actor,
            )
            return
        if current and _text(current.get("status"), upper=True) in {
            "COOLDOWN",
            "HALTED",
        }:
            return

        phase = (
            _text(current.get("phase"), upper=True)
            if current
            else "PROBATION"
        )
        if phase not in {"PROBATION", "CONTROL"}:
            phase = "PROBATION"
        cap = (
            float(current.get("notional_cap_usdc") or 0.0)
            if current
            else 0.0
        )
        if cap <= 0.0:
            cap = (
                self.config.live_notional_cap_usdc
                if phase == "CONTROL"
                else self.config.probation_notional_cap_usdc
            )
        issued_at = (
            int(current.get("issued_at_ms") or now) if current else now
        )
        guard_payload = {
            **asdict(cohort),
            "lease_id": (
                _text(current.get("lease_id"))
                if current
                else _lease_id(cohort.key, f"paid:{source_id}")
            ),
            "phase": phase,
            "status": "ACTIVE",
            "notional_cap_usdc": cap,
            "evidence_window_start_ms": aggregate.snapshot.window_started_at_ms,
            "evidence_as_of_ms": aggregate.snapshot.snapshot_at_ms,
            "evidence_watermark": aggregate.evidence_watermark,
            "evidence_snapshot": dict(aggregate.revision_payload),
            "issued_at_ms": issued_at,
            "renewed_at_ms": now,
            "expires_at_ms": now + self.config.lease_ttl_seconds * 1000,
            "boot_id": self.boot_id,
            "owner_id": self.owner_id,
            "soft_failures": int(current.get("soft_failures") or 0)
            if current
            else 0,
            "demotion_reason": None,
            "demoted_at_ms": None,
            "cooldown_until_ms": None,
        }
        await self.repository.upsert_guard_state(
            guard_payload,
            expected_generation=generation,
            status="COOLDOWN",
            reason="paid_risk_quarantine",
            event_time_ms=now,
            cooldown_until_ms=cooldown_until,
            idempotency_key=idempotency_key,
            actor=self.actor,
        )

    def _lease_payload(
        self,
        *,
        cohort: PromotionCohort,
        decision: AdaptivePromotionDecision,
        aggregate: AggregatedPromotionEvidence,
        now_ms: int,
        evaluation_id: str,
        current: Mapping[str, Any] | None,
        extend_lease: bool,
    ) -> dict[str, Any]:
        active_current = bool(
            current and _text(current.get("status"), upper=True) == "ACTIVE"
        )
        issued_at = (
            int(current["issued_at_ms"]) if active_current else now_ms
        )
        renewed_at = (
            now_ms
            if extend_lease
            else int(current["renewed_at_ms"])
            if active_current
            else now_ms
        )
        expires_at = (
            int(decision.lease_expires_at_ms)
            if decision.lease_expires_at_ms is not None
            else int(current["expires_at_ms"])
            if active_current
            else now_ms + self.config.lease_ttl_seconds * 1000
        )
        return {
            **asdict(cohort),
            "lease_id": (
                _text(current.get("lease_id"))
                if active_current
                else _lease_id(cohort.key, evaluation_id)
            ),
            "phase": _phase(decision.state),
            "status": "ACTIVE",
            "notional_cap_usdc": decision.max_notional_usdc,
            "evidence_window_start_ms": (
                aggregate.snapshot.window_started_at_ms
            ),
            "evidence_as_of_ms": aggregate.snapshot.snapshot_at_ms,
            "evidence_watermark": aggregate.evidence_watermark,
            "evidence_snapshot": dict(aggregate.revision_payload),
            "issued_at_ms": issued_at,
            "renewed_at_ms": renewed_at,
            "expires_at_ms": expires_at,
            "boot_id": self.boot_id,
            "owner_id": self.owner_id,
            "soft_failures": decision.soft_breach_count,
            "demotion_reason": None,
            "demoted_at_ms": None,
        }

    async def _persist_decision(
        self,
        *,
        cohort: PromotionCohort,
        decision: AdaptivePromotionDecision,
        aggregate: AggregatedPromotionEvidence,
        current: Mapping[str, Any] | None,
        now_ms: int,
        evaluation_id: str,
    ) -> Mapping[str, Any] | None:
        generation = (
            int(current["generation"])
            if current and current.get("generation") is not None
            else None
        )
        active = bool(
            current and _text(current.get("status"), upper=True) == "ACTIVE"
        )
        expired = bool(
            active and int(current.get("expires_at_ms") or 0) <= now_ms
        )
        if decision.state in {PromotionState.COOLDOWN, PromotionState.HALTED}:
            guard_status = decision.state.value
            if active:
                if decision.state is PromotionState.COOLDOWN:
                    cooldown_until = int(
                        decision.lease_expires_at_ms
                        or now_ms + self.config.cooldown_seconds * 1000
                    )
                    return await self.repository.cooldown_lease(
                        cohort.key,
                        expected_generation=int(generation),
                        reason=decision.reason,
                        event_time_ms=now_ms,
                        cooldown_until_ms=cooldown_until,
                        idempotency_key=_idempotency_key(
                            cohort.key, evaluation_id, "COOLDOWN"
                        ),
                        actor=self.actor,
                    )
                return await self.repository.halt_lease(
                    cohort.key,
                    expected_generation=int(generation),
                    reason=decision.reason,
                    event_time_ms=now_ms,
                    idempotency_key=_idempotency_key(
                        cohort.key, evaluation_id, "HALTED"
                    ),
                    actor=self.actor,
                )

            storage_state = (
                PromotionState.LIVE
                if current and _text(current.get("phase"), upper=True) == "CONTROL"
                else PromotionState.PROBATION
            )
            storage_decision = replace(
                decision,
                state=storage_state,
                max_notional_usdc=(
                    self.config.live_notional_cap_usdc
                    if storage_state is PromotionState.LIVE
                    else self.config.probation_notional_cap_usdc
                ),
                lease_expires_at_ms=now_ms + self.config.lease_ttl_seconds * 1000,
            )
            guard_payload = self._lease_payload(
                cohort=cohort,
                decision=storage_decision,
                aggregate=aggregate,
                now_ms=now_ms,
                evaluation_id=evaluation_id,
                current=current,
                extend_lease=True,
            )
            return await self.repository.upsert_guard_state(
                guard_payload,
                expected_generation=generation,
                status=guard_status,
                reason=decision.reason,
                event_time_ms=now_ms,
                cooldown_until_ms=(
                    int(
                        decision.lease_expires_at_ms
                        or now_ms + self.config.cooldown_seconds * 1000
                    )
                    if decision.state is PromotionState.COOLDOWN
                    else None
                ),
                idempotency_key=_idempotency_key(
                    cohort.key, evaluation_id, guard_status
                ),
                actor=self.actor,
            )

        if decision.issue_new_lease:
            prior_phase = (
                _text(current.get("phase"), upper=True) if active else ""
            )
            target_phase = _phase(decision.state)
            if target_phase == "CONTROL" and prior_phase != "CONTROL":
                event_type = "CONTROL_GRANTED"
            elif active:
                event_type = "LEASE_RENEWED"
            else:
                event_type = "PROBATION_GRANTED"
            payload = self._lease_payload(
                cohort=cohort,
                decision=decision,
                aggregate=aggregate,
                now_ms=now_ms,
                evaluation_id=evaluation_id,
                current=current,
                extend_lease=True,
            )
            return await self.repository.upsert_lease(
                payload,
                expected_generation=generation,
                event_type=event_type,
                event_time_ms=now_ms,
                idempotency_key=_idempotency_key(
                    cohort.key, evaluation_id, event_type
                ),
                actor=self.actor,
                event_payload={"reason": decision.reason},
            )

        if decision.revoke_existing_lease and active:
            if expired:
                return await self.repository.expire_lease(
                    cohort.key,
                    expected_generation=int(generation),
                    now_ms=now_ms,
                    idempotency_key=_idempotency_key(
                        cohort.key, evaluation_id, "EXPIRED"
                    ),
                    actor=self.actor,
                )
            return await self.repository.demote_lease(
                cohort.key,
                expected_generation=int(generation),
                reason=decision.reason,
                event_time_ms=now_ms,
                idempotency_key=_idempotency_key(
                    cohort.key, evaluation_id, "DEMOTED"
                ),
                actor=self.actor,
            )

        if (
            active
            and decision.permits_order
            and int(current.get("soft_failures") or 0)
            != decision.soft_breach_count
        ):
            payload = self._lease_payload(
                cohort=cohort,
                decision=decision,
                aggregate=aggregate,
                now_ms=now_ms,
                evaluation_id=evaluation_id,
                current=current,
                extend_lease=False,
            )
            return await self.repository.upsert_lease(
                payload,
                expected_generation=generation,
                event_type="EVALUATED",
                event_time_ms=now_ms,
                idempotency_key=_idempotency_key(
                    cohort.key, evaluation_id, "SOFT_EVALUATED"
                ),
                actor=self.actor,
                event_payload={"reason": decision.reason},
            )
        return current

    def _database_failure(
        self,
        *,
        cohort: PromotionCohort,
        candidate_notional_usdc: float,
        regime: PromotionRegimeInput,
        risk: PromotionRiskInput,
        evidence: PromotionEvidenceSnapshot,
        existing_lease: PromotionLeaseSnapshot | None,
        now_ms: int,
        evaluation_id: str,
        error: Exception,
    ) -> PromotionRuntimeResult:
        self._database_healthy = False
        blocked_risk = replace(risk, database_healthy=False)
        decision = select_adaptive_promotion_decision(
            profile_hash=cohort.resolved_profile_hash,
            cohort_key=cohort.key,
            candidate_notional_usdc=candidate_notional_usdc,
            evidence=evidence,
            regime=regime,
            risk=blocked_risk,
            now_ms=now_ms,
            existing_lease=existing_lease,
            config=self.config,
        )
        return PromotionRuntimeResult(
            decision=decision,
            metadata=_metadata(
                cohort=cohort,
                decision=decision,
                evaluation_id=evaluation_id,
                now_ms=now_ms,
                lease_row=None,
                adaptive_authorized=False,
                reason="adaptive_database_unhealthy",
            ),
            persistence_healthy=False,
            error=f"{type(error).__name__}:{str(error)[:200]}",
        )

    async def evaluate_candidate(
        self,
        *,
        cohort: PromotionCohort,
        candidate_notional_usdc: float,
        regime: PromotionRegimeSnapshot,
        risk: PromotionRiskInput,
        now_ms: int,
        evaluation_id: str,
    ) -> PromotionRuntimeResult:
        """Evaluate and atomically materialize adaptive admission authority."""

        now = _integer(now_ms, "now_ms")
        evaluation = _text(evaluation_id)
        if not evaluation:
            raise ValueError("evaluation_id must be non-empty")
        if cohort.promotion_policy_hash != self.config.policy_hash:
            raise ValueError("cohort promotion policy hash is not active")
        regime_input = derive_regime_input(
            regime,
            cohort=cohort,
            now_ms=now,
            minimum_confirmations=self.regime_confirmations,
            max_age_seconds=self.regime_max_age_seconds,
            confirmation_window_seconds=self.regime_confirmation_window_seconds,
        )
        empty = _empty_evidence(cohort=cohort, now_ms=now, config=self.config)
        if not self._database_healthy:
            return self._database_failure(
                cohort=cohort,
                candidate_notional_usdc=candidate_notional_usdc,
                regime=regime_input,
                risk=risk,
                evidence=empty,
                existing_lease=None,
                now_ms=now,
                evaluation_id=evaluation,
                error=RuntimeError("database_health_latched"),
            )
        if not self.enabled:
            decision = select_adaptive_promotion_decision(
                profile_hash=cohort.resolved_profile_hash,
                cohort_key=cohort.key,
                candidate_notional_usdc=candidate_notional_usdc,
                evidence=empty,
                regime=regime_input,
                risk=replace(risk, final_incumbent_accepted=False),
                now_ms=now,
                config=self.config,
            )
            return PromotionRuntimeResult(
                decision,
                _metadata(
                    cohort=cohort,
                    decision=decision,
                    evaluation_id=evaluation,
                    now_ms=now,
                    lease_row=None,
                    adaptive_authorized=False,
                    reason="adaptive_promotion_disabled",
                ),
                True,
            )

        existing_row: Mapping[str, Any] | None = None
        existing_snapshot: PromotionLeaseSnapshot | None = None
        try:
            existing_row = await self.repository.get_lease(cohort.key)
            if existing_row is not None:
                existing_snapshot = lease_row_to_engine_snapshot(existing_row)
            rows = await self.repository.list_sliding_evidence(
                cohort,
                window_start_ms=max(
                    0,
                    now - self.config.evidence_window_seconds * 1000,
                ),
                as_of_ms=now,
                activation_cutoff_ms=self.activation_cutoff_ms,
                max_terminal_latency_ms=self.max_terminal_latency_ms,
                eligible_only=False,
            )
            lane_paid_rows = await self.repository.list_lane_paid_evidence(
                environment=cohort.environment,
                symbol=cohort.symbol,
                lane_code=cohort.lane_code,
                window_start_ms=max(
                    0,
                    now - self.config.evidence_window_seconds * 1000,
                ),
                as_of_ms=now,
                activation_cutoff_ms=self.activation_cutoff_ms,
            )
            aggregate = aggregate_promotion_evidence(
                rows,
                cohort=cohort,
                now_ms=now,
                config=self.config,
                activation_cutoff_ms=self.activation_cutoff_ms,
            )
            lane_paid_risk = summarize_paid_risk(lane_paid_rows)
        except Exception as exc:
            return self._database_failure(
                cohort=cohort,
                candidate_notional_usdc=candidate_notional_usdc,
                regime=regime_input,
                risk=risk,
                evidence=empty,
                existing_lease=existing_snapshot,
                now_ms=now,
                evaluation_id=evaluation,
                error=exc,
            )

        identity_valid = validate_exact_registry_identity(asdict(cohort)) is None
        effective_risk = replace(
            risk,
            identity_valid=bool(risk.identity_valid and identity_valid),
            consecutive_paid_losses=lane_paid_risk.consecutive_losses,
            lane_net_pnl_usdc=lane_paid_risk.net_pnl_usdc,
            cohort_net_pnl_usdc=aggregate.snapshot.paid_net_pnl_usdc,
        )
        decision = select_adaptive_promotion_decision(
            profile_hash=cohort.resolved_profile_hash,
            cohort_key=cohort.key,
            candidate_notional_usdc=candidate_notional_usdc,
            evidence=aggregate.snapshot,
            regime=regime_input,
            risk=effective_risk,
            now_ms=now,
            existing_lease=existing_snapshot,
            config=self.config,
        )
        try:
            materialized = await self._persist_decision(
                cohort=cohort,
                decision=decision,
                aggregate=aggregate,
                current=existing_row,
                now_ms=now,
                evaluation_id=evaluation,
            )
        except Exception as exc:
            return self._database_failure(
                cohort=cohort,
                candidate_notional_usdc=candidate_notional_usdc,
                regime=regime_input,
                risk=effective_risk,
                evidence=aggregate.snapshot,
                existing_lease=existing_snapshot,
                now_ms=now,
                evaluation_id=evaluation,
                error=exc,
            )

        authorized = bool(
            decision.permits_order
            and materialized
            and _text(materialized.get("status"), upper=True) == "ACTIVE"
            and _text(materialized.get("cohort_key")) == cohort.key
            and _text(materialized.get("promotion_policy_hash"))
            == self.config.policy_hash
            and int(materialized.get("expires_at_ms") or 0) > now
            and float(materialized.get("notional_cap_usdc") or 0.0)
            >= decision.applied_notional_usdc
            and (
                not decision.issue_new_lease
                or _text(materialized.get("evidence_snapshot_hash"))
                == aggregate.snapshot.evidence_revision
            )
        )
        metadata = _metadata(
            cohort=cohort,
            decision=decision,
            evaluation_id=evaluation,
            now_ms=now,
            lease_row=materialized,
            adaptive_authorized=authorized,
            reason=decision.reason if authorized or not decision.permits_order else "lease_materialization_invalid",
        )
        return PromotionRuntimeResult(
            decision=decision,
            metadata=metadata,
            persistence_healthy=True,
        )

    async def revalidate_before_submit(
        self,
        metadata: PromotionAdmissionMetadata | Mapping[str, Any],
        now_ms: int,
        *,
        current_cohort: PromotionCohort | None = None,
        actual_notional_usdc: float | None = None,
        risk: PromotionRiskInput | None = None,
        regime: PromotionRegimeSnapshot | None = None,
        consume_id: str | None = None,
        enabled: bool | None = None,
        database_healthy: bool = True,
    ) -> PromotionRevalidation:
        """Revalidate every mutable gate and atomically consume one generation."""

        meta = (
            metadata
            if isinstance(metadata, PromotionAdmissionMetadata)
            else PromotionAdmissionMetadata.from_payload(metadata)
        )
        now = _integer(now_ms, "now_ms")
        if not meta.adaptive_authorized:
            return PromotionRevalidation(False, "metadata_not_authorized")
        if not bool(self.enabled if enabled is None else enabled):
            return PromotionRevalidation(False, "adaptive_promotion_disabled")
        if not database_healthy or not self._database_healthy:
            return PromotionRevalidation(False, "database_unhealthy")
        if (
            current_cohort is None
            or actual_notional_usdc is None
            or risk is None
            or regime is None
            or not _text(consume_id)
        ):
            return PromotionRevalidation(False, "revalidation_context_missing")
        if current_cohort.promotion_policy_hash != self.config.policy_hash:
            return PromotionRevalidation(False, "promotion_policy_hash_changed")
        if validate_exact_registry_identity(asdict(current_cohort)) is not None:
            return PromotionRevalidation(False, "current_identity_invalid")
        if current_cohort.key != meta.cohort_key or any(
            _text(getattr(current_cohort, name)) != _text(getattr(meta, name))
            for name in (*_EVIDENCE_IDENTITY_FIELDS, "promotion_policy_hash")
        ):
            return PromotionRevalidation(False, "cohort_identity_changed")
        try:
            actual_notional = _finite(
                actual_notional_usdc,
                "actual_notional_usdc",
            )
        except ValueError:
            return PromotionRevalidation(False, "actual_notional_invalid")
        assert actual_notional is not None
        if (
            actual_notional <= 0.0
            or actual_notional > meta.applied_notional_usdc
            or actual_notional > meta.notional_cap_usdc
        ):
            return PromotionRevalidation(False, "actual_notional_exceeds_admission")
        try:
            regime_input = derive_regime_input(
                regime,
                cohort=current_cohort,
                now_ms=now,
                minimum_confirmations=self.regime_confirmations,
                max_age_seconds=self.regime_max_age_seconds,
                confirmation_window_seconds=self.regime_confirmation_window_seconds,
            )
        except (TypeError, ValueError):
            return PromotionRevalidation(False, "regime_invalid")
        for accepted, reason in (
            (regime_input.supportive, "regime_not_supportive"),
            (regime_input.fresh, "regime_stale"),
            (regime_input.exact_cohort_match, "regime_cohort_mismatch"),
            (regime_input.confirmed, "regime_unconfirmed"),
        ):
            if not accepted:
                return PromotionRevalidation(False, reason)
        try:
            rows = await self.repository.list_sliding_evidence(
                current_cohort,
                window_start_ms=max(
                    0,
                    now - self.config.evidence_window_seconds * 1000,
                ),
                as_of_ms=now,
                activation_cutoff_ms=self.activation_cutoff_ms,
                max_terminal_latency_ms=self.max_terminal_latency_ms,
                eligible_only=False,
            )
            lane_paid_rows = await self.repository.list_lane_paid_evidence(
                environment=current_cohort.environment,
                symbol=current_cohort.symbol,
                lane_code=current_cohort.lane_code,
                window_start_ms=max(
                    0,
                    now - self.config.evidence_window_seconds * 1000,
                ),
                as_of_ms=now,
                activation_cutoff_ms=self.activation_cutoff_ms,
            )
            aggregate = aggregate_promotion_evidence(
                rows,
                cohort=current_cohort,
                now_ms=now,
                config=self.config,
                activation_cutoff_ms=self.activation_cutoff_ms,
            )
            lane_paid_risk = summarize_paid_risk(lane_paid_rows)
            exact_risk = replace(
                risk,
                consecutive_paid_losses=lane_paid_risk.consecutive_losses,
                lane_net_pnl_usdc=lane_paid_risk.net_pnl_usdc,
                cohort_net_pnl_usdc=aggregate.snapshot.paid_net_pnl_usdc,
            )
            risk_reason = _risk_block_reason(exact_risk, self.config)
            if risk_reason is not None:
                return PromotionRevalidation(False, risk_reason)
            row = await self.repository.get_lease(meta.cohort_key)
        except Exception:
            self._database_healthy = False
            return PromotionRevalidation(False, "database_unhealthy")
        if row is None:
            return PromotionRevalidation(False, "lease_missing")
        checks = (
            (_text(row.get("status"), upper=True) == "ACTIVE", "lease_not_active"),
            (_text(row.get("lease_id")) == _text(meta.lease_id), "lease_id_changed"),
            (
                _text(row.get("promotion_policy_hash"))
                == meta.promotion_policy_hash,
                "promotion_policy_hash_changed",
            ),
            (
                _text(row.get("evidence_snapshot_hash"))
                == meta.evidence_snapshot_hash,
                "evidence_snapshot_hash_changed",
            ),
            (
                int(row.get("expires_at_ms") or 0) == meta.expires_at_ms
                and int(row.get("expires_at_ms") or 0) > now,
                "lease_expired_or_changed",
            ),
            (
                float(row.get("notional_cap_usdc") or 0.0)
                == float(meta.notional_cap_usdc)
                and actual_notional
                <= float(row.get("notional_cap_usdc") or 0.0),
                "notional_cap_changed",
            ),
            (
                all(
                    _text(row.get(name)) == _text(getattr(meta, name))
                    for name in _EVIDENCE_IDENTITY_FIELDS
                ),
                "cohort_identity_changed",
            ),
        )
        for valid, reason in checks:
            if not valid:
                return PromotionRevalidation(False, reason)
        try:
            claimed = await self.repository.claim_admission(
                meta.cohort_key,
                lease_id=_text(meta.lease_id),
                expected_generation=int(meta.generation),
                current_identity=current_cohort,
                now_ms=now,
                actual_notional_usdc=actual_notional,
                idempotency_key=_idempotency_key(
                    meta.cohort_key,
                    _text(consume_id),
                    "ADMISSION_CONSUMED",
                ),
                actor=self.actor,
            )
        except AdmissionClaimError as exc:
            return PromotionRevalidation(
                False,
                f"admission_claim_conflict:{str(exc)[:120]}",
            )
        except Exception:
            self._database_healthy = False
            return PromotionRevalidation(False, "database_unhealthy")
        if not bool(claimed.get("claim_granted")):
            return PromotionRevalidation(False, "admission_claim_replayed")
        claim_generation = int(claimed.get("claim_generation") or 0)
        if claim_generation != int(meta.generation) + 1:
            self._database_healthy = False
            return PromotionRevalidation(False, "admission_claim_invalid")
        updated_metadata = replace(
            meta,
            generation=claim_generation,
            lease_status=_text(claimed.get("status"), upper=True),
            lease_phase=_text(claimed.get("phase"), upper=True),
            expires_at_ms=int(claimed["expires_at_ms"]),
            notional_cap_usdc=float(claimed["notional_cap_usdc"]),
            applied_notional_usdc=actual_notional,
        )
        return PromotionRevalidation(
            True,
            "active_lease_claimed",
            float(claimed["notional_cap_usdc"]),
            claim_generation,
            updated_metadata,
        )


__all__ = [
    "AggregatedPromotionEvidence",
    "EvidenceProjection",
    "PaidRiskSummary",
    "PromotionAdmissionMetadata",
    "PromotionRegimeSnapshot",
    "PromotionRevalidation",
    "PromotionRuntimeResult",
    "V1464PromotionRuntime",
    "adaptive_promotion_config_from_settings",
    "aggregate_promotion_evidence",
    "derive_regime_input",
    "project_paid_terminal_evidence",
    "promotion_cohort_from_identity",
    "summarize_paid_risk",
    "validate_exact_registry_identity",
]
