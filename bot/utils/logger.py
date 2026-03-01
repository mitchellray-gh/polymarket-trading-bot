"""
Structured logging setup for the Polymarket trading bot.
"""

import logging
import sys
from config import settings


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger for the given module name."""
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger  # already configured

    logger.setLevel(getattr(logging, settings.LOG_LEVEL, logging.INFO))

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(getattr(logging, settings.LOG_LEVEL, logging.INFO))

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.propagate = False

    return logger
