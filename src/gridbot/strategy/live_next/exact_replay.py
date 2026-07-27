"""Canonical aggTrade replay with deterministic maker semantics.

Ordering is fixed by ``(transact_time_ms, agg_trade_id)``.  Resting maker
orders require the expected aggressor side and, by default, one tick of
trade-through.  The engine is cache-only and has no network or order API.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from enum import Enum
from hashlib import sha256
from typing import Sequence

from .contracts import (
    ContractError,
    Decision,
    DecisionAction,
    Opportunity,
    Outcome,
    OutcomeStatus,
    Side,
)
from .execution_policy import EntryExecutionMode
from .replay import (
    ExecutionProfile,
    ExitProfile,
    ReplayCostModel,
    ReplayDataError,
    ReplayResult,
)


class MakerFillModel(str, Enum):
    TOUCH = "TOUCH"
    TRADE_THROUGH = "TRADE_THROUGH"


def _decimal(value: object, name: str, *, positive: bool = True) -> Decimal:
    if isinstance(value, bool):
        raise ReplayDataError(f"{name} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ReplayDataError(f"{name} must be decimal-compatible") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise ReplayDataError(f"{name} must be positive and finite")
    return result


@dataclass(frozen=True, slots=True)
class ExactAggTrade:
    transact_time_ms: int
    agg_trade_id: int
    price: Decimal | str | float
    quantity: Decimal | str | float
    is_buyer_maker: bool

    def __post_init__(self) -> None:
        for name in ("transact_time_ms", "agg_trade_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ReplayDataError(f"{name} must be a non-negative integer")
        if not isinstance(self.is_buyer_maker, bool):
            raise ReplayDataError("is_buyer_maker must be boolean")
        object.__setattr__(self, "price", _decimal(self.price, "price"))
        object.__setattr__(self, "quantity", _decimal(self.quantity, "quantity"))

    @property
    def ordering_key(self) -> tuple[int, int]:
        return self.transact_time_ms, self.agg_trade_id


@dataclass(frozen=True, slots=True)
class VerifiedAggTradeWindow:
    trades: tuple[ExactAggTrade, ...]
    source_artifact_sha256: str
    require_contiguous_ids: bool = True

    def __post_init__(self) -> None:
        if not self.trades:
            raise ReplayDataError("verified aggTrade window cannot be empty")
        if (
            not isinstance(self.source_artifact_sha256, str)
            or len(self.source_artifact_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.source_artifact_sha256.lower())
        ):
            raise ReplayDataError("source_artifact_sha256 must be a 64-character hex digest")
        if not isinstance(self.require_contiguous_ids, bool):
            raise ReplayDataError("require_contiguous_ids must be boolean")
        previous: ExactAggTrade | None = None
        for trade in self.trades:
            if not isinstance(trade, ExactAggTrade):
                raise ReplayDataError("window must contain ExactAggTrade values")
            if previous is not None:
                if trade.ordering_key <= previous.ordering_key:
                    raise ReplayDataError("aggTrades must be strictly ordered by (T, a)")
                if trade.agg_trade_id <= previous.agg_trade_id:
                    raise ReplayDataError("aggregate trade IDs must be strictly increasing")
                if self.require_contiguous_ids and trade.agg_trade_id != previous.agg_trade_id + 1:
                    raise ReplayDataError("aggregate trade IDs must be contiguous")
            previous = trade

    @property
    def first_time_ms(self) -> int:
        return self.trades[0].transact_time_ms

    @property
    def last_time_ms(self) -> int:
        return self.trades[-1].transact_time_ms

    @property
    def window_hash(self) -> str:
        digest = sha256()
        for trade in self.trades:
            digest.update(
                (
                    f"{trade.transact_time_ms},{trade.agg_trade_id},"
                    f"{trade.price},{trade.quantity},{int(trade.is_buyer_maker)}\n"
                ).encode("ascii")
            )
        return digest.hexdigest()

    def assert_covers(self, start_ms: int, end_ms: int) -> None:
        if self.first_time_ms > start_ms or self.last_time_ms < end_ms:
            raise ReplayDataError(
                "aggTrade window lacks required pre-start or post-end coverage"
            )


@dataclass(frozen=True, slots=True)
class ExactReplayConfig:
    tick_size: Decimal | str | float
    entry_fill_model: MakerFillModel = MakerFillModel.TRADE_THROUGH
    tp_fill_model: MakerFillModel = MakerFillModel.TRADE_THROUGH
    order_latency_ms: int = 0
    include_same_millisecond: bool = False
    cancel_remainder_on_first_fill: bool = True
    entry_execution_mode: EntryExecutionMode | str = EntryExecutionMode.MAKER
    maker_phase_ms: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "tick_size", _decimal(self.tick_size, "tick_size"))
        object.__setattr__(self, "entry_fill_model", MakerFillModel(self.entry_fill_model))
        object.__setattr__(self, "tp_fill_model", MakerFillModel(self.tp_fill_model))
        object.__setattr__(self, "entry_execution_mode", EntryExecutionMode(self.entry_execution_mode))
        if isinstance(self.order_latency_ms, bool) or not isinstance(self.order_latency_ms, int):
            raise ContractError("order_latency_ms must be an integer")
        if self.order_latency_ms < 0:
            raise ContractError("order_latency_ms must be non-negative")
        if isinstance(self.maker_phase_ms, bool) or not isinstance(self.maker_phase_ms, int):
            raise ContractError("maker_phase_ms must be an integer")
        if self.maker_phase_ms < 0:
            raise ContractError("maker_phase_ms must be non-negative")
        if self.entry_execution_mode is not EntryExecutionMode.HYBRID and self.maker_phase_ms:
            raise ContractError("maker_phase_ms is only valid for HYBRID entry")
        if self.entry_execution_mode is EntryExecutionMode.HYBRID and self.maker_phase_ms <= 0:
            raise ContractError("HYBRID entry requires a positive maker_phase_ms")
        for name in ("include_same_millisecond", "cancel_remainder_on_first_fill"):
            if not isinstance(getattr(self, name), bool):
                raise ContractError(f"{name} must be boolean")


@dataclass(frozen=True, slots=True)
class ExactReplayResult:
    candidate_id: str
    opportunity_id: str
    decision_id: str
    entry_limit_price: float | None
    entry_filled_fraction: float
    entry_fill_trade_id: int | None
    exit_trade_id: int | None
    max_favorable_excursion_bps: float
    max_adverse_excursion_bps: float
    outcome: Outcome
    entry_liquidity: str | None = None

    def as_replay_result(self) -> ReplayResult:
        return ReplayResult(
            candidate_id=self.candidate_id,
            opportunity_id=self.opportunity_id,
            decision_id=self.decision_id,
            entry_limit_price=self.entry_limit_price,
            max_favorable_excursion_bps=self.max_favorable_excursion_bps,
            max_adverse_excursion_bps=self.max_adverse_excursion_bps,
            outcome=self.outcome,
        )


def replay_exact_aggtrades(
    *,
    candidate_id: str,
    opportunity: Opportunity,
    decision: Decision,
    reference_price: Decimal | str | float,
    window: VerifiedAggTradeWindow,
    execution: ExecutionProfile,
    exit_profile: ExitProfile,
    cost_model: ReplayCostModel,
    notional_usdc: Decimal | str | float,
    config: ExactReplayConfig,
) -> ExactReplayResult:
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

    if decision.action is not DecisionAction.ACCEPT:
        status = OutcomeStatus.BLOCKED if decision.action is DecisionAction.BLOCK else OutcomeStatus.SKIPPED
        outcome = Outcome.create(
            decision,
            outcome_at_ms=decision.decided_at_ms,
            status=status,
        )
        return ExactReplayResult(
            candidate_id,
            opportunity.opportunity_id,
            decision.decision_id,
            None,
            0.0,
            None,
            None,
            0.0,
            0.0,
            outcome,
        )

    reference = _decimal(reference_price, "reference_price")
    notional = _decimal(notional_usdc, "notional_usdc")
    live_at_ms = decision.decided_at_ms + config.order_latency_ms
    entry_expiry_ms = live_at_ms + execution.entry_ttl_ms
    window.assert_covers(decision.decided_at_ms, entry_expiry_ms)
    mode = config.entry_execution_mode
    if mode is EntryExecutionMode.TAKER_CONFIRM and execution.entry_offset_bps != 0:
        raise ContractError("taker confirmation requires zero entry offset")
    if mode is EntryExecutionMode.HYBRID and config.maker_phase_ms >= execution.entry_ttl_ms:
        raise ContractError("hybrid maker phase must end before entry TTL")

    entry_limit = _entry_limit_exact(
        reference,
        opportunity.side,
        Decimal(str(execution.entry_offset_bps)),
        config.tick_size,
    )
    entry_trade: ExactAggTrade | None = None
    entry_price = entry_limit
    entry_liquidity: str | None = None

    if mode in {EntryExecutionMode.MAKER, EntryExecutionMode.HYBRID}:
        maker_deadline_ms = (
            entry_expiry_ms
            if mode is EntryExecutionMode.MAKER
            else live_at_ms + config.maker_phase_ms
        )
        entry_trade = next(
            (
                trade
                for trade in window.trades
                if _after_order_live(
                    trade,
                    live_at_ms,
                    config.include_same_millisecond,
                )
                and trade.transact_time_ms <= maker_deadline_ms
                and _maker_entry_fill(
                    trade,
                    entry_limit,
                    opportunity.side,
                    config.tick_size,
                    config.entry_fill_model,
                )
            ),
            None,
        )
        if entry_trade is not None:
            entry_liquidity = "MAKER"

    if entry_trade is None and mode in {
        EntryExecutionMode.TAKER_CONFIRM,
        EntryExecutionMode.HYBRID,
    }:
        taker_live_ms = (
            live_at_ms
            if mode is EntryExecutionMode.TAKER_CONFIRM
            else live_at_ms + config.maker_phase_ms
        )
        entry_trade = next(
            (
                trade
                for trade in window.trades
                if _after_order_live(
                    trade,
                    taker_live_ms,
                    config.include_same_millisecond,
                )
                and trade.transact_time_ms <= entry_expiry_ms
            ),
            None,
        )
        if entry_trade is not None:
            entry_price = entry_trade.price
            entry_liquidity = "TAKER"

    if entry_trade is None or entry_liquidity is None:
        outcome = Outcome.create(
            decision,
            outcome_at_ms=entry_expiry_ms,
            status=OutcomeStatus.ENTRY_EXPIRED,
        )
        return ExactReplayResult(
            candidate_id,
            opportunity.opportunity_id,
            decision.decision_id,
            float(entry_limit),
            0.0,
            None,
            None,
            0.0,
            0.0,
            outcome,
        )

    cost_model.assert_economic(exit_profile, entry_liquidity)
    desired_quantity = notional / entry_price
    if entry_liquidity == "MAKER":
        if not config.cancel_remainder_on_first_fill and entry_trade.quantity < desired_quantity:
            raise ReplayDataError(
                "multi-event partial fill replay is intentionally unsupported; fail closed"
            )
        filled_quantity = min(entry_trade.quantity, desired_quantity)
        filled_fraction = filled_quantity / desired_quantity
    else:
        filled_quantity = desired_quantity
        filled_fraction = Decimal("1")
    tp_price, sl_price = _exit_prices_exact(
        entry_price,
        opportunity.side,
        Decimal(str(exit_profile.take_profit_bps)),
        Decimal(str(exit_profile.stop_loss_bps)),
        config.tick_size,
    )
    terminal: tuple[ExactAggTrade, Decimal, str] | None = None
    max_favorable = Decimal("0")
    max_adverse = Decimal("0")
    for trade in window.trades:
        if trade.ordering_key <= entry_trade.ordering_key:
            continue
        favorable, adverse = _excursions_exact(entry_price, trade.price, opportunity.side)
        max_favorable = max(max_favorable, favorable)
        max_adverse = max(max_adverse, adverse)
        if _active_sl_touch(trade.price, sl_price, opportunity.side):
            terminal = (trade, sl_price, "SL")
            break
        if _maker_tp_fill(
            trade,
            tp_price,
            opportunity.side,
            config.tick_size,
            config.tp_fill_model,
        ):
            terminal = (trade, tp_price, "TP")
            break
        held_ms = trade.transact_time_ms - entry_trade.transact_time_ms
        if held_ms >= exit_profile.t1_ms and max_favorable < Decimal(str(exit_profile.t1_min_mfe_bps)):
            terminal = (trade, trade.price, "T1_NO_MFE")
            break
        if held_ms >= exit_profile.t2_ms:
            terminal = (trade, trade.price, "T2_MAX_HOLD")
            break

    coverage_end_ms = entry_trade.transact_time_ms + exit_profile.t2_ms
    if terminal is None:
        window.assert_covers(decision.decided_at_ms, coverage_end_ms)
        raise ReplayDataError("terminal replay invariant failed despite complete coverage")

    exit_trade, exit_price, exit_reason = terminal
    gross_decimal = _gross_pnl_exact(
        entry_price, exit_price, filled_quantity, opportunity.side
    )
    entry_notional = entry_price * filled_quantity
    exit_notional = exit_price * filled_quantity
    all_in_cost = cost_model.all_in_cost_usdc(
        float(entry_notional),
        float(exit_notional),
        exit_reason,
        entry_liquidity,
    )
    gross = float(gross_decimal)
    net = gross - all_in_cost
    outcome = Outcome.create(
        decision,
        outcome_at_ms=exit_trade.transact_time_ms,
        status=OutcomeStatus.CLOSED,
        filled=True,
        entry_filled_at_ms=entry_trade.transact_time_ms,
        closed_at_ms=exit_trade.transact_time_ms,
        entry_price=float(entry_price),
        exit_price=float(exit_price),
        quantity=float(filled_quantity),
        exit_reason=exit_reason,
        gross_pnl_usdc=gross,
        all_in_cost_usdc=all_in_cost,
        net_pnl_usdc=net,
    )
    return ExactReplayResult(
        candidate_id=candidate_id,
        opportunity_id=opportunity.opportunity_id,
        decision_id=decision.decision_id,
        entry_limit_price=float(entry_price),
        entry_filled_fraction=float(filled_fraction),
        entry_fill_trade_id=entry_trade.agg_trade_id,
        exit_trade_id=exit_trade.agg_trade_id,
        max_favorable_excursion_bps=float(max_favorable),
        max_adverse_excursion_bps=float(max_adverse),
        outcome=outcome,
        entry_liquidity=entry_liquidity,
    )


def _after_order_live(
    trade: ExactAggTrade, live_at_ms: int, include_same_millisecond: bool
) -> bool:
    return (
        trade.transact_time_ms >= live_at_ms
        if include_same_millisecond
        else trade.transact_time_ms > live_at_ms
    )


def _quantize_floor(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def _quantize_ceil(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def _entry_limit_exact(
    reference: Decimal, side: Side, offset_bps: Decimal, tick: Decimal
) -> Decimal:
    factor = offset_bps / Decimal("10000")
    raw = reference * (Decimal("1") - factor if side is Side.LONG else Decimal("1") + factor)
    return _quantize_floor(raw, tick) if side is Side.LONG else _quantize_ceil(raw, tick)


def _exit_prices_exact(
    entry: Decimal,
    side: Side,
    tp_bps: Decimal,
    sl_bps: Decimal,
    tick: Decimal,
) -> tuple[Decimal, Decimal]:
    tp_factor = tp_bps / Decimal("10000")
    sl_factor = sl_bps / Decimal("10000")
    if side is Side.LONG:
        return (
            _quantize_floor(entry * (Decimal("1") + tp_factor), tick),
            _quantize_floor(entry * (Decimal("1") - sl_factor), tick),
        )
    return (
        _quantize_ceil(entry * (Decimal("1") - tp_factor), tick),
        _quantize_ceil(entry * (Decimal("1") + sl_factor), tick),
    )


def _maker_entry_fill(
    trade: ExactAggTrade,
    limit: Decimal,
    side: Side,
    tick: Decimal,
    model: MakerFillModel,
) -> bool:
    if side is Side.LONG:
        if not trade.is_buyer_maker:
            return False
        threshold = limit - tick if model is MakerFillModel.TRADE_THROUGH else limit
        return trade.price <= threshold
    if trade.is_buyer_maker:
        return False
    threshold = limit + tick if model is MakerFillModel.TRADE_THROUGH else limit
    return trade.price >= threshold


def _maker_tp_fill(
    trade: ExactAggTrade,
    target: Decimal,
    side: Side,
    tick: Decimal,
    model: MakerFillModel,
) -> bool:
    if side is Side.LONG:
        if trade.is_buyer_maker:
            return False
        threshold = target + tick if model is MakerFillModel.TRADE_THROUGH else target
        return trade.price >= threshold
    if not trade.is_buyer_maker:
        return False
    threshold = target - tick if model is MakerFillModel.TRADE_THROUGH else target
    return trade.price <= threshold


def _active_sl_touch(price: Decimal, stop: Decimal, side: Side) -> bool:
    return price <= stop if side is Side.LONG else price >= stop


def _excursions_exact(
    entry: Decimal, price: Decimal, side: Side
) -> tuple[Decimal, Decimal]:
    signed = (price - entry) / entry * Decimal("10000")
    if side is Side.SHORT:
        signed *= Decimal("-1")
    return max(signed, Decimal("0")), max(-signed, Decimal("0"))


def _gross_pnl_exact(
    entry: Decimal, exit_price: Decimal, quantity: Decimal, side: Side
) -> Decimal:
    signed = exit_price - entry
    if side is Side.SHORT:
        signed *= Decimal("-1")
    return signed * quantity


__all__ = [
    "ExactAggTrade",
    "ExactReplayConfig",
    "ExactReplayResult",
    "MakerFillModel",
    "VerifiedAggTradeWindow",
    "replay_exact_aggtrades",
]
