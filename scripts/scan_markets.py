"""
Standalone market scanner script.

Prints a list of active markets and any detected arbitrage opportunities.

Usage
-----
    python scripts/scan_markets.py
    python scripts/scan_markets.py --min-volume 1000
"""

from __future__ import annotations

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.client import PolymarketClient
from bot.market_scanner import MarketScanner
from bot.utils.logger import get_logger

logger = get_logger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket Market Scanner")
    parser.add_argument("--min-volume", type=float, default=0.0,
                        help="Minimum market volume filter (default: 0)")
    parser.add_argument("--category", type=str, default=None,
                        help="Filter by market category")
    parser.add_argument("--arbitrage", action="store_true",
                        help="Show only arbitrage opportunities")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    client = PolymarketClient()
    scanner = MarketScanner(client=client)

    markets = scanner.get_active_markets(
        min_volume=args.min_volume,
        category=args.category,
    )

    print(f"\nFound {len(markets)} active markets\n")

    if args.arbitrage:
        opportunities = scanner.scan_arbitrage(markets)
        if not opportunities:
            print("No arbitrage opportunities found.")
        else:
            print(f"{'#':<4} {'Question':<60} {'YES Ask':>8} {'NO Ask':>8} {'Profit':>8}")
            print("-" * 92)
            for i, opp in enumerate(opportunities, 1):
                print(
                    f"{i:<4} {opp.question[:60]:<60} "
                    f"{opp.yes_ask:>8.4f} {opp.no_ask:>8.4f} "
                    f"{opp.expected_profit:>8.4f}"
                )
    else:
        print(f"{'#':<4} {'Question':<70} {'Volume':>12}")
        print("-" * 90)
        for i, market in enumerate(markets[:50], 1):
            print(
                f"{i:<4} {market.get('question', '')[:70]:<70} "
                f"{float(market.get('volume', 0)):>12,.0f}"
            )
        if len(markets) > 50:
            print(f"  ... and {len(markets) - 50} more")


if __name__ == "__main__":
    main()
