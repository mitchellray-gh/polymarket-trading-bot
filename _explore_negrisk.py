import asyncio, sys, json
sys.path.insert(0, ".")
import aiohttp

async def main():
    async with aiohttp.ClientSession() as s:
        async with s.get("https://gamma-api.polymarket.com/markets", params={
            "active": "true", "closed": "false", "enableOrderBook": "true",
            "limit": 200, "negRisk": "true"
        }) as r:
            data = await r.json()

    print(f"Total negRisk markets: {len(data)}")

    events = {}
    for m in data:
        evs = m.get("events", [])
        eid   = evs[0]["id"]    if evs else "none"
        etitle = evs[0]["title"] if evs else m["question"]
        if eid not in events:
            events[eid] = {"title": etitle, "markets": []}
        events[eid]["markets"].append(m)

    print(f"Unique negRisk events: {len(events)}\n")

    for eid, ev in list(events.items())[:8]:
        markets  = ev["markets"]
        import json as _json
        yes_sum  = sum(
            float(_json.loads(m.get("outcomePrices", "[0]"))[0])
            for m in markets
        )
        dev = yes_sum - 1.0
        flag = "  <-- OVERROUND ARB" if dev > 0.005 else ("  <-- UNDERROUND ARB" if dev < -0.005 else "")
        title = ev["title"]
        print(f"Event: {title[:70]}")
        print(f"  Markets: {len(markets)}   YES-sum: {yes_sum:.4f}   deviation: {dev:+.4f}{flag}")
        for m in markets:
            import json as _json
            prices = _json.loads(m.get("outcomePrices", "[0.5,0.5]"))
            yes_p  = float(prices[0]) if prices else 0.0
            spread = m.get("spread", "?")
            endDate = m.get("endDateIso", "?")
            print(f"    {m['question'][:58]:<58} YES={yes_p:.3f}  spr={spread}  end={endDate}")
        print()

asyncio.run(main())
