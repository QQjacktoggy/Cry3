"""Binance Futures API async client.

Uses python-binance's built-in FAPI methods for standard endpoints
and aiohttp with HMAC signing for SAPI fallback (if ever needed).
All endpoints verified against live Binance API (2026-05-02).
"""

from binance import AsyncClient, BinanceAPIException

from config.settings import Settings
from src.gridbot.binance.models import (
    AccountInfo,
    FetchResult,
    FuturesTrade,
    IncomeRecord,
    MarketSnapshot,
    PositionInfo,
)
from src.gridbot.utils.logging import get_logger
from src.gridbot.utils.retry import async_retry

logger = get_logger(__name__)

# Grid bot orders have this clientOrderId prefix
GRID_ORDER_PREFIX = "aos_"


def is_grid_order(client_order_id: str) -> bool:
    """Check if an order was placed by the grid bot strategy."""
    return client_order_id.startswith(GRID_ORDER_PREFIX)


class BinanceFuturesClient:
    """Async client for Binance USD-M Futures API (FAPI endpoints)."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: AsyncClient | None = None

    async def connect(self) -> None:
        self._client = await AsyncClient.create(
            api_key=self._settings.binance_api_key,
            api_secret=self._settings.binance_api_secret,
            testnet=self._settings.binance_testnet,
        )
        logger.info("binance_connected", testnet=self._settings.binance_testnet)

    async def close(self) -> None:
        if self._client:
            await self._client.close_connection()
            self._client = None

    @property
    def client(self) -> AsyncClient:
        if self._client is None:
            raise RuntimeError("Binance client not connected. Call connect() first.")
        return self._client

    # ── Market Data ──────────────────────────────────────────────────

    @async_retry(max_attempts=3, base_delay=1.0, exceptions=(Exception,))
    async def get_ticker_24hr(self, symbol: str) -> dict:
        """GET /fapi/v1/ticker/24hr"""
        tickers = await self.client.futures_ticker(symbol=symbol)
        if isinstance(tickers, list):
            return tickers[0] if tickers else {}
        return tickers

    @async_retry(max_attempts=3, base_delay=1.0, exceptions=(Exception,))
    async def get_klines(self, symbol: str, interval: str = "1h", limit: int = 24) -> list[list]:
        """GET /fapi/v1/klines"""
        return await self.client.futures_klines(symbol=symbol, interval=interval, limit=limit)

    @async_retry(max_attempts=3, base_delay=1.0, exceptions=(Exception,))
    async def get_mark_price(self, symbol: str) -> dict:
        """GET /fapi/v1/markPrice — includes lastFundingRate and nextFundingTime"""
        return await self.client.futures_mark_price(symbol=symbol)

    @async_retry(max_attempts=3, base_delay=1.0, exceptions=(Exception,))
    async def get_funding_rate_history(self, symbol: str, limit: int = 10) -> list[dict]:
        """GET /fapi/v1/fundingRate — historical funding rates"""
        return await self.client.futures_funding_rate(symbol=symbol, limit=limit)

    # ── Account & Position ───────────────────────────────────────────

    @async_retry(max_attempts=3, base_delay=2.0, exceptions=(Exception,))
    async def get_account_info(self) -> AccountInfo:
        """GET /fapi/v2/account — margin balances and risk info"""
        data = await self.client.futures_account()
        return AccountInfo.from_api(data)

    @async_retry(max_attempts=3, base_delay=1.0, exceptions=(Exception,))
    async def get_position(self, symbol: str) -> PositionInfo | None:
        """GET /fapi/v2/positionRisk — current position for a symbol"""
        positions = await self.client.futures_position_information(symbol=symbol)
        for p in positions:
            if float(p.get("positionAmt", 0)) != 0:
                return PositionInfo.from_api(p)
        return None

    @async_retry(max_attempts=3, base_delay=1.0, exceptions=(Exception,))
    async def get_commission_rate(self, symbol: str) -> dict:
        """GET /fapi/v1/commissionRate"""
        return await self.client.futures_commission_rate(symbol=symbol)

    # ── Trades & Orders ──────────────────────────────────────────────

    @async_retry(max_attempts=3, base_delay=2.0, exceptions=(BinanceAPIException, Exception))
    async def get_user_trades(
        self,
        symbol: str,
        start_time: int | None = None,
        limit: int = 500,
    ) -> list[FuturesTrade]:
        """GET /fapi/v1/userTrades — trade history for a symbol.

        Returns trades sorted by time ascending.
        """
        params: dict = {"symbol": symbol, "limit": limit}
        if start_time:
            params["startTime"] = start_time

        raw_trades = await self.client.futures_account_trades(**params)
        return [FuturesTrade.from_api(t) for t in raw_trades]

    @async_retry(max_attempts=3, base_delay=2.0, exceptions=(BinanceAPIException, Exception))
    async def get_all_orders(
        self,
        symbol: str,
        start_time: int | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """GET /fapi/v1/allOrders — order history (for clientOrderId filtering)."""
        params: dict = {"symbol": symbol, "limit": limit}
        if start_time:
            params["startTime"] = start_time
        return await self.client.futures_get_all_orders(**params)

    @async_retry(max_attempts=3, base_delay=2.0, exceptions=(BinanceAPIException, Exception))
    async def get_open_orders(self, symbol: str) -> list[dict]:
        """GET /fapi/v1/openOrders — currently open orders."""
        return await self.client.futures_get_open_orders(symbol=symbol)

    # ── Income History ───────────────────────────────────────────────

    @async_retry(max_attempts=3, base_delay=2.0, exceptions=(BinanceAPIException, Exception))
    async def get_income_history(
        self,
        income_type: str | None = None,
        symbol: str | None = None,
        start_time: int | None = None,
        end_time: int | None = None,
        limit: int = 100,
    ) -> list[IncomeRecord]:
        """GET /fapi/v1/income — income/loss records.

        income_type: REALIZED_PNL, COMMISSION, FUNDING_FEE,
                     STRATEGY_UMFUTURES_TRANSFER, TRANSFER, etc.
        """
        params: dict = {"limit": limit}
        if income_type:
            params["incomeType"] = income_type
        if symbol:
            params["symbol"] = symbol
        if start_time:
            params["startTime"] = start_time
        if end_time:
            params["endTime"] = end_time

        raw = await self.client.futures_income_history(**params)
        return [IncomeRecord.from_api(r) for r in raw]

    async def get_all_income_since(
        self,
        since_ms: int,
        income_type: str | None = None,
    ) -> list[IncomeRecord]:
        """Paginate through income records since a given timestamp."""
        all_records: list[IncomeRecord] = []
        start = since_ms

        while True:
            batch = await self.get_income_history(
                income_type=income_type,
                start_time=start,
                limit=1000,
            )
            if not batch:
                break
            all_records.extend(batch)
            if len(batch) < 1000:
                break
            # Move start past the last record
            start = batch[-1].time_ms + 1

        return all_records

    # ── Composite Fetchers ───────────────────────────────────────────

    async def fetch_market_snapshot(self, symbol: str) -> MarketSnapshot:
        """Build a complete market snapshot for a symbol."""
        ticker = await self.get_ticker_24hr(symbol)
        klines = await self.get_klines(symbol, interval="1h", limit=24)
        mark_data = await self.get_mark_price(symbol)

        funding_rate = None
        next_funding_time = None
        index_price = None
        mark_price = None

        if mark_data:
            funding_rate = float(mark_data.get("lastFundingRate", 0))
            next_funding_time = mark_data.get("nextFundingTime")
            mark_price = float(mark_data.get("markPrice", 0))
            index_price = float(mark_data.get("indexPrice", 0)) if mark_data.get("indexPrice") else None

        return MarketSnapshot(
            symbol=symbol,
            current_price=float(ticker.get("lastPrice", 0)),
            high_24h=float(ticker.get("highPrice", 0)),
            low_24h=float(ticker.get("lowPrice", 0)),
            volume_24h=float(ticker.get("quoteVolume", 0)),
            price_change_pct_24h=float(ticker.get("priceChangePercent", 0)),
            funding_rate=funding_rate,
            next_funding_time_ms=next_funding_time,
            mark_price=mark_price,
            index_price=index_price,
            klines=klines,
        )

    async def fetch_symbol_data(
        self,
        symbol: str,
        trades_since_ms: int | None = None,
        income_since_ms: int | None = None,
    ) -> FetchResult:
        """Fetch all data for a single symbol in one cycle."""
        # Trades
        trades = await self.get_user_trades(symbol, start_time=trades_since_ms)

        # Income (all types for this symbol)
        income: list[IncomeRecord] = []
        if income_since_ms:
            for itype in ["REALIZED_PNL", "COMMISSION", "FUNDING_FEE"]:
                batch = await self.get_income_history(
                    income_type=itype,
                    symbol=symbol,
                    start_time=income_since_ms,
                    limit=500,
                )
                income.extend(batch)
        else:
            for itype in ["REALIZED_PNL", "COMMISSION", "FUNDING_FEE"]:
                batch = await self.get_income_history(
                    income_type=itype,
                    symbol=symbol,
                    limit=100,
                )
                income.extend(batch)

        # Market snapshot
        market = await self.fetch_market_snapshot(symbol)

        # Position
        position = await self.get_position(symbol)

        # Account
        account = await self.get_account_info()

        logger.info(
            "symbol_data_fetched",
            symbol=symbol,
            trades=len(trades),
            income_records=len(income),
            has_position=position is not None,
            price=market.current_price,
        )

        return FetchResult(
            symbol=symbol,
            trades=trades,
            income_records=income,
            market=market,
            position=position,
            account=account,
        )
