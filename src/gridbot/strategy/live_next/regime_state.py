"""Bounded, deterministic market-regime state for Live Next.

This module owns classification state only: it does not select strategies or
place orders.  Normal transitions require both hysteresis/confirmation and a
minimum dwell.  Fresh SHOCK evidence preempts immediately, while stale or
invalidly ordered data immediately fails closed to UNCERTAIN.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from math import isfinite
from typing import Iterable

from .features import FeatureSnapshot


class RegimeInputError(ValueError):
    """Raised for malformed or non-causal regime evidence."""


class TimestampOrderError(RegimeInputError):
    """Raised when decision timestamps move backwards during replay."""


class Regime(str, Enum):
    TREND = "TREND"
    RANGE = "RANGE"
    SHOCK = "SHOCK"
    UNCERTAIN = "UNCERTAIN"


class RegimeDirection(str, Enum):
    UP = "UP"
    DOWN = "DOWN"
    NONE = "NONE"


# Naming aliases keep the public vocabulary explicit without duplicating types.
RegimeKind = Regime
Direction = RegimeDirection


@dataclass(frozen=True)
class RegimeFeatureKeys:
    """Injectable mapping from feature names to bounded regime scores."""

    trend: str = "trend_score"
    range: str = "range_score"
    shock: str = "shock_score"
    direction: str | None = "direction_score"

    def __post_init__(self) -> None:
        for name, value in (
            ("trend", self.trend),
            ("range", self.range),
            ("shock", self.shock),
        ):
            if not isinstance(value, str) or not value.strip():
                raise RegimeInputError(f"{name} feature key must be non-empty")
        if self.direction is not None and (
            not isinstance(self.direction, str) or not self.direction.strip()
        ):
            raise RegimeInputError("direction feature key must be non-empty or None")
        if len({self.trend, self.range, self.shock}) != 3:
            raise RegimeInputError("trend, range, and shock feature keys must be distinct")


@dataclass(frozen=True)
class RegimeConfig:
    """Thresholds and transition bounds for the finite state machine."""

    trend_enter: float = 0.65
    trend_exit: float = 0.45
    range_enter: float = 0.65
    range_exit: float = 0.45
    shock_enter: float = 0.80
    shock_exit: float = 0.50
    switch_margin: float = 0.08
    direction_deadband: float = 0.05
    confirmations: int = 2
    min_dwell_ms: int = 15_000
    stale_after_ms: int = 5_000
    blocking_quality_flags: tuple[str, ...] = (
        "causality_violation",
        "invalid_data",
        "missing_data",
        "timestamp_inversion",
    )

    def __post_init__(self) -> None:
        for name in (
            "trend_enter",
            "trend_exit",
            "range_enter",
            "range_exit",
            "shock_enter",
            "shock_exit",
            "switch_margin",
            "direction_deadband",
        ):
            _validate_unit_interval(name, getattr(self, name))
        if self.trend_exit >= self.trend_enter:
            raise RegimeInputError("trend_exit must be lower than trend_enter")
        if self.range_exit >= self.range_enter:
            raise RegimeInputError("range_exit must be lower than range_enter")
        if self.shock_exit >= self.shock_enter:
            raise RegimeInputError("shock_exit must be lower than shock_enter")
        if self.direction_deadband >= self.trend_enter:
            raise RegimeInputError("direction_deadband must be lower than trend_enter")
        if (
            not isinstance(self.confirmations, int)
            or isinstance(self.confirmations, bool)
            or self.confirmations < 1
        ):
            raise RegimeInputError("confirmations must be a positive integer")
        _validate_duration("min_dwell_ms", self.min_dwell_ms)
        _validate_duration("stale_after_ms", self.stale_after_ms)
        flags = _normalize_flags(self.blocking_quality_flags)
        object.__setattr__(self, "blocking_quality_flags", flags)

    @property
    def trend_enter_threshold(self) -> float:
        return self.trend_enter

    @property
    def trend_exit_threshold(self) -> float:
        return self.trend_exit

    @property
    def range_enter_threshold(self) -> float:
        return self.range_enter

    @property
    def range_exit_threshold(self) -> float:
        return self.range_exit

    @property
    def shock_enter_threshold(self) -> float:
        return self.shock_enter

    @property
    def shock_exit_threshold(self) -> float:
        return self.shock_exit


RegimeStateConfig = RegimeConfig


@dataclass(frozen=True)
class RegimeEvidence:
    """Normalized causal inputs consumed by one state transition.

    ``trend_score`` is signed in ``[-1, 1]``; its magnitude is trend strength.
    Range and shock scores are in ``[0, 1]``.  ``direction_score`` can provide
    a separate signed direction for SHOCK, otherwise trend direction is used.
    """

    decision_time_ms: int
    event_time_ms: int
    available_at_ms: int
    trend_score: float
    range_score: float
    shock_score: float
    direction_score: float | None = None
    quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_timestamp("decision_time_ms", self.decision_time_ms)
        _validate_timestamp("event_time_ms", self.event_time_ms)
        _validate_timestamp("available_at_ms", self.available_at_ms)
        if self.event_time_ms > self.available_at_ms:
            raise RegimeInputError("event_time_ms cannot be after available_at_ms")
        if self.available_at_ms > self.decision_time_ms:
            raise RegimeInputError(
                "regime evidence was not available by decision_time_ms"
            )
        _validate_signed_score("trend_score", self.trend_score)
        _validate_unit_interval("range_score", self.range_score)
        _validate_unit_interval("shock_score", self.shock_score)
        if self.direction_score is not None:
            _validate_signed_score("direction_score", self.direction_score)
        object.__setattr__(self, "trend_score", float(self.trend_score))
        object.__setattr__(self, "range_score", float(self.range_score))
        object.__setattr__(self, "shock_score", float(self.shock_score))
        if self.direction_score is not None:
            object.__setattr__(self, "direction_score", float(self.direction_score))
        object.__setattr__(self, "quality_flags", _normalize_flags(self.quality_flags))

    @property
    def age_ms(self) -> int:
        return self.decision_time_ms - self.event_time_ms

    @property
    def effective_direction_score(self) -> float:
        return self.trend_score if self.direction_score is None else self.direction_score

    @classmethod
    def from_snapshot(
        cls,
        snapshot: FeatureSnapshot,
        keys: RegimeFeatureKeys | None = None,
        *,
        decision_time_ms: int | None = None,
    ) -> "RegimeEvidence":
        """Extract bounded scores from the immutable feature-row contract.

        ``FeatureSnapshot.observed_at_ms`` is the row's availability time. A
        later decision time may be supplied for replay; the snapshot validates
        that the decision did not happen before availability.
        """

        keys = keys or RegimeFeatureKeys()
        decided_at_ms = (
            snapshot.observed_at_ms
            if decision_time_ms is None
            else decision_time_ms
        )
        try:
            snapshot.assert_usable_at(decided_at_ms)
        except ValueError as exc:
            raise RegimeInputError("feature snapshot is not causal for decision") from exc

        snapshot_values = snapshot.values
        observations = []
        values: list[float] = []
        for name in (keys.trend, keys.range, keys.shock):
            try:
                value = snapshot_values[name]
                observation = snapshot.observation(name)
            except KeyError as exc:
                raise RegimeInputError(f"missing regime feature: {name}") from exc
            values.append(_numeric_feature(name, value))
            observations.append(observation)

        direction_score: float | None = None
        if keys.direction is not None and keys.direction in snapshot_values:
            direction_score = _numeric_feature(keys.direction, snapshot_values[keys.direction])
            observations.append(snapshot.observation(keys.direction))

        return cls(
            decision_time_ms=decided_at_ms,
            event_time_ms=min(item.event_time_ms for item in observations),
            available_at_ms=max(item.available_at_ms for item in observations),
            trend_score=values[0],
            range_score=values[1],
            shock_score=values[2],
            direction_score=direction_score,
            quality_flags=snapshot.quality_flags,
        )


@dataclass(frozen=True)
class RegimeState:
    """Complete replayable state, including a pending transition candidate."""

    regime: Regime | str
    direction: RegimeDirection | str
    confidence: float
    since_ms: int
    last_decision_time_ms: int
    last_event_time_ms: int
    last_available_at_ms: int
    transition_count: int = 0
    pending_regime: Regime | str | None = None
    pending_direction: RegimeDirection | str = RegimeDirection.NONE
    pending_count: int = 0
    transitioned: bool = False
    reason: str = "initialized"

    def __post_init__(self) -> None:
        regime = _coerce_regime(self.regime)
        direction = _coerce_direction(self.direction)
        pending_regime = (
            None if self.pending_regime is None else _coerce_regime(self.pending_regime)
        )
        pending_direction = _coerce_direction(self.pending_direction)
        object.__setattr__(self, "regime", regime)
        object.__setattr__(self, "direction", direction)
        object.__setattr__(self, "pending_regime", pending_regime)
        object.__setattr__(self, "pending_direction", pending_direction)

        _validate_unit_interval("confidence", self.confidence)
        object.__setattr__(self, "confidence", float(self.confidence))
        for name in (
            "since_ms",
            "last_decision_time_ms",
            "last_event_time_ms",
            "last_available_at_ms",
        ):
            _validate_timestamp(name, getattr(self, name))
        if not (
            self.last_event_time_ms
            <= self.last_available_at_ms
            <= self.last_decision_time_ms
        ):
            raise RegimeInputError("state timestamps violate causal ordering")
        if self.since_ms > self.last_decision_time_ms:
            raise RegimeInputError("since_ms cannot be in the future")
        for name in ("transition_count", "pending_count"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise RegimeInputError(f"{name} must be a non-negative integer")
        if not isinstance(self.transitioned, bool):
            raise RegimeInputError("transitioned must be a bool")
        if not isinstance(self.reason, str) or not self.reason:
            raise RegimeInputError("reason must be a non-empty string")
        if regime in {Regime.RANGE, Regime.UNCERTAIN} and direction is not RegimeDirection.NONE:
            raise RegimeInputError(f"{regime.value} cannot carry a direction")
        if pending_regime is None:
            if self.pending_count != 0 or pending_direction is not RegimeDirection.NONE:
                raise RegimeInputError("empty pending state must have zero count/direction")
        elif self.pending_count < 1:
            raise RegimeInputError("pending state must have at least one confirmation")
        elif pending_regime in {Regime.RANGE, Regime.UNCERTAIN} and pending_direction is not RegimeDirection.NONE:
            raise RegimeInputError(f"pending {pending_regime.value} cannot carry a direction")

    @property
    def state_name(self) -> str:
        if self.direction is RegimeDirection.NONE:
            return self.regime.value
        return f"{self.regime.value}_{self.direction.value}"

    @property
    def dwell_ms(self) -> int:
        return self.last_decision_time_ms - self.since_ms


@dataclass(frozen=True)
class _Proposal:
    regime: Regime
    direction: RegimeDirection
    confidence: float
    reason: str


class RegimeStateMachine:
    """Small stateful wrapper around the pure ``transition_regime`` function."""

    def __init__(
        self,
        config: RegimeConfig | None = None,
        *,
        initial_state: RegimeState | None = None,
        feature_keys: RegimeFeatureKeys | None = None,
    ) -> None:
        self._config = config or RegimeConfig()
        self._state = initial_state
        self._feature_keys = feature_keys or RegimeFeatureKeys()

    @property
    def config(self) -> RegimeConfig:
        return self._config

    @property
    def state(self) -> RegimeState | None:
        return self._state

    def update(
        self,
        evidence: RegimeEvidence | FeatureSnapshot,
    ) -> RegimeState:
        if isinstance(evidence, FeatureSnapshot):
            evidence = RegimeEvidence.from_snapshot(evidence, self._feature_keys)
        if not isinstance(evidence, RegimeEvidence):
            raise RegimeInputError("update requires RegimeEvidence or FeatureSnapshot")
        self._state = transition_regime(self._state, evidence, self._config)
        return self._state

    step = update

    def reset(self, state: RegimeState | None = None) -> None:
        self._state = state


def transition_regime(
    previous: RegimeState | None,
    evidence: RegimeEvidence,
    config: RegimeConfig | None = None,
) -> RegimeState:
    """Pure deterministic state transition suitable for offline replay."""

    config = config or RegimeConfig()
    if not isinstance(evidence, RegimeEvidence):
        raise RegimeInputError("evidence must be RegimeEvidence")
    if previous is not None:
        if evidence.decision_time_ms < previous.last_decision_time_ms:
            raise TimestampOrderError("decision_time_ms moved backwards")
        if evidence.decision_time_ms == previous.last_decision_time_ms:
            # Duplicate scans at one logical decision time must not accumulate
            # confirmations or produce a second transition.
            return previous

    current = previous or _initial_uncertain(evidence)
    blocking_flags = sorted(
        set(evidence.quality_flags).intersection(config.blocking_quality_flags)
    )
    if blocking_flags:
        return _force_uncertain(
            current, evidence, f"quality_flag:{blocking_flags[0]}"
        )
    if evidence.age_ms > config.stale_after_ms:
        return _force_uncertain(current, evidence, "stale_data")
    if previous is not None and evidence.event_time_ms < previous.last_event_time_ms:
        return _force_uncertain(current, evidence, "event_time_inversion")

    if evidence.shock_score >= config.shock_enter:
        direction = (
            current.direction
            if current.regime is Regime.SHOCK
            else _direction(evidence.effective_direction_score, config)
        )
        return _immediate_state(
            current,
            evidence,
            Regime.SHOCK,
            direction,
            evidence.shock_score,
            "shock_preempted",
        )

    proposal = _propose(current, evidence, config)
    if _same_state(current, proposal):
        return replace(
            current,
            confidence=proposal.confidence,
            last_decision_time_ms=evidence.decision_time_ms,
            last_event_time_ms=evidence.event_time_ms,
            last_available_at_ms=evidence.available_at_ms,
            pending_regime=None,
            pending_direction=RegimeDirection.NONE,
            pending_count=0,
            transitioned=False,
            reason=proposal.reason,
        )

    same_pending = (
        current.pending_regime is proposal.regime
        and current.pending_direction is proposal.direction
    )
    pending_count = current.pending_count + 1 if same_pending else 1
    dwell_ready = (
        evidence.decision_time_ms - current.since_ms >= config.min_dwell_ms
    )
    confirmations_ready = pending_count >= config.confirmations
    if dwell_ready and confirmations_ready:
        return RegimeState(
            regime=proposal.regime,
            direction=proposal.direction,
            confidence=proposal.confidence,
            since_ms=evidence.decision_time_ms,
            last_decision_time_ms=evidence.decision_time_ms,
            last_event_time_ms=evidence.event_time_ms,
            last_available_at_ms=evidence.available_at_ms,
            transition_count=current.transition_count + 1,
            transitioned=True,
            reason=f"transition:{proposal.reason}",
        )

    if not dwell_ready and not confirmations_ready:
        wait_reason = "awaiting_confirmation_and_minimum_dwell"
    elif not dwell_ready:
        wait_reason = "minimum_dwell"
    else:
        wait_reason = "awaiting_confirmation"
    return replace(
        current,
        confidence=_current_confidence(current, evidence),
        last_decision_time_ms=evidence.decision_time_ms,
        last_event_time_ms=evidence.event_time_ms,
        last_available_at_ms=evidence.available_at_ms,
        pending_regime=proposal.regime,
        pending_direction=proposal.direction,
        pending_count=pending_count,
        transitioned=False,
        reason=wait_reason,
    )


update_regime_state = transition_regime
next_regime_state = transition_regime


def _initial_uncertain(evidence: RegimeEvidence) -> RegimeState:
    return RegimeState(
        regime=Regime.UNCERTAIN,
        direction=RegimeDirection.NONE,
        confidence=0.0,
        since_ms=evidence.decision_time_ms,
        last_decision_time_ms=evidence.decision_time_ms,
        last_event_time_ms=evidence.event_time_ms,
        last_available_at_ms=evidence.available_at_ms,
        reason="initialized",
    )


def _force_uncertain(
    current: RegimeState,
    evidence: RegimeEvidence,
    reason: str,
) -> RegimeState:
    return _immediate_state(
        current,
        evidence,
        Regime.UNCERTAIN,
        RegimeDirection.NONE,
        0.0,
        reason,
    )


def _immediate_state(
    current: RegimeState,
    evidence: RegimeEvidence,
    regime: Regime,
    direction: RegimeDirection,
    confidence: float,
    reason: str,
) -> RegimeState:
    if current.regime is regime and current.direction is direction:
        return replace(
            current,
            confidence=confidence,
            last_decision_time_ms=evidence.decision_time_ms,
            last_event_time_ms=evidence.event_time_ms,
            last_available_at_ms=evidence.available_at_ms,
            pending_regime=None,
            pending_direction=RegimeDirection.NONE,
            pending_count=0,
            transitioned=False,
            reason=reason,
        )
    return RegimeState(
        regime=regime,
        direction=direction,
        confidence=confidence,
        since_ms=evidence.decision_time_ms,
        last_decision_time_ms=evidence.decision_time_ms,
        last_event_time_ms=evidence.event_time_ms,
        last_available_at_ms=evidence.available_at_ms,
        transition_count=current.transition_count + 1,
        transitioned=True,
        reason=reason,
    )


def _propose(
    current: RegimeState,
    evidence: RegimeEvidence,
    config: RegimeConfig,
) -> _Proposal:
    trend_strength = abs(evidence.trend_score)
    trend_direction = _direction(evidence.trend_score, config)

    if current.regime is Regime.SHOCK and evidence.shock_score >= config.shock_exit:
        return _Proposal(
            Regime.SHOCK,
            current.direction,
            evidence.shock_score,
            "shock_exit_hysteresis",
        )

    if current.regime is Regime.TREND:
        same_direction = (
            trend_direction is current.direction
            and trend_direction is not RegimeDirection.NONE
        )
        if same_direction and trend_strength >= config.trend_exit:
            range_wins = (
                evidence.range_score >= config.range_enter
                and evidence.range_score >= trend_strength + config.switch_margin
            )
            if not range_wins:
                return _Proposal(
                    Regime.TREND,
                    current.direction,
                    trend_strength,
                    "trend_exit_hysteresis",
                )

    if current.regime is Regime.RANGE and evidence.range_score >= config.range_exit:
        trend_wins = (
            trend_strength >= config.trend_enter
            and trend_strength >= evidence.range_score + config.switch_margin
        )
        if not trend_wins:
            return _Proposal(
                Regime.RANGE,
                RegimeDirection.NONE,
                evidence.range_score,
                "range_exit_hysteresis",
            )

    return _entrant_proposal(evidence, trend_strength, trend_direction, config)


def _entrant_proposal(
    evidence: RegimeEvidence,
    trend_strength: float,
    trend_direction: RegimeDirection,
    config: RegimeConfig,
) -> _Proposal:
    trend_ready = (
        trend_strength >= config.trend_enter
        and trend_direction is not RegimeDirection.NONE
    )
    range_ready = evidence.range_score >= config.range_enter
    if trend_ready and range_ready:
        if trend_strength >= evidence.range_score + config.switch_margin:
            return _Proposal(
                Regime.TREND, trend_direction, trend_strength, "trend_margin_confirmed"
            )
        if evidence.range_score >= trend_strength + config.switch_margin:
            return _Proposal(
                Regime.RANGE,
                RegimeDirection.NONE,
                evidence.range_score,
                "range_margin_confirmed",
            )
        return _Proposal(
            Regime.UNCERTAIN,
            RegimeDirection.NONE,
            _uncertain_confidence(evidence),
            "ambiguous_trend_range",
        )
    if trend_ready:
        return _Proposal(
            Regime.TREND, trend_direction, trend_strength, "trend_enter_confirmed"
        )
    if range_ready:
        return _Proposal(
            Regime.RANGE,
            RegimeDirection.NONE,
            evidence.range_score,
            "range_enter_confirmed",
        )
    return _Proposal(
        Regime.UNCERTAIN,
        RegimeDirection.NONE,
        _uncertain_confidence(evidence),
        "insufficient_regime_evidence",
    )


def _same_state(current: RegimeState, proposal: _Proposal) -> bool:
    return current.regime is proposal.regime and current.direction is proposal.direction


def _current_confidence(current: RegimeState, evidence: RegimeEvidence) -> float:
    if current.regime is Regime.TREND:
        return abs(evidence.trend_score)
    if current.regime is Regime.RANGE:
        return evidence.range_score
    if current.regime is Regime.SHOCK:
        return evidence.shock_score
    return _uncertain_confidence(evidence)


def _uncertain_confidence(evidence: RegimeEvidence) -> float:
    strongest = max(
        abs(evidence.trend_score), evidence.range_score, evidence.shock_score
    )
    return max(0.0, min(1.0, 1.0 - strongest))


def _direction(score: float, config: RegimeConfig) -> RegimeDirection:
    if score > config.direction_deadband:
        return RegimeDirection.UP
    if score < -config.direction_deadband:
        return RegimeDirection.DOWN
    return RegimeDirection.NONE


def _numeric_feature(name: str, value: object) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not isfinite(float(value))
    ):
        raise RegimeInputError(f"regime feature {name!r} must be finite numeric")
    return float(value)


def _coerce_regime(value: Regime | str) -> Regime:
    try:
        return value if isinstance(value, Regime) else Regime(value)
    except (TypeError, ValueError) as exc:
        raise RegimeInputError(f"unknown regime: {value!r}") from exc


def _coerce_direction(value: RegimeDirection | str) -> RegimeDirection:
    try:
        return value if isinstance(value, RegimeDirection) else RegimeDirection(value)
    except (TypeError, ValueError) as exc:
        raise RegimeInputError(f"unknown regime direction: {value!r}") from exc


def _validate_timestamp(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RegimeInputError(f"{name} must be a non-negative integer")


def _validate_duration(name: str, value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RegimeInputError(f"{name} must be a non-negative integer")


def _validate_unit_interval(name: str, value: object) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not isfinite(float(value))
        or not 0.0 <= float(value) <= 1.0
    ):
        raise RegimeInputError(f"{name} must be finite and in [0, 1]")


def _validate_signed_score(name: str, value: object) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not isfinite(float(value))
        or not -1.0 <= float(value) <= 1.0
    ):
        raise RegimeInputError(f"{name} must be finite and in [-1, 1]")


def _normalize_flags(flags: Iterable[str]) -> tuple[str, ...]:
    try:
        normalized = tuple(sorted(set(flags)))
    except TypeError as exc:
        raise RegimeInputError("quality flags must be an iterable of strings") from exc
    if any(not isinstance(flag, str) or not flag.strip() for flag in normalized):
        raise RegimeInputError("quality flags must contain non-empty strings")
    return normalized
