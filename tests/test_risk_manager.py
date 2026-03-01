"""
Unit tests for the risk manager.
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock

from bot.execution.risk_manager import RiskManager


class TestPositionSizing(unittest.TestCase):

    def _rm(self, max_exposure_pct=0.5, max_per_market_pct=0.1):
        return RiskManager(
            client=None,
            max_exposure_pct=max_exposure_pct,
            max_per_market_pct=max_per_market_pct,
            paper_trading=True,
        )

    def test_basic_position_size(self):
        """Basic sizing: constrained by per-market limit."""
        rm = self._rm(max_exposure_pct=0.5, max_per_market_pct=0.1)
        size = rm.position_size(balance=1000.0, market_id="m1")
        # max_per_market = 100, max_total = 500 → capped at 100
        self.assertAlmostEqual(size, 100.0)

    def test_zero_balance(self):
        """Zero balance → zero size."""
        rm = self._rm()
        self.assertEqual(rm.position_size(balance=0.0), 0.0)

    def test_exposure_limits_reduce_size(self):
        """After deploying capital, available size shrinks."""
        rm = self._rm(max_exposure_pct=0.5, max_per_market_pct=0.1)
        # Deploy 80 out of 100 allowed for m1
        rm.record_order("m1", 80.0)
        size = rm.position_size(balance=1000.0, market_id="m1")
        self.assertAlmostEqual(size, 20.0)

    def test_fully_deployed_market_returns_zero(self):
        """When per-market limit is exhausted, size is zero."""
        rm = self._rm(max_exposure_pct=0.5, max_per_market_pct=0.1)
        rm.record_order("m1", 100.0)  # full per-market limit
        size = rm.position_size(balance=1000.0, market_id="m1")
        self.assertEqual(size, 0.0)

    def test_total_exposure_caps_size(self):
        """When total exposure is exhausted, size is zero regardless of per-market."""
        rm = self._rm(max_exposure_pct=0.5, max_per_market_pct=0.1)
        rm._total_exposure = 500.0  # max_total exhausted
        size = rm.position_size(balance=1000.0, market_id="new_market")
        self.assertEqual(size, 0.0)

    def test_release_increases_available_capacity(self):
        """Releasing exposure makes capacity available again."""
        rm = self._rm()
        rm.record_order("m1", 80.0)
        rm.release_order("m1", 50.0)
        size = rm.position_size(balance=1000.0, market_id="m1")
        # remaining_market = 100 - 30 = 70
        self.assertAlmostEqual(size, 70.0)

    def test_kelly_sizing(self):
        """Kelly criterion produces a reasonable non-zero size."""
        rm = self._rm()
        size = rm.position_size(
            balance=1000.0,
            market_id="m_kelly",
            kelly_fraction=0.25,
            win_prob=0.60,
            win_payout=2.0,  # 2x payout (e.g., buy YES at 0.50)
        )
        self.assertGreater(size, 0.0)

    def test_kelly_negative_edge_returns_zero(self):
        """Kelly with negative edge → zero size."""
        rm = self._rm()
        size = rm.position_size(
            balance=1000.0,
            market_id="m_bad",
            kelly_fraction=0.25,
            win_prob=0.30,
            win_payout=1.2,
        )
        self.assertEqual(size, 0.0)


class TestRateLimiting(unittest.TestCase):

    def test_allows_orders_within_limit(self):
        rm = RiskManager(client=None, max_orders_per_minute=5, paper_trading=True)
        for _ in range(5):
            self.assertTrue(rm.check_rate_limit())

    def test_blocks_after_limit_exceeded(self):
        rm = RiskManager(client=None, max_orders_per_minute=3, paper_trading=True)
        for _ in range(3):
            rm.check_rate_limit()
        self.assertFalse(rm.check_rate_limit())


class TestStopLoss(unittest.TestCase):

    def test_no_trigger_on_first_call(self):
        """First call sets the starting balance; never triggers."""
        rm = RiskManager(client=None, stop_loss_pct=0.05, paper_trading=True)
        self.assertFalse(rm.check_stop_loss(1000.0))

    def test_no_trigger_within_threshold(self):
        rm = RiskManager(client=None, stop_loss_pct=0.05, paper_trading=True)
        rm.check_stop_loss(1000.0)
        # 3% loss — below 5% threshold
        self.assertFalse(rm.check_stop_loss(970.0))

    def test_triggers_at_threshold(self):
        rm = RiskManager(client=None, stop_loss_pct=0.05, paper_trading=True)
        rm.check_stop_loss(1000.0)
        # 5% loss — exactly at threshold
        self.assertTrue(rm.check_stop_loss(950.0))

    def test_triggers_above_threshold(self):
        rm = RiskManager(client=None, stop_loss_pct=0.05, paper_trading=True)
        rm.check_stop_loss(1000.0)
        self.assertTrue(rm.check_stop_loss(900.0))

    def test_no_trigger_when_balance_increases(self):
        rm = RiskManager(client=None, stop_loss_pct=0.05, paper_trading=True)
        rm.check_stop_loss(1000.0)
        self.assertFalse(rm.check_stop_loss(1100.0))


class TestGetBalance(unittest.TestCase):

    def test_returns_client_balance(self):
        client = MagicMock()
        client.get_balance.return_value = 500.0
        rm = RiskManager(client=client, paper_trading=True)
        self.assertEqual(rm.get_balance(), 500.0)

    def test_returns_zero_when_client_is_none(self):
        rm = RiskManager(client=None, paper_trading=True)
        self.assertEqual(rm.get_balance(), 0.0)

    def test_returns_zero_on_client_error(self):
        client = MagicMock()
        client.get_balance.side_effect = Exception("network error")
        rm = RiskManager(client=client, paper_trading=True)
        self.assertEqual(rm.get_balance(), 0.0)


if __name__ == "__main__":
    unittest.main()
