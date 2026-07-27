"""Immutable, outcome-blind contracts shared by replay and shadow.

The paid path is deliberately absent.  These records make opportunity
deduplication, causal decisions, and cost-after outcomes independently
auditable before any mainnet wiring is considered.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from hashlib import sha256
import json
from math import isfinite
from typing import Any, Mapping


class ContractError(ValueError):
    """Raised when evidence is incomplete, non-causal, or contradictory."""


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class DecisionAction(str, Enum):
    ACCEPT = "ACCEPT"
    SKIP = "SKIP"
    BLOCK = "BLOCK"


class OutcomeStatus(str, Enum):
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"
    ENTRY_EXPIRED = "ENTRY_EXPIRED"
    ENTRY_REJECTED = "ENTRY_REJECTED"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"


OPPORTUNITY_SCHEMA_VERSION = "live_next.opportunity.v1"
DECISION_SCHEMA_VERSION = "live_next.decision.v1"
OUTCOME_SCHEMA_VERSION = "live_next.outcome.v1"


def _canonicalize(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            item.name: _canonicalize(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ContractError("canonical evidence object keys must be strings")
        return {
            key: _canonicalize(item)
            for key, item in sorted(value.items())
        }
    if isinstance(value, (tuple, list)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_canonicalize(item) for item in value)
    if isinstance(value, float) and not isfinite(value):
        raise ContractError("canonical evidence cannot contain non-finite floats")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ContractError(f"unsupported canonical evidence type: {type(value).__name__}")


def canonical_dict(value: Any) -> dict[str, Any]:
    """Return a detached, recursively canonical JSON object."""

    serializer = getattr(value, "to_dict", None)
    if is_dataclass(value) and callable(serializer):
        normalized = _canonicalize(serializer())
    else:
        normalized = _canonicalize(value)
    if not isinstance(normalized, dict):
        raise ContractError("canonical dict root must be an object")
    return normalized


def canonical_json(value: Any) -> str:
    """Return stable JSON suitable for hashing and durable evidence."""

    try:
        normalized = (
            canonical_dict(value)
            if is_dataclass(value) or isinstance(value, Mapping)
            else _canonicalize(value)
        )
        return json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except ContractError:
        raise
    except (TypeError, ValueError) as exc:
        raise ContractError("evidence must be canonical JSON") from exc


def canonical_sha256(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} is required")
    return value


def _timestamp(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContractError(f"{name} must be a non-negative integer")
    return value


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"{name} must be numeric")
    result = float(value)
    if not isfinite(result):
        raise ContractError(f"{name} must be finite")
    return result


def _stable_id(prefix: str, payload: Mapping[str, Any], length: int = 24) -> str:
    return f"{prefix}_{canonical_sha256(payload)[:length]}"


@dataclass(frozen=True, slots=True)
class Opportunity:
    """One deduplicated, causally observed market opportunity."""

    opportunity_id: str
    session_id: str
    observed_at_ms: int
    market_data_max_event_ms: int
    symbol: str
    side: Side
    expert_family: str
    anchor_event_id: str
    regime: str
    regime_version: str
    cooldown_bucket: int
    feature_payload_json: str
    feature_hash: str
    config_hash: str
    schema_version: str = OPPORTUNITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "opportunity_id",
            "session_id",
            "symbol",
            "expert_family",
            "anchor_event_id",
            "regime",
            "regime_version",
            "feature_hash",
            "config_hash",
            "schema_version",
        ):
            _required_text(getattr(self, name), name)
        if self.schema_version != OPPORTUNITY_SCHEMA_VERSION:
            raise ContractError("unsupported opportunity schema_version")
        object.__setattr__(self, "side", Side(self.side))
        _timestamp(self.observed_at_ms, "observed_at_ms")
        _timestamp(self.market_data_max_event_ms, "market_data_max_event_ms")
        if self.market_data_max_event_ms > self.observed_at_ms:
            raise ContractError("market data cannot arrive after the opportunity observation")
        if isinstance(self.cooldown_bucket, bool) or not isinstance(self.cooldown_bucket, int):
            raise ContractError("cooldown_bucket must be an integer")
        if self.cooldown_bucket < 0:
            raise ContractError("cooldown_bucket must be non-negative")
        try:
            feature_payload = json.loads(self.feature_payload_json)
        except (TypeError, ValueError) as exc:
            raise ContractError("feature_payload_json must be valid JSON") from exc
        if not isinstance(feature_payload, Mapping):
            raise ContractError("feature payload must be an object")
        canonical_payload = canonical_json(feature_payload)
        if canonical_payload != self.feature_payload_json:
            raise ContractError("feature payload must use canonical JSON")
        if canonical_sha256(feature_payload) != self.feature_hash:
            raise ContractError("feature_hash does not match feature payload")
        expected_id = self.build_id(
            symbol=self.symbol,
            expert_family=self.expert_family,
            side=self.side,
            anchor_event_id=self.anchor_event_id,
            regime_version=self.regime_version,
            cooldown_bucket=self.cooldown_bucket,
        )
        if self.opportunity_id != expected_id:
            raise ContractError("opportunity_id does not match its identity fields")

    @staticmethod
    def build_id(
        *,
        symbol: str,
        expert_family: str,
        side: Side | str,
        anchor_event_id: str,
        regime_version: str,
        cooldown_bucket: int,
    ) -> str:
        return _stable_id(
            "lnopp",
            {
                "anchor_event_id": anchor_event_id,
                "cooldown_bucket": cooldown_bucket,
                "expert_family": expert_family,
                "regime_version": regime_version,
                "side": Side(side).value,
                "symbol": symbol,
            },
        )

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        observed_at_ms: int,
        market_data_max_event_ms: int,
        symbol: str,
        side: Side | str,
        expert_family: str,
        anchor_event_id: str,
        regime: str,
        regime_version: str,
        cooldown_bucket: int,
        features: Mapping[str, Any],
        config_hash: str,
    ) -> "Opportunity":
        payload_json = canonical_json(features)
        normalized_side = Side(side)
        return cls(
            opportunity_id=cls.build_id(
                symbol=symbol,
                expert_family=expert_family,
                side=normalized_side,
                anchor_event_id=anchor_event_id,
                regime_version=regime_version,
                cooldown_bucket=cooldown_bucket,
            ),
            session_id=session_id,
            observed_at_ms=observed_at_ms,
            market_data_max_event_ms=market_data_max_event_ms,
            symbol=symbol,
            side=normalized_side,
            expert_family=expert_family,
            anchor_event_id=anchor_event_id,
            regime=regime,
            regime_version=regime_version,
            cooldown_bucket=cooldown_bucket,
            feature_payload_json=payload_json,
            feature_hash=canonical_sha256(features),
            config_hash=config_hash,
        )

    @property
    def features(self) -> dict[str, Any]:
        return dict(json.loads(self.feature_payload_json))

    def to_dict(self) -> dict[str, Any]:
        payload = _canonicalize(self)
        payload["features"] = json.loads(payload.pop("feature_payload_json"))
        return payload

    def to_canonical_dict(self) -> dict[str, Any]:
        return self.to_dict()


@dataclass(frozen=True, slots=True)
class Decision:
    """One outcome-blind policy decision for one opportunity."""

    decision_id: str
    opportunity_id: str
    session_id: str
    symbol: str
    side: Side
    observed_at_ms: int
    decided_at_ms: int
    action: DecisionAction
    reason: str
    score: float
    threshold: float
    policy_version: str
    config_hash: str
    feature_hash: str
    expert_id: str
    execution_profile_id: str | None = None
    exit_profile_id: str | None = None
    schema_version: str = DECISION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "decision_id",
            "opportunity_id",
            "symbol",
            "session_id",
            "reason",
            "policy_version",
            "config_hash",
            "feature_hash",
            "expert_id",
            "schema_version",
        ):
            _required_text(getattr(self, name), name)
        if self.schema_version != DECISION_SCHEMA_VERSION:
            raise ContractError("unsupported decision schema_version")
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(self, "action", DecisionAction(self.action))
        _timestamp(self.observed_at_ms, "observed_at_ms")
        _timestamp(self.decided_at_ms, "decided_at_ms")
        if self.decided_at_ms < self.observed_at_ms:
            raise ContractError("decision cannot precede opportunity observation")
        score = _finite(self.score, "score")
        threshold = _finite(self.threshold, "threshold")
        if not 0.0 <= score <= 100.0 or not 0.0 <= threshold <= 100.0:
            raise ContractError("score and threshold must be between 0 and 100")
        if self.action is DecisionAction.ACCEPT:
            _required_text(self.execution_profile_id, "execution_profile_id")
            _required_text(self.exit_profile_id, "exit_profile_id")
            if score < threshold:
                raise ContractError("accepted decision score cannot be below threshold")
        expected_id = self.build_id(
            opportunity_id=self.opportunity_id,
            policy_version=self.policy_version,
            config_hash=self.config_hash,
        )
        if self.decision_id != expected_id:
            raise ContractError("decision_id does not match its identity fields")

    @staticmethod
    def build_id(*, opportunity_id: str, policy_version: str, config_hash: str) -> str:
        return _stable_id(
            "lndec",
            {
                "config_hash": config_hash,
                "opportunity_id": opportunity_id,
                "policy_version": policy_version,
            },
        )

    @classmethod
    def create(
        cls,
        opportunity: Opportunity,
        *,
        decided_at_ms: int,
        action: DecisionAction | str,
        reason: str,
        score: float,
        threshold: float,
        policy_version: str,
        expert_id: str,
        execution_profile_id: str | None = None,
        exit_profile_id: str | None = None,
    ) -> "Decision":
        return cls(
            decision_id=cls.build_id(
                opportunity_id=opportunity.opportunity_id,
                policy_version=policy_version,
                config_hash=opportunity.config_hash,
            ),
            opportunity_id=opportunity.opportunity_id,
            session_id=opportunity.session_id,
            symbol=opportunity.symbol,
            side=opportunity.side,
            observed_at_ms=opportunity.observed_at_ms,
            decided_at_ms=decided_at_ms,
            action=DecisionAction(action),
            reason=reason,
            score=score,
            threshold=threshold,
            policy_version=policy_version,
            config_hash=opportunity.config_hash,
            feature_hash=opportunity.feature_hash,
            expert_id=expert_id,
            execution_profile_id=execution_profile_id,
            exit_profile_id=exit_profile_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return _canonicalize(self)

    def to_canonical_dict(self) -> dict[str, Any]:
        return self.to_dict()

    def validate_opportunity(self, opportunity: Opportunity) -> None:
        """Fail if this decision is not bound to the supplied opportunity."""

        expected = {
            "opportunity_id": opportunity.opportunity_id,
            "session_id": opportunity.session_id,
            "symbol": opportunity.symbol,
            "side": opportunity.side,
            "observed_at_ms": opportunity.observed_at_ms,
            "config_hash": opportunity.config_hash,
            "feature_hash": opportunity.feature_hash,
        }
        mismatches = [
            name
            for name, value in expected.items()
            if getattr(self, name) != value
        ]
        if mismatches:
            raise ContractError(
                "decision does not match opportunity: " + ", ".join(mismatches)
            )


@dataclass(frozen=True, slots=True)
class Outcome:
    """Terminal result recorded only after a Decision already exists."""

    outcome_id: str
    decision_id: str
    opportunity_id: str
    session_id: str
    symbol: str
    side: Side
    feature_hash: str
    config_hash: str
    decision_action: DecisionAction
    observed_at_ms: int
    decided_at_ms: int
    outcome_at_ms: int
    status: OutcomeStatus
    filled: bool
    entry_filled_at_ms: int | None
    closed_at_ms: int | None
    entry_price: float | None
    exit_price: float | None
    quantity: float | None
    exit_reason: str | None
    gross_pnl_usdc: float
    all_in_cost_usdc: float
    net_pnl_usdc: float
    schema_version: str = OUTCOME_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "outcome_id",
            "decision_id",
            "opportunity_id",
            "session_id",
            "symbol",
            "feature_hash",
            "config_hash",
            "schema_version",
        ):
            _required_text(getattr(self, name), name)
        if self.schema_version != OUTCOME_SCHEMA_VERSION:
            raise ContractError("unsupported outcome schema_version")
        object.__setattr__(self, "side", Side(self.side))
        object.__setattr__(self, "decision_action", DecisionAction(self.decision_action))
        object.__setattr__(self, "status", OutcomeStatus(self.status))
        _timestamp(self.observed_at_ms, "observed_at_ms")
        _timestamp(self.decided_at_ms, "decided_at_ms")
        _timestamp(self.outcome_at_ms, "outcome_at_ms")
        if self.decided_at_ms < self.observed_at_ms:
            raise ContractError("decision cannot precede opportunity observation")
        if self.outcome_at_ms < self.decided_at_ms:
            raise ContractError("outcome cannot precede decision")
        if not isinstance(self.filled, bool):
            raise ContractError("filled must be boolean")
        for name in ("entry_filled_at_ms", "closed_at_ms"):
            value = getattr(self, name)
            if value is not None:
                _timestamp(value, name)
                if value < self.decided_at_ms:
                    raise ContractError(f"{name} cannot precede decision")
                if value > self.outcome_at_ms:
                    raise ContractError(f"{name} cannot follow outcome")
        if self.closed_at_ms is not None and self.entry_filled_at_ms is not None:
            if self.closed_at_ms < self.entry_filled_at_ms:
                raise ContractError("close cannot precede fill")
        gross = _finite(self.gross_pnl_usdc, "gross_pnl_usdc")
        cost = _finite(self.all_in_cost_usdc, "all_in_cost_usdc")
        net = _finite(self.net_pnl_usdc, "net_pnl_usdc")
        if cost < 0:
            raise ContractError("all_in_cost_usdc must be non-negative")
        if abs(net - (gross - cost)) > 1e-9:
            raise ContractError("net_pnl_usdc must equal gross minus all-in cost")
        if self.filled:
            if self.entry_filled_at_ms is None:
                raise ContractError("filled outcome requires entry_filled_at_ms")
            if self.entry_price is None or _finite(self.entry_price, "entry_price") <= 0:
                raise ContractError("filled outcome requires positive entry_price")
            if self.quantity is None or _finite(self.quantity, "quantity") <= 0:
                raise ContractError("filled outcome requires positive quantity")
        else:
            if any(value is not None for value in (self.entry_filled_at_ms, self.closed_at_ms, self.entry_price, self.exit_price, self.quantity, self.exit_reason)):
                raise ContractError("unfilled outcome cannot contain fill or close fields")
            if gross != 0.0 or cost != 0.0 or net != 0.0:
                raise ContractError("unfilled outcome cannot contain PnL or cost")
        if self.status is OutcomeStatus.CLOSED:
            if not self.filled or self.closed_at_ms is None:
                raise ContractError("closed outcome requires a filled and closed trade")
            if self.exit_price is None or _finite(self.exit_price, "exit_price") <= 0:
                raise ContractError("closed outcome requires positive exit_price")
            _required_text(self.exit_reason, "exit_reason")
        elif (
            self.closed_at_ms is not None
            or self.exit_price is not None
            or self.exit_reason is not None
        ):
            raise ContractError("only CLOSED outcomes may contain close fields")
        expected_no_trade_status = {
            DecisionAction.SKIP: OutcomeStatus.SKIPPED,
            DecisionAction.BLOCK: OutcomeStatus.BLOCKED,
        }
        if self.decision_action is DecisionAction.ACCEPT:
            if self.status in {OutcomeStatus.SKIPPED, OutcomeStatus.BLOCKED}:
                raise ContractError("accepted decision cannot have a no-trade outcome")
        elif self.status is not expected_no_trade_status[self.decision_action]:
            raise ContractError("no-trade decision and outcome status do not match")
        if self.filled and self.status is not OutcomeStatus.CLOSED:
            raise ContractError("a terminal filled outcome must be CLOSED")
        expected_id = self.build_id(
            decision_id=self.decision_id,
            status=self.status,
            outcome_at_ms=self.outcome_at_ms,
        )
        if self.outcome_id != expected_id:
            raise ContractError("outcome_id does not match its identity fields")

    @staticmethod
    def build_id(
        *, decision_id: str, status: OutcomeStatus | str, outcome_at_ms: int
    ) -> str:
        return _stable_id(
            "lnout",
            {
                "decision_id": decision_id,
                "outcome_at_ms": outcome_at_ms,
                "status": OutcomeStatus(status).value,
            },
        )

    @classmethod
    def create(
        cls,
        decision: Decision,
        *,
        outcome_at_ms: int,
        status: OutcomeStatus | str,
        filled: bool = False,
        entry_filled_at_ms: int | None = None,
        closed_at_ms: int | None = None,
        entry_price: float | None = None,
        exit_price: float | None = None,
        quantity: float | None = None,
        exit_reason: str | None = None,
        gross_pnl_usdc: float = 0.0,
        all_in_cost_usdc: float = 0.0,
        net_pnl_usdc: float = 0.0,
    ) -> "Outcome":
        normalized_status = OutcomeStatus(status)
        return cls(
            outcome_id=cls.build_id(
                decision_id=decision.decision_id,
                status=normalized_status,
                outcome_at_ms=outcome_at_ms,
            ),
            decision_id=decision.decision_id,
            opportunity_id=decision.opportunity_id,
            session_id=decision.session_id,
            symbol=decision.symbol,
            side=decision.side,
            feature_hash=decision.feature_hash,
            config_hash=decision.config_hash,
            decision_action=decision.action,
            observed_at_ms=decision.observed_at_ms,
            decided_at_ms=decision.decided_at_ms,
            outcome_at_ms=outcome_at_ms,
            status=normalized_status,
            filled=filled,
            entry_filled_at_ms=entry_filled_at_ms,
            closed_at_ms=closed_at_ms,
            entry_price=entry_price,
            exit_price=exit_price,
            quantity=quantity,
            exit_reason=exit_reason,
            gross_pnl_usdc=gross_pnl_usdc,
            all_in_cost_usdc=all_in_cost_usdc,
            net_pnl_usdc=net_pnl_usdc,
        )

    @property
    def is_win(self) -> bool:
        return self.status is OutcomeStatus.CLOSED and self.net_pnl_usdc > 0.0

    def to_dict(self) -> dict[str, Any]:
        return _canonicalize(self)

    def to_canonical_dict(self) -> dict[str, Any]:
        return self.to_dict()

    @property
    def terminal_outcome(self) -> OutcomeStatus:
        return self.status

    def validate_decision(self, decision: Decision) -> None:
        """Fail if this outcome is not bound to the supplied decision."""

        expected = {
            "decision_id": decision.decision_id,
            "opportunity_id": decision.opportunity_id,
            "session_id": decision.session_id,
            "symbol": decision.symbol,
            "side": decision.side,
            "feature_hash": decision.feature_hash,
            "config_hash": decision.config_hash,
            "decision_action": decision.action,
            "observed_at_ms": decision.observed_at_ms,
            "decided_at_ms": decision.decided_at_ms,
        }
        mismatches = [
            name
            for name, value in expected.items()
            if getattr(self, name) != value
        ]
        if mismatches:
            raise ContractError(
                "outcome does not match decision: " + ", ".join(mismatches)
            )


__all__ = [
    "ContractError",
    "Decision",
    "DecisionAction",
    "Opportunity",
    "Outcome",
    "OutcomeStatus",
    "Side",
    "canonical_dict",
    "canonical_json",
    "canonical_sha256",
]
