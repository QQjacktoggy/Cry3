"""Pure, fail-closed KPI aggregation for the Codex v1.4.59 canary.

This module deliberately has no database, exchange, deployment, service, or
order capability.  It accepts already-scoped evidence, validates identity and
provenance, and produces the immutable :mod:`v1459_canary_contract` snapshot.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
import json
from typing import Any, Literal

from src.gridbot.mainnet.v1459_canary_contract import (
    CanaryContractError,
    CanaryKpiSnapshot,
    V1459_CANARY_TARGET,
)


class CanaryRuntimeError(CanaryContractError):
    """Raised when evidence cannot support an exact canary snapshot."""


_MISSING = object()
_COMPLETE = "COMPLETE"
_DATA_INCOMPLETE = "DATA_INCOMPLETE"
_FILL_COUNT_FIELDS = (
    "entry_maker_fills",
    "entry_taker_fills",
    "exit_maker_fills",
    "exit_taker_fills",
)
_MONEY_DECIMAL_PLACES = 12
_MONEY_MAX_INTEGER_DIGITS = 18
_MONEY_SCALE = 10**_MONEY_DECIMAL_PLACES
_MONEY_MAX_UNITS = 10 ** (
    _MONEY_MAX_INTEGER_DIGITS + _MONEY_DECIMAL_PLACES
) - 1


@dataclass(frozen=True, slots=True)
class _SessionScope:
    session_id: str
    environment: str
    account_fingerprint: str
    symbol: str


@dataclass(frozen=True, slots=True)
class _Membership:
    session_id: str
    opportunity_id: str
    run_id: str
    reconciliation_revision: int
    symbol: str


@dataclass(frozen=True, slots=True)
class _RunEvidence:
    session_id: str
    opportunity_id: str
    run_id: str
    reconciliation_revision: int
    environment: str
    account_fingerprint: str
    symbol: str
    status: Literal["COMPLETE", "DATA_INCOMPLETE"]
    gross_pnl_units: int | None = None
    commission_units: int | None = None
    funding_units: int | None = None
    net_pnl_units: int | None = None
    entry_fills: int = 0
    exit_fills: int = 0
    eligible_for_wr_ev: bool | None = None
    exchange_trade_ids: tuple[str, ...] = ()
    exchange_income_ids: tuple[str, ...] = ()

    @property
    def is_paid_closed_fill(self) -> bool:
        return (
            self.status == _COMPLETE
            and self.entry_fills > 0
            and self.exit_fills > 0
            and self.eligible_for_wr_ev is True
        )

    @property
    def is_incomplete(self) -> bool:
        if self.status == _DATA_INCOMPLETE:
            return True
        no_fill = self.entry_fills == 0 and self.exit_fills == 0
        if no_fill:
            return False
        return not self.is_paid_closed_fill


def _required_text(record: Mapping[str, Any], key: str, *, label: str | None = None) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise CanaryRuntimeError(f"{label or key} must be non-empty text")
    return value.strip()


def _non_negative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CanaryRuntimeError(f"{label} must be a non-negative integer")
    return value


def _required_non_negative_int(
    record: Mapping[str, Any], key: str, *, label: str | None = None
) -> int:
    if key not in record:
        raise CanaryRuntimeError(f"{label or key} is required")
    return _non_negative_int(record[key], label or key)


def _optional_non_negative_int(
    record: Mapping[str, Any], key: str, *, label: str | None = None
) -> int | None:
    value = record.get(key)
    if value is None:
        return None
    return _non_negative_int(value, label or key)


def _json_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        raise CanaryRuntimeError(f"{label} must encode a JSON object")
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError) as exc:
        raise CanaryRuntimeError(f"{label} must encode a JSON object") from exc
    if not isinstance(decoded, Mapping):
        raise CanaryRuntimeError(f"{label} must encode a JSON object")
    return decoded


def _source_mappings(record: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    sources: list[Mapping[str, Any]] = []
    if "source" in record and record["source"] is not None:
        source = record["source"]
        if not isinstance(source, Mapping):
            raise CanaryRuntimeError("source must be a mapping")
        sources.append(source)
    if "source_json" in record and record["source_json"] is not None:
        sources.append(_json_mapping(record["source_json"], "source_json"))
    return tuple(sources)


def _fixed_units(value: Any, label: str) -> int:
    """Convert Decimal to 12-place integer units without using its context."""

    if not isinstance(value, Decimal):
        raise CanaryRuntimeError(f"{label} must be Decimal")
    if not value.is_finite():
        raise CanaryRuntimeError(f"{label} must be finite")
    sign, digits_tuple, exponent = value.as_tuple()
    digits = list(digits_tuple)
    if not any(digits):
        return 0

    # Remove insignificant trailing zeroes manually. Decimal.normalize() is
    # context-sensitive and therefore unsuitable for exact KPI accounting.
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1

    if exponent < -_MONEY_DECIMAL_PLACES:
        raise CanaryRuntimeError(
            f"{label} exceeds {_MONEY_DECIMAL_PLACES} decimal places"
        )
    integer_digits = max(len(digits) + exponent, 0)
    if integer_digits > _MONEY_MAX_INTEGER_DIGITS:
        raise CanaryRuntimeError(
            f"{label} exceeds {_MONEY_MAX_INTEGER_DIGITS} integer digits"
        )

    coefficient = 0
    for digit in digits:
        coefficient = coefficient * 10 + digit
    units = coefficient * 10 ** (exponent + _MONEY_DECIMAL_PLACES)
    if units > _MONEY_MAX_UNITS:
        raise CanaryRuntimeError(
            f"{label} exceeds {_MONEY_MAX_INTEGER_DIGITS} integer digits"
        )
    return -units if sign else units


def _units_to_decimal(units: int) -> Decimal:
    if units == 0:
        return Decimal("0")
    sign = 1 if units < 0 else 0
    fractional_places = _MONEY_DECIMAL_PLACES
    while fractional_places > 2 and units % 10 == 0:
        units //= 10
        fractional_places -= 1
    digits = tuple(int(char) for char in str(abs(units)))
    return Decimal((sign, digits, -fractional_places))


def _money_units(
    record: Mapping[str, Any], names: tuple[str, ...], label: str
) -> int:
    present = [name for name in names if name in record and record[name] is not None]
    if not present:
        raise CanaryRuntimeError(f"{label} is required for COMPLETE reconciliation")
    values = [_fixed_units(record[name], name) for name in present]
    if any(value != values[0] for value in values[1:]):
        raise CanaryRuntimeError(f"conflicting {label} values")
    return values[0]


def _fill_counts(record: Mapping[str, Any]) -> tuple[int, int]:
    aggregate_present = "entry_fills" in record or "exit_fills" in record
    detailed_present = any(key in record for key in _FILL_COUNT_FIELDS)

    aggregate: tuple[int, int] | None = None
    detailed: tuple[int, int] | None = None
    if aggregate_present:
        if "entry_fills" not in record or "exit_fills" not in record:
            raise CanaryRuntimeError("entry_fills and exit_fills are both required")
        aggregate = (
            _non_negative_int(record["entry_fills"], "entry_fills"),
            _non_negative_int(record["exit_fills"], "exit_fills"),
        )
    if detailed_present:
        if not all(key in record for key in _FILL_COUNT_FIELDS):
            raise CanaryRuntimeError("all reconciliation fill counts are required")
        values = {
            key: _non_negative_int(record[key], key) for key in _FILL_COUNT_FIELDS
        }
        detailed = (
            values["entry_maker_fills"] + values["entry_taker_fills"],
            values["exit_maker_fills"] + values["exit_taker_fills"],
        )
    if aggregate is None and detailed is None:
        raise CanaryRuntimeError("reconciliation fill counts are required")
    if aggregate is not None and detailed is not None and aggregate != detailed:
        raise CanaryRuntimeError("conflicting reconciliation fill counts")
    return aggregate if aggregate is not None else detailed  # type: ignore[return-value]


def _eligible_for_wr_ev(record: Mapping[str, Any]) -> bool | None:
    flags: list[bool] = []
    if "eligible_for_wr_ev" in record and record["eligible_for_wr_ev"] is not None:
        value = record["eligible_for_wr_ev"]
        if not isinstance(value, bool):
            raise CanaryRuntimeError("eligible_for_wr_ev must be boolean")
        flags.append(value)
    for source in _source_mappings(record):
        if "eligible_for_wr_ev" not in source or source["eligible_for_wr_ev"] is None:
            continue
        value = source["eligible_for_wr_ev"]
        if not isinstance(value, bool):
            raise CanaryRuntimeError("source eligible_for_wr_ev must be boolean")
        flags.append(value)
    if not flags:
        return None
    if any(value != flags[0] for value in flags[1:]):
        raise CanaryRuntimeError("conflicting eligible_for_wr_ev evidence")
    return flags[0]


def _normalize_ids(value: Any, label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise CanaryRuntimeError(f"{label} must be an iterable of IDs")
    ids: list[str] = []
    for raw in value:
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            raise CanaryRuntimeError(f"{label} must contain string or integer IDs")
        item = str(raw).strip()
        if not item:
            raise CanaryRuntimeError(f"{label} must contain non-empty IDs")
        ids.append(item)
    if len(ids) != len(set(ids)):
        raise CanaryRuntimeError(f"{label} contains duplicate IDs")
    return tuple(sorted(ids))


def _exchange_ids(record: Mapping[str, Any], key: str) -> tuple[str, ...]:
    candidates: list[tuple[str, ...]] = []
    if key in record and record[key] is not None:
        candidates.append(_normalize_ids(record[key], key))
    for source in _source_mappings(record):
        if key in source and source[key] is not None:
            candidates.append(_normalize_ids(source[key], f"source.{key}"))
    if not candidates:
        return ()
    if any(candidate != candidates[0] for candidate in candidates[1:]):
        raise CanaryRuntimeError(f"conflicting {key} evidence")
    return candidates[0]


def _session_scope(session_record: Mapping[str, Any]) -> _SessionScope:
    return _SessionScope(
        session_id=_required_text(session_record, "session_id"),
        environment=_required_text(session_record, "environment"),
        account_fingerprint=_required_text(session_record, "account_fingerprint"),
        symbol=_required_text(session_record, "symbol"),
    )


def _session_counters(session_record: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    counters: list[Mapping[str, Any]] = []
    if "counters" in session_record and session_record["counters"] is not None:
        value = session_record["counters"]
        if not isinstance(value, Mapping):
            raise CanaryRuntimeError("counters must be a mapping")
        counters.append(value)
    if "counters_json" in session_record and session_record["counters_json"] is not None:
        counters.append(_json_mapping(session_record["counters_json"], "counters_json"))
    return tuple(counters)


def _accepted_opportunities(session_record: Mapping[str, Any]) -> int:
    candidates: list[int] = []
    if "accepted_opportunities" in session_record:
        candidates.append(
            _non_negative_int(
                session_record["accepted_opportunities"], "accepted_opportunities"
            )
        )
    for counters in _session_counters(session_record):
        if "accepted_opportunities" in counters:
            candidates.append(
                _non_negative_int(
                    counters["accepted_opportunities"],
                    "counters.accepted_opportunities",
                )
            )
    # Use the historical alias only when exact accepted evidence is absent.
    if not candidates and "opportunities" in session_record:
        candidates.append(
            _non_negative_int(session_record["opportunities"], "opportunities")
        )
    if not candidates:
        for counters in _session_counters(session_record):
            if "opportunities" in counters:
                candidates.append(
                    _non_negative_int(
                        counters["opportunities"], "counters.opportunities"
                    )
                )
    if not candidates:
        raise CanaryRuntimeError("accepted opportunities are required")
    if any(value != candidates[0] for value in candidates[1:]):
        raise CanaryRuntimeError("conflicting accepted opportunity counts")
    return candidates[0]


def _raw_memberships(session_record: Mapping[str, Any]) -> Any:
    candidates = [
        session_record[key]
        for key in ("run_memberships", "accepted_run_memberships")
        if key in session_record
    ]
    if not candidates:
        raise CanaryRuntimeError("run_memberships are required")
    if len(candidates) > 1 and candidates[0] != candidates[1]:
        raise CanaryRuntimeError("conflicting run_memberships evidence")
    return candidates[0]


def _memberships(
    session_record: Mapping[str, Any], scope: _SessionScope
) -> dict[str, _Membership]:
    raw = _raw_memberships(session_record)
    rows: list[Mapping[str, Any]] = []
    if isinstance(raw, Mapping):
        for run_key, value in raw.items():
            if not isinstance(value, Mapping):
                raise CanaryRuntimeError("each run membership must be a mapping")
            row = dict(value)
            if "run_id" in row and str(row["run_id"]) != str(run_key):
                raise CanaryRuntimeError("run_memberships key conflicts with run_id")
            row.setdefault("run_id", run_key)
            rows.append(row)
    elif isinstance(raw, Iterable) and not isinstance(raw, (str, bytes)):
        for value in raw:
            if not isinstance(value, Mapping):
                raise CanaryRuntimeError("each run membership must be a mapping")
            rows.append(value)
    else:
        raise CanaryRuntimeError("run_memberships must be a mapping or iterable")

    by_run: dict[str, _Membership] = {}
    opportunity_ids: set[str] = set()
    for row in rows:
        membership = _Membership(
            session_id=_required_text(row, "session_id", label="membership.session_id"),
            opportunity_id=_required_text(
                row, "opportunity_id", label="membership.opportunity_id"
            ),
            run_id=_required_text(row, "run_id", label="membership.run_id"),
            reconciliation_revision=_required_non_negative_int(
                row,
                "reconciliation_revision",
                label="membership.reconciliation_revision",
            ),
            symbol=_required_text(row, "symbol", label="membership.symbol"),
        )
        if membership.session_id != scope.session_id:
            raise CanaryRuntimeError("membership session_id is outside canary scope")
        if membership.symbol != scope.symbol:
            raise CanaryRuntimeError("membership symbol is outside canary scope")
        for key, expected in (
            ("environment", scope.environment),
            ("account_fingerprint", scope.account_fingerprint),
        ):
            if key in row and _required_text(row, key, label=f"membership.{key}") != expected:
                raise CanaryRuntimeError(f"membership {key} is outside canary scope")
        if membership.run_id in by_run:
            raise CanaryRuntimeError(f"duplicate run membership {membership.run_id}")
        if membership.opportunity_id in opportunity_ids:
            raise CanaryRuntimeError(
                f"duplicate opportunity membership {membership.opportunity_id}"
            )
        by_run[membership.run_id] = membership
        opportunity_ids.add(membership.opportunity_id)
    return by_run


def _normalize_reconciliation(
    record: Mapping[str, Any],
    scope: _SessionScope,
    memberships: Mapping[str, _Membership],
) -> _RunEvidence:
    if not isinstance(record, Mapping):
        raise CanaryRuntimeError("each reconciliation record must be a mapping")
    run_id = _required_text(record, "run_id")
    membership = memberships.get(run_id)
    if membership is None:
        raise CanaryRuntimeError(f"run_id {run_id} is outside accepted membership")

    session_id = _required_text(record, "session_id")
    opportunity_id = _required_text(record, "opportunity_id")
    environment = _required_text(record, "environment")
    account_fingerprint = _required_text(record, "account_fingerprint")
    symbol = _required_text(record, "symbol")
    revision = _required_non_negative_int(record, "reconciliation_revision")
    if session_id != scope.session_id or session_id != membership.session_id:
        raise CanaryRuntimeError(f"run {run_id} session_id is outside canary scope")
    if opportunity_id != membership.opportunity_id:
        raise CanaryRuntimeError(f"run {run_id} opportunity_id conflicts with membership")
    if environment != scope.environment:
        raise CanaryRuntimeError(f"run {run_id} environment is outside canary scope")
    if account_fingerprint != scope.account_fingerprint:
        raise CanaryRuntimeError(
            f"run {run_id} account_fingerprint is outside canary scope"
        )
    if symbol != scope.symbol or symbol != membership.symbol:
        raise CanaryRuntimeError(f"run {run_id} symbol is outside canary scope")
    if revision != membership.reconciliation_revision:
        raise CanaryRuntimeError(
            f"run {run_id} reconciliation_revision conflicts with membership"
        )

    status_value = record.get("reconciliation_status", record.get("status"))
    if not isinstance(status_value, str):
        raise CanaryRuntimeError(
            "reconciliation_status must be COMPLETE or DATA_INCOMPLETE"
        )
    status = status_value.upper()
    if status not in {_COMPLETE, _DATA_INCOMPLETE}:
        raise CanaryRuntimeError(
            "reconciliation_status must be COMPLETE or DATA_INCOMPLETE"
        )

    eligibility = _eligible_for_wr_ev(record)
    trade_ids = _exchange_ids(record, "exchange_trade_ids")
    income_ids = _exchange_ids(record, "exchange_income_ids")
    base = dict(
        session_id=session_id,
        opportunity_id=opportunity_id,
        run_id=run_id,
        reconciliation_revision=revision,
        environment=environment,
        account_fingerprint=account_fingerprint,
        symbol=symbol,
        status=status,
        eligible_for_wr_ev=eligibility,
        exchange_trade_ids=trade_ids,
        exchange_income_ids=income_ids,
    )
    if status == _DATA_INCOMPLETE:
        return _RunEvidence(**base)  # type: ignore[arg-type]

    gross = _money_units(
        record,
        ("gross_realized_pnl_usdc", "gross_pnl_usdc"),
        "gross_realized_pnl_usdc",
    )
    commission = _money_units(record, ("commission_usdc",), "commission_usdc")
    funding = _money_units(record, ("funding_usdc",), "funding_usdc")
    net = _money_units(record, ("net_pnl_usdc",), "net_pnl_usdc")
    if commission < 0:
        raise CanaryRuntimeError("commission_usdc must be non-negative")
    if net != gross - commission - funding:
        raise CanaryRuntimeError(f"reconciliation is not exact for run_id {run_id}")
    entry_fills, exit_fills = _fill_counts(record)
    if entry_fills == 0 and exit_fills == 0 and (trade_ids or income_ids):
        raise CanaryRuntimeError(f"no-fill run {run_id} cannot carry exchange IDs")
    if entry_fills > 0 and exit_fills > 0 and eligibility is True and not trade_ids:
        raise CanaryRuntimeError(
            f"paid closed fill {run_id} requires exchange_trade_ids provenance"
        )
    return _RunEvidence(
        **base,  # type: ignore[arg-type]
        gross_pnl_units=gross,
        commission_units=commission,
        funding_units=funding,
        net_pnl_units=net,
        entry_fills=entry_fills,
        exit_fills=exit_fills,
    )


def _unique_reconciliations(
    records: Iterable[Mapping[str, Any]],
    scope: _SessionScope,
    memberships: Mapping[str, _Membership],
) -> tuple[_RunEvidence, ...]:
    if isinstance(records, (str, bytes, Mapping)):
        raise CanaryRuntimeError("reconciliation_records must be an iterable of mappings")
    try:
        iterator = iter(records)
    except TypeError as exc:
        raise CanaryRuntimeError(
            "reconciliation_records must be an iterable of mappings"
        ) from exc

    by_run: dict[str, _RunEvidence] = {}
    trade_owner: dict[str, str] = {}
    income_owner: dict[str, str] = {}
    for raw in iterator:
        evidence = _normalize_reconciliation(raw, scope, memberships)
        previous = by_run.get(evidence.run_id)
        if previous is not None:
            if previous != evidence:
                raise CanaryRuntimeError(
                    f"conflicting duplicate reconciliation for run_id {evidence.run_id}"
                )
            continue
        for exchange_id, owners, label in (
            *((value, trade_owner, "exchange_trade_id") for value in evidence.exchange_trade_ids),
            *((value, income_owner, "exchange_income_id") for value in evidence.exchange_income_ids),
        ):
            owner = owners.get(exchange_id)
            if owner is not None and owner != evidence.run_id:
                raise CanaryRuntimeError(
                    f"{label} {exchange_id} is shared by run_ids {owner} and {evidence.run_id}"
                )
            owners[exchange_id] = evidence.run_id
        by_run[evidence.run_id] = evidence
    return tuple(by_run[run_id] for run_id in sorted(by_run))


def _deadline_reached(session_record: Mapping[str, Any]) -> bool:
    started = _required_non_negative_int(session_record, "started_at_ms")
    derived_deadline = started + V1459_CANARY_TARGET.deadline_hours * 60 * 60 * 1000
    explicit_deadline = _optional_non_negative_int(session_record, "deadline_at_ms")
    if explicit_deadline is not None and explicit_deadline != derived_deadline:
        raise CanaryRuntimeError("deadline_at_ms conflicts with the immutable 72h target")

    observed_values: list[int] = []
    for key in ("last_checkpoint_at_ms", "checkpoint_at_ms", "stopped_at_ms"):
        value = _optional_non_negative_int(session_record, key)
        if value is not None:
            if value < started:
                raise CanaryRuntimeError(f"{key} cannot precede started_at_ms")
            observed_values.append(value)
    if not observed_values:
        raise CanaryRuntimeError("deadline requires observed timestamp evidence")
    derived = max(observed_values) >= derived_deadline

    explicit = session_record.get("deadline_reached", _MISSING)
    if explicit is not _MISSING:
        if not isinstance(explicit, bool):
            raise CanaryRuntimeError("deadline_reached must be boolean")
        if explicit != derived:
            raise CanaryRuntimeError("deadline_reached conflicts with timestamp evidence")
    stop_reason = session_record.get("stop_reason")
    if stop_reason is not None and not isinstance(stop_reason, str):
        raise CanaryRuntimeError("stop_reason must be a string or None")
    if stop_reason == "wall_clock_cap" and not derived:
        raise CanaryRuntimeError("wall_clock_cap conflicts with timestamp evidence")
    return derived


def build_canary_kpi_snapshot(
    session_record: Mapping[str, Any],
    reconciliation_records: Iterable[Mapping[str, Any]],
) -> CanaryKpiSnapshot:
    """Build an exact, outcome-blind snapshot from already persisted evidence."""

    if not isinstance(session_record, Mapping):
        raise CanaryRuntimeError("session_record must be a mapping")
    scope = _session_scope(session_record)
    accepted = _accepted_opportunities(session_record)
    memberships = _memberships(session_record, scope)
    if len(memberships) != accepted:
        raise CanaryRuntimeError(
            "accepted opportunity count conflicts with exact run memberships"
        )
    reconciliations = _unique_reconciliations(
        reconciliation_records, scope, memberships
    )
    paid = tuple(item for item in reconciliations if item.is_paid_closed_fill)
    if len(paid) > V1459_CANARY_TARGET.paid_closed_fills:
        raise CanaryRuntimeError("paid closed fills exceed the canary target")
    if accepted < len(paid):
        raise CanaryRuntimeError(
            "accepted opportunities cannot be fewer than paid closed fills"
        )
    if any(item.net_pnl_units is None for item in paid):
        raise CanaryRuntimeError("paid closed fill is missing exact net PnL")

    net_units = sum(item.net_pnl_units or 0 for item in paid)
    wins = sum(1 for item in paid if (item.net_pnl_units or 0) > 0)
    losses = sum(1 for item in paid if (item.net_pnl_units or 0) < 0)
    flats = sum(1 for item in paid if (item.net_pnl_units or 0) == 0)
    incomplete = sum(1 for item in reconciliations if item.is_incomplete)
    try:
        return CanaryKpiSnapshot(
            paid_closed_fills=len(paid),
            exact_reconciled_paid_closed_fills=len(paid),
            wins=wins,
            losses=losses,
            flats=flats,
            accepted_opportunities=accepted,
            net_pnl_usdc=_units_to_decimal(net_units),
            incomplete_reconciliations=incomplete,
            deadline_reached=_deadline_reached(session_record),
        )
    except CanaryContractError as exc:
        raise CanaryRuntimeError(str(exc)) from exc


aggregate_canary_kpi_snapshot = build_canary_kpi_snapshot
aggregate_canary_snapshot = build_canary_kpi_snapshot


class V1459CanaryRuntime:
    """Stateless facade for composition code; intentionally incapable of I/O."""

    @property
    def permits_order_mutation(self) -> bool:
        return False

    def aggregate(
        self,
        session_record: Mapping[str, Any],
        reconciliation_records: Iterable[Mapping[str, Any]],
    ) -> CanaryKpiSnapshot:
        return build_canary_kpi_snapshot(session_record, reconciliation_records)


__all__ = [
    "CanaryRuntimeError",
    "V1459CanaryRuntime",
    "aggregate_canary_kpi_snapshot",
    "aggregate_canary_snapshot",
    "build_canary_kpi_snapshot",
]
