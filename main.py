"""
main.py — Polymarket Trading Engine entry point
─────────────────────────────────────────────────
Usage:
  python main.py           # starts the trading loop (reads .env)
  python main.py --scan    # single scan + print opportunities, then exit
  python main.py --help    # show help
"""
from __future__ import annotations

import argparse
import asyncio
import sys

# Ensure UTF-8 output on Windows so Unicode log characters don't crash
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from engine.config import load_config
from engine.logger_setup import setup_logging


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Polymarket binary-arbitrage trading engine",
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="Run a single market scan, print opportunities, then exit (no trading).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Override DRY_RUN=true regardless of .env setting.",
    )
    return parser.parse_args()


async def _single_scan() -> None:
    """Run one scan cycle and print results without trading."""
    import logging
    from engine.market_scanner import MarketScanner
    from engine.opportunity_detector import OpportunityDetector
    from tabulate import tabulate

    logger = logging.getLogger("main.scan")
    cfg    = load_config()

    async with MarketScanner(batch_size=cfg.scan_batch_size) as scanner:
        logger.info("Discovering markets…")
        market_meta = await scanner.discover_markets(max_markets=cfg.scan_batch_size)
        logger.info("Found %d tradable markets, fetching order books…", len(market_meta))
        snapshots = await scanner.refresh_books(market_meta)
        logger.info("Fetched %d liquid markets", len(snapshots))

    detector = OpportunityDetector(min_profit_threshold=cfg.min_profit_threshold)
    signals  = detector.evaluate_many(snapshots)

    if not signals:
        print("\nNo actionable opportunities found in this scan.")
        return

    rows = [
        [
            sig.signal_type.name,
            sig.snapshot.question[:60],
            f"{sig.yes_price:.4f}",
            f"{sig.no_price:.4f}",
            f"{sig.estimated_profit:.4f}",
            sig.notes[:70],
        ]
        for sig in signals
    ]
    print(
        "\n"
        + tabulate(
            rows,
            headers=["Type", "Market", "YES price", "NO price", "Profit/notional", "Notes"],
            tablefmt="rounded_outline",
        )
    )
    print(f"\nFound {len(signals)} actionable signal(s) across {len(snapshots)} markets.")


async def _run_engine(dry_run_override: bool | None) -> None:
    """Start the continuous trading loop."""
    import os

    if dry_run_override:
        os.environ["DRY_RUN"] = "true"  # force dry-run before config is loaded

    cfg = load_config()
    setup_logging(level=cfg.log_level, log_file=cfg.log_file)

    from engine.trading_engine import TradingEngine
    engine = TradingEngine(cfg)
    await engine.run()


def main() -> None:
    args   = _parse_args()
    cfg    = load_config()
    setup_logging(level=cfg.log_level, log_file=cfg.log_file)

    try:
        if args.scan:
            asyncio.run(_single_scan())
        else:
            asyncio.run(_run_engine(dry_run_override=args.dry_run))
    except KeyboardInterrupt:
        print("\nEngine stopped by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
