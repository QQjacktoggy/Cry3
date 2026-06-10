#!/usr/bin/env python3
"""
Cancel the currently active Mainnet One Run.
Usage (on VM, from repo root):
    python -m scripts.cancel_active_run
"""

import asyncio
import sys
import os

# Ensure project root is in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if sys.platform.startswith("win"):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

from dotenv import load_dotenv
load_dotenv()

from config.settings import Settings
from src.gridbot.storage.database import Database
from src.gridbot.binance.client import BinanceFuturesClient
from src.gridbot.storage.repositories import MainnetRunRepository
from src.gridbot.mainnet.one_run import MainnetOneRunManager
from src.gridbot.utils.logging import get_logger

logger = get_logger(__name__)

async def main():
    settings = Settings()
    db = Database(settings.db_path)
    await db.initialize()
    
    # Initialize mainnet settings
    mainnet_settings = settings.model_copy(
        update={
            "binance_api_key": settings.mainnet_api_key,
            "binance_api_secret": settings.mainnet_api_secret,
            "binance_testnet": False,
        }
    )
    
    mainnet_binance = BinanceFuturesClient(mainnet_settings)
    await mainnet_binance.connect()
    
    mainnet_run_repo = MainnetRunRepository(db)
    
    # Check if there is an active run first
    active = await mainnet_run_repo.get_active_run()
    if not active:
        print("❌ 沒有找到 active 的 mainnet run。")
        await mainnet_binance.close()
        await db.close()
        return
        
    print(f"🔍 找到 active run: {active['run_id']} (status: {active['status']})")
    
    # Instantiate manager and cancel
    manager = MainnetOneRunManager(
        settings=settings,
        client=mainnet_binance,
        repo=mainnet_run_repo,
    )
    
    result = await manager.cancel()
    print(result)
    
    await mainnet_binance.close()
    await db.close()

if __name__ == "__main__":
    asyncio.run(main())
