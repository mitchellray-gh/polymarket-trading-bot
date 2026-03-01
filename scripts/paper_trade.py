"""
Paper trading script — simulates trades without real orders.

Tracks hypothetical P&L and logs all would-be trades to a CSV file.

Usage
-----
    python scripts/paper_trade.py --strategy arbitrage --duration 3600
    python scripts/paper_trade.py --strategy midpoint --scan-interval 5
"""

from __future__ import annotations

import argparse
import csv
import os
import signal
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from bot.client import PolymarketClient
from bot.market_scanner import MarketScanner
from bot.execution.order_manager import OrderManager, OrderStatus
from bot.execution.risk_manager import RiskManager
from bot.strategies.arbitrage import ArbitrageStrategy
from bot.strategies.midpoint import MidpointStrategy
from bot.strategies.market_making import MarketMakingStrategy
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
    _shutdown = True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket Paper Trading")
    parser.add_argument(
        "--strategy",
        choices=list(_STRATEGY_MAP),
        default="arbitrage",
    )
    parser.add_argument(
        "--scan-interval",
        type=int,
        default=settings.SCAN_INTERVAL,
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=0,
        help="Run for this many seconds then stop (0 = run forever)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="paper_trades.csv",
        help="CSV file to write simulated trades to",
    )
    return parser.parse_args()


def write_trades_to_csv(orders: list[dict], filepath: str) -> None:
    if not orders:
        return
    fieldnames = ["timestamp", "order_id", "token_id", "side", "type", "price", "size", "status"]
    with open(filepath, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for order in orders:
            writer.writerow({
                "timestamp": datetime.fromtimestamp(order.get("placed_at", 0)).isoformat(),
                "order_id": order.get("order_id", ""),
                "token_id": order.get("token_id", ""),
                "side": order.get("side", ""),
                "type": order.get("type", ""),
                "price": order.get("price", ""),
                "size": order.get("size", ""),
                "status": order.get("status", ""),
            })
    logger.info("Trades written to %s", filepath)


def main() -> None:
    args = parse_args()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info(
        "=== PAPER TRADING: strategy=%s interval=%ds ===",
        args.strategy,
        args.scan_interval,
    )

    client = PolymarketClient()
    risk_manager = RiskManager(client=client, paper_trading=True)
    order_manager = OrderManager(client=client, risk_manager=risk_manager, paper_trading=True)
    scanner = MarketScanner(client=client)

    strategy_cls = _STRATEGY_MAP[args.strategy]
    strategy = strategy_cls(
        client=client,
        order_manager=order_manager,
        risk_manager=risk_manager,
    )

    start_time = time.monotonic()

    while not _shutdown:
        if args.duration > 0 and time.monotonic() - start_time >= args.duration:
            logger.info("Duration reached — stopping")
            break

        try:
            markets = scanner.get_active_markets()
            for market in markets:
                if _shutdown:
                    break
                strategy.run(market)
        except Exception as exc:
            logger.exception("Error during paper trading scan: %s", exc)

        time.sleep(args.scan_interval)

    # Summary
    all_orders = order_manager.get_all_orders()
    filled = [o for o in all_orders if o["status"] == OrderStatus.FILLED]
    total_spent = sum(o.get("size", 0) for o in filled)

    print(f"\n=== Paper Trading Summary ===")
    print(f"Total simulated orders: {len(all_orders)}")
    print(f"Filled: {len(filled)}")
    print(f"Total USDC deployed (simulated): ${total_spent:.2f}")

    write_trades_to_csv(all_orders, args.output)


if __name__ == "__main__":
    main()
