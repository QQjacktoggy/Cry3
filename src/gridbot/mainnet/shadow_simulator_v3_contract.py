"""Contracts shared by the authoritative offline fixed-exit shadow V3."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Literal

SideV3 = Literal["BUY", "SELL"]
FillModelV3 = Literal["TOUCH_UPPER_BOUND", "TRADE_THROUGH"]
AnchorV3 = Literal["ABSOLUTE", "SIGNAL", "ENTRY"]
ExitReasonV3 = Literal["TP", "SL", "MAX_HOLD"]
DecimalLike = Decimal | int | float | str

MAX_HOLD_POLICY_V3 = "FIRST_POST_FILL_AGGTRADE_AT_OR_AFTER_ABSOLUTE_DEADLINE"
PRICE_QUANTIZATION_POLICY_V3 = "BINANCE_CLIENT_DECIMAL_ROUND_HALF_UP"
TARGET_PRICE_POLICY_V3 = "SOURCE_TRIGGER_PRESERVED_EXECUTABLE_PRICE_ROUND_HALF_UP"
METRIC_CONTRACT_V3 = "COMPLETE_NO_FILL_WR_EXCLUDED_EV_OPPORTUNITY_ZERO"
SIMULATION_SCOPE_V3 = "FIXED_EXIT_ONLY_NO_TRAILING_NO_PARTIAL_FILL"
BPS_V3, ZERO_V3 = Decimal("10000"), Decimal("0")
VARIANT_OFFSET_BP_V3 = {"E0": Decimal("0"), "E1": Decimal("1"), "E2": Decimal("2")}


class ShadowSimulationInputErrorV3(ValueError):
    """Malformed, ambiguous, or unproved replay evidence."""


def decimal_v3(value: DecimalLike, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ShadowSimulationInputErrorV3(f"{name} must be numeric")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ShadowSimulationInputErrorV3(f"{name} must be numeric") from exc
    if not parsed.is_finite():
        raise ShadowSimulationInputErrorV3(f"{name} must be finite")
    return parsed


def positive_v3(value: DecimalLike, name: str) -> Decimal:
    parsed = decimal_v3(value, name)
    if parsed <= 0:
        raise ShadowSimulationInputErrorV3(f"{name} must be positive")
    return parsed


def nonnegative_v3(value: DecimalLike, name: str) -> Decimal:
    parsed = decimal_v3(value, name)
    if parsed < 0:
        raise ShadowSimulationInputErrorV3(f"{name} must be non-negative")
    return parsed


def int_v3(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ShadowSimulationInputErrorV3(f"{name} must be a non-negative integer")
    return value


def quantize_tick_price_v3(price: DecimalLike, tick_size: DecimalLike) -> Decimal:
    """Mirror the existing Binance client ``ROUND_HALF_UP`` formatter."""
    price_d, tick_d = positive_v3(price, "price"), positive_v3(tick_size, "tick_size")
    ticks = (price_d / tick_d).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return positive_v3(ticks * tick_d, "quantized_price")


@dataclass(frozen=True)
class CoverageIntervalV3:
    """V4 requested range plus continuous-ID post-end sentinel proof."""
    start_ms: int
    requested_end_ms: int
    proof_end_ms: int
    proof: str

    def __post_init__(self) -> None:
        int_v3(self.start_ms, "coverage.start_ms")
        int_v3(self.requested_end_ms, "coverage.requested_end_ms")
        int_v3(self.proof_end_ms, "coverage.proof_end_ms")
        if self.requested_end_ms < self.start_ms or self.proof_end_ms < self.requested_end_ms:
            raise ShadowSimulationInputErrorV3("invalid coverage/proof interval")
        if not isinstance(self.proof, str) or not self.proof.strip():
            raise ShadowSimulationInputErrorV3("coverage proof is required")
        object.__setattr__(self, "proof", self.proof.strip())

    def contains(self, timestamp_ms: int) -> bool:
        return self.start_ms <= timestamp_ms <= self.proof_end_ms


@dataclass(frozen=True)
class VerifiedCoverageV3:
    intervals: tuple[CoverageIntervalV3, ...]
    provenance: str

    def __post_init__(self) -> None:
        intervals = tuple(self.intervals)
        if not intervals:
            raise ShadowSimulationInputErrorV3("verified coverage needs intervals")
        if not isinstance(self.provenance, str) or not self.provenance.strip():
            raise ShadowSimulationInputErrorV3("verified coverage needs provenance")
        previous: int | None = None
        for interval in intervals:
            if not isinstance(interval, CoverageIntervalV3):
                raise ShadowSimulationInputErrorV3("invalid coverage interval type")
            if previous is not None and interval.start_ms < previous:
                raise ShadowSimulationInputErrorV3("coverage intervals must be ordered")
            previous = interval.start_ms
        object.__setattr__(self, "intervals", intervals)
        object.__setattr__(self, "provenance", self.provenance.strip())

    def contains(self, timestamp_ms: int) -> bool:
        return any(interval.contains(timestamp_ms) for interval in self.intervals)

    def covers(self, start_ms: int, end_ms: int) -> bool:
        """Union ordered overlapping/adjacent proof intervals; reject true gaps."""
        if start_ms < 0 or end_ms < start_ms:
            return False
        cursor = start_ms
        for interval in self.intervals:
            if interval.proof_end_ms < cursor:
                continue
            if interval.start_ms > cursor:
                return False
            if interval.proof_end_ms >= end_ms:
                return True
            cursor = interval.proof_end_ms + 1
        return False

    @property
    def max_proof_end_ms(self) -> int:
        return max(interval.proof_end_ms for interval in self.intervals)


@dataclass(frozen=True)
class ShadowTickV3:
    timestamp_ms: int
    aggregate_trade_id: int
    price: Decimal

    def __post_init__(self) -> None:
        int_v3(self.timestamp_ms, "tick.timestamp_ms")
        int_v3(self.aggregate_trade_id, "tick.aggregate_trade_id")
        object.__setattr__(self, "price", positive_v3(self.price, "tick.price"))


@dataclass(frozen=True)
class TargetLevelV3:
    anchor: AnchorV3
    absolute_price: Decimal | None = None
    distance_bp: Decimal | None = None

    def __post_init__(self) -> None:
        anchor = str(self.anchor).upper()
        if anchor not in {"ABSOLUTE", "SIGNAL", "ENTRY"}:
            raise ShadowSimulationInputErrorV3("invalid target anchor")
        object.__setattr__(self, "anchor", anchor)
        if anchor == "ABSOLUTE":
            if self.absolute_price is None or self.distance_bp is not None:
                raise ShadowSimulationInputErrorV3("ABSOLUTE target requires only absolute_price")
            object.__setattr__(self, "absolute_price", positive_v3(self.absolute_price, "target.absolute_price"))
        elif self.distance_bp is None or self.absolute_price is not None:
            raise ShadowSimulationInputErrorV3(f"{anchor} target requires only distance_bp")
        else:
            object.__setattr__(self, "distance_bp", positive_v3(self.distance_bp, "target.distance_bp"))


@dataclass(frozen=True)
class FeeScheduleV3:
    entry_fee_rate: Decimal
    tp_exit_fee_rate: Decimal
    sl_exit_fee_rate: Decimal
    max_hold_exit_fee_rate: Decimal
    sl_adverse_slippage_bp: Decimal
    max_hold_adverse_slippage_bp: Decimal
    funding_cost_usdc: Decimal
    fee_provenance: str
    funding_provenance: str

    def __post_init__(self) -> None:
        names = ("entry_fee_rate", "tp_exit_fee_rate", "sl_exit_fee_rate", "max_hold_exit_fee_rate", "sl_adverse_slippage_bp", "max_hold_adverse_slippage_bp")
        for name in names:
            object.__setattr__(self, name, nonnegative_v3(getattr(self, name), f"fees.{name}"))
        object.__setattr__(self, "funding_cost_usdc", decimal_v3(self.funding_cost_usdc, "fees.funding_cost_usdc"))
        for name in ("fee_provenance", "funding_provenance"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ShadowSimulationInputErrorV3(f"{name} is required")
            object.__setattr__(self, name, value.strip())

    def exit_fee_rate(self, reason: ExitReasonV3) -> Decimal:
        return self.tp_exit_fee_rate if reason == "TP" else self.sl_exit_fee_rate if reason == "SL" else self.max_hold_exit_fee_rate

    def adverse_slippage_bp(self, reason: ExitReasonV3) -> Decimal:
        return self.sl_adverse_slippage_bp if reason == "SL" else self.max_hold_adverse_slippage_bp if reason == "MAX_HOLD" else ZERO_V3


@dataclass(frozen=True)
class ShadowTradeSpecV3:
    opportunity_id: str
    variant: Literal["E0", "E1", "E2"]
    fill_model: FillModelV3
    simulation_version: str
    side: SideV3
    start_ms: int
    decision_latency_ms: int
    entry_ttl_ms: int
    outcome_deadline_ms: int
    signal_price: Decimal
    tick_size: Decimal
    quantity: Decimal
    tp: TargetLevelV3
    sl: TargetLevelV3
    fees: FeeScheduleV3

    def __post_init__(self) -> None:
        if not isinstance(self.opportunity_id, str) or not self.opportunity_id.strip() or not isinstance(self.simulation_version, str) or not self.simulation_version.strip():
            raise ShadowSimulationInputErrorV3("opportunity_id and simulation_version are required")
        variant, side, model = str(self.variant).upper(), str(self.side).upper(), str(self.fill_model).upper()
        if variant not in VARIANT_OFFSET_BP_V3 or side not in {"BUY", "SELL"} or model not in {"TOUCH_UPPER_BOUND", "TRADE_THROUGH"}:
            raise ShadowSimulationInputErrorV3("invalid variant, side, or fill model")
        int_v3(self.start_ms, "start_ms")
        int_v3(self.decision_latency_ms, "decision_latency_ms")
        if isinstance(self.entry_ttl_ms, bool) or not isinstance(self.entry_ttl_ms, int) or self.entry_ttl_ms <= 0:
            raise ShadowSimulationInputErrorV3("entry_ttl_ms must be positive")
        if isinstance(self.outcome_deadline_ms, bool) or not isinstance(self.outcome_deadline_ms, int) or self.outcome_deadline_ms < self.entry_deadline_ms:
            raise ShadowSimulationInputErrorV3("outcome_deadline_ms must be absolute and not precede entry deadline")
        if not isinstance(self.tp, TargetLevelV3) or not isinstance(self.sl, TargetLevelV3) or not isinstance(self.fees, FeeScheduleV3):
            raise ShadowSimulationInputErrorV3("explicit targets and fees are required")
        object.__setattr__(self, "opportunity_id", self.opportunity_id.strip())
        object.__setattr__(self, "simulation_version", self.simulation_version.strip())
        object.__setattr__(self, "variant", variant)
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "fill_model", model)
        object.__setattr__(self, "signal_price", positive_v3(self.signal_price, "signal_price"))
        object.__setattr__(self, "tick_size", positive_v3(self.tick_size, "tick_size"))
        object.__setattr__(self, "quantity", positive_v3(self.quantity, "quantity"))

    @property
    def entry_eligible_ms(self) -> int:
        return self.start_ms + self.decision_latency_ms

    @property
    def entry_deadline_ms(self) -> int:
        return self.start_ms + self.entry_ttl_ms

    @property
    def entry_offset_bp(self) -> Decimal:
        return VARIANT_OFFSET_BP_V3[self.variant]

    @property
    def raw_entry_limit_price(self) -> Decimal:
        sign = Decimal("-1") if self.side == "BUY" else Decimal("1")
        return self.signal_price * (Decimal("1") + sign * self.entry_offset_bp / BPS_V3)

    @property
    def entry_limit_price(self) -> Decimal:
        return quantize_tick_price_v3(self.raw_entry_limit_price, self.tick_size)


@dataclass(frozen=True)
class ResolvedTargetV3:
    anchor: str
    source_trigger_price: Decimal
    executable_price: Decimal
