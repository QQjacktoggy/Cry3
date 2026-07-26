"""Bounded restart-only reconciliation for v1.4.69 paid claims.

This runtime has no submit callable by design.  It can only look up the
deterministic client order id for an already durable non-terminal claim and
CAS an exchange-visible SUBMITTING/UNKNOWN claim to SUBMITTED.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from src.gridbot.mainnet.v1469_paid_execution_adapter import (
    deterministic_client_order_id,
)
from src.gridbot.storage.v1469_paid_execution_claim_repository import (
    DurablePaidExecutionClaim,
    V1469PaidClaimConflictError,
    V1469PaidExecutionClaimRepository,
)


MAX_RECONCILE_CLAIMS = 100
MAX_RECONCILIATION_ERRORS = 100
_RECONCILABLE_STATUSES = frozenset(
    {"CLAIMED", "SUBMITTING", "UNKNOWN", "SUBMITTED"}
)

OrderLookup = Callable[
    [str, str],
    Awaitable[Mapping[str, Any] | None],
]


@dataclass(frozen=True, slots=True)
class PaidReconciliationError:
    code: str
    detail: str
    claim_id: str | None = None


@dataclass(frozen=True, slots=True)
class PaidClaimReconciliationTelemetry:
    claim_id: str
    client_order_id: str
    status_before: str
    status_after: str
    outcome: str


@dataclass(frozen=True, slots=True)
class PaidReconciliationResult:
    requested_limit: int
    enumerated_claims: int
    processed_claims: int
    lookup_calls: int
    visible_orders: int
    absent_orders: int
    transitioned_claims: int
    already_submitted_claims: int
    telemetry: tuple[PaidClaimReconciliationTelemetry, ...]
    errors: tuple[PaidReconciliationError, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def _bounded_int(value: object, name: str, *, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer from 1 to {maximum}")
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            f"{name} must be an integer from 1 to {maximum}"
        ) from exc
    if normalized < 1 or normalized > maximum:
        raise ValueError(f"{name} must be an integer from 1 to {maximum}")
    return normalized


def _non_negative_ms(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a non-negative integer") from exc
    if normalized < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return normalized


def _error_detail(exc: BaseException) -> str:
    detail = str(exc).strip() or type(exc).__name__
    return detail[:240]


class V1469PaidReconciler:
    """Reconcile a bounded durable claim page exactly once after restart."""

    def __init__(
        self,
        repository: V1469PaidExecutionClaimRepository,
    ) -> None:
        self._repository = repository

    async def reconcile_on_restart(
        self,
        *,
        environment: str,
        symbol: str | None = None,
        now_ms: int,
        limit: int,
        find_by_client_order_id: OrderLookup,
        actor: str = "v1469-paid-restart-reconciler",
    ) -> PaidReconciliationResult:
        page_limit = _bounded_int(
            limit,
            "limit",
            maximum=MAX_RECONCILE_CLAIMS,
        )
        transition_at_ms = _non_negative_ms(now_ms, "now_ms")
        normalized_environment = str(environment or "").strip().upper()
        if not normalized_environment:
            raise ValueError("environment must be non-empty")
        normalized_symbol = (
            str(symbol).strip().upper() if symbol is not None else None
        )
        if symbol is not None and not normalized_symbol:
            raise ValueError("symbol must be non-empty when supplied")
        normalized_actor = str(actor or "").strip()
        if not normalized_actor:
            raise ValueError("actor must be non-empty")
        if not callable(find_by_client_order_id):
            raise TypeError("find_by_client_order_id must be callable")

        errors: list[PaidReconciliationError] = []

        def add_error(
            code: str,
            detail: str,
            claim_id: str | None = None,
        ) -> None:
            if len(errors) >= MAX_RECONCILIATION_ERRORS:
                return
            errors.append(
                PaidReconciliationError(
                    code=code,
                    detail=str(detail)[:240],
                    claim_id=claim_id,
                )
            )

        try:
            raw_claims = await self._repository.list_reconcilable_claims(
                limit=page_limit,
                environment=normalized_environment,
                symbol=normalized_symbol,
            )
        except Exception as exc:
            add_error("REPOSITORY_LIST_ERROR", _error_detail(exc))
            return PaidReconciliationResult(
                requested_limit=page_limit,
                enumerated_claims=0,
                processed_claims=0,
                lookup_calls=0,
                visible_orders=0,
                absent_orders=0,
                transitioned_claims=0,
                already_submitted_claims=0,
                telemetry=(),
                errors=tuple(errors),
            )

        if not isinstance(raw_claims, tuple):
            add_error(
                "REPOSITORY_RESULT_INVALID",
                "list_reconcilable_claims must return a tuple",
            )
            raw_claims = tuple(raw_claims)
        enumerated = len(raw_claims)
        if enumerated > page_limit:
            add_error(
                "REPOSITORY_LIMIT_VIOLATION",
                f"returned={enumerated};limit={page_limit}",
            )
        page = raw_claims[:page_limit]

        by_claim_id: dict[str, DurablePaidExecutionClaim] = {}
        for claim in page:
            claim_id = str(getattr(claim, "claim_id", "") or "").strip()
            if claim_id in by_claim_id:
                add_error(
                    "DUPLICATE_CLAIM",
                    "repository returned the same claim more than once",
                    claim_id or None,
                )
            by_claim_id[claim_id] = claim
        claims = tuple(
            sorted(
                by_claim_id.values(),
                key=lambda item: (
                    int(getattr(item, "updated_at_ms", 0)),
                    str(getattr(item, "claim_id", "")),
                ),
            )
        )

        telemetry: list[PaidClaimReconciliationTelemetry] = []
        lookup_calls = 0
        visible_orders = 0
        absent_orders = 0
        transitioned_claims = 0
        already_submitted_claims = 0

        for claim in claims:
            claim_id = str(getattr(claim, "claim_id", "") or "").strip()
            status = str(getattr(claim, "status", "") or "").strip().upper()
            claim_environment = str(
                getattr(claim, "environment", "") or ""
            ).strip().upper()
            claim_symbol = str(
                getattr(claim, "symbol", "") or ""
            ).strip().upper()
            if claim_environment != normalized_environment:
                add_error(
                    "CLAIM_ENVIRONMENT_MISMATCH",
                    (
                        f"claim={claim_environment or 'EMPTY'};"
                        f"requested={normalized_environment}"
                    ),
                    claim_id or None,
                )
                telemetry.append(
                    PaidClaimReconciliationTelemetry(
                        claim_id=claim_id,
                        client_order_id="",
                        status_before=status,
                        status_after=status,
                        outcome="CLAIM_ENVIRONMENT_MISMATCH",
                    )
                )
                continue
            if (
                normalized_symbol is not None
                and claim_symbol != normalized_symbol
            ):
                add_error(
                    "CLAIM_SYMBOL_MISMATCH",
                    (
                        f"claim={claim_symbol or 'EMPTY'};"
                        f"requested={normalized_symbol}"
                    ),
                    claim_id or None,
                )
                telemetry.append(
                    PaidClaimReconciliationTelemetry(
                        claim_id=claim_id,
                        client_order_id="",
                        status_before=status,
                        status_after=status,
                        outcome="CLAIM_SYMBOL_MISMATCH",
                    )
                )
                continue
            if status not in _RECONCILABLE_STATUSES:
                add_error(
                    "INVALID_CLAIM_STATUS",
                    f"status={status or 'EMPTY'}",
                    claim_id or None,
                )
                telemetry.append(
                    PaidClaimReconciliationTelemetry(
                        claim_id=claim_id,
                        client_order_id="",
                        status_before=status,
                        status_after=status,
                        outcome="INVALID_CLAIM_STATUS",
                    )
                )
                continue
            try:
                client_order_id = deterministic_client_order_id(claim_id)
                if not claim_symbol:
                    raise ValueError("claim symbol must be non-empty")
            except (TypeError, ValueError) as exc:
                add_error(
                    "INVALID_CLAIM_IDENTITY",
                    _error_detail(exc),
                    claim_id or None,
                )
                telemetry.append(
                    PaidClaimReconciliationTelemetry(
                        claim_id=claim_id,
                        client_order_id="",
                        status_before=status,
                        status_after=status,
                        outcome="INVALID_CLAIM_IDENTITY",
                    )
                )
                continue

            lookup_calls += 1
            try:
                visible = await find_by_client_order_id(
                    claim_symbol,
                    client_order_id,
                )
            except Exception as exc:
                add_error(
                    "LOOKUP_ERROR",
                    _error_detail(exc),
                    claim_id,
                )
                telemetry.append(
                    PaidClaimReconciliationTelemetry(
                        claim_id=claim_id,
                        client_order_id=client_order_id,
                        status_before=status,
                        status_after=status,
                        outcome="LOOKUP_ERROR",
                    )
                )
                continue

            if visible is None:
                absent_orders += 1
                if status == "CLAIMED":
                    try:
                        mutation = await self._repository.abandon_claim(
                            claim_id=claim_id,
                            expected_generation=int(claim.generation),
                            abandoned_at_ms=transition_at_ms,
                            terminal_reason="RESTART_PRE_SUBMIT_NO_ORDER",
                            idempotency_key=(
                                f"abandoned:{claim_id}:"
                                f"{int(claim.generation)}:restart"
                            ),
                            actor=normalized_actor,
                            result_payload={
                                "submitted": False,
                                "client_order_id": client_order_id,
                            },
                        )
                    except Exception as exc:
                        add_error(
                            "CLAIMED_ABANDON_ERROR",
                            _error_detail(exc),
                            claim_id,
                        )
                        outcome = "CLAIMED_ABANDON_ERROR"
                        status_after = status
                    else:
                        transitioned_claims += 1
                        outcome = "ABSENT_CLAIMED_ABANDONED"
                        status_after = str(mutation.claim.status).upper()
                    telemetry.append(
                        PaidClaimReconciliationTelemetry(
                            claim_id=claim_id,
                            client_order_id=client_order_id,
                            status_before=status,
                            status_after=status_after,
                            outcome=outcome,
                        )
                    )
                    continue
                add_error(
                    "ORDER_ABSENT_UNRESOLVED",
                    (
                        f"status={status}; deterministic client order id "
                        "not visible"
                    ),
                    claim_id,
                )
                telemetry.append(
                    PaidClaimReconciliationTelemetry(
                        claim_id=claim_id,
                        client_order_id=client_order_id,
                        status_before=status,
                        status_after=status,
                        outcome="ABSENT_FAIL_CLOSED",
                    )
                )
                continue
            if not isinstance(visible, Mapping):
                add_error(
                    "VISIBLE_ORDER_INVALID",
                    "exchange lookup result must be an object",
                    claim_id,
                )
                telemetry.append(
                    PaidClaimReconciliationTelemetry(
                        claim_id=claim_id,
                        client_order_id=client_order_id,
                        status_before=status,
                        status_after=status,
                        outcome="VISIBLE_ORDER_INVALID",
                    )
                )
                continue
            visible_cid = str(
                visible.get("clientOrderId") or ""
            ).strip()
            visible_symbol = str(visible.get("symbol") or "").strip().upper()
            if (
                visible_cid != client_order_id
                or (visible_symbol and visible_symbol != claim_symbol)
            ):
                add_error(
                    "VISIBLE_ORDER_IDENTITY_MISMATCH",
                    "exchange order does not match claim symbol/CID",
                    claim_id,
                )
                telemetry.append(
                    PaidClaimReconciliationTelemetry(
                        claim_id=claim_id,
                        client_order_id=client_order_id,
                        status_before=status,
                        status_after=status,
                        outcome="VISIBLE_ORDER_IDENTITY_MISMATCH",
                    )
                )
                continue

            visible_orders += 1
            if status == "CLAIMED":
                add_error(
                    "VISIBLE_ORDER_FROM_CLAIMED",
                    "exchange order exists before durable submit transition",
                    claim_id,
                )
                telemetry.append(
                    PaidClaimReconciliationTelemetry(
                        claim_id=claim_id,
                        client_order_id=client_order_id,
                        status_before=status,
                        status_after=status,
                        outcome="VISIBLE_ORDER_FROM_CLAIMED",
                    )
                )
                continue
            if status == "SUBMITTED":
                already_submitted_claims += 1
                telemetry.append(
                    PaidClaimReconciliationTelemetry(
                        claim_id=claim_id,
                        client_order_id=client_order_id,
                        status_before=status,
                        status_after=status,
                        outcome="VISIBLE_ALREADY_SUBMITTED",
                    )
                )
                continue

            try:
                mutation = await self._repository.transition_submission(
                    claim_id=claim_id,
                    expected_generation=int(claim.generation),
                    target_status="SUBMITTED",
                    transition_at_ms=transition_at_ms,
                    idempotency_key=(
                        f"submitted:{claim_id}:{int(claim.generation)}"
                    ),
                    actor=normalized_actor,
                    payload={
                        "client_order_id": client_order_id,
                        "reconciled": True,
                    },
                )
            except V1469PaidClaimConflictError as exc:
                add_error(
                    "TRANSITION_CONFLICT",
                    _error_detail(exc),
                    claim_id,
                )
                telemetry.append(
                    PaidClaimReconciliationTelemetry(
                        claim_id=claim_id,
                        client_order_id=client_order_id,
                        status_before=status,
                        status_after=status,
                        outcome="TRANSITION_CONFLICT",
                    )
                )
                continue
            except Exception as exc:
                add_error(
                    "TRANSITION_ERROR",
                    _error_detail(exc),
                    claim_id,
                )
                telemetry.append(
                    PaidClaimReconciliationTelemetry(
                        claim_id=claim_id,
                        client_order_id=client_order_id,
                        status_before=status,
                        status_after=status,
                        outcome="TRANSITION_ERROR",
                    )
                )
                continue

            status_after = str(
                getattr(mutation.claim, "status", "") or ""
            ).strip().upper()
            if status_after != "SUBMITTED":
                add_error(
                    "TRANSITION_RESULT_INVALID",
                    f"status={status_after or 'EMPTY'}",
                    claim_id,
                )
                telemetry.append(
                    PaidClaimReconciliationTelemetry(
                        claim_id=claim_id,
                        client_order_id=client_order_id,
                        status_before=status,
                        status_after=status_after,
                        outcome="TRANSITION_RESULT_INVALID",
                    )
                )
                continue
            transitioned_claims += 1
            telemetry.append(
                PaidClaimReconciliationTelemetry(
                    claim_id=claim_id,
                    client_order_id=client_order_id,
                    status_before=status,
                    status_after=status_after,
                    outcome="RECONCILED_SUBMITTED",
                )
            )

        return PaidReconciliationResult(
            requested_limit=page_limit,
            enumerated_claims=enumerated,
            processed_claims=len(telemetry),
            lookup_calls=lookup_calls,
            visible_orders=visible_orders,
            absent_orders=absent_orders,
            transitioned_claims=transitioned_claims,
            already_submitted_claims=already_submitted_claims,
            telemetry=tuple(telemetry),
            errors=tuple(errors),
        )


__all__ = [
    "MAX_RECONCILE_CLAIMS",
    "MAX_RECONCILIATION_ERRORS",
    "OrderLookup",
    "PaidClaimReconciliationTelemetry",
    "PaidReconciliationError",
    "PaidReconciliationResult",
    "V1469PaidReconciler",
]
