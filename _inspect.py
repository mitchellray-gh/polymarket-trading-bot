"""Quick inspection of live market data — shows markets closest to arb."""
import asyncio
import sys
sys.path.insert(0, ".")
from engine.market_scanner import MarketScanner


async def main():
    async with MarketScanner() as s:
        meta = await s.discover_markets(max_markets=500)
        snaps = await s.refresh_books(meta)

    liquid = [
        (sn, sn.combined_ask, sn.combined_bid)
        for sn in snaps
        if sn.combined_ask is not None and sn.combined_bid is not None
    ]

    print(f"\n{len(liquid)} liquid markets\n")

    # --- cheapest combined ask (BUY_BOTH candidates) ---
    by_ask = sorted(liquid, key=lambda x: x[1])
    print(f"  {'Market':<55} {'askYES':>7} {'askNO':>7} {'comb_ask':>9}")
    print("  " + "-" * 82)
    for sn, ca, cb in by_ask[:12]:
        ya = sn.yes_best_ask or 0
        na = sn.no_best_ask or 0
        flag = " <-- BUY BOTH" if ca < 1.00 else ""
        print(f"  {sn.question[:55]:<55} {ya:>7.4f} {na:>7.4f} {ca:>9.4f}{flag}")

    print()

    # --- highest combined bid (SELL_BOTH candidates) ---
    by_bid = sorted(liquid, key=lambda x: -x[2])
    print(f"  {'Market':<55} {'bidYES':>7} {'bidNO':>7} {'comb_bid':>9}")
    print("  " + "-" * 82)
    for sn, ca, cb in by_bid[:12]:
        yb = sn.yes_best_bid or 0
        nb = sn.no_best_bid or 0
        flag = " <-- SELL BOTH" if cb > 1.00 else ""
        print(f"  {sn.question[:55]:<55} {yb:>7.4f} {nb:>7.4f} {cb:>9.4f}{flag}")


asyncio.run(main())
