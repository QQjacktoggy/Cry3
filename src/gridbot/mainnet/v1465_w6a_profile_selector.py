"""Pure v1.4.65 W6A profile selector.

The module deliberately has no database, exchange, settings, or runtime
dependency.  It accepts repository-shaped mappings, rejects evidence that is
not terminal and authoritative as of the supplied timestamp, evaluates three
fixed windows, and applies deterministic winner hysteresis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from types import MappingProxyType
from typing import Any, Mapping, Sequence


V1465_VERSION = "v1.4.65"
W6A_SELECTOR_LEASE_TTL_SECONDS = 10 * 60

_PROFILE_ORDER = ("W6A_BASE", "W6A_TIGHT", "W6A_PASSIVE")
_TP_OUTCOMES = frozenset({"TP", "TP_FIRST", "TP1_FIRST", "TAKE_PROFIT"})
_SL_OUTCOMES = frozenset({"SL", "SL_FIRST", "STOP", "STOP_LOSS"})
_NON_EVALUABLE_OUTCOMES = frozenset(
    {"NO_FILL", "ENTRY_EXPIRED", "ENTRY_TTL_EXPIRED", "MAX_HOLD"}
)
_AMBIGUOUS_OUTCOMES = frozenset(
    {"AMBIGUOUS", "AMBIGUOUS_BOTH", "BOTH", "TP_AND_SL"}
)


@dataclass(frozen=True, slots=True)
class W6AProfileDefinition:
    profile_id: str
    entry_offset_bp: float
    tp_bp: float
    sl_bp: float
    entry_ttl_seconds: int
    full_exit: bool = True
    partial_exit_pct: float = 1.0
    dca_enabled: bool = False
    runner_enabled: bool = False
    one_step_reprice_enabled: bool = False

    def __post_init__(self) -> None:
        if self.profile_id not in _PROFILE_ORDER:
            raise ValueError("unknown W6A profile_id")
        for name in ("entry_offset_bp", "tp_bp", "sl_bp"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        if (
            isinstance(self.entry_ttl_seconds, bool)
            or not isinstance(self.entry_ttl_seconds, int)
            or self.entry_ttl_seconds <= 0
        ):
            raise ValueError("entry_ttl_seconds must be a positive integer")
        if not self.full_exit or self.partial_exit_pct != 1.0:
            raise ValueError("v1.4.65 W6A profiles must use a full exit")
        if self.dca_enabled or self.runner_enabled or self.one_step_reprice_enabled:
            raise ValueError("DCA, runner, and one-step reprice must be disabled")

    @property
    def entry_bp(self) -> float:
        return self.entry_offset_bp

    @property
    def tp1_bp(self) -> float:
        return self.tp_bp

    @property
    def full_tp_bp(self) -> float:
        return self.tp_bp

    @property
    def entry_ttl_s(self) -> int:
        return self.entry_ttl_seconds

    @property
    def ttl_seconds(self) -> int:
        return self.entry_ttl_seconds

    @property
    def reprice_enabled(self) -> bool:
        return self.one_step_reprice_enabled

    @property
    def metadata(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "exit_mode": "full",
                "full_exit": self.full_exit,
                "partial_exit_pct": self.partial_exit_pct,
                "dca_enabled": self.dca_enabled,
                "runner_enabled": self.runner_enabled,
                "reprice_enabled": self.one_step_reprice_enabled,
            }
        )


W6A_PROFILES: Mapping[str, W6AProfileDefinition] = MappingProxyType(
    {
        "W6A_BASE": W6AProfileDefinition("W6A_BASE", 0.0, 6.0, 20.0, 180),
        "W6A_TIGHT": W6AProfileDefinition("W6A_TIGHT", 0.0, 6.0, 10.0, 90),
        "W6A_PASSIVE": W6AProfileDefinition(
            "W6A_PASSIVE", 2.0, 8.0, 12.0, 120
        ),
    }
)


@dataclass(frozen=True, slots=True)
class W6AWindowConfig:
    safety_window_seconds: int = 15 * 60
    authority_window_seconds: int = 30 * 60
    guard_window_seconds: int = 90 * 60
    safety_sl_limit: int = 2
    authority_min_evaluable: int = 5
    authority_min_tp: int = 4
    authority_min_ev_bp: float = 1.0
    guard_min_evaluable: int = 8
    guard_min_ev_bp: float = -6.0
    guard_max_sl_ratio: float = 0.45
    switch_min_ev_delta_bp: float = 2.0
    switch_min_paired_wins: int = 3
    lease_ttl_seconds: int = W6A_SELECTOR_LEASE_TTL_SECONDS

    def __post_init__(self) -> None:
        for name in (
            "safety_window_seconds",
            "authority_window_seconds",
            "guard_window_seconds",
            "safety_sl_limit",
            "authority_min_evaluable",
            "authority_min_tp",
            "guard_min_evaluable",
            "switch_min_paired_wins",
            "lease_ttl_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not (
            self.safety_window_seconds
            <= self.authority_window_seconds
            <= self.guard_window_seconds
        ):
            raise ValueError("W6A windows must be ordered safety <= authority <= guard")
        if self.authority_min_tp > self.authority_min_evaluable:
            raise ValueError("authority_min_tp exceeds authority_min_evaluable")
        for name in (
            "authority_min_ev_bp",
            "guard_min_ev_bp",
            "guard_max_sl_ratio",
            "switch_min_ev_delta_bp",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        if not 0.0 <= self.guard_max_sl_ratio <= 1.0:
            raise ValueError("guard_max_sl_ratio must be in [0, 1]")
        if self.switch_min_ev_delta_bp < 0.0:
            raise ValueError("switch_min_ev_delta_bp must be non-negative")


DEFAULT_W6A_WINDOW_CONFIG = W6AWindowConfig()


@dataclass(frozen=True, slots=True)
class W6AMarketState:
    state: str
    missing: tuple[str, ...]
    risk_score: int
    no_reclaim: bool | None
    risk_flags: Mapping[str, bool]
    stale_hard: bool

    @property
    def market_state(self) -> str:
        return self.state


def _lookup(source: Any, *keys: str) -> Any:
    sources = [source]
    if isinstance(source, Mapping):
        for nested_key in ("metrics", "features"):
            nested = source.get(nested_key)
            if isinstance(nested, Mapping):
                sources.append(nested)
    for candidate in sources:
        if isinstance(candidate, Mapping):
            for key in keys:
                if key in candidate and candidate.get(key) is not None:
                    return candidate.get(key)
        else:
            for key in keys:
                value = getattr(candidate, key, None)
                if value is not None:
                    return value
    return None


def _feature_number(source: Any, *keys: str) -> float | None:
    value = _lookup(source, *keys)
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def classify_w6a_market_state(features: Mapping[str, Any] | Any) -> W6AMarketState:
    """Classify the W6A state using the existing v1.3.7 risk features.

    Missing or non-finite required features always force ``mixed``.  This is
    intentional: default values must never manufacture reclaim/falling
    authority.
    """

    setup_age = _feature_number(
        features, "setup_age_sec", "reprice_wait_elapsed_seconds"
    )
    d30 = _feature_number(features, "d30", "d30_bp")
    vwap = _feature_number(features, "vwap_dist_bp")
    pullback = _feature_number(
        features, "pullback_from_recent_high_bp", "pullback"
    )
    reclaimed = _feature_number(features, "price_above_or_reclaimed_vwap")
    values = {
        "setup_age_sec": setup_age,
        "d30": d30,
        "vwap_dist_bp": vwap,
        "pullback_from_recent_high_bp": pullback,
        "price_above_or_reclaimed_vwap": reclaimed,
    }
    missing = tuple(name for name, value in values.items() if value is None)
    if missing:
        return W6AMarketState(
            state="mixed",
            missing=missing,
            risk_score=0,
            no_reclaim=None,
            risk_flags=MappingProxyType({}),
            stale_hard=False,
        )

    assert (
        setup_age is not None
        and d30 is not None
        and vwap is not None
        and pullback is not None
        and reclaimed is not None
    )
    no_reclaim = reclaimed <= 0.0
    risk_flags = {
        "no_reclaim": no_reclaim,
        "vwap_lte_neg45": vwap <= -45.0,
        "pullback_gte_25": pullback >= 25.0,
        "setup_age_gte_300": setup_age >= 300.0,
        "d30_lte_neg30": d30 <= -30.0,
    }
    risk_score = sum(risk_flags.values())
    stale_hard = setup_age >= 600.0 and no_reclaim and vwap <= -30.0
    if stale_hard or risk_score >= 4:
        state = "falling_trap"
    elif not no_reclaim and d30 > -30.0 and pullback < 25.0 and setup_age <= 300.0:
        state = "reclaim"
    else:
        state = "mixed"
    return W6AMarketState(
        state=state,
        missing=(),
        risk_score=risk_score,
        no_reclaim=no_reclaim,
        risk_flags=MappingProxyType(risk_flags),
        stale_hard=stale_hard,
    )


class W6AEvidenceError(ValueError):
    """A fail-closed evidence rejection with a stable machine-readable reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _as_nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or value is None:
        raise W6AEvidenceError(f"{field_name}_invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise W6AEvidenceError(f"{field_name}_invalid") from exc
    if parsed < 0 or str(value).strip() not in {str(parsed), f"{parsed}.0"}:
        raise W6AEvidenceError(f"{field_name}_invalid")
    return parsed


def _as_finite_float(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or value is None:
        raise W6AEvidenceError(f"{field_name}_invalid")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise W6AEvidenceError(f"{field_name}_invalid") from exc
    if not isfinite(parsed):
        raise W6AEvidenceError(f"{field_name}_invalid")
    return parsed


def _flag(row: Mapping[str, Any], *keys: str) -> bool:
    value = _lookup(row, *keys)
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _text(row: Mapping[str, Any], *keys: str) -> str:
    value = _lookup(row, *keys)
    return str(value or "").strip()


def _normalize_outcome(value: Any) -> str:
    outcome = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if outcome in _TP_OUTCOMES:
        return "TP"
    if outcome in _SL_OUTCOMES:
        return "SL"
    if outcome in _NON_EVALUABLE_OUTCOMES:
        return outcome
    if outcome in _AMBIGUOUS_OUTCOMES:
        raise W6AEvidenceError("ambiguous")
    raise W6AEvidenceError("terminal_outcome_invalid")


@dataclass(frozen=True, slots=True)
class W6AProfileEvidence:
    opportunity_id: str
    profile_id: str
    observed_at_ms: int
    terminal_at_ms: int
    terminal_outcome: str
    net_pnl_bp: float

    @property
    def outcome(self) -> str:
        return self.terminal_outcome

    @property
    def evaluable(self) -> bool:
        return self.terminal_outcome in {"TP", "SL"}

    @classmethod
    def from_mapping(
        cls, row: Mapping[str, Any], *, as_of_ms: int
    ) -> "W6AProfileEvidence":
        result = parse_w6a_profile_evidence(row, as_of_ms=as_of_ms)
        if result.evidence is None:
            raise W6AEvidenceError(result.reason or "evidence_excluded")
        return result.evidence


@dataclass(frozen=True, slots=True)
class W6AEvidenceParseResult:
    evidence: W6AProfileEvidence | None
    reason: str | None = None

    @property
    def accepted(self) -> bool:
        return self.evidence is not None


def parse_w6a_profile_evidence(
    row: Mapping[str, Any], *, as_of_ms: int
) -> W6AEvidenceParseResult:
    """Parse one repository row, returning an explicit exclusion reason."""

    if not isinstance(row, Mapping):
        return W6AEvidenceParseResult(None, "mapping_required")
    try:
        now = _as_nonnegative_int(as_of_ms, "as_of_ms")
        if _flag(
            row,
            "diagnostic_only",
            "excluded_from_selector",
            "excluded_from_promotion",
        ) or _text(row, "source_type").upper() == "DIAGNOSTIC":
            raise W6AEvidenceError("diagnostic")
        if not _flag(row, "data_complete", "complete"):
            raise W6AEvidenceError("incomplete")
        if _flag(row, "ambiguous", "ambiguity_flag"):
            raise W6AEvidenceError("ambiguous")

        profile_id = _text(row, "profile_id", "candidate_profile_id").upper()
        if profile_id not in W6A_PROFILES:
            raise W6AEvidenceError("profile_id_invalid")
        opportunity_id = _text(row, "opportunity_id", "episode_id", "sample_id")
        if not opportunity_id:
            raise W6AEvidenceError("opportunity_id_invalid")
        observed = _as_nonnegative_int(
            _lookup(row, "observed_at_ms"), "observed_at_ms"
        )
        terminal = _as_nonnegative_int(
            _lookup(row, "terminal_at_ms", "resolved_at_ms"), "terminal_at_ms"
        )
        if terminal < observed:
            raise W6AEvidenceError("terminal_before_observed")
        if observed > now:
            raise W6AEvidenceError("future_observation")
        if terminal > now:
            raise W6AEvidenceError("future_terminal")
        outcome = _normalize_outcome(
            _lookup(row, "terminal_outcome", "outcome", "result")
        )
        net_pnl_bp = _as_finite_float(
            _lookup(
                row,
                "net_pnl_bp",
                "fee_net_pnl_bp",
                "net_ev_bp",
                "ev_bp",
            ),
            "net_pnl_bp",
        )
        return W6AEvidenceParseResult(
            W6AProfileEvidence(
                opportunity_id=opportunity_id,
                profile_id=profile_id,
                observed_at_ms=observed,
                terminal_at_ms=terminal,
                terminal_outcome=outcome,
                net_pnl_bp=net_pnl_bp,
            )
        )
    except W6AEvidenceError as exc:
        return W6AEvidenceParseResult(None, exc.reason)


@dataclass(frozen=True, slots=True)
class W6AProfileSummary:
    profile_id: str
    eligible: bool
    blockers: tuple[str, ...]
    metrics: Mapping[str, Any]
    evidence: tuple[W6AProfileEvidence, ...] = field(
        default=(), repr=False, compare=False
    )

    @property
    def authority_ev_bp(self) -> float:
        return float(self.metrics["authority_ev_bp"])


def _window(
    evidence: Sequence[W6AProfileEvidence], *, now_ms: int, seconds: int
) -> tuple[W6AProfileEvidence, ...]:
    cutoff = max(0, now_ms - seconds * 1000)
    return tuple(row for row in evidence if cutoff <= row.observed_at_ms <= now_ms)


def _ev(rows: Sequence[W6AProfileEvidence]) -> float:
    # EV is per captured, complete opportunity.  A no-fill contributes zero
    # instead of disappearing from the denominator; otherwise a passive
    # profile can look artificially superior merely by filling less often.
    values = [row.net_pnl_bp for row in rows]
    return sum(values) / len(values) if values else 0.0


def evaluate_w6a_profile(
    rows: Sequence[Mapping[str, Any] | W6AProfileEvidence],
    profile_id: str,
    now_ms: int,
    config: W6AWindowConfig = DEFAULT_W6A_WINDOW_CONFIG,
) -> W6AProfileSummary:
    """Evaluate one profile against safety, authority, and guard windows."""

    profile = str(profile_id or "").strip().upper()
    if profile not in W6A_PROFILES:
        raise ValueError("unknown W6A profile_id")
    now = _as_nonnegative_int(now_ms, "now_ms")
    if not isinstance(config, W6AWindowConfig):
        raise TypeError("config must be W6AWindowConfig")

    accepted: list[W6AProfileEvidence] = []
    exclusions: dict[str, int] = {}
    for raw in rows:
        if isinstance(raw, W6AProfileEvidence):
            if raw.profile_id != profile:
                continue
            if raw.observed_at_ms > now:
                exclusions["future_observation"] = (
                    exclusions.get("future_observation", 0) + 1
                )
                continue
            if raw.terminal_at_ms > now:
                exclusions["future_terminal"] = (
                    exclusions.get("future_terminal", 0) + 1
                )
                continue
            accepted.append(raw)
            continue
        raw_profile = _text(raw, "profile_id", "candidate_profile_id").upper()
        if raw_profile and raw_profile != profile:
            continue
        parsed = parse_w6a_profile_evidence(raw, as_of_ms=now)
        if parsed.evidence is None:
            reason = parsed.reason or "evidence_excluded"
            exclusions[reason] = exclusions.get(reason, 0) + 1
        elif parsed.evidence.profile_id == profile:
            accepted.append(parsed.evidence)

    accepted.sort(
        key=lambda row: (
            row.observed_at_ms,
            row.terminal_at_ms,
            row.opportunity_id,
        )
    )
    safety = _window(
        accepted, now_ms=now, seconds=config.safety_window_seconds
    )
    authority = _window(
        accepted, now_ms=now, seconds=config.authority_window_seconds
    )
    guard = _window(accepted, now_ms=now, seconds=config.guard_window_seconds)
    safety_eval = tuple(row for row in safety if row.evaluable)
    authority_eval = tuple(row for row in authority if row.evaluable)
    guard_eval = tuple(row for row in guard if row.evaluable)
    safety_sl = sum(row.terminal_outcome == "SL" for row in safety_eval)
    authority_tp = sum(row.terminal_outcome == "TP" for row in authority_eval)
    authority_sl = sum(row.terminal_outcome == "SL" for row in authority_eval)
    guard_tp = sum(row.terminal_outcome == "TP" for row in guard_eval)
    guard_sl = sum(row.terminal_outcome == "SL" for row in guard_eval)
    authority_ev = _ev(authority)
    guard_ev = _ev(guard)
    guard_sl_ratio = guard_sl / len(guard_eval) if guard_eval else 0.0
    latest_safety_outcome = (
        safety_eval[-1].terminal_outcome if safety_eval else None
    )

    blockers: list[str] = []
    if safety_sl >= config.safety_sl_limit:
        blockers.append("safety_sl_limit")
    if latest_safety_outcome == "SL":
        blockers.append("safety_latest_sl")
    if len(authority_eval) < config.authority_min_evaluable:
        blockers.append("authority_insufficient_evaluable")
    if authority_tp < config.authority_min_tp:
        blockers.append("authority_insufficient_tp")
    if not authority_ev > config.authority_min_ev_bp:
        blockers.append("authority_ev_not_above_threshold")
    if len(guard_eval) < config.guard_min_evaluable:
        blockers.append("guard_insufficient_evaluable")
    if not guard_ev > config.guard_min_ev_bp:
        blockers.append("guard_ev_not_above_threshold")
    if guard_sl_ratio > config.guard_max_sl_ratio:
        blockers.append("guard_sl_ratio_above_limit")

    metrics: Mapping[str, Any] = MappingProxyType(
        {
            "safety_cutoff_ms": max(
                0, now - config.safety_window_seconds * 1000
            ),
            "safety_evaluable": len(safety_eval),
            "safety_sl": safety_sl,
            "safety_latest_outcome": latest_safety_outcome,
            "authority_cutoff_ms": max(
                0, now - config.authority_window_seconds * 1000
            ),
            "authority_evaluable": len(authority_eval),
            "authority_tp": authority_tp,
            "authority_sl": authority_sl,
            "authority_ev_bp": authority_ev,
            "guard_cutoff_ms": max(
                0, now - config.guard_window_seconds * 1000
            ),
            "guard_evaluable": len(guard_eval),
            "guard_tp": guard_tp,
            "guard_sl": guard_sl,
            "guard_ev_bp": guard_ev,
            "guard_sl_ratio": guard_sl_ratio,
            "accepted": len(accepted),
            "excluded": sum(exclusions.values()),
            "exclusions": MappingProxyType(dict(sorted(exclusions.items()))),
        }
    )
    return W6AProfileSummary(
        profile_id=profile,
        eligible=not blockers,
        blockers=tuple(blockers),
        metrics=metrics,
        evidence=tuple(accepted),
    )


@dataclass(frozen=True, slots=True)
class W6ASelectionDecision:
    winner_profile_id: str | None
    previous_profile_id: str | None
    changed: bool
    reason: str
    lease_ttl_seconds: int
    blockers: Mapping[str, tuple[str, ...]]
    metrics: Mapping[str, Any]
    summaries: Mapping[str, W6AProfileSummary]

    @property
    def selected_profile_id(self) -> str | None:
        return self.winner_profile_id

    @property
    def should_switch(self) -> bool:
        return self.changed


def _paired_wins(
    challenger: W6AProfileSummary,
    incumbent: W6AProfileSummary,
    *,
    now_ms: int,
    config: W6AWindowConfig,
) -> tuple[int, int]:
    cutoff = max(0, now_ms - config.authority_window_seconds * 1000)

    def by_opportunity(summary: W6AProfileSummary) -> dict[str, W6AProfileEvidence]:
        result: dict[str, W6AProfileEvidence] = {}
        for row in summary.evidence:
            if not cutoff <= row.observed_at_ms <= now_ms:
                continue
            previous = result.get(row.opportunity_id)
            if previous is None or (
                row.observed_at_ms,
                row.terminal_at_ms,
            ) > (
                previous.observed_at_ms,
                previous.terminal_at_ms,
            ):
                result[row.opportunity_id] = row
        return result

    challenger_rows = by_opportunity(challenger)
    incumbent_rows = by_opportunity(incumbent)
    paired = sorted(challenger_rows.keys() & incumbent_rows.keys())
    wins = sum(
        challenger_rows[key].net_pnl_bp > incumbent_rows[key].net_pnl_bp
        for key in paired
    )
    return wins, len(paired)


def select_w6a_winner(
    rows: Sequence[Mapping[str, Any] | W6AProfileEvidence],
    now_ms: int,
    current_winner_profile_id: str | None = None,
    config: W6AWindowConfig = DEFAULT_W6A_WINDOW_CONFIG,
) -> W6ASelectionDecision:
    """Select only among eligible profiles and apply incumbent hysteresis."""

    now = _as_nonnegative_int(now_ms, "now_ms")
    summaries_dict = {
        profile_id: evaluate_w6a_profile(rows, profile_id, now, config)
        for profile_id in _PROFILE_ORDER
    }
    summaries = MappingProxyType(summaries_dict)
    eligible = [
        summaries_dict[profile_id]
        for profile_id in _PROFILE_ORDER
        if summaries_dict[profile_id].eligible
    ]
    blockers = MappingProxyType(
        {
            profile_id: summary.blockers
            for profile_id, summary in summaries_dict.items()
        }
    )
    current = str(current_winner_profile_id or "").strip().upper() or None
    if current not in W6A_PROFILES:
        current = None

    base_metrics: dict[str, Any] = {
        "eligible_profile_ids": tuple(summary.profile_id for summary in eligible),
        "authority_ev_bp": MappingProxyType(
            {
                profile_id: summaries_dict[profile_id].authority_ev_bp
                for profile_id in _PROFILE_ORDER
            }
        ),
        "switch_min_ev_delta_bp": config.switch_min_ev_delta_bp,
        "switch_min_paired_wins": config.switch_min_paired_wins,
    }
    if not eligible:
        return W6ASelectionDecision(
            winner_profile_id=None,
            previous_profile_id=current,
            changed=current is not None,
            reason="no_eligible_profile",
            lease_ttl_seconds=config.lease_ttl_seconds,
            blockers=blockers,
            metrics=MappingProxyType(base_metrics),
            summaries=summaries,
        )

    rank = {profile_id: index for index, profile_id in enumerate(_PROFILE_ORDER)}
    ranked = sorted(
        eligible,
        key=lambda summary: (
            -summary.authority_ev_bp,
            rank[summary.profile_id],
        ),
    )
    current_summary = summaries_dict.get(current) if current is not None else None
    if current_summary is None or not current_summary.eligible:
        selected = ranked[0]
        base_metrics["selected_authority_ev_bp"] = selected.authority_ev_bp
        return W6ASelectionDecision(
            winner_profile_id=selected.profile_id,
            previous_profile_id=current,
            changed=selected.profile_id != current,
            reason=(
                "highest_ev_no_incumbent"
                if current is None
                else "highest_ev_incumbent_ineligible"
            ),
            lease_ttl_seconds=config.lease_ttl_seconds,
            blockers=blockers,
            metrics=MappingProxyType(base_metrics),
            summaries=summaries,
        )

    qualifying: list[tuple[W6AProfileSummary, int, int, float]] = []
    comparisons: dict[str, Mapping[str, Any]] = {}
    for challenger in ranked:
        if challenger.profile_id == current:
            continue
        wins, paired = _paired_wins(
            challenger, current_summary, now_ms=now, config=config
        )
        delta = challenger.authority_ev_bp - current_summary.authority_ev_bp
        qualifies = bool(
            delta >= config.switch_min_ev_delta_bp
            and wins >= config.switch_min_paired_wins
        )
        comparisons[challenger.profile_id] = MappingProxyType(
            {
                "ev_delta_bp": delta,
                "paired_wins": wins,
                "paired_opportunities": paired,
                "qualifies": qualifies,
            }
        )
        if qualifies:
            qualifying.append((challenger, wins, paired, delta))
    base_metrics["challenger_comparisons"] = MappingProxyType(comparisons)
    if not qualifying:
        base_metrics["selected_authority_ev_bp"] = (
            current_summary.authority_ev_bp
        )
        return W6ASelectionDecision(
            winner_profile_id=current,
            previous_profile_id=current,
            changed=False,
            reason="incumbent_retained_hysteresis",
            lease_ttl_seconds=config.lease_ttl_seconds,
            blockers=blockers,
            metrics=MappingProxyType(base_metrics),
            summaries=summaries,
        )

    qualifying.sort(
        key=lambda item: (-item[0].authority_ev_bp, rank[item[0].profile_id])
    )
    selected, wins, paired, delta = qualifying[0]
    base_metrics.update(
        {
            "selected_authority_ev_bp": selected.authority_ev_bp,
            "selected_ev_delta_bp": delta,
            "selected_paired_wins": wins,
            "selected_paired_opportunities": paired,
        }
    )
    return W6ASelectionDecision(
        winner_profile_id=selected.profile_id,
        previous_profile_id=current,
        changed=True,
        reason="challenger_won_hysteresis",
        lease_ttl_seconds=config.lease_ttl_seconds,
        blockers=blockers,
        metrics=MappingProxyType(base_metrics),
        summaries=summaries,
    )


__all__ = [
    "DEFAULT_W6A_WINDOW_CONFIG",
    "V1465_VERSION",
    "W6AEvidenceError",
    "W6AEvidenceParseResult",
    "W6AMarketState",
    "W6AProfileDefinition",
    "W6AProfileEvidence",
    "W6AProfileSummary",
    "W6ASelectionDecision",
    "W6AWindowConfig",
    "W6A_PROFILES",
    "W6A_SELECTOR_LEASE_TTL_SECONDS",
    "classify_w6a_market_state",
    "evaluate_w6a_profile",
    "parse_w6a_profile_evidence",
    "select_w6a_winner",
]
