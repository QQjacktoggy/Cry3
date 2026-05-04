"""Binance Futures API response data models.

Based on actual API responses from FAPI endpoints (verified 2026-05-02).
"""

from dataclasses import dataclass, field


@dataclass
class FuturesTrade:
    """Maps to /fapi/v1/userTrades response."""

    trade_id: int
    order_id: int
    symbol: str
    side: str  # BUY / SELL
    price: float
    qty: float
    quote_qty: float
    realized_pnl: float
    commission: float
    commission_asset: str
    time_ms: int
    position_side: str  # BOTH / LONG / SHORT
    is_buyer: bool
    is_maker: bool

    @classmethod
    def from_api(cls, data: dict) -> "FuturesTrade":
        return cls(
            trade_id=data["id"],
            order_id=data["orderId"],
            symbol=data["symbol"],
            side=data["side"],
            price=float(data["price"]),
            qty=float(data["qty"]),
            quote_qty=float(data.get("quoteQty", 0)),
            realized_pnl=float(data.get("realizedPnl", 0)),
            commission=float(data.get("commission", 0)),
            commission_asset=data.get("commissionAsset", "USDC"),
            time_ms=data["time"],
            position_side=data.get("positionSide", "BOTH"),
            is_buyer=data.get("buyer", False),
            is_maker=data.get("maker", False),
        )

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "price": self.price,
            "qty": self.qty,
            "quote_qty": self.quote_qty,
            "realized_pnl": self.realized_pnl,
            "commission": self.commission,
            "commission_asset": self.commission_asset,
            "time_ms": self.time_ms,
            "position_side": self.position_side,
            "is_buyer": self.is_buyer,
            "is_maker": self.is_maker,
        }

    @classmethod
    def from_db(cls, row: dict) -> "FuturesTrade":
        """Reconstruct a FuturesTrade from a SQLite row dict."""
        return cls(
            trade_id=row["trade_id"],
            order_id=row["order_id"],
            symbol=row["symbol"],
            side=row["side"],
            price=float(row["price"]),
            qty=float(row["qty"]),
            quote_qty=float(row["quote_qty"]),
            realized_pnl=float(row["realized_pnl"]),
            commission=float(row["commission"]),
            commission_asset=row["commission_asset"],
            time_ms=row["time_ms"],
            position_side=row.get("position_side", "BOTH"),
            is_buyer=row["side"] == "BUY",
            is_maker=bool(row.get("is_maker", False)),
        )


@dataclass
class IncomeRecord:
    """Maps to /fapi/v1/income response.

    income_type values:
    - REALIZED_PNL: per-trade realized profit/loss
    - COMMISSION: trading fee
    - FUNDING_FEE: funding rate payment (every 8h)
    - STRATEGY_UMFUTURES_TRANSFER: grid bot CREATE/CLOSE fund transfer
    - TRANSFER: manual fund transfer
    """

    tran_id: int
    symbol: str  # may be empty for STRATEGY_UMFUTURES_TRANSFER
    income_type: str
    income: float
    asset: str
    time_ms: int
    info: str  # trade ID, "FUNDING_FEE", "UM_GRID_CREATE", "UM_GRID_CLOSE", etc.
    trade_id: str  # may be empty

    @classmethod
    def from_api(cls, data: dict) -> "IncomeRecord":
        return cls(
            tran_id=data["tranId"],
            symbol=data.get("symbol", ""),
            income_type=data["incomeType"],
            income=float(data["income"]),
            asset=data.get("asset", "USDC"),
            time_ms=data["time"],
            info=data.get("info", ""),
            trade_id=data.get("tradeId", ""),
        )

    def to_dict(self) -> dict:
        return {
            "tran_id": self.tran_id,
            "symbol": self.symbol,
            "income_type": self.income_type,
            "income": self.income,
            "asset": self.asset,
            "time_ms": self.time_ms,
            "info": self.info,
            "trade_id": self.trade_id,
        }

    @property
    def is_grid_create(self) -> bool:
        return self.income_type == "STRATEGY_UMFUTURES_TRANSFER" and self.info == "UM_GRID_CREATE"

    @property
    def is_grid_close(self) -> bool:
        return self.income_type == "STRATEGY_UMFUTURES_TRANSFER" and self.info == "UM_GRID_CLOSE"


@dataclass
class GridSession:
    """A grid bot lifecycle from CREATE to CLOSE.

    Built by pairing STRATEGY_UMFUTURES_TRANSFER records.
    Grid config fields are populated later from the Binance share link.
    """

    create_time_ms: int
    close_time_ms: int | None  # None if still running
    invested_amount: float  # absolute value of CREATE transfer
    returned_amount: float | None  # CLOSE transfer amount
    net_profit: float | None  # returned - invested
    asset: str
    create_tran_id: int
    close_tran_id: int | None
    symbol: str | None = None          # inferred from trades in the time window
    is_active: bool = True
    # Grid configuration — populated from Binance share link
    direction: str | None = None       # NEUTRAL / LONG / SHORT
    grid_type: str | None = None       # GEO / ARITHMETIC
    leverage: int | None = None
    grid_count: int | None = None
    lower_price: float | None = None
    upper_price: float | None = None
    stop_loss_price: float | None = None
    take_profit_price: float | None = None
    strategy_id: str | None = None    # csi from share link
    share_link: str | None = None
    notified_close: bool = False       # True after close notification sent

    @classmethod
    def from_create(cls, income: IncomeRecord) -> "GridSession":
        return cls(
            create_time_ms=income.time_ms,
            close_time_ms=None,
            invested_amount=abs(income.income),
            returned_amount=None,
            net_profit=None,
            asset=income.asset,
            create_tran_id=income.tran_id,
            close_tran_id=None,
            is_active=True,
        )

    def close_with(self, income: IncomeRecord) -> None:
        self.close_time_ms = income.time_ms
        self.returned_amount = income.income
        self.net_profit = income.income - self.invested_amount
        self.close_tran_id = income.tran_id
        self.is_active = False

    def to_dict(self) -> dict:
        return {
            "create_time_ms": self.create_time_ms,
            "close_time_ms": self.close_time_ms,
            "invested_amount": self.invested_amount,
            "returned_amount": self.returned_amount,
            "net_profit": self.net_profit,
            "asset": self.asset,
            "create_tran_id": self.create_tran_id,
            "close_tran_id": self.close_tran_id,
            "symbol": self.symbol,
            "is_active": self.is_active,
            "direction": self.direction,
            "grid_type": self.grid_type,
            "leverage": self.leverage,
            "grid_count": self.grid_count,
            "lower_price": self.lower_price,
            "upper_price": self.upper_price,
            "stop_loss_price": self.stop_loss_price,
            "take_profit_price": self.take_profit_price,
            "strategy_id": self.strategy_id,
            "share_link": self.share_link,
            "notified_close": self.notified_close,
        }


@dataclass
class MarketSnapshot:
    """Market data for a single symbol at a point in time."""

    symbol: str
    current_price: float
    high_24h: float
    low_24h: float
    volume_24h: float
    price_change_pct_24h: float
    funding_rate: float | None = None
    next_funding_time_ms: int | None = None
    mark_price: float | None = None
    index_price: float | None = None
    klines: list[list] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "current_price": self.current_price,
            "high_24h": self.high_24h,
            "low_24h": self.low_24h,
            "volume_24h": self.volume_24h,
            "price_change_pct_24h": self.price_change_pct_24h,
            "funding_rate": self.funding_rate,
            "next_funding_time_ms": self.next_funding_time_ms,
            "mark_price": self.mark_price,
            "index_price": self.index_price,
            "klines": self.klines,
        }


@dataclass
class PositionInfo:
    """Current futures position for a symbol."""

    symbol: str
    position_amt: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    liquidation_price: float
    leverage: int
    margin_type: str  # "cross" or "isolated"
    isolated_margin: float | None = None
    initial_margin: float | None = None
    maint_margin: float | None = None

    @classmethod
    def from_api(cls, data: dict) -> "PositionInfo":
        return cls(
            symbol=data["symbol"],
            position_amt=float(data["positionAmt"]),
            entry_price=float(data["entryPrice"]),
            mark_price=float(data["markPrice"]),
            unrealized_pnl=float(data["unRealizedProfit"]),
            liquidation_price=float(data.get("liquidationPrice", 0)),
            leverage=int(data["leverage"]),
            margin_type=data.get("marginType", "cross"),
            isolated_margin=float(data["isolatedMargin"]) if data.get("isolatedMargin") else None,
            initial_margin=float(data["initialMargin"]) if data.get("initialMargin") else None,
            maint_margin=float(data["maintMargin"]) if data.get("maintMargin") else None,
        )

    @property
    def distance_to_liquidation_pct(self) -> float | None:
        """Percentage distance from current mark price to liquidation price."""
        if not self.liquidation_price or self.liquidation_price == 0 or not self.mark_price:
            return None
        return ((self.mark_price - self.liquidation_price) / self.mark_price) * 100

    @property
    def position_direction(self) -> str:
        if self.position_amt > 0:
            return "LONG"
        elif self.position_amt < 0:
            return "SHORT"
        return "FLAT"


@dataclass
class AccountInfo:
    """Summary of futures account margin status."""

    total_wallet_balance: float
    total_unrealized_profit: float
    total_margin_balance: float
    total_maint_margin: float
    available_balance: float

    @classmethod
    def from_api(cls, data: dict) -> "AccountInfo":
        return cls(
            total_wallet_balance=float(data.get("totalWalletBalance", 0)),
            total_unrealized_profit=float(data.get("totalUnrealizedProfit", 0)),
            total_margin_balance=float(data.get("totalMarginBalance", 0)),
            total_maint_margin=float(data.get("totalMaintMargin", 0)),
            available_balance=float(data.get("availableBalance", 0)),
        )

    @property
    def margin_ratio(self) -> float | None:
        """Maintenance margin / margin balance. Higher = riskier."""
        if self.total_margin_balance <= 0:
            return None
        return self.total_maint_margin / self.total_margin_balance


@dataclass
class FetchResult:
    """Aggregated result from a single fetch cycle for one symbol."""

    symbol: str
    trades: list[FuturesTrade]
    income_records: list[IncomeRecord]
    market: MarketSnapshot
    position: PositionInfo | None = None
    account: AccountInfo | None = None
