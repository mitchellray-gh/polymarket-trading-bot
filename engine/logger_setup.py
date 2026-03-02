"""
engine/logger_setup.py
───────────────────────
Configure structured logging for the trading engine.
Outputs to both stdout (colourised) and a rotating log file.
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from colorama import Fore, Style, init as colorama_init

colorama_init(autoreset=True)


class ColouredFormatter(logging.Formatter):
    """Adds colour to log levels so the console is easy to scan at a glance."""

    LEVEL_COLOURS = {
        logging.DEBUG:    Fore.CYAN,
        logging.INFO:     Fore.GREEN,
        logging.WARNING:  Fore.YELLOW,
        logging.ERROR:    Fore.RED,
        logging.CRITICAL: Fore.MAGENTA,
    }

    def format(self, record: logging.LogRecord) -> str:
        colour = self.LEVEL_COLOURS.get(record.levelno, "")
        record.levelname = f"{colour}{record.levelname:<8}{Style.RESET_ALL}"
        return super().format(record)


def setup_logging(level: str = "INFO", log_file: str = "trading_engine.log") -> None:
    """Configure root logger with a console handler and a rotating file handler."""
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric_level)

    # ── Console ──────────────────────────────────────────────────────────────
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(numeric_level)
    console.setFormatter(
        ColouredFormatter(
            fmt="%(asctime)s %(levelname)s %(name)-25s %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(console)

    # ── Rotating file ─────────────────────────────────────────────────────────
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10 MB per file
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-8s %(name)-25s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(file_handler)

    # Suppress noisy third-party loggers
    for noisy in ("urllib3", "aiohttp", "asyncio", "web3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
