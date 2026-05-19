"""APScheduler wrapper for periodic fetch + analysis cycles."""

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.gridbot.utils.logging import get_logger

logger = get_logger(__name__)


class Scheduler:
    """Manages periodic tasks using APScheduler."""

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler()
        self._paused = False

    def add_fetch_job(self, func, interval_minutes: int, **kwargs) -> None:
        """Add a periodic fetch job."""
        self._scheduler.add_job(
            func,
            "interval",
            minutes=interval_minutes,
            id="fetch_cycle",
            replace_existing=True,
            **kwargs,
        )
        logger.info("scheduler_job_added", job="fetch_cycle", interval_minutes=interval_minutes)

    def add_analysis_job(self, func, interval_minutes: int = 60, **kwargs) -> None:
        """Add a periodic AI analysis job."""
        self._scheduler.add_job(
            func,
            "interval",
            minutes=interval_minutes,
            id="analysis_cycle",
            replace_existing=True,
            **kwargs,
        )
        logger.info("scheduler_job_added", job="analysis_cycle", interval_minutes=interval_minutes)

    def add_testnet_trade_job(self, func, interval_minutes: int, **kwargs) -> None:
        """Add a periodic testnet strategy execution job."""
        self._scheduler.add_job(
            func,
            "interval",
            minutes=interval_minutes,
            id="testnet_trade_cycle",
            replace_existing=True,
            **kwargs,
        )
        logger.info("scheduler_job_added", job="testnet_trade_cycle", interval_minutes=interval_minutes)

    def start(self) -> None:
        self._scheduler.start()
        logger.info("scheduler_started")

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)
        logger.info("scheduler_stopped")

    def pause(self) -> None:
        self._scheduler.pause()
        self._paused = True
        logger.info("scheduler_paused")

    def resume(self) -> None:
        self._scheduler.resume()
        self._paused = False
        logger.info("scheduler_resumed")

    @property
    def is_paused(self) -> bool:
        return self._paused
