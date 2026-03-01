"""
Unit tests for the market scanner.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.market_scanner import MarketScanner, MarketOpportunity


def _make_simplified_market(
    condition_id: str = "cond-1",
    question: str = "Will X happen?",
    active: bool = True,
    closed: bool = False,
    volume: float = 5000.0,
    category: str = "politics",
    yes_token_id: str = "yes-1",
    no_token_id: str = "no-1",
) -> dict:
    return {
        "condition_id": condition_id,
        "question": question,
        "active": active,
        "closed": closed,
        "volume": str(volume),
        "category": category,
        "tokens": [
            {"token_id": yes_token_id, "outcome": "YES"},
            {"token_id": no_token_id, "outcome": "NO"},
        ],
    }


def _paged(markets: list[dict]) -> dict:
    """Wrap markets in the paginated response format."""
    return {"data": markets, "next_cursor": "LTE="}


class TestGetActiveMarkets(unittest.TestCase):

    def _scanner(self, markets: list[dict]) -> MarketScanner:
        client = MagicMock()
        client.get_simplified_markets.return_value = _paged(markets)
        return MarketScanner(client=client)

    def test_returns_active_markets(self):
        markets = [_make_simplified_market(active=True, closed=False)]
        scanner = self._scanner(markets)
        result = scanner.get_active_markets()
        self.assertEqual(len(result), 1)

    def test_excludes_inactive_markets(self):
        markets = [
            _make_simplified_market(condition_id="a", active=True),
            _make_simplified_market(condition_id="b", active=False),
        ]
        scanner = self._scanner(markets)
        result = scanner.get_active_markets()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["condition_id"], "a")

    def test_excludes_closed_markets(self):
        markets = [
            _make_simplified_market(condition_id="a", closed=False),
            _make_simplified_market(condition_id="b", closed=True),
        ]
        scanner = self._scanner(markets)
        result = scanner.get_active_markets()
        self.assertEqual(len(result), 1)

    def test_filters_by_min_volume(self):
        markets = [
            _make_simplified_market(condition_id="low", volume=100.0),
            _make_simplified_market(condition_id="high", volume=5000.0),
        ]
        scanner = self._scanner(markets)
        result = scanner.get_active_markets(min_volume=1000.0)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["condition_id"], "high")

    def test_filters_by_category(self):
        markets = [
            _make_simplified_market(condition_id="a", category="politics"),
            _make_simplified_market(condition_id="b", category="sports"),
        ]
        scanner = self._scanner(markets)
        result = scanner.get_active_markets(category="sports")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["condition_id"], "b")

    def test_empty_market_list(self):
        scanner = self._scanner([])
        result = scanner.get_active_markets()
        self.assertEqual(result, [])


class TestScanArbitrage(unittest.TestCase):

    def _scanner(self, yes_ask: float, no_ask: float) -> MarketScanner:
        client = MagicMock()
        client.get_orderbook.side_effect = [
            {"asks": [{"price": str(yes_ask)}]},
            {"asks": [{"price": str(no_ask)}]},
        ]
        return MarketScanner(client=client)

    def test_detects_opportunity(self):
        """YES=0.45 + NO=0.45 < 1.0 - 0.02 = 0.98 → opportunity."""
        scanner = self._scanner(yes_ask=0.45, no_ask=0.45)
        markets = [_make_simplified_market()]
        opps = scanner.scan_arbitrage(markets, fee_threshold=0.02, min_profit=0.005)
        self.assertEqual(len(opps), 1)
        self.assertAlmostEqual(opps[0].expected_profit, 0.08, places=6)

    def test_no_opportunity_when_combined_at_1(self):
        """YES=0.50 + NO=0.50 = 1.0 → no profit after fees."""
        scanner = self._scanner(yes_ask=0.50, no_ask=0.50)
        markets = [_make_simplified_market()]
        opps = scanner.scan_arbitrage(markets, fee_threshold=0.02, min_profit=0.005)
        self.assertEqual(len(opps), 0)

    def test_no_opportunity_above_1(self):
        """Combined > 1.0 → no opportunity."""
        scanner = self._scanner(yes_ask=0.55, no_ask=0.55)
        markets = [_make_simplified_market()]
        opps = scanner.scan_arbitrage(markets, fee_threshold=0.02, min_profit=0.005)
        self.assertEqual(len(opps), 0)

    def test_sorted_by_descending_profit(self):
        """Multiple opportunities returned sorted by expected profit, highest first."""
        client = MagicMock()
        # Market 1: profit = 1 - 0.80 - 0.02 = 0.18
        # Market 2: profit = 1 - 0.90 - 0.02 = 0.08
        client.get_orderbook.side_effect = [
            {"asks": [{"price": "0.40"}]},  # m1 YES
            {"asks": [{"price": "0.40"}]},  # m1 NO
            {"asks": [{"price": "0.45"}]},  # m2 YES
            {"asks": [{"price": "0.45"}]},  # m2 NO
        ]
        scanner = MarketScanner(client=client)
        markets = [
            _make_simplified_market(condition_id="m1", yes_token_id="y1", no_token_id="n1"),
            _make_simplified_market(condition_id="m2", yes_token_id="y2", no_token_id="n2"),
        ]
        opps = scanner.scan_arbitrage(markets, fee_threshold=0.02, min_profit=0.005)
        self.assertEqual(len(opps), 2)
        self.assertGreater(opps[0].expected_profit, opps[1].expected_profit)

    def test_skips_market_without_tokens(self):
        """Markets with no YES/NO tokens are safely skipped."""
        client = MagicMock()
        scanner = MarketScanner(client=client)
        markets = [{"condition_id": "x", "tokens": [], "active": True}]
        opps = scanner.scan_arbitrage(markets)
        self.assertEqual(opps, [])
        client.get_orderbook.assert_not_called()

    def test_handles_orderbook_fetch_error(self):
        """Orderbook errors for one market do not crash the scanner."""
        client = MagicMock()
        client.get_orderbook.side_effect = Exception("timeout")
        scanner = MarketScanner(client=client)
        markets = [_make_simplified_market()]
        # Should not raise
        opps = scanner.scan_arbitrage(markets)
        self.assertEqual(opps, [])

    def test_best_ask_static_method(self):
        """_best_ask extracts the lowest ask price correctly."""
        book = {
            "asks": [
                {"price": "0.55"},
                {"price": "0.48"},
                {"price": "0.60"},
            ]
        }
        self.assertAlmostEqual(MarketScanner._best_ask(book), 0.48)

    def test_best_ask_empty_book(self):
        self.assertIsNone(MarketScanner._best_ask({"asks": []}))


if __name__ == "__main__":
    unittest.main()
