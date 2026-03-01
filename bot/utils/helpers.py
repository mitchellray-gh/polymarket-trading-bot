"""Common utility helpers."""

from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

from bot.utils.logger import get_logger

logger = get_logger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def retry(
    max_attempts: int = 3,
    backoff: float = 1.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
):
    """Decorator: retry a function up to *max_attempts* times with exponential back-off."""

    def decorator(func: F) -> F:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            delay = backoff
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    if attempt == max_attempts:
                        raise
                    logger.warning(
                        "Attempt %d/%d for %s failed: %s — retrying in %.1fs",
                        attempt,
                        max_attempts,
                        func.__name__,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
                    delay *= 2

        return wrapper  # type: ignore[return-value]

    return decorator


def clamp(value: float, lo: float, hi: float) -> float:
    """Clamp *value* between *lo* and *hi*."""
    return max(lo, min(hi, value))


def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Return *numerator* / *denominator*, or *default* if denominator is zero."""
    if denominator == 0:
        return default
    return numerator / denominator
