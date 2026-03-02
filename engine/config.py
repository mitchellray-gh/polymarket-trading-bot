"""
engine/config.py
────────────────
Central configuration for the Polymarket Trading Engine.
All values are loaded from environment variables (via .env) with sensible defaults.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (one level above this file)
load_dotenv(Path(__file__).parent.parent / ".env")


# ─── Public endpoints ────────────────────────────────────────────────────────
CLOB_HOST       = "https://clob.polymarket.com"
GAMMA_API_HOST  = "https://gamma-api.polymarket.com"


@dataclass
class Config:
    """
    Runtime configuration.  Populated once at startup from env-vars.
    Pass this object around so every module reads the same settings.
    """

    # ── Wallet ──────────────────────────────────────────────────────────────
    private_key:      str  = field(default_factory=lambda: os.getenv("PRIVATE_KEY", ""))
    funder_address:   str  = field(default_factory=lambda: os.getenv("FUNDER_ADDRESS", ""))
    chain_id:         int  = field(default_factory=lambda: int(os.getenv("CHAIN_ID", "137")))
    signature_type:   int  = field(default_factory=lambda: int(os.getenv("SIGNATURE_TYPE", "0")))

    # ── Risk / position limits ───────────────────────────────────────────────
    max_position_usdc:      float = field(default_factory=lambda: float(os.getenv("MAX_POSITION_SIZE_USDC", "50")))
    min_profit_threshold:   float = field(default_factory=lambda: float(os.getenv("MIN_PROFIT_THRESHOLD", "0.001")))
    max_open_positions:     int   = field(default_factory=lambda: int(os.getenv("MAX_OPEN_POSITIONS", "10")))

    # ── Engine behaviour ────────────────────────────────────────────────────
    scan_interval_seconds:  float = field(default_factory=lambda: float(os.getenv("SCAN_INTERVAL_SECONDS", "0.5")))
    scan_batch_size:        int   = field(default_factory=lambda: int(os.getenv("SCAN_BATCH_SIZE", "500")))
    dry_run:                bool  = field(default_factory=lambda: os.getenv("DRY_RUN", "true").lower() == "true")

    # ── Logging ─────────────────────────────────────────────────────────────
    log_level:  str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    log_file:   str = field(default_factory=lambda: os.getenv("LOG_FILE", "trading_engine.log"))


    @property
    def has_credentials(self) -> bool:
        """True when both private_key and funder_address are set."""
        return bool(self.private_key and self.funder_address)


def load_config() -> Config:
    """Build and return the engine configuration."""
    return Config()
