"""Pure, fail-closed reconciliation of one already-scoped mainnet run.

This module intentionally has no exchange, database, clock, or order-manager
dependency.  Its caller must first collect and scope the exchange records to a
single run.  In particular, this function does *not* infer ownership from an
order prefix: every supplied record must explicitly declare ``owned=True``.

Expected trade fields (snake_case is preferred; Binance-style aliases are
accepted): ``exchange_trade_id``, ``owned``, ``role`` (ENTRY or EXIT),
``is_maker``, ``realized_pnl_usdc``, ``commission_amount``, and
``commission_asset``.  A non-USDC commission also requires an explicit
``commission_conversion_rate_to_usdc``.

Expected income fields are ``exchange_income_id``, ``owned``, ``income_type``
(FUNDING_FEE), ``amount`` (the signed exchange income), and ``asset``.  A
non-USDC amount requires ``amount_conversion_rate_to_usdc``.  Binance reports
funding as signed income, while this module exposes ``funding_usdc`` as a
signed *cost*: a payment of ``-0.01 USDC`` therefore becomes ``+0.01`` and a
rebate becomes negative.  The reported net is always ``gross - commission -
funding``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Iterable, Literal, Mapping


ReconciliationStatus = Literal["COMPLETE", "DATA_INCOMPLETE"]


@dataclass(frozen=True)
class RunReconciliation:
    """A deterministic reconciliation result safe for persistence/reporting.

    Monetary totals and maker/taker counts are only populated for COMPLETE
    evidence.  Callers must use ``eligible_for_wr_ev`` rather than trying to
    derive WR or EV from partial exchange observations.
    """

    reconciliation_status: ReconciliationStatus
    completeness_reasons: tuple[str, ...]
    eligible_for_wr_ev: bool
    gross_realized_pnl_usdc: float | None
    commission_usdc: float | None
    funding_usdc: float | None
    net_pnl_usdc: float | None
    entry_maker_fills: int
    entry_taker_fills: int
    exit_maker_fills: int
    exit_taker_fills: int
    exchange_trade_ids: tuple[str, ...]
    exchange_income_ids: tuple[str, ...]

    @property
    def completeness_reason(self) -> str | None:
        """Storage-ready stable reason string for the current result."""

        return ";".join(self.completeness_reasons) or None

    def as_dict(self) -> dict[str, object]:
        """Return a serializable result with no hidden calculation state."""

        return asdict(self)


@dataclass(frozen=True)
class _Trade:
    exchange_trade_id: str
    role: Literal["ENTRY", "EXIT"]
    is_maker: bool
    realized_pnl_usdc: float
    commission_usdc: float


@dataclass(frozen=True)
class _FundingIncome:
    exchange_income_id: str
    funding_cost_usdc: float


_TRADE_ID_FIELDS = ("exchange_trade_id", "trade_id", "tradeId", "id")
_INCOME_ID_FIELDS = ("exchange_income_id", "income_id", "tranId", "id")
_OWNERSHIP_FIELDS = ("owned", "is_owned")
_TRADE_ROLE_FIELDS = ("role", "fill_role")
_MAKER_FIELDS = ("is_maker", "maker", "isMaker")
_GROSS_FIELDS = ("realized_pnl_usdc", "realized_pnl", "realizedPnl")
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


def _field(record: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in record:
            return record[name]
    return None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    return normalized if math.isfinite(normalized) else None


def _owned(record: Mapping[str, Any]) -> bool | None:
    value = _field(record, _OWNERSHIP_FIELDS)
    return value if isinstance(value, bool) else None


def _rate_to_usdc(
    record: Mapping[str, Any],
    fields: tuple[str, ...],
    *,
    reason_prefix: str,
    record_id: str,
    asset: str,
    reasons: list[str],
) -> float | None:
    """Require an explicit positive conversion rate for a non-USDC asset."""

    raw_rates = [record[field] for field in fields if field in record]
    if not raw_rates or any(rate is None for rate in raw_rates):
        reasons.append(f"MISSING_{reason_prefix}_USDC_CONVERSION:{record_id}:{asset}")
        return None
    normalized_rates = [_number(rate) for rate in raw_rates]
    if any(rate is None or rate <= 0 for rate in normalized_rates):
        reasons.append(f"INVALID_{reason_prefix}_USDC_CONVERSION:{record_id}:{asset}")
        return None
    first = normalized_rates[0]
    if any(rate != first for rate in normalized_rates[1:]):
        reasons.append(f"CONFLICTING_{reason_prefix}_USDC_CONVERSION:{record_id}:{asset}")
        return None
    return first


def _normalize_trade(record: Any, reasons: list[str]) -> _Trade | None:
    if not isinstance(record, Mapping):
        reasons.append("INVALID_TRADE_RECORD")
        return None
    trade_id = _text(_field(record, _TRADE_ID_FIELDS))
    if trade_id is None:
        reasons.append("MISSING_TRADE_ID")
        return None
    ownership = _owned(record)
    if ownership is None:
        reasons.append(f"UNKNOWN_TRADE_OWNERSHIP:{trade_id}")
        return None
    if not ownership:
        reasons.append(f"UNOWNED_TRADE:{trade_id}")
        return None

    role = _text(_field(record, _TRADE_ROLE_FIELDS))
    normalized_role = role.upper() if role is not None else None
    if normalized_role not in {"ENTRY", "EXIT"}:
        reasons.append(f"INVALID_TRADE_ROLE:{trade_id}")
        return None
    maker = _field(record, _MAKER_FIELDS)
    if not isinstance(maker, bool):
        reasons.append(f"MISSING_TRADE_MAKER_FLAG:{trade_id}")
        return None
    gross = _number(_field(record, _GROSS_FIELDS))
    if gross is None:
        reasons.append(f"MISSING_TRADE_REALIZED_PNL:{trade_id}")
        return None
    commission_amount = _number(_field(record, _COMMISSION_AMOUNT_FIELDS))
    if commission_amount is None or commission_amount < 0:
        reasons.append(f"INVALID_TRADE_COMMISSION:{trade_id}")
        return None
    asset = _text(_field(record, _COMMISSION_ASSET_FIELDS))
    if asset is None:
        reasons.append(f"MISSING_TRADE_COMMISSION_ASSET:{trade_id}")
        return None
    asset = asset.upper()
    if asset == "USDC":
        commission_usdc = commission_amount
    else:
        rate = _rate_to_usdc(
            record,
            _COMMISSION_RATE_FIELDS,
            reason_prefix="COMMISSION",
            record_id=trade_id,
            asset=asset,
            reasons=reasons,
        )
        if rate is None:
            return None
        commission_usdc = commission_amount * rate
    return _Trade(trade_id, normalized_role, maker, gross, commission_usdc)


def _normalize_income(record: Any, reasons: list[str]) -> _FundingIncome | None:
    if not isinstance(record, Mapping):
        reasons.append("INVALID_INCOME_RECORD")
        return None
    income_id = _text(_field(record, _INCOME_ID_FIELDS))
    if income_id is None:
        reasons.append("MISSING_INCOME_ID")
        return None
    ownership = _owned(record)
    if ownership is None:
        reasons.append(f"UNKNOWN_INCOME_OWNERSHIP:{income_id}")
        return None
    if not ownership:
        # Do not silently attribute an unowned account-level funding event.
        reasons.append(f"UNOWNED_INCOME:{income_id}")
        return None
    income_type = _text(_field(record, _INCOME_TYPE_FIELDS))
    if income_type is None or income_type.upper() != "FUNDING_FEE":
        reasons.append(f"UNSUPPORTED_INCOME_TYPE:{income_id}")
        return None
    amount = _number(_field(record, _INCOME_AMOUNT_FIELDS))
    if amount is None:
        reasons.append(f"MISSING_FUNDING_AMOUNT:{income_id}")
        return None
    asset = _text(record.get("asset"))
    if asset is None:
        reasons.append(f"MISSING_FUNDING_ASSET:{income_id}")
        return None
    asset = asset.upper()
    if asset == "USDC":
        amount_usdc = amount
    else:
        rate = _rate_to_usdc(
            record,
            _INCOME_RATE_FIELDS,
            reason_prefix="FUNDING",
            record_id=income_id,
            asset=asset,
            reasons=reasons,
        )
        if rate is None:
            return None
        amount_usdc = amount * rate
    # Exchange income is negative for a funding payment; public output is cost.
    return _FundingIncome(income_id, -amount_usdc)


def _unique_by_id(
    records: Iterable[_Trade] | Iterable[_FundingIncome],
    *,
    id_attribute: str,
    conflict_prefix: str,
    reasons: list[str],
) -> list[_Trade] | list[_FundingIncome]:
    grouped: dict[str, list[_Trade] | list[_FundingIncome]] = {}
    for record in records:
        record_id = getattr(record, id_attribute)
        grouped.setdefault(record_id, []).append(record)
    unique: list[_Trade] | list[_FundingIncome] = []
    for record_id in sorted(grouped):
        entries = grouped[record_id]
        if any(entry != entries[0] for entry in entries[1:]):
            reasons.append(f"CONFLICTING_{conflict_prefix}_ID:{record_id}")
            continue
        unique.append(entries[0])
    return unique


def reconcile_run(
    trades: Iterable[Mapping[str, Any]],
    incomes: Iterable[Mapping[str, Any]],
    *,
    require_closed_run: bool = False,
) -> RunReconciliation:
    """Reconcile pre-scoped exchange records without I/O or ownership inference.

    Exact duplicate IDs collapse to one record.  A duplicate ID whose normalized
    financial/role payload conflicts makes the entire result DATA_INCOMPLETE.
    Any failure similarly clears aggregate totals and count fields, preventing
    accidental inclusion in WR or EV calculations.
    """

    reasons: list[str] = []
    try:
        normalized_trades = [
            result
            for result in (_normalize_trade(record, reasons) for record in trades)
            if result is not None
        ]
    except TypeError:
        reasons.append("INVALID_TRADES_ITERABLE")
        normalized_trades = []
    try:
        normalized_incomes = [
            result
            for result in (_normalize_income(record, reasons) for record in incomes)
            if result is not None
        ]
    except TypeError:
        reasons.append("INVALID_INCOMES_ITERABLE")
        normalized_incomes = []

    unique_trades = _unique_by_id(
        normalized_trades,
        id_attribute="exchange_trade_id",
        conflict_prefix="TRADE",
        reasons=reasons,
    )
    unique_incomes = _unique_by_id(
        normalized_incomes,
        id_attribute="exchange_income_id",
        conflict_prefix="INCOME",
        reasons=reasons,
    )
    if require_closed_run:
        if not any(trade.role == "ENTRY" for trade in unique_trades):
            reasons.append("MISSING_ENTRY_TRADE")
        if not any(trade.role == "EXIT" for trade in unique_trades):
            reasons.append("MISSING_EXIT_TRADE")

    trade_ids = tuple(sorted(trade.exchange_trade_id for trade in unique_trades))
    income_ids = tuple(sorted(income.exchange_income_id for income in unique_incomes))
    stable_reasons = tuple(sorted(set(reasons)))
    if stable_reasons:
        return RunReconciliation(
            reconciliation_status="DATA_INCOMPLETE",
            completeness_reasons=stable_reasons,
            eligible_for_wr_ev=False,
            gross_realized_pnl_usdc=None,
            commission_usdc=None,
            funding_usdc=None,
            net_pnl_usdc=None,
            entry_maker_fills=0,
            entry_taker_fills=0,
            exit_maker_fills=0,
            exit_taker_fills=0,
            exchange_trade_ids=trade_ids,
            exchange_income_ids=income_ids,
        )

    gross = math.fsum(trade.realized_pnl_usdc for trade in unique_trades)
    commission = math.fsum(trade.commission_usdc for trade in unique_trades)
    funding = math.fsum(income.funding_cost_usdc for income in unique_incomes)
    return RunReconciliation(
        reconciliation_status="COMPLETE",
        completeness_reasons=(),
        eligible_for_wr_ev=True,
        gross_realized_pnl_usdc=gross,
        commission_usdc=commission,
        funding_usdc=funding,
        net_pnl_usdc=gross - commission - funding,
        entry_maker_fills=sum(
            trade.role == "ENTRY" and trade.is_maker for trade in unique_trades
        ),
        entry_taker_fills=sum(
            trade.role == "ENTRY" and not trade.is_maker for trade in unique_trades
        ),
        exit_maker_fills=sum(
            trade.role == "EXIT" and trade.is_maker for trade in unique_trades
        ),
        exit_taker_fills=sum(
            trade.role == "EXIT" and not trade.is_maker for trade in unique_trades
        ),
        exchange_trade_ids=trade_ids,
        exchange_income_ids=income_ids,
    )
