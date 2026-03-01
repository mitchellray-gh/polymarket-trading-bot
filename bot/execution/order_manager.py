"""
Order manager: places, tracks, and cancels orders via the CLOB client.

In paper-trading mode all orders are simulated; no real API calls are made.
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any

from config import settings
from bot.utils.logger import get_logger

logger = get_logger(__name__)


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class OrderManager:
    """
    Manages the full lifecycle of orders.

    Parameters
    ----------
    client:
        A ``PolymarketClient`` instance (or ``None`` in paper-trading mode).
    risk_manager:
        A ``RiskManager`` instance used for rate-limit checks.
    paper_trading:
        When ``True`` orders are logged but NOT sent to the exchange.
    """

    def __init__(
        self,
        client: Any | None = None,
        risk_manager: Any | None = None,
        paper_trading: bool | None = None,
    ) -> None:
        self._client = client
        self._risk_manager = risk_manager
        self.paper_trading = paper_trading if paper_trading is not None else settings.PAPER_TRADING
        # order_id → order dict
        self._orders: dict[str, dict[str, Any]] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def place_limit_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str = "BUY",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Place a GTC limit order. Returns order dict (real or simulated)."""
        if not self._pre_flight_check():
            return {}

        if self.paper_trading:
            return self._simulate_order(
                token_id, price, size, side, "LIMIT", metadata
            )

        try:
            result = self._client.place_limit_order(token_id, price, size, side)
            order = self._track(result, token_id, price, size, side, "LIMIT", metadata)
            logger.info("Limit order placed: %s", order["order_id"])
            return order
        except Exception as exc:
            logger.error("Failed to place limit order: %s", exc)
            return {"status": OrderStatus.FAILED, "error": str(exc)}

    def place_market_order(
        self,
        token_id: str,
        amount: float,
        side: str = "BUY",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Place a FOK market order for *amount* USDC. Returns order dict."""
        if not self._pre_flight_check():
            return {}

        if self.paper_trading:
            return self._simulate_order(
                token_id, None, amount, side, "MARKET", metadata
            )

        try:
            result = self._client.place_market_order(token_id, amount, side)
            order = self._track(result, token_id, None, amount, side, "MARKET", metadata)
            logger.info("Market order placed: %s", order["order_id"])
            return order
        except Exception as exc:
            logger.error("Failed to place market order: %s", exc)
            return {"status": OrderStatus.FAILED, "error": str(exc)}

    def cancel_order(self, order_id: str) -> bool:
        """Cancel *order_id*. Returns ``True`` on success."""
        if self.paper_trading:
            if order_id in self._orders:
                self._orders[order_id]["status"] = OrderStatus.CANCELLED
            logger.info("[PAPER] Cancelled order %s", order_id)
            return True

        try:
            self._client.cancel_order(order_id)
            if order_id in self._orders:
                self._orders[order_id]["status"] = OrderStatus.CANCELLED
            logger.info("Cancelled order %s", order_id)
            return True
        except Exception as exc:
            logger.error("Failed to cancel order %s: %s", order_id, exc)
            return False

    def cancel_all_orders(self) -> bool:
        """Cancel all open orders. Returns ``True`` on success."""
        if self.paper_trading:
            for order in self._orders.values():
                if order["status"] == OrderStatus.OPEN:
                    order["status"] = OrderStatus.CANCELLED
            logger.info("[PAPER] All orders cancelled")
            return True

        try:
            self._client.cancel_all_orders()
            for order in self._orders.values():
                if order["status"] in (OrderStatus.PENDING, OrderStatus.OPEN):
                    order["status"] = OrderStatus.CANCELLED
            logger.info("All orders cancelled")
            return True
        except Exception as exc:
            logger.error("Failed to cancel all orders: %s", exc)
            return False

    def get_open_orders(self) -> list[dict[str, Any]]:
        """Return a list of locally-tracked open orders."""
        return [
            o for o in self._orders.values()
            if o["status"] in (OrderStatus.PENDING, OrderStatus.OPEN)
        ]

    def get_all_orders(self) -> list[dict[str, Any]]:
        return list(self._orders.values())

    # ── Private helpers ───────────────────────────────────────────────────────

    def _pre_flight_check(self) -> bool:
        if self._risk_manager and not self._risk_manager.check_rate_limit():
            logger.warning("Order blocked by rate limiter")
            return False
        return True

    def _simulate_order(
        self,
        token_id: str,
        price: float | None,
        size: float,
        side: str,
        order_type: str,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        order_id = str(uuid.uuid4())
        order: dict[str, Any] = {
            "order_id": order_id,
            "token_id": token_id,
            "price": price,
            "size": size,
            "side": side,
            "type": order_type,
            "status": OrderStatus.FILLED,  # paper = instantly filled
            "placed_at": time.time(),
            "paper": True,
            "metadata": metadata or {},
        }
        self._orders[order_id] = order
        logger.info(
            "[PAPER] %s %s order: token=%s price=%s size=%.2f",
            side,
            order_type,
            token_id[:16],
            price,
            size,
        )
        return order

    def _track(
        self,
        result: dict[str, Any],
        token_id: str,
        price: float | None,
        size: float,
        side: str,
        order_type: str,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        order_id = result.get("orderID") or result.get("order_id") or str(uuid.uuid4())
        order: dict[str, Any] = {
            "order_id": order_id,
            "token_id": token_id,
            "price": price,
            "size": size,
            "side": side,
            "type": order_type,
            "status": OrderStatus.OPEN,
            "placed_at": time.time(),
            "paper": False,
            "metadata": metadata or {},
            "raw": result,
        }
        self._orders[order_id] = order
        return order
