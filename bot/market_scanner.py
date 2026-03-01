"""
Market scanner: discovers active Polymarket markets and scores opportunities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from config import settings
from bot.utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class MarketOpportunity:
    """Represents a potential trading opportunity in a market."""

    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    yes_ask: float
    no_ask: float
    expected_profit: float
    strategy: str = "arbitrage"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def combined_ask(self) -> float:
        return self.yes_ask + self.no_ask


class MarketScanner:
    """Scans Polymarket markets and surfaces trading opportunities."""

    def __init__(self, client: Any) -> None:
        self._client = client

    # ── Public API ────────────────────────────────────────────────────────────

    def get_active_markets(
        self,
        min_volume: float = 0.0,
        category: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Return a list of active markets, optionally filtered by volume / category.
        """
        markets: list[dict[str, Any]] = []
        cursor = ""

        while True:
            response = self._client.get_simplified_markets(next_cursor=cursor)
            page: list[dict[str, Any]] = response.get("data", [])
            next_cursor: str = response.get("next_cursor", "")

            for market in page:
                if not market.get("active", False):
                    continue
                if market.get("closed", False):
                    continue
                volume = float(market.get("volume", 0))
                if volume < min_volume:
                    continue
                if category and market.get("category", "").lower() != category.lower():
                    continue
                markets.append(market)

            if not next_cursor or next_cursor == "LTE=":
                break
            cursor = next_cursor

        logger.info("MarketScanner: found %d active markets", len(markets))
        return markets

    def scan_arbitrage(
        self,
        markets: list[dict[str, Any]],
        fee_threshold: float | None = None,
        min_profit: float | None = None,
    ) -> list[MarketOpportunity]:
        """
        Scan *markets* for YES+NO arbitrage opportunities.

        An opportunity exists when:
            yes_ask + no_ask < 1.0 - fee_threshold

        Returns a list sorted by expected profit (descending).
        """
        if fee_threshold is None:
            fee_threshold = settings.FEE_RATE
        if min_profit is None:
            min_profit = settings.MIN_PROFIT_THRESHOLD

        opportunities: list[MarketOpportunity] = []

        for market in markets:
            tokens = market.get("tokens", [])
            if len(tokens) < 2:
                continue

            yes_token = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), None)
            no_token = next((t for t in tokens if t.get("outcome", "").upper() == "NO"), None)
            if not yes_token or not no_token:
                continue

            yes_token_id = yes_token.get("token_id", "")
            no_token_id = no_token.get("token_id", "")
            if not yes_token_id or not no_token_id:
                continue

            try:
                yes_book = self._client.get_orderbook(yes_token_id)
                no_book = self._client.get_orderbook(no_token_id)
            except Exception as exc:
                logger.debug("Failed to fetch orderbook for %s: %s", market.get("condition_id"), exc)
                continue

            yes_ask = self._best_ask(yes_book)
            no_ask = self._best_ask(no_book)
            if yes_ask is None or no_ask is None:
                continue

            combined = yes_ask + no_ask
            expected_profit = 1.0 - combined - fee_threshold

            if expected_profit < min_profit:
                continue

            opp = MarketOpportunity(
                condition_id=market.get("condition_id", ""),
                question=market.get("question", ""),
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
                yes_ask=yes_ask,
                no_ask=no_ask,
                expected_profit=expected_profit,
                strategy="arbitrage",
                metadata={"volume": market.get("volume", 0)},
            )
            opportunities.append(opp)
            logger.info(
                "Arbitrage opportunity: %s | YES=%.4f NO=%.4f profit=%.4f",
                opp.question[:60],
                yes_ask,
                no_ask,
                expected_profit,
            )

        opportunities.sort(key=lambda o: o.expected_profit, reverse=True)
        return opportunities

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _best_ask(orderbook: dict[str, Any]) -> float | None:
        """Extract the best (lowest) ask price from an orderbook response."""
        asks = orderbook.get("asks", [])
        if not asks:
            return None
        try:
            return min(float(a["price"]) for a in asks if "price" in a)
        except (ValueError, TypeError):
            return None
