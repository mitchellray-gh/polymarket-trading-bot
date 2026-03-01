"""
YES+NO Arbitrage strategy.

Identifies markets where buying both YES and NO tokens costs less than $1.00,
guaranteeing a risk-free profit (minus fees).

Entry condition:
    yes_ask + no_ask < 1.0 - fee_threshold

The strategy uses Fill-Or-Kill market orders for fast execution.
"""

from __future__ import annotations

from typing import Any

from config import settings
from bot.strategies.base import BaseStrategy
from bot.market_scanner import MarketScanner


class ArbitrageStrategy(BaseStrategy):
    """Executes YES+NO arbitrage when combined ask < 1.0 - fee_threshold."""

    def __init__(
        self,
        client: Any,
        order_manager: Any,
        risk_manager: Any,
        fee_threshold: float | None = None,
        min_profit: float | None = None,
    ) -> None:
        super().__init__(client, order_manager, risk_manager)
        self.fee_threshold = fee_threshold if fee_threshold is not None else settings.FEE_RATE
        self.min_profit = min_profit if min_profit is not None else settings.MIN_PROFIT_THRESHOLD

    # ── BaseStrategy interface ────────────────────────────────────────────────

    def run(self, market: dict[str, Any]) -> None:
        """
        Evaluate a single *market* dict for arbitrage opportunity and execute if found.
        """
        tokens = market.get("tokens", [])
        yes_token = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), None)
        no_token = next((t for t in tokens if t.get("outcome", "").upper() == "NO"), None)
        if not yes_token or not no_token:
            return

        yes_token_id = yes_token.get("token_id", "")
        no_token_id = no_token.get("token_id", "")

        yes_ask = self._get_best_ask(yes_token_id)
        no_ask = self._get_best_ask(no_token_id)
        if yes_ask is None or no_ask is None:
            return

        combined = yes_ask + no_ask
        expected_profit = 1.0 - combined - self.fee_threshold

        if expected_profit < self.min_profit:
            self.logger.debug(
                "No arb in %s: combined=%.4f profit=%.4f < threshold=%.4f",
                market.get("condition_id"),
                combined,
                expected_profit,
                self.min_profit,
            )
            return

        self.logger.info(
            "Arbitrage found: %s | YES=%.4f NO=%.4f profit=%.4f",
            market.get("question", "")[:60],
            yes_ask,
            no_ask,
            expected_profit,
        )

        trade_size = self._risk_manager.position_size(
            balance=self._risk_manager.get_balance(),
            market_id=market.get("condition_id", ""),
        )
        if trade_size <= 0:
            self.logger.warning("Risk manager blocked trade (size=0)")
            return

        self._order_manager.place_market_order(
            token_id=yes_token_id,
            amount=trade_size,
            side="BUY",
            metadata={"strategy": "arbitrage", "leg": "YES"},
        )
        self._order_manager.place_market_order(
            token_id=no_token_id,
            amount=trade_size,
            side="BUY",
            metadata={"strategy": "arbitrage", "leg": "NO"},
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def detect_opportunity(
        self, yes_ask: float, no_ask: float
    ) -> tuple[bool, float]:
        """
        Pure function: returns ``(is_opportunity, expected_profit)``.

        Useful for testing and market scanning without side-effects.
        """
        combined = yes_ask + no_ask
        expected_profit = 1.0 - combined - self.fee_threshold
        return expected_profit >= self.min_profit, expected_profit

    def _get_best_ask(self, token_id: str) -> float | None:
        try:
            book = self._client.get_orderbook(token_id)
            asks = book.get("asks", [])
            if not asks:
                return None
            return min(float(a["price"]) for a in asks if "price" in a)
        except Exception as exc:
            self.logger.debug("Failed to fetch asks for %s: %s", token_id, exc)
            return None
