"""Pure Phase-C risk policy and Asia/Taipei daily-risk reducer.

The module deliberately has no database, exchange, or live-runtime dependency.
It consumes immutable inputs and returns immutable decisions so submit-time
integration can fail closed without making a network call.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .v1469_adaptive_identity import RiskPolicy, canonical_sha256


PHASE_C_SCHEMA = "v1469.phase-c-risk.1"
TAIPEI_TIMEZONE = "Asia/Taipei"
RISK_SNAPSHOT_MAX_AGE_MS = 10_000
SOFT_ENTRY_CAP_USDC = 25.0
PROFIT_FLOOR_MIN_USDC = 0.02

try:
    _TAIPEI_TZ = ZoneInfo(TAIPEI_TIMEZONE)
except ZoneInfoNotFoundError:  # pragma: no cover - Windows without tzdata
    _TAIPEI_TZ = timezone(timedelta(hours=8), name=TAIPEI_TIMEZONE)


class RiskStage(str, Enum):
    SHADOW = "SHADOW"
    PROBATION = "PROBATION"
    LIVE = "LIVE"


class RiskAction(str, Enum):
    NEW_PAID_ENTRY = "NEW_PAID_ENTRY"
    RISK_REDUCING_EXIT = "RISK_REDUCING_EXIT"


class RiskDecision(str, Enum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"


STAGE_CAP_USDC = {
    RiskStage.SHADOW: 0.0,
    RiskStage.PROBATION: 25.0,
    RiskStage.LIVE: 50.0,
}


DEFAULT_RISK_POLICY = RiskPolicy(
    policy_id="V1469_PHASE_C",
    paid_notional_cap_usdc=50.0,
    per_trade_loss_cap_usdc=0.30,
    lane_open_notional_cap_usdc=50.0,
    global_open_notional_cap_usdc=50.0,
    daily_soft_loss_cap_usdc=0.15,
    daily_hard_loss_cap_usdc=0.30,
    daily_profit_lock_trigger_usdc=0.15,
    daily_profit_lock_giveback_usdc=0.15,
    max_consecutive_losses=2,
    cooldown_s=300,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyRiskEvent:
    """One durable paid-close delta used to reconstruct the active day."""

    event_id: str
    occurred_at_ms: int
    fee_net_pnl_delta_usdc: float
    risk_policy_hash: str
    event_type: str = "PAID_CLOSED"

    def canonical_payload(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "occurred_at_ms": self.occurred_at_ms,
            "fee_net_pnl_delta_usdc": self.fee_net_pnl_delta_usdc,
            "risk_policy_hash": self.risk_policy_hash,
            "event_type": self.event_type,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyRiskSnapshot:
    """Restart-reconstructable paid-risk state for one Taipei active day."""

    active_day: str
    as_of_ms: int
    risk_policy_hash: str
    evidence_revision: str
    closed_fee_net_pnl_usdc: float
    high_water_usdc: float
    profit_floor_usdc: float | None
    remaining_daily_risk_usdc: float
    paid_closed_event_count: int
    event_ids: tuple[str, ...]
    last_event_at_ms: int | None
    soft_loss_triggered: bool
    hard_loss_triggered: bool
    profit_floor_triggered: bool
    entry_blocked: bool
    data_valid: bool
    reason: str


@dataclass(frozen=True, slots=True, kw_only=True)
class NotionalCapRequest:
    """Submit-time inputs for a new paid entry or a protective exit."""

    stage: RiskStage | str
    global_cap_usdc: float
    lane_cap_usdc: float
    remaining_daily_risk_usdc: float
    sl_bp: float
    roundtrip_fee_bp: float
    slippage_bp: float
    exchange_min_notional_usdc: float
    now_ms: int
    expected_risk_policy_hash: str
    action: RiskAction | str = RiskAction.NEW_PAID_ENTRY
    active_position: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class NotionalCapDecision:
    decision: RiskDecision
    reason: str
    notional_cap_usdc: float
    safe_computed_cap_usdc: float
    stage_cap_usdc: float
    risk_limited_cap_usdc: float
    all_in_loss_bp: float
    risk_policy_hash: str
    active_day: str

    @property
    def allowed(self) -> bool:
        return self.decision is RiskDecision.ALLOW


def active_day_key(timestamp_ms: int | float) -> str:
    """Return the Asia/Taipei calendar day for a non-negative UTC epoch-ms."""

    if not _is_nonnegative_finite(timestamp_ms):
        raise ValueError("timestamp_ms must be a finite non-negative number")
    moment = datetime.fromtimestamp(float(timestamp_ms) / 1000.0, tz=timezone.utc)
    return moment.astimezone(_TAIPEI_TZ).date().isoformat()


def reduce_daily_risk(
    events: Iterable[DailyRiskEvent],
    *,
    as_of_ms: int,
    policy: RiskPolicy = DEFAULT_RISK_POLICY,
    expected_risk_policy_hash: str | None = None,
) -> DailyRiskSnapshot:
    """Reduce append-only paid-close events into the current Taipei day.

    Prior-day events are ignored. Future events, malformed events, policy-hash
    mismatches, and conflicting duplicate IDs produce a fail-closed snapshot.
    Exact duplicate records are idempotent.
    """

    policy_hash = _policy_hash(policy)
    if not _is_nonnegative_finite(as_of_ms):
        return _invalid_snapshot(
            as_of_ms=0,
            active_day="INVALID",
            policy_hash=policy_hash,
            reason="invalid_as_of_ms",
        )

    normalized_as_of_ms = int(as_of_ms)
    active_day = active_day_key(normalized_as_of_ms)
    if (
        expected_risk_policy_hash is not None
        and expected_risk_policy_hash != policy_hash
    ):
        return _invalid_snapshot(
            as_of_ms=normalized_as_of_ms,
            active_day=active_day,
            policy_hash=policy_hash,
            reason="risk_policy_hash_mismatch",
        )

    try:
        materialized = tuple(events)
    except Exception:
        return _invalid_snapshot(
            as_of_ms=normalized_as_of_ms,
            active_day=active_day,
            policy_hash=policy_hash,
            reason="invalid_event_stream",
        )

    current_day_by_id: dict[str, DailyRiskEvent] = {}
    for event in materialized:
        if not isinstance(event, DailyRiskEvent):
            return _invalid_snapshot(
                as_of_ms=normalized_as_of_ms,
                active_day=active_day,
                policy_hash=policy_hash,
                reason="invalid_event_type",
            )
        if not _valid_event_shape(event):
            return _invalid_snapshot(
                as_of_ms=normalized_as_of_ms,
                active_day=active_day,
                policy_hash=policy_hash,
                reason="invalid_event",
            )
        if int(event.occurred_at_ms) > normalized_as_of_ms:
            return _invalid_snapshot(
                as_of_ms=normalized_as_of_ms,
                active_day=active_day,
                policy_hash=policy_hash,
                reason="future_event",
            )
        if active_day_key(event.occurred_at_ms) != active_day:
            continue
        if event.risk_policy_hash != policy_hash:
            return _invalid_snapshot(
                as_of_ms=normalized_as_of_ms,
                active_day=active_day,
                policy_hash=policy_hash,
                reason="risk_policy_hash_mismatch",
            )

        prior = current_day_by_id.get(event.event_id)
        if prior is not None:
            if prior.canonical_payload() != event.canonical_payload():
                return _invalid_snapshot(
                    as_of_ms=normalized_as_of_ms,
                    active_day=active_day,
                    policy_hash=policy_hash,
                    reason="conflicting_duplicate_event",
                )
            continue
        current_day_by_id[event.event_id] = event

    ordered = tuple(
        sorted(
            current_day_by_id.values(),
            key=lambda item: (int(item.occurred_at_ms), item.event_id),
        )
    )
    closed_pnl = 0.0
    high_water = 0.0
    profit_floor: float | None = None
    soft_loss_triggered = False
    hard_loss_triggered = False
    profit_floor_triggered = False

    for event in ordered:
        closed_pnl += float(event.fee_net_pnl_delta_usdc)
        high_water = max(high_water, closed_pnl)
        soft_loss_triggered = (
            soft_loss_triggered
            or closed_pnl <= -policy.daily_soft_loss_cap_usdc
        )
        hard_loss_triggered = (
            hard_loss_triggered
            or closed_pnl <= -policy.daily_hard_loss_cap_usdc
        )
        if (
            policy.daily_profit_lock_trigger_usdc > 0.0
            and high_water >= policy.daily_profit_lock_trigger_usdc
        ):
            profit_floor = max(
                PROFIT_FLOOR_MIN_USDC,
                high_water - policy.daily_profit_lock_giveback_usdc,
            )
            if closed_pnl <= profit_floor:
                profit_floor_triggered = True

    entry_blocked = hard_loss_triggered or profit_floor_triggered
    remaining_daily_risk = _remaining_daily_risk(
        closed_pnl=closed_pnl,
        profit_floor=profit_floor,
        policy=policy,
        entry_blocked=entry_blocked,
    )
    event_payloads = [event.canonical_payload() for event in ordered]
    evidence_revision = canonical_sha256(
        {
            "schema": PHASE_C_SCHEMA,
            "active_day": active_day,
            "risk_policy_hash": policy_hash,
            "events": event_payloads,
        }
    )
    if entry_blocked:
        reason = (
            "daily_hard_loss"
            if hard_loss_triggered
            else "daily_profit_floor"
        )
    elif soft_loss_triggered:
        reason = "daily_soft_loss"
    else:
        reason = "healthy"

    return DailyRiskSnapshot(
        active_day=active_day,
        as_of_ms=normalized_as_of_ms,
        risk_policy_hash=policy_hash,
        evidence_revision=evidence_revision,
        closed_fee_net_pnl_usdc=closed_pnl,
        high_water_usdc=high_water,
        profit_floor_usdc=profit_floor,
        remaining_daily_risk_usdc=remaining_daily_risk,
        paid_closed_event_count=len(ordered),
        event_ids=tuple(event.event_id for event in ordered),
        last_event_at_ms=(
            int(ordered[-1].occurred_at_ms) if ordered else None
        ),
        soft_loss_triggered=soft_loss_triggered,
        hard_loss_triggered=hard_loss_triggered,
        profit_floor_triggered=profit_floor_triggered,
        entry_blocked=entry_blocked,
        data_valid=True,
        reason=reason,
    )


def evaluate_notional_cap(
    request: NotionalCapRequest,
    snapshot: DailyRiskSnapshot,
    *,
    policy: RiskPolicy = DEFAULT_RISK_POLICY,
) -> NotionalCapDecision:
    """Evaluate the Phase-C cap without placing or mutating an order."""

    policy_hash = _policy_hash(policy)
    action = _coerce_action(request.action)

    # A protective reduction must never be disabled by stale entry authority.
    if action is RiskAction.RISK_REDUCING_EXIT and request.active_position is True:
        return NotionalCapDecision(
            decision=RiskDecision.ALLOW,
            reason="risk_reducing_exit_always_allowed",
            notional_cap_usdc=0.0,
            safe_computed_cap_usdc=0.0,
            stage_cap_usdc=0.0,
            risk_limited_cap_usdc=0.0,
            all_in_loss_bp=0.0,
            risk_policy_hash=policy_hash,
            active_day=getattr(snapshot, "active_day", "UNKNOWN"),
        )

    if action is None:
        return _blocked_decision(
            reason="invalid_action",
            policy_hash=policy_hash,
            snapshot=snapshot,
        )
    if action is RiskAction.RISK_REDUCING_EXIT:
        return _blocked_decision(
            reason="risk_reducing_exit_requires_active_position",
            policy_hash=policy_hash,
            snapshot=snapshot,
        )

    stage = _coerce_stage(request.stage)
    if stage is None:
        return _blocked_decision(
            reason="invalid_stage",
            policy_hash=policy_hash,
            snapshot=snapshot,
        )

    numeric_inputs = (
        request.global_cap_usdc,
        request.lane_cap_usdc,
        request.remaining_daily_risk_usdc,
        request.sl_bp,
        request.roundtrip_fee_bp,
        request.slippage_bp,
        request.exchange_min_notional_usdc,
        request.now_ms,
    )
    if not all(_is_nonnegative_finite(value) for value in numeric_inputs):
        return _blocked_decision(
            reason="invalid_numeric_input",
            policy_hash=policy_hash,
            snapshot=snapshot,
        )
    now_ms = int(request.now_ms)

    if request.expected_risk_policy_hash != policy_hash:
        return _blocked_decision(
            reason="risk_policy_hash_mismatch",
            policy_hash=policy_hash,
            snapshot=snapshot,
        )
    snapshot_problem = _snapshot_problem(
        snapshot=snapshot,
        now_ms=now_ms,
        policy_hash=policy_hash,
    )
    if snapshot_problem is not None:
        return _blocked_decision(
            reason=snapshot_problem,
            policy_hash=policy_hash,
            snapshot=snapshot,
        )
    if snapshot.entry_blocked:
        return _blocked_decision(
            reason=snapshot.reason,
            policy_hash=policy_hash,
            snapshot=snapshot,
        )

    all_in_loss_bp = (
        float(request.sl_bp)
        + float(request.roundtrip_fee_bp)
        + float(request.slippage_bp)
    )
    if not math.isfinite(all_in_loss_bp) or all_in_loss_bp <= 0.0:
        return _blocked_decision(
            reason="invalid_all_in_loss_bp",
            policy_hash=policy_hash,
            snapshot=snapshot,
        )

    stage_cap = min(
        STAGE_CAP_USDC[stage],
        policy.paid_notional_cap_usdc,
    )
    effective_remaining_risk = min(
        float(request.remaining_daily_risk_usdc),
        snapshot.remaining_daily_risk_usdc,
        policy.per_trade_loss_cap_usdc,
    )
    risk_limited_cap = effective_remaining_risk / (
        all_in_loss_bp / 10_000.0
    )
    safe_cap = min(
        stage_cap,
        float(request.global_cap_usdc),
        policy.global_open_notional_cap_usdc,
        float(request.lane_cap_usdc),
        policy.lane_open_notional_cap_usdc,
        risk_limited_cap,
    )
    if snapshot.soft_loss_triggered:
        safe_cap = min(safe_cap, SOFT_ENTRY_CAP_USDC)

    if stage is RiskStage.SHADOW:
        return _blocked_decision(
            reason="shadow_stage_has_zero_paid_cap",
            policy_hash=policy_hash,
            snapshot=snapshot,
            safe_cap=safe_cap,
            stage_cap=stage_cap,
            risk_limited_cap=risk_limited_cap,
            all_in_loss_bp=all_in_loss_bp,
        )
    if safe_cap < float(request.exchange_min_notional_usdc):
        return _blocked_decision(
            reason="below_exchange_min_notional",
            policy_hash=policy_hash,
            snapshot=snapshot,
            safe_cap=safe_cap,
            stage_cap=stage_cap,
            risk_limited_cap=risk_limited_cap,
            all_in_loss_bp=all_in_loss_bp,
        )

    return NotionalCapDecision(
        decision=RiskDecision.ALLOW,
        reason=(
            "daily_soft_loss_cap"
            if snapshot.soft_loss_triggered
            else "allowed"
        ),
        notional_cap_usdc=safe_cap,
        safe_computed_cap_usdc=safe_cap,
        stage_cap_usdc=stage_cap,
        risk_limited_cap_usdc=risk_limited_cap,
        all_in_loss_bp=all_in_loss_bp,
        risk_policy_hash=policy_hash,
        active_day=snapshot.active_day,
    )


def _policy_hash(policy: RiskPolicy) -> str:
    try:
        return policy.policy_hash
    except Exception:
        return ""


def _is_nonnegative_finite(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value)) and float(value) >= 0.0


def _is_finite(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(float(value))


def _coerce_stage(value: object) -> RiskStage | None:
    if isinstance(value, RiskStage):
        return value
    try:
        return RiskStage(str(value).strip().upper())
    except (TypeError, ValueError):
        return None


def _coerce_action(value: object) -> RiskAction | None:
    if isinstance(value, RiskAction):
        return value
    try:
        return RiskAction(str(value).strip().upper())
    except (TypeError, ValueError):
        return None


def _valid_event_shape(event: DailyRiskEvent) -> bool:
    return (
        isinstance(event.event_id, str)
        and bool(event.event_id.strip())
        and _is_nonnegative_finite(event.occurred_at_ms)
        and _is_finite(event.fee_net_pnl_delta_usdc)
        and isinstance(event.risk_policy_hash, str)
        and bool(event.risk_policy_hash)
        and event.event_type == "PAID_CLOSED"
    )


def _remaining_daily_risk(
    *,
    closed_pnl: float,
    profit_floor: float | None,
    policy: RiskPolicy,
    entry_blocked: bool,
) -> float:
    if entry_blocked:
        return 0.0
    loss_used = max(0.0, -closed_pnl)
    remaining = max(0.0, policy.daily_hard_loss_cap_usdc - loss_used)
    if profit_floor is not None:
        remaining = min(remaining, max(0.0, closed_pnl - profit_floor))
    return remaining


def _snapshot_problem(
    *,
    snapshot: DailyRiskSnapshot,
    now_ms: int,
    policy_hash: str,
) -> str | None:
    if not isinstance(snapshot, DailyRiskSnapshot):
        return "invalid_risk_snapshot"
    if not snapshot.data_valid:
        return snapshot.reason or "invalid_risk_snapshot"
    if snapshot.risk_policy_hash != policy_hash:
        return "risk_policy_hash_mismatch"
    if not _is_nonnegative_finite(snapshot.as_of_ms):
        return "invalid_risk_snapshot"
    if snapshot.as_of_ms > now_ms:
        return "future_risk_snapshot"
    if now_ms - snapshot.as_of_ms > RISK_SNAPSHOT_MAX_AGE_MS:
        return "stale_risk_snapshot"
    try:
        expected_day = active_day_key(now_ms)
    except ValueError:
        return "invalid_risk_snapshot"
    if snapshot.active_day != expected_day:
        return "risk_snapshot_day_mismatch"
    numeric_fields = (
        snapshot.closed_fee_net_pnl_usdc,
        snapshot.high_water_usdc,
        snapshot.remaining_daily_risk_usdc,
    )
    if not all(_is_finite(value) for value in numeric_fields):
        return "invalid_risk_snapshot"
    if (
        snapshot.high_water_usdc < 0.0
        or snapshot.remaining_daily_risk_usdc < 0.0
        or (
            snapshot.profit_floor_usdc is not None
            and not _is_nonnegative_finite(snapshot.profit_floor_usdc)
        )
        or snapshot.paid_closed_event_count < 0
    ):
        return "invalid_risk_snapshot"
    if (
        (snapshot.hard_loss_triggered or snapshot.profit_floor_triggered)
        and not snapshot.entry_blocked
    ):
        return "invalid_risk_snapshot"
    return None


def _invalid_snapshot(
    *,
    as_of_ms: int,
    active_day: str,
    policy_hash: str,
    reason: str,
) -> DailyRiskSnapshot:
    return DailyRiskSnapshot(
        active_day=active_day,
        as_of_ms=as_of_ms,
        risk_policy_hash=policy_hash,
        evidence_revision="",
        closed_fee_net_pnl_usdc=0.0,
        high_water_usdc=0.0,
        profit_floor_usdc=None,
        remaining_daily_risk_usdc=0.0,
        paid_closed_event_count=0,
        event_ids=(),
        last_event_at_ms=None,
        soft_loss_triggered=False,
        hard_loss_triggered=False,
        profit_floor_triggered=False,
        entry_blocked=True,
        data_valid=False,
        reason=reason,
    )


def _blocked_decision(
    *,
    reason: str,
    policy_hash: str,
    snapshot: object,
    safe_cap: float = 0.0,
    stage_cap: float = 0.0,
    risk_limited_cap: float = 0.0,
    all_in_loss_bp: float = 0.0,
) -> NotionalCapDecision:
    return NotionalCapDecision(
        decision=RiskDecision.BLOCK,
        reason=reason,
        notional_cap_usdc=0.0,
        safe_computed_cap_usdc=safe_cap,
        stage_cap_usdc=stage_cap,
        risk_limited_cap_usdc=risk_limited_cap,
        all_in_loss_bp=all_in_loss_bp,
        risk_policy_hash=policy_hash,
        active_day=getattr(snapshot, "active_day", "UNKNOWN"),
    )


__all__ = [
    "DEFAULT_RISK_POLICY",
    "DailyRiskEvent",
    "DailyRiskSnapshot",
    "NotionalCapDecision",
    "NotionalCapRequest",
    "PHASE_C_SCHEMA",
    "PROFIT_FLOOR_MIN_USDC",
    "RISK_SNAPSHOT_MAX_AGE_MS",
    "RiskAction",
    "RiskDecision",
    "RiskPolicy",
    "RiskStage",
    "SOFT_ENTRY_CAP_USDC",
    "STAGE_CAP_USDC",
    "TAIPEI_TIMEZONE",
    "active_day_key",
    "evaluate_notional_cap",
    "reduce_daily_risk",
]
