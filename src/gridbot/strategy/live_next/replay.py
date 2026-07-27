"""Deterministic first-touch replay for bounded Live Next candidates."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from math import isfinite
from typing import Iterable, Sequence

from .contracts import (
    ContractError,
    Decision,
    DecisionAction,
    Opportunity,
    Outcome,
    OutcomeStatus,
    Side,
)


class ReplayDataError(ContractError):
    """Raised when the supplied future stream cannot prove a terminal result."""


@dataclass(frozen=True, slots=True)
class PricePoint:
    event_time_ms: int
    price: float

    def __post_init__(self) -> None:
        if isinstance(self.event_time_ms, bool) or not isinstance(self.event_time_ms, int):
            raise ReplayDataError("event_time_ms must be an integer")
        if self.event_time_ms < 0:
            raise ReplayDataError("event_time_ms must be non-negative")
        if isinstance(self.price, bool) or not isinstance(self.price, (int, float)):
            raise ReplayDataError("price must be numeric")
        if not isfinite(float(self.price)) or float(self.price) <= 0:
            raise ReplayDataError("price must be positive and finite")


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    profile_id: str
    entry_offset_bps: float
    entry_ttl_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not self.profile_id.strip():
            raise ContractError("execution profile_id is required")
        _non_negative_float(self.entry_offset_bps, "entry_offset_bps")
        if isinstance(self.entry_ttl_ms, bool) or not isinstance(self.entry_ttl_ms, int):
            raise ContractError("entry_ttl_ms must be an integer")
        if self.entry_ttl_ms <= 0:
            raise ContractError("entry_ttl_ms must be positive")


@dataclass(frozen=True, slots=True)
class ExitProfile:
    profile_id: str
    take_profit_bps: float
    stop_loss_bps: float
    t1_ms: int
    t1_min_mfe_bps: float
    t2_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.profile_id, str) or not self.profile_id.strip():
            raise ContractError("exit profile_id is required")
        for name in ("take_profit_bps", "stop_loss_bps", "t1_min_mfe_bps"):
            _non_negative_float(getattr(self, name), name)
        if self.take_profit_bps <= 0 or self.stop_loss_bps <= 0:
            raise ContractError("TP and SL must be positive")
        for name in ("t1_ms", "t2_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ContractError(f"{name} must be a positive integer")
        if self.t2_ms <= self.t1_ms:
            raise ContractError("t2_ms must follow t1_ms")


_ACTIVE_EXIT_REASONS = frozenset({"SL", "T1_NO_MFE", "T2_MAX_HOLD"})
_KNOWN_EXIT_REASONS = frozenset({"TP", *_ACTIVE_EXIT_REASONS})
_KNOWN_ENTRY_LIQUIDITY = frozenset({"MAKER", "TAKER"})


@dataclass(frozen=True, slots=True)
class ReplayCostModel:
    entry_fee_bps: float
    exit_fee_bps: float
    spread_slippage_bps: float
    adverse_selection_buffer_bps: float = 0.0
    active_exit_fee_bps: float | None = None
    active_exit_slippage_bps: float = 0.0
    funding_cost_usdc_per_fill: float = 0.0
    taker_entry_fee_bps: float | None = None
    taker_entry_slippage_bps: float = 0.0

    def __post_init__(self) -> None:
        for name in (
            "entry_fee_bps",
            "exit_fee_bps",
            "spread_slippage_bps",
            "adverse_selection_buffer_bps",
        ):
            _non_negative_float(getattr(self, name), name)
        active_fee = self.active_exit_fee_bps
        if active_fee is None:
            active_fee = float(self.exit_fee_bps)
        else:
            active_fee = _non_negative_float(active_fee, "active_exit_fee_bps")
        object.__setattr__(self, "active_exit_fee_bps", active_fee)
        _non_negative_float(self.active_exit_slippage_bps, "active_exit_slippage_bps")
        _non_negative_float(
            self.funding_cost_usdc_per_fill,
            "funding_cost_usdc_per_fill",
        )
        taker_fee = self.taker_entry_fee_bps
        if taker_fee is None:
            taker_fee = float(self.entry_fee_bps)
        else:
            taker_fee = _non_negative_float(
                taker_fee,
                "taker_entry_fee_bps",
            )
        object.__setattr__(self, "taker_entry_fee_bps", taker_fee)
        _non_negative_float(
            self.taker_entry_slippage_bps,
            "taker_entry_slippage_bps",
        )

    @property
    def round_trip_bps(self) -> float:
        return (
            float(self.entry_fee_bps)
            + float(self.exit_fee_bps)
            + float(self.spread_slippage_bps)
        )

    @property
    def minimum_economic_tp_bps(self) -> float:
        return self.round_trip_bps + float(self.adverse_selection_buffer_bps)

    @staticmethod
    def _validated_entry_liquidity(entry_liquidity: object) -> str:
        if not isinstance(entry_liquidity, str):
            raise ContractError(
                f"unknown entry_liquidity: {entry_liquidity!r}"
            )
        normalized = entry_liquidity.upper()
        if normalized not in _KNOWN_ENTRY_LIQUIDITY:
            raise ContractError(
                f"unknown entry_liquidity: {entry_liquidity!r}"
            )
        return normalized

    @staticmethod
    def _validated_exit_reason(exit_reason: object) -> str:
        if not isinstance(exit_reason, str) or exit_reason not in _KNOWN_EXIT_REASONS:
            raise ContractError(f"unknown exit_reason: {exit_reason!r}")
        return exit_reason

    def entry_fee_bps_for(self, entry_liquidity: object) -> float:
        liquidity = self._validated_entry_liquidity(entry_liquidity)
        if liquidity == "MAKER":
            return float(self.entry_fee_bps)
        assert self.taker_entry_fee_bps is not None
        return float(self.taker_entry_fee_bps)

    def entry_slippage_bps_for(self, entry_liquidity: object) -> float:
        liquidity = self._validated_entry_liquidity(entry_liquidity)
        taker = (
            float(self.taker_entry_slippage_bps)
            if liquidity == "TAKER"
            else 0.0
        )
        return float(self.spread_slippage_bps) + taker

    def exit_fee_bps_for(self, exit_reason: object) -> float:
        reason = self._validated_exit_reason(exit_reason)
        if reason == "TP":
            return float(self.exit_fee_bps)
        assert self.active_exit_fee_bps is not None
        return float(self.active_exit_fee_bps)

    def slippage_bps_for(self, exit_reason: object) -> float:
        reason = self._validated_exit_reason(exit_reason)
        return (
            float(self.active_exit_slippage_bps)
            if reason in _ACTIVE_EXIT_REASONS
            else 0.0
        )

    def minimum_economic_tp_bps_for(self, entry_liquidity: object) -> float:
        return (
            self.entry_fee_bps_for(entry_liquidity)
            + self.entry_slippage_bps_for(entry_liquidity)
            + float(self.exit_fee_bps)
            + float(self.adverse_selection_buffer_bps)
        )

    def assert_economic(
        self,
        exit_profile: ExitProfile,
        entry_liquidity: object = "MAKER",
    ) -> None:
        if (
            exit_profile.take_profit_bps
            <= self.minimum_economic_tp_bps_for(entry_liquidity)
        ):
            raise ContractError(
                "take_profit_bps must exceed round-trip cost plus adverse-selection buffer"
            )

    def cost_components(
        self,
        entry_notional_usdc: object,
        exit_notional_usdc: object,
        exit_reason: object,
        entry_liquidity: object = "MAKER",
    ) -> dict[str, float]:
        entry_notional = _non_negative_float(
            entry_notional_usdc,
            "entry_notional_usdc",
        )
        exit_notional = _non_negative_float(
            exit_notional_usdc,
            "exit_notional_usdc",
        )
        reason = self._validated_exit_reason(exit_reason)
        liquidity = self._validated_entry_liquidity(entry_liquidity)
        return {
            "entry_fee_usdc": (
                entry_notional * self.entry_fee_bps_for(liquidity) / 10_000.0
            ),
            "exit_fee_usdc": (
                exit_notional * self.exit_fee_bps_for(reason) / 10_000.0
            ),
            "spread_slippage_usdc": (
                entry_notional
                * self.entry_slippage_bps_for(liquidity)
                / 10_000.0
            ),
            "active_exit_slippage_usdc": (
                exit_notional * self.slippage_bps_for(reason) / 10_000.0
            ),
            "funding_cost_usdc": float(self.funding_cost_usdc_per_fill),
        }

    def all_in_cost_usdc(
        self,
        entry_notional_usdc: object,
        exit_notional_usdc: object,
        exit_reason: object,
        entry_liquidity: object = "MAKER",
    ) -> float:
        return sum(
            self.cost_components(
                entry_notional_usdc,
                exit_notional_usdc,
                exit_reason,
                entry_liquidity,
            ).values()
        )

    def stressed(
        self,
        *,
        fee_multiplier: float = 1.0,
        extra_latency_cost_bps: float = 0.0,
    ) -> "ReplayCostModel":
        if fee_multiplier < 1.0:
            raise ContractError("fee_multiplier cannot reduce baseline fees")
        _non_negative_float(extra_latency_cost_bps, "extra_latency_cost_bps")
        assert self.active_exit_fee_bps is not None
        assert self.taker_entry_fee_bps is not None
        return ReplayCostModel(
            entry_fee_bps=self.entry_fee_bps * fee_multiplier,
            exit_fee_bps=self.exit_fee_bps * fee_multiplier,
            spread_slippage_bps=(
                self.spread_slippage_bps + extra_latency_cost_bps
            ),
            adverse_selection_buffer_bps=self.adverse_selection_buffer_bps,
            active_exit_fee_bps=(
                float(self.active_exit_fee_bps) * fee_multiplier
            ),
            active_exit_slippage_bps=self.active_exit_slippage_bps,
            funding_cost_usdc_per_fill=self.funding_cost_usdc_per_fill,
            taker_entry_fee_bps=(
                float(self.taker_entry_fee_bps) * fee_multiplier
            ),
            taker_entry_slippage_bps=self.taker_entry_slippage_bps,
        )


@dataclass(frozen=True, slots=True)
class ReplayResult:
    candidate_id: str
    opportunity_id: str
    decision_id: str
    entry_limit_price: float | None
    max_favorable_excursion_bps: float
    max_adverse_excursion_bps: float
    outcome: Outcome


@dataclass(frozen=True, slots=True)
class ReplayMetrics:
    opportunities: int
    accepted: int
    placed: int
    fills: int
    closed: int
    wins: int
    raw_win_rate: float | None
    gross_pnl_usdc: float
    all_in_cost_usdc: float
    net_pnl_usdc: float
    ev_per_opportunity_usdc: float
    max_hold_share: float | None
    exit_reason_counts: dict[str, int]
    terminal_status_counts: dict[str, int]


def replay_decision(
    *,
    candidate_id: str,
    opportunity: Opportunity,
    decision: Decision,
    reference_price: float,
    price_points: Sequence[PricePoint],
    execution: ExecutionProfile,
    exit_profile: ExitProfile,
    cost_model: ReplayCostModel,
    notional_usdc: float,
) -> ReplayResult:
    """Replay one already-made decision against later market events only."""

    if decision.opportunity_id != opportunity.opportunity_id:
        raise ContractError("decision and opportunity identity mismatch")
    if decision.feature_hash != opportunity.feature_hash:
        raise ContractError("decision and opportunity feature hash mismatch")
    if decision.execution_profile_id not in (None, execution.profile_id):
        raise ContractError("decision execution profile mismatch")
    if decision.exit_profile_id not in (None, exit_profile.profile_id):
        raise ContractError("decision exit profile mismatch")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ContractError("candidate_id is required")
    reference_price = _positive_float(reference_price, "reference_price")
    notional_usdc = _positive_float(notional_usdc, "notional_usdc")
    points = _validated_points(price_points, decision.decided_at_ms)

    if decision.action is not DecisionAction.ACCEPT:
        status = (
            OutcomeStatus.BLOCKED
            if decision.action is DecisionAction.BLOCK
            else OutcomeStatus.SKIPPED
        )
        outcome = Outcome.create(
            decision,
            outcome_at_ms=decision.decided_at_ms,
            status=status,
        )
        return ReplayResult(
            candidate_id=candidate_id,
            opportunity_id=opportunity.opportunity_id,
            decision_id=decision.decision_id,
            entry_limit_price=None,
            max_favorable_excursion_bps=0.0,
            max_adverse_excursion_bps=0.0,
            outcome=outcome,
        )

    cost_model.assert_economic(exit_profile)
    entry_limit = _entry_limit(reference_price, opportunity.side, execution.entry_offset_bps)
    entry_expiry_ms = decision.decided_at_ms + execution.entry_ttl_ms
    entry_point = next(
        (
            point
            for point in points
            if point.event_time_ms <= entry_expiry_ms
            and _entry_touched(point.price, entry_limit, opportunity.side)
        ),
        None,
    )
    if entry_point is None:
        if points[-1].event_time_ms < entry_expiry_ms:
            raise ReplayDataError("price stream ends before entry TTL can be resolved")
        outcome = Outcome.create(
            decision,
            outcome_at_ms=entry_expiry_ms,
            status=OutcomeStatus.ENTRY_EXPIRED,
        )
        return ReplayResult(
            candidate_id=candidate_id,
            opportunity_id=opportunity.opportunity_id,
            decision_id=decision.decision_id,
            entry_limit_price=entry_limit,
            max_favorable_excursion_bps=0.0,
            max_adverse_excursion_bps=0.0,
            outcome=outcome,
        )

    fill_time_ms = entry_point.event_time_ms
    tp_price, sl_price = _exit_prices(
        entry_limit,
        opportunity.side,
        exit_profile.take_profit_bps,
        exit_profile.stop_loss_bps,
    )
    max_favorable_bps = 0.0
    max_adverse_bps = 0.0
    terminal: tuple[PricePoint, float, str] | None = None
    for point in points:
        if point.event_time_ms < fill_time_ms:
            continue
        favorable, adverse = _excursions_bps(entry_limit, point.price, opportunity.side)
        max_favorable_bps = max(max_favorable_bps, favorable)
        max_adverse_bps = max(max_adverse_bps, adverse)
        if _tp_touched(point.price, tp_price, opportunity.side):
            terminal = (point, tp_price, "TP")
            break
        if _sl_touched(point.price, sl_price, opportunity.side):
            terminal = (point, sl_price, "SL")
            break
        held_ms = point.event_time_ms - fill_time_ms
        if held_ms >= exit_profile.t1_ms and max_favorable_bps < exit_profile.t1_min_mfe_bps:
            terminal = (point, point.price, "T1_NO_MFE")
            break
        if held_ms >= exit_profile.t2_ms:
            terminal = (point, point.price, "T2_MAX_HOLD")
            break

    coverage_required_ms = fill_time_ms + exit_profile.t2_ms
    if terminal is None:
        if points[-1].event_time_ms < coverage_required_ms:
            raise ReplayDataError("price stream ends before T2 MAX_HOLD can be resolved")
        raise ReplayDataError("terminal replay invariant failed despite complete coverage")

    terminal_point, exit_price, exit_reason = terminal
    quantity = notional_usdc / entry_limit
    gross_pnl = _gross_pnl(entry_limit, exit_price, quantity, opportunity.side)
    entry_notional = entry_limit * quantity
    exit_notional = exit_price * quantity
    all_in_cost = cost_model.all_in_cost_usdc(
        entry_notional,
        exit_notional,
        exit_reason,
    )
    net_pnl = gross_pnl - all_in_cost
    outcome = Outcome.create(
        decision,
        outcome_at_ms=terminal_point.event_time_ms,
        status=OutcomeStatus.CLOSED,
        filled=True,
        entry_filled_at_ms=fill_time_ms,
        closed_at_ms=terminal_point.event_time_ms,
        entry_price=entry_limit,
        exit_price=exit_price,
        quantity=quantity,
        exit_reason=exit_reason,
        gross_pnl_usdc=gross_pnl,
        all_in_cost_usdc=all_in_cost,
        net_pnl_usdc=net_pnl,
    )
    return ReplayResult(
        candidate_id=candidate_id,
        opportunity_id=opportunity.opportunity_id,
        decision_id=decision.decision_id,
        entry_limit_price=entry_limit,
        max_favorable_excursion_bps=max_favorable_bps,
        max_adverse_excursion_bps=max_adverse_bps,
        outcome=outcome,
    )


def summarize_results(results: Iterable[ReplayResult]) -> ReplayMetrics:
    rows = list(results)
    opportunity_ids = {row.opportunity_id for row in rows}
    if len(opportunity_ids) != len(rows):
        raise ContractError("replay results must contain one paid decision per opportunity")
    outcomes = [row.outcome for row in rows]
    accepted = sum(
        outcome.status
        not in (OutcomeStatus.SKIPPED, OutcomeStatus.BLOCKED)
        for outcome in outcomes
    )
    placed = accepted
    fills = sum(outcome.filled for outcome in outcomes)
    closed_outcomes = [
        outcome for outcome in outcomes if outcome.status is OutcomeStatus.CLOSED
    ]
    wins = sum(outcome.is_win for outcome in closed_outcomes)
    gross = sum(outcome.gross_pnl_usdc for outcome in outcomes)
    cost = sum(outcome.all_in_cost_usdc for outcome in outcomes)
    net = sum(outcome.net_pnl_usdc for outcome in outcomes)
    exit_reasons = Counter(
        outcome.exit_reason for outcome in closed_outcomes if outcome.exit_reason
    )
    statuses = Counter(outcome.status.value for outcome in outcomes)
    closed = len(closed_outcomes)
    max_hold = exit_reasons["T2_MAX_HOLD"]
    return ReplayMetrics(
        opportunities=len(rows),
        accepted=accepted,
        placed=placed,
        fills=fills,
        closed=closed,
        wins=wins,
        raw_win_rate=wins / closed if closed else None,
        gross_pnl_usdc=gross,
        all_in_cost_usdc=cost,
        net_pnl_usdc=net,
        ev_per_opportunity_usdc=net / len(rows) if rows else 0.0,
        max_hold_share=max_hold / closed if closed else None,
        exit_reason_counts=dict(sorted(exit_reasons.items())),
        terminal_status_counts=dict(sorted(statuses.items())),
    )


def _validated_points(
    points: Sequence[PricePoint], decided_at_ms: int
) -> tuple[PricePoint, ...]:
    if not points:
        raise ReplayDataError("price stream cannot be empty")
    normalized = tuple(points)
    previous = decided_at_ms
    for point in normalized:
        if not isinstance(point, PricePoint):
            raise ReplayDataError("price stream must contain PricePoint values")
        if point.event_time_ms < decided_at_ms:
            raise ReplayDataError("future stream contains pre-decision market data")
        if point.event_time_ms < previous:
            raise ReplayDataError("price stream must be sorted by event time")
        previous = point.event_time_ms
    return normalized


def _entry_limit(reference: float, side: Side, offset_bps: float) -> float:
    factor = float(offset_bps) / 10_000.0
    return reference * (1.0 - factor if side is Side.LONG else 1.0 + factor)


def _entry_touched(price: float, limit: float, side: Side) -> bool:
    return price <= limit if side is Side.LONG else price >= limit


def _exit_prices(
    entry: float, side: Side, tp_bps: float, sl_bps: float
) -> tuple[float, float]:
    tp = float(tp_bps) / 10_000.0
    sl = float(sl_bps) / 10_000.0
    if side is Side.LONG:
        return entry * (1.0 + tp), entry * (1.0 - sl)
    return entry * (1.0 - tp), entry * (1.0 + sl)


def _tp_touched(price: float, target: float, side: Side) -> bool:
    return price >= target if side is Side.LONG else price <= target


def _sl_touched(price: float, stop: float, side: Side) -> bool:
    return price <= stop if side is Side.LONG else price >= stop


def _excursions_bps(entry: float, price: float, side: Side) -> tuple[float, float]:
    signed = (price - entry) / entry * 10_000.0
    if side is Side.SHORT:
        signed *= -1.0
    return max(signed, 0.0), max(-signed, 0.0)


def _gross_pnl(entry: float, exit_price: float, quantity: float, side: Side) -> float:
    signed = exit_price - entry
    if side is Side.SHORT:
        signed *= -1.0
    return signed * quantity


def _non_negative_float(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{name} must be numeric")
    result = float(value)
    if not isfinite(result) or result < 0:
        raise ContractError(f"{name} must be non-negative and finite")
    return result


def _positive_float(value: object, name: str) -> float:
    result = _non_negative_float(value, name)
    if result <= 0:
        raise ContractError(f"{name} must be positive")
    return result


__all__ = [
    "ExecutionProfile",
    "ExitProfile",
    "PricePoint",
    "ReplayCostModel",
    "ReplayDataError",
    "ReplayMetrics",
    "ReplayResult",
    "replay_decision",
    "summarize_results",
]
