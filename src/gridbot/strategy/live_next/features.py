"""Causal feature snapshots for Live Next replay and shadow."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .contracts import ContractError, canonical_json, canonical_sha256


DEFAULT_FEATURE_VERSION = "live_next.features.v1"


class FeatureLeakageError(ContractError):
    """Backward-compatible base for causal and outcome leakage failures."""


class CausalityViolation(FeatureLeakageError):
    """Raised when evidence was not causally available at decision time."""


class OutcomeLeakageError(FeatureLeakageError):
    """Raised when post-decision evidence is presented as an input feature."""


class FeatureRole(str, Enum):
    INPUT = "INPUT"
    OUTCOME = "OUTCOME"


_FORBIDDEN_OUTCOME_KEYS = frozenset(
    {
        "outcome",
        "outcome_id",
        "outcome_at_ms",
        "outcome_status",
        "status",
        "filled",
        "fill_at_ms",
        "filled_at_ms",
        "entry_fill_at_ms",
        "entry_filled_at_ms",
        "closed_at_ms",
        "entry_price",
        "fill_price",
        "exit_price",
        "quantity",
        "qty",
        "filled_quantity",
        "exit_reason",
        "realized_pnl",
        "realized_pnl_usdc",
        "gross_pnl",
        "gross_pnl_usdc",
        "cost_usdc",
        "all_in_cost_usdc",
        "net_pnl",
        "net_pnl_usdc",
        "mfe",
        "mfe_bp",
        "mae",
        "mae_bp",
        "label",
        "target",
        "future",
        "forward",
    }
)


def _validate_timestamp(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(f"{name} must be a non-negative integer")
    return value


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} is required")
    return value


def _normalize_quality_flags(flags: Iterable[str]) -> tuple[str, ...]:
    if isinstance(flags, (str, bytes)):
        raise ContractError("quality_flags must be an iterable of strings")
    try:
        values = tuple(flags)
    except TypeError as exc:
        raise ContractError("quality_flags must be an iterable of strings") from exc
    if any(not isinstance(flag, str) or not flag.strip() for flag in values):
        raise ContractError("quality_flags must contain non-empty strings")
    return tuple(sorted(set(values)))


def _is_forbidden_outcome_key(name: str) -> bool:
    return (
        name in _FORBIDDEN_OUTCOME_KEYS
        or name.startswith("future_")
        or name.startswith("forward_")
        or name.startswith("outcome_")
        or name.endswith("_outcome")
    )


def _assert_outcome_blind(value: Any, path: str = "features") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key).strip().lower()
            if _is_forbidden_outcome_key(name):
                raise OutcomeLeakageError(
                    f"outcome-derived feature is forbidden: {path}.{key}"
                )
            _assert_outcome_blind(item, f"{path}.{key}")
    elif isinstance(value, (tuple, list, set, frozenset)):
        for index, item in enumerate(value):
            _assert_outcome_blind(item, f"{path}[{index}]")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _freeze_canonical_value(value: Any) -> Any:
    return _freeze_json(json.loads(canonical_json(value)))


@dataclass(frozen=True, slots=True)
class FeatureObservation:
    """One immutable value with event and availability timestamps."""

    value: Any
    event_time_ms: int
    available_at_ms: int
    role: FeatureRole | str = FeatureRole.INPUT

    def __post_init__(self) -> None:
        _validate_timestamp(self.event_time_ms, "event_time_ms")
        _validate_timestamp(self.available_at_ms, "available_at_ms")
        if self.event_time_ms > self.available_at_ms:
            raise CausalityViolation(
                "event_time_ms cannot be after available_at_ms"
            )
        try:
            role = (
                self.role
                if isinstance(self.role, FeatureRole)
                else FeatureRole(str(self.role).strip().upper())
            )
        except (TypeError, ValueError) as exc:
            raise ContractError(f"unknown feature role: {self.role!r}") from exc
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "value", _freeze_canonical_value(self.value))


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    """Immutable, deterministic evidence available at one decision time."""

    decision_time_ms: int
    features: Mapping[str, FeatureObservation]
    quality_flags: tuple[str, ...] = ()
    feature_version: str = DEFAULT_FEATURE_VERSION
    schema_version: str = "live_next.features.v1"
    observed_at_ms: int = field(init=False)
    _legacy_event_time_ms: int | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        _validate_timestamp(self.decision_time_ms, "decision_time_ms")
        object.__setattr__(self, "observed_at_ms", self.decision_time_ms)
        _required_text(self.feature_version, "feature_version")
        _required_text(self.schema_version, "schema_version")
        if not isinstance(self.features, Mapping):
            raise ContractError(
                "features must be a mapping of FeatureObservation values"
            )

        items = tuple(self.features.items())
        if any(not isinstance(name, str) or not name.strip() for name, _ in items):
            raise ContractError("feature names must be non-empty strings")

        normalized: dict[str, FeatureObservation] = {}
        for name, observation in sorted(items, key=lambda item: item[0]):
            if not isinstance(observation, FeatureObservation):
                raise ContractError(
                    f"feature {name!r} must be a FeatureObservation"
                )
            if observation.available_at_ms > self.decision_time_ms:
                raise CausalityViolation(
                    f"feature {name!r} is available after decision time"
                )
            if observation.role is FeatureRole.OUTCOME:
                raise OutcomeLeakageError(
                    f"outcome-role feature is forbidden: features.{name}"
                )
            if _is_forbidden_outcome_key(name.strip().lower()):
                raise OutcomeLeakageError(
                    f"outcome-derived feature is forbidden: features.{name}"
                )
            _assert_outcome_blind(observation.value, f"features.{name}.value")
            normalized[name] = observation

        object.__setattr__(self, "features", MappingProxyType(normalized))
        object.__setattr__(
            self,
            "quality_flags",
            _normalize_quality_flags(self.quality_flags),
        )

    @classmethod
    def create(
        cls,
        *,
        observed_at_ms: int,
        market_data_max_event_ms: int,
        values: Mapping[str, Any],
        feature_version: str,
    ) -> "FeatureSnapshot":
        """Build the legacy snapshot shape on the causal observation model."""

        _validate_timestamp(observed_at_ms, "observed_at_ms")
        _validate_timestamp(
            market_data_max_event_ms,
            "market_data_max_event_ms",
        )
        if market_data_max_event_ms > observed_at_ms:
            raise CausalityViolation(
                "market data timestamp cannot exceed observation time"
            )
        if not isinstance(values, Mapping):
            raise ContractError("feature snapshot values must be an object")

        canonical_values = json.loads(canonical_json(values))
        if not isinstance(canonical_values, Mapping):
            raise ContractError("feature snapshot values must be an object")
        _assert_outcome_blind(canonical_values)

        snapshot = cls(
            decision_time_ms=observed_at_ms,
            features={
                name: FeatureObservation(
                    value=value,
                    event_time_ms=market_data_max_event_ms,
                    available_at_ms=observed_at_ms,
                )
                for name, value in canonical_values.items()
            },
            feature_version=feature_version,
        )
        object.__setattr__(
            snapshot,
            "_legacy_event_time_ms",
            market_data_max_event_ms,
        )
        return snapshot

    @property
    def values(self) -> dict[str, Any]:
        return dict(json.loads(self.values_json))

    def observation(self, name: str) -> FeatureObservation:
        return self.features[name]

    @property
    def event_time_ms(self) -> int:
        if self.features:
            return max(item.event_time_ms for item in self.features.values())
        if self._legacy_event_time_ms is not None:
            return self._legacy_event_time_ms
        return self.decision_time_ms

    @property
    def available_at_ms(self) -> int:
        if self.features:
            return max(item.available_at_ms for item in self.features.values())
        return self.decision_time_ms

    @property
    def feature_snapshot_id(self) -> str:
        digest = canonical_sha256(
            {
                "available_at_ms": self.available_at_ms,
                "decision_time_ms": self.decision_time_ms,
                "event_time_ms": self.event_time_ms,
                "feature_version": self.feature_version,
                "features": {
                    name: {
                        "available_at_ms": observation.available_at_ms,
                        "event_time_ms": observation.event_time_ms,
                        "role": observation.role.value,
                        "value": observation.value,
                    }
                    for name, observation in self.features.items()
                },
                "quality_flags": self.quality_flags,
                "schema_version": self.schema_version,
            }
        )
        return f"lnfs_{digest[:24]}"

    def is_stale(
        self,
        max_age: int,
        feature_names: Iterable[str] | None = None,
    ) -> bool:
        _validate_timestamp(max_age, "max_age")
        if feature_names is None:
            names = tuple(self.features)
        elif isinstance(feature_names, str):
            names = (feature_names,)
        else:
            try:
                names = tuple(feature_names)
            except TypeError as exc:
                raise ContractError(
                    "feature_names must be an iterable of strings"
                ) from exc
        if any(not isinstance(name, str) or not name for name in names):
            raise ContractError("feature_names must contain non-empty strings")
        if not names:
            return False
        oldest_event_ms = min(
            self.observation(name).event_time_ms for name in names
        )
        return self.decision_time_ms - oldest_event_ms > max_age

    @property
    def market_data_max_event_ms(self) -> int:
        return self.event_time_ms

    @property
    def values_json(self) -> str:
        return canonical_json(
            {
                name: observation.value
                for name, observation in self.features.items()
            }
        )

    @property
    def feature_hash(self) -> str:
        return canonical_sha256(
            {
                name: observation.value
                for name, observation in self.features.items()
            }
        )

    @property
    def data_age_ms(self) -> int:
        return self.observed_at_ms - self.market_data_max_event_ms

    def assert_usable_at(self, decided_at_ms: int) -> None:
        _validate_timestamp(decided_at_ms, "decided_at_ms")
        if decided_at_ms < self.observed_at_ms:
            raise CausalityViolation(
                "decision cannot use a feature snapshot from the future"
            )


def build_feature_snapshot(
    decision_time_ms: int,
    observations: Mapping[str, FeatureObservation],
    quality_flags: Iterable[str] = (),
    feature_version: str = DEFAULT_FEATURE_VERSION,
) -> FeatureSnapshot:
    return FeatureSnapshot(
        decision_time_ms=decision_time_ms,
        features=observations,
        quality_flags=quality_flags,  # type: ignore[arg-type]
        feature_version=feature_version,
    )


__all__ = [
    "CausalityViolation",
    "FeatureLeakageError",
    "FeatureObservation",
    "FeatureRole",
    "FeatureSnapshot",
    "OutcomeLeakageError",
    "build_feature_snapshot",
]
