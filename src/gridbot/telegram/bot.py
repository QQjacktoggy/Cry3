"""Telegram bot application setup.

Registers all command handlers and builds the Application.
"""

from telegram.ext import Application, CommandHandler, MessageHandler, filters

from config.settings import Settings
from src.gridbot.ai.gemini import GeminiAnalyzer
from src.gridbot.binance.client import BinanceFuturesClient
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
from src.gridbot.telegram.handlers import (
    cmd_help,
    cmd_pause,
    cmd_recommend,
    cmd_resume,
    cmd_start,
    cmd_testnet,
    handle_share_link,
)
from src.gridbot.utils.logging import get_logger

logger = get_logger(__name__)


def build_telegram_app(
    settings: Settings,
    binance_client: BinanceFuturesClient,
    gemini_analyzer: GeminiAnalyzer,
    db: Database,
) -> Application:
    """Build and configure the Telegram Application with all handlers.

    Shared state (client, repos, analyzer) is stored in bot_data
    so all handlers can access them.
    """
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not configured")

    app = Application.builder().token(settings.telegram_bot_token).build()

    # Store shared state in bot_data
    app.bot_data["settings"] = settings
    app.bot_data["binance_client"] = binance_client
    app.bot_data["gemini_analyzer"] = gemini_analyzer
    app.bot_data["db"] = db

    # Initialize repositories
    app.bot_data["trade_repo"] = FuturesTradeRepository(db)
    app.bot_data["income_repo"] = IncomeRepository(db)
    app.bot_data["session_repo"] = GridSessionRepository(db)
    app.bot_data["market_repo"] = MarketSnapshotRepository(db)
    app.bot_data["perf_repo"] = PerformanceRepository(db)
    app.bot_data["rec_repo"] = RecommendationRepository(db)
    app.bot_data["audit_repo"] = AuditLogRepository(db)

    # Register command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("testnet", cmd_testnet))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("recommend", cmd_recommend))

    # Auto-detect Binance share links in any non-command text message
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_share_link))

    logger.info("telegram_app_configured", handlers=7)

    return app
