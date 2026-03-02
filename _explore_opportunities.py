"""Scan for momentum, near-expiry, and wide-spread opportunities."""
import asyncio, sys
from datetime import datetime, timezone
sys.path.insert(0, ".")
import aiohttp

async def main():
    async with aiohttp.ClientSession() as s:
        async with s.get("https://gamma-api.polymarket.com/markets", params={
            "active": "true", "closed": "false", "enableOrderBook": "true",
            "limit": 500
        }) as r:
            data = await r.json()

    now = datetime.now(timezone.utc)
    momentum, near_expiry, wide_spread = [], [], []

    for m in data:
        try:
            yes_p = float(_json.loads(m.get("outcomePrices", "[0.5]"))[0])
        except Exception:
            yes_p = 0.5
        hour_chg = m.get("oneHourPriceChange") or 0
        day_chg  = m.get("oneDayPriceChange")  or 0
        spread   = float(m.get("spread") or 0)
        vol24    = float(m.get("volume24hr") or 0)
        liq      = float(m.get("liquidityNum") or 0)
        end_raw  = m.get("endDate") or m.get("endDateIso") or ""
        q        = m.get("question", "?")[:60]

        # --- momentum: >5% move in 1h with decent liquidity ---
        if abs(hour_chg) >= 0.05 and liq >= 100:
            momentum.append((abs(hour_chg), q, yes_p, hour_chg, day_chg, spread, liq))

        # --- near-expiry staleness: expires in <72h, price NOT near 0 or 1 ---
        try:
            end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            hrs_left = (end_dt - now).total_seconds() / 3600
            if 0 < hrs_left < 72 and 0.05 < yes_p < 0.95 and liq >= 50:
                near_expiry.append((hrs_left, q, yes_p, spread, vol24, liq))
        except Exception:
            pass

        # --- wide spread: >3% spread with real volume ---
        if spread >= 0.03 and vol24 >= 500 and 0.1 < yes_p < 0.9:
            wide_spread.append((spread, q, yes_p, vol24, liq))

    # ── Momentum signals ─────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  MOMENTUM (>5% 1-hour price move, liq >= $100)")
    print(f"{'='*70}")
    if momentum:
        momentum.sort(reverse=True)
        print(f"  {'Market':<58} {'YES':>5} {'1h':>7} {'1d':>7} {'spr':>5} {'liq':>8}")
        print(f"  {'-'*93}")
        for _, q, yes_p, h, d, spr, liq in momentum[:15]:
            print(f"  {q:<58} {yes_p:>5.3f} {h:>+7.3f} {d:>+7.3f} {spr:>5.3f} {liq:>8.0f}")
    else:
        print("  None found.")

    # ── Near-expiry staleness ─────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  NEAR-EXPIRY STALENESS (expires <72h, price 0.05-0.95)")
    print(f"{'='*70}")
    if near_expiry:
        near_expiry.sort()
        print(f"  {'Market':<58} {'hrs':>5} {'YES':>5} {'spr':>5} {'vol24':>8} {'liq':>8}")
        print(f"  {'-'*93}")
        for hrs, q, yes_p, spr, vol24, liq in near_expiry[:15]:
            print(f"  {q:<58} {hrs:>5.1f} {yes_p:>5.3f} {spr:>5.3f} {vol24:>8.0f} {liq:>8.0f}")
    else:
        print("  None found.")

    # ── Wide spread opportunities ─────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"  WIDE SPREAD (>3%, vol24 >= $500)  — passive market-making targets")
    print(f"{'='*70}")
    if wide_spread:
        wide_spread.sort(reverse=True)
        print(f"  {'Market':<58} {'spr':>5} {'YES':>5} {'vol24':>9} {'liq':>8}")
        print(f"  {'-'*85}")
        for spr, q, yes_p, vol24, liq in wide_spread[:15]:
            print(f"  {q:<58} {spr:>5.3f} {yes_p:>5.3f} {vol24:>9.0f} {liq:>8.0f}")
    else:
        print("  None found.")

    print()

asyncio.run(main())
