"""
Unit tests for trading strategy logic.

All tests use mock objects — no real network calls are made.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from bot.strategies.arbitrage import ArbitrageStrategy
from bot.strategies.midpoint import MidpointStrategy
from bot.strategies.market_making import MarketMakingStrategy
from bot.execution.order_manager import OrderManager
from bot.execution.risk_manager import RiskManager


def _make_market(
    yes_token_id: str = "yes-token-1",
    no_token_id: str = "no-token-1",
    condition_id: str = "cond-1",
    question: str = "Will X happen?",
) -> dict:
    return {
        "condition_id": condition_id,
        "question": question,
        "active": True,
        "closed": False,
        "volume": 10000,
        "tokens": [
            {"token_id": yes_token_id, "outcome": "YES"},
            {"token_id": no_token_id, "outcome": "NO"},
        ],
    }


def _make_orderbook(best_ask: float) -> dict:
    return {"asks": [{"price": str(best_ask), "size": "100"}]}


class TestArbitrageStrategy(unittest.TestCase):

    def _strategy(self, fee_threshold: float = 0.02, min_profit: float = 0.005):
        client = MagicMock()
        risk_manager = RiskManager(client=None, paper_trading=True)
        order_manager = OrderManager(paper_trading=True, risk_manager=risk_manager)
        return ArbitrageStrategy(
            client=client,
            order_manager=order_manager,
            risk_manager=risk_manager,
            fee_threshold=fee_threshold,
            min_profit=min_profit,
        )

    # ── detect_opportunity ────────────────────────────────────────────────────

    def test_detects_profitable_opportunity(self):
        """Combined ask well below 1.0 after fees → opportunity."""
        strategy = self._strategy(fee_threshold=0.02, min_profit=0.005)
        # 0.45 + 0.45 = 0.90 → profit = 1 - 0.90 - 0.02 = 0.08
        is_opp, profit = strategy.detect_opportunity(yes_ask=0.45, no_ask=0.45)
        self.assertTrue(is_opp)
        self.assertAlmostEqual(profit, 0.08, places=6)

    def test_rejects_no_profit_case(self):
        """Combined ask equal to 1.0 → no opportunity after fees."""
        strategy = self._strategy(fee_threshold=0.02, min_profit=0.005)
        # 0.50 + 0.50 = 1.00 → profit = 1 - 1.00 - 0.02 = -0.02
        is_opp, profit = strategy.detect_opportunity(yes_ask=0.50, no_ask=0.50)
        self.assertFalse(is_opp)
        self.assertAlmostEqual(profit, -0.02, places=6)

    def test_rejects_above_threshold(self):
        """Combined ask > 1.0 → definitely no opportunity."""
        strategy = self._strategy(fee_threshold=0.02, min_profit=0.005)
        is_opp, profit = strategy.detect_opportunity(yes_ask=0.60, no_ask=0.50)
        self.assertFalse(is_opp)
        self.assertLess(profit, 0)

    def test_profit_below_min_threshold_rejected(self):
        """Profit above fee but below min_profit → rejected."""
        strategy = self._strategy(fee_threshold=0.02, min_profit=0.05)
        # 0.48 + 0.48 = 0.96 → profit = 1 - 0.96 - 0.02 = 0.02 < min_profit 0.05
        is_opp, profit = strategy.detect_opportunity(yes_ask=0.48, no_ask=0.48)
        self.assertFalse(is_opp)

    def test_exact_threshold_boundary(self):
        """Profit exactly equal to min_profit → opportunity (≥ threshold)."""
        strategy = self._strategy(fee_threshold=0.02, min_profit=0.03)
        # 0.45 + 0.50 = 0.95 → profit = 1 - 0.95 - 0.02 = 0.03
        is_opp, profit = strategy.detect_opportunity(yes_ask=0.45, no_ask=0.50)
        self.assertTrue(is_opp)
        self.assertAlmostEqual(profit, 0.03, places=6)

    # ── run() with mocked orderbooks ─────────────────────────────────────────

    def test_run_places_orders_on_opportunity(self):
        """run() places two market orders when opportunity is found."""
        strategy = self._strategy(fee_threshold=0.02, min_profit=0.005)
        strategy._client.get_orderbook.side_effect = [
            _make_orderbook(0.45),   # YES ask
            _make_orderbook(0.45),   # NO ask
        ]
        strategy._risk_manager.get_balance = MagicMock(return_value=1000.0)
        strategy._order_manager.place_market_order = MagicMock(return_value={"order_id": "x"})

        market = _make_market()
        strategy.run(market)

        self.assertEqual(strategy._order_manager.place_market_order.call_count, 2)

    def test_run_skips_when_no_opportunity(self):
        """run() places no orders when combined ask ≥ 1.0."""
        strategy = self._strategy()
        strategy._client.get_orderbook.side_effect = [
            _make_orderbook(0.52),
            _make_orderbook(0.52),
        ]
        strategy._order_manager.place_market_order = MagicMock()

        market = _make_market()
        strategy.run(market)

        strategy._order_manager.place_market_order.assert_not_called()

    def test_run_skips_market_without_tokens(self):
        """run() handles market missing YES/NO tokens gracefully."""
        strategy = self._strategy()
        strategy._order_manager.place_market_order = MagicMock()
        strategy.run({"condition_id": "x", "tokens": []})
        strategy._order_manager.place_market_order.assert_not_called()

    def test_run_handles_orderbook_error(self):
        """run() does not raise when orderbook fetch fails."""
        strategy = self._strategy()
        strategy._client.get_orderbook.side_effect = Exception("network error")
        # Should not raise
        strategy.run(_make_market())


class TestMidpointStrategy(unittest.TestCase):

    def _strategy(self, lookback: int = 5, deviation_threshold: float = 0.05):
        client = MagicMock()
        risk_manager = RiskManager(client=None, paper_trading=True)
        order_manager = OrderManager(paper_trading=True, risk_manager=risk_manager)
        return MidpointStrategy(
            client=client,
            order_manager=order_manager,
            risk_manager=risk_manager,
            lookback=lookback,
            deviation_threshold=deviation_threshold,
        )

    def test_no_signal_until_history_full(self):
        """Strategy waits until the lookback window is full before signalling."""
        strategy = self._strategy(lookback=5)
        strategy._client.get_midpoint.return_value = 0.50
        strategy._risk_manager.get_balance = MagicMock(return_value=1000.0)
        strategy._order_manager.place_limit_order = MagicMock()

        market = _make_market()
        for _ in range(4):
            strategy.run(market)

        strategy._order_manager.place_limit_order.assert_not_called()

    def test_buy_signal_on_downward_deviation(self):
        """Places BUY order when price drops below moving average."""
        strategy = self._strategy(lookback=5, deviation_threshold=0.03)
        # Fill history with 0.50 then drop sharply
        strategy._client.get_midpoint.return_value = 0.50
        strategy._risk_manager.get_balance = MagicMock(return_value=1000.0)
        strategy._order_manager.place_limit_order = MagicMock(return_value={"order_id": "o1"})

        market = _make_market()
        for _ in range(4):
            strategy.run(market)

        # Now price drops to 0.45 — deviation = (0.45 - 0.50)/0.50 = -10%
        strategy._client.get_midpoint.return_value = 0.45
        strategy.run(market)

        calls = strategy._order_manager.place_limit_order.call_args_list
        self.assertTrue(any(c.kwargs.get("side") == "BUY" for c in calls))

    def test_sell_signal_on_upward_deviation(self):
        """Places SELL order when price rises above moving average."""
        strategy = self._strategy(lookback=5, deviation_threshold=0.03)
        strategy._client.get_midpoint.return_value = 0.50
        strategy._risk_manager.get_balance = MagicMock(return_value=1000.0)
        strategy._order_manager.place_limit_order = MagicMock(return_value={"order_id": "o2"})

        market = _make_market()
        for _ in range(4):
            strategy.run(market)

        strategy._client.get_midpoint.return_value = 0.56
        strategy.run(market)

        calls = strategy._order_manager.place_limit_order.call_args_list
        self.assertTrue(any(c.kwargs.get("side") == "SELL" for c in calls))


class TestMarketMakingStrategy(unittest.TestCase):

    def _strategy(self, spread: float = 0.02):
        client = MagicMock()
        risk_manager = RiskManager(client=None, paper_trading=True)
        order_manager = OrderManager(paper_trading=True, risk_manager=risk_manager)
        return MarketMakingStrategy(
            client=client,
            order_manager=order_manager,
            risk_manager=risk_manager,
            spread=spread,
            order_size=10.0,
        )

    def test_places_bid_and_ask(self):
        """Places both a BUY and SELL limit order on each run."""
        strategy = self._strategy(spread=0.02)
        strategy._client.get_midpoint.return_value = 0.50
        strategy._risk_manager.get_balance = MagicMock(return_value=1000.0)
        strategy._order_manager.place_limit_order = MagicMock(return_value={"order_id": "mm1"})

        strategy.run(_make_market())

        calls = strategy._order_manager.place_limit_order.call_args_list
        sides = {c.kwargs.get("side") for c in calls}
        self.assertIn("BUY", sides)
        self.assertIn("SELL", sides)

    def test_bid_below_ask(self):
        """Bid price must be strictly less than ask price."""
        strategy = self._strategy(spread=0.02)
        strategy._client.get_midpoint.return_value = 0.50
        strategy._risk_manager.get_balance = MagicMock(return_value=1000.0)
        prices: dict[str, float] = {}
        strategy._order_manager.place_limit_order = MagicMock(
            side_effect=lambda **kw: prices.update({kw["side"]: kw["price"]}) or {}
        )

        strategy.run(_make_market())

        self.assertLess(prices["BUY"], prices["SELL"])

    def test_skips_degenerate_midpoint(self):
        """Does not post orders if midpoint is 0 or 1."""
        strategy = self._strategy()
        strategy._client.get_midpoint.return_value = 0.0
        strategy._order_manager.place_limit_order = MagicMock()

        strategy.run(_make_market())
        strategy._order_manager.place_limit_order.assert_not_called()


if __name__ == "__main__":
    unittest.main()
