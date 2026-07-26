"""Read-only legacy, v1.4.64, and v1.4.65 Lane Monitor for Telegram.

The registry is the fixed left side of the join, so all 27 lanes remain
visible even when no evidence has been captured.  Evidence is reduced to one
row per opportunity before lane totals are calculated; a durable v1.4.63
ticket, the later shadow-sample event, and adaptive-table copies must never
inflate the cohort.
"""
from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from typing import Any

from src.gridbot.mainnet.v1462_admission import V1462_POLICY_HASH
from src.gridbot.mainnet.v1462_lane_registry import (
    CNL_SAFE_LINEAGE_KIND,
    LEGACY_LANE_REGISTRY,
    LIVE_CONTROL_CONTRACTS,
    REGISTRY_HASH as V1462_REGISTRY_HASH,
)
from src.gridbot.mainnet.v1469_arbiter_evidence_mapper import (
    map_durable_paired_evidence,
)
from src.gridbot.mainnet.v1469_arm_arbiter import (
    ArbiterRequest,
    CurrentLease,
    LeasePhase,
    RegimeSnapshot,
    evaluate_rolling_arbiter,
)
from src.gridbot.storage.v1469_arm_observation_repository import (
    V1469ArmObservationRepository,
)

try:  # Importing the view must not make non-Telegram review tooling fail.
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
except ImportError:  # pragma: no cover - production installs python-telegram-bot
    InlineKeyboardButton = InlineKeyboardMarkup = None  # type: ignore[assignment,misc]


_LANE_CODES = tuple(str(row["lane_code"]) for row in LEGACY_LANE_REGISTRY)
_FILLED_OUTCOMES = {"tp1_first", "tp_first", "tp", "sl_first", "sl", "max_hold"}
_KNOWN_TERMINAL_OUTCOMES = {*_FILLED_OUTCOMES, "no_fill", "ambiguous_both"}
_MIN_EVALUABLE = 8
_MIN_TP_FIRST = 6
_MIN_UTC_DATES = 2
_LANE_MONITOR_EVENT_TYPES = (
    "entry_codex_v1462_admission",
    "entry_codex_v1462_shadow_opportunity",
    "entry_codex_v1_shadow_sample_started",
    "entry_codex_v1_shadow_sample_dropped",
    "entry_codex_v1_shadow_outcome",
)


def _event_query(after_id: int = 0) -> str:
    cutoff = max(0, int(after_id))
    if cutoff == 0:
        streams = [
            "SELECT * FROM mainnet_run_events "
            f"WHERE event_type = '{event_type}' AND id > 0"
            for event_type in _LANE_MONITOR_EVENT_TYPES
        ]
        return " UNION ALL ".join(streams) + " ORDER BY id DESC"
    event_types = ", ".join(
        f"'{event_type}'" for event_type in _LANE_MONITOR_EVENT_TYPES
    )
    return (
        # The frozen snapshot makes id the narrowest live range.  A sequential
        # rowid walk avoids five random table-lookup streams through the
        # event_type index when details_json is cold on disk.
        "SELECT * FROM mainnet_run_events NOT INDEXED "
        f"WHERE id > {cutoff} AND event_type IN ({event_types}) "
        "ORDER BY id DESC"
    )


_TABLE_QUERIES = {
    "adaptive_opportunities": (
        "SELECT * FROM adaptive_opportunities "
        "ORDER BY rowid DESC"
    ),
    "shadow_evaluations": (
        "SELECT * FROM shadow_evaluations "
        "ORDER BY rowid DESC"
    ),
    "mainnet_runs": (
        "SELECT * FROM mainnet_runs "
        "ORDER BY rowid DESC LIMIT 5000"
    ),
    "v1462_lane_monitor_legacy_summary": (
        "SELECT * FROM v1462_lane_monitor_legacy_summary "
        "ORDER BY lane_code LIMIT 100"
    ),
    "v1463_lane_monitor_snapshot_meta": (
        "SELECT * FROM v1463_lane_monitor_snapshot_meta "
        "WHERE singleton_id = 1 LIMIT 2"
    ),
    "mainnet_run_events": _event_query(),
    "v1464_promotion_evidence": (
        "SELECT * FROM v1464_promotion_evidence "
        "ORDER BY observed_at_ms DESC, opportunity_id"
    ),
    "v1464_lane_promotion_leases": (
        "SELECT * FROM v1464_lane_promotion_leases "
        "ORDER BY updated_at_ms DESC, cohort_key"
    ),
    "v1464_lane_promotion_events": (
        "SELECT * FROM v1464_lane_promotion_events "
        "ORDER BY event_time_ms DESC, id DESC LIMIT 5000"
    ),
    "v1465_w6a_profile_evidence": (
        "SELECT * FROM v1465_w6a_profile_evidence "
        "ORDER BY observed_at_ms DESC LIMIT 5000"
    ),
    "v1465_w6a_profile_selections": (
        "SELECT * FROM v1465_w6a_profile_selections "
        "ORDER BY updated_at_ms DESC, selector_key LIMIT 500"
    ),
    "v1465_w6a_profile_selection_events": (
        "SELECT * FROM v1465_w6a_profile_selection_events "
        "ORDER BY event_time_ms DESC, id DESC LIMIT 500"
    ),
}
_IDENTITY_FIELDS = (
    "registry_hash",
    "policy_hash",
    "resolved_profile_hash",
    "state",
    "effective_side",
)
_READINESS_SOURCES = {
    "adaptive_opportunities",
    "shadow_evaluations",
    "mainnet_run_events",
    "v1463_lane_monitor_snapshot_meta",
}
_HTML_TOKEN_RE = re.compile(
    r"(</?[A-Za-z][^>]*>|&(?:#[0-9]+|#x[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]+);|.)",
    re.DOTALL,
)
_HTML_TAG_NAME_RE = re.compile(r"^</?([A-Za-z][A-Za-z0-9]*)")
_HTML_VOID_TAGS = {"br", "hr", "img", "input", "meta", "link"}
_CNL_CONTROL_RULE_IDS = frozenset(
    contract.rule_id
    for contract in LIVE_CONTROL_CONTRACTS
    if contract.lane_code == "CNL-WPR-L"
    and contract.safe_lineage_kind == CNL_SAFE_LINEAGE_KIND
)


def lane_monitor_html_chunks(text: str, *, limit: int = 3900) -> list[str]:
    """Split Telegram HTML while preserving tags, entities, and all blockers."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    if not text:
        return [""]

    chunks: list[str] = []
    current = ""
    open_tags: list[tuple[str, str]] = []

    def closing_suffix(tags: list[tuple[str, str]]) -> str:
        return "".join(f"</{name}>" for name, _opening in reversed(tags))

    def reopening_prefix(tags: list[tuple[str, str]]) -> str:
        return "".join(opening for _name, opening in tags)

    for token in _HTML_TOKEN_RE.findall(text):
        prospective_tags = list(open_tags)
        tag_match = _HTML_TAG_NAME_RE.match(token)
        is_closing = token.startswith("</")
        is_opening = bool(
            tag_match
            and not is_closing
            and not token.rstrip().endswith("/>")
            and tag_match.group(1).lower() not in _HTML_VOID_TAGS
        )
        if is_closing and tag_match:
            name = tag_match.group(1).lower()
            if prospective_tags and prospective_tags[-1][0] == name:
                prospective_tags.pop()
        elif is_opening and tag_match:
            prospective_tags.append((tag_match.group(1).lower(), token))

        required = len(token) + len(closing_suffix(prospective_tags))
        if current and len(current) + required > limit:
            suffix = closing_suffix(open_tags)
            chunks.append(current + suffix)
            current = reopening_prefix(open_tags)
        if len(current) + required > limit:
            raise ValueError("limit is too small for one HTML token and its tags")

        current += token
        open_tags = prospective_tags

    if current or not chunks:
        current += closing_suffix(open_tags)
        if len(current) > limit:
            raise ValueError("final HTML chunk exceeds limit")
        chunks.append(current)
    return chunks


@dataclass(frozen=True, order=True)
class CohortKey:
    lane_code: str
    state: str
    effective_side: str
    resolved_profile_hash: str
    registry_hash: str
    policy_hash: str

    @property
    def label(self) -> str:
        def short(value: str) -> str:
            if value.startswith(("MISSING", "MIXED")):
                return value
            return value[:10]

        return (
            f"{self.state}/{self.effective_side} "
            f"profile={short(self.resolved_profile_hash)} "
            f"reg={short(self.registry_hash)} policy={short(self.policy_hash)}"
        )


@dataclass
class CohortEvidence:
    key: CohortKey
    captured: int = 0
    complete: int = 0
    evaluable: int = 0
    invalid: int = 0
    incomplete: int = 0
    ambiguous: int = 0
    dropped: int = 0
    pending: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)
    ev_total: float = 0.0
    ev_count: int = 0
    last_at_ms: int = 0
    invalid_reasons: Counter[str] = field(default_factory=Counter)
    utc_dates: set[str] = field(default_factory=set)
    identity_missing: int = 0
    identity_conflicts: int = 0
    terminal_conflicts: int = 0
    lane_conflicts: int = 0
    live_reopen_breaches: int = 0
    unavailable_sources: set[str] = field(default_factory=set)
    global_blockers: set[str] = field(default_factory=set)

    @property
    def tp_first(self) -> int:
        return (
            self.outcomes["tp1_first"]
            + self.outcomes["tp_first"]
            + self.outcomes["tp"]
        )

    @property
    def ev_per_opportunity(self) -> float | None:
        return self.ev_total / self.ev_count if self.ev_count else None

    @property
    def data_blockers(self) -> tuple[str, ...]:
        blockers = sorted(self.global_blockers)
        if self.incomplete:
            blockers.append(f"incomplete={self.incomplete} (must 0)")
        if self.ambiguous:
            blockers.append(f"ambiguous={self.ambiguous} (must 0)")
        if self.dropped:
            blockers.append(f"dropped={self.dropped} (collection incomplete)")
        if self.invalid:
            blockers.append(f"invalid={self.invalid} (must 0)")
        if self.identity_missing:
            blockers.append(f"identity_missing={self.identity_missing}")
        if self.identity_conflicts:
            blockers.append(f"identity_conflicts={self.identity_conflicts}")
        if self.terminal_conflicts:
            blockers.append(f"terminal_conflicts={self.terminal_conflicts}")
        if self.lane_conflicts:
            blockers.append(f"lane_code_conflicts={self.lane_conflicts}")
        if self.key.registry_hash != V1462_REGISTRY_HASH:
            blockers.append("registry_hash_not_current")
        if self.key.policy_hash != V1462_POLICY_HASH:
            blockers.append("policy_hash_not_current")
        if self.live_reopen_breaches:
            blockers.append(
                f"reject_reopen_live_breach={self.live_reopen_breaches} (must 0)"
            )
        if self.captured and self.unavailable_sources:
            blockers.append(
                "unavailable_sources=" + ",".join(sorted(self.unavailable_sources))
            )
        return tuple(blockers)

    @property
    def threshold_gaps(self) -> tuple[str, ...]:
        gaps: list[str] = []
        if self.pending:
            gaps.append(f"pending={self.pending} (wait terminal)")
        if self.evaluable < _MIN_EVALUABLE:
            gaps.append(
                f"evaluable={self.evaluable}/{_MIN_EVALUABLE} "
                f"(need {_MIN_EVALUABLE - self.evaluable})"
            )
        if self.tp_first < _MIN_TP_FIRST:
            gaps.append(
                f"TP_FIRST={self.tp_first}/{_MIN_TP_FIRST} "
                f"(need {_MIN_TP_FIRST - self.tp_first})"
            )
        ev = self.ev_per_opportunity
        if ev is None:
            gaps.append("fee-net EV/opportunity=missing (need >0)")
        elif ev <= 0:
            gaps.append(f"fee-net EV/opportunity={ev:+.4f} (need >0)")
        if len(self.utc_dates) < _MIN_UTC_DATES:
            gaps.append(
                f"legacy UTC diversity days={len(self.utc_dates)}/{_MIN_UTC_DATES} "
                f"(need {_MIN_UTC_DATES - len(self.utc_dates)})"
            )
        return tuple(gaps)

    @property
    def promotion_blockers(self) -> tuple[str, ...]:
        return (*self.data_blockers, *self.threshold_gaps)

    @property
    def readiness(self) -> str:
        if self.data_blockers:
            return "DATA_BLOCKED"
        if self.captured and not self.threshold_gaps:
            return "REVIEW_READY"
        return "COLLECTING"


@dataclass
class LaneEvidence:
    code: str
    intended_mode: str
    captured: int = 0
    complete: int = 0
    evaluable: int = 0
    invalid: int = 0
    incomplete: int = 0
    ambiguous: int = 0
    dropped: int = 0
    pending: int = 0
    outcomes: Counter[str] = field(default_factory=Counter)
    ev_total: float = 0.0
    ev_count: int = 0
    paid_wins: int = 0
    paid_losses: int = 0
    paid_net: float = 0.0
    paid_count: int = 0
    legacy_adaptive: int = 0
    legacy_shadow_outcomes: int = 0
    legacy_last_at_ms: int = 0
    last_at_ms: int = 0
    invalid_reasons: Counter[str] = field(default_factory=Counter)
    utc_dates: set[str] = field(default_factory=set)
    identity_values: dict[str, set[str]] = field(
        default_factory=lambda: {name: set() for name in _IDENTITY_FIELDS}
    )
    identity_missing: int = 0
    identity_conflicts: int = 0
    terminal_conflicts: int = 0
    lane_conflicts: int = 0
    live_reopen_breaches: int = 0
    unavailable_sources: set[str] = field(default_factory=set)
    global_blockers: set[str] = field(default_factory=set)
    cohorts: dict[CohortKey, CohortEvidence] = field(default_factory=dict)

    @property
    def tp_first(self) -> int:
        return (
            self.outcomes["tp1_first"]
            + self.outcomes["tp_first"]
            + self.outcomes["tp"]
        )

    @property
    def ev_per_opportunity(self) -> float | None:
        return self.ev_total / self.ev_count if self.ev_count else None

    @property
    def data_blockers(self) -> tuple[str, ...]:
        if self.global_blockers:
            return tuple(sorted(self.global_blockers))
        if len(self.cohorts) == 1:
            return next(iter(self.cohorts.values())).data_blockers
        if len(self.cohorts) > 1:
            return (f"exact_cohorts={len(self.cohorts)} (review separately)",)
        return ()

    @property
    def threshold_gaps(self) -> tuple[str, ...]:
        if len(self.cohorts) == 1:
            return next(iter(self.cohorts.values())).threshold_gaps
        if len(self.cohorts) > 1:
            return ()
        gaps: list[str] = []
        if self.pending:
            gaps.append(f"pending={self.pending} (wait terminal)")
        if self.evaluable < _MIN_EVALUABLE:
            gaps.append(
                f"evaluable={self.evaluable}/{_MIN_EVALUABLE} "
                f"(need {_MIN_EVALUABLE - self.evaluable})"
            )
        if self.tp_first < _MIN_TP_FIRST:
            gaps.append(
                f"TP_FIRST={self.tp_first}/{_MIN_TP_FIRST} "
                f"(need {_MIN_TP_FIRST - self.tp_first})"
            )
        ev = self.ev_per_opportunity
        if ev is None:
            gaps.append("fee-net EV/opportunity=missing (need >0)")
        elif ev <= 0:
            gaps.append(f"fee-net EV/opportunity={ev:+.4f} (need >0)")
        if len(self.utc_dates) < _MIN_UTC_DATES:
            gaps.append(
                f"legacy UTC diversity days={len(self.utc_dates)}/{_MIN_UTC_DATES} "
                f"(need {_MIN_UTC_DATES - len(self.utc_dates)})"
            )
        return tuple(gaps)

    @property
    def promotion_blockers(self) -> tuple[str, ...]:
        return (*self.data_blockers, *self.threshold_gaps)

    @property
    def readiness(self) -> str:
        if self.global_blockers:
            return "DATA_BLOCKED"
        if len(self.cohorts) == 1:
            return next(iter(self.cohorts.values())).readiness
        if len(self.cohorts) > 1:
            return "COHORT_SPLIT"
        return "COLLECTING"


@dataclass
class PromotionRuntimeView:
    """Read-only v1.4.64 runtime status for one exact promotion cohort."""

    cohort_key: str
    lane_code: str
    market_state: str
    effective_side: str
    strategy: str
    resolved_profile_hash: str
    registry_hash: str
    admission_policy_hash: str
    promotion_policy_hash: str = ""
    state: str = "SHADOW"
    lease_id: str = ""
    generation: int = 0
    notional_cap_usdc: float | None = None
    expires_at_ms: int = 0
    evidence_count: int = 0
    evaluable: int = 0
    tp_first: int = 0
    sl_first: int = 0
    no_fill: int = 0
    fee_net_total: float = 0.0
    paid_complete: int = 0
    paid_wins: int = 0
    paid_net_pnl_usdc: float = 0.0
    blockers: list[str] = field(default_factory=list)
    latest_event_type: str = ""
    latest_event_at_ms: int = 0
    latest_event_reason: str = ""

    @property
    def fee_net_ev_per_opportunity(self) -> float | None:
        if not self.evidence_count:
            return None
        return self.fee_net_total / self.evidence_count


@dataclass(frozen=True)
class PromotionRuntimeHealth:
    tables: Mapping[str, bool]
    window_minutes: int = 90

    @property
    def healthy(self) -> bool:
        return all(self.tables.values())


@dataclass
class ProfileWindowEvidence:
    """One bounded, terminal-only W6A profile evidence window."""

    count: int = 0
    evaluable: int = 0
    tp_first: int = 0
    sl_first: int = 0
    no_fill: int = 0
    net_pnl_total_bp: float = 0.0
    net_pnl_count: int = 0

    @property
    def ev_bp(self) -> float | None:
        if not self.net_pnl_count:
            return None
        return self.net_pnl_total_bp / self.net_pnl_count


@dataclass
class W6AProfileSelectorView:
    """Read-only v1.4.65 profile-selector state for a W6A selector key."""

    selector_key: str
    winner_profile_id: str = ""
    winner_profile_hash: str = ""
    market_state: str = ""
    generation: int = 0
    state: str = "SHADOW"
    notional_cap_usdc: float | None = None
    expires_at_ms: int = 0
    profile_plan_hash: str = ""
    blockers: list[str] = field(default_factory=list)
    latest_event_type: str = ""
    latest_event_at_ms: int = 0
    latest_event_reason: str = ""
    profiles: dict[tuple[str, str, str], dict[int, ProfileWindowEvidence]] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class W6AProfileSelectorHealth:
    tables: Mapping[str, bool]

    @property
    def healthy(self) -> bool:
        return all(self.tables.values())


@dataclass
class V1469ArmObservationView:
    """One exact execution-profile arm in the rolling monitor window."""

    arm_key: str
    lane_code: str
    side: str
    regime: str
    profile_id: str
    evidence: int = 0
    pending: int = 0
    terminal: int = 0
    dropped: int = 0
    evaluable: int = 0
    reward_net_bp: float = 0.0
    tp_first: int = 0
    sl_first: int = 0
    no_fill: int = 0
    last_evidence_at_ms: int = 0
    arbiter_eligible: bool = False
    arbiter_blockers: tuple[str, ...] = ()
    lease_phase: str = "NONE"
    lease_status: str = "NONE"
    lease_expires_at_ms: int = 0
    notional_cap_usdc: float = 0.0

    @property
    def ev_bp(self) -> float | None:
        if not self.evaluable:
            return None
        return self.reward_net_bp / self.evaluable


@dataclass
class V1469LaneObservationView:
    """Rolling compact observation-only metrics for one legacy lane code."""

    lane_code: str
    matched: int = 0
    selected: int = 0
    suppressed: int = 0
    safe: int = 0
    hard_blocked: int = 0
    data_blocked: int = 0
    not_evaluated: int = 0
    evaluable: int = 0
    evaluable_reward_net_bp: float = 0.0
    last_observed_at_ms: int = 0
    suppressed_by: Counter[str] = field(default_factory=Counter)
    arms: list[V1469ArmObservationView] = field(default_factory=list)

    @property
    def ev_bp(self) -> float | None:
        if not self.evaluable:
            return None
        return self.evaluable_reward_net_bp / self.evaluable


@dataclass(frozen=True)
class V1469ObservationHealth:
    available: bool
    window_minutes: int = 90
    opportunities: int = 0
    complete_opportunities: int = 0
    last_observed_at_ms: int = 0
    durable_rows: int = 0
    ledger_scope_complete: bool = False
    trusted_paired_rows: int = 0
    mapping_issues: int = 0
    arbiter_state: str = "UNAVAILABLE"
    arbiter_winner_arm_key: str = ""
    arbiter_winner_lane: str = ""
    arbiter_winner_profile: str = ""
    arbiter_lease_action: str = "NONE"
    arbiter_blockers: tuple[str, ...] = ()


@dataclass
class _Opportunity:
    key: str
    lane_code: str
    last_at_ms: int = 0
    outcome_row: dict[str, Any] | None = None
    outcome_source: str = ""
    identity_values: dict[str, set[str]] = field(
        default_factory=lambda: {name: set() for name in _IDENTITY_FIELDS}
    )
    cohort_expected: bool = False
    authoritative_terminal_signatures: set[tuple[object, ...]] = field(
        default_factory=set
    )
    terminal_conflict: bool = False
    live_reopen_breaches: int = 0
    counts_as_captured: bool = True
    lane_codes: set[str] = field(default_factory=set)
    drop_reasons: Counter[str] = field(default_factory=Counter)


def _registry_item(item: object) -> dict[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    if callable(getattr(item, "to_payload", None)):
        return dict(item.to_payload())
    return {
        key: getattr(item, key)
        for key in ("lane_code", "intended_mode", "mode", "family", "side")
        if hasattr(item, key)
    }


def normalize_lane_registry(registry: object | None = None) -> tuple[dict[str, Any], ...]:
    """Return exactly the frozen 27 lanes, optionally overlaying known rows."""
    rows = {str(item["lane_code"]): dict(item) for item in LEGACY_LANE_REGISTRY}
    source: Iterable[Any]
    if registry is None:
        source = ()
    elif hasattr(registry, "LEGACY_LANE_REGISTRY"):
        source = getattr(registry, "LEGACY_LANE_REGISTRY")
    elif hasattr(registry, "LANE_REGISTRY"):
        source = getattr(registry, "LANE_REGISTRY")
    elif hasattr(registry, "LANES"):
        source = getattr(registry, "LANES")
    elif isinstance(registry, Mapping):
        source = (
            dict(value, lane_code=key)
            if isinstance(value, Mapping)
            else {"lane_code": key}
            for key, value in registry.items()
        )
    elif isinstance(registry, Iterable) and not isinstance(registry, (str, bytes)):
        source = registry
    else:
        source = ()
    for item in source:
        value = _registry_item(item)
        code = str(value.get("lane_code") or value.get("code") or "").strip().upper()
        if code not in rows:  # The monitor is intentionally fixed to 27 lanes.
            continue
        rows[code].update(value)
        rows[code]["lane_code"] = code
    for row in rows.values():
        mode = row.get("intended_mode") or row.get("mode") or "SHADOW_ONLY"
        row["intended_mode"] = str(getattr(mode, "value", mode))
    return tuple(rows[code] for code in _LANE_CODES)


def _json(value: object) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _row_map(row: object) -> dict[str, Any]:
    try:
        return dict(row)  # sqlite3.Row is not registered as a Mapping.
    except (TypeError, ValueError):
        return {}


def _nested(row: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(row)
    for key, value in row.items():
        if key.endswith("_json") or key in {"details", "payload"}:
            merged.update({k: v for k, v in _json(value).items() if k not in merged})
    return merged


def _lane(row: Mapping[str, Any]) -> str:
    data = _nested(row)
    for key in ("legacy_lane_code", "lane_code", "candidate_lane", "shadow_lane"):
        value = data.get(key)
        if value:
            return str(value).strip().upper()
    for value in data.values():
        if isinstance(value, Mapping):
            found = _lane(value)
            if found:
                return found
    return ""


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _positive_int(value: object) -> int:
    number = _number(value)
    return max(0, int(number)) if number is not None else 0


def _flag(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "complete", "ok"}:
        return True
    if text in {"0", "false", "no", "incomplete", "data_incomplete", "invalid"}:
        return False
    return None


def _time_ms(row: Mapping[str, Any]) -> int:
    for key in (
        "recorded_at_ms", "event_time_ms", "observed_at_ms", "decision_at_ms",
        "completed_at_ms", "updated_at_ms", "armed_at_ms", "start_ms",
        "reconciled_at_ms",
    ):
        value = _number(row.get(key))
        if value is not None:
            return max(0, int(value))
    return 0


def _opportunity_id(row: Mapping[str, Any]) -> str:
    # v1.4.63 uses a durable id across runs.  Legacy shadow opportunity/sample
    # ids are variant-level identifiers and must never split that denominator.
    for key in (
        "v1462_opportunity_id",
        "opportunity_id",
        "strict_sample_id",
        "sample_id",
    ):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _durable_opportunity_id(row: Mapping[str, Any]) -> str:
    value = _nested(row).get("v1462_opportunity_id")
    return "" if value in (None, "") else str(value)


def _sample_id(row: Mapping[str, Any]) -> str:
    value = _nested(row).get("sample_id")
    return "" if value in (None, "") else str(value)


def _drop_reason(row: Mapping[str, Any]) -> str:
    value = _nested(row).get("drop_reason")
    reason = str(value or "UNKNOWN").strip().lower()
    return reason or "unknown"


def _is_legacy_reconciliation_drop(row: Mapping[str, Any]) -> bool:
    """Return whether a post-cutoff drop only tombstones pre-v1.4.63 state.

    Startup reconciliation deliberately records stale samples as
    ``registry_version_mismatch``.  Those audit rows can be written after the
    snapshot migration even though the samples themselves predate the forward
    cohort.  They remain in the durable ledger, but must not block current
    Lane Monitor readiness unless they carry a v1.4.63 durable opportunity id.
    """

    return bool(
        not _durable_opportunity_id(row)
        and _drop_reason(row) == "data_incomplete:registry_version_mismatch"
    )


def _is_out_of_scope_shadow_lane(lane_code: str) -> bool:
    """Identify explicit synthetic lanes outside the frozen legacy registry."""

    return str(lane_code or "").strip().upper().startswith("SH_")


def _outcome(row: Mapping[str, Any]) -> str:
    if _flag(row.get("ambiguous_touch")) is True:
        return "ambiguous_both"
    value = str(
        row.get("shadow_outcome")
        or row.get("outcome")
        or row.get("exit_reason")
        or ""
    ).strip().lower()
    aliases = {
        "take_profit": "tp1_first", "tp_hit": "tp1_first",
        "stop_loss": "sl_first", "sl_hit": "sl_first",
        "expired": "no_fill", "entry_expired": "no_fill",
        "timeout": "max_hold", "max_hold_expired": "max_hold",
    }
    value = aliases.get(value, value)
    fill_status = str(row.get("fill_status") or "").strip().lower()
    if not value and fill_status in {"no_fill", "not_filled", "expired"}:
        return "no_fill"
    return value


def _quality(row: Mapping[str, Any], source: str) -> tuple[bool, str]:
    outcome = _outcome(row)
    if outcome == "ambiguous_both":
        return False, "AMBIGUOUS"
    if outcome and outcome not in _KNOWN_TERMINAL_OUTCOMES:
        return False, f"UNKNOWN_OUTCOME:{outcome[:80]}"
    raw_quality = row.get("data_quality")
    quality_payload = _json(raw_quality)
    quality_flag = (
        _flag(quality_payload.get("complete"))
        if quality_payload
        else _flag(raw_quality)
    )
    explicit_flag = _flag(row.get("data_complete")) if "data_complete" in row else None
    if source == "event" and explicit_flag is not True:
        return False, str(row.get("terminal_reason") or "DATA_COMPLETE_NOT_TRUE")
    if explicit_flag is False or quality_flag is False:
        return False, str(row.get("invalid_reason") or row.get("terminal_reason") or "DATA_INCOMPLETE")
    if source == "adaptive" and quality_flag is not True and explicit_flag is not True:
        return False, str(row.get("invalid_reason") or raw_quality or "DATA_INCOMPLETE")
    if not outcome:
        return False, "OUTCOME_MISSING"
    pnl = _pnl(row, outcome)
    if pnl is None:
        return False, "PNL_MISSING"
    return True, ""


def _pnl(row: Mapping[str, Any], outcome: str) -> float | None:
    if outcome == "no_fill":
        return 0.0
    for key in (
        "paper_pnl_usdc_after_fee", "net_pnl_usdc", "paper_net_pnl_usdc",
        "expected_value_usdc", "ev_usdc",
    ):
        value = _number(row.get(key))
        if value is not None:
            return value
    return None


def _outcome_score(row: Mapping[str, Any], source: str) -> tuple[int, int, int, int]:
    complete, _ = _quality(row, source)
    return (
        int(complete),
        int(not bool(_flag(row.get("diagnostic_only")))),
        int(str(row.get("fill_model") or "limit_touch") == "limit_touch"),
        _time_ms(row),
    )


def _is_authoritative_terminal(row: Mapping[str, Any]) -> bool:
    """Only the strict v1.4.63 evaluator may feed readiness statistics."""
    return bool(
        _flag(row.get("evidence_evaluator_eligible")) is True
        and _flag(row.get("diagnostic_only")) is not True
        and str(row.get("fill_model") or "").strip().lower() == "limit_touch"
    )


def _terminal_signature(row: Mapping[str, Any], source: str) -> tuple[object, ...]:
    outcome = _outcome(row)
    pnl = _pnl(row, outcome)
    complete, reason = _quality(row, source)
    return (
        outcome,
        None if pnl is None else round(pnl, 12),
        complete,
        reason if not complete else "",
    )


def _identity_value(row: Mapping[str, Any], field_name: str) -> str:
    aliases = {
        "registry_hash": ("registry_hash", "v1462_registry_hash"),
        "policy_hash": ("policy_hash", "v1462_policy_hash"),
        "resolved_profile_hash": (
            "resolved_profile_hash",
            "resolved_effective_profile_hash",
            "action_profile_hash",
            "profile_hash",
        ),
        "state": ("state", "market_state"),
        "effective_side": ("effective_side", "side"),
    }[field_name]

    def find(value: Mapping[str, Any]) -> str:
        for key in aliases:
            item = value.get(key)
            if item not in (None, "") and not isinstance(item, (Mapping, list, tuple)):
                return str(item).strip()
        for item in value.values():
            if isinstance(item, Mapping):
                found = find(item)
                if found:
                    return found
        return ""

    return find(_nested(row))


def _record_identity(item: _Opportunity, row: Mapping[str, Any]) -> None:
    for name in _IDENTITY_FIELDS:
        value = _identity_value(row, name)
        if value:
            item.identity_values[name].add(
                value.upper() if name == "effective_side" else value
            )


def _identity_component(item: _Opportunity, name: str) -> str:
    values = item.identity_values[name]
    if not values:
        return "MISSING"
    if len(values) == 1:
        return next(iter(values))
    return "MIXED[" + "|".join(sorted(values)) + "]"


def _cohort_key(item: _Opportunity, lane_code: str | None = None) -> CohortKey:
    return CohortKey(
        lane_code=lane_code or item.lane_code,
        state=_identity_component(item, "state"),
        effective_side=_identity_component(item, "effective_side"),
        resolved_profile_hash=_identity_component(item, "resolved_profile_hash"),
        registry_hash=_identity_component(item, "registry_hash"),
        policy_hash=_identity_component(item, "policy_hash"),
    )


def _utc_date(at_ms: int) -> str:
    if not at_ms:
        return ""
    try:
        return datetime.fromtimestamp(at_ms / 1000.0, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _has_reject_lineage(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return bool(value)
    text = str(value).strip()
    if not text or text.lower() in {"none", "null"}:
        return False
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return text not in {"()"}
    if isinstance(parsed, (list, tuple, dict)):
        return bool(parsed)
    return bool(parsed)


def _is_exact_current_cnl_safe_control(row: Mapping[str, Any]) -> bool:
    data = _nested(row)
    return bool(
        _lane(data) == "CNL-WPR-L"
        and str(data.get("matrix_rule_id") or "") in _CNL_CONTROL_RULE_IDS
        and str(data.get("safe_lineage_kind") or "") == CNL_SAFE_LINEAGE_KIND
        and _flag(data.get("registry_identity_valid")) is True
        and str(data.get("registry_lane_code") or "").strip().upper()
        == "CNL-WPR-L"
        and _identity_value(data, "registry_hash") == V1462_REGISTRY_HASH
        and _identity_value(data, "policy_hash") == V1462_POLICY_HASH
        and _flag(data.get("raw_accepted")) is False
        and _flag(data.get("reject_reopen_detected")) is not True
        and not _has_reject_lineage(data.get("reject_lineage"))
    )


def _is_reject_reopen_live_breach(row: Mapping[str, Any]) -> bool:
    data = _nested(row)
    route = str(
        data.get("final_route")
        or data.get("mode")
        or data.get("route")
        or ""
    ).strip().upper()
    is_live = route == "LIVE" or _flag(data.get("permits_order")) is True
    has_lineage = _has_reject_lineage(data.get("reject_lineage"))
    if is_live and _is_exact_current_cnl_safe_control(data):
        return False
    return bool(
        is_live
        and (
            _flag(data.get("reject_reopen_detected")) is True
            or has_lineage
            or _flag(data.get("raw_accepted")) is False
        )
    )


async def _read_rows(
    db: Any,
    table: str,
    *,
    query: str | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    if db is None or not hasattr(db, "fetchall"):
        return [], False
    try:
        rows = await db.fetchall(query or _TABLE_QUERIES[table])
    except Exception:  # old schemas and read-only replicas are supported states
        return [], False
    return [_row_map(row) for row in rows], True


def _merge_opportunity(
    opportunities: dict[str, _Opportunity],
    *,
    key: str,
    lane_code: str,
    at_ms: int,
    outcome_row: dict[str, Any] | None = None,
    outcome_source: str = "",
    metadata_row: Mapping[str, Any] | None = None,
    cohort_expected: bool = False,
) -> None:
    if lane_code not in _LANE_CODES:
        return
    item = opportunities.get(key)
    if item is None:
        item = _Opportunity(key, lane_code)
        opportunities[key] = item
    item.lane_codes.add(lane_code)
    item.last_at_ms = max(item.last_at_ms, at_ms)
    item.cohort_expected = item.cohort_expected or cohort_expected
    for row in (metadata_row, outcome_row):
        if row is not None and _nested(row).get("v1462_opportunity_id") not in (None, ""):
            item.cohort_expected = True
    if metadata_row is not None:
        _record_identity(item, metadata_row)
    if outcome_row is not None and (
        _is_authoritative_terminal(outcome_row)
    ):
        item.authoritative_terminal_signatures.add(
            _terminal_signature(outcome_row, outcome_source)
        )
        item.terminal_conflict = len(item.authoritative_terminal_signatures) > 1
        if (
            item.outcome_row is None
            or _outcome_score(outcome_row, outcome_source)
            > _outcome_score(item.outcome_row, item.outcome_source)
        ):
            item.outcome_row = outcome_row
            item.outcome_source = outcome_source


def _cohort_for(
    evidence: LaneEvidence,
    sample: _Opportunity,
    unavailable: set[str],
    *,
    lane_code: str | None = None,
) -> CohortEvidence:
    key = _cohort_key(sample, lane_code)
    cohort = evidence.cohorts.get(key)
    if cohort is None:
        cohort = CohortEvidence(key=key)
        cohort.unavailable_sources.update(unavailable & _READINESS_SOURCES)
        cohort.global_blockers.update(evidence.global_blockers)
        evidence.cohorts[key] = cohort
    return cohort


def _add_sample_to_cohort(cohort: CohortEvidence, sample: _Opportunity) -> None:
    if sample.counts_as_captured:
        cohort.captured += 1
    cohort.last_at_ms = max(cohort.last_at_ms, sample.last_at_ms)
    missing = sum(not sample.identity_values[name] for name in _IDENTITY_FIELDS)
    mixed = sum(len(sample.identity_values[name]) > 1 for name in _IDENTITY_FIELDS)
    if missing:
        cohort.identity_missing += 1
    if mixed:
        cohort.identity_conflicts += 1
    cohort.live_reopen_breaches += sample.live_reopen_breaches

    if not sample.counts_as_captured:
        return
    if len(sample.lane_codes) > 1:
        cohort.lane_conflicts += 1
        cohort.invalid += 1
        cohort.invalid_reasons["DURABLE_ID_LANE_CONFLICT"] += 1
        return
    if sample.terminal_conflict:
        cohort.terminal_conflicts += 1
        cohort.invalid += 1
        cohort.invalid_reasons["AUTHORITATIVE_TERMINAL_CONFLICT"] += 1
        return
    # A scheduler/cap/cooldown drop is a terminal collection status, not a
    # market no-fill.  A later authoritative outcome for the same durable id
    # supersedes it (for example, a cooldown duplicate of an active sample).
    if sample.outcome_row is None and sample.drop_reasons:
        cohort.dropped += 1
        cohort.invalid += 1
        reasons = ",".join(sorted(sample.drop_reasons))
        cohort.invalid_reasons[f"COLLECTION_DROPPED:{reasons}"] += 1
        return
    if sample.outcome_row is None:
        cohort.pending += 1
        return
    row = sample.outcome_row
    outcome = _outcome(row)
    if outcome:
        cohort.outcomes[outcome] += 1
    complete, reason = _quality(row, sample.outcome_source)
    if not complete:
        cohort.invalid += 1
        cohort.invalid_reasons[reason] += 1
        if reason == "AMBIGUOUS":
            cohort.ambiguous += 1
        else:
            cohort.incomplete += 1
        return
    cohort.complete += 1
    if outcome in _FILLED_OUTCOMES:
        cohort.evaluable += 1
        date = _utc_date(sample.last_at_ms)
        if date:
            cohort.utc_dates.add(date)
    pnl = _pnl(row, outcome)
    if pnl is not None:
        cohort.ev_total += pnl
        cohort.ev_count += 1


def _roll_up_cohorts(evidence: LaneEvidence) -> None:
    for cohort in evidence.cohorts.values():
        evidence.captured += cohort.captured
        evidence.complete += cohort.complete
        evidence.evaluable += cohort.evaluable
        evidence.invalid += cohort.invalid
        evidence.incomplete += cohort.incomplete
        evidence.ambiguous += cohort.ambiguous
        evidence.dropped += cohort.dropped
        evidence.pending += cohort.pending
        evidence.outcomes.update(cohort.outcomes)
        evidence.ev_total += cohort.ev_total
        evidence.ev_count += cohort.ev_count
        evidence.last_at_ms = max(evidence.last_at_ms, cohort.last_at_ms)
        evidence.invalid_reasons.update(cohort.invalid_reasons)
        evidence.utc_dates.update(cohort.utc_dates)
        evidence.identity_missing += cohort.identity_missing
        evidence.identity_conflicts += cohort.identity_conflicts
        evidence.terminal_conflicts += cohort.terminal_conflicts
        evidence.lane_conflicts += cohort.lane_conflicts
        evidence.live_reopen_breaches += cohort.live_reopen_breaches
        for name, value in (
            ("state", cohort.key.state),
            ("effective_side", cohort.key.effective_side),
            ("resolved_profile_hash", cohort.key.resolved_profile_hash),
            ("registry_hash", cohort.key.registry_hash),
            ("policy_hash", cohort.key.policy_hash),
        ):
            if not value.startswith(("MISSING", "MIXED")):
                evidence.identity_values[name].add(value)


async def collect_lane_evidence(
    db: Any,
    registry: object | None = None,
) -> tuple[dict[str, LaneEvidence], dict[str, bool]]:
    """Collect durable sources and isolate the current cohort from legacy history."""
    lanes = {
        str(item["lane_code"]): LaneEvidence(
            str(item["lane_code"]),
            str(item.get("intended_mode") or "SHADOW_ONLY"),
        )
        for item in normalize_lane_registry(registry)
    }
    snapshot_rows, has_snapshot_meta = await _read_rows(
        db, "v1463_lane_monitor_snapshot_meta"
    )
    snapshot_valid = False
    snapshot_event_id = 0
    snapshot_at_ms = 0
    if has_snapshot_meta and len(snapshot_rows) == 1:
        event_id_value = _number(snapshot_rows[0].get("snapshot_max_event_id"))
        at_ms_value = _number(snapshot_rows[0].get("snapshot_at_ms"))
        snapshot_valid = bool(
            event_id_value is not None
            and at_ms_value is not None
            and event_id_value >= 0
            and at_ms_value >= 0
        )
        if snapshot_valid:
            snapshot_event_id = int(event_id_value)
            snapshot_at_ms = int(at_ms_value)

    adaptive_opps, has_opps = await _read_rows(db, "adaptive_opportunities")
    evaluations, has_evals = await _read_rows(db, "shadow_evaluations")
    runs, has_runs = await _read_rows(db, "mainnet_runs")
    legacy_rows, has_legacy_summary = await _read_rows(
        db, "v1462_lane_monitor_legacy_summary"
    )
    events, has_events = await _read_rows(
        db,
        "mainnet_run_events",
        query=_event_query(snapshot_event_id),
    )
    availability = {
        "adaptive_opportunities": has_opps,
        "shadow_evaluations": has_evals,
        "mainnet_runs": has_runs,
        "v1462_lane_monitor_legacy_summary": has_legacy_summary,
        "v1463_lane_monitor_snapshot_meta": snapshot_valid,
        "mainnet_run_events": has_events,
    }
    unavailable = {name for name, available in availability.items() if not available}
    for evidence in lanes.values():
        evidence.unavailable_sources.update(unavailable)
        if not snapshot_valid:
            evidence.global_blockers.add("snapshot_meta_missing_or_invalid")

    for raw in legacy_rows:
        row = _nested(raw)
        code = _lane(row)
        if code not in lanes:
            continue
        evidence = lanes[code]
        evidence.legacy_shadow_outcomes = max(
            evidence.legacy_shadow_outcomes,
            _positive_int(row.get("outcome_opportunities")),
        )
        evidence.legacy_last_at_ms = max(
            evidence.legacy_last_at_ms,
            _positive_int(row.get("last_outcome_at_ms")),
        )

    samples: dict[str, _Opportunity] = {}
    lane_by_adaptive_id: dict[str, str] = {}
    for ordinal, raw in enumerate(adaptive_opps):
        row = _nested(raw)
        code = _lane(row)
        opportunity_id = _opportunity_id(row)
        key = f"opp:{opportunity_id}" if opportunity_id else f"adaptive:{row.get('session_id')}:{ordinal}"
        _merge_opportunity(
            samples, key=key, lane_code=code, at_ms=_time_ms(row),
            metadata_row=row,
            cohort_expected=(
                _time_ms(row) > snapshot_at_ms
                if snapshot_valid
                else bool(_nested(row).get("v1462_opportunity_id"))
            ),
        )
        if opportunity_id and code:
            lane_by_adaptive_id[opportunity_id] = code

    for ordinal, raw in enumerate(evaluations):
        row = _nested(raw)
        opportunity_id = _opportunity_id(row)
        code = _lane(row) or lane_by_adaptive_id.get(opportunity_id, "")
        key = f"opp:{opportunity_id}" if opportunity_id else f"evaluation:{row.get('session_id')}:{ordinal}"
        _merge_opportunity(
            samples,
            key=key,
            lane_code=code,
            at_ms=_time_ms(row),
            outcome_row=row,
            outcome_source="adaptive",
            metadata_row=row,
            cohort_expected=(
                _time_ms(row) > snapshot_at_ms
                if snapshot_valid
                else bool(_nested(row).get("v1462_opportunity_id"))
            ),
        )

    ticket_rows: list[tuple[dict[str, Any], str]] = []
    drop_rows: list[dict[str, Any]] = []
    started_by_sample: dict[str, list[tuple[str, str, bool]]] = {}
    event_rows: list[dict[str, Any]] = []
    for raw in events:
        row = _nested(raw)
        # Event details can be tens of kilobytes.  Flatten them once for both
        # reduction passes, then discard the raw envelope so helper calls do
        # not repeatedly decode the same JSON.
        row.pop("details_json", None)
        row.pop("details", None)
        event_rows.append(row)
    for ordinal, row in enumerate(event_rows):
        if _flag(row.get("v1465_profile_evidence")) is True:
            continue
        if str(row.get("event_type") or "") != "entry_codex_v1_shadow_sample_started":
            continue
        sample_id = _sample_id(row)
        if not sample_id:
            continue
        opportunity_id = _opportunity_id(row)
        key = (
            f"opp:{opportunity_id}"
            if opportunity_id
            else f"event:{row.get('run_id')}:{row.get('id') or ordinal}"
        )
        started_by_sample.setdefault(sample_id, []).append(
            (key, _lane(row), _flag(row.get("diagnostic_only")) is True)
        )
    breach_keys: set[tuple[str, str, str]] = set()
    orphan_breaches_by_lane: Counter[str] = Counter()
    unattributed_breaches = 0
    admission_rows_by_key: dict[str, list[dict[str, Any]]] = {}
    for ordinal, row in enumerate(event_rows):
        if _flag(row.get("v1465_profile_evidence")) is True:
            continue
        event_type = str(row.get("event_type") or "")
        if event_type == "entry_codex_v1462_admission":
            code = _lane(row)
            opportunity_id = _opportunity_id(row)
            if _is_reject_reopen_live_breach(row):
                if code in lanes and opportunity_id:
                    breach_key = (
                        str(row.get("run_id") or ""),
                        opportunity_id,
                        code,
                    )
                    if breach_key not in breach_keys:
                        breach_keys.add(breach_key)
                elif code in lanes:
                    orphan_breaches_by_lane[code] += 1
                else:
                    unattributed_breaches += 1
            if code in lanes and opportunity_id:
                key = f"opp:{opportunity_id}"
                admission_rows_by_key.setdefault(key, []).append(row)
            # Admission rows are authoritative audits, never opportunities.
            continue
        if event_type == "entry_codex_v1_shadow_sample_dropped":
            drop_rows.append(row)
            continue
        if event_type not in {
            "entry_codex_v1462_shadow_opportunity",
            "entry_codex_v1_shadow_sample_started",
            "entry_codex_v1_shadow_outcome",
        }:
            continue
        code = _lane(row)
        if code not in lanes:
            continue
        if event_type == "entry_codex_v1462_shadow_opportunity":
            ticket_rows.append((row, f"ticket:{row.get('id') or ordinal}"))
            continue
        opportunity_id = _opportunity_id(row)
        key = f"opp:{opportunity_id}" if opportunity_id else f"event:{row.get('run_id')}:{row.get('id') or ordinal}"
        _merge_opportunity(
            samples,
            key=key,
            lane_code=code,
            at_ms=_time_ms(row),
            outcome_row=row if event_type == "entry_codex_v1_shadow_outcome" else None,
            outcome_source="event" if event_type == "entry_codex_v1_shadow_outcome" else "",
            metadata_row=row,
            cohort_expected=snapshot_valid,
        )

    for code, count in orphan_breaches_by_lane.items():
        lanes[code].live_reopen_breaches += count
        lanes[code].global_blockers.add(
            f"orphan_reject_reopen_live_breach={count} (missing opportunity id)"
        )
    if unattributed_breaches:
        blocker = (
            f"unattributed_reject_reopen_live_breach={unattributed_breaches} "
            "(missing lane and opportunity id)"
        )
        for evidence in lanes.values():
            evidence.global_blockers.add(blocker)

    # A v1.4.63 ticket is the durable fallback when mapping/sampling could not
    # start.  It may merge only with the same durable opportunity id; another
    # ticket in the same run/lane is a distinct collection obligation.
    for row, fallback_key in ticket_rows:
        code = _lane(row)
        opportunity_id = _opportunity_id(row)
        key = f"opp:{opportunity_id}" if opportunity_id else fallback_key
        _merge_opportunity(
            samples, key=key, lane_code=code, at_ms=_time_ms(row),
            metadata_row=row,
            cohort_expected=True,
        )

    orphan_drops_by_lane: Counter[str] = Counter()
    unattributed_drops = 0
    for row in drop_rows:
        if _flag(row.get("diagnostic_only")) is True:
            continue
        if _is_legacy_reconciliation_drop(row):
            continue

        key = ""
        inferred_code = ""
        diagnostic_started = False
        durable_id = _durable_opportunity_id(row)
        if durable_id:
            key = f"opp:{durable_id}"
        else:
            started = started_by_sample.get(_sample_id(row), [])
            started_keys = {value[0] for value in started}
            if len(started_keys) == 1:
                key = next(iter(started_keys))
                inferred_codes = {value[1] for value in started if value[1]}
                if len(inferred_codes) == 1:
                    inferred_code = next(iter(inferred_codes))
                diagnostic_started = bool(started) and all(value[2] for value in started)
            if not key:
                generic_id = str(row.get("opportunity_id") or "").strip()
                generic_key = f"opp:{generic_id}" if generic_id else ""
                if generic_key in samples:
                    key = generic_key

        if diagnostic_started:
            continue
        code = _lane(row) or inferred_code
        # Synthetic SH_* lanes are explicitly outside this 27-lane legacy
        # monitor.  Their durable audit rows remain queryable, but they cannot
        # make every unrelated legacy lane DATA_BLOCKED.
        if code not in lanes and _is_out_of_scope_shadow_lane(code):
            continue
        sample = samples.get(key) if key else None
        if sample is not None and code not in lanes:
            code = sample.lane_code
        if key and code in lanes:
            if sample is None:
                _merge_opportunity(
                    samples,
                    key=key,
                    lane_code=code,
                    at_ms=_time_ms(row),
                    cohort_expected=True,
                )
                sample = samples.get(key)
            if sample is not None:
                sample.lane_codes.add(code)
                sample.last_at_ms = max(sample.last_at_ms, _time_ms(row))
                sample.cohort_expected = True
                sample.drop_reasons[_drop_reason(row)] += 1
                continue
        if code in lanes:
            orphan_drops_by_lane[code] += 1
            lanes[code].last_at_ms = max(lanes[code].last_at_ms, _time_ms(row))
        else:
            unattributed_drops += 1

    for code, count in orphan_drops_by_lane.items():
        lanes[code].dropped += count
        lanes[code].global_blockers.add(
            f"orphan_shadow_sample_drop={count} (cannot join durable opportunity)"
        )
    if unattributed_drops:
        blocker = (
            f"unattributed_shadow_sample_drop={unattributed_drops} "
            "(cannot join lane or durable opportunity)"
        )
        for evidence in lanes.values():
            evidence.global_blockers.add(blocker)

    # Audit rows can precede or follow their shadow/sample rows in the ledger.
    # Join after all opportunities are reduced so event ordering cannot decide
    # whether the strict-admission policy identity is visible.
    for key, admission_rows in admission_rows_by_key.items():
        sample = samples.get(key)
        if sample is None:
            first = admission_rows[0]
            code = _lane(first)
            if code not in lanes or not any(
                _is_reject_reopen_live_breach(row) for row in admission_rows
            ):
                continue
            sample = _Opportunity(
                key=key,
                lane_code=code,
                cohort_expected=True,
                counts_as_captured=False,
            )
            samples[key] = sample
        for row in admission_rows:
            _record_identity(sample, row)
            breach_key = (
                str(row.get("run_id") or ""),
                _opportunity_id(row),
                _lane(row),
            )
            if breach_key in breach_keys:
                sample.live_reopen_breaches += 1
                breach_keys.discard(breach_key)

    for sample in samples.values():
        evidence = lanes[sample.lane_code]
        if not sample.cohort_expected:
            evidence.legacy_adaptive += 1
            evidence.legacy_last_at_ms = max(
                evidence.legacy_last_at_ms,
                sample.last_at_ms,
            )
            continue
        target_codes = sample.lane_codes or {sample.lane_code}
        for target_code in sorted(target_codes):
            target_evidence = lanes[target_code]
            cohort = _cohort_for(
                target_evidence,
                sample,
                unavailable,
                lane_code=target_code,
            )
            _add_sample_to_cohort(cohort, sample)

    for evidence in lanes.values():
        _roll_up_cohorts(evidence)

    seen_runs: set[str] = set()
    for ordinal, raw in enumerate(runs):
        row = _nested(raw)
        code = _lane(row)
        if code not in lanes or str(row.get("status") or "").upper() != "COMPLETED":
            continue
        run_id = str(row.get("run_id") or f"row:{ordinal}")
        if run_id in seen_runs:
            continue
        realized = _number(row.get("realized_pnl_usdc"))
        net = _number(row.get("net_pnl_usdc"))
        if net is None and realized is not None:
            net = realized - (_number(row.get("commission_usdc")) or 0.0)
        if net is None:
            continue
        seen_runs.add(run_id)
        evidence = lanes[code]
        evidence.last_at_ms = max(evidence.last_at_ms, _time_ms(row))
        evidence.paid_count += 1
        evidence.paid_net += net
        if net > 0:
            evidence.paid_wins += 1
        elif net < 0:
            evidence.paid_losses += 1

    return lanes, availability


_PROMOTION_IDENTITY_FIELDS = (
    "environment",
    "symbol",
    "lane_code",
    "market_state",
    "effective_side",
    "strategy",
    "resolved_profile_hash",
    "registry_hash",
    "admission_policy_hash",
)
_PROMOTION_TP_OUTCOMES = {"tp1_first", "tp_first", "tp"}
_PROMOTION_SL_OUTCOMES = {"sl_first", "sl"}
_PROMOTION_EVALUABLE_OUTCOMES = {
    *_PROMOTION_TP_OUTCOMES,
    *_PROMOTION_SL_OUTCOMES,
    "max_hold",
}


def _promotion_identity(row: Mapping[str, Any]) -> tuple[str, ...]:
    data = _nested(row)
    return tuple(str(data.get(name) or "").strip() for name in _PROMOTION_IDENTITY_FIELDS)


def _promotion_shadow_key(identity: tuple[str, ...]) -> str:
    return "shadow|" + "|".join(identity)


def _promotion_state(row: Mapping[str, Any], now_ms: int) -> tuple[str, list[str]]:
    data = _nested(row)
    status = str(data.get("status") or "").strip().upper()
    phase = str(data.get("phase") or "").strip().upper()
    blockers: list[str] = []
    expires_at_ms = _positive_int(data.get("expires_at_ms"))
    if status == "ACTIVE" and expires_at_ms and now_ms >= expires_at_ms:
        blockers.append("lease_expired_not_reconciled")
        return "EXPIRED", blockers
    if status == "ACTIVE" and phase == "PROBATION":
        return "PROBATION", blockers
    if status == "ACTIVE" and phase == "CONTROL":
        return "LIVE", blockers
    if status == "EXPIRED":
        return "EXPIRED", blockers
    if status in {"DEMOTED", "REVOKED", "HALTED"}:
        reason = str(data.get("demotion_reason") or status.lower()).strip()
        if reason:
            blockers.append(reason)
        return "DEMOTED", blockers
    blockers.append("lease_status_unknown")
    return "SHADOW", blockers


def _promotion_snapshot_blockers(snapshot: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    if snapshot and _flag(snapshot.get("data_complete")) is False:
        blockers.append("data_incomplete")
    for name in (
        "identity_conflicts",
        "data_conflicts",
        "incomplete",
        "ambiguous",
        "dropped",
        "overdue",
    ):
        count = _positive_int(snapshot.get(name))
        if count:
            blockers.append(f"{name}={count}")
    raw = snapshot.get("blockers", snapshot.get("promotion_blockers"))
    if isinstance(raw, str):
        blockers.extend(item.strip() for item in raw.split(";") if item.strip())
    elif isinstance(raw, Iterable) and not isinstance(raw, (str, bytes, Mapping)):
        blockers.extend(str(item).strip() for item in raw if str(item).strip())
    reason = str(snapshot.get("reason") or "").strip()
    if reason and reason.lower() not in {"accepted", "lease_retained"}:
        blockers.append(reason)
    return blockers


async def collect_promotion_runtime(
    db: Any,
    *,
    now_ms: int,
    window_minutes: int = 90,
) -> tuple[list[PromotionRuntimeView], PromotionRuntimeHealth]:
    """Read v1.4.64 tables without changing v1.4.63 evidence semantics."""

    bounded_minutes = max(1, int(window_minutes))
    cutoff_ms = max(0, now_ms - bounded_minutes * 60 * 1000)
    evidence_rows, has_evidence = await _read_rows(
        db,
        "v1464_promotion_evidence",
        query=(
            "SELECT * FROM v1464_promotion_evidence "
            f"WHERE observed_at_ms >= {cutoff_ms} "
            f"AND observed_at_ms <= {max(0, int(now_ms))} "
            f"AND terminal_at_ms <= {max(0, int(now_ms))} "
            "ORDER BY observed_at_ms DESC, opportunity_id"
        ),
    )
    lease_rows, has_leases = await _read_rows(
        db, "v1464_lane_promotion_leases"
    )
    event_rows, has_events = await _read_rows(
        db, "v1464_lane_promotion_events"
    )
    health = PromotionRuntimeHealth(
        {
            "v1464_promotion_evidence": has_evidence,
            "v1464_lane_promotion_leases": has_leases,
            "v1464_lane_promotion_events": has_events,
        },
        window_minutes=bounded_minutes,
    )

    views: list[PromotionRuntimeView] = []
    views_by_identity: dict[tuple[str, ...], list[PromotionRuntimeView]] = {}
    views_by_cohort_key: dict[str, PromotionRuntimeView] = {}
    for raw in lease_rows:
        row = _nested(raw)
        identity = _promotion_identity(row)
        if not all(identity):
            continue
        state, blockers = _promotion_state(row, now_ms)
        snapshot = _json(row.get("evidence_snapshot_json"))
        view = PromotionRuntimeView(
            cohort_key=str(row.get("cohort_key") or "").strip(),
            lane_code=identity[2],
            market_state=identity[3],
            effective_side=identity[4],
            strategy=identity[5],
            resolved_profile_hash=identity[6],
            registry_hash=identity[7],
            admission_policy_hash=identity[8],
            promotion_policy_hash=str(
                row.get("promotion_policy_hash") or ""
            ).strip(),
            state=state,
            lease_id=str(row.get("lease_id") or "").strip(),
            generation=_positive_int(row.get("generation")),
            notional_cap_usdc=_number(row.get("notional_cap_usdc")),
            expires_at_ms=_positive_int(row.get("expires_at_ms")),
            paid_complete=_positive_int(snapshot.get("paid_complete")),
            paid_wins=_positive_int(snapshot.get("paid_wins")),
            paid_net_pnl_usdc=_number(snapshot.get("paid_net_pnl_usdc"))
            or 0.0,
            blockers=[
                *blockers,
                *_promotion_snapshot_blockers(snapshot),
            ],
        )
        views.append(view)
        views_by_identity.setdefault(identity, []).append(view)
        if view.cohort_key:
            views_by_cohort_key[view.cohort_key] = view

    for raw in evidence_rows:
        row = _nested(raw)
        observed_at_ms = _positive_int(row.get("observed_at_ms"))
        terminal_at_ms = _positive_int(row.get("terminal_at_ms"))
        if (
            observed_at_ms < cutoff_ms
            or observed_at_ms > now_ms
            or terminal_at_ms > now_ms
        ):
            continue
        identity = _promotion_identity(row)
        if not all(identity):
            continue
        targets = views_by_identity.get(identity)
        if not targets:
            view = PromotionRuntimeView(
                cohort_key=_promotion_shadow_key(identity),
                lane_code=identity[2],
                market_state=identity[3],
                effective_side=identity[4],
                strategy=identity[5],
                resolved_profile_hash=identity[6],
                registry_hash=identity[7],
                admission_policy_hash=identity[8],
                state="SHADOW",
                blockers=["no_runtime_lease"],
            )
            views.append(view)
            views_by_identity[identity] = [view]
            views_by_cohort_key[view.cohort_key] = view
            targets = [view]
        diagnostic = _flag(row.get("diagnostic_only")) is True
        complete = _flag(row.get("data_complete")) is True
        ambiguous = (
            _flag(row.get("ambiguous")) is True
            or str(row.get("outcome") or "").lower() == "ambiguous_both"
        )
        outcome = str(row.get("outcome") or "").strip().lower()
        for view in targets:
            if diagnostic:
                if "diagnostic_evidence_present" not in view.blockers:
                    view.blockers.append("diagnostic_evidence_present")
                continue
            view.evidence_count += 1
            if not complete:
                if "incomplete_90m_evidence" not in view.blockers:
                    view.blockers.append("incomplete_90m_evidence")
                continue
            if ambiguous:
                if "ambiguous_90m_evidence" not in view.blockers:
                    view.blockers.append("ambiguous_90m_evidence")
                continue
            if outcome in _PROMOTION_EVALUABLE_OUTCOMES:
                view.evaluable += 1
            if outcome in _PROMOTION_TP_OUTCOMES:
                view.tp_first += 1
            elif outcome in _PROMOTION_SL_OUTCOMES:
                view.sl_first += 1
            elif outcome == "no_fill":
                view.no_fill += 1
            net = _number(row.get("net_pnl_usdc"))
            if net is not None:
                view.fee_net_total += net

    for raw in event_rows:
        row = _nested(raw)
        cohort_key = str(row.get("cohort_key") or "").strip()
        view = views_by_cohort_key.get(cohort_key)
        event_at_ms = _positive_int(row.get("event_time_ms"))
        if view is None or (
            view.latest_event_type and event_at_ms <= view.latest_event_at_ms
        ):
            continue
        payload = _json(row.get("payload_json"))
        details = payload.get("details")
        if not isinstance(details, Mapping):
            details = {}
        view.latest_event_type = str(row.get("event_type") or "").strip()
        view.latest_event_at_ms = event_at_ms
        view.latest_event_reason = str(
            details.get("reason")
            or payload.get("reason")
            or payload.get("demotion_reason")
            or ""
        ).strip()

    for view in views:
        if view.state in {"PROBATION", "LIVE"} and not view.evidence_count:
            view.blockers.append("no_90m_evidence")
        view.blockers = list(dict.fromkeys(item for item in view.blockers if item))
    views.sort(
        key=lambda item: (
            item.lane_code,
            item.market_state,
            item.effective_side,
            item.strategy,
            item.cohort_key,
        )
    )
    return views, health


async def collect_v1469_observation(
    db: Any,
    *,
    now_ms: int,
) -> tuple[dict[str, V1469LaneObservationView], V1469ObservationHealth]:
    """Read the compact 90-minute observation ledger without affecting legacy views."""

    now = max(0, int(now_ms))
    start = max(0, now - 90 * 60 * 1000)
    if db is None or not hasattr(db, "fetchall") or not hasattr(db, "fetchone"):
        return {}, V1469ObservationHealth(False)
    try:
        summary = await V1469ArmObservationRepository(
            db
        ).get_monitor_summary(
            environment="MAINNET",
            symbol="ETHUSDC",
            window_start_ms=start,
            as_of_ms=now,
        )
        if not isinstance(summary, Mapping):
            raise TypeError("v1469 monitor summary must be a mapping")
        lane_rows = summary.get("lanes")
        suppressed_rows = summary.get("suppressed_by")
        opportunity_totals = summary.get("opportunities")
        arm_rows = summary.get("arms", [])
        lease_rows = summary.get("leases", [])
        if not isinstance(lane_rows, list) or not isinstance(
            suppressed_rows, list
        ) or not isinstance(opportunity_totals, Mapping):
            raise TypeError("v1469 monitor summary rows are unavailable")
        views: dict[str, V1469LaneObservationView] = {}
        for raw in lane_rows:
            if not isinstance(raw, Mapping):
                raise TypeError("v1469 lane summary row is invalid")
            code = str(raw.get("lane_code") or "").strip().upper()
            if code not in _LANE_CODES:
                continue
            views[code] = V1469LaneObservationView(
                lane_code=code,
                matched=_positive_int(raw.get("matched")),
                selected=_positive_int(raw.get("selected")),
                suppressed=_positive_int(raw.get("suppressed")),
                safe=_positive_int(raw.get("safe")),
                hard_blocked=_positive_int(raw.get("hard_blocked")),
                data_blocked=_positive_int(raw.get("data_blocked")),
                not_evaluated=_positive_int(raw.get("not_evaluated")),
                evaluable=_positive_int(raw.get("evaluable")),
                evaluable_reward_net_bp=(
                    _number(raw.get("evaluable_reward_net_bp")) or 0.0
                ),
                last_observed_at_ms=_positive_int(
                    raw.get("last_observed_at_ms")
                ),
            )
        if not isinstance(arm_rows, list) or not isinstance(lease_rows, list):
            raise TypeError("v1469 arm/lease monitor rows are unavailable")
        leases = {
            str(row.get("arm_key") or ""): row
            for row in lease_rows
            if isinstance(row, Mapping)
        }
        for raw in arm_rows:
            if not isinstance(raw, Mapping):
                raise TypeError("v1469 arm summary row is invalid")
            code = str(raw.get("lane_code") or "").strip().upper()
            view = views.get(code)
            if view is None:
                continue
            arm_key = str(raw.get("arm_key") or "").strip()
            lease = leases.get(arm_key, {})
            view.arms.append(V1469ArmObservationView(
                arm_key=arm_key,
                lane_code=code,
                side=str(raw.get("effective_side") or "").strip().upper(),
                regime=str(raw.get("coarse_regime") or "").strip().upper(),
                profile_id=str(raw.get("execution_profile_id") or "").strip(),
                evidence=_positive_int(raw.get("evidence")),
                pending=_positive_int(raw.get("pending")),
                terminal=_positive_int(raw.get("terminal")),
                dropped=_positive_int(raw.get("dropped")),
                evaluable=_positive_int(raw.get("evaluable")),
                reward_net_bp=_number(raw.get("evaluable_reward_net_bp")) or 0.0,
                tp_first=_positive_int(raw.get("tp_first")),
                sl_first=_positive_int(raw.get("sl_first")),
                no_fill=_positive_int(raw.get("no_fill")),
                last_evidence_at_ms=_positive_int(raw.get("last_evidence_at_ms")),
                lease_phase=str(lease.get("phase") or "NONE").upper(),
                lease_status=str(lease.get("status") or "NONE").upper(),
                lease_expires_at_ms=_positive_int(lease.get("expires_at_ms")),
                notional_cap_usdc=_number(lease.get("notional_cap_usdc")) or 0.0,
            ))
        for raw in suppressed_rows:
            if not isinstance(raw, Mapping):
                raise TypeError("v1469 suppression row is invalid")
            code = str(raw.get("lane_code") or "").strip().upper()
            view = views.get(code)
            if view is None:
                continue
            suppressor = str(
                raw.get("suppressed_by_lane_code") or "UNSPECIFIED"
            ).strip().upper()
            view.suppressed_by[suppressor or "UNSPECIFIED"] += _positive_int(
                raw.get("candidates")
            )
        return views, V1469ObservationHealth(
            True,
            opportunities=_positive_int(
                opportunity_totals.get("opportunities")
            ),
            complete_opportunities=_positive_int(
                opportunity_totals.get("complete_opportunities")
            ),
            last_observed_at_ms=_positive_int(
                opportunity_totals.get("last_observed_at_ms")
            ),
        )
    except Exception:
        # Missing migration, old read replicas, and temporary query failures
        # must never take down the established Lane Monitor.
        return {}, V1469ObservationHealth(False)


def _freshness(last_at_ms: int, now_ms: int) -> str:
    if not last_at_ms:
        return "—"
    seconds = max(0, (now_ms - last_at_ms) // 1000)
    if seconds < 3600:
        return f"{seconds // 60}m" if seconds >= 60 else f"{seconds}s"
    return f"{seconds // 3600}h"


def _lease_remaining(expires_at_ms: int, now_ms: int) -> str:
    if not expires_at_ms:
        return "—"
    remaining_s = (expires_at_ms - now_ms) // 1000
    if remaining_s <= 0:
        return "expired"
    if remaining_s < 60:
        return f"{remaining_s}s"
    if remaining_s < 3600:
        return f"{remaining_s // 60}m{remaining_s % 60:02d}s"
    return f"{remaining_s // 3600}h{(remaining_s % 3600) // 60:02d}m"


def _promotion_health_line(
    views: Iterable[PromotionRuntimeView],
    health: PromotionRuntimeHealth,
) -> str:
    materialized = list(views)
    active = sum(item.state in {"PROBATION", "LIVE"} for item in materialized)
    state = "HEALTHY" if health.healthy else "DEGRADED"
    line = (
        f"🤖 <b>v1.4.64 rolling 90m authority {state}</b> | "
        f"active leases {active} | exact cohorts {len(materialized)} | "
        f"evidence {health.window_minutes}m"
    )
    missing = [name for name, available in health.tables.items() if not available]
    if missing:
        line += "\n⚠️ promotion schema unavailable: <code>" + escape(
            ", ".join(missing)
        ) + "</code>"
    return line


def _promotion_lane_summary(
    views: Iterable[PromotionRuntimeView],
    lane_code: str,
) -> str:
    matching = [item for item in views if item.lane_code == lane_code]
    if not matching:
        return ""
    counts = Counter(item.state for item in matching)
    labels = " ".join(
        f"{state}={counts[state]}"
        for state in ("SHADOW", "PROBATION", "LIVE", "DEMOTED", "EXPIRED")
        if counts[state]
    )
    blocked = sum(bool(item.blockers) for item in matching)
    return (
        f"\n  v1464 exact {len(matching)} | {labels or 'SHADOW=0'} | "
        f"blocked {blocked}"
    )


def _promotion_runtime_section(
    views: Iterable[PromotionRuntimeView],
    health: PromotionRuntimeHealth,
    *,
    now_ms: int,
    lane_code: str,
) -> str:
    matching = [item for item in views if item.lane_code == lane_code]
    lines = [
        "<b>v1.4.64 rolling 90m auto-promotion authority (read-only)</b>",
        _promotion_health_line(matching, health),
        "Only this exact-cohort state/lease is authority; legacy UTC diversity is not.",
        "No manual promotion action is available.",
    ]
    if not matching:
        lines.append("No v1.4.64 exact cohort evidence or lease for this lane.")
        return "\n".join(lines)

    for index, item in enumerate(matching, start=1):
        ev = (
            "—"
            if item.fee_net_ev_per_opportunity is None
            else f"{item.fee_net_ev_per_opportunity:+.4f}"
        )
        cap = (
            "—"
            if item.notional_cap_usdc is None
            else f"${item.notional_cap_usdc:g}"
        )
        blocker = "; ".join(item.blockers) or "none"
        event = item.latest_event_type or "—"
        if item.latest_event_at_ms:
            event += f" ({_freshness(item.latest_event_at_ms, now_ms)} ago)"
        if item.latest_event_reason:
            event += f" reason={item.latest_event_reason}"
        identity = (
            f"state={item.market_state} side={item.effective_side} "
            f"strategy={item.strategy} profile={item.resolved_profile_hash[:12]} "
            f"reg={item.registry_hash[:12]} "
            f"admission={item.admission_policy_hash[:12]} "
            f"promotion={item.promotion_policy_hash[:12] or '—'}"
        )
        lines.extend(
            (
                "",
                f"<b>P{index} {escape(item.state)}</b> "
                f"<code>{escape(item.cohort_key[:28])}</code>",
                f"  <code>{escape(identity)}</code>",
                f"  lease gen {item.generation or '—'} | "
                f"remaining {_lease_remaining(item.expires_at_ms, now_ms)} | "
                f"cap {cap}",
                f"  {health.window_minutes}m evidence "
                f"n/eval {item.evidence_count}/{item.evaluable} | "
                f"TP {item.tp_first} SL {item.sl_first} NF {item.no_fill} | "
                f"fee-net EV/op {ev}",
                f"  paid {item.paid_wins}W/{item.paid_complete} complete | "
                f"net {item.paid_net_pnl_usdc:+.4f} USDC",
                f"  blocker <code>{escape(blocker)}</code>",
                f"  latest <code>{escape(event)}</code>",
            )
        )
    return "\n".join(lines)


_W6A_PROFILE_WINDOWS = ((15, "safety"), (30, "authority"), (90, "guard"))


def _w6a_selection_state(row: Mapping[str, Any], now_ms: int) -> tuple[str, list[str]]:
    status = str(row.get("status") or "").strip().upper()
    blockers: list[str] = []
    expires_at_ms = _positive_int(row.get("expires_at_ms"))
    if status in {"ACTIVE", "SHADOW", "PROBATION", "LIVE"} and expires_at_ms and now_ms >= expires_at_ms:
        return "EXPIRED", ["lease_expired_not_reconciled"]
    if status in {"DEMOTED", "REVOKED", "HALTED"}:
        reason = str(row.get("demotion_reason") or status.lower()).strip()
        return "DEMOTED", [reason] if reason else []
    cooldown_until_ms = _positive_int(row.get("cooldown_until_ms"))
    if cooldown_until_ms > now_ms:
        blockers.append("cooldown_active")
    if status in {
        "ACTIVE",
        "SHADOW",
        "PROBATION",
        "LIVE",
        "COOLDOWN",
        "EXPIRED",
    }:
        return status, blockers
    blockers.append("selection_status_unknown")
    return "SHADOW", blockers


def _w6a_snapshot_blockers(snapshot: Mapping[str, Any]) -> list[str]:
    blockers: list[str] = []
    if snapshot and _flag(snapshot.get("data_complete")) is False:
        blockers.append("data_incomplete")
    raw = snapshot.get("blockers", snapshot.get("selector_blockers"))
    if isinstance(raw, str):
        blockers.extend(item.strip() for item in raw.split(";") if item.strip())
    elif isinstance(raw, Mapping):
        for profile_id, values in raw.items():
            if isinstance(values, str):
                values = [values]
            if isinstance(values, Iterable) and not isinstance(
                values, (str, bytes, Mapping)
            ):
                blockers.extend(
                    f"{profile_id}:{item}"
                    for item in values
                    if str(item).strip()
                )
    elif isinstance(raw, Iterable) and not isinstance(raw, (str, bytes, Mapping)):
        blockers.extend(str(item).strip() for item in raw if str(item).strip())
    return blockers


def _selector_is_w6a(row: Mapping[str, Any]) -> bool:
    """Accept explicit/snapshot lane markers without assuming selector-key syntax."""
    if str(row.get("lane_code") or "").strip().upper() == "W6A":
        return True
    selector_key = str(row.get("selector_key") or "").upper()
    if "W6A" in selector_key:
        return True
    snapshot = _json(row.get("evidence_snapshot_json"))
    return str(snapshot.get("lane_code") or "").strip().upper() == "W6A"


def _w6a_profile_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return tuple(
        str(row.get(name) or "").strip()
        for name in ("profile_id", "resolved_profile_hash", "profile_plan_hash")
    )


def _w6a_selector_identity(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(row.get(name) or "").strip().upper()
        for name in (
            "environment",
            "symbol",
            "lane_code",
            "market_state",
            "effective_side",
            "strategy",
        )
    )


def _w6a_add_evidence(
    bucket: ProfileWindowEvidence,
    row: Mapping[str, Any],
) -> None:
    bucket.count += 1
    complete = _flag(row.get("data_complete")) is True
    ambiguous = _flag(row.get("ambiguous")) is True
    outcome = str(row.get("outcome") or "").strip().lower()
    if complete and not ambiguous:
        if outcome in _PROMOTION_EVALUABLE_OUTCOMES:
            bucket.evaluable += 1
        if outcome in _PROMOTION_TP_OUTCOMES:
            bucket.tp_first += 1
        elif outcome in _PROMOTION_SL_OUTCOMES:
            bucket.sl_first += 1
        elif outcome == "no_fill":
            bucket.no_fill += 1
        net_pnl_bp = _number(row.get("net_pnl_bp"))
        if outcome in _KNOWN_TERMINAL_OUTCOMES and net_pnl_bp is not None:
            bucket.net_pnl_total_bp += net_pnl_bp
            bucket.net_pnl_count += 1


async def collect_w6a_profile_selector(
    db: Any,
    *,
    now_ms: int,
) -> tuple[list[W6AProfileSelectorView], W6AProfileSelectorHealth]:
    """Read v1.4.65 selector tables only; missing tables are a safe degrade."""
    now = max(0, int(now_ms))
    guard_cutoff_ms = max(0, now - 90 * 60 * 1000)
    evidence_rows, has_evidence = await _read_rows(
        db,
        "v1465_w6a_profile_evidence",
        query=(
            "SELECT * FROM v1465_w6a_profile_evidence "
            "WHERE lane_code = 'W6A' "
            f"AND observed_at_ms >= {guard_cutoff_ms} "
            f"AND observed_at_ms <= {now} "
            f"AND terminal_at_ms <= {now} "
            "ORDER BY observed_at_ms DESC, terminal_at_ms DESC LIMIT 5000"
        ),
    )
    selection_rows, has_selections = await _read_rows(
        db,
        "v1465_w6a_profile_selections",
        query=(
            "SELECT * FROM v1465_w6a_profile_selections "
            "ORDER BY updated_at_ms DESC, selector_key LIMIT 500"
        ),
    )
    event_rows, has_events = await _read_rows(
        db,
        "v1465_w6a_profile_selection_events",
        query=(
            "SELECT * FROM v1465_w6a_profile_selection_events "
            "ORDER BY event_time_ms DESC, id DESC LIMIT 500"
        ),
    )
    health = W6AProfileSelectorHealth({
        "v1465_w6a_profile_evidence": has_evidence,
        "v1465_w6a_profile_selections": has_selections,
        "v1465_w6a_profile_selection_events": has_events,
    })

    views_by_key: dict[str, W6AProfileSelectorView] = {}
    for raw in selection_rows:
        row = _nested(raw)
        if not _selector_is_w6a(row):
            continue
        selector_key = str(row.get("selector_key") or "").strip()
        if not selector_key:
            continue
        updated_at_ms = max(
            _positive_int(row.get("updated_at_ms")),
            _positive_int(row.get("renewed_at_ms")),
            _positive_int(row.get("issued_at_ms")),
        )
        existing = views_by_key.get(selector_key)
        if existing is not None and getattr(existing, "_updated_at_ms", 0) >= updated_at_ms:
            continue
        state, blockers = _w6a_selection_state(row, now)
        snapshot = _json(row.get("evidence_snapshot_json"))
        view = W6AProfileSelectorView(
            selector_key=selector_key,
            winner_profile_id=str(row.get("winner_profile_id") or "").strip(),
            winner_profile_hash=str(
                row.get("winner_resolved_profile_hash")
                or row.get("winner_profile_hash")
                or ""
            ).strip(),
            market_state=str(row.get("market_state") or "").strip(),
            generation=_positive_int(row.get("generation")),
            state=state,
            notional_cap_usdc=_number(row.get("notional_cap_usdc")),
            expires_at_ms=_positive_int(row.get("expires_at_ms")),
            blockers=[*blockers, *_w6a_snapshot_blockers(snapshot)],
        )
        # Kept private to the reader; the selection schema remains untouched.
        setattr(view, "_updated_at_ms", updated_at_ms)
        setattr(view, "_identity", _w6a_selector_identity(row))
        views_by_key[selector_key] = view

    evidence_by_selector: dict[
        tuple[str, ...],
        dict[tuple[str, str, str], dict[int, ProfileWindowEvidence]],
    ] = {}
    for raw in evidence_rows:
        row = _nested(raw)
        observed_raw = _number(row.get("observed_at_ms"))
        terminal_raw = _number(row.get("terminal_at_ms"))
        if observed_raw is None or terminal_raw is None:
            continue
        observed_at_ms = _positive_int(row.get("observed_at_ms"))
        terminal_at_ms = _positive_int(row.get("terminal_at_ms"))
        if (
            str(row.get("lane_code") or "").strip().upper() != "W6A"
            or observed_raw < 0
            or terminal_raw < 0
            or observed_at_ms < guard_cutoff_ms
            or observed_at_ms > now
            or terminal_at_ms > now
            or _flag(row.get("diagnostic_only")) is True
        ):
            continue
        key = _w6a_profile_key(row)
        if not all(key[:2]):
            continue
        identity = _w6a_selector_identity(row)
        windows = evidence_by_selector.setdefault(identity, {}).setdefault(
            key, {minutes: ProfileWindowEvidence() for minutes, _ in _W6A_PROFILE_WINDOWS}
        )
        for minutes, _label in _W6A_PROFILE_WINDOWS:
            if observed_at_ms >= max(0, now - minutes * 60 * 1000):
                _w6a_add_evidence(windows[minutes], row)

    views = list(views_by_key.values())
    # A selector row is not a substitute for another exact identity's row.
    # Keep evidence-only cohorts visible even when a different market-state /
    # side / strategy already has a materialized selection.
    represented_identities = {
        getattr(view, "_identity", ())
        for view in views
        if any(getattr(view, "_identity", ()))
    }
    evidence_only_index = 0
    for identity in sorted(evidence_by_selector):
        if identity in represented_identities:
            continue
        evidence_only_index += 1
        view = W6AProfileSelectorView(
            selector_key=f"W6A evidence-only {evidence_only_index}",
            market_state=identity[3] if len(identity) > 3 else "",
            blockers=["no_profile_selection"],
        )
        setattr(view, "_identity", identity)
        views.append(view)
    for view in views:
        identity = getattr(view, "_identity", ())
        profile_source = evidence_by_selector.get(identity)
        if (
            profile_source is None
            and not any(identity)
            and len(evidence_by_selector) == 1
        ):
            # Compatibility for early v1.4.65/read-test rows that predate
            # selector identity columns.  Production 014 rows always match
            # exact identity and never take this fallback.
            profile_source = next(iter(evidence_by_selector.values()))
        view.profiles = {
            key: windows.copy()
            for key, windows in (profile_source or {}).items()
        }
        if view.state in {"ACTIVE", "PROBATION", "LIVE"} and not view.winner_profile_id:
            view.blockers.append("winner_missing")
        view.blockers = list(dict.fromkeys(item for item in view.blockers if item))

    for raw in event_rows:
        row = _nested(raw)
        view = views_by_key.get(str(row.get("selector_key") or "").strip())
        event_at_ms = _positive_int(row.get("event_time_ms"))
        if view is None or (view.latest_event_type and event_at_ms <= view.latest_event_at_ms):
            continue
        payload = _json(row.get("payload_json"))
        details = payload.get("details") if isinstance(payload.get("details"), Mapping) else {}
        view.latest_event_type = str(row.get("event_type") or "").strip()
        view.latest_event_at_ms = event_at_ms
        view.latest_event_reason = str(
            details.get("reason") or payload.get("reason") or row.get("demotion_reason") or ""
        ).strip()

    views.sort(key=lambda item: (item.selector_key, item.winner_profile_id, item.winner_profile_hash))
    return views, health


def _w6a_profile_health_line(health: W6AProfileSelectorHealth) -> str:
    state = "HEALTHY" if health.healthy else "DEGRADED"
    line = f"🧪 <b>v1.4.65 W6A 15/30/90m profile selector {state}</b>"
    missing = [name for name, available in health.tables.items() if not available]
    if missing:
        line += " | schema unavailable: <code>" + escape(", ".join(missing)) + "</code>"
    return line


def _w6a_profile_lane_summary(
    views: Iterable[W6AProfileSelectorView],
) -> str:
    materialized = list(views)
    if not materialized:
        return "\n  v1465 W6A winner — | state SHADOW"
    winners = [item for item in materialized if item.winner_profile_id or item.winner_profile_hash]
    candidates = winners or materialized
    state_rank = {
        "LIVE": 0,
        "PROBATION": 1,
        "ACTIVE": 2,
        "SHADOW": 3,
        "EXPIRED": 4,
        "DEMOTED": 5,
    }
    winner = min(
        candidates,
        key=lambda item: (
            state_rank.get(item.state, 9),
            -int(getattr(item, "_updated_at_ms", 0)),
            item.selector_key,
        ),
    )
    profile = winner.winner_profile_id or "—"
    if winner.winner_profile_hash:
        profile += f"/{winner.winner_profile_hash[:12]}"
    state = winner.state
    if winner.market_state:
        state += f"/{winner.market_state}"
    return f"\n  v1465 W6A winner {escape(profile)} | state {escape(state)}"


def _w6a_profile_selector_section(
    views: Iterable[W6AProfileSelectorView],
    health: W6AProfileSelectorHealth,
    *,
    now_ms: int,
) -> str:
    materialized = list(views)
    lines = [
        "<b>v1.4.65 W6A rolling 15/30/90m profile selector (read-only)</b>",
        _w6a_profile_health_line(health),
        "15m safety | 30m authority | 90m guard; profile-specific and independent of legacy UTC diversity.",
        "No selector action is available.",
    ]
    if health.healthy and not any(item.profiles for item in materialized):
        lines.append(
            "⚠️ <b>EVIDENCE_STALLED</b>: no terminal W6A profile evidence in the "
            "current rolling 90m window (loop may be idle or no eligible W6A opportunity)."
        )
    if not materialized:
        lines.append("No W6A profile selection or terminal evidence.")
        return "\n".join(lines)
    for index, item in enumerate(materialized, start=1):
        winner = item.winner_profile_id or "—"
        if item.winner_profile_hash:
            winner += f"/{item.winner_profile_hash[:12]}"
        cap = "—" if item.notional_cap_usdc is None else f"${item.notional_cap_usdc:g}"
        latest = item.latest_event_type or "—"
        if item.latest_event_at_ms:
            latest += f" ({_freshness(item.latest_event_at_ms, now_ms)} ago)"
        if item.latest_event_reason:
            latest += f" reason={item.latest_event_reason}"
        state = item.state
        if item.market_state:
            state += f"/{item.market_state}"
        lines.extend((
            "",
            f"<b>W6A-S{index} {escape(state)}</b> "
            f"<code>{escape(item.selector_key)}</code>",
            f"  winner <code>{escape(winner)}</code> | gen {item.generation or '—'} | "
            f"lease {_lease_remaining(item.expires_at_ms, now_ms)} | cap {cap}",
        ))
        if not item.profiles:
            lines.append("  profiles: none")
        for profile_key, windows in sorted(item.profiles.items()):
            profile_id, profile_hash, plan_hash = profile_key
            lines.append(
                f"  <code>profile={escape(profile_id)} hash={escape(profile_hash[:12])} "
                f"plan={escape(plan_hash[:12] or '—')}</code>"
            )
            for minutes, label in _W6A_PROFILE_WINDOWS:
                metrics = windows[minutes]
                ev = "—" if metrics.ev_bp is None else f"{metrics.ev_bp:+.2f}bp"
                lines.append(
                    f"    {minutes}m {label} n/eval {metrics.count}/{metrics.evaluable} | "
                    f"TP {metrics.tp_first} SL {metrics.sl_first} NF {metrics.no_fill} | EV {ev}"
                )
        lines.append(f"  blockers <code>{escape('; '.join(item.blockers) or 'none')}</code>")
        lines.append(f"  latest <code>{escape(latest)}</code>")
    return "\n".join(lines)


def _v1469_observation_health_line(
    health: V1469ObservationHealth,
) -> str:
    state = "AVAILABLE" if health.available else "unavailable"
    return (
        f"🔬 <b>v1.4.69 rolling {health.window_minutes}m observation-only "
        f"{state}</b> | opportunities "
        f"{health.complete_opportunities}/{health.opportunities} complete | "
        "no live/order authority"
    )


def _v1469_lane_summary(
    item: V1469LaneObservationView | None,
    health: V1469ObservationHealth,
    *,
    lane_code: str,
    now_ms: int,
) -> str:
    if not health.available:
        return ""
    view = item or V1469LaneObservationView(lane_code=lane_code)
    ev = "—" if view.ev_bp is None else f"{view.ev_bp:+.2f}"
    return (
        f"\n  v1469 OBS {health.window_minutes}m | "
        f"matched {view.matched} | selected {view.selected} | "
        f"suppressed {view.suppressed} | safe {view.safe} | "
        f"hard {view.hard_blocked} | data {view.data_blocked} | "
        f"not-eval {view.not_evaluated} | evaluable {view.evaluable} | "
        f"EV {ev} bp | fresh {_freshness(view.last_observed_at_ms, now_ms)}"
    )


def _v1469_observation_detail_section(
    item: V1469LaneObservationView | None,
    health: V1469ObservationHealth,
    *,
    lane_code: str,
    now_ms: int,
) -> str:
    lines = [
        (
            f"<b>v1.4.69 rolling {health.window_minutes}m "
            "Adaptive Arm observation-only</b>"
        ),
        "Read-only evidence; no live/order authority.",
    ]
    if not health.available:
        lines.append(
            "⚠️ observation tables/query unavailable; legacy monitor remains active."
        )
        return "\n".join(lines)
    view = item or V1469LaneObservationView(lane_code=lane_code)
    if item is None:
        if health.opportunities:
            lines.append(
                "zero-match reason: "
                f"<code>predicate did not match across "
                f"{health.opportunities} captured opportunities</code>"
            )
        else:
            lines.append(
                "zero-match reason: "
                "<code>no v1.4.69 opportunities captured in this window</code>"
            )
    ev = "—" if view.ev_bp is None else f"{view.ev_bp:+.2f} bp"
    histogram = ", ".join(
        f"{escape(code)}={count}"
        for code, count in sorted(
            view.suppressed_by.items(),
            key=lambda pair: (-pair[1], pair[0]),
        )
    ) or "none"
    lines.extend(
        (
            f"matched/selected/suppressed: "
            f"<b>{view.matched}/{view.selected}/{view.suppressed}</b>",
            f"safe/hard/data/not-eval/evaluable: "
            f"<b>{view.safe}/{view.hard_blocked}/{view.data_blocked}/"
            f"{view.not_evaluated}/{view.evaluable}</b>",
            f"EV: <b>{ev}</b> | freshness: "
            f"<b>{_freshness(view.last_observed_at_ms, now_ms)}</b>",
            f"suppressed-by: <code>{histogram}</code>",
        )
    )
    if view.arms:
        lines.append("<b>Exact arms (legacy control remains paid authority)</b>")
    for arm in view.arms:
        ev = "—" if arm.ev_bp is None else f"{arm.ev_bp:+.2f}bp"
        blockers = "none" if arm.evaluable else "rolling_window_not_ready"
        lines.extend((
            f"<code>{escape(arm.arm_key[:16])}</code> {escape(arm.side)}/"
            f"{escape(arm.regime)} profile=<code>{escape(arm.profile_id)}</code>",
            f"  evidence {arm.evidence} | terminal/evaluable "
            f"{arm.terminal}/{arm.evaluable} | TP/SL/NF "
            f"{arm.tp_first}/{arm.sl_first}/{arm.no_fill} | EV {ev} | "
            f"fresh {_freshness(arm.last_evidence_at_ms, now_ms)}",
            f"  state/lease {escape(arm.lease_phase)}/{escape(arm.lease_status)} "
            f"({_lease_remaining(arm.lease_expires_at_ms, now_ms)}) | "
            f"cap {arm.notional_cap_usdc:.2f} | blockers <code>{blockers}</code>",
        ))
    return "\n".join(lines)


def _line(
    item: LaneEvidence,
    now_ms: int,
    promotion_views: Iterable[PromotionRuntimeView] = (),
    profile_selector_views: Iterable[W6AProfileSelectorView] = (),
    observation_view: V1469LaneObservationView | None = None,
    observation_health: V1469ObservationHealth = V1469ObservationHealth(False),
) -> str:
    outcomes = item.outcomes
    ev = "—" if not item.ev_count else f"{item.ev_total / item.ev_count:+.4f}"
    paid = "—" if not item.paid_count else f"{item.paid_wins}W/{item.paid_losses}L {item.paid_net:+.4f}"
    reason = item.invalid_reasons.most_common(1)[0][0] if item.invalid_reasons else "—"
    blockers = "; ".join(item.promotion_blockers) or "none (manual review only)"
    ready_cohorts = sum(
        cohort.readiness == "REVIEW_READY" for cohort in item.cohorts.values()
    )
    return (
        f"<code>{escape(item.code).ljust(10)}</code> <b>{escape(item.intended_mode)}</b> {item.readiness}\n"
        f"  cohorts {len(item.cohorts)} | review-ready {ready_cohorts} (cohort-only)\n"
        f"  cap {item.captured} | ok {item.complete} | evl {item.evaluable} | bad {item.invalid} | "
        f"drop {item.dropped} | pend {item.pending} | "
        f"TP {outcomes['tp1_first'] + outcomes['tp_first'] + outcomes['tp']} "
        f"SL {outcomes['sl_first'] + outcomes['sl']} NF {outcomes['no_fill']} "
        f"Amb {outcomes['ambiguous_both']}\n"
        f"  EV/op {ev} | paid {paid} | legacy A/O {item.legacy_adaptive}/{item.legacy_shadow_outcomes} | "
        f"fresh {_freshness(item.last_at_ms, now_ms)} | "
        f"invalid <code>{escape(reason)}</code> | reopen {item.live_reopen_breaches}\n"
        f"  blockers <code>{escape(blockers)}</code>"
        f"{_promotion_lane_summary(promotion_views, item.code)}"
        f"{_w6a_profile_lane_summary(profile_selector_views) if item.code == 'W6A' else ''}"
        f"{_v1469_lane_summary(observation_view, observation_health, lane_code=item.code, now_ms=now_ms)}"
    )


async def build_lane_monitor(
    db: Any,
    registry: object | None = None,
    *,
    now_ms: int | None = None,
) -> str:
    now = int(time.time() * 1000) if now_ms is None else now_ms
    lanes, tables = await collect_lane_evidence(db, registry)
    promotion_views, promotion_health = await collect_promotion_runtime(
        db, now_ms=now
    )
    profile_selector_views, profile_selector_health = await collect_w6a_profile_selector(
        db, now_ms=now
    )
    observation_views, observation_health = await collect_v1469_observation(
        db, now_ms=now
    )
    unavailable = [name for name, available in tables.items() if not available]
    header = (
        "🧭 <b>Legacy Lane Monitor</b>\n"
        "27 frozen lanes | historical evidence ledger (no live authority)\n"
        "Legacy REVIEW_READY and UTC diversity are historical diagnostics only; "
        "v1.4.64 rolling 90m authority is shown separately by state/lease"
    )
    if unavailable:
        header += "\n⚠️ unavailable: <code>" + escape(", ".join(unavailable)) + "</code>"
    safety_warnings = sorted({
        blocker
        for lane in lanes.values()
        for blocker in lane.global_blockers
        if "reject_reopen_live_breach" in blocker
    })
    if safety_warnings:
        header += "\n⚠️ safety: <code>" + escape("; ".join(safety_warnings)) + "</code>"
    header += "\n" + _promotion_health_line(
        promotion_views, promotion_health
    )
    header += "\n" + _w6a_profile_health_line(profile_selector_health)
    header += "\n" + _v1469_observation_health_line(observation_health)
    return header + "\n\n" + "\n\n".join(
        _line(
            item,
            now,
            promotion_views,
            profile_selector_views,
            observation_views.get(item.code),
            observation_health,
        )
        for item in lanes.values()
    )


async def build_lane_detail(
    db: Any,
    lane_code: str,
    registry: object | None = None,
    *,
    now_ms: int | None = None,
) -> str:
    lanes, tables = await collect_lane_evidence(db, registry)
    code = str(lane_code).strip().upper()
    item = lanes.get(code)
    if item is None:
        return f"⚠️ Unknown legacy lane: <code>{escape(code)}</code>"
    now = int(time.time() * 1000) if now_ms is None else now_ms
    promotion_views, promotion_health = await collect_promotion_runtime(
        db, now_ms=now
    )
    profile_selector_views, profile_selector_health = await collect_w6a_profile_selector(
        db, now_ms=now
    )
    observation_views, observation_health = await collect_v1469_observation(
        db, now_ms=now
    )
    missing = ", ".join(name for name, available in tables.items() if not available) or "none"
    outcomes = ", ".join(f"{escape(key)}={value}" for key, value in sorted(item.outcomes.items())) or "none"
    invalid = item.invalid_reasons.most_common(1)[0][0] if item.invalid_reasons else "none"
    ev = "—" if not item.ev_count else f"{item.ev_total / item.ev_count:+.4f} USDC"
    identities = ", ".join(
        f"{name}={next(iter(values)) if len(values) == 1 else ('MISSING' if not values else f'MIXED({len(values)})')}"
        for name, values in item.identity_values.items()
    )
    blockers = "; ".join(item.promotion_blockers) or "none"
    cohort_lines: list[str] = []
    ordered_cohorts = sorted(
        item.cohorts.values(),
        key=lambda cohort: (cohort.last_at_ms, cohort.key),
        reverse=True,
    )
    for index, cohort in enumerate(ordered_cohorts, start=1):
        cohort_ev = (
            "—"
            if not cohort.ev_count
            else f"{cohort.ev_total / cohort.ev_count:+.4f} USDC"
        )
        cohort_blockers = "; ".join(cohort.promotion_blockers) or "none"
        cohort_lines.append(
            f"<b>C{index} {escape(cohort.readiness)}</b> "
            f"<code>{escape(cohort.key.label)}</code>\n"
            f"  cap/ok/evl/bad/drop/pend: "
            f"{cohort.captured}/{cohort.complete}/{cohort.evaluable}/"
            f"{cohort.invalid}/{cohort.dropped}/{cohort.pending} | "
            f"TP {cohort.tp_first} | EV/op {cohort_ev} | "
            f"UTC {len(cohort.utc_dates)} | reopen {cohort.live_reopen_breaches}\n"
            f"  legacy review diagnostics <code>{escape(cohort_blockers)}</code>"
        )
    cohort_section = (
        "\n\n<b>Legacy exact cohorts — historical review diagnostics only</b>\n"
        + ("\n\n".join(cohort_lines) if cohort_lines else "none")
    )
    profile_selector_section = (
        "\n\n" + _w6a_profile_selector_section(
            profile_selector_views, profile_selector_health, now_ms=now
        )
        if code == "W6A"
        else ""
    )
    return (
        f"🧭 <b>{escape(item.code)}</b> | <b>{escape(item.intended_mode)}</b> | <b>{item.readiness}</b>\n"
        f"Lane totals are informational; readiness is cohort-specific.\n"
        f"Captured/complete/evaluable/invalid: <b>{item.captured}/{item.complete}/{item.evaluable}/{item.invalid}</b>\n"
        f"Legacy adaptive/shadow outcomes (not cohort): "
        f"<b>{item.legacy_adaptive}/{item.legacy_shadow_outcomes}</b>\n"
        f"Historical terminal quality — pending/dropped/incomplete/ambiguous/"
        f"legacy UTC diversity/reopen breach: "
        f"<b>{item.pending}/{item.dropped}/{item.incomplete}/{item.ambiguous}/{len(item.utc_dates)}/{item.live_reopen_breaches}</b>\n"
        f"Outcomes: <code>{outcomes}</code>\nEV/op: <b>{ev}</b>\n"
        f"Paid: <b>{item.paid_wins}W/{item.paid_losses}L {item.paid_net:+.4f} USDC</b> ({item.paid_count} runs)\n"
        f"Freshness: <b>{_freshness(item.last_at_ms, now)}</b> | top invalid: <code>{escape(str(invalid))}</code>\n"
        f"Cohort identity: <code>{escape(identities)}</code>\n"
        f"Legacy review diagnostics (not v1.4.64/v1.4.65 authority): <code>{escape(blockers)}</code>\n"
        f"Legacy REVIEW_READY and UTC diversity are informational; only the v1.4.64 "
        f"rolling 90m state/lease below can authorize a legacy lane.\n"
        f"Unavailable tables: <code>{escape(missing)}</code>"
        f"{cohort_section}\n\n"
        f"{_promotion_runtime_section(promotion_views, promotion_health, now_ms=now, lane_code=code)}"
        f"{profile_selector_section}"
        f"\n\n{_v1469_observation_detail_section(observation_views.get(code), observation_health, lane_code=code, now_ms=now)}"
    )


def lane_monitor_keyboard(registry: object | None = None):
    """Return the fixed monitor keyboard; callbacks remain below 64 bytes."""
    if InlineKeyboardButton is None or InlineKeyboardMarkup is None:  # pragma: no cover
        return []
    rows = [[
        InlineKeyboardButton("📋 Lanes", callback_data="mainnet:lanes"),
        InlineKeyboardButton("🔄 Refresh", callback_data="mainnet:lanes:refresh"),
    ]]
    codes = [str(item["lane_code"]) for item in normalize_lane_registry(registry)]
    for offset in range(0, len(codes), 3):
        rows.append([
            InlineKeyboardButton(code, callback_data=f"mainnet:lane:{code}")
            for code in codes[offset:offset + 3]
        ])
    return InlineKeyboardMarkup(rows)


def lane_monitor_callback() -> str:
    return "mainnet:lanes"


# Explicit aliases make integration call-sites self-describing.
build_lane_monitor_overview = build_lane_monitor
build_lane_detail_view = build_lane_detail
build_lane_monitor_keyboard = lane_monitor_keyboard
