"""Application orchestrator — ties all components together.

Handles initialization, scheduled tasks, and graceful shutdown.
"""

from config.settings import Settings
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
from src.gridbot.ai.gemini import GeminiAnalyzer
from src.gridbot.binance.client import BinanceFuturesClient
from src.gridbot.binance.fetcher import BinanceFetcher
from src.gridbot.binance.models import FuturesTrade, IncomeRecord, MarketSnapshot, PositionInfo
from src.gridbot.core.scheduler import Scheduler
from src.gridbot.grid.analyzer import compute_metrics
from src.gridbot.grid.models import GridMetrics
from src.gridbot.mainnet.one_run import MainnetOneRunManager
from src.gridbot.mainnet.v1459_app_runtime_v3 import build_v1459_app_runtime_v3
from src.gridbot.mainnet.v1459_readonly_identity_client import V1459ReadOnlyIdentityClient
from src.gridbot.mainnet.v1469_paid_execution_adapter import (
    V1469PaidExecutionAdapter,
)
from src.gridbot.storage.database import Database
from src.gridbot.storage.v1464_promotion_repository import (
    V1464PromotionRepository,
)
from src.gridbot.storage.v1465_w6a_profile_repository import (
    V1465W6AProfileRepository,
)
from src.gridbot.storage.v1469_arm_observation_repository import (
    V1469ArmObservationRepository,
)
from src.gridbot.storage.v1469_lease_repository import V1469LeaseRepository
from src.gridbot.storage.v1469_paid_execution_claim_repository import (
    V1469PaidExecutionClaimRepository,
)
from src.gridbot.storage.v1469_risk_event_repository import (
    V1469RiskEventRepository,
)
from src.gridbot.storage.repositories import (
    AuditLogRepository,
    ConfigRepository,
    FuturesTradeRepository,
    GridSessionRepository,
    IncomeRepository,
    MarketSnapshotRepository,
    MainnetRunRepository,
    PerformanceRepository,
    RecommendationRepository,
)
from src.gridbot.telegram.bot import build_telegram_app, sync_command_menu
from src.gridbot.telegram.formatters import (
    format_full_report,
    format_recommendation,
    format_testnet_daily_report,
)
from src.gridbot.testnet.auto_trader import TestnetAutoTrader
from src.gridbot.utils.logging import get_logger

logger = get_logger(__name__)


class App:
    """Main application orchestrator."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings.db_path)
        # Telegram Lane Monitor reads large immutable evidence envelopes.  A
        # dedicated connection keeps those reads out of the latency-critical
        # mainnet run-cycle queue.
        self.lane_monitor_db = Database(settings.db_path)
        self.binance = BinanceFuturesClient(settings)
        self.mainnet_settings = settings.model_copy(
            update={
                "binance_api_key": settings.mainnet_api_key,
                "binance_api_secret": settings.mainnet_api_secret,
                "binance_testnet": False,
            }
        )
        self.mainnet_binance = BinanceFuturesClient(self.mainnet_settings)
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
        self.mainnet_run_repo = MainnetRunRepository(self.db)
        self.config_repo = ConfigRepository(self.db)
        self.v1464_promotion_repo = V1464PromotionRepository(self.db)
        self.v1465_w6a_profile_repo = V1465W6AProfileRepository(self.db)
        self.v1469_arm_observation_repo = V1469ArmObservationRepository(self.db)
        self.v1469_lease_repo = V1469LeaseRepository(self.db)
        self.v1469_paid_claim_repo = V1469PaidExecutionClaimRepository(self.db)
        self.v1469_risk_event_repo = V1469RiskEventRepository(self.db)
        self.v1469_paid_execution_adapter = V1469PaidExecutionAdapter(
            self.v1469_paid_claim_repo
        )
        self.v1469_arm_observation_ready = False
        self.v1469_authority_ready = False

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
        self.mainnet_one_run_manager = None
        self.adaptive_evidence_repo = None
        self.adaptive_result_repo = None
        self.v1459_observation_runtime = None

    async def initialize(self) -> None:
        """Initialize all components."""
        # v1.4.64: validate the paid Codex boundary before opening the database
        # or making any exchange connection.  A missing/mistyped live flag must
        # stop startup instead of silently falling back to a legacy paid path.
        self.settings.assert_mainnet_v1463_runtime_safety()
        await self.db.initialize()
        if (
            self.settings.telegram_bot_token
            and getattr(self, "lane_monitor_db", None) is not None
        ):
            await self.lane_monitor_db.initialize()
        if bool(self.settings.mainnet_codex_v1464_auto_promotion_enabled):
            try:
                fingerprint = (
                    await self.v1464_promotion_repo.assert_schema_ready()
                )
            except Exception as exc:
                raise RuntimeError(
                    "unsafe v1.4.64 promotion database schema: "
                    f"{type(exc).__name__}:{str(exc)[:500]}"
                ) from exc
            logger.info(
                "v1464_promotion_schema_ready",
                fingerprint=fingerprint,
            )
        if bool(
            self.settings.mainnet_codex_v1465_w6a_profile_shadow_enabled
            or self.settings.mainnet_codex_v1465_w6a_profile_selector_enabled
            or self.settings.mainnet_codex_v1465_w6a_profile_enforcement_enabled
        ):
            try:
                fingerprint = (
                    await self.v1465_w6a_profile_repo.assert_schema_ready()
                )
            except Exception as exc:
                raise RuntimeError(
                    "unsafe v1.4.65 W6A profile database schema: "
                    f"{type(exc).__name__}:{str(exc)[:500]}"
                ) from exc
            logger.info(
                "v1465_w6a_profile_schema_ready",
                fingerprint=fingerprint,
            )
        if bool(self.settings.mainnet_codex_v1469_observation_enabled):
            try:
                bucket_seconds = int(
                    self.settings.mainnet_codex_v1469_observation_bucket_seconds
                )
                if bucket_seconds != 30:
                    raise ValueError("observation bucket must be exactly 30 seconds")
                fingerprint = (
                    await self.v1469_arm_observation_repo.assert_schema_ready()
                )
            except Exception as exc:
                self.v1469_arm_observation_ready = False
                raise RuntimeError(
                    "unsafe v1.4.69 observation configuration/schema: "
                    f"{type(exc).__name__}:{str(exc)[:500]}"
                ) from exc
            else:
                self.v1469_arm_observation_ready = True
                logger.info(
                    "v1469_observation_schema_ready",
                    fingerprint=fingerprint,
                    bucket_seconds=bucket_seconds,
                )
        v1469_authority_enabled = bool(
            self.settings.mainnet_codex_v1469_arbiter_enabled
            or self.settings.mainnet_codex_v1469_live_enforcement_enabled
        )
        if v1469_authority_enabled:
            # This entire readiness boundary deliberately precedes both
            # Binance connect calls.  Each repository validates the concrete
            # schema it owns (migrations 016 through 020); a migration marker
            # alone is not treated as proof of readiness.
            try:
                if int(
                    self.settings.mainnet_codex_v1469_observation_bucket_seconds
                ) != 30:
                    raise ValueError("observation bucket must be exactly 30 seconds")
                exact_runtime = {
                    "mainnet_codex_v1469_safety_window_seconds": 15 * 60,
                    "mainnet_codex_v1469_authority_window_seconds": 45 * 60,
                    "mainnet_codex_v1469_guard_window_seconds": 180 * 60,
                    "mainnet_codex_v1469_regime_max_age_seconds": 60,
                    "mainnet_codex_v1469_submit_max_age_seconds": 10,
                    "mainnet_codex_v1469_probation_lease_seconds": 5 * 60,
                    "mainnet_codex_v1469_live_lease_seconds": 10 * 60,
                }
                mismatched = {
                    name: getattr(self.settings, name, None)
                    for name, expected in exact_runtime.items()
                    if int(getattr(self.settings, name, -1)) != expected
                }
                if mismatched:
                    raise ValueError(
                        f"v1.4.69 exact runtime settings mismatch: {mismatched}"
                    )
                await self.v1469_arm_observation_repo.assert_schema_ready()
                await self.v1469_lease_repo.assert_schema_ready()
                await self.v1469_risk_event_repo.assert_schema_ready()
                await self.v1469_paid_claim_repo.assert_schema_ready()
            except Exception as exc:
                self.v1469_authority_ready = False
                raise RuntimeError(
                    "unsafe v1.4.69 authority configuration/schema: "
                    f"{type(exc).__name__}:{str(exc)[:500]}"
                ) from exc
            self.v1469_authority_ready = True
            logger.info("v1469_authority_schema_ready", bucket_seconds=30)
        await self.binance.connect()
        if self.settings.mainnet_one_run_enabled and self.settings.mainnet_api_key:
            await self.mainnet_binance.connect()
        logger.info("app_initialized")

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        self.scheduler.shutdown()
        manager = getattr(self, "mainnet_one_run_manager", None)
        if manager is not None:
            try:
                await manager.shutdown_v1469_observation_writer()
            except Exception as exc:  # observation must not block shutdown
                logger.warning(
                    "v1469_observation_shutdown_failed",
                    error=str(exc)[:300],
                )
        await self.binance.close()
        await self.mainnet_binance.close()
        lane_monitor_db = getattr(self, "lane_monitor_db", None)
        if lane_monitor_db is not None:
            await lane_monitor_db.close()
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

    async def send_testnet_daily_report(self) -> None:
        """Send scheduled daily testnet P&L report via Telegram."""
        if not self.settings.testnet_legacy_enabled:
            logger.info("testnet_daily_report_skipped", reason="legacy_testnet_disabled")
            return
        if not self.telegram_app or not self.settings.telegram_chat_id_int:
            logger.info("testnet_daily_report_skipped", reason="telegram_not_configured")
            return
        try:
            report_tz = ZoneInfo(self.settings.testnet_daily_report_timezone)
            local_today = datetime.now(report_tz).date()
            day_start = datetime.combine(local_today, time.min, tzinfo=report_tz)
            day_start_ms = int(day_start.astimezone(timezone.utc).timestamp() * 1000)

            positions: dict[str, PositionInfo | None] = {}
            open_algo_orders: dict[str, list[dict]] = {}
            today_income: dict[str, list[IncomeRecord]] = {}

            for symbol in self.settings.symbols_list:
                positions[symbol] = await self.binance.get_position(symbol)
                open_algo_orders[symbol] = await self.binance.get_open_algo_orders(symbol)
                records: list[IncomeRecord] = []
                for income_type in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
                    records.extend(
                        await self.binance.get_income_history(
                            income_type=income_type,
                            symbol=symbol,
                            start_time=day_start_ms,
                            limit=1000,
                        )
                    )
                today_income[symbol] = records

            report = format_testnet_daily_report(
                settings=self.settings,
                positions=positions,
                open_algo_orders=open_algo_orders,
                today_income=today_income,
                report_timezone=self.settings.testnet_daily_report_timezone,
            )
            await self.telegram_app.bot.send_message(
                chat_id=self.settings.telegram_chat_id_int,
                text=report,
                parse_mode="HTML",
            )
            logger.info("testnet_daily_report_sent", timezone=self.settings.testnet_daily_report_timezone)
        except Exception as exc:
            logger.error("testnet_daily_report_failed", error=str(exc))

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
                self.telegram_app.bot_data["lane_monitor_db"] = (
                    self.lane_monitor_db
                )
                self.telegram_app.bot_data["scheduler"] = self.scheduler
            else:
                logger.warning("telegram_disabled", msg="TELEGRAM_BOT_TOKEN not configured")

            self.testnet_auto_trader = TestnetAutoTrader(
                settings=self.settings,
                client=self.binance,
                telegram_app=self.telegram_app,
            )
            if self.telegram_app:
                self.telegram_app.bot_data["trader"] = self.testnet_auto_trader

            v1459 = await build_v1459_app_runtime_v3(
                settings=self.settings,
                db=self.db,
                read_only_identity_client=V1459ReadOnlyIdentityClient(self.mainnet_binance),
                code_version="v1.4.59-continuation-observation-v2",
            )
            self.adaptive_evidence_repo = v1459.composition.evidence_repository
            self.adaptive_result_repo = v1459.composition.result_repository
            self.v1459_observation_runtime = v1459.runtime

            from src.gridbot.mainnet.v1459_cohort_tracking import V1459CohortTracker

            self.v1459_cohort_tracker = V1459CohortTracker(
                db=self.db,
                config_repo=self.config_repo,
            )

            self.mainnet_one_run_manager = MainnetOneRunManager(
                settings=self.settings,
                client=self.mainnet_binance,
                repo=self.mainnet_run_repo,
                trade_repo=self.trade_repo,
                telegram_app=self.telegram_app,
                config_repo=self.config_repo,
                observation_runtime=self.v1459_observation_runtime,
                cohort_tracker=self.v1459_cohort_tracker,
                promotion_repo=self.v1464_promotion_repo,
                w6a_profile_repo=self.v1465_w6a_profile_repo,
                arm_observation_repo=(
                    self.v1469_arm_observation_repo
                    if self.v1469_arm_observation_ready
                    else None
                ),
                v1469_lease_repo=(
                    self.v1469_lease_repo if self.v1469_authority_ready else None
                ),
                v1469_paid_claim_repo=(
                    self.v1469_paid_claim_repo
                    if self.v1469_authority_ready
                    else None
                ),
                v1469_risk_event_repo=(
                    self.v1469_risk_event_repo
                    if self.v1469_authority_ready
                    else None
                ),
                v1469_paid_execution_adapter=(
                    self.v1469_paid_execution_adapter
                    if self.v1469_authority_ready
                    else None
                ),
            )
            if self.telegram_app:
                self.telegram_app.bot_data["mainnet_one_run_manager"] = self.mainnet_one_run_manager
                self.telegram_app.bot_data["config_repo"] = self.config_repo

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
            if self.settings.testnet_legacy_enabled and self.settings.trading_mode == "testnet_live":
                self.scheduler.add_testnet_trade_job(
                    self.testnet_auto_trader.run_entry_cycle,
                    interval_minutes=self.settings.testnet_auto_trade_interval_minutes,
                )
                self.scheduler.add_testnet_manage_job(
                    self.testnet_auto_trader.run_manage_cycle,
                    interval_seconds=self.settings.testnet_manage_interval_seconds,
                )
                if self.settings.testnet_daily_report_enabled:
                    self.scheduler.add_testnet_daily_report_job(
                        self.send_testnet_daily_report,
                        hour=self.settings.testnet_daily_report_hour,
                        minute=self.settings.testnet_daily_report_minute,
                        timezone=self.settings.testnet_daily_report_timezone,
                    )
            if self.settings.mainnet_one_run_enabled:
                self.scheduler.add_mainnet_one_run_job(
                    self.mainnet_one_run_manager.run_cycle,
                    interval_seconds=min(
                        self.settings.mainnet_one_run_entry_scan_interval_seconds,
                        self.settings.mainnet_one_run_manage_interval_seconds,
                    ),
                )
            self.scheduler.start()

            # Run initial fetch
            logger.info("running_initial_fetch")
            await self.run_fetch_cycle()

            # Start Telegram polling
            if self.telegram_app:
                logger.info("starting_telegram_bot")
                await self.telegram_app.initialize()
                # Manual startup does not invoke PTB post_init.
                await sync_command_menu(self.telegram_app)
                await self.telegram_app.start()
                await self.telegram_app.updater.start_polling(drop_pending_updates=True)
            else:
                logger.info("telegram_bot_skipped")

            if self.settings.testnet_legacy_enabled and self.settings.trading_mode == "testnet_live":
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
