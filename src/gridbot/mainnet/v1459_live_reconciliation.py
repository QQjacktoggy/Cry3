"""Pure Binance evidence mapping for v1.4.59 live reconciliation.

The caller owns collection and exact run/time/symbol scoping. This module only
proves order ownership, assigns fill roles, and builds the four sequences
accepted by :meth:`V1459ObservationRuntime.record_reconciliation`. It performs
no exchange, database, clock, or other I/O.

Ownership is intentionally fail-closed. A trade is owned only when its order
ID is present in the map proved by a ``<run_id>_`` client-order prefix or by a
caller-supplied event-owned order ID. Explicit stop-loss IDs are treated as
caller-supplied event evidence and are assigned the EXIT role.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterator

from src.gridbot.mainnet.run_reconciler import reconcile_run


_ORDER_RECORD_ID_FIELDS = (
    "order_id",
    "orderId",
    "actualOrderId",
    "algoId",
    "id",
)
_TRADE_ORDER_ID_FIELDS = ("order_id", "orderId")
_CLIENT_ORDER_ID_FIELDS = (
    "client_order_id",
    "clientOrderId",
    "origClientOrderId",
    "newClientOrderId",
    "clientAlgoId",
)
_TRADE_ID_FIELDS = ("exchange_trade_id", "trade_id", "tradeId", "id")
_INCOME_ID_FIELDS = (
    "exchange_income_id",
    "income_id",
    "tran_id",
    "tranId",
    "id",
)
_MAKER_FIELDS = ("is_maker", "maker", "isMaker")
_REALIZED_PNL_FIELDS = ("realized_pnl_usdc", "realized_pnl", "realizedPnl")
_COMMISSION_AMOUNT_FIELDS = ("commission_amount", "commission")
_COMMISSION_ASSET_FIELDS = ("commission_asset", "commissionAsset")
_COMMISSION_RATE_FIELDS = (
    "commission_conversion_rate_to_usdc",
    "commission_to_usdc_rate",
    "conversion_rate_to_usdc",
)
_INCOME_TYPE_FIELDS = ("income_type", "incomeType")
_INCOME_AMOUNT_FIELDS = ("amount", "income")
_INCOME_RATE_FIELDS = (
    "amount_conversion_rate_to_usdc",
    "income_conversion_rate_to_usdc",
    "conversion_rate_to_usdc",
)
_OWNERSHIP_FIELDS = ("owned", "is_owned")

_ENTRY_SUFFIX_FAMILIES = ("_entry_r", "_dca")
_EXIT_SUFFIX_FAMILIES = (
    "_close",
    "_tp",
    "_sl",
    "_trail",
    "_be",
    "_no_bounce",
    "_dust",
)


@dataclass(frozen=True)
class V1459ReconciliationPayloads:
    """The four sequences consumed by ``record_reconciliation``."""

    reconciler_trades: tuple[dict[str, Any], ...]
    reconciler_incomes: tuple[dict[str, Any], ...]
    persistence_trades: tuple[dict[str, Any], ...]
    persistence_incomes: tuple[dict[str, Any], ...]

    @property
    def trades(self) -> tuple[dict[str, Any], ...]:
        """Alias matching ``record_reconciliation(trades=...)``."""

        return self.reconciler_trades

    @property
    def incomes(self) -> tuple[dict[str, Any], ...]:
        """Alias matching ``record_reconciliation(incomes=...)``."""

        return self.reconciler_incomes

    def as_record_reconciliation_kwargs(
        self,
    ) -> dict[str, tuple[dict[str, Any], ...]]:
        """Return only the four keyword arguments owned by this mapper."""

        return {
            "trades": self.reconciler_trades,
            "incomes": self.reconciler_incomes,
            "persistence_trades": self.persistence_trades,
            "persistence_incomes": self.persistence_incomes,
        }

    def __iter__(self) -> Iterator[tuple[dict[str, Any], ...]]:
        """Allow explicit four-value unpacking without changing field names."""

        yield self.reconciler_trades
        yield self.reconciler_incomes
        yield self.persistence_trades
        yield self.persistence_incomes


def _values(record: Mapping[str, Any], fields: tuple[str, ...]) -> list[Any]:
    return [record[field] for field in fields if field in record]


def _identifier(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _coalesced_identifier(
    record: Mapping[str, Any], fields: tuple[str, ...]
) -> str | None:
    values = _values(record, fields)
    if not values:
        return None
    normalized = [_identifier(value) for value in values]
    if any(value is None for value in normalized) or len(set(normalized)) != 1:
        return None
    return normalized[0]


def _text(value: Any, *, uppercase: bool = False) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    return normalized.upper() if uppercase else normalized


def _coalesced_text(
    record: Mapping[str, Any],
    fields: tuple[str, ...],
    *,
    uppercase: bool = False,
) -> str | None:
    values = _values(record, fields)
    if not values:
        return None
    normalized = [_text(value, uppercase=uppercase) for value in values]
    if any(value is None for value in normalized) or len(set(normalized)) != 1:
        return None
    return normalized[0]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return normalized if math.isfinite(normalized) else None


def _coalesced_number(
    record: Mapping[str, Any], fields: tuple[str, ...]
) -> float | None:
    values = _values(record, fields)
    if not values:
        return None
    normalized = [_number(value) for value in values]
    if any(value is None for value in normalized) or len(set(normalized)) != 1:
        return None
    return normalized[0]


def _coalesced_flag(
    record: Mapping[str, Any], fields: tuple[str, ...]
) -> bool | None:
    values = _values(record, fields)
    if not values or any(not isinstance(value, bool) for value in values):
        return None
    if len(set(values)) != 1:
        return None
    return values[0]


def _explicit_owned(record: Mapping[str, Any]) -> bool:
    values = _values(record, _OWNERSHIP_FIELDS)
    return bool(values) and all(value is True for value in values)


def _source(record: Mapping[str, Any]) -> dict[str, Any]:
    value = record.get("source")
    return dict(value) if isinstance(value, Mapping) else {}


def _normalized_rates(
    record: Mapping[str, Any], fields: tuple[str, ...]
) -> dict[str, float | None]:
    return {field: _number(record[field]) for field in fields if field in record}


def _consistent_positive_rate(rates: Mapping[str, float | None]) -> float | None:
    if not rates:
        return None
    values = tuple(rates.values())
    if any(value is None or value <= 0 for value in values):
        return None
    return values[0] if len(set(values)) == 1 else None


def _amount_usdc(
    amount: float | None,
    asset: str | None,
    rates: Mapping[str, float | None],
) -> float | None:
    if amount is None or asset is None:
        return None
    if asset == "USDC":
        return amount
    rate = _consistent_positive_rate(rates)
    return None if rate is None else amount * rate


def _identifier_set(values: Iterable[Any] | Any | None) -> set[str]:
    if values is None:
        return set()
    if isinstance(values, (str, int)) and not isinstance(values, bool):
        candidates = (values,)
    else:
        try:
            candidates = tuple(values)
        except TypeError:
            candidates = (values,)
    return {
        normalized
        for value in candidates
        if (normalized := _identifier(value)) is not None
    }


def _is_suffix_family(suffix: str, family: str) -> bool:
    if suffix == family:
        return True
    if not suffix.startswith(family):
        return False
    remainder = suffix[len(family) :]
    return bool(remainder) and (remainder[0].isdigit() or remainder[0] == "_")


def _role_from_client_order_id(run_id: str, client_order_id: str | None) -> str | None:
    prefix = f"{run_id}_"
    if client_order_id is None or not client_order_id.startswith(prefix):
        return None
    suffix = client_order_id[len(run_id) :]
    if suffix == "_entry" or any(
        _is_suffix_family(suffix, family) for family in _ENTRY_SUFFIX_FAMILIES
    ):
        return "ENTRY"
    if any(_is_suffix_family(suffix, family) for family in _EXIT_SUFFIX_FAMILIES):
        return "EXIT"
    return None


def _owned_order_roles(
    run_id: str,
    orders: Iterable[Mapping[str, Any]],
    *,
    event_owned_order_ids: set[str],
    explicit_sl_order_ids: set[str],
    event_owned_order_roles: Mapping[str, str],
) -> dict[str, str | None]:
    owned_ids = set(event_owned_order_ids) | set(explicit_sl_order_ids)
    role_candidates: dict[str, set[str]] = {
        order_id: {"EXIT"} for order_id in explicit_sl_order_ids
    }
    for order_id, role in event_owned_order_roles.items():
        owned_ids.add(order_id)
        role_candidates.setdefault(order_id, set()).add(role)
    prefix = f"{run_id}_"

    for raw_order in orders:
        if not isinstance(raw_order, Mapping):
            continue
        order_id = _coalesced_identifier(raw_order, _ORDER_RECORD_ID_FIELDS)
        if order_id is None:
            continue
        client_order_id = _coalesced_text(raw_order, _CLIENT_ORDER_ID_FIELDS)
        prefix_owned = client_order_id is not None and client_order_id.startswith(prefix)
        if not prefix_owned and order_id not in owned_ids:
            continue
        owned_ids.add(order_id)
        role = _role_from_client_order_id(run_id, client_order_id)
        if role is not None:
            role_candidates.setdefault(order_id, set()).add(role)

    return {
        order_id: next(iter(candidates)) if len(candidates) == 1 else None
        for order_id in owned_ids
        for candidates in (role_candidates.get(order_id, set()),)
    }


def _event_owned_order_roles(values: Mapping[Any, Any] | None) -> dict[str, str]:
    """Normalize explicit event-proven order roles without inferring fills."""

    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise ValueError("event_owned_order_roles must be a mapping")
    normalized: dict[str, str] = {}
    for raw_order_id, raw_role in values.items():
        order_id = _identifier(raw_order_id)
        role = _text(raw_role, uppercase=True)
        if order_id is None or role not in {"ENTRY", "EXIT"}:
            raise ValueError("event_owned_order_roles must contain ENTRY or EXIT roles")
        existing = normalized.get(order_id)
        if existing is not None and existing != role:
            raise ValueError("event-owned order role conflict")
        normalized[order_id] = role
    return normalized


def _trade_payloads(
    raw_trade: Any, owned_order_roles: Mapping[str, str | None]
) -> tuple[dict[str, Any], dict[str, Any]]:
    trade = raw_trade if isinstance(raw_trade, Mapping) else {}
    exchange_trade_id = _coalesced_identifier(trade, _TRADE_ID_FIELDS)
    order_id = _coalesced_identifier(trade, _TRADE_ORDER_ID_FIELDS)
    owned = order_id is not None and order_id in owned_order_roles
    role = owned_order_roles.get(order_id) if owned else None
    maker = _coalesced_flag(trade, _MAKER_FIELDS)
    realized_pnl = _coalesced_number(trade, _REALIZED_PNL_FIELDS)
    commission_amount = _coalesced_number(trade, _COMMISSION_AMOUNT_FIELDS)
    commission_asset = _coalesced_text(
        trade, _COMMISSION_ASSET_FIELDS, uppercase=True
    )
    rates = _normalized_rates(trade, _COMMISSION_RATE_FIELDS)
    source = _source(trade)

    reconciler = {
        "exchange_trade_id": exchange_trade_id,
        "owned": owned,
        "role": role,
        "is_maker": maker,
        "realized_pnl_usdc": realized_pnl,
        "commission_amount": commission_amount,
        "commission_asset": commission_asset,
        "source": source,
        **rates,
    }
    persistence = {
        "exchange_trade_id": exchange_trade_id,
        "order_id": order_id,
        "role": role,
        "is_maker": maker,
        "realized_pnl_usdc": realized_pnl,
        "commission_amount": commission_amount,
        "commission_asset": commission_asset,
        "commission_usdc": _amount_usdc(
            commission_amount, commission_asset, rates
        ),
        "source": source,
    }
    return reconciler, persistence


def _income_payloads(raw_income: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    income = raw_income if isinstance(raw_income, Mapping) else {}
    exchange_income_id = _coalesced_identifier(income, _INCOME_ID_FIELDS)
    income_type = _coalesced_text(income, _INCOME_TYPE_FIELDS, uppercase=True)
    amount = _coalesced_number(income, _INCOME_AMOUNT_FIELDS)
    asset = _coalesced_text(income, ("asset",), uppercase=True)
    rates = _normalized_rates(income, _INCOME_RATE_FIELDS)
    source = _source(income)

    reconciler = {
        "exchange_income_id": exchange_income_id,
        "owned": _explicit_owned(income),
        "income_type": income_type,
        "amount": amount,
        "asset": asset,
        "source": source,
        **rates,
    }
    persistence = {
        "exchange_income_id": exchange_income_id,
        "income_type": income_type,
        "amount": amount,
        "asset": asset,
        "amount_usdc": _amount_usdc(amount, asset, rates),
        "source": source,
    }
    return reconciler, persistence


def _accepted_persistence(
    candidates: Iterable[dict[str, Any]],
    *,
    id_field: str,
    accepted_ids: tuple[str, ...],
) -> tuple[dict[str, Any], ...]:
    accepted = set(accepted_ids)
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        record_id = candidate.get(id_field)
        if record_id not in accepted or record_id in seen:
            continue
        seen.add(record_id)
        result.append(candidate)
    return tuple(result)


def build_reconciliation_payloads(
    *,
    run_id: str,
    orders: Iterable[Mapping[str, Any]],
    trades: Iterable[Mapping[str, Any]],
    funding_incomes: Iterable[Mapping[str, Any]] = (),
    event_owned_order_ids: Iterable[Any] = (),
    explicit_sl_order_ids: Iterable[Any] = (),
    event_owned_order_roles: Mapping[Any, Any] | None = None,
) -> V1459ReconciliationPayloads:
    """Map already-scoped Binance records to observation runtime payloads.

    Numeric Binance strings are normalized to finite floats and exchange IDs
    are normalized to strings for the existing reconciler/repository contract.
    Conversion rates are copied only when explicitly supplied. Records that
    cannot be fully reconciled remain in the reconciler sequences so that
    ``reconcile_run`` returns ``DATA_INCOMPLETE``; only IDs accepted by that
    reconciler are emitted to persistence sequences.
    """

    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id is required")
    normalized_run_id = run_id.strip()
    order_rows = tuple(orders)
    trade_rows = tuple(trades)
    income_rows = tuple(funding_incomes)
    event_ids = _identifier_set(event_owned_order_ids)
    sl_ids = _identifier_set(explicit_sl_order_ids)
    event_roles = _event_owned_order_roles(event_owned_order_roles)
    owned_order_roles = _owned_order_roles(
        normalized_run_id,
        order_rows,
        event_owned_order_ids=event_ids,
        explicit_sl_order_ids=sl_ids,
        event_owned_order_roles=event_roles,
    )

    trade_pairs = tuple(
        _trade_payloads(trade, owned_order_roles) for trade in trade_rows
    )
    income_pairs = tuple(_income_payloads(income) for income in income_rows)
    reconciler_trades = tuple(pair[0] for pair in trade_pairs)
    reconciler_incomes = tuple(pair[0] for pair in income_pairs)
    reconciliation = reconcile_run(reconciler_trades, reconciler_incomes)
    persistence_trades = _accepted_persistence(
        (pair[1] for pair in trade_pairs),
        id_field="exchange_trade_id",
        accepted_ids=reconciliation.exchange_trade_ids,
    )
    persistence_incomes = _accepted_persistence(
        (pair[1] for pair in income_pairs),
        id_field="exchange_income_id",
        accepted_ids=reconciliation.exchange_income_ids,
    )
    return V1459ReconciliationPayloads(
        reconciler_trades=reconciler_trades,
        reconciler_incomes=reconciler_incomes,
        persistence_trades=persistence_trades,
        persistence_incomes=persistence_incomes,
    )


# Descriptive alias for callers that prefer the version/module name at import.
build_v1459_live_reconciliation_payloads = build_reconciliation_payloads


__all__ = [
    "V1459ReconciliationPayloads",
    "build_reconciliation_payloads",
    "build_v1459_live_reconciliation_payloads",
]
