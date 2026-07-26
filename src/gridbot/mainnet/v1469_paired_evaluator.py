"""Pure paired shadow evaluator for v1.4.69 adaptive arms.

The evaluator consumes one immutable market-path envelope and applies every
legal arm profile to that exact envelope.  It has no exchange/order API,
storage, settings, or live-enforcement dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from itertools import groupby
from typing import Any, Literal, TypeAlias

from src.gridbot.mainnet.v1469_adaptive_identity import (
    ExecutionProfile,
    MarketStateIdentity,
    canonical_sha256,
)
from src.gridbot.mainnet.v1469_arm_profiles import (
    RISK_OFF,
    ArmProfileDefinition,
    arm_identity_hash,
    profiles_for_matched_candidate,
)


PAIRED_EVALUATOR_SCHEMA = "v1469.paired-evaluator.1"
TICK_ENVELOPE_SCHEMA = "v1469.tick-envelope.1"
COST_MODEL_SCHEMA = "v1469.shadow-cost-model.1"
TERMINAL_RESULT_SCHEMA = "v1469.arm-terminal-result.1"

_BPS = 10_000.0
_FLOAT_QUANTUM = Decimal("0.000001")
_CANDIDATE_STATUSES = frozenset({"SAFE", "NOT_EVALUATED"})

FillStatus = Literal["RISK_OFF", "NO_FILL", "FILLED", "INCOMPLETE"]
TerminalReason = Literal[
    "RISK_OFF",
    "NO_FILL",
    "TP",
    "SL",
    "MAX_HOLD",
    "AMBIGUOUS_BOTH",
    "DATA_INCOMPLETE",
]

_REPOSITORY_OUTCOME = {
    "NO_FILL": "no_fill",
    "TP": "tp",
    "SL": "sl",
    "MAX_HOLD": "max_hold",
    "AMBIGUOUS_BOTH": "ambiguous_both",
    "DATA_INCOMPLETE": "data_incomplete",
}


def _finite_float(value: Any, field_name: str) -> float:
    if value is None or value == "" or isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite number") from exc
    if not number.is_finite():
        raise ValueError(f"{field_name} must be a finite number")
    try:
        rounded = number.quantize(_FLOAT_QUANTUM, rounding=ROUND_HALF_EVEN)
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} is outside the numeric range") from exc
    if rounded == 0:
        rounded = Decimal(0)
    return float(rounded)


def _positive_float(value: Any, field_name: str) -> float:
    number = _finite_float(value, field_name)
    if number <= 0.0:
        raise ValueError(f"{field_name} must be positive")
    return number


def _nonnegative_float(value: Any, field_name: str) -> float:
    number = _finite_float(value, field_name)
    if number < 0.0:
        raise ValueError(f"{field_name} must be non-negative")
    return number


def _nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _required_text(value: Any, field_name: str) -> str:
    rendered = str(value or "").strip()
    if not rendered:
        raise ValueError(f"{field_name} is required")
    return rendered


@dataclass(frozen=True, slots=True, kw_only=True)
class BookPathTick:
    timestamp_ms: int
    available_at_ms: int
    bid_price: float
    ask_price: float
    mark_price: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "timestamp_ms",
            _nonnegative_int(self.timestamp_ms, "book_tick.timestamp_ms"),
        )
        object.__setattr__(
            self,
            "available_at_ms",
            _nonnegative_int(
                self.available_at_ms, "book_tick.available_at_ms"
            ),
        )
        for name in ("bid_price", "ask_price", "mark_price"):
            object.__setattr__(
                self,
                name,
                _positive_float(getattr(self, name), f"book_tick.{name}"),
            )
        if self.available_at_ms < self.timestamp_ms:
            raise ValueError("book tick cannot be available before its timestamp")
        if self.bid_price > self.ask_price:
            raise ValueError("book tick bid_price must not exceed ask_price")

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": "BOOK",
            "timestamp_ms": self.timestamp_ms,
            "available_at_ms": self.available_at_ms,
            "bid_price": self.bid_price,
            "ask_price": self.ask_price,
            "mark_price": self.mark_price,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class AggTradePathTick:
    timestamp_ms: int
    available_at_ms: int
    aggregate_trade_id: int
    price: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "timestamp_ms",
            _nonnegative_int(self.timestamp_ms, "agg_tick.timestamp_ms"),
        )
        object.__setattr__(
            self,
            "available_at_ms",
            _nonnegative_int(
                self.available_at_ms, "agg_tick.available_at_ms"
            ),
        )
        object.__setattr__(
            self,
            "aggregate_trade_id",
            _nonnegative_int(
                self.aggregate_trade_id, "agg_tick.aggregate_trade_id"
            ),
        )
        object.__setattr__(
            self, "price", _positive_float(self.price, "agg_tick.price")
        )
        if self.available_at_ms < self.timestamp_ms:
            raise ValueError(
                "aggregate trade cannot be available before its timestamp"
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": "AGG_TRADE",
            "timestamp_ms": self.timestamp_ms,
            "available_at_ms": self.available_at_ms,
            "aggregate_trade_id": self.aggregate_trade_id,
            "price": self.price,
        }


MarketPathTick: TypeAlias = BookPathTick | AggTradePathTick


def _tick_sort_key(tick: MarketPathTick) -> tuple[Any, ...]:
    if isinstance(tick, BookPathTick):
        return (
            tick.timestamp_ms,
            tick.available_at_ms,
            0,
            tick.bid_price,
            tick.ask_price,
            tick.mark_price,
        )
    return (
        tick.timestamp_ms,
        tick.available_at_ms,
        1,
        tick.aggregate_trade_id,
        tick.price,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class TickEnvelope:
    """One immutable replay path shared by every arm for an opportunity.

    ``decision_at_ms`` is the evaluator's causal information cutoff: a path
    point is usable only after the opportunity observation and once its
    ``available_at_ms`` is no later than that cutoff.
    """

    opportunity_id: str
    observed_at_ms: int
    decision_at_ms: int
    coverage_through_ms: int
    ticks: tuple[MarketPathTick, ...]
    provenance: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "opportunity_id",
            _required_text(self.opportunity_id, "opportunity_id"),
        )
        for name in (
            "observed_at_ms",
            "decision_at_ms",
            "coverage_through_ms",
        ):
            object.__setattr__(
                self, name, _nonnegative_int(getattr(self, name), name)
            )
        if self.decision_at_ms < self.observed_at_ms:
            raise ValueError("decision_at_ms must not precede observed_at_ms")
        if not (
            self.observed_at_ms
            <= self.coverage_through_ms
            <= self.decision_at_ms
        ):
            raise ValueError(
                "coverage_through_ms must be within observation/decision bounds"
            )
        if not isinstance(self.ticks, tuple) or any(
            not isinstance(tick, (BookPathTick, AggTradePathTick))
            for tick in self.ticks
        ):
            raise TypeError("ticks must be a tuple of immutable path ticks")
        kinds = {type(tick) for tick in self.ticks}
        if len(kinds) > 1:
            raise ValueError("one envelope must use one deterministic path kind")
        if kinds == {AggTradePathTick}:
            trade_ids = [
                tick.aggregate_trade_id
                for tick in self.ticks
                if isinstance(tick, AggTradePathTick)
            ]
            if len(trade_ids) != len(set(trade_ids)):
                raise ValueError(
                    "aggregate_trade_id values must be unique in one envelope"
                )
        object.__setattr__(self, "ticks", tuple(sorted(self.ticks, key=_tick_sort_key)))
        object.__setattr__(
            self, "provenance", _required_text(self.provenance, "provenance")
        )

    @property
    def path_kind(self) -> str:
        if not self.ticks:
            return "EMPTY"
        return "BOOK" if isinstance(self.ticks[0], BookPathTick) else "AGG_TRADE"

    @property
    def usable_ticks(self) -> tuple[MarketPathTick, ...]:
        """Return only causally available post-observation points."""

        return tuple(
            tick
            for tick in self.ticks
            if self.observed_at_ms < tick.timestamp_ms
            <= self.coverage_through_ms
            and tick.available_at_ms <= self.decision_at_ms
        )

    @property
    def rejected_tick_count(self) -> int:
        return len(self.ticks) - len(self.usable_ticks)

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": TICK_ENVELOPE_SCHEMA,
            "opportunity_id": self.opportunity_id,
            "observed_at_ms": self.observed_at_ms,
            "decision_at_ms": self.decision_at_ms,
            "coverage_through_ms": self.coverage_through_ms,
            "path_kind": self.path_kind,
            "provenance": self.provenance,
            "ticks": [tick.to_payload() for tick in self.ticks],
        }

    @property
    def envelope_hash(self) -> str:
        return canonical_sha256(self.to_payload())


@dataclass(frozen=True, slots=True, kw_only=True)
class ShadowCostModel:
    maker_fee_bp: float
    taker_fee_bp: float
    adverse_slippage_bp: float
    provenance: str

    def __post_init__(self) -> None:
        for name in (
            "maker_fee_bp",
            "taker_fee_bp",
            "adverse_slippage_bp",
        ):
            object.__setattr__(
                self,
                name,
                _nonnegative_float(getattr(self, name), f"cost_model.{name}"),
            )
        object.__setattr__(
            self, "provenance", _required_text(self.provenance, "provenance")
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": COST_MODEL_SCHEMA,
            "maker_fee_bp": self.maker_fee_bp,
            "taker_fee_bp": self.taker_fee_bp,
            "adverse_slippage_bp": self.adverse_slippage_bp,
            "provenance": self.provenance,
        }

    @property
    def cost_model_hash(self) -> str:
        return canonical_sha256(self.to_payload())


@dataclass(frozen=True, slots=True, kw_only=True)
class MatchedArmOpportunity:
    opportunity_id: str
    candidate_status: str
    market_identity: MarketStateIdentity
    signal_price: float
    legacy_profile: ArmProfileDefinition | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "opportunity_id",
            _required_text(self.opportunity_id, "opportunity_id"),
        )
        status = str(self.candidate_status or "").strip().upper()
        if status not in _CANDIDATE_STATUSES:
            raise ValueError("candidate_status must be SAFE or NOT_EVALUATED")
        if not isinstance(self.market_identity, MarketStateIdentity):
            raise TypeError("market_identity must be MarketStateIdentity")
        object.__setattr__(self, "candidate_status", status)
        object.__setattr__(
            self,
            "signal_price",
            _positive_float(self.signal_price, "signal_price"),
        )
        if self.legacy_profile is not None:
            if not isinstance(self.legacy_profile, ArmProfileDefinition):
                raise TypeError("legacy_profile must be ArmProfileDefinition")
            if self.legacy_profile.profile_id != "LEGACY_CONTROL":
                raise ValueError("legacy_profile must be LEGACY_CONTROL")


@dataclass(frozen=True, slots=True, kw_only=True)
class ArmExitFill:
    level_id: str
    reason: str
    fraction: float
    price: float
    timestamp_ms: int
    liquidity: Literal["MAKER", "TAKER"]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "level_id", _required_text(self.level_id, "exit.level_id").upper()
        )
        reason = _required_text(self.reason, "exit.reason").upper()
        if reason not in {"TP", "SL", "MAX_HOLD"}:
            raise ValueError("exit.reason must be TP, SL, or MAX_HOLD")
        object.__setattr__(self, "reason", reason)
        fraction = _positive_float(self.fraction, "exit.fraction")
        if fraction > 1.0:
            raise ValueError("exit.fraction must not exceed 1")
        object.__setattr__(self, "fraction", fraction)
        object.__setattr__(
            self, "price", _positive_float(self.price, "exit.price")
        )
        object.__setattr__(
            self,
            "timestamp_ms",
            _nonnegative_int(self.timestamp_ms, "exit.timestamp_ms"),
        )
        liquidity = _required_text(
            self.liquidity, "exit.liquidity"
        ).upper()
        if liquidity not in {"MAKER", "TAKER"}:
            raise ValueError("exit.liquidity must be MAKER or TAKER")
        object.__setattr__(self, "liquidity", liquidity)

    def to_payload(self) -> dict[str, Any]:
        return {
            "level_id": self.level_id,
            "reason": self.reason,
            "fraction": self.fraction,
            "price": self.price,
            "timestamp_ms": self.timestamp_ms,
            "liquidity": self.liquidity,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ArmTerminalResult:
    opportunity_id: str
    profile_id: str
    arm_hash: str
    market_state_hash: str
    execution_profile_hash: str | None
    envelope_hash: str
    cost_model_hash: str
    side: str
    fill_status: FillStatus
    entry_limit_price: float | None
    entry_price: float | None
    filled_at_ms: int | None
    terminal_reason: TerminalReason
    terminal_at_ms: int | None
    terminal_price: float | None
    exits: tuple[ArmExitFill, ...]
    data_complete: bool
    evaluable: bool
    gross_reward_bp: float | None
    maker_fee_cost_bp: float | None
    taker_fee_cost_bp: float | None
    slippage_cost_bp: float | None
    reward_net_bp: float | None
    mfe_bp: float | None
    mae_bp: float | None

    def to_payload(self, *, include_terminal_hash: bool = True) -> dict[str, Any]:
        payload = {
            "schema": TERMINAL_RESULT_SCHEMA,
            "opportunity_id": self.opportunity_id,
            "profile_id": self.profile_id,
            "arm_hash": self.arm_hash,
            "market_state_hash": self.market_state_hash,
            "execution_profile_hash": self.execution_profile_hash,
            "envelope_hash": self.envelope_hash,
            "cost_model_hash": self.cost_model_hash,
            "side": self.side,
            "fill_status": self.fill_status,
            "entry_limit_price": self.entry_limit_price,
            "entry_price": self.entry_price,
            "filled_at_ms": self.filled_at_ms,
            "terminal_reason": self.terminal_reason,
            "terminal_at_ms": self.terminal_at_ms,
            "terminal_price": self.terminal_price,
            "exits": [exit_fill.to_payload() for exit_fill in self.exits],
            "data_complete": self.data_complete,
            "evaluable": self.evaluable,
            "gross_reward_bp": self.gross_reward_bp,
            "maker_fee_cost_bp": self.maker_fee_cost_bp,
            "taker_fee_cost_bp": self.taker_fee_cost_bp,
            "slippage_cost_bp": self.slippage_cost_bp,
            "reward_net_bp": self.reward_net_bp,
            "mfe_bp": self.mfe_bp,
            "mae_bp": self.mae_bp,
        }
        if include_terminal_hash:
            payload["terminal_hash"] = self.terminal_hash
        return payload

    @property
    def terminal_hash(self) -> str:
        return canonical_sha256(self.to_payload(include_terminal_hash=False))

    @property
    def repository_outcome(self) -> str | None:
        """Map to v1469_arm_evidence.outcome without importing storage.

        RISK_OFF has no ExecutionProfile hash and is therefore an action
        comparator, not a persistable execution-evidence row.
        """
        if self.terminal_reason in {"TP", "SL", "MAX_HOLD"} and self.exits:
            first_exit = self.exits[0]
            first_reason = str(first_exit.reason or "").strip().upper()
            if first_reason == "TP":
                return (
                    "tp1_first"
                    if str(first_exit.level_id or "").strip().upper()
                    == "TP1"
                    else "tp_first"
                )
            if first_reason == "SL":
                return "sl_first"
            if first_reason == "MAX_HOLD":
                return "max_hold"
        return _REPOSITORY_OUTCOME.get(self.terminal_reason)

    def to_repository_terminal_payload(
        self, *, updated_at_ms: int
    ) -> dict[str, Any]:
        outcome = self.repository_outcome
        if outcome is None:
            raise ValueError(
                "RISK_OFF is not persistable execution evidence"
            )
        terminal_at_ms = self.terminal_at_ms
        if terminal_at_ms is None:
            raise ValueError("terminal result requires terminal_at_ms")
        updated = _nonnegative_int(updated_at_ms, "updated_at_ms")
        if updated < terminal_at_ms:
            raise ValueError("updated_at_ms must not precede terminal_at_ms")
        fill_status = (
            self.fill_status
            if self.fill_status in {"FILLED", "NO_FILL"}
            else "UNKNOWN"
        )
        return {
            "status": (
                "DROPPED"
                if self.terminal_reason == "DATA_INCOMPLETE"
                else "TERMINAL"
            ),
            "terminal_at_ms": terminal_at_ms,
            "outcome": outcome,
            "fill_status": fill_status,
            "data_complete": self.data_complete,
            "ambiguous": self.terminal_reason == "AMBIGUOUS_BOTH",
            "reward_net_bp": self.reward_net_bp,
            "mfe_bp": self.mfe_bp,
            "mae_bp": self.mae_bp,
            "terminal_reason": self.terminal_reason,
            "terminal_payload": self.to_payload(),
            "updated_at_ms": updated,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PairedArmEvaluation:
    opportunity: MatchedArmOpportunity
    envelope: TickEnvelope
    cost_model: ShadowCostModel
    results: tuple[ArmTerminalResult, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.opportunity, MatchedArmOpportunity):
            raise TypeError("opportunity must be MatchedArmOpportunity")
        if not isinstance(self.envelope, TickEnvelope):
            raise TypeError("envelope must be TickEnvelope")
        if not isinstance(self.cost_model, ShadowCostModel):
            raise TypeError("cost_model must be ShadowCostModel")
        if not isinstance(self.results, tuple) or any(
            not isinstance(result, ArmTerminalResult) for result in self.results
        ):
            raise TypeError("results must be a tuple of ArmTerminalResult")
        if self.opportunity.opportunity_id != self.envelope.opportunity_id:
            raise ValueError("opportunity and envelope identity mismatch")
        if any(
            result.opportunity_id != self.opportunity.opportunity_id
            or result.envelope_hash != self.envelope.envelope_hash
            for result in self.results
        ):
            raise ValueError("paired results do not share one opportunity envelope")

    @property
    def envelope_hash(self) -> str:
        return self.envelope.envelope_hash


def _entry_limit(
    profile: ExecutionProfile, side: str, signal_price: float
) -> float:
    direction = -1.0 if side == "LONG" else 1.0
    return _finite_float(
        signal_price
        * (1.0 + direction * profile.entry_offset_bp / _BPS),
        "entry_limit_price",
    )


def _tick_prices(
    tick: MarketPathTick, side: str
) -> tuple[float, ...]:
    if isinstance(tick, BookPathTick):
        # Exit barriers must use a price that can actually liquidate the
        # position: LONG sells at bid; SHORT buys at ask.  Ask/mark for LONG
        # (or bid/mark for SHORT) would manufacture optimistic TP touches.
        return (
            (tick.bid_price,)
            if side == "LONG"
            else (tick.ask_price,)
        )
    return (tick.price,)


def _entry_touched(
    tick: MarketPathTick, side: str, entry_limit: float
) -> bool:
    if isinstance(tick, BookPathTick):
        return (
            tick.ask_price <= entry_limit
            if side == "LONG"
            else tick.bid_price >= entry_limit
        )
    return tick.price <= entry_limit if side == "LONG" else tick.price >= entry_limit


def _reference_exit_price(tick: MarketPathTick, side: str) -> float:
    if isinstance(tick, BookPathTick):
        return tick.bid_price if side == "LONG" else tick.ask_price
    return tick.price


def _target_price(entry_price: float, side: str, distance_bp: float) -> float:
    direction = 1.0 if side == "LONG" else -1.0
    return _finite_float(
        entry_price * (1.0 + direction * distance_bp / _BPS),
        "target_price",
    )


def _stop_price(entry_price: float, side: str, distance_bp: float) -> float:
    direction = -1.0 if side == "LONG" else 1.0
    return _finite_float(
        entry_price * (1.0 + direction * distance_bp / _BPS),
        "stop_price",
    )


def _tp_hit(side: str, prices: tuple[float, ...], target: float) -> bool:
    return max(prices) >= target if side == "LONG" else min(prices) <= target


def _sl_hit(side: str, prices: tuple[float, ...], stop: float) -> bool:
    return min(prices) <= stop if side == "LONG" else max(prices) >= stop


def _mfe_mae(
    side: str, entry_price: float, market_prices: list[float]
) -> tuple[float, float]:
    if not market_prices:
        return 0.0, 0.0
    if side == "LONG":
        returns = [
            (market_price / entry_price - 1.0) * _BPS
            for market_price in market_prices
        ]
    else:
        returns = [
            (1.0 - market_price / entry_price) * _BPS
            for market_price in market_prices
        ]
    return (
        _finite_float(max(0.0, max(returns)), "mfe_bp"),
        _finite_float(max(0.0, -min(returns)), "mae_bp"),
    )


def _reward_components(
    side: str,
    entry_price: float,
    exits: tuple[ArmExitFill, ...],
    cost_model: ShadowCostModel,
) -> tuple[float, float, float, float, float]:
    gross = 0.0
    maker_cost = cost_model.maker_fee_bp
    taker_cost = 0.0
    slippage_cost = 0.0
    for exit_fill in exits:
        raw_return = (
            exit_fill.price / entry_price - 1.0
            if side == "LONG"
            else 1.0 - exit_fill.price / entry_price
        ) * _BPS
        gross += exit_fill.fraction * raw_return
        exit_notional_ratio = exit_fill.price / entry_price
        if exit_fill.liquidity == "MAKER":
            maker_cost += (
                exit_fill.fraction
                * cost_model.maker_fee_bp
                * exit_notional_ratio
            )
        else:
            taker_cost += (
                exit_fill.fraction
                * cost_model.taker_fee_bp
                * exit_notional_ratio
            )
            slippage_cost += (
                exit_fill.fraction * cost_model.adverse_slippage_bp
            )
    net = gross - maker_cost - taker_cost - slippage_cost
    return tuple(
        _finite_float(value, "reward_component")
        for value in (gross, maker_cost, taker_cost, slippage_cost, net)
    )  # type: ignore[return-value]


def _base_result(
    opportunity: MatchedArmOpportunity,
    profile: ArmProfileDefinition,
    envelope: TickEnvelope,
    cost_model: ShadowCostModel,
) -> dict[str, Any]:
    return {
        "opportunity_id": opportunity.opportunity_id,
        "profile_id": profile.profile_id,
        "arm_hash": arm_identity_hash(
            opportunity.market_identity, profile
        ),
        "market_state_hash": opportunity.market_identity.identity_hash,
        "execution_profile_hash": profile.execution_profile_hash,
        "envelope_hash": envelope.envelope_hash,
        "cost_model_hash": cost_model.cost_model_hash,
        "side": opportunity.market_identity.effective_side,
    }


def _risk_off_result(
    opportunity: MatchedArmOpportunity,
    profile: ArmProfileDefinition,
    envelope: TickEnvelope,
    cost_model: ShadowCostModel,
) -> ArmTerminalResult:
    return ArmTerminalResult(
        **_base_result(opportunity, profile, envelope, cost_model),
        fill_status="RISK_OFF",
        entry_limit_price=None,
        entry_price=None,
        filled_at_ms=None,
        terminal_reason="RISK_OFF",
        terminal_at_ms=envelope.observed_at_ms,
        terminal_price=None,
        exits=(),
        data_complete=True,
        evaluable=True,
        gross_reward_bp=0.0,
        maker_fee_cost_bp=0.0,
        taker_fee_cost_bp=0.0,
        slippage_cost_bp=0.0,
        reward_net_bp=0.0,
        mfe_bp=None,
        mae_bp=None,
    )


def _incomplete_result(
    *,
    opportunity: MatchedArmOpportunity,
    profile: ArmProfileDefinition,
    envelope: TickEnvelope,
    cost_model: ShadowCostModel,
    entry_limit: float,
    entry_price: float | None,
    filled_at_ms: int | None,
    exits: tuple[ArmExitFill, ...],
    market_prices: list[float],
) -> ArmTerminalResult:
    mfe, mae = (
        _mfe_mae(
            opportunity.market_identity.effective_side,
            entry_price,
            market_prices,
        )
        if entry_price is not None
        else (None, None)
    )
    return ArmTerminalResult(
        **_base_result(opportunity, profile, envelope, cost_model),
        fill_status="FILLED" if entry_price is not None else "INCOMPLETE",
        entry_limit_price=entry_limit,
        entry_price=entry_price,
        filled_at_ms=filled_at_ms,
        terminal_reason="DATA_INCOMPLETE",
        terminal_at_ms=envelope.coverage_through_ms,
        terminal_price=None,
        exits=exits,
        data_complete=False,
        evaluable=False,
        gross_reward_bp=None,
        maker_fee_cost_bp=None,
        taker_fee_cost_bp=None,
        slippage_cost_bp=None,
        reward_net_bp=None,
        mfe_bp=mfe,
        mae_bp=mae,
    )


def _terminal_result(
    *,
    opportunity: MatchedArmOpportunity,
    profile: ArmProfileDefinition,
    envelope: TickEnvelope,
    cost_model: ShadowCostModel,
    entry_limit: float,
    entry_price: float,
    filled_at_ms: int,
    reason: Literal["TP", "SL", "MAX_HOLD"],
    terminal_at_ms: int,
    terminal_price: float,
    exits: tuple[ArmExitFill, ...],
    market_prices: list[float],
) -> ArmTerminalResult:
    gross, maker_cost, taker_cost, slippage_cost, net = _reward_components(
        opportunity.market_identity.effective_side,
        entry_price,
        exits,
        cost_model,
    )
    mfe, mae = _mfe_mae(
        opportunity.market_identity.effective_side,
        entry_price,
        market_prices,
    )
    return ArmTerminalResult(
        **_base_result(opportunity, profile, envelope, cost_model),
        fill_status="FILLED",
        entry_limit_price=entry_limit,
        entry_price=entry_price,
        filled_at_ms=filled_at_ms,
        terminal_reason=reason,
        terminal_at_ms=terminal_at_ms,
        terminal_price=terminal_price,
        exits=exits,
        data_complete=True,
        evaluable=True,
        gross_reward_bp=gross,
        maker_fee_cost_bp=maker_cost,
        taker_fee_cost_bp=taker_cost,
        slippage_cost_bp=slippage_cost,
        reward_net_bp=net,
        mfe_bp=mfe,
        mae_bp=mae,
    )


def _ambiguous_result(
    *,
    opportunity: MatchedArmOpportunity,
    profile: ArmProfileDefinition,
    envelope: TickEnvelope,
    cost_model: ShadowCostModel,
    entry_limit: float,
    entry_price: float,
    filled_at_ms: int,
    terminal_at_ms: int,
    exits: tuple[ArmExitFill, ...],
    market_prices: list[float],
) -> ArmTerminalResult:
    mfe, mae = _mfe_mae(
        opportunity.market_identity.effective_side,
        entry_price,
        market_prices,
    )
    return ArmTerminalResult(
        **_base_result(opportunity, profile, envelope, cost_model),
        fill_status="FILLED",
        entry_limit_price=entry_limit,
        entry_price=entry_price,
        filled_at_ms=filled_at_ms,
        terminal_reason="AMBIGUOUS_BOTH",
        terminal_at_ms=terminal_at_ms,
        terminal_price=None,
        exits=exits,
        data_complete=True,
        evaluable=False,
        gross_reward_bp=None,
        maker_fee_cost_bp=None,
        taker_fee_cost_bp=None,
        slippage_cost_bp=None,
        reward_net_bp=None,
        mfe_bp=mfe,
        mae_bp=mae,
    )


def evaluate_arm_profile(
    opportunity: MatchedArmOpportunity,
    profile: ArmProfileDefinition,
    envelope: TickEnvelope,
    cost_model: ShadowCostModel,
) -> ArmTerminalResult:
    """Evaluate one arm without side effects or live-order authority."""

    if not isinstance(opportunity, MatchedArmOpportunity):
        raise TypeError("opportunity must be MatchedArmOpportunity")
    if not isinstance(profile, ArmProfileDefinition):
        raise TypeError("profile must be ArmProfileDefinition")
    if not isinstance(envelope, TickEnvelope):
        raise TypeError("envelope must be TickEnvelope")
    if not isinstance(cost_model, ShadowCostModel):
        raise TypeError("cost_model must be ShadowCostModel")
    if opportunity.opportunity_id != envelope.opportunity_id:
        raise ValueError("opportunity and envelope identity mismatch")
    if (
        opportunity.market_identity.coarse_regime
        not in profile.allowed_regimes
    ):
        raise ValueError("profile is not legal for the opportunity regime")
    if profile.profile_id == RISK_OFF:
        return _risk_off_result(opportunity, profile, envelope, cost_model)

    execution = profile.execution_profile
    if execution is None:
        raise AssertionError("tradable profile is missing execution geometry")
    if execution.maker_mode not in {"POST_ONLY", "PASSIVE_LIMIT"}:
        raise ValueError("paired shadow supports maker-entry profiles only")

    side = opportunity.market_identity.effective_side
    entry_limit = _entry_limit(execution, side, opportunity.signal_price)
    entry_deadline = (
        envelope.observed_at_ms + execution.entry_ttl_s * 1_000
    )
    usable = envelope.usable_ticks
    fill_tick = next(
        (
            tick
            for tick in usable
            if tick.timestamp_ms <= entry_deadline
            and _entry_touched(tick, side, entry_limit)
        ),
        None,
    )
    if fill_tick is None:
        if envelope.coverage_through_ms < entry_deadline:
            return _incomplete_result(
                opportunity=opportunity,
                profile=profile,
                envelope=envelope,
                cost_model=cost_model,
                entry_limit=entry_limit,
                entry_price=None,
                filled_at_ms=None,
                exits=(),
                market_prices=[],
            )
        return ArmTerminalResult(
            **_base_result(opportunity, profile, envelope, cost_model),
            fill_status="NO_FILL",
            entry_limit_price=entry_limit,
            entry_price=None,
            filled_at_ms=None,
            terminal_reason="NO_FILL",
            terminal_at_ms=entry_deadline,
            terminal_price=None,
            exits=(),
            data_complete=True,
            evaluable=True,
            gross_reward_bp=0.0,
            maker_fee_cost_bp=0.0,
            taker_fee_cost_bp=0.0,
            slippage_cost_bp=0.0,
            reward_net_bp=0.0,
            mfe_bp=None,
            mae_bp=None,
        )

    fill_time = fill_tick.timestamp_ms
    hold_deadline = fill_time + execution.max_hold_s * 1_000
    stop_price = _stop_price(entry_limit, side, execution.sl_bp)
    targets = tuple(
        (
            level.level_id,
            _target_price(entry_limit, side, level.target_bp),
            level.fraction,
        )
        for level in execution.take_profits
    )
    remaining_targets = list(targets)
    exits: list[ArmExitFill] = []
    market_prices: list[float] = [entry_limit]

    post_fill_ticks = tuple(
        tick for tick in usable if tick.timestamp_ms > fill_time
    )
    last_defensible_exit_price: float | None = None
    for timestamp_ms, grouped in groupby(
        post_fill_ticks, key=lambda tick: tick.timestamp_ms
    ):
        group = tuple(grouped)
        # A point after the contractual hold deadline cannot determine either
        # the MAX_HOLD exit price or path extrema.  coverage_through_ms is the
        # proof that the interval was observed; the exit itself uses the last
        # causally available executable/reference price at or before deadline.
        if timestamp_ms > hold_deadline:
            break
        prices = tuple(
            price
            for tick in group
            for price in _tick_prices(tick, side)
        )
        market_prices.extend(prices)
        last_defensible_exit_price = _reference_exit_price(group[-1], side)

        if timestamp_ms == hold_deadline:
            break

        touched_targets = [
            target
            for target in remaining_targets
            if _tp_hit(side, prices, target[1])
        ]
        stop_hit = _sl_hit(side, prices, stop_price)
        if touched_targets and stop_hit:
            return _ambiguous_result(
                opportunity=opportunity,
                profile=profile,
                envelope=envelope,
                cost_model=cost_model,
                entry_limit=entry_limit,
                entry_price=entry_limit,
                filled_at_ms=fill_time,
                terminal_at_ms=timestamp_ms,
                exits=tuple(exits),
                market_prices=market_prices,
            )
        if stop_hit:
            remaining_fraction = _finite_float(
                1.0 - sum(exit_fill.fraction for exit_fill in exits),
                "remaining_fraction",
            )
            if remaining_fraction > 0.0:
                exits.append(
                    ArmExitFill(
                        level_id="SL",
                        reason="SL",
                        fraction=remaining_fraction,
                        price=stop_price,
                        timestamp_ms=timestamp_ms,
                        liquidity="TAKER",
                    )
                )
            return _terminal_result(
                opportunity=opportunity,
                profile=profile,
                envelope=envelope,
                cost_model=cost_model,
                entry_limit=entry_limit,
                entry_price=entry_limit,
                filled_at_ms=fill_time,
                reason="SL",
                terminal_at_ms=timestamp_ms,
                terminal_price=stop_price,
                exits=tuple(exits),
                market_prices=market_prices,
            )
        if touched_targets:
            for level_id, target_price, fraction in touched_targets:
                exits.append(
                    ArmExitFill(
                        level_id=level_id,
                        reason="TP",
                        fraction=fraction,
                        price=target_price,
                        timestamp_ms=timestamp_ms,
                        liquidity="MAKER",
                    )
                )
                remaining_targets.remove(
                    (level_id, target_price, fraction)
                )
            if not remaining_targets:
                return _terminal_result(
                    opportunity=opportunity,
                    profile=profile,
                    envelope=envelope,
                    cost_model=cost_model,
                    entry_limit=entry_limit,
                    entry_price=entry_limit,
                    filled_at_ms=fill_time,
                    reason="TP",
                    terminal_at_ms=timestamp_ms,
                    terminal_price=touched_targets[-1][1],
                    exits=tuple(exits),
                    market_prices=market_prices,
                )

    if envelope.coverage_through_ms >= hold_deadline:
        if last_defensible_exit_price is None:
            return _incomplete_result(
                opportunity=opportunity,
                profile=profile,
                envelope=envelope,
                cost_model=cost_model,
                entry_limit=entry_limit,
                entry_price=entry_limit,
                filled_at_ms=fill_time,
                exits=tuple(exits),
                market_prices=market_prices,
            )
        remaining_fraction = _finite_float(
            1.0 - sum(exit_fill.fraction for exit_fill in exits),
            "remaining_fraction",
        )
        if remaining_fraction > 0.0:
            exits.append(
                ArmExitFill(
                    level_id="MAX_HOLD",
                    reason="MAX_HOLD",
                    fraction=remaining_fraction,
                    price=last_defensible_exit_price,
                    timestamp_ms=hold_deadline,
                    liquidity="TAKER",
                )
            )
        return _terminal_result(
            opportunity=opportunity,
            profile=profile,
            envelope=envelope,
            cost_model=cost_model,
            entry_limit=entry_limit,
            entry_price=entry_limit,
            filled_at_ms=fill_time,
            reason="MAX_HOLD",
            terminal_at_ms=hold_deadline,
            terminal_price=last_defensible_exit_price,
            exits=tuple(exits),
            market_prices=market_prices,
        )

    return _incomplete_result(
        opportunity=opportunity,
        profile=profile,
        envelope=envelope,
        cost_model=cost_model,
        entry_limit=entry_limit,
        entry_price=entry_limit,
        filled_at_ms=fill_time,
        exits=tuple(exits),
        market_prices=market_prices,
    )


def evaluate_paired_arms(
    opportunity: MatchedArmOpportunity,
    envelope: TickEnvelope,
    cost_model: ShadowCostModel,
) -> PairedArmEvaluation:
    """Evaluate every legal profile against one shared immutable envelope."""

    if not isinstance(opportunity, MatchedArmOpportunity):
        raise TypeError("opportunity must be MatchedArmOpportunity")
    if not isinstance(envelope, TickEnvelope):
        raise TypeError("envelope must be TickEnvelope")
    if not isinstance(cost_model, ShadowCostModel):
        raise TypeError("cost_model must be ShadowCostModel")
    if opportunity.opportunity_id != envelope.opportunity_id:
        raise ValueError("opportunity and envelope identity mismatch")
    profiles = profiles_for_matched_candidate(
        opportunity.market_identity, opportunity.candidate_status
    )
    if opportunity.legacy_profile is not None:
        profiles = (opportunity.legacy_profile,) + profiles
    results = tuple(
        evaluate_arm_profile(
            opportunity,
            profile,
            envelope,
            cost_model,
        )
        for profile in profiles
    )
    return PairedArmEvaluation(
        opportunity=opportunity,
        envelope=envelope,
        cost_model=cost_model,
        results=results,
    )


__all__ = [
    "AggTradePathTick",
    "ArmExitFill",
    "ArmTerminalResult",
    "BookPathTick",
    "COST_MODEL_SCHEMA",
    "MatchedArmOpportunity",
    "PAIRED_EVALUATOR_SCHEMA",
    "PairedArmEvaluation",
    "ShadowCostModel",
    "TERMINAL_RESULT_SCHEMA",
    "TICK_ENVELOPE_SCHEMA",
    "TickEnvelope",
    "evaluate_arm_profile",
    "evaluate_paired_arms",
]
