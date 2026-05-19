"""Application orchestrator — ties all components together.

Handles initialization, scheduled tasks, and graceful shutdown.
"""

from config.settings import Settings
from src.gridbot.ai.gemini import GeminiAnalyzer
from src.gridbot.binance.client import BinanceFuturesClient
from src.gridbot.binance.fetcher import BinanceFetcher
from src.gridbot.binance.models import FuturesTrade, IncomeRecord, MarketSnapshot, PositionInfo
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
from src.gridbot.testnet.auto_trader import TestnetAutoTrader
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
        self.testnet_auto_trader = None

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

            if self.settings.trading_mode == "legacy_monitor":
                # Notify for any newly closed grid sessions (must run after fetch updates DB)
                await self._notify_closed_sessions()

                # Send periodic status report via Telegram if configured
                if self.telegram_app and self.settings.telegram_chat_id_int:
                    await self._send_telegram_report(results)

            logger.info("fetch_cycle_done", symbols=len(results))

        except Exception as exc:
            logger.error("fetch_cycle_error", error=str(exc))

    async def run_analysis_cycle(self) -> None:
        """Execute a single AI analysis cycle.

        If any symbol has an active session with grid config (from share link),
        runs monitor_grid() for that session. Falls back to general analyze()
        when no configured active sessions exist.
        """
        try:
            if not self.settings.gemini_api_key:
                logger.warning("gemini_not_configured")
                return

            # Prefer monitor_grid for sessions that have config from share link
            monitored_any = False
            for symbol in self.settings.symbols_list:
                active = await self.session_repo.get_active_session(symbol=symbol)
                if active and active.get("lower_price") and active.get("upper_price"):
                    await self._run_grid_monitor(symbol, active)
                    monitored_any = True

            if not monitored_any:
                # No active configured session — run general strategy analysis
                await self._run_general_analysis()

        except Exception as exc:
            logger.error("analysis_cycle_error", error=str(exc))

    async def _run_grid_monitor(self, symbol: str, session: dict) -> None:
        """Run monitor_grid() for one active session and send result via Telegram."""
        try:
            result = await self.binance.fetch_symbol_data(symbol)
            market = result.market

            # Realized P&L scoped to this session only
            income_rows = await self.income_repo.get_records(
                income_type="REALIZED_PNL",
                symbol=symbol,
                since_ms=session["created_at_ms"],
                grid_only=True,
            )
            realized_pnl = sum(float(r["income"]) for r in income_rows)

            funding_history = await self.binance.get_funding_rate_history(symbol, limit=10)
            klines = await self.binance.get_klines(symbol, interval="1h", limit=48)

            report = await self.gemini.monitor_grid(
                symbol=symbol,
                market_price=market.mark_price or market.current_price,
                lower_price=float(session["lower_price"]),
                upper_price=float(session["upper_price"]),
                grid_count=int(session.get("grid_count") or 0),
                grid_type=session.get("grid_type") or "GEO",
                leverage=int(session.get("leverage") or 1),
                direction=session.get("direction") or "NEUTRAL",
                stop_loss_price=float(session["stop_loss_price"]) if session.get("stop_loss_price") else None,
                take_profit_price=float(session["take_profit_price"]) if session.get("take_profit_price") else None,
                invested_amount=float(session["invested_amount"]),
                session_start_ms=int(session["created_at_ms"]),
                realized_pnl=realized_pnl,
                funding_rate=float(market.funding_rate or 0.0),
                funding_history=funding_history,
                klines=klines,
            )

            if self.telegram_app and self.settings.telegram_chat_id_int:
                await self.telegram_app.bot.send_message(
                    chat_id=self.settings.telegram_chat_id_int,
                    text=report,
                )
            logger.info("grid_monitor_done", symbol=symbol, session_id=session.get("create_tran_id"))

        except Exception as exc:
            logger.error("grid_monitor_error", symbol=symbol, error=str(exc))

    async def _run_general_analysis(self) -> None:
        """Fallback analysis using the original analyze() method (no active grid config)."""
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

        if self.telegram_app and self.settings.telegram_chat_id_int:
            report = format_recommendation(rec, self.settings.active_strategy_name)
            await self.telegram_app.bot.send_message(
                chat_id=self.settings.telegram_chat_id_int,
                text=report,
                parse_mode="HTML",
            )
        logger.info("general_analysis_done", strategy=rec.recommended_strategy)

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

            # Load grid-only trades from DB (excludes manual futures trades)
            grid_trade_rows = await self.trade_repo.get_trades(symbol, grid_only=True)
            grid_trades = [FuturesTrade.from_db(r) for r in grid_trade_rows]

            # Per-symbol session lookup — prevents cross-symbol contamination
            active_session = await self.session_repo.get_active_session(symbol=symbol)
            session_invested = active_session["invested_amount"] if active_session else None
            session_start = active_session["created_at_ms"] if active_session else None

            all_metrics[symbol] = compute_metrics(
                result,
                income_records=income_records,
                grid_trades=grid_trades,
                session_invested=session_invested,
                session_start_ms=session_start,
            )
            all_markets[symbol] = result.market
            all_positions[symbol] = result.position
            all_funding[symbol] = await self.binance.get_funding_rate_history(symbol, limit=10)

        return all_metrics, all_markets, all_positions, all_funding

    async def _notify_closed_sessions(self) -> None:
        """Send Telegram notifications for newly closed grid sessions.

        Called after each fetch cycle so users hear about closes promptly.
        Marks each session notified individually so partial failures retry.
        """
        if not self.telegram_app or not self.settings.telegram_chat_id_int:
            return
        try:
            closes = await self.session_repo.get_unnotified_closes()
            for session in closes:
                await self._send_close_notification(session)
                await self.session_repo.mark_close_notified(session["create_tran_id"])
        except Exception as exc:
            logger.error("close_notification_error", error=str(exc))

    async def _send_close_notification(self, session: dict) -> None:
        """Format and send a close notification for one grid session."""
        from datetime import datetime, timezone

        symbol = session.get("symbol") or "未知"
        net_profit = float(session.get("net_profit") or 0)
        pnl_emoji = "🟢" if net_profit >= 0 else "🔴"
        invested = float(session.get("invested_amount") or 0)
        returned = float(session.get("returned_amount") or 0)
        roi_pct = (net_profit / invested * 100) if invested > 0 else 0

        open_str = datetime.fromtimestamp(
            session["created_at_ms"] / 1000, tz=timezone.utc
        ).strftime("%m/%d %H:%M UTC")

        close_str = "N/A"
        duration_h = 0.0
        if session.get("closed_at_ms"):
            close_str = datetime.fromtimestamp(
                session["closed_at_ms"] / 1000, tz=timezone.utc
            ).strftime("%m/%d %H:%M UTC")
            duration_h = (session["closed_at_ms"] - session["created_at_ms"]) / 3_600_000

        lines = [
            f"{pnl_emoji} <b>網格已關閉</b>",
            "",
            f"交易對: <b>{symbol}</b>",
            f"開倉: {open_str}",
            f"關倉: {close_str} (運行 {duration_h:.1f}h)",
            f"投入: ${invested:,.2f} → 回收: ${returned:,.2f}",
            f"淨損益: <b>{pnl_emoji} ${net_profit:+,.4f} ({roi_pct:+.2f}%)</b>",
        ]
        if session.get("lower_price") and session.get("upper_price"):
            gt = "等比" if session.get("grid_type") == "GEO" else "等差"
            gc = session.get("grid_count") or "?"
            lp = session["lower_price"]
            up = session["upper_price"]
            lines.append(f"設定: {gt} {gc}格 ${lp:,.2f}~${up:,.2f}")

        await self.telegram_app.bot.send_message(
            chat_id=self.settings.telegram_chat_id_int,
            text="\n".join(lines),
            parse_mode="HTML",
        )
        logger.info(
            "close_notification_sent",
            symbol=symbol,
            net_profit=net_profit,
            duration_h=round(duration_h, 1),
        )

    async def _send_telegram_report(self, results: dict) -> None:
        """Send periodic status report via Telegram.

        Uses grid-only filtered trades and income from DB to ensure
        scheduled reports exclude manual futures trades.
        """
        try:
            all_metrics: dict[str, GridMetrics] = {}
            all_markets: dict[str, MarketSnapshot] = {}
            all_positions: dict[str, PositionInfo | None] = {}

            for symbol, result in results.items():
                # Load grid-only income from DB
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

                # Load grid-only trades from DB
                grid_trade_rows = await self.trade_repo.get_trades(symbol, grid_only=True)
                grid_trades = [FuturesTrade.from_db(r) for r in grid_trade_rows]

                # Per-symbol session lookup
                active_session = await self.session_repo.get_active_session(symbol=symbol)
                session_invested = active_session["invested_amount"] if active_session else None
                session_start = active_session["created_at_ms"] if active_session else None

                all_metrics[symbol] = compute_metrics(
                    result,
                    income_records=income_records,
                    grid_trades=grid_trades,
                    session_invested=session_invested,
                    session_start_ms=session_start,
                )
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

            # Build Telegram app only when configured. Testnet signal-only runs
            # should still stay alive when Telegram credentials are not present.
            if self.settings.telegram_bot_token:
                self.telegram_app = build_telegram_app(
                    settings=self.settings,
                    binance_client=self.binance,
                    gemini_analyzer=self.gemini,
                    db=self.db,
                )
                self.telegram_app.bot_data["scheduler"] = self.scheduler
            else:
                logger.warning("telegram_disabled", msg="TELEGRAM_BOT_TOKEN not configured")

            self.testnet_auto_trader = TestnetAutoTrader(
                settings=self.settings,
                client=self.binance,
                telegram_app=self.telegram_app,
            )

            # Schedule periodic tasks
            self.scheduler.add_fetch_job(
                self.run_fetch_cycle,
                interval_minutes=self.settings.fetch_interval_minutes,
            )
            if self.settings.gemini_api_key and self.settings.trading_mode == "legacy_monitor":
                self.scheduler.add_analysis_job(
                    self.run_analysis_cycle,
                    interval_minutes=30,  # monitor every 30 min
                )
            if self.settings.trading_mode == "testnet_live":
                self.scheduler.add_testnet_trade_job(
                    self.testnet_auto_trader.run_cycle,
                    interval_minutes=self.settings.testnet_auto_trade_interval_minutes,
                )
            self.scheduler.start()

            # Run initial fetch
            logger.info("running_initial_fetch")
            await self.run_fetch_cycle()

            # Start Telegram polling
            if self.telegram_app:
                logger.info("starting_telegram_bot")
                await self.telegram_app.initialize()
                await self.telegram_app.start()
                await self.telegram_app.updater.start_polling(drop_pending_updates=True)
            else:
                logger.info("telegram_bot_skipped")

            if self.settings.trading_mode == "testnet_live":
                logger.info("running_initial_testnet_trade_cycle")
                await self.testnet_auto_trader.run_cycle()

            # Keep running
            try:
                await asyncio.Event().wait()
            except (KeyboardInterrupt, SystemExit):
                pass
            finally:
                if self.telegram_app:
                    await self.telegram_app.updater.stop()
                    await self.telegram_app.stop()
                    await self.telegram_app.shutdown()
                await self.shutdown()

        asyncio.run(_run())
