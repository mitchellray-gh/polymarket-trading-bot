"""
main.py — Polymarket Trading Engine entry point
─────────────────────────────────────────────────
Usage:
  python main.py                  # starts the trading loop (reads .env)
  python main.py --scan           # binary-arb single scan, then exit
  python main.py --advanced-scan  # multi-strategy scan (negRisk / near-expiry / MM), then exit
  python main.py --dry-run        # live loop but simulated orders
  python main.py --help           # show help
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
        "--advanced-scan",
        action="store_true",
        help="Run the advanced multi-strategy scanner (negRisk / near-expiry / MM) once and exit.",
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


async def _advanced_scan() -> None:
    """
    Run all three advanced strategies and pretty-print results.
    Requires no API credentials (read-only Gamma + CLOB public endpoints).
    """
    import logging
    import aiohttp
    from tabulate import tabulate
    from engine.advanced_detector import run_advanced_scan

    logging.getLogger("engine").setLevel(logging.WARNING)  # suppress debug noise
    connector = aiohttp.TCPConnector(limit=50)
    async with aiohttp.ClientSession(connector=connector) as session:
        result = await run_advanced_scan(session)

    # ── 1. negRisk overround ─────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  STRATEGY 1 — negRisk OVERROUND  (mechanical arb if net_profit > 0)")
    print("=" * 70)
    if result.negrisk:
        rows = [
            [
                sig.direction,
                sig.event_title[:50],
                sig.n_legs,
                f"{sig.ask_sum:.4f}",
                f"{sig.bid_sum:.4f}",
                f"{sig.net_profit:+.4f}",
            ]
            for sig in result.negrisk
        ]
        print(tabulate(rows,
            headers=["Direction", "Event", "Legs", "ask_sum", "bid_sum", "Net profit/¢"],
            tablefmt="rounded_outline"))
    else:
        print("  No negRisk arb found at current order-book prices.")

    # ── 2. Near-expiry mispricing ─────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  STRATEGY 2 — NEAR-EXPIRY MISPRICING  (directional, check news!)")
    print("=" * 70)
    if result.near_expiry:
        rows = [
            [
                sig.question[:52],
                f"{sig.yes_price:.3f}",
                f"{sig.hours_left:.1f} h",
                f"${sig.volume_24h:,.0f}",
                f"${sig.liquidity:,.0f}",
                sig.end_date,
            ]
            for sig in result.near_expiry
        ]
        print(tabulate(rows,
            headers=["Market", "YES", "Left", "Vol24h", "Liq", "Expiry"],
            tablefmt="rounded_outline"))
        print(f"\n  *** {len(result.near_expiry)} market(s) expiring soon with unsettled prices! ***")
        print("  Action: verify outcomes via news/data and trade the stale side.")
    else:
        print("  No near-expiry mispricing found.")

    # ── 3. Wide-spread market making ─────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  STRATEGY 3 — WIDE-SPREAD MARKET MAKING  (passive limit orders)")
    print("=" * 70)
    if result.market_maker:
        rows = [
            [
                sig.question[:48],
                f"{sig.best_bid:.3f}",
                f"{sig.best_ask:.3f}",
                f"{sig.spread:.3f}",
                f"{sig.suggested_bid:.3f}",
                f"{sig.suggested_ask:.3f}",
                f"${sig.volume_24h:,.0f}",
            ]
            for sig in result.market_maker[:20]  # top 20 by spread
        ]
        print(tabulate(rows,
            headers=["Market", "Bid", "Ask", "Spread", "My bid", "My ask", "Vol24h"],
            tablefmt="rounded_outline"))
        print(f"\n  Post limit orders at 'My bid' / 'My ask' to earn the spread on fills.")
    else:
        print("  No wide-spread MM opportunities found.")

    print(f"\n  Scan completed in {result.elapsed_ms:.0f} ms.\n")


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
        elif args.advanced_scan:
            asyncio.run(_advanced_scan())
        else:
            asyncio.run(_run_engine(dry_run_override=args.dry_run))
    except KeyboardInterrupt:
        print("\nEngine stopped by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
