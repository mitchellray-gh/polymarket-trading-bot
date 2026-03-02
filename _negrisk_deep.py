"""
_negrisk_deep.py
─────────────────
Deep-dive on the negRisk maker-order exploit.

Key insight: Polymarket maker fees = 0%.
So if midprice_sum > 1.00 for a negRisk event, posting LIMIT SELL orders
at or below the current ask earns the overround risk-free (once all legs fill).

This script:
1. Shows exact bid/mid/ask breakdown for every multi-leg negRisk event
2. Identifies which events have profitable maker-sell opportunities
3. Calculates exact $/trade and $/day at realistic fill rates
"""
import asyncio, aiohttp, json, sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MAKER_FEE = 0.000   # Polymarket maker fee = 0%
TAKER_FEE = 0.001   # 0.1% taker

async def main():
    async with aiohttp.ClientSession() as session:
        # Fetch all negRisk markets
        r = await session.get(
            "https://gamma-api.polymarket.com/markets",
            params={"negRisk": "true", "active": "true", "closed": "false", "limit": "500"}
        )
        markets = await r.json(content_type=None)
        print(f"Total negRisk markets fetched: {len(markets)}")

        # Group by event
        events = {}
        for m in markets:
            evs  = m.get("events", [])
            eid  = evs[0]["id"]    if evs else m.get("conditionId", "solo")
            etit = evs[0]["title"] if evs else m.get("question", "?")
            if eid not in events:
                events[eid] = {"title": etit, "markets": []}
            events[eid]["markets"].append(m)

        multi = {k: v for k, v in events.items() if len(v["markets"]) >= 2}

        results = []
        for eid, ev in multi.items():
            mids, bids, asks = [], [], []
            ok = True
            for m in ev["markets"]:
                try:
                    prices = json.loads(m.get("outcomePrices", "[]"))
                    mid  = float(prices[0])
                    bid  = float(m.get("bestBid") or 0)
                    ask  = float(m.get("bestAsk") or 0)
                    if bid <= 0 or ask <= 0:
                        ok = False; break
                    mids.append(mid)
                    bids.append(bid)
                    asks.append(ask)
                except Exception:
                    ok = False; break
            if not ok or not mids:
                continue

            mid_sum = sum(mids)
            bid_sum = sum(bids)
            ask_sum = sum(asks)
            n       = len(mids)

            # MAKER SELL: post limit sells at midprice, collect mid_sum when filled
            # Fee = 0 (maker). At resolution, pay $1.00.
            maker_sell_profit = mid_sum - 1.0  # no fees

            # TAKER BUY: pay ask_sum now, redeem $1.00 at resolution
            taker_buy_profit  = 1.0 - ask_sum - TAKER_FEE * n

            results.append({
                "title":             ev["title"],
                "n":                 n,
                "mid_sum":           round(mid_sum, 5),
                "bid_sum":           round(bid_sum, 5),
                "ask_sum":           round(ask_sum, 5),
                "maker_sell_profit": round(maker_sell_profit, 5),
                "taker_buy_profit":  round(taker_buy_profit, 5),
                "vol24":             sum(float(m.get("volume24hr") or 0) for m in ev["markets"]),
                "liq":               sum(float(m.get("liquidityNum") or 0) for m in ev["markets"]),
                "markets":           ev["markets"],
            })

        # Sort by maker_sell_profit descending
        results.sort(key=lambda x: -x["maker_sell_profit"])

        print("\n" + "="*70)
        print("  MAKER-ORDER SELL BUNDLE — guaranteed if mid_sum > 1.00, fee=0%")
        print("="*70)
        print(f"  {'Event':<42} {'N':>3} {'mid_sum':>8} {'bid_sum':>8} {'ask_sum':>8} {'maker$':>8} {'taker$':>8}")
        print(f"  {'-'*42} {'-'*3} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")
        for r in results[:20]:
            tag = " ◄" if r["maker_sell_profit"] > 0.005 else ""
            print(f"  {r['title'][:42]:<42} {r['n']:>3} {r['mid_sum']:>8.4f} "
                  f"{r['bid_sum']:>8.4f} {r['ask_sum']:>8.4f} "
                  f"{r['maker_sell_profit']:>+8.4f} {r['taker_buy_profit']:>+8.4f}{tag}")

        print("\n" + "="*70)
        print("  DETAILED ANALYSIS — events with maker_sell_profit > 0.003")
        print("="*70)

        profitable = [r for r in results if r["maker_sell_profit"] > 0.003]
        if not profitable:
            print("  None found above 0.003 threshold — checking > 0.001...")
            profitable = [r for r in results if r["maker_sell_profit"] > 0.001]

        for r in profitable[:5]:
            pct   = r["maker_sell_profit"] * 100
            notional = 1000
            gross = r["maker_sell_profit"] * notional
            # Fill rate: on a negRisk event, one outcome WILL resolve — all sells eventually fill
            # Conservative: avg days to resolution = 30 days
            # Aggressive: active trading event, fills within hours
            # We use vol24/liq as fill_rate proxy
            avg_liq = r["liq"] / max(r["n"], 1)
            avg_vol = r["vol24"] / max(r["n"], 1)
            fill_rate_day = min(avg_vol / max(avg_liq, 1), 1.0)
            days_to_fill = 1 / max(fill_rate_day, 0.001)

            print(f"\n  >>> {r['title'][:65]}")
            print(f"      Legs={r['n']}  mid_sum={r['mid_sum']:.4f}  bid_sum={r['bid_sum']:.4f}  ask_sum={r['ask_sum']:.4f}")
            print(f"      Overround       = {pct:.2f}%  (sum of midprices above $1.00)")
            print(f"      Maker sell edge  = {r['maker_sell_profit']:+.5f} per $1 notional  (fee=0%)")
            print(f"      Taker buy edge   = {r['taker_buy_profit']:+.5f} per $1 notional  (fee=0.1%/leg)")
            print(f"      On ${notional} bundle: gross maker profit = ${gross:.2f}")
            print(f"      Event liquidity = ${r['liq']:,.0f}  vol24 = ${r['vol24']:,.0f}")
            print(f"      Fill rate       ~ {fill_rate_day*100:.1f}%/day  → fills in ~{days_to_fill:.1f} days avg")
            print(f"      $/day           = ${gross / max(days_to_fill,1):.4f}")
            print(f"      $/sec           = ${gross / max(days_to_fill,1) / 86400:.7f}")
            print()
            print(f"      HOW TO EXECUTE:")
            print(f"        For each of the {r['n']} YES tokens:")
            print(f"          POST limit SELL order at the current midprice (bid+ask)/2")
            print(f"          Maker fee = 0% → keep full spread")
            print(f"          When all {r['n']} legs fill, you've sold bundle for ${r['mid_sum']:.4f}")
            print(f"          At resolution, exactly 1 leg settles at $1.00 → you pay $1.00")
            print(f"          Net profit = ${r['mid_sum']:.4f} - $1.0000 = ${r['maker_sell_profit']:.4f} per $1 notional")

            # Per-leg breakdown
            print(f"\n      PER-LEG DETAIL:")
            for m in r["markets"]:
                try:
                    prices = json.loads(m.get("outcomePrices", "[]"))
                    mid  = float(prices[0])
                    bid  = float(m.get("bestBid") or 0)
                    ask  = float(m.get("bestAsk") or 0)
                    vol  = float(m.get("volume24hr") or 0)
                    q    = m.get("question","?")[:55]
                    print(f"        bid={bid:.3f} mid={mid:.3f} ask={ask:.3f} vol24=${vol:,.0f}  {q}")
                except Exception:
                    pass

        # Summary
        print("\n" + "="*70)
        print("  THE CORE EXPLOIT — why this works")
        print("="*70)
        print("""
  Polymarket negRisk events: exactly ONE outcome resolves YES.
  The sum of all YES midprices SHOULD equal 1.00 exactly.
  In practice it's often 1.02–1.10 due to:
    - Bid-ask spread asymmetry (market makers lean asks higher)
    - Lazy re-quoting after news
    - Low-liquidity long-tail outcomes priced above zero

  TAKER orders: pay 0.1% fee per leg → 6 legs = 0.6% total → eats most edge
  MAKER orders: pay 0% fee             → keep the full overround

  The strategy:
    1. Find negRisk event where sum(midprices) > 1.00 + epsilon
    2. POST limit SELL orders on every YES leg at the midprice (or just below ask)
    3. Orders sit in the book; buyers fill them over time
    4. Once all legs fill, collect sum > 1.00
    5. When event resolves, pay $1.00 on the winning leg
    6. Profit = sum_collected - 1.00

  Risk: fills are NOT simultaneous — if only some legs fill and prices move,
  you're partially exposed. Mitigation: only execute on high-volume events
  where all legs fill within hours.
""")

asyncio.run(main())
