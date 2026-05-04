"""Telegram bot application setup."""

from telegram.ext import Application, CommandHandler, MessageHandler, filters

from config.settings import Settings
from src.gridbot.ai.gemini import GeminiAnalyzer
from src.gridbot.binance.client import BinanceFuturesClient
from src.gridbot.storage.database import Database
from src.gridbot.storage.repositories import (
    FuturesTradeRepository,
    GridSessionRepository,
    IncomeRepository,
)
from src.gridbot.telegram.handlers import (
    cmd_ask,
    cmd_help,
    cmd_pause,
    cmd_pnl,
    cmd_recommend,
    cmd_resume,
    cmd_risk,
    cmd_sessions,
    cmd_start,
    cmd_status,
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
    """Build and configure the Telegram Application with all handlers."""
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not configured")

    app = Application.builder().token(settings.telegram_bot_token).build()

    app.bot_data["settings"] = settings
    app.bot_data["binance_client"] = binance_client
    app.bot_data["gemini_analyzer"] = gemini_analyzer
    app.bot_data["db"] = db

    app.bot_data["trade_repo"] = FuturesTradeRepository(db)
    app.bot_data["income_repo"] = IncomeRepository(db)
    app.bot_data["session_repo"] = GridSessionRepository(db)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("pnl", cmd_pnl))
    app.add_handler(CommandHandler("risk", cmd_risk))
    app.add_handler(CommandHandler("recommend", cmd_recommend))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_share_link))

    logger.info("telegram_app_configured", handlers=10)

    return app
