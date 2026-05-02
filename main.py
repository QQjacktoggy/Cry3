"""Binance Futures Grid Bot Monitor — Entry Point.

Usage:
    python main.py
"""

import sys
import os

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from config.settings import Settings
from src.gridbot.core.app import App
from src.gridbot.utils.logging import get_logger

logger = get_logger(__name__)


def main() -> None:
    """Start the Grid Bot Monitor."""
    settings = Settings()

    logger.info(
        "app_starting",
        symbols=settings.symbols_list,
        strategy=settings.active_strategy_name,
        fetch_interval=settings.fetch_interval_minutes,
        testnet=settings.binance_testnet,
        has_gemini=bool(settings.gemini_api_key),
        has_telegram=bool(settings.telegram_bot_token),
    )

    if not settings.telegram_bot_token:
        logger.warning("telegram_not_configured", msg="Bot will run without Telegram. Set TELEGRAM_BOT_TOKEN in .env.")

    app = App(settings)
    app.start()


if __name__ == "__main__":
    main()
