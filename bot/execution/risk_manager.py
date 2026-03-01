"""
Risk manager: position sizing, exposure limits, stop-loss, and rate limiting.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

from config import settings
from bot.utils.logger import get_logger
from bot.utils.helpers import clamp, safe_divide

logger = get_logger(__name__)


class RiskManager:
    """
    Enforces risk controls before any order is placed.

    Parameters mirror ``config/settings.py``; pass overrides in tests or
    paper-trading scenarios.
    """

    def __init__(
        self,
        client: Any | None = None,
        max_exposure_pct: float | None = None,
        max_per_market_pct: float | None = None,
        stop_loss_pct: float | None = None,
        max_orders_per_minute: int | None = None,
        paper_trading: bool | None = None,
    ) -> None:
        self._client = client
        self.max_exposure_pct = max_exposure_pct if max_exposure_pct is not None else settings.MAX_EXPOSURE_PCT
        self.max_per_market_pct = max_per_market_pct if max_per_market_pct is not None else settings.MAX_PER_MARKET_PCT
        self.stop_loss_pct = stop_loss_pct if stop_loss_pct is not None else settings.STOP_LOSS_PCT
        self.max_orders_per_minute = max_orders_per_minute if max_orders_per_minute is not None else settings.MAX_ORDERS_PER_MINUTE
        self.paper_trading = paper_trading if paper_trading is not None else settings.PAPER_TRADING

        # Order rate-limiting: track timestamps of recent orders
        self._order_timestamps: deque[float] = deque()

        # Per-market exposure tracking: market_id → USDC deployed
        self._market_exposure: dict[str, float] = {}
        self._total_exposure: float = 0.0

        # Realised P&L tracking (for stop-loss)
        self._starting_balance: float | None = None

    # ── Balance ───────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        """Return current USDC balance (or 0 if client is unavailable)."""
        if self._client is None:
            return 0.0
        try:
            return self._client.get_balance()
        except Exception as exc:
            logger.warning("Failed to fetch balance: %s", exc)
            return 0.0

    # ── Position sizing ───────────────────────────────────────────────────────

    def position_size(
        self,
        balance: float,
        market_id: str = "",
        kelly_fraction: float | None = None,
        win_prob: float | None = None,
        win_payout: float | None = None,
    ) -> float:
        """
        Calculate a safe order size in USDC.

        If *kelly_fraction*, *win_prob*, and *win_payout* are all provided the
        Kelly criterion is used.  Otherwise fixed-fractional sizing is applied.
        """
        max_total = balance * self.max_exposure_pct
        max_market = balance * self.max_per_market_pct

        # Remaining capacity
        remaining_total = max(0.0, max_total - self._total_exposure)
        market_deployed = self._market_exposure.get(market_id, 0.0)
        remaining_market = max(0.0, max_market - market_deployed)

        capacity = min(remaining_total, remaining_market)

        if kelly_fraction is not None and win_prob is not None and win_payout is not None:
            size = self._kelly_size(balance, kelly_fraction, win_prob, win_payout)
        else:
            # Fixed fractional: use the smaller of per-market limit and total capacity
            size = capacity

        size = clamp(size, 0.0, capacity)
        logger.debug(
            "position_size: balance=%.2f capacity=%.2f size=%.2f [%s]",
            balance,
            capacity,
            size,
            market_id,
        )
        return round(size, 2)

    def record_order(self, market_id: str, amount: float) -> None:
        """Record that *amount* USDC has been committed to *market_id*."""
        self._market_exposure[market_id] = self._market_exposure.get(market_id, 0.0) + amount
        self._total_exposure += amount

    def release_order(self, market_id: str, amount: float) -> None:
        """Release *amount* USDC from the exposure tracker for *market_id*."""
        self._market_exposure[market_id] = max(
            0.0, self._market_exposure.get(market_id, 0.0) - amount
        )
        self._total_exposure = max(0.0, self._total_exposure - amount)

    # ── Rate limiting ─────────────────────────────────────────────────────────

    def check_rate_limit(self) -> bool:
        """
        Return ``True`` if a new order is allowed under the current rate limit.
        Cleans up timestamps older than 60 seconds.
        """
        now = time.monotonic()
        cutoff = now - 60.0
        while self._order_timestamps and self._order_timestamps[0] < cutoff:
            self._order_timestamps.popleft()

        if len(self._order_timestamps) >= self.max_orders_per_minute:
            logger.warning(
                "Rate limit hit: %d orders in the last minute (max=%d)",
                len(self._order_timestamps),
                self.max_orders_per_minute,
            )
            return False

        self._order_timestamps.append(now)
        return True

    # ── Stop-loss ─────────────────────────────────────────────────────────────

    def check_stop_loss(self, current_balance: float) -> bool:
        """
        Return ``True`` if the stop-loss has been triggered (i.e. we should halt).
        Sets the starting balance on first call.
        """
        if self._starting_balance is None:
            self._starting_balance = current_balance
            return False

        loss_pct = safe_divide(
            self._starting_balance - current_balance, self._starting_balance
        )
        if loss_pct >= self.stop_loss_pct:
            logger.error(
                "Stop-loss triggered: balance dropped %.2f%% (threshold=%.2f%%)",
                loss_pct * 100,
                self.stop_loss_pct * 100,
            )
            return True
        return False

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _kelly_size(
        balance: float,
        kelly_fraction: float,
        win_prob: float,
        win_payout: float,
    ) -> float:
        """
        Full-Kelly fraction of *balance*.

        kelly_fraction allows fractional Kelly (e.g. 0.25 for quarter-Kelly).
        """
        b = win_payout - 1.0  # net odds
        q = 1.0 - win_prob
        if b <= 0:
            return 0.0
        full_kelly = (win_prob * b - q) / b
        return balance * kelly_fraction * max(0.0, full_kelly)
