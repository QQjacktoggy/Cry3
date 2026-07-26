"""Crash-safe v1.4.69 paid submission adapter.

The adapter never chooses an arm.  It accepts an already durable single-winner
claim and makes the exchange client order id the idempotency boundary across
timeouts, process death, and competing runners.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any, Awaitable, Callable, Mapping

from src.gridbot.storage.v1469_paid_execution_claim_repository import (
    DurablePaidExecutionClaim,
    V1469PaidClaimConflictError,
    V1469PaidExecutionClaimRepository,
)


def deterministic_client_order_id(claim_id: str) -> str:
    """Return a Binance-safe, stable id (<=36 ASCII characters)."""
    value = str(claim_id or "").strip()
    if not value:
        raise ValueError("claim_id must be non-empty")
    return "c69_" + hashlib.sha256(("v1469.cid.1|" + value).encode()).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    claim: DurablePaidExecutionClaim
    client_order_id: str
    exchange_order: Mapping[str, Any] | None
    submitted_now: bool


_AMBIGUOUS_BINANCE_CODES = frozenset({-1000, -1001, -1006, -1007})


def _http_status_from_exception(exc: BaseException) -> int | None:
    """Best-effort extraction for HTTP clients without coupling to one SDK."""
    for source in (exc, getattr(exc, "response", None)):
        for name in ("status_code", "status", "http_status"):
            value = getattr(source, name, None)
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def _is_ambiguous_submit_error(exc: BaseException) -> bool:
    """Whether the exchange may have accepted a submit despite this error."""
    try:
        exchange_code = int(getattr(exc, "code", None))
    except (TypeError, ValueError):
        exchange_code = None
    if exchange_code in _AMBIGUOUS_BINANCE_CODES:
        return True
    # urllib-style HTTP errors expose the response status as ``code``.
    if exchange_code is not None and 500 <= exchange_code <= 599:
        return True
    http_status = _http_status_from_exception(exc)
    return http_status is not None and 500 <= http_status <= 599


class V1469PaidExecutionAdapter:
    def __init__(self, repository: V1469PaidExecutionClaimRepository) -> None:
        self._repository = repository

    async def submit_or_reconcile(
        self,
        *,
        claim: DurablePaidExecutionClaim,
        now_ms: int,
        find_by_client_order_id: Callable[[str], Awaitable[Mapping[str, Any] | None]],
        submit: Callable[[str], Awaitable[Mapping[str, Any]]],
        actor: str,
        before_submit: Callable[
            [str, DurablePaidExecutionClaim], Awaitable[None]
        ] | None = None,
    ) -> SubmissionResult:
        cid = deterministic_client_order_id(claim.claim_id)
        durable = await self._repository.get_claim_by_id(claim.claim_id)
        if durable is None:
            raise RuntimeError("paid claim disappeared")
        if durable.status in {"TERMINAL", "ABANDONED"}:
            return SubmissionResult(durable, cid, None, False)

        # Reconcile before every submit. This closes both restart and the
        # exchange-acknowledged / local-persistence crash window.
        visible = await find_by_client_order_id(cid)
        if visible is not None:
            if durable.status != "SUBMITTED":
                durable = (await self._repository.transition_submission(
                    claim_id=durable.claim_id, expected_generation=durable.generation,
                    target_status="SUBMITTED", transition_at_ms=now_ms,
                    idempotency_key=f"submitted:{durable.claim_id}:{durable.generation}",
                    actor=actor, payload={"client_order_id": cid, "reconciled": True},
                )).claim
            return SubmissionResult(durable, cid, visible, False)

        owns_submission = False
        if durable.status == "CLAIMED":
            try:
                durable = (await self._repository.transition_submission(
                    claim_id=durable.claim_id, expected_generation=durable.generation,
                    target_status="SUBMITTING", transition_at_ms=now_ms,
                    idempotency_key=f"submitting:{durable.claim_id}", actor=actor,
                    payload={"client_order_id": cid},
                )).claim
                owns_submission = True
            except V1469PaidClaimConflictError:
                # Another process won the durable CAS.  Reload and reconcile,
                # but this invocation must never inherit its submit authority.
                durable = await self._repository.get_claim_by_id(claim.claim_id)
                if durable is None:
                    raise RuntimeError("paid claim disappeared after CAS conflict")
                visible = await find_by_client_order_id(cid)
                if visible is not None and durable.status not in {"TERMINAL", "ABANDONED"}:
                    try:
                        durable = (await self._repository.transition_submission(
                            claim_id=durable.claim_id,
                            expected_generation=durable.generation,
                            target_status="SUBMITTED", transition_at_ms=now_ms,
                            idempotency_key=f"submitted:{durable.claim_id}:{durable.generation}",
                            actor=actor,
                            payload={"client_order_id": cid, "reconciled": True},
                        )).claim
                    except V1469PaidClaimConflictError:
                        durable = await self._repository.get_claim_by_id(claim.claim_id) or durable
                return SubmissionResult(durable, cid, visible, False)
        elif durable.status in {"SUBMITTING", "UNKNOWN"}:
            # Absence is not proof of non-acceptance; remain fail-closed.
            return SubmissionResult(durable, cid, None, False)
        elif durable.status == "SUBMITTED":
            return SubmissionResult(durable, cid, None, False)

        if not owns_submission:
            return SubmissionResult(durable, cid, None, False)
        if before_submit is not None:
            try:
                await before_submit(cid, durable)
            except BaseException:
                try:
                    await self._repository.abandon_claim(
                        claim_id=durable.claim_id,
                        expected_generation=durable.generation,
                        abandoned_at_ms=now_ms,
                        terminal_reason="PRE_SUBMIT_BINDING_FAILED",
                        idempotency_key=(
                            f"abandoned:{durable.claim_id}:"
                            f"{durable.generation}:binding"
                        ),
                        actor=actor,
                        result_payload={
                            "client_order_id": cid,
                            "submitted": False,
                        },
                    )
                except BaseException:
                    pass
                raise
        try:
            order = await submit(cid)
        except BaseException as exc:
            # Binance's internal/timeout codes and HTTP 5xx do not prove that
            # the exchange rejected the order. Look up the deterministic CID
            # before deciding whether this invocation may return successfully.
            exchange_code = getattr(exc, "code", None)
            if _is_ambiguous_submit_error(exc):
                try:
                    visible = await find_by_client_order_id(cid)
                except BaseException:
                    visible = None
                if visible is not None:
                    durable = (await self._repository.transition_submission(
                        claim_id=durable.claim_id,
                        expected_generation=durable.generation,
                        target_status="SUBMITTED",
                        transition_at_ms=now_ms,
                        idempotency_key=(
                            f"submitted:{durable.claim_id}:{durable.generation}"
                        ),
                        actor=actor,
                        payload={
                            "client_order_id": cid,
                            "reconciled_after_ambiguous_submit_error": True,
                        },
                    )).claim
                    return SubmissionResult(durable, cid, visible, False)
            # An explicit non-ambiguous exchange error code proves this
            # request was rejected. Timeouts and transport failures remain
            # UNKNOWN.
            if exchange_code is not None and not _is_ambiguous_submit_error(exc):
                try:
                    await self._repository.abandon_claim(
                        claim_id=durable.claim_id,
                        expected_generation=durable.generation,
                        abandoned_at_ms=now_ms,
                        terminal_reason="EXCHANGE_REJECTED",
                        idempotency_key=(
                            f"abandoned:{durable.claim_id}:"
                            f"{durable.generation}"
                        ),
                        actor=actor,
                        result_payload={
                            "client_order_id": cid,
                            "exchange_code": exchange_code,
                        },
                    )
                except BaseException:
                    pass
                raise
            # Persist ambiguity even for cancellation; the caller may be dying.
            try:
                await self._repository.transition_submission(
                    claim_id=durable.claim_id, expected_generation=durable.generation,
                    target_status="UNKNOWN", transition_at_ms=now_ms,
                    idempotency_key=f"unknown:{durable.claim_id}:{durable.generation}",
                    actor=actor, payload={"client_order_id": cid},
                )
            except BaseException:
                # Persistence is best effort here and must not replace the
                # original timeout, exception, or cancellation.
                pass
            raise
        durable = (await self._repository.transition_submission(
            claim_id=durable.claim_id, expected_generation=durable.generation,
            target_status="SUBMITTED", transition_at_ms=now_ms,
            idempotency_key=f"submitted:{durable.claim_id}:{durable.generation}",
            actor=actor, payload={"client_order_id": cid},
        )).claim
        return SubmissionResult(durable, cid, order, True)


__all__ = ["SubmissionResult", "V1469PaidExecutionAdapter", "deterministic_client_order_id"]
