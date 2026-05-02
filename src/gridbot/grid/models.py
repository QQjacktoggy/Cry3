"""Grid performance metrics data model."""

from dataclasses import dataclass, field


@dataclass
class GridMetrics:
    """Computed performance metrics for a grid bot on a single symbol."""

    symbol: str

    # P&L breakdown
    realized_pnl: float  # from income REALIZED_PNL
    unrealized_pnl: float  # from position info
    funding_cost: float  # from income FUNDING_FEE (actual, not estimated)
    commission_total: float  # from income COMMISSION
    net_pnl: float  # realized + unrealized - funding - commission

    # Trade stats
    total_trades: int
    buy_trades: int
    sell_trades: int
    maker_trades: int
    taker_trades: int
    maker_ratio: float  # maker_trades / total_trades

    # Grid efficiency
    fill_rate: float
    price_range_utilization: float
    avg_trade_interval_minutes: float | None
    trades_per_hour: float

    # APR
    apr_estimate: float | None

    # Futures-specific
    leverage: int | None = None
    liquidation_price: float | None = None
    distance_to_liquidation_pct: float | None = None
    margin_ratio: float | None = None
    position_direction: str = "FLAT"  # LONG / SHORT / FLAT
    position_size: float | None = None  # abs(positionAmt)

    # Grid range
    grid_lower_price: float | None = None
    grid_upper_price: float | None = None
    current_price: float | None = None

    # Session info
    investment_amount: float | None = None  # from grid session
    grid_session_profit: float | None = None  # current session net profit
    running_hours: float | None = None

    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "funding_cost": self.funding_cost,
            "commission_total": self.commission_total,
            "net_pnl": self.net_pnl,
            "total_trades": self.total_trades,
            "buy_trades": self.buy_trades,
            "sell_trades": self.sell_trades,
            "maker_trades": self.maker_trades,
            "taker_trades": self.taker_trades,
            "maker_ratio": self.maker_ratio,
            "fill_rate": self.fill_rate,
            "price_range_utilization": self.price_range_utilization,
            "avg_trade_interval_minutes": self.avg_trade_interval_minutes,
            "trades_per_hour": self.trades_per_hour,
            "apr_estimate": self.apr_estimate,
            "leverage": self.leverage,
            "liquidation_price": self.liquidation_price,
            "distance_to_liquidation_pct": self.distance_to_liquidation_pct,
            "margin_ratio": self.margin_ratio,
            "position_direction": self.position_direction,
            "position_size": self.position_size,
            "grid_lower_price": self.grid_lower_price,
            "grid_upper_price": self.grid_upper_price,
            "current_price": self.current_price,
            "investment_amount": self.investment_amount,
            "grid_session_profit": self.grid_session_profit,
            "running_hours": self.running_hours,
        }
