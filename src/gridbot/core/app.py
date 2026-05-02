"""Application orchestrator — ties all components together.

Handles initialization, scheduled tasks, and graceful shutdown.
"""

from config.settings import Settings
from src.gridbot.ai.gemini import GeminiAnalyzer
from src.gridbot.binance.client import BinanceFuturesClient
from src.gridbot.binance.fetcher import BinanceFetcher
from src.gridbot.binance.models import IncomeRecord, MarketSnapshot, PositionInfo
from src.gridbot.core.scheduler import Scheduler
from src.gridbot.grid.analyzer import compute_metrics
from src.gridbot.grid.models import GridMetrics
from src.gridbot.storage.database import Database
from src.gridbot.storage.repositories import (
    AuditLogRepository,
    FuturesTradeRepository,
    GridSessionRepository,
    IncomeRepository,
    MarketSnapshotRepository,
    PerformanceRepository,
    RecommendationRepository,
)
from src.gridbot.telegram.bot import build_telegram_app
from src.gridbot.telegram.formatters import format_full_report, format_recommendation
from src.gridbot.utils.logging import get_logger

logger = get_logger(__name__)


class App:
    """Main application orchestrator."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings.db_path)
        self.binance = BinanceFuturesClient(settings)
        self.gemini = GeminiAnalyzer(settings)
        self.scheduler = Scheduler()

        # Repositories
        self.trade_repo = FuturesTradeRepository(self.db)
        self.income_repo = IncomeRepository(self.db)
        self.session_repo = GridSessionRepository(self.db)
        self.market_repo = MarketSnapshotRepository(self.db)
        self.perf_repo = PerformanceRepository(self.db)
        self.rec_repo = RecommendationRepository(self.db)
        self.audit_repo = AuditLogRepository(self.db)

        # Fetcher
        self.fetcher = BinanceFetcher(
            client=self.binance,
            trade_repo=self.trade_repo,
            income_repo=self.income_repo,
            session_repo=self.session_repo,
            market_repo=self.market_repo,
            audit_repo=self.audit_repo,
        )

        self.telegram_app = None

    async def initialize(self) -> None:
        """Initialize all components."""
        await self.db.initialize()
        await self.binance.connect()
        logger.info("app_initialized")

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        self.scheduler.shutdown()
        await self.binance.close()
        await self.db.close()
        logger.info("app_shutdown")

    async def run_fetch_cycle(self) -> None:
        """Execute a single fetch cycle for all symbols."""
        try:
            results = await self.fetcher.fetch_all_symbols(self.settings.symbols_list)

            # Send report via Telegram if configured
            if self.telegram_app and self.settings.telegram_chat_id_int:
                await self._send_telegram_report(results)

            logger.info("fetch_cycle_done", symbols=len(results))

        except Exception as exc:
            logger.error("fetch_cycle_error", error=str(exc))

    async def run_analysis_cycle(self) -> None:
        """Execute a single AI analysis cycle."""
        try:
            if not self.settings.gemini_api_key:
                logger.warning("gemini_not_configured")
                return

            all_metrics, all_markets, all_positions, all_funding = await self._collect_analysis_data()

            account = await self.binance.get_account_info()

            rec = await self.gemini.analyze(
                metrics=all_metrics,
                markets=all_markets,
                positions=all_positions,
                funding_rates=all_funding,
                current_strategy=self.settings.active_strategy_name,
                account_balance=account.total_margin_balance,
                margin_ratio=account.margin_ratio,
            )

            # Save recommendation
            await self.rec_repo.save({
                "symbol": ",".join(self.settings.symbols_list),
                "recommended_strategy": rec.recommended_strategy,
                "confidence": rec.confidence,
                "parameter_adjustments": [a.model_dump() for a in rec.parameter_adjustments],
                "market_summary": rec.market_condition_summary,
                "reasoning": rec.reasoning,
                "risk_warnings": rec.risk_warnings,
                "trigger": "scheduled",
            })

            # Send via Telegram
            if self.telegram_app and self.settings.telegram_chat_id_int:
                report = format_recommendation(rec, self.settings.active_strategy_name)
                await self.telegram_app.bot.send_message(
                    chat_id=self.settings.telegram_chat_id_int,
                    text=report,
                    parse_mode="HTML",
                )

            logger.info("analysis_cycle_done", strategy=rec.recommended_strategy)

        except Exception as exc:
            logger.error("analysis_cycle_error", error=str(exc))

    async def _collect_analysis_data(self) -> tuple[
        dict[str, GridMetrics],
        dict[str, MarketSnapshot],
        dict[str, PositionInfo | None],
        dict[str, list[dict]],
    ]:
        """Collect all data needed for AI analysis."""
        all_metrics: dict[str, GridMetrics] = {}
        all_markets: dict[str, MarketSnapshot] = {}
        all_positions: dict[str, PositionInfo | None] = {}
        all_funding: dict[str, list[dict]] = {}

        for symbol in self.settings.symbols_list:
            result = await self.binance.fetch_symbol_data(symbol)
            income_records = [
                IncomeRecord.from_api({
                    "tranId": r["tran_id"], "symbol": r.get("symbol", ""),
                    "incomeType": r["income_type"], "income": str(r["income"]),
                    "asset": r["asset"], "time": r["time_ms"],
                    "info": r.get("info", ""), "tradeId": r.get("trade_id", ""),
                })
                for r in await self.income_repo.get_records(
                    symbol=symbol, grid_only=True
                )
            ]

            # Per-symbol session lookup — prevents cross-symbol contamination
            active_session = await self.session_repo.get_active_session(symbol=symbol)
            session_invested = active_session["invested_amount"] if active_session else None
            session_start = active_session["created_at_ms"] if active_session else None

            all_metrics[symbol] = compute_metrics(
                result,
                income_records=income_records if income_records else None,
                session_invested=session_invested,
                session_start_ms=session_start,
            )
            all_markets[symbol] = result.market
            all_positions[symbol] = result.position
            all_funding[symbol] = await self.binance.get_funding_rate_history(symbol, limit=10)

        return all_metrics, all_markets, all_positions, all_funding

    async def _send_telegram_report(self, results: dict) -> None:
        """Send periodic status report via Telegram."""
        try:
            all_metrics: dict[str, GridMetrics] = {}
            all_markets: dict[str, MarketSnapshot] = {}
            all_positions: dict[str, PositionInfo | None] = {}

            for symbol, result in results.items():
                all_metrics[symbol] = compute_metrics(result)
                all_markets[symbol] = result.market
                all_positions[symbol] = result.position

            account = await self.binance.get_account_info()
            report = format_full_report(
                metrics=all_metrics,
                markets=all_markets,
                positions=all_positions,
                account_balance=account.total_margin_balance,
                margin_ratio=account.margin_ratio,
            )

            await self.telegram_app.bot.send_message(
                chat_id=self.settings.telegram_chat_id_int,
                text=report,
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.error("telegram_report_failed", error=str(exc))

    def start(self) -> None:
        """Start the application with Telegram bot and scheduler."""
        import asyncio

        async def _run():
            await self.initialize()

            # Build Telegram app
            self.telegram_app = build_telegram_app(
                settings=self.settings,
                binance_client=self.binance,
                gemini_analyzer=self.gemini,
                db=self.db,
            )
            self.telegram_app.bot_data["scheduler"] = self.scheduler

            # Schedule periodic tasks
            self.scheduler.add_fetch_job(
                self.run_fetch_cycle,
                interval_minutes=self.settings.fetch_interval_minutes,
            )
            if self.settings.gemini_api_key:
                self.scheduler.add_analysis_job(
                    self.run_analysis_cycle,
                    interval_minutes=60,  # analyze hourly
                )
            self.scheduler.start()

            # Run initial fetch
            logger.info("running_initial_fetch")
            await self.run_fetch_cycle()

            # Start Telegram polling
            logger.info("starting_telegram_bot")
            await self.telegram_app.initialize()
            await self.telegram_app.start()
            await self.telegram_app.updater.start_polling(drop_pending_updates=True)

            # Keep running
            try:
                await asyncio.Event().wait()
            except (KeyboardInterrupt, SystemExit):
                pass
            finally:
                await self.telegram_app.updater.stop()
                await self.telegram_app.stop()
                await self.telegram_app.shutdown()
                await self.shutdown()

        asyncio.run(_run())
