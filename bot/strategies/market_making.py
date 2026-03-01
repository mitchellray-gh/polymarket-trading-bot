"""
Simple spread-based market making strategy.

Places a symmetric bid and ask around the current midpoint.
Stale orders are automatically cancelled after a configurable timeout.
"""

from __future__ import annotations

import time
from typing import Any

from bot.strategies.base import BaseStrategy


class MarketMakingStrategy(BaseStrategy):
    """
    Posts limit orders on both sides of the book around the midpoint.

    Parameters
    ----------
    spread:
        Half-spread as a fraction of price (e.g., 0.02 → 2 % each side).
    order_size:
        Fixed USDC amount per order.
    max_inventory:
        Maximum net position (in USDC) before the strategy stops adding.
    order_ttl:
        Seconds before a posted order is considered stale and cancelled.
    """

    def __init__(
        self,
        client: Any,
        order_manager: Any,
        risk_manager: Any,
        spread: float = 0.02,
        order_size: float = 10.0,
        max_inventory: float = 100.0,
        order_ttl: float = 60.0,
    ) -> None:
        super().__init__(client, order_manager, risk_manager)
        self.spread = spread
        self.order_size = order_size
        self.max_inventory = max_inventory
        self.order_ttl = order_ttl
        # token_id → list of (order_id, placed_at_ts)
        self._open_orders: dict[str, list[tuple[str, float]]] = {}

    # ── BaseStrategy interface ────────────────────────────────────────────────

    def run(self, market: dict[str, Any]) -> None:
        tokens = market.get("tokens", [])
        yes_token = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), None)
        if not yes_token:
            return

        token_id = yes_token.get("token_id", "")
        if not token_id:
            return

        # Cancel stale orders first
        self._cancel_stale_orders(token_id)

        try:
            mid = self._client.get_midpoint(token_id)
        except Exception as exc:
            self.logger.debug("Failed to get midpoint for %s: %s", token_id, exc)
            return

        if mid <= 0 or mid >= 1:
            return  # degenerate price

        bid_price = round(mid * (1 - self.spread), 4)
        ask_price = round(mid * (1 + self.spread), 4)

        # Clamp to valid prediction-market range
        bid_price = max(0.01, min(bid_price, 0.99))
        ask_price = max(0.01, min(ask_price, 0.99))

        balance = self._risk_manager.get_balance()
        trade_size = min(
            self.order_size,
            self._risk_manager.position_size(
                balance=balance,
                market_id=market.get("condition_id", ""),
            ),
        )
        if trade_size <= 0:
            return

        self.logger.info(
            "Market-making %s | mid=%.4f bid=%.4f ask=%.4f size=%.2f",
            market.get("question", "")[:50],
            mid,
            bid_price,
            ask_price,
            trade_size,
        )

        for price, side in [(bid_price, "BUY"), (ask_price, "SELL")]:
            result = self._order_manager.place_limit_order(
                token_id=token_id,
                price=price,
                size=trade_size,
                side=side,
                metadata={"strategy": "market_making"},
            )
            if result and result.get("order_id"):
                orders = self._open_orders.setdefault(token_id, [])
                orders.append((result["order_id"], time.monotonic()))

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _cancel_stale_orders(self, token_id: str) -> None:
        now = time.monotonic()
        orders = self._open_orders.get(token_id, [])
        still_open: list[tuple[str, float]] = []
        for order_id, placed_at in orders:
            if now - placed_at > self.order_ttl:
                try:
                    self._order_manager.cancel_order(order_id)
                    self.logger.debug("Cancelled stale order %s", order_id)
                except Exception as exc:
                    self.logger.warning("Failed to cancel order %s: %s", order_id, exc)
            else:
                still_open.append((order_id, placed_at))
        self._open_orders[token_id] = still_open
