"""
Polymarket CLOB client wrapper.

Wraps ``py_clob_client.client.ClobClient`` with:
- retry logic
- basic rate-limiting
- clean error handling
- initialisation from environment variables
"""

from __future__ import annotations

import time
from typing import Any

from config import settings
from bot.utils.logger import get_logger
from bot.utils.helpers import retry

logger = get_logger(__name__)

# ── Optional SDK import ───────────────────────────────────────────────────────
try:
    from py_clob_client.client import ClobClient as _ClobClient
    from py_clob_client.clob_types import (
        OrderArgs,
        MarketOrderArgs,
        BookParams,
        OpenOrderParams,
    )
    from py_clob_client.order_builder.constants import BUY, SELL
    _SDK_AVAILABLE = True
except ImportError:  # pragma: no cover
    _SDK_AVAILABLE = False
    _ClobClient = None  # type: ignore[assignment,misc]
    OrderArgs = MarketOrderArgs = BookParams = OpenOrderParams = None  # type: ignore[assignment]
    BUY = "BUY"
    SELL = "SELL"


class PolymarketClient:
    """Thin wrapper around the Polymarket CLOB client SDK."""

    # Minimum seconds between consecutive API calls
    _MIN_CALL_INTERVAL: float = 60.0 / settings.MAX_ORDERS_PER_MINUTE

    def __init__(self) -> None:
        if not _SDK_AVAILABLE:
            raise ImportError(
                "py-clob-client is not installed. Run: pip install py-clob-client"
            )
        if not settings.POLYMARKET_PRIVATE_KEY:
            raise ValueError(
                "POLYMARKET_PRIVATE_KEY environment variable is required."
            )

        kwargs: dict[str, Any] = {
            "key": settings.POLYMARKET_PRIVATE_KEY,
            "chain_id": settings.POLYMARKET_CHAIN_ID,
            "host": settings.POLYMARKET_HOST,
            "signature_type": settings.SIGNATURE_TYPE,
        }
        if settings.POLYMARKET_FUNDER_ADDRESS:
            kwargs["funder"] = settings.POLYMARKET_FUNDER_ADDRESS

        self._client: Any = _ClobClient(**kwargs)
        self._last_call: float = 0.0
        logger.info(
            "PolymarketClient initialised (host=%s, chain=%d, sig_type=%d)",
            settings.POLYMARKET_HOST,
            settings.POLYMARKET_CHAIN_ID,
            settings.SIGNATURE_TYPE,
        )

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _throttle(self) -> None:
        """Sleep if necessary to respect the rate limit."""
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._MIN_CALL_INTERVAL:
            time.sleep(self._MIN_CALL_INTERVAL - elapsed)
        self._last_call = time.monotonic()

    # ── Market data ───────────────────────────────────────────────────────────

    @retry(max_attempts=3, backoff=1.0)
    def get_markets(self, next_cursor: str = "") -> dict[str, Any]:
        """Return a page of active markets."""
        self._throttle()
        return self._client.get_markets(next_cursor=next_cursor)

    @retry(max_attempts=3, backoff=1.0)
    def get_simplified_markets(self, next_cursor: str = "") -> dict[str, Any]:
        """Return simplified market data (lower bandwidth)."""
        self._throttle()
        return self._client.get_simplified_markets(next_cursor=next_cursor)

    @retry(max_attempts=3, backoff=1.0)
    def get_orderbook(self, token_id: str) -> dict[str, Any]:
        """Return the full order book for *token_id*."""
        self._throttle()
        params = BookParams(token_id=token_id)
        return self._client.get_order_book(params)

    @retry(max_attempts=3, backoff=1.0)
    def get_midpoint(self, token_id: str) -> float:
        """Return the current midpoint price for *token_id* (0–1 range)."""
        self._throttle()
        result = self._client.get_midpoint(token_id=token_id)
        return float(result.get("mid", 0.5))

    # ── Order management ──────────────────────────────────────────────────────

    @retry(max_attempts=2, backoff=0.5)
    def place_limit_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str = "BUY",
    ) -> dict[str, Any]:
        """Place a GTC limit order. Returns the order response dict."""
        self._throttle()
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
        )
        signed_order = self._client.create_order(order_args)
        return self._client.post_order(signed_order)

    @retry(max_attempts=2, backoff=0.5)
    def place_market_order(
        self,
        token_id: str,
        amount: float,
        side: str = "BUY",
    ) -> dict[str, Any]:
        """Place a FOK market order for *amount* USDC. Returns order response."""
        self._throttle()
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=amount,
        )
        signed_order = self._client.create_market_order(order_args)
        return self._client.post_order(signed_order)

    @retry(max_attempts=2, backoff=0.5)
    def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel a single open order by its ID."""
        self._throttle()
        return self._client.cancel(order_id=order_id)

    @retry(max_attempts=2, backoff=0.5)
    def cancel_all_orders(self) -> dict[str, Any]:
        """Cancel ALL open orders for this account."""
        self._throttle()
        return self._client.cancel_all()

    @retry(max_attempts=3, backoff=1.0)
    def get_open_orders(self, market: str | None = None) -> list[dict[str, Any]]:
        """Return a list of currently open orders."""
        self._throttle()
        params = OpenOrderParams(market=market) if market else OpenOrderParams()
        return self._client.get_orders(params)

    @retry(max_attempts=3, backoff=1.0)
    def get_positions(self) -> list[dict[str, Any]]:
        """Return the current token positions for this account."""
        self._throttle()
        return self._client.get_positions()

    @retry(max_attempts=3, backoff=1.0)
    def get_trades(self) -> list[dict[str, Any]]:
        """Return recent trade history for this account."""
        self._throttle()
        return self._client.get_trades()

    @retry(max_attempts=3, backoff=1.0)
    def get_balance(self) -> float:
        """Return the USDC balance available for trading."""
        self._throttle()
        result = self._client.get_balance_allowance()
        return float(result.get("balance", 0.0))
