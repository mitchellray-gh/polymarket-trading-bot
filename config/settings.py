"""
Configuration management for the Polymarket trading bot.

All settings are loaded from environment variables with sensible defaults.
Copy .env.example to .env and fill in your values before running.
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _get_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key, str(default)).lower()
    return val in ("1", "true", "yes")


def _get_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _get_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


# ── Polymarket connection ────────────────────────────────────────────────────
POLYMARKET_HOST: str = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
POLYMARKET_CHAIN_ID: int = _get_int("POLYMARKET_CHAIN_ID", 137)
POLYMARKET_PRIVATE_KEY: str = os.getenv("POLYMARKET_PRIVATE_KEY", "")
POLYMARKET_FUNDER_ADDRESS: str = os.getenv("POLYMARKET_FUNDER_ADDRESS", "")

# 0 = EOA, 1 = Magic / Gnosis Safe, 2 = Proxy
SIGNATURE_TYPE: int = _get_int("SIGNATURE_TYPE", 0)

# ── Trading parameters ───────────────────────────────────────────────────────
MIN_PROFIT_THRESHOLD: float = _get_float("MIN_PROFIT_THRESHOLD", 0.005)  # 0.5 %
FEE_RATE: float = _get_float("FEE_RATE", 0.02)  # 2 %

# ── Risk management ──────────────────────────────────────────────────────────
MAX_EXPOSURE_PCT: float = _get_float("MAX_EXPOSURE_PCT", 0.50)
MAX_PER_MARKET_PCT: float = _get_float("MAX_PER_MARKET_PCT", 0.10)
STOP_LOSS_PCT: float = _get_float("STOP_LOSS_PCT", 0.05)  # 5 %
MAX_ORDERS_PER_MINUTE: int = _get_int("MAX_ORDERS_PER_MINUTE", 30)

# ── Bot behaviour ────────────────────────────────────────────────────────────
SCAN_INTERVAL: int = _get_int("SCAN_INTERVAL", 10)  # seconds
PAPER_TRADING: bool = _get_bool("PAPER_TRADING", False)

# ── Alerts ───────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Logging ──────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
