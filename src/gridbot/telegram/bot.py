"""Telegram bot application setup.

Registers all command handlers and builds the Application.
"""

from telegram import BotCommand, BotCommandScopeAllPrivateChats
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

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
    MainnetRunRepository,
    PerformanceRepository,
    RecommendationRepository,
)
from src.gridbot.telegram.handlers import (
    cmd_ai,
    cmd_help,
    cmd_lanes,
    cmd_mainnet,
    cmd_pause,
    cmd_pnl,
    cmd_rescue,
    cmd_resume,
    cmd_signal,
    cmd_start,
    cmd_testnet,
    handle_mainnet_callback,
    handle_manual_signal_callback,
    handle_rescue_callback,
    handle_text_message,
    handle_unknown_command,
)
from src.gridbot.telegram.adaptive_status import cmd_status
from src.gridbot.telegram.clean_commands import cmd_help as clean_cmd_help, cmd_start as clean_cmd_start
from src.gridbot.utils.logging import get_logger

logger = get_logger(__name__)


TELEGRAM_MENU_COMMANDS = [
    BotCommand("status", "查看 Adaptive 目前 run 與狀態"),
    BotCommand("lanes", "查看全部 legacy lane 狀態"),
    BotCommand("mainnet", "管理 mainnet Adaptive run"),
    BotCommand("pnl", "查看 mainnet 今日 PnL"),
    BotCommand("help", "顯示可用命令"),
]


async def sync_command_menu(application: Application) -> None:
    """Replace stale default/private-chat menus with the current minimal menu."""
    try:
        await application.bot.set_my_commands(TELEGRAM_MENU_COMMANDS)
        await application.bot.set_my_commands(
            TELEGRAM_MENU_COMMANDS,
            scope=BotCommandScopeAllPrivateChats(),
        )
        logger.info("telegram_bot_commands_set_success", count=len(TELEGRAM_MENU_COMMANDS))
    except Exception as e:
        logger.error("telegram_bot_commands_set_failed", error=str(e))

async def post_init(application: Application) -> None:
    await sync_command_menu(application)

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

    app = Application.builder().token(settings.telegram_bot_token).post_init(post_init).build()

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
    app.bot_data["mainnet_run_repo"] = MainnetRunRepository(db)

    # Register command handlers
    app.add_handler(CommandHandler("start", clean_cmd_start))
    app.add_handler(CommandHandler("help", clean_cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("lanes", cmd_lanes))
    app.add_handler(CommandHandler("testnet", cmd_testnet))
    app.add_handler(CommandHandler("mainnet", cmd_mainnet))
    app.add_handler(CommandHandler("pnl", cmd_pnl))
    app.add_handler(CommandHandler("ai", cmd_ai))
    app.add_handler(CommandHandler("signal", cmd_signal))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("rescue", cmd_rescue))
    app.add_handler(CallbackQueryHandler(handle_mainnet_callback, pattern=r"^mainnet:"))
    app.add_handler(CallbackQueryHandler(handle_rescue_callback, pattern=r"^rescue:"))
    app.add_handler(CallbackQueryHandler(handle_manual_signal_callback, pattern=r"^manual_signal:"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    app.add_handler(MessageHandler(filters.COMMAND, handle_unknown_command))

    logger.info("telegram_app_configured", handlers=14)

    return app
