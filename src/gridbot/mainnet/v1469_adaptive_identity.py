"""Stable identity primitives for the v1.4.69 adaptive-arm redesign.

This module is intentionally independent from the live selector, persistence,
and order submission paths.  It defines the immutable contracts those paths
can adopt in later phases:

* a market-state identity,
* a complete execution-profile identity, and
* a separate risk-policy identity.

Execution profiles are allow-list based.  Known absolute-price and runtime
fields may be present on an input mapping, but are deliberately excluded from
the execution hash.  Any other unknown field fails closed so a newly added
execution control cannot silently escape cohort identity.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from enum import Enum
from typing import Any


V1469_VERSION = "v1.4.69"
MARKET_STATE_SCHEMA = "v1469.market-state.1"
EXECUTION_PROFILE_SCHEMA = "v1469.execution-profile.1"
RISK_POLICY_SCHEMA = "v1469.risk-policy.1"

_FLOAT_QUANTUM = Decimal("0.000001")
_TOKEN_PATTERN = re.compile(r"^[A-Z][A-Z0-9_-]{0,63}$")
_COARSE_REGIMES = frozenset(
    {"TREND_UP", "TREND_DOWN", "RANGE", "SHOCK", "UNCERTAIN"}
)
_SIDES = frozenset({"LONG", "SHORT"})
_MAKER_MODES = frozenset({"POST_ONLY", "PASSIVE_LIMIT", "MARKET"})

# These fields describe a particular quote, not execution geometry.
ABSOLUTE_PRICE_FIELDS = frozenset(
    {
        "entry",
        "entry_price",
        "entry_limit_price",
        "reference_price",
        "signal_price",
        "mark_price",
        "bid_price",
        "ask_price",
        "last_price",
        "fill_price",
        "average_fill_price",
        "close_price",
        "tp",
        "tp_price",
        "tp1_price",
        "tp2_price",
        "tp3_price",
        "full_tp_price",
        "sl",
        "sl_price",
        "stop",
        "stop_price",
        "stop_loss",
    }
)

# These fields identify one observation or order, not a reusable profile.
RUNTIME_FIELDS = frozenset(
    {
        "timestamp",
        "timestamp_ms",
        "run_id",
        "runtime_id",
        "opportunity_id",
        "sample_id",
        "order_id",
        "entry_order_id",
        "client_order_id",
        "observed_at",
        "observed_at_ms",
        "decision_at",
        "decision_at_ms",
        "recorded_at",
        "recorded_at_ms",
        "resolved_at",
        "resolved_at_ms",
        "event_time",
        "event_time_ms",
        "created_at",
        "created_at_ms",
        "updated_at",
        "updated_at_ms",
        "submitted_at",
        "submitted_at_ms",
        "filled_at",
        "filled_at_ms",
        "terminal_at",
        "terminal_at_ms",
        "expires_at",
        "expires_at_ms",
    }
)

# Risk is joined to an execution profile at submit time, but must not fragment
# geometry evidence.  These are the only risk-context fields a combined
# observation mapping may carry without failing the execution-profile parser.
RISK_CONTEXT_FIELDS = frozenset(
    {
        "cap_tier",
        "risk_policy_id",
        "risk_policy_hash",
        "paid_notional_cap_usdc",
        "per_trade_loss_cap_usdc",
        "lane_open_notional_cap_usdc",
        "global_open_notional_cap_usdc",
        "daily_soft_loss_cap_usdc",
        "daily_hard_loss_cap_usdc",
        "daily_profit_lock_trigger_usdc",
        "daily_profit_lock_giveback_usdc",
        "max_consecutive_losses",
        "cooldown_s",
    }
)

_EXECUTION_FIELDS = frozenset(
    {
        "schema",
        "profile_id",
        "entry_offset_bp",
        "entry_ttl_s",
        "maker_mode",
        "reprice",
        "take_profits",
        "sl_bp",
        "breakeven",
        "max_hold_s",
        "trail",
        "runner",
        "early_fail",
        "dca",
    }
)
_RISK_FIELDS = frozenset(
    {
        "schema",
        "policy_id",
        "paid_notional_cap_usdc",
        "per_trade_loss_cap_usdc",
        "lane_open_notional_cap_usdc",
        "global_open_notional_cap_usdc",
        "daily_soft_loss_cap_usdc",
        "daily_hard_loss_cap_usdc",
        "daily_profit_lock_trigger_usdc",
        "daily_profit_lock_giveback_usdc",
        "max_consecutive_losses",
        "cooldown_s",
    }
)
_MARKET_STATE_FIELDS = frozenset(
    {
        "schema",
        "environment",
        "symbol",
        "lane_code",
        "effective_side",
        "strategy",
        "coarse_regime",
        "market_state",
    }
)


def _canonical_float(value: Any, field_name: str) -> float:
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
        raise ValueError(
            f"{field_name} is outside the canonical numeric range"
        ) from exc
    if rounded == 0:
        rounded = Decimal(0)
    return float(rounded)


def _positive_float(value: Any, field_name: str) -> float:
    number = _canonical_float(value, field_name)
    if number <= 0.0:
        raise ValueError(f"{field_name} must be positive")
    return number


def _nonnegative_float(value: Any, field_name: str) -> float:
    number = _canonical_float(value, field_name)
    if number < 0.0:
        raise ValueError(f"{field_name} must be non-negative")
    return number


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _nonnegative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _strict_bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _canonical_token(value: Any, field_name: str) -> str:
    if isinstance(value, Enum):
        value = value.value
    token = str(value or "").strip().upper()
    if not _TOKEN_PATTERN.fullmatch(token):
        raise ValueError(f"{field_name} must be a non-empty canonical token")
    return token


def _canonical_detail(value: Any, field_name: str) -> str:
    detail = str(value or "").strip().lower().replace("-", "_")
    detail = re.sub(r"\s+", "_", detail)
    if not detail or len(detail) > 128:
        raise ValueError(f"{field_name} must be non-empty and at most 128 characters")
    if not re.fullmatch(r"[a-z0-9_:.]+", detail):
        raise ValueError(f"{field_name} contains unsupported characters")
    return detail


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise TypeError(f"{field_name} keys must be strings")
    return value


def _reject_unknown_fields(
    value: Mapping[str, Any],
    allowed: frozenset[str],
    field_name: str,
    *,
    ignored: frozenset[str] = frozenset(),
) -> None:
    unknown = set(value) - allowed - ignored
    if unknown:
        rendered = ", ".join(sorted(unknown))
        raise ValueError(f"{field_name} contains unknown fields: {rendered}")


def _require_fields(
    value: Mapping[str, Any], required: frozenset[str], field_name: str
) -> None:
    missing = required - set(value)
    if missing:
        rendered = ", ".join(sorted(missing))
        raise ValueError(f"{field_name} is missing required fields: {rendered}")


def _validate_schema(value: Mapping[str, Any], expected: str, field_name: str) -> None:
    supplied = value.get("schema")
    if supplied is not None and supplied != expected:
        raise ValueError(f"{field_name}.schema must be {expected}")


def _canonical_json_value(value: Any, path: str) -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, (float, Decimal)):
        return _canonical_float(value, path)
    if isinstance(value, Enum):
        return _canonical_json_value(value.value, path)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError(f"{path} keys must be strings")
        return {
            key: _canonical_json_value(item, f"{path}.{key}")
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [
            _canonical_json_value(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    raise TypeError(f"{path} contains unsupported type {type(value).__name__}")


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Return deterministic JSON and reject non-finite/unsupported values."""

    normalized = _canonical_json_value(_mapping(payload, "payload"), "payload")
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class MarketStateIdentity:
    environment: str
    symbol: str
    lane_code: str
    effective_side: str
    strategy: str
    coarse_regime: str
    market_state: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "environment", _canonical_token(self.environment, "environment")
        )
        object.__setattr__(self, "symbol", _canonical_token(self.symbol, "symbol"))
        object.__setattr__(
            self, "lane_code", _canonical_token(self.lane_code, "lane_code")
        )
        object.__setattr__(
            self,
            "effective_side",
            _canonical_token(self.effective_side, "effective_side"),
        )
        object.__setattr__(
            self, "strategy", _canonical_token(self.strategy, "strategy")
        )
        object.__setattr__(
            self,
            "coarse_regime",
            _canonical_token(self.coarse_regime, "coarse_regime"),
        )
        object.__setattr__(
            self,
            "market_state",
            _canonical_detail(self.market_state, "market_state"),
        )
        if self.effective_side not in _SIDES:
            raise ValueError("effective_side must be LONG or SHORT")
        if self.coarse_regime not in _COARSE_REGIMES:
            raise ValueError(
                "coarse_regime must be TREND_UP, TREND_DOWN, RANGE, SHOCK, or UNCERTAIN"
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": MARKET_STATE_SCHEMA,
            "environment": self.environment,
            "symbol": self.symbol,
            "lane_code": self.lane_code,
            "effective_side": self.effective_side,
            "strategy": self.strategy,
            "coarse_regime": self.coarse_regime,
            "market_state": self.market_state,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MarketStateIdentity":
        data = _mapping(value, "market_state_identity")
        _reject_unknown_fields(data, _MARKET_STATE_FIELDS, "market_state_identity")
        _validate_schema(data, MARKET_STATE_SCHEMA, "market_state_identity")
        required = _MARKET_STATE_FIELDS - {"schema"}
        _require_fields(data, required, "market_state_identity")
        return cls(
            environment=data["environment"],
            symbol=data["symbol"],
            lane_code=data["lane_code"],
            effective_side=data["effective_side"],
            strategy=data["strategy"],
            coarse_regime=data["coarse_regime"],
            market_state=data["market_state"],
        )

    @property
    def identity_hash(self) -> str:
        return canonical_sha256(self.to_payload())


def market_state_hash(
    identity: MarketStateIdentity | Mapping[str, Any],
) -> str:
    resolved = (
        identity
        if isinstance(identity, MarketStateIdentity)
        else MarketStateIdentity.from_mapping(identity)
    )
    return resolved.identity_hash


@dataclass(frozen=True, slots=True, kw_only=True)
class RepricePolicy:
    enabled: bool = False
    after_s: int = 0
    offset_bp: float = 0.0
    max_reprices: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "enabled", _strict_bool(self.enabled, "reprice.enabled")
        )
        object.__setattr__(
            self, "after_s", _nonnegative_int(self.after_s, "reprice.after_s")
        )
        object.__setattr__(
            self,
            "offset_bp",
            _nonnegative_float(self.offset_bp, "reprice.offset_bp"),
        )
        object.__setattr__(
            self,
            "max_reprices",
            _nonnegative_int(self.max_reprices, "reprice.max_reprices"),
        )
        if self.enabled:
            if self.after_s <= 0 or self.max_reprices <= 0:
                raise ValueError(
                    "enabled reprice requires positive after_s and max_reprices"
                )
        elif self.after_s != 0 or self.offset_bp != 0.0 or self.max_reprices != 0:
            raise ValueError("disabled reprice must use zero-valued controls")

    def to_payload(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "after_s": self.after_s,
            "offset_bp": self.offset_bp,
            "max_reprices": self.max_reprices,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RepricePolicy":
        data = _mapping(value, "reprice")
        fields = frozenset({"enabled", "after_s", "offset_bp", "max_reprices"})
        _reject_unknown_fields(data, fields, "reprice")
        _require_fields(data, fields, "reprice")
        return cls(**{name: data[name] for name in fields})


@dataclass(frozen=True, slots=True, kw_only=True)
class TakeProfitLevel:
    level_id: str
    target_bp: float
    fraction: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "level_id", _canonical_token(self.level_id, "take_profit.level_id")
        )
        object.__setattr__(
            self,
            "target_bp",
            _positive_float(self.target_bp, "take_profit.target_bp"),
        )
        fraction = _positive_float(self.fraction, "take_profit.fraction")
        if fraction > 1.0:
            raise ValueError("take_profit.fraction must not exceed 1")
        object.__setattr__(self, "fraction", fraction)

    def to_payload(self) -> dict[str, Any]:
        return {
            "level_id": self.level_id,
            "target_bp": self.target_bp,
            "fraction": self.fraction,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TakeProfitLevel":
        data = _mapping(value, "take_profit")
        fields = frozenset({"level_id", "target_bp", "fraction"})
        _reject_unknown_fields(data, fields, "take_profit")
        _require_fields(data, fields, "take_profit")
        return cls(**{name: data[name] for name in fields})


@dataclass(frozen=True, slots=True, kw_only=True)
class BreakevenPolicy:
    enabled: bool = False
    trigger_bp: float = 0.0
    lock_bp: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "enabled", _strict_bool(self.enabled, "breakeven.enabled")
        )
        object.__setattr__(
            self,
            "trigger_bp",
            _nonnegative_float(self.trigger_bp, "breakeven.trigger_bp"),
        )
        object.__setattr__(
            self, "lock_bp", _nonnegative_float(self.lock_bp, "breakeven.lock_bp")
        )
        if self.enabled:
            if self.trigger_bp <= 0.0:
                raise ValueError("enabled breakeven requires a positive trigger_bp")
            if self.lock_bp >= self.trigger_bp:
                raise ValueError("breakeven.lock_bp must be below trigger_bp")
        elif self.trigger_bp != 0.0 or self.lock_bp != 0.0:
            raise ValueError("disabled breakeven must use zero-valued controls")

    def to_payload(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "trigger_bp": self.trigger_bp,
            "lock_bp": self.lock_bp,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "BreakevenPolicy":
        data = _mapping(value, "breakeven")
        fields = frozenset({"enabled", "trigger_bp", "lock_bp"})
        _reject_unknown_fields(data, fields, "breakeven")
        _require_fields(data, fields, "breakeven")
        return cls(**{name: data[name] for name in fields})


@dataclass(frozen=True, slots=True, kw_only=True)
class TrailPolicy:
    enabled: bool = False
    arm_bp: float = 0.0
    giveback_bp: float = 0.0
    floor_bp: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", _strict_bool(self.enabled, "trail.enabled"))
        object.__setattr__(
            self, "arm_bp", _nonnegative_float(self.arm_bp, "trail.arm_bp")
        )
        object.__setattr__(
            self,
            "giveback_bp",
            _nonnegative_float(self.giveback_bp, "trail.giveback_bp"),
        )
        object.__setattr__(
            self, "floor_bp", _nonnegative_float(self.floor_bp, "trail.floor_bp")
        )
        if self.enabled:
            if self.arm_bp <= 0.0 or self.giveback_bp <= 0.0:
                raise ValueError(
                    "enabled trail requires positive arm_bp and giveback_bp"
                )
            if self.floor_bp >= self.arm_bp:
                raise ValueError("trail.floor_bp must be below arm_bp")
        elif (
            self.arm_bp != 0.0
            or self.giveback_bp != 0.0
            or self.floor_bp != 0.0
        ):
            raise ValueError("disabled trail must use zero-valued controls")

    def to_payload(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "arm_bp": self.arm_bp,
            "giveback_bp": self.giveback_bp,
            "floor_bp": self.floor_bp,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TrailPolicy":
        data = _mapping(value, "trail")
        fields = frozenset({"enabled", "arm_bp", "giveback_bp", "floor_bp"})
        _reject_unknown_fields(data, fields, "trail")
        _require_fields(data, fields, "trail")
        return cls(**{name: data[name] for name in fields})


@dataclass(frozen=True, slots=True, kw_only=True)
class RunnerPolicy:
    enabled: bool = False
    fraction: float = 0.0
    take_profit_cap_bp: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "enabled", _strict_bool(self.enabled, "runner.enabled")
        )
        fraction = _nonnegative_float(self.fraction, "runner.fraction")
        if fraction > 1.0:
            raise ValueError("runner.fraction must not exceed 1")
        object.__setattr__(self, "fraction", fraction)
        object.__setattr__(
            self,
            "take_profit_cap_bp",
            _nonnegative_float(
                self.take_profit_cap_bp, "runner.take_profit_cap_bp"
            ),
        )
        if self.enabled:
            if self.fraction <= 0.0:
                raise ValueError("enabled runner requires a positive fraction")
        elif self.fraction != 0.0 or self.take_profit_cap_bp != 0.0:
            raise ValueError("disabled runner must use zero-valued controls")

    def to_payload(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "fraction": self.fraction,
            "take_profit_cap_bp": self.take_profit_cap_bp,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RunnerPolicy":
        data = _mapping(value, "runner")
        fields = frozenset({"enabled", "fraction", "take_profit_cap_bp"})
        _reject_unknown_fields(data, fields, "runner")
        _require_fields(data, fields, "runner")
        return cls(**{name: data[name] for name in fields})


@dataclass(frozen=True, slots=True, kw_only=True)
class EarlyFailPolicy:
    enabled: bool = False
    after_s: int = 0
    max_mfe_bp: float = 0.0
    adverse_bp: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "enabled", _strict_bool(self.enabled, "early_fail.enabled")
        )
        object.__setattr__(
            self, "after_s", _nonnegative_int(self.after_s, "early_fail.after_s")
        )
        object.__setattr__(
            self,
            "max_mfe_bp",
            _nonnegative_float(self.max_mfe_bp, "early_fail.max_mfe_bp"),
        )
        object.__setattr__(
            self,
            "adverse_bp",
            _nonnegative_float(self.adverse_bp, "early_fail.adverse_bp"),
        )
        if self.enabled:
            if self.after_s <= 0 or self.adverse_bp <= 0.0:
                raise ValueError(
                    "enabled early_fail requires positive after_s and adverse_bp"
                )
        elif (
            self.after_s != 0
            or self.max_mfe_bp != 0.0
            or self.adverse_bp != 0.0
        ):
            raise ValueError("disabled early_fail must use zero-valued controls")

    def to_payload(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "after_s": self.after_s,
            "max_mfe_bp": self.max_mfe_bp,
            "adverse_bp": self.adverse_bp,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EarlyFailPolicy":
        data = _mapping(value, "early_fail")
        fields = frozenset({"enabled", "after_s", "max_mfe_bp", "adverse_bp"})
        _reject_unknown_fields(data, fields, "early_fail")
        _require_fields(data, fields, "early_fail")
        return cls(**{name: data[name] for name in fields})


@dataclass(frozen=True, slots=True, kw_only=True)
class DcaLayer:
    layer_id: str
    trigger_adverse_bp: float
    additional_fraction: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "layer_id", _canonical_token(self.layer_id, "dca.layer_id")
        )
        object.__setattr__(
            self,
            "trigger_adverse_bp",
            _positive_float(self.trigger_adverse_bp, "dca.trigger_adverse_bp"),
        )
        fraction = _positive_float(
            self.additional_fraction, "dca.additional_fraction"
        )
        if fraction > 1.0:
            raise ValueError("dca.additional_fraction must not exceed 1")
        object.__setattr__(self, "additional_fraction", fraction)

    def to_payload(self) -> dict[str, Any]:
        return {
            "layer_id": self.layer_id,
            "trigger_adverse_bp": self.trigger_adverse_bp,
            "additional_fraction": self.additional_fraction,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DcaLayer":
        data = _mapping(value, "dca.layer")
        fields = frozenset(
            {"layer_id", "trigger_adverse_bp", "additional_fraction"}
        )
        _reject_unknown_fields(data, fields, "dca.layer")
        _require_fields(data, fields, "dca.layer")
        return cls(**{name: data[name] for name in fields})


@dataclass(frozen=True, slots=True, kw_only=True)
class DcaPolicy:
    enabled: bool = False
    layers: tuple[DcaLayer, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "enabled", _strict_bool(self.enabled, "dca.enabled"))
        if not isinstance(self.layers, tuple) or any(
            not isinstance(layer, DcaLayer) for layer in self.layers
        ):
            raise TypeError("dca.layers must be a tuple of DcaLayer")
        if self.enabled and not self.layers:
            raise ValueError("enabled dca requires at least one layer")
        if not self.enabled and self.layers:
            raise ValueError("disabled dca must not contain layers")
        triggers = [layer.trigger_adverse_bp for layer in self.layers]
        if any(right <= left for left, right in zip(triggers, triggers[1:])):
            raise ValueError("dca layer triggers must be strictly increasing")
        if sum(layer.additional_fraction for layer in self.layers) > 1.000001:
            raise ValueError("dca additional fractions must not exceed 1 in total")

    def to_payload(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "layers": [layer.to_payload() for layer in self.layers],
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DcaPolicy":
        data = _mapping(value, "dca")
        fields = frozenset({"enabled", "layers"})
        _reject_unknown_fields(data, fields, "dca")
        _require_fields(data, fields, "dca")
        raw_layers = data["layers"]
        if (
            isinstance(raw_layers, (str, bytes))
            or not isinstance(raw_layers, Sequence)
        ):
            raise TypeError("dca.layers must be a sequence")
        layers = tuple(
            item if isinstance(item, DcaLayer) else DcaLayer.from_mapping(item)
            for item in raw_layers
        )
        return cls(enabled=data["enabled"], layers=layers)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecutionProfile:
    profile_id: str
    entry_offset_bp: float
    entry_ttl_s: int
    maker_mode: str
    take_profits: tuple[TakeProfitLevel, ...]
    sl_bp: float
    max_hold_s: int
    reprice: RepricePolicy = field(default_factory=RepricePolicy)
    breakeven: BreakevenPolicy = field(default_factory=BreakevenPolicy)
    trail: TrailPolicy = field(default_factory=TrailPolicy)
    runner: RunnerPolicy = field(default_factory=RunnerPolicy)
    early_fail: EarlyFailPolicy = field(default_factory=EarlyFailPolicy)
    dca: DcaPolicy = field(default_factory=DcaPolicy)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "profile_id", _canonical_token(self.profile_id, "profile_id")
        )
        object.__setattr__(
            self,
            "entry_offset_bp",
            _canonical_float(self.entry_offset_bp, "entry_offset_bp"),
        )
        object.__setattr__(
            self, "entry_ttl_s", _positive_int(self.entry_ttl_s, "entry_ttl_s")
        )
        object.__setattr__(
            self, "maker_mode", _canonical_token(self.maker_mode, "maker_mode")
        )
        if self.maker_mode not in _MAKER_MODES:
            raise ValueError(
                "maker_mode must be POST_ONLY, PASSIVE_LIMIT, or MARKET"
            )
        if not isinstance(self.take_profits, tuple) or any(
            not isinstance(level, TakeProfitLevel) for level in self.take_profits
        ):
            raise TypeError("take_profits must be a tuple of TakeProfitLevel")
        if not self.take_profits:
            raise ValueError("take_profits must contain at least one level")
        targets = [level.target_bp for level in self.take_profits]
        if any(right <= left for left, right in zip(targets, targets[1:])):
            raise ValueError("take-profit targets must be strictly increasing")
        level_ids = [level.level_id for level in self.take_profits]
        if len(set(level_ids)) != len(level_ids):
            raise ValueError("take-profit level_id values must be unique")
        object.__setattr__(self, "sl_bp", _positive_float(self.sl_bp, "sl_bp"))
        object.__setattr__(
            self, "max_hold_s", _positive_int(self.max_hold_s, "max_hold_s")
        )
        for name, expected_type in (
            ("reprice", RepricePolicy),
            ("breakeven", BreakevenPolicy),
            ("trail", TrailPolicy),
            ("runner", RunnerPolicy),
            ("early_fail", EarlyFailPolicy),
            ("dca", DcaPolicy),
        ):
            if not isinstance(getattr(self, name), expected_type):
                raise TypeError(f"{name} must be {expected_type.__name__}")
        if self.reprice.enabled and self.reprice.after_s >= self.entry_ttl_s:
            raise ValueError("reprice.after_s must be below entry_ttl_s")
        if self.early_fail.enabled and self.early_fail.after_s >= self.max_hold_s:
            raise ValueError("early_fail.after_s must be below max_hold_s")

        total_fraction = sum(level.fraction for level in self.take_profits)
        if abs(total_fraction - 1.0) > 0.000001:
            raise ValueError("take-profit fractions must total exactly 1")
        if self.runner.enabled:
            final_level = self.take_profits[-1]
            if abs(final_level.fraction - self.runner.fraction) > 0.000001:
                raise ValueError(
                    "runner fraction must match the final take-profit fraction"
                )
            if (
                self.runner.take_profit_cap_bp > 0.0
                and abs(
                    final_level.target_bp - self.runner.take_profit_cap_bp
                )
                > 0.000001
            ):
                raise ValueError(
                    "runner take-profit cap must match the final take-profit target"
                )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": EXECUTION_PROFILE_SCHEMA,
            "profile_id": self.profile_id,
            "entry_offset_bp": self.entry_offset_bp,
            "entry_ttl_s": self.entry_ttl_s,
            "maker_mode": self.maker_mode,
            "reprice": self.reprice.to_payload(),
            "take_profits": [
                level.to_payload() for level in self.take_profits
            ],
            "sl_bp": self.sl_bp,
            "breakeven": self.breakeven.to_payload(),
            "max_hold_s": self.max_hold_s,
            "trail": self.trail.to_payload(),
            "runner": self.runner.to_payload(),
            "early_fail": self.early_fail.to_payload(),
            "dca": self.dca.to_payload(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ExecutionProfile":
        data = _mapping(value, "execution_profile")
        ignored = ABSOLUTE_PRICE_FIELDS | RUNTIME_FIELDS | RISK_CONTEXT_FIELDS
        _reject_unknown_fields(
            data, _EXECUTION_FIELDS, "execution_profile", ignored=ignored
        )
        _validate_schema(data, EXECUTION_PROFILE_SCHEMA, "execution_profile")
        required = _EXECUTION_FIELDS - {"schema"}
        _require_fields(data, required, "execution_profile")
        raw_take_profits = data["take_profits"]
        if (
            isinstance(raw_take_profits, (str, bytes))
            or not isinstance(raw_take_profits, Sequence)
        ):
            raise TypeError("take_profits must be a sequence")
        take_profits = tuple(
            item
            if isinstance(item, TakeProfitLevel)
            else TakeProfitLevel.from_mapping(item)
            for item in raw_take_profits
        )
        return cls(
            profile_id=data["profile_id"],
            entry_offset_bp=data["entry_offset_bp"],
            entry_ttl_s=data["entry_ttl_s"],
            maker_mode=data["maker_mode"],
            reprice=(
                data["reprice"]
                if isinstance(data["reprice"], RepricePolicy)
                else RepricePolicy.from_mapping(data["reprice"])
            ),
            take_profits=take_profits,
            sl_bp=data["sl_bp"],
            breakeven=(
                data["breakeven"]
                if isinstance(data["breakeven"], BreakevenPolicy)
                else BreakevenPolicy.from_mapping(data["breakeven"])
            ),
            max_hold_s=data["max_hold_s"],
            trail=(
                data["trail"]
                if isinstance(data["trail"], TrailPolicy)
                else TrailPolicy.from_mapping(data["trail"])
            ),
            runner=(
                data["runner"]
                if isinstance(data["runner"], RunnerPolicy)
                else RunnerPolicy.from_mapping(data["runner"])
            ),
            early_fail=(
                data["early_fail"]
                if isinstance(data["early_fail"], EarlyFailPolicy)
                else EarlyFailPolicy.from_mapping(data["early_fail"])
            ),
            dca=(
                data["dca"]
                if isinstance(data["dca"], DcaPolicy)
                else DcaPolicy.from_mapping(data["dca"])
            ),
        )

    @property
    def profile_hash(self) -> str:
        return canonical_sha256(self.to_payload())


def canonicalize_execution_profile(
    profile: ExecutionProfile | Mapping[str, Any],
) -> dict[str, Any]:
    resolved = (
        profile
        if isinstance(profile, ExecutionProfile)
        else ExecutionProfile.from_mapping(profile)
    )
    return resolved.to_payload()


def execution_profile_hash(
    profile: ExecutionProfile | Mapping[str, Any],
) -> str:
    return canonical_sha256(canonicalize_execution_profile(profile))


@dataclass(frozen=True, slots=True, kw_only=True)
class RiskPolicy:
    policy_id: str
    paid_notional_cap_usdc: float
    per_trade_loss_cap_usdc: float
    lane_open_notional_cap_usdc: float
    global_open_notional_cap_usdc: float
    daily_soft_loss_cap_usdc: float
    daily_hard_loss_cap_usdc: float
    daily_profit_lock_trigger_usdc: float
    daily_profit_lock_giveback_usdc: float
    max_consecutive_losses: int
    cooldown_s: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "policy_id", _canonical_token(self.policy_id, "policy_id")
        )
        for name in (
            "paid_notional_cap_usdc",
            "per_trade_loss_cap_usdc",
            "lane_open_notional_cap_usdc",
            "global_open_notional_cap_usdc",
            "daily_soft_loss_cap_usdc",
            "daily_hard_loss_cap_usdc",
        ):
            object.__setattr__(
                self, name, _positive_float(getattr(self, name), name)
            )
        for name in (
            "daily_profit_lock_trigger_usdc",
            "daily_profit_lock_giveback_usdc",
        ):
            object.__setattr__(
                self, name, _nonnegative_float(getattr(self, name), name)
            )
        object.__setattr__(
            self,
            "max_consecutive_losses",
            _positive_int(self.max_consecutive_losses, "max_consecutive_losses"),
        )
        object.__setattr__(
            self, "cooldown_s", _positive_int(self.cooldown_s, "cooldown_s")
        )

        if self.per_trade_loss_cap_usdc > self.paid_notional_cap_usdc:
            raise ValueError(
                "per_trade_loss_cap_usdc must not exceed paid_notional_cap_usdc"
            )
        if self.lane_open_notional_cap_usdc < self.paid_notional_cap_usdc:
            raise ValueError(
                "lane_open_notional_cap_usdc must cover paid_notional_cap_usdc"
            )
        if (
            self.global_open_notional_cap_usdc
            < self.lane_open_notional_cap_usdc
        ):
            raise ValueError(
                "global_open_notional_cap_usdc must cover lane_open_notional_cap_usdc"
            )
        if self.daily_hard_loss_cap_usdc < self.daily_soft_loss_cap_usdc:
            raise ValueError(
                "daily_hard_loss_cap_usdc must be at least daily_soft_loss_cap_usdc"
            )
        if self.daily_profit_lock_trigger_usdc == 0.0:
            if self.daily_profit_lock_giveback_usdc != 0.0:
                raise ValueError(
                    "profit-lock giveback must be zero when trigger is disabled"
                )
        elif (
            self.daily_profit_lock_giveback_usdc
            > self.daily_profit_lock_trigger_usdc
        ):
            raise ValueError(
                "daily_profit_lock_giveback_usdc must not exceed its trigger"
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": RISK_POLICY_SCHEMA,
            "policy_id": self.policy_id,
            "paid_notional_cap_usdc": self.paid_notional_cap_usdc,
            "per_trade_loss_cap_usdc": self.per_trade_loss_cap_usdc,
            "lane_open_notional_cap_usdc": self.lane_open_notional_cap_usdc,
            "global_open_notional_cap_usdc": self.global_open_notional_cap_usdc,
            "daily_soft_loss_cap_usdc": self.daily_soft_loss_cap_usdc,
            "daily_hard_loss_cap_usdc": self.daily_hard_loss_cap_usdc,
            "daily_profit_lock_trigger_usdc": (
                self.daily_profit_lock_trigger_usdc
            ),
            "daily_profit_lock_giveback_usdc": (
                self.daily_profit_lock_giveback_usdc
            ),
            "max_consecutive_losses": self.max_consecutive_losses,
            "cooldown_s": self.cooldown_s,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RiskPolicy":
        data = _mapping(value, "risk_policy")
        _reject_unknown_fields(data, _RISK_FIELDS, "risk_policy")
        _validate_schema(data, RISK_POLICY_SCHEMA, "risk_policy")
        required = _RISK_FIELDS - {"schema"}
        _require_fields(data, required, "risk_policy")
        return cls(**{name: data[name] for name in required})

    @property
    def policy_hash(self) -> str:
        return canonical_sha256(self.to_payload())


def canonicalize_risk_policy(
    policy: RiskPolicy | Mapping[str, Any],
) -> dict[str, Any]:
    resolved = (
        policy if isinstance(policy, RiskPolicy) else RiskPolicy.from_mapping(policy)
    )
    return resolved.to_payload()


def risk_policy_hash(policy: RiskPolicy | Mapping[str, Any]) -> str:
    return canonical_sha256(canonicalize_risk_policy(policy))


__all__ = [
    "ABSOLUTE_PRICE_FIELDS",
    "BreakevenPolicy",
    "DcaLayer",
    "DcaPolicy",
    "EarlyFailPolicy",
    "EXECUTION_PROFILE_SCHEMA",
    "ExecutionProfile",
    "MARKET_STATE_SCHEMA",
    "MarketStateIdentity",
    "RISK_POLICY_SCHEMA",
    "RISK_CONTEXT_FIELDS",
    "RUNTIME_FIELDS",
    "RepricePolicy",
    "RiskPolicy",
    "RunnerPolicy",
    "TakeProfitLevel",
    "TrailPolicy",
    "V1469_VERSION",
    "canonical_json",
    "canonical_sha256",
    "canonicalize_execution_profile",
    "canonicalize_risk_policy",
    "execution_profile_hash",
    "market_state_hash",
    "risk_policy_hash",
]
