"""
Midpoint deviation strategy.

Places limit orders near the midpoint when the current mid deviates
significantly from its recent moving average, betting on mean-reversion.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from config import settings
from bot.strategies.base import BaseStrategy


class MidpointStrategy(BaseStrategy):
    """
    Buys when midpoint is below the moving average by *deviation_threshold*,
    sells when it is above by the same margin.
    """

    def __init__(
        self,
        client: Any,
        order_manager: Any,
        risk_manager: Any,
        lookback: int = 20,
        deviation_threshold: float = 0.03,
    ) -> None:
        super().__init__(client, order_manager, risk_manager)
        self.lookback = lookback
        self.deviation_threshold = deviation_threshold
        # token_id → deque of recent midpoints
        self._history: dict[str, deque[float]] = {}

    # ── BaseStrategy interface ────────────────────────────────────────────────

    def run(self, market: dict[str, Any]) -> None:
        tokens = market.get("tokens", [])
        yes_token = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), None)
        if not yes_token:
            return

        token_id = yes_token.get("token_id", "")
        if not token_id:
            return

        try:
            mid = self._client.get_midpoint(token_id)
        except Exception as exc:
            self.logger.debug("Failed to get midpoint for %s: %s", token_id, exc)
            return

        history = self._history.setdefault(token_id, deque(maxlen=self.lookback))
        history.append(mid)

        if len(history) < self.lookback:
            return  # not enough data yet

        avg = sum(history) / len(history)
        deviation = (mid - avg) / avg if avg > 0 else 0.0

        balance = self._risk_manager.get_balance()
        trade_size = self._risk_manager.position_size(
            balance=balance,
            market_id=market.get("condition_id", ""),
        )
        if trade_size <= 0:
            return

        if deviation <= -self.deviation_threshold:
            # Price is depressed — buy near midpoint expecting recovery
            limit_price = round(mid * 1.001, 4)  # slightly above mid
            self.logger.info(
                "Midpoint BUY signal: %s mid=%.4f avg=%.4f dev=%.2f%%",
                market.get("question", "")[:50],
                mid,
                avg,
                deviation * 100,
            )
            self._order_manager.place_limit_order(
                token_id=token_id,
                price=limit_price,
                size=trade_size,
                side="BUY",
                metadata={"strategy": "midpoint", "deviation": deviation},
            )

        elif deviation >= self.deviation_threshold:
            # Price is elevated — sell near midpoint
            limit_price = round(mid * 0.999, 4)  # slightly below mid
            self.logger.info(
                "Midpoint SELL signal: %s mid=%.4f avg=%.4f dev=%.2f%%",
                market.get("question", "")[:50],
                mid,
                avg,
                deviation * 100,
            )
            self._order_manager.place_limit_order(
                token_id=token_id,
                price=limit_price,
                size=trade_size,
                side="SELL",
                metadata={"strategy": "midpoint", "deviation": deviation},
            )
