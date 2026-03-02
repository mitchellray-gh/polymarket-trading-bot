"""
Profit-per-second estimator.

Runs N live scan cycles, records every market's deviation from $1.00,
and calculates:
  - Scan rate (cycles/sec)
  - Opportunity frequency (how often a gap > threshold appears)
  - Average profit per opportunity
  - Estimated profit per second (if every signal were filled)
  - Realistic profit per second (after 0.1% taker fee each leg)
"""
import asyncio
import sys
import time
from collections import defaultdict

sys.path.insert(0, ".")
from engine.market_scanner import MarketScanner

CYCLES         = 10      # how many hot-path refresh cycles to measure
NOTIONAL_USDC  = 100.0   # assumed trade size per leg
TAKER_FEE_PCT  = 0.001   # 0.1% per order (Polymarket standard)


async def main():
    print(f"\nRunning {CYCLES} live scan cycles...\n")

    async with MarketScanner() as s:
        # ── Phase 1: discover market list ──────────────────────────────────
        t0 = time.monotonic()
        meta = await s.discover_markets(max_markets=500)
        discover_ms = (time.monotonic() - t0) * 1000
        print(f"  Market discovery : {len(meta)} markets  ({discover_ms:.0f} ms, runs every 60 s)")

        # warm-up cycle (not counted)
        await s.refresh_books(meta)

        # ── Phase 2: N timed cycles ────────────────────────────────────────
        cycle_times   = []
        buy_gaps   = []   # 1 - combined_ask  (positive = BUY_BOTH profit)
        sell_gaps  = []   # combined_bid - 1  (positive = SELL_BOTH profit)
        all_buy_gaps_by_cycle  = []
        all_sell_gaps_by_cycle = []

        for i in range(CYCLES):
            t0 = time.monotonic()
            snaps = await s.refresh_books(meta)
            elapsed = time.monotonic() - t0
            cycle_times.append(elapsed)

            cycle_buys  = []
            cycle_sells = []
            for sn in snaps:
                ca = sn.combined_ask
                cb = sn.combined_bid
                if ca is not None:
                    gap = round(1.0 - ca, 6)
                    buy_gaps.append(gap)
                    if gap > 0:
                        cycle_buys.append(gap)
                if cb is not None:
                    gap = round(cb - 1.0, 6)
                    sell_gaps.append(gap)
                    if gap > 0:
                        cycle_sells.append(gap)

            all_buy_gaps_by_cycle.append(cycle_buys)
            all_sell_gaps_by_cycle.append(cycle_sells)

            total_opps = len(cycle_buys) + len(cycle_sells)
            print(
                f"  Cycle {i+1:2d}: {elapsed*1000:5.0f} ms  "
                f"{len(snaps):3d} markets  "
                f"BUY_BOTH={len(cycle_buys)}  SELL_BOTH={len(cycle_sells)}  "
                f"({'signal!' if total_opps else 'no signal'})"
            )

    # ── Analysis ───────────────────────────────────────────────────────────
    avg_cycle_s  = sum(cycle_times) / len(cycle_times)
    cycles_per_s = 1.0 / avg_cycle_s

    print(f"\n{'─'*60}")
    print(f"  Scan performance")
    print(f"{'─'*60}")
    print(f"  Avg cycle time   : {avg_cycle_s*1000:.0f} ms")
    print(f"  Min cycle time   : {min(cycle_times)*1000:.0f} ms")
    print(f"  Cycles / second  : {cycles_per_s:.2f}")

    # Spread distribution (shows how close markets are to $1.00)
    pos_buy  = [g for g in buy_gaps  if g > 0]
    pos_sell = [g for g in sell_gaps if g > 0]
    neg_buy  = [g for g in buy_gaps  if g <= 0]

    print(f"\n{'─'*60}")
    print(f"  Market pricing distribution ({CYCLES} cycles × {len(buy_gaps)//CYCLES} markets)")
    print(f"{'─'*60}")
    print(f"  Markets w/ combined_ask < 1.00  (BUY_BOTH edge) : {len(pos_buy)}")
    print(f"  Markets w/ combined_ask = 1.00                  : {buy_gaps.count(0.0)}")
    print(f"  Markets w/ combined_ask > 1.00  (no buy edge)   : {len(neg_buy)}")
    if pos_buy:
        print(f"  Best BUY_BOTH gap seen           : ${max(pos_buy):.6f}")
        print(f"  Avg BUY_BOTH gap (when > 0)      : ${sum(pos_buy)/len(pos_buy):.6f}")
    if pos_sell:
        print(f"  Best SELL_BOTH gap seen          : ${max(pos_sell):.6f}")

    # Typical gap most markets sit at
    all_abs = [abs(g) for g in buy_gaps]
    avg_abs = sum(all_abs) / len(all_abs)
    print(f"  Avg |deviation| from $1.00       : ${avg_abs:.6f}  ({avg_abs*100:.4f}%)")

    # Profit estimate
    print(f"\n{'─'*60}")
    print(f"  Profit estimate  (notional = ${NOTIONAL_USDC:.0f} per leg)")
    print(f"{'─'*60}")

    opps_per_cycle = (len(pos_buy) + len(pos_sell)) / CYCLES
    opps_per_sec   = opps_per_cycle * cycles_per_s

    if pos_buy or pos_sell:
        all_gaps     = pos_buy + pos_sell
        avg_gap      = sum(all_gaps) / len(all_gaps)
        gross_profit = avg_gap * NOTIONAL_USDC
        fee_cost     = NOTIONAL_USDC * TAKER_FEE_PCT * 2   # 2 legs
        net_profit   = gross_profit - fee_cost

        print(f"  Opportunities / cycle            : {opps_per_cycle:.1f}")
        print(f"  Opportunities / second           : {opps_per_sec:.2f}")
        print(f"  Avg raw gap                      : ${avg_gap:.6f}")
        print(f"  Gross profit / trade @ ${NOTIONAL_USDC:.0f}       : ${gross_profit:.4f}")
        print(f"  Taker fee (0.1% × 2 legs)        : -${fee_cost:.4f}")
        print(f"  Net profit / trade               : ${net_profit:.4f}")
        if net_profit > 0:
            print(f"  Est. net profit / second         : ${net_profit * opps_per_sec:.4f}")
            print(f"  Est. net profit / hour           : ${net_profit * opps_per_sec * 3600:.2f}")
        else:
            print(f"  Net profit / trade is NEGATIVE after fees — gap too small to trade.")
            breakeven = fee_cost / NOTIONAL_USDC
            print(f"  Break-even gap required          : ${breakeven:.6f} ({breakeven*100:.3f}%)")
    else:
        print(f"  No gaps > $0 observed in {CYCLES} cycles.")
        print(f"  All markets priced with combined_ask >= 1.00 and combined_bid <= 1.00.")
        breakeven = NOTIONAL_USDC * TAKER_FEE_PCT * 2 / NOTIONAL_USDC
        print(f"  Break-even gap required (fees)   : ${breakeven:.6f} ({breakeven*100:.3f}%)")
        print(f"\n  The market is currently fully efficient — no arb available.")
        print(f"  Engine is ready and will fire the instant a gap opens.")

    print(f"\n{'─'*60}\n")


asyncio.run(main())
