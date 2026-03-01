"""
Main entry point for the Polymarket trading bot.

Usage
-----
    python scripts/run_bot.py --strategy arbitrage --scan-interval 10
    python scripts/run_bot.py --strategy midpoint --paper
    python scripts/run_bot.py --strategy market_making
"""

from __future__ import annotations

import argparse
import signal
import sys
import time

# Ensure package root is on the path when run as a script
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from bot.client import PolymarketClient
from bot.market_scanner import MarketScanner
from bot.execution.order_manager import OrderManager
from bot.execution.risk_manager import RiskManager
from bot.strategies.arbitrage import ArbitrageStrategy
from bot.strategies.midpoint import MidpointStrategy
from bot.strategies.market_making import MarketMakingStrategy
from bot.monitoring import alerts
from bot.utils.logger import get_logger

logger = get_logger(__name__)

_STRATEGY_MAP = {
    "arbitrage": ArbitrageStrategy,
    "midpoint": MidpointStrategy,
    "market_making": MarketMakingStrategy,
}

_shutdown = False


def _handle_signal(signum: int, frame: object) -> None:  # noqa: ARG001
    global _shutdown
    logger.info("Shutdown signal received — stopping after current iteration")
    _shutdown = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket Trading Bot")
    parser.add_argument(
        "--strategy",
        choices=list(_STRATEGY_MAP),
        default="arbitrage",
        help="Trading strategy to use (default: arbitrage)",
    )
    parser.add_argument(
        "--paper",
        action="store_true",
        help="Run in paper-trading mode (no real orders)",
    )
    parser.add_argument(
        "--scan-interval",
        type=int,
        default=settings.SCAN_INTERVAL,
        help=f"Seconds between market scans (default: {settings.SCAN_INTERVAL})",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    paper = args.paper or settings.PAPER_TRADING

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if paper:
        logger.info("=== PAPER TRADING MODE — no real orders will be placed ===")

    logger.info("Starting bot: strategy=%s interval=%ds paper=%s",
                args.strategy, args.scan_interval, paper)

    client = PolymarketClient()
    risk_manager = RiskManager(client=client, paper_trading=paper)
    order_manager = OrderManager(client=client, risk_manager=risk_manager, paper_trading=paper)
    scanner = MarketScanner(client=client)

    strategy_cls = _STRATEGY_MAP[args.strategy]
    strategy = strategy_cls(
        client=client,
        order_manager=order_manager,
        risk_manager=risk_manager,
    )

    logger.info("Initialised strategy: %s", strategy.name())

    while not _shutdown:
        try:
            balance = risk_manager.get_balance()
            logger.info("Balance: $%.2f", balance)

            if risk_manager.check_stop_loss(balance):
                alerts.alert_stop_loss(balance, risk_manager._starting_balance or balance)
                logger.error("Stop-loss triggered — halting bot")
                break

            markets = scanner.get_active_markets()
            logger.info("Scanning %d markets with strategy: %s", len(markets), args.strategy)

            for market in markets:
                if _shutdown:
                    break
                try:
                    strategy.run(market)
                except Exception as exc:
                    alerts.alert_error(f"strategy.run [{market.get('condition_id')}]", exc)

        except Exception as exc:
            alerts.alert_error("main loop", exc)
            logger.exception("Unexpected error in main loop")

        if not _shutdown:
            logger.debug("Sleeping %ds until next scan", args.scan_interval)
            time.sleep(args.scan_interval)

    order_manager.cancel_all_orders()
    logger.info("Bot stopped gracefully")


if __name__ == "__main__":
    main()
