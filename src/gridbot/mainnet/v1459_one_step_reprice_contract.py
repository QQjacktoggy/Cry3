"""Pure, fail-closed ONE_STEP_REPRICE finite-state contract for v1.4.59.

The contract consumes already-observed facts and returns immutable shadow
evidence. It performs no I/O and grants no live order-mutation authority.
A future executor must atomically compare-and-swap the predecessor hash before
using a deterministic claim key.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum
import hashlib
import json
from typing import ClassVar, Final


CONTRACT_VERSION: Final[str] = "v1459-one-step-reprice-v2"
MAX_REPRICES: Final[int] = 1
LIVE_AUTHORITY_DEFAULT: Final[bool] = False
MAX_DECIMAL_PRECISION: Final[int] = 38
MAX_DECIMAL_SCALE: Final[int] = 18
MAX_DECIMAL_INTEGER_DIGITS: Final[int] = 24
CLIENT_ORDER_KEY_PREFIX: Final[str] = "v1459r_"


class RepriceContractError(ValueError):
    """Raised when an object cannot satisfy the static contract."""


class RepriceState(str, Enum):
    INCUMBENT_OPEN = "INCUMBENT_OPEN"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCEL_CONFIRMED = "CANCEL_CONFIRMED"
    REPLACE_ALLOWED = "REPLACE_ALLOWED"
    EXPIRE = "EXPIRE"


class RepriceEventType(str, Enum):
    REQUEST_CANCEL = "REQUEST_CANCEL"
    CONFIRM_CANCEL = "CONFIRM_CANCEL"
    REQUEST_REPLACE = "REQUEST_REPLACE"
    EXPIRE = "EXPIRE"


class RepriceReason(str, Enum):
    INCUMBENT_OPENED = "INCUMBENT_OPENED"
    CANCEL_REQUEST_ACCEPTED = "CANCEL_REQUEST_ACCEPTED"
    CANCEL_CONFIRMATION_ACCEPTED = "CANCEL_CONFIRMATION_ACCEPTED"
    REPLACE_INTENT_ALLOWED = "REPLACE_INTENT_ALLOWED"
    EXECUTOR_NOT_READY = "EXECUTOR_NOT_READY"
    CLAIM_NOT_AVAILABLE = "CLAIM_NOT_AVAILABLE"
    EXPIRE_REQUESTED = "EXPIRE_REQUESTED"
    TTL_EXPIRED = "TTL_EXPIRED"
    OBSERVATION_TIME_REGRESSION = "OBSERVATION_TIME_REGRESSION"
    CONTRACT_BINDING_MISMATCH = "CONTRACT_BINDING_MISMATCH"
    PREDECESSOR_MISMATCH = "PREDECESSOR_MISMATCH"
    OWNERSHIP_FAILED = "OWNERSHIP_FAILED"
    SIGNAL_FAILED = "SIGNAL_FAILED"
    SPREAD_FAILED = "SPREAD_FAILED"
    EVENT_OUT_OF_ORDER = "EVENT_OUT_OF_ORDER"
    EVENT_PAYLOAD_INVALID = "EVENT_PAYLOAD_INVALID"
    CANCEL_NOT_CONFIRMED = "CANCEL_NOT_CONFIRMED"
    CANCEL_ORDER_MISMATCH = "CANCEL_ORDER_MISMATCH"
    REPRICE_ALREADY_USED = "REPRICE_ALREADY_USED"
    REPLACE_REQUEST_MISSING = "REPLACE_REQUEST_MISSING"
    TAKER_FORBIDDEN = "TAKER_FORBIDDEN"
    NO_CLOSER_FROZEN_STEP = "NO_CLOSER_FROZEN_STEP"
    NOT_ONE_FROZEN_STEP = "NOT_ONE_FROZEN_STEP"
    INVALID_TOP_OF_BOOK = "INVALID_TOP_OF_BOOK"
    PRICE_WOULD_CROSS = "PRICE_WOULD_CROSS"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class FrozenMenuStep(str, Enum):
    E2 = "E2"
    E1 = "E1"
    E0 = "E0"


FROZEN_MENU_STEPS: Final[tuple[FrozenMenuStep, ...]] = (
    FrozenMenuStep.E2,
    FrozenMenuStep.E1,
    FrozenMenuStep.E0,
)


def _decimal_text(value: Decimal) -> str:
    """Exact canonical form that never consults the ambient Decimal context."""

    if not isinstance(value, Decimal) or not value.is_finite():
        raise RepriceContractError("canonical decimal must be finite Decimal")
    sign, raw_digits, raw_exponent = value.as_tuple()
    if not isinstance(raw_exponent, int):
        raise RepriceContractError("canonical decimal exponent must be integer")
    digits = list(raw_digits)
    if not digits or all(digit == 0 for digit in digits):
        return "0e+0"
    while len(digits) > 1 and digits[0] == 0:
        digits.pop(0)
    exponent = raw_exponent
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    precision = len(digits)
    scale = max(-exponent, 0)
    integer_digits = max(precision + exponent, 0)
    if precision > MAX_DECIMAL_PRECISION:
        raise RepriceContractError(
            f"decimal precision exceeds {MAX_DECIMAL_PRECISION} digits"
        )
    if scale > MAX_DECIMAL_SCALE:
        raise RepriceContractError(
            f"decimal scale exceeds {MAX_DECIMAL_SCALE} places"
        )
    if integer_digits > MAX_DECIMAL_INTEGER_DIGITS:
        raise RepriceContractError(
            f"decimal integer width exceeds {MAX_DECIMAL_INTEGER_DIGITS} digits"
        )
    coefficient = "".join(str(digit) for digit in digits)
    return f"{'-' if sign else ''}{coefficient}e{exponent:+d}"


def _stable_hash(payload: object) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()



def _policy_payload() -> dict[str, object]:
    """Return the identity-free rules committed to by the policy hash."""

    return {
        "contract_version": CONTRACT_VERSION,
        "execution_mode": "SHADOW_ONLY",
        "executor_ready": False,
        "io_authority": False,
        "live_authority": LIVE_AUTHORITY_DEFAULT,
        "order_mutation_authority": False,
        "max_reprices": MAX_REPRICES,
        "ordered_path": [
            RepriceState.INCUMBENT_OPEN.value,
            RepriceState.CANCEL_REQUESTED.value,
            RepriceState.CANCEL_CONFIRMED.value,
            RepriceState.REPLACE_ALLOWED.value,
        ],
        "required_events": [
            RepriceEventType.REQUEST_CANCEL.value,
            RepriceEventType.CONFIRM_CANCEL.value,
            RepriceEventType.REQUEST_REPLACE.value,
        ],
        "safety_facts": {
            "ownership_valid": True,
            "signal_valid": True,
            "spread_valid": True,
            "recheck_each_event": True,
        },
        "ttl": {
            "deadline_is_immutable": True,
            "expires_when_observed_at_gte_deadline": True,
        },
        "replacement": {
            "frozen_menu": [step.value for step in FROZEN_MENU_STEPS],
            "move_exactly_one_adjacent_step": True,
            "post_only": True,
            "allow_taker": False,
            "must_not_cross_opposite_quote": True,
        },
        "decimal_canonicalization": {
            "format": "signed-coefficient-e-exponent",
            "max_precision": MAX_DECIMAL_PRECISION,
            "max_scale": MAX_DECIMAL_SCALE,
            "max_integer_digits": MAX_DECIMAL_INTEGER_DIGITS,
            "ambient_context_independent": True,
        },
    }


# This digest identifies policy semantics only. Opportunity, order, price, and
# observation identity belong to each snapshot's stable_hash instead.
ONE_STEP_REPRICE_POLICY_HASH: Final[str] = _stable_hash(_policy_payload())

def _require_non_empty(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise RepriceContractError(f"{name} must be a non-empty string")


def _require_timestamp(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RepriceContractError(f"{name} must be a non-negative integer")


def _require_hash(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RepriceContractError(f"{name} must be a lowercase SHA-256 digest")


def _require_positive_decimal(value: object, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise RepriceContractError(f"{name} must be positive finite Decimal")
    _decimal_text(value)


@dataclass(frozen=True, slots=True)
class RepriceScope:
    environment: str
    account_fingerprint: str
    symbol: str
    session_id: str
    strategy_id: str

    def __post_init__(self) -> None:
        for name in (
            "environment",
            "account_fingerprint",
            "symbol",
            "session_id",
            "strategy_id",
        ):
            _require_non_empty(getattr(self, name), name)

    def canonical_payload(self) -> dict[str, str]:
        return {
            "environment": self.environment,
            "account_fingerprint": self.account_fingerprint,
            "symbol": self.symbol,
            "session_id": self.session_id,
            "strategy_id": self.strategy_id,
        }


@dataclass(frozen=True, slots=True)
class FrozenPriceMenu:
    side: OrderSide
    e2: Decimal
    e1: Decimal
    e0: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.side, OrderSide):
            raise RepriceContractError("side must be OrderSide")
        for name in ("e2", "e1", "e0"):
            _require_positive_decimal(getattr(self, name), name)
        prices = (self.e2, self.e1, self.e0)
        closer = (
            prices[0] < prices[1] < prices[2]
            if self.side is OrderSide.BUY
            else prices[0] > prices[1] > prices[2]
        )
        if not closer:
            raise RepriceContractError(
                "E2/E1/E0 prices must move strictly closer for the order side"
            )

    def price(self, step: FrozenMenuStep) -> Decimal:
        if not isinstance(step, FrozenMenuStep):
            raise RepriceContractError("step must be FrozenMenuStep")
        return {
            FrozenMenuStep.E2: self.e2,
            FrozenMenuStep.E1: self.e1,
            FrozenMenuStep.E0: self.e0,
        }[step]

    def next_step(self, step: FrozenMenuStep) -> FrozenMenuStep | None:
        if not isinstance(step, FrozenMenuStep):
            raise RepriceContractError("step must be FrozenMenuStep")
        index = FROZEN_MENU_STEPS.index(step)
        if index + 1 == len(FROZEN_MENU_STEPS):
            return None
        return FROZEN_MENU_STEPS[index + 1]

    def canonical_payload(self) -> dict[str, object]:
        return {
            "side": self.side.value,
            "prices": {
                "E2": _decimal_text(self.e2),
                "E1": _decimal_text(self.e1),
                "E0": _decimal_text(self.e0),
            },
        }


@dataclass(frozen=True, slots=True)
class SafetyFacts:
    ownership_valid: bool | None
    signal_valid: bool | None
    spread_valid: bool | None

    def __post_init__(self) -> None:
        for name in ("ownership_valid", "signal_valid", "spread_valid"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, bool):
                raise RepriceContractError(f"{name} must be bool or None")

    def failure_reason(self) -> RepriceReason | None:
        if self.ownership_valid is not True:
            return RepriceReason.OWNERSHIP_FAILED
        if self.signal_valid is not True:
            return RepriceReason.SIGNAL_FAILED
        if self.spread_valid is not True:
            return RepriceReason.SPREAD_FAILED
        return None

    def canonical_payload(self) -> dict[str, bool | None]:
        return {
            "ownership_valid": self.ownership_valid,
            "signal_valid": self.signal_valid,
            "spread_valid": self.spread_valid,
        }


@dataclass(frozen=True, slots=True)
class ReplaceRequest:
    requested_step: FrozenMenuStep
    best_bid: Decimal
    best_ask: Decimal
    post_only: bool = True
    allow_taker: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.requested_step, FrozenMenuStep):
            raise RepriceContractError("requested_step must be FrozenMenuStep")
        _require_positive_decimal(self.best_bid, "best_bid")
        _require_positive_decimal(self.best_ask, "best_ask")
        if not isinstance(self.post_only, bool) or not isinstance(self.allow_taker, bool):
            raise RepriceContractError("post_only and allow_taker must be boolean")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "requested_step": self.requested_step.value,
            "best_bid": _decimal_text(self.best_bid),
            "best_ask": _decimal_text(self.best_ask),
            "post_only": self.post_only,
            "allow_taker": self.allow_taker,
        }


@dataclass(frozen=True, slots=True)
class ReplacePriceIntent:
    side: OrderSide
    from_step: FrozenMenuStep
    to_step: FrozenMenuStep
    limit_price: Decimal
    observed_best_bid: Decimal
    observed_best_ask: Decimal
    ttl_deadline_ms: int
    post_only: bool = True
    allow_taker: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.side, OrderSide):
            raise RepriceContractError("intent side must be OrderSide")
        if not isinstance(self.from_step, FrozenMenuStep) or not isinstance(
            self.to_step, FrozenMenuStep
        ):
            raise RepriceContractError("intent steps must be FrozenMenuStep")
        for name in ("limit_price", "observed_best_bid", "observed_best_ask"):
            _require_positive_decimal(getattr(self, name), name)
        if self.observed_best_bid >= self.observed_best_ask:
            raise RepriceContractError("intent top of book must have bid below ask")
        _require_timestamp(self.ttl_deadline_ms, "ttl_deadline_ms")
        if self.post_only is not True or self.allow_taker is not False:
            raise RepriceContractError("replacement intent must be post-only maker")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "side": self.side.value,
            "from_step": self.from_step.value,
            "to_step": self.to_step.value,
            "limit_price": _decimal_text(self.limit_price),
            "observed_best_bid": _decimal_text(self.observed_best_bid),
            "observed_best_ask": _decimal_text(self.observed_best_ask),
            "ttl_deadline_ms": self.ttl_deadline_ms,
            "post_only": self.post_only,
            "allow_taker": self.allow_taker,
        }


@dataclass(frozen=True, slots=True)
class RepriceEvent:
    event_type: RepriceEventType
    observed_at_ms: int
    safety: SafetyFacts
    cancel_confirmed: bool | None = None
    confirmed_order_id: str | None = None
    replacement: ReplaceRequest | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, RepriceEventType):
            raise RepriceContractError("event_type must be RepriceEventType")
        _require_timestamp(self.observed_at_ms, "observed_at_ms")
        if not isinstance(self.safety, SafetyFacts):
            raise RepriceContractError("safety must be SafetyFacts")
        if self.cancel_confirmed is not None and not isinstance(
            self.cancel_confirmed, bool
        ):
            raise RepriceContractError("cancel_confirmed must be bool or None")
        if self.confirmed_order_id is not None and not isinstance(
            self.confirmed_order_id, str
        ):
            raise RepriceContractError("confirmed_order_id must be str or None")
        if self.replacement is not None and not isinstance(
            self.replacement, ReplaceRequest
        ):
            raise RepriceContractError("replacement must be ReplaceRequest or None")

    @classmethod
    def request_cancel(
        cls, *, observed_at_ms: int, safety: SafetyFacts
    ) -> "RepriceEvent":
        return cls(
            event_type=RepriceEventType.REQUEST_CANCEL,
            observed_at_ms=observed_at_ms,
            safety=safety,
        )

    @classmethod
    def confirm_cancel(
        cls,
        *,
        observed_at_ms: int,
        safety: SafetyFacts,
        cancel_confirmed: bool,
        confirmed_order_id: str | None,
    ) -> "RepriceEvent":
        return cls(
            event_type=RepriceEventType.CONFIRM_CANCEL,
            observed_at_ms=observed_at_ms,
            safety=safety,
            cancel_confirmed=cancel_confirmed,
            confirmed_order_id=confirmed_order_id,
        )

    @classmethod
    def request_replace(
        cls,
        *,
        observed_at_ms: int,
        safety: SafetyFacts,
        replacement: ReplaceRequest,
    ) -> "RepriceEvent":
        return cls(
            event_type=RepriceEventType.REQUEST_REPLACE,
            observed_at_ms=observed_at_ms,
            safety=safety,
            replacement=replacement,
        )

    @classmethod
    def expire(
        cls, *, observed_at_ms: int, safety: SafetyFacts
    ) -> "RepriceEvent":
        return cls(
            event_type=RepriceEventType.EXPIRE,
            observed_at_ms=observed_at_ms,
            safety=safety,
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "event_type": self.event_type.value,
            "observed_at_ms": self.observed_at_ms,
            "safety": self.safety.canonical_payload(),
            "cancel_confirmed": self.cancel_confirmed,
            "confirmed_order_id": self.confirmed_order_id,
            "replacement": (
                None
                if self.replacement is None
                else self.replacement.canonical_payload()
            ),
        }


@dataclass(frozen=True, slots=True)
class OneStepRepriceContract:
    """Immutable evidence snapshot; never an executable order instruction."""

    opportunity_id: str
    incumbent_order_id: str
    opened_at_ms: int
    ttl_deadline_ms: int
    frozen_menu: FrozenPriceMenu
    incumbent_step: FrozenMenuStep
    state: RepriceState
    reason: RepriceReason
    observed_at_ms: int
    cancel_confirmed: bool = False
    reprice_count: int = 0
    price_intent: ReplacePriceIntent | None = None
    scope: RepriceScope | None = None

    policy_hash: ClassVar[str] = ONE_STEP_REPRICE_POLICY_HASH
    live_authority: ClassVar[bool] = LIVE_AUTHORITY_DEFAULT
    permits_order_mutation: ClassVar[bool] = False
    executor_ready: ClassVar[bool] = False

    def __post_init__(self) -> None:
        _require_non_empty(self.opportunity_id, "opportunity_id")
        _require_non_empty(self.incumbent_order_id, "incumbent_order_id")
        _require_timestamp(self.opened_at_ms, "opened_at_ms")
        _require_timestamp(self.ttl_deadline_ms, "ttl_deadline_ms")
        _require_timestamp(self.observed_at_ms, "observed_at_ms")
        if self.ttl_deadline_ms <= self.opened_at_ms:
            raise RepriceContractError("ttl_deadline_ms must be after opened_at_ms")
        if self.observed_at_ms < self.opened_at_ms:
            raise RepriceContractError("observed_at_ms cannot precede opened_at_ms")
        if not isinstance(self.frozen_menu, FrozenPriceMenu):
            raise RepriceContractError("frozen_menu must be FrozenPriceMenu")
        if not isinstance(self.incumbent_step, FrozenMenuStep):
            raise RepriceContractError("incumbent_step must be FrozenMenuStep")
        if not isinstance(self.state, RepriceState):
            raise RepriceContractError("state must be RepriceState")
        if not isinstance(self.reason, RepriceReason):
            raise RepriceContractError("reason must be RepriceReason")
        if not isinstance(self.cancel_confirmed, bool):
            raise RepriceContractError("cancel_confirmed must be bool")
        if isinstance(self.reprice_count, bool) or self.reprice_count not in (0, 1):
            raise RepriceContractError("reprice_count must be zero or one")
        if self.price_intent is not None and not isinstance(
            self.price_intent, ReplacePriceIntent
        ):
            raise RepriceContractError(
                "price_intent must be ReplacePriceIntent or None"
            )
        if self.scope is not None and not isinstance(self.scope, RepriceScope):
            raise RepriceContractError("scope must be RepriceScope or None")
        if self.state is RepriceState.REPLACE_ALLOWED:
            if self.reprice_count != MAX_REPRICES or self.price_intent is None:
                raise RepriceContractError(
                    "REPLACE_ALLOWED requires exactly one shadow price intent"
                )

    @classmethod
    def open(
        cls,
        *,
        opportunity_id: str,
        incumbent_order_id: str,
        opened_at_ms: int,
        ttl_deadline_ms: int,
        frozen_menu: FrozenPriceMenu,
        incumbent_step: FrozenMenuStep = FrozenMenuStep.E2,
        scope: RepriceScope | None = None,
    ) -> "OneStepRepriceContract":
        return cls(
            opportunity_id=opportunity_id,
            incumbent_order_id=incumbent_order_id,
            opened_at_ms=opened_at_ms,
            ttl_deadline_ms=ttl_deadline_ms,
            frozen_menu=frozen_menu,
            incumbent_step=incumbent_step,
            state=RepriceState.INCUMBENT_OPEN,
            reason=RepriceReason.INCUMBENT_OPENED,
            observed_at_ms=opened_at_ms,
            scope=scope,
        )

    @property
    def reason_code(self) -> str:
        return self.reason.value

    @property
    def replace_allowed(self) -> bool:
        return self.state is RepriceState.REPLACE_ALLOWED

    @property
    def stable_hash(self) -> str:
        return _stable_hash(self.canonical_payload())

    def canonical_payload(self) -> dict[str, object]:
        return {
            "contract_version": CONTRACT_VERSION,
            "policy_hash": self.policy_hash,
            "scope": None if self.scope is None else self.scope.canonical_payload(),
            "opportunity_id": self.opportunity_id,
            "incumbent_order_id": self.incumbent_order_id,
            "opened_at_ms": self.opened_at_ms,
            "ttl_deadline_ms": self.ttl_deadline_ms,
            "frozen_menu": self.frozen_menu.canonical_payload(),
            "incumbent_step": self.incumbent_step.value,
            "state": self.state.value,
            "reason": self.reason.value,
            "observed_at_ms": self.observed_at_ms,
            "cancel_confirmed": self.cancel_confirmed,
            "reprice_count": self.reprice_count,
            "price_intent": (
                None
                if self.price_intent is None
                else self.price_intent.canonical_payload()
            ),
            "live_authority": self.live_authority,
            "permits_order_mutation": self.permits_order_mutation,
            "executor_ready": self.executor_ready,
        }

    def transition(self, event: RepriceEvent) -> "OneStepRepriceContract":
        return transition_one_step_reprice(self, event)

    def _expire(
        self, reason: RepriceReason, *, observed_at_ms: int | None = None
    ) -> "OneStepRepriceContract":
        if self.state is RepriceState.EXPIRE:
            return self
        next_observed_at = self.observed_at_ms
        if observed_at_ms is not None and observed_at_ms >= self.observed_at_ms:
            next_observed_at = observed_at_ms
        return replace(
            self,
            state=RepriceState.EXPIRE,
            reason=reason,
            observed_at_ms=next_observed_at,
        )


def _replacement_intent(
    snapshot: OneStepRepriceContract, request: ReplaceRequest
) -> ReplacePriceIntent | RepriceReason:
    if request.post_only is not True or request.allow_taker is not False:
        return RepriceReason.TAKER_FORBIDDEN
    if request.best_bid >= request.best_ask:
        return RepriceReason.INVALID_TOP_OF_BOOK

    next_step = snapshot.frozen_menu.next_step(snapshot.incumbent_step)
    if next_step is None:
        return RepriceReason.NO_CLOSER_FROZEN_STEP
    if request.requested_step is not next_step:
        return RepriceReason.NOT_ONE_FROZEN_STEP

    limit_price = snapshot.frozen_menu.price(next_step)
    if snapshot.frozen_menu.side is OrderSide.BUY:
        if limit_price >= request.best_ask:
            return RepriceReason.PRICE_WOULD_CROSS
    elif limit_price <= request.best_bid:
        return RepriceReason.PRICE_WOULD_CROSS

    return ReplacePriceIntent(
        side=snapshot.frozen_menu.side,
        from_step=snapshot.incumbent_step,
        to_step=next_step,
        limit_price=limit_price,
        observed_best_bid=request.best_bid,
        observed_best_ask=request.best_ask,
        ttl_deadline_ms=snapshot.ttl_deadline_ms,
        post_only=True,
        allow_taker=False,
    )


def transition_one_step_reprice(
    snapshot: OneStepRepriceContract, event: RepriceEvent
) -> OneStepRepriceContract:
    """Apply one fact-only transition and fail closed on every ambiguity."""

    if not isinstance(snapshot, OneStepRepriceContract):
        raise RepriceContractError("snapshot must be OneStepRepriceContract")
    if not isinstance(event, RepriceEvent):
        raise RepriceContractError("event must be RepriceEvent")
    if snapshot.state is RepriceState.EXPIRE:
        return snapshot
    if event.observed_at_ms < snapshot.observed_at_ms:
        return snapshot._expire(RepriceReason.OBSERVATION_TIME_REGRESSION)
    if event.observed_at_ms >= snapshot.ttl_deadline_ms:
        return snapshot._expire(
            RepriceReason.TTL_EXPIRED, observed_at_ms=event.observed_at_ms
        )
    if snapshot.state is RepriceState.REPLACE_ALLOWED:
        return snapshot._expire(
            RepriceReason.REPRICE_ALREADY_USED,
            observed_at_ms=event.observed_at_ms,
        )
    if event.event_type is RepriceEventType.EXPIRE:
        return snapshot._expire(
            RepriceReason.EXPIRE_REQUESTED, observed_at_ms=event.observed_at_ms
        )

    safety_failure = event.safety.failure_reason()
    if safety_failure is not None:
        return snapshot._expire(
            safety_failure, observed_at_ms=event.observed_at_ms
        )

    if snapshot.state is RepriceState.INCUMBENT_OPEN:
        if event.event_type is not RepriceEventType.REQUEST_CANCEL:
            return snapshot._expire(
                RepriceReason.EVENT_OUT_OF_ORDER,
                observed_at_ms=event.observed_at_ms,
            )
        return replace(
            snapshot,
            state=RepriceState.CANCEL_REQUESTED,
            reason=RepriceReason.CANCEL_REQUEST_ACCEPTED,
            observed_at_ms=event.observed_at_ms,
        )

    if snapshot.state is RepriceState.CANCEL_REQUESTED:
        if event.event_type is RepriceEventType.REQUEST_REPLACE:
            return snapshot._expire(
                RepriceReason.CANCEL_NOT_CONFIRMED,
                observed_at_ms=event.observed_at_ms,
            )
        if event.event_type is not RepriceEventType.CONFIRM_CANCEL:
            return snapshot._expire(
                RepriceReason.EVENT_OUT_OF_ORDER,
                observed_at_ms=event.observed_at_ms,
            )
        if event.cancel_confirmed is not True:
            return snapshot._expire(
                RepriceReason.CANCEL_NOT_CONFIRMED,
                observed_at_ms=event.observed_at_ms,
            )
        if event.confirmed_order_id != snapshot.incumbent_order_id:
            return snapshot._expire(
                RepriceReason.CANCEL_ORDER_MISMATCH,
                observed_at_ms=event.observed_at_ms,
            )
        return replace(
            snapshot,
            state=RepriceState.CANCEL_CONFIRMED,
            reason=RepriceReason.CANCEL_CONFIRMATION_ACCEPTED,
            observed_at_ms=event.observed_at_ms,
            cancel_confirmed=True,
        )

    if snapshot.state is not RepriceState.CANCEL_CONFIRMED:
        return snapshot._expire(
            RepriceReason.EVENT_OUT_OF_ORDER,
            observed_at_ms=event.observed_at_ms,
        )
    if event.event_type is not RepriceEventType.REQUEST_REPLACE:
        return snapshot._expire(
            RepriceReason.EVENT_OUT_OF_ORDER,
            observed_at_ms=event.observed_at_ms,
        )
    if snapshot.reprice_count >= MAX_REPRICES:
        return snapshot._expire(
            RepriceReason.REPRICE_ALREADY_USED,
            observed_at_ms=event.observed_at_ms,
        )
    if event.replacement is None:
        return snapshot._expire(
            RepriceReason.REPLACE_REQUEST_MISSING,
            observed_at_ms=event.observed_at_ms,
        )

    intent_or_reason = _replacement_intent(snapshot, event.replacement)
    if isinstance(intent_or_reason, RepriceReason):
        return snapshot._expire(
            intent_or_reason, observed_at_ms=event.observed_at_ms
        )
    return replace(
        snapshot,
        state=RepriceState.REPLACE_ALLOWED,
        reason=RepriceReason.REPLACE_INTENT_ALLOWED,
        observed_at_ms=event.observed_at_ms,
        reprice_count=MAX_REPRICES,
        price_intent=intent_or_reason,
    )

