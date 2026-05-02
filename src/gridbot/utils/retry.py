import asyncio
import functools
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from src.gridbot.utils.logging import get_logger

T = TypeVar("T")
logger = get_logger(__name__)


def async_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[Callable[..., Coroutine[Any, Any, T]]], Callable[..., Coroutine[Any, Any, T]]]:
    def decorator(func: Callable[..., Coroutine[Any, Any, T]]) -> Callable[..., Coroutine[Any, Any, T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exc: Exception | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt == max_attempts:
                        break
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    logger.warning(
                        "retry_attempt",
                        func=func.__name__,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        delay=delay,
                        error=str(exc),
                    )
                    await asyncio.sleep(delay)
            raise last_exc  # type: ignore[misc]

        return wrapper

    return decorator
