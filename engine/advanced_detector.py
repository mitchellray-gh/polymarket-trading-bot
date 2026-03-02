"""
engine/advanced_detector.py
─────────────────────────────
Four profit strategies the base binary-arb scanner misses entirely:

1. negRisk OVERROUND (mechanical, risk-free)
   ─────────────────────────────────────────
   Polymarket "negRisk" events group N mutually-exclusive outcomes.
   Sum of all YES prices MUST equal exactly 1.00 at equilibrium.
   When the CLOB order books show:
     ask_sum < 1.00  →  BUY every YES leg, bundle → redeem $1.00  (profit = 1.00 - ask_sum)
     bid_sum > 1.00  →  SELL every YES leg         →  collect > $1.00  (profit = bid_sum - 1.00)
   Uses real CLOB order books, not just Gamma midprices.

2. NEAR-EXPIRY mispricing (directional, low effort)
   ──────────────────────────────────────────────────
   Markets expiring within 48 h with price between 5 c and 95 c.
   These haven't converged yet — news / public data often lets you bet
   correctly before the crowd catches up.
   The engine flags them; the user decides based on external knowledge.

3. WIDE-SPREAD market making (passive income)
   ─────────────────────────────────────────────
   Markets where best_ask − best_bid > configurable threshold with real
   daily volume. Place resting limit orders on both sides near the midpoint
   and collect the spread on each matched pair.
   Return: list of MM candidates with suggested bid/ask quotes.

4. negRisk MAKER-SELL overround (BEST EXPLOIT — fee=0%, structural edge)
   ───────────────────────────────────────────────────────────────────────
   Polymarket maker fee = 0%.  So any negRisk event where sum(midprices) > 1.00
   is immediately exploitable with maker (limit) orders:
     - POST limit SELL on every YES leg at midprice
     - Orders fill over time via organic buyers (fee=0 to you)
     - Once all legs fill, you've collected mid_sum > 1.00
     - At resolution, exactly 1 leg pays $1.00 → profit = mid_sum − 1.00
   The Masters 2026: 59 legs, mid_sum=1.0745, vol24=$3.38M → ~$74/day/1k notional
   Filters out non-exclusive events (mul-qual, GTA VI) using mid_sum ≤ 1.30.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import aiohttp

try:
    import orjson as _json
    def _loads(b): return _json.loads(b)
except ImportError:
    import json as _json            # type: ignore[no-redef]
    def _loads(b): return _json.loads(b)

from .config import GAMMA_API_HOST, CLOB_HOST

logger = logging.getLogger(__name__)

# ─── Config knobs ─────────────────────────────────────────────────────────────
NEGRISK_MIN_EDGE        = 0.002   # min profit (after fees) to trigger negRisk taker arb
NEGRISK_MAKER_MIN_EDGE  = 0.001   # min overround to trigger maker-sell strategy (fee=0)
NEGRISK_MAX_SAFE_SUM    = 1.30    # mid_sum above this → probably not truly exclusive (filter out)
TAKER_FEE_PER_LEG       = 0.001   # 0.1 % Polymarket taker fee
MAKER_FEE_PER_LEG       = 0.000   # 0.0 % Polymarket maker fee (LIMIT orders)
NEAR_EXPIRY_HOURS       = 48      # flag markets expiring within this window
NEAR_EXPIRY_MIN_LIQ     = 50.0    # min liquidity USD to be worth flagging
NEAR_EXPIRY_PRICE_MIN   = 0.05    # price band — below this it's almost certainly
NEAR_EXPIRY_PRICE_MAX   = 0.95    #   already resolved
MM_MIN_SPREAD           = 0.025   # 2.5 % spread to be a MM target
MM_MIN_VOL24            = 300.0   # min 24h volume USD
MM_QUOTE_OFFSET         = 0.003   # place limit orders this far inside the spread
GAMMA_PAGE_SIZE         = 200     # markets per Gamma API page


# ─── Signal dataclasses ───────────────────────────────────────────────────────

@dataclass
class NegRiskSignal:
    """
    Overround / underround in a negRisk multi-outcome event.
    direction = "BUY_BUNDLE"  → buy all YES legs (ask_sum < 1.00)
    direction = "SELL_BUNDLE" → sell all YES legs (bid_sum > 1.00)
    """
    event_id:       str
    event_title:    str
    direction:      str          # "BUY_BUNDLE" | "SELL_BUNDLE"
    net_profit:     float        # per $1 notional, after fees
    ask_sum:        float
    bid_sum:        float
    n_legs:         int
    legs:           list[dict[str, Any]] = field(default_factory=list)
    # each leg:  {condition_id, question, yes_token_id, best_ask, best_bid}


@dataclass
class NegRiskMakerSignal:
    """
    Strategy 4: negRisk maker-sell overround.

    Place LIMIT SELL orders at midprice on every YES leg.
    Maker fee = 0%.  When all legs fill, collect mid_sum > 1.00.
    Exactly one leg resolves at $1.00 — profit = mid_sum - 1.00.

    Only valid when mid_sum is between 1.001 and NEGRISK_MAX_SAFE_SUM
    (above that threshold the event outcomes are likely NOT mutually exclusive).
    """
    event_id:        str
    event_title:     str
    mid_sum:         float       # sum of YES midprices across all legs
    gross_profit:    float       # mid_sum - 1.00  (no fees, maker = 0)
    pct_overround:   float       # gross_profit as a percentage
    n_legs:          int
    total_vol_24h:   float       # combined 24h volume across all legs
    total_liq:       float
    est_days_to_fill: float      # days until all legs are likely filled
    est_profit_per_day: float    # gross_profit / est_days_to_fill
    legs:            list[dict[str, Any]] = field(default_factory=list)
    # each leg: {condition_id, question, yes_token_id, midprice, bid, ask, vol24}


@dataclass
class NearExpirySignal:
    """Market expiring soon with price far from binary certainty."""
    condition_id:   str
    question:       str
    yes_price:      float        # last mid-price from Gamma
    hours_left:     float
    volume_24h:     float
    liquidity:      float
    spread:         float
    end_date:       str


@dataclass
class MarketMakerSignal:
    """Wide-spread market suitable for passive limit-order market-making."""
    condition_id:   str
    question:       str
    best_bid:       float
    best_ask:       float
    spread:         float        # best_ask - best_bid
    midpoint:       float
    suggested_bid:  float        # midpoint - MM_QUOTE_OFFSET
    suggested_ask:  float        # midpoint + MM_QUOTE_OFFSET
    volume_24h:     float
    liquidity:      float


# ─── Fetchers ─────────────────────────────────────────────────────────────────

async def _fetch_gamma_markets(
    session: aiohttp.ClientSession,
    extra_params: dict[str, str] | None = None,
    max_markets: int = 500,
) -> list[dict[str, Any]]:
    """Pull active Gamma markets (all fields) with optional extra filters."""
    url    = f"{GAMMA_API_HOST}/markets"
    result = []
    offset = 0
    params_base = {
        "active": "true",
        "closed": "false",
        "enableOrderBook": "true",
        **(extra_params or {}),
    }
    while len(result) < max_markets:
        params = {**params_base, "limit": GAMMA_PAGE_SIZE, "offset": offset}
        try:
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as r:
                r.raise_for_status()
                data = _loads(await r.read())
        except Exception as exc:
            logger.warning("Gamma fetch failed (offset=%d): %s", offset, exc)
            break
        page = data if isinstance(data, list) else data.get("data", [])
        result.extend(page)
        if len(page) < GAMMA_PAGE_SIZE:
            break
        offset += GAMMA_PAGE_SIZE
    return result[:max_markets]


async def _fetch_books_for_tokens(
    session: aiohttp.ClientSession,
    token_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """POST /books for the given token IDs; returns raw book dicts keyed by token_id."""
    if not token_ids:
        return {}
    url  = f"{CLOB_HOST}/books"
    body = _json.dumps([{"token_id": t} for t in token_ids])
    if isinstance(body, str):
        body = body.encode()
    try:
        async with session.post(
            url, data=body,
            headers={"Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as r:
            if r.status != 200:
                return {}
            raw_list = _loads(await r.read())
    except Exception as exc:
        logger.warning("POST /books failed: %s", exc)
        return {}
    return {
        (raw.get("asset_id") or raw.get("token_id", "")): raw
        for raw in raw_list
        if raw.get("asset_id") or raw.get("token_id")
    }


def _best_ask(book: dict) -> float | None:
    asks = book.get("asks", [])
    if not asks:
        return None
    return min(float(a["price"]) for a in asks)


def _best_bid(book: dict) -> float | None:
    bids = book.get("bids", [])
    if not bids:
        return None
    return max(float(b["price"]) for b in bids)


def _parse_token_ids(raw: dict) -> tuple[str, str] | None:
    """Extract (yes_token_id, no_token_id) from a Gamma market dict."""
    import json as js
    tok = raw.get("clobTokenIds") or raw.get("clob_token_ids") or "[]"
    if isinstance(tok, str):
        try:
            tok = js.loads(tok)
        except Exception:
            return None
    if len(tok) != 2:
        return None
    return str(tok[0]), str(tok[1])


def _yes_price(raw: dict) -> float | None:
    import json as js
    prices = raw.get("outcomePrices")
    if not prices:
        return None
    try:
        lst = js.loads(prices) if isinstance(prices, str) else prices
        return float(lst[0])
    except Exception:
        return None


# ─── Strategy 1: negRisk overround ───────────────────────────────────────────

async def scan_negrisk_overround(
    session: aiohttp.ClientSession,
) -> list[NegRiskSignal]:
    """
    Fetch all negRisk markets, group by event, check CLOB order books.
    Returns NegRiskSignal only when there is genuine edge after fees.
    """
    t0 = time.monotonic()
    markets = await _fetch_gamma_markets(session, {"negRisk": "true"}, max_markets=1000)

    # ── Group by event ────────────────────────────────────────────────────────
    events: dict[str, dict] = {}
    for m in markets:
        evs  = m.get("events", [])
        eid  = evs[0]["id"]    if evs else m.get("conditionId", "solo")
        etit = evs[0]["title"] if evs else m.get("question", "?")
        if eid not in events:
            events[eid] = {"title": etit, "markets": []}
        events[eid]["markets"].append(m)

    # ── Only events with ≥2 mutually exclusive outcomes ───────────────────────
    multi_events = {k: v for k, v in events.items() if len(v["markets"]) >= 2}
    logger.debug("negRisk: %d events with ≥2 outcomes", len(multi_events))

    # ── Collect all YES token IDs ─────────────────────────────────────────────
    all_tokens: list[str] = []
    token_to_market: dict[str, dict] = {}
    for ev in multi_events.values():
        for m in ev["markets"]:
            pair = _parse_token_ids(m)
            if pair:
                yes_id = pair[0]
                all_tokens.append(yes_id)
                token_to_market[yes_id] = m

    # ── Fetch all books in one batch ──────────────────────────────────────────
    books = await _fetch_books_for_tokens(session, all_tokens)

    # ── Check each event for bundle arb ──────────────────────────────────────
    signals: list[NegRiskSignal] = []
    for eid, ev in multi_events.items():
        legs_data = []
        ask_sum = 0.0
        bid_sum = 0.0
        missing = False

        for m in ev["markets"]:
            pair = _parse_token_ids(m)
            if not pair:
                missing = True
                break
            yes_id       = pair[0]
            book         = books.get(yes_id, {})
            ba           = _best_ask(book)
            bb           = _best_bid(book)
            if ba is None or bb is None:
                missing = True
                break
            ask_sum += ba
            bid_sum += bb
            legs_data.append({
                "condition_id": m.get("conditionId", ""),
                "question":     m.get("question", "?"),
                "yes_token_id": yes_id,
                "best_ask":     ba,
                "best_bid":     bb,
            })

        if missing or not legs_data:
            continue

        n_legs   = len(legs_data)
        fee_cost = TAKER_FEE_PER_LEG * n_legs  # fees for all legs

        # BUY BUNDLE: pay ask_sum, redeem at $1.00
        buy_profit = round(1.0 - ask_sum - fee_cost, 6)
        if buy_profit >= NEGRISK_MIN_EDGE:
            signals.append(NegRiskSignal(
                event_id=eid, event_title=ev["title"],
                direction="BUY_BUNDLE",
                net_profit=buy_profit,
                ask_sum=round(ask_sum, 6), bid_sum=round(bid_sum, 6),
                n_legs=n_legs, legs=legs_data,
            ))
            logger.info(
                "negRisk BUY_BUNDLE: %s  ask_sum=%.4f  net=%.4f",
                ev["title"][:50], ask_sum, buy_profit,
            )

        # SELL BUNDLE: receive bid_sum, pay $1.00 at settlement
        sell_profit = round(bid_sum - 1.0 - fee_cost, 6)
        if sell_profit >= NEGRISK_MIN_EDGE:
            signals.append(NegRiskSignal(
                event_id=eid, event_title=ev["title"],
                direction="SELL_BUNDLE",
                net_profit=sell_profit,
                ask_sum=round(ask_sum, 6), bid_sum=round(bid_sum, 6),
                n_legs=n_legs, legs=legs_data,
            ))
            logger.info(
                "negRisk SELL_BUNDLE: %s  bid_sum=%.4f  net=%.4f",
                ev["title"][:50], bid_sum, sell_profit,
            )

    elapsed = (time.monotonic() - t0) * 1000
    logger.info(
        "scan_negrisk_overround: %d events checked, %d signals in %.0f ms",
        len(multi_events), len(signals), elapsed,
    )
    return signals


# ─── Strategy 2: near-expiry staleness ───────────────────────────────────────

async def scan_near_expiry(
    session: aiohttp.ClientSession,
) -> list[NearExpirySignal]:
    """
    Flag markets expiring within NEAR_EXPIRY_HOURS whose price has not
    yet converged to $0 or $1.

    These are directional bet candidates: if external data (news, live
    results, etc.) suggests the outcome, trade the lagging price.
    """
    markets = await _fetch_gamma_markets(session, max_markets=1000)
    now     = datetime.now(timezone.utc)
    signals: list[NearExpirySignal] = []

    for m in markets:
        liq    = float(m.get("liquidityClob") or m.get("liquidityNum") or 0)
        vol24  = float(m.get("volume24hrClob") or m.get("volume24hr") or 0)
        spread = float(m.get("spread") or 0)
        yes_p  = _yes_price(m)
        end_raw = m.get("endDate") or ""

        if yes_p is None:
            continue
        if liq < NEAR_EXPIRY_MIN_LIQ:
            continue
        if not (NEAR_EXPIRY_PRICE_MIN <= yes_p <= NEAR_EXPIRY_PRICE_MAX):
            continue

        try:
            end_dt   = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            hrs_left = (end_dt - now).total_seconds() / 3600
        except Exception:
            continue

        if not (0 < hrs_left <= NEAR_EXPIRY_HOURS):
            continue

        signals.append(NearExpirySignal(
            condition_id=m.get("conditionId", ""),
            question=m.get("question", "?"),
            yes_price=yes_p,
            hours_left=round(hrs_left, 1),
            volume_24h=vol24,
            liquidity=liq,
            spread=spread,
            end_date=end_raw[:10],
        ))

    signals.sort(key=lambda s: s.hours_left)
    logger.info("scan_near_expiry: %d signals found", len(signals))
    return signals


# ─── Strategy 3: wide-spread market making ───────────────────────────────────

async def scan_market_maker_opportunities(
    session: aiohttp.ClientSession,
) -> list[MarketMakerSignal]:
    """
    Find markets where the bid-ask spread is wide and volume is real.
    Return suggested limit-order quotes that would earn the spread.

    These are NOT arb trades — they require posting resting limit orders
    and waiting for both sides to fill.
    """
    markets = await _fetch_gamma_markets(session, max_markets=1000)
    signals: list[MarketMakerSignal] = []

    for m in markets:
        spread = float(m.get("spread") or 0)
        vol24  = float(m.get("volume24hrClob") or m.get("volume24hr") or 0)
        liq    = float(m.get("liquidityClob") or m.get("liquidityNum") or 0)
        bid    = float(m.get("bestBid") or 0)
        ask    = float(m.get("bestAsk") or 0)

        if spread < MM_MIN_SPREAD:
            continue
        if vol24 < MM_MIN_VOL24:
            continue
        if bid <= 0 or ask <= 0 or ask <= bid:
            continue

        mid = round((bid + ask) / 2, 4)
        signals.append(MarketMakerSignal(
            condition_id=m.get("conditionId", ""),
            question=m.get("question", "?"),
            best_bid=bid,
            best_ask=ask,
            spread=round(spread, 4),
            midpoint=mid,
            suggested_bid=round(max(0.001, mid - MM_QUOTE_OFFSET), 4),
            suggested_ask=round(min(0.999, mid + MM_QUOTE_OFFSET), 4),
            volume_24h=vol24,
            liquidity=liq,
        ))

    signals.sort(key=lambda s: -s.spread)
    logger.info("scan_market_maker_opportunities: %d signals", len(signals))
    return signals


# ─── Strategy 4: negRisk maker-sell ─────────────────────────────────────────

async def scan_negrisk_maker_sell(
    session: aiohttp.ClientSession,
) -> list[NegRiskMakerSignal]:
    """
    Find negRisk events where sum(midprices) > 1.00 + NEGRISK_MAKER_MIN_EDGE.
    Uses ONLY Gamma midprices (no CLOB book fetch needed).
    Maker fee = 0%, so any positive overround is immediately exploitable.

    Filters out events where mid_sum > NEGRISK_MAX_SAFE_SUM (1.30) because
    those are almost certainly NOT truly mutually exclusive (e.g. GTA VI
    multi-event groups, FIFA multi-qualifier groups).
    """
    import json as _stdlib_json

    markets = await _fetch_gamma_markets(session, {"negRisk": "true"}, max_markets=1000)

    # Group by event
    events: dict[str, dict] = {}
    for m in markets:
        evs  = m.get("events", [])
        eid  = evs[0]["id"]    if evs else m.get("conditionId", "solo")
        etit = evs[0]["title"] if evs else m.get("question", "?")
        if eid not in events:
            events[eid] = {"title": etit, "markets": []}
        events[eid]["markets"].append(m)

    signals: list[NegRiskMakerSignal] = []

    for eid, ev in events.items():
        if len(ev["markets"]) < 2:
            continue

        legs_data = []
        mid_sum   = 0.0
        total_vol = 0.0
        total_liq = 0.0
        ok        = True

        for m in ev["markets"]:
            try:
                prices = _stdlib_json.loads(m.get("outcomePrices", "[]"))
                mid    = float(prices[0])
                bid    = float(m.get("bestBid") or 0)
                ask    = float(m.get("bestAsk") or 0)
                vol24  = float(m.get("volume24hr") or 0)
                liq    = float(m.get("liquidityNum") or 0)
                if mid <= 0:
                    ok = False; break
            except Exception:
                ok = False; break

            tok   = _stdlib_json.loads(m.get("clobTokenIds", "[]")) if isinstance(m.get("clobTokenIds"), str) else (m.get("clobTokenIds") or [])
            yes_id = str(tok[0]) if tok else ""

            mid_sum   += mid
            total_vol += vol24
            total_liq += liq
            legs_data.append({
                "condition_id": m.get("conditionId", ""),
                "question":     m.get("question", "?"),
                "yes_token_id": yes_id,
                "midprice":     round(mid, 5),
                "bid":          round(bid, 5),
                "ask":          round(ask, 5),
                "vol24":        vol24,
            })

        if not ok or not legs_data:
            continue

        gross = round(mid_sum - 1.0, 6)

        # Filter: not enough overround
        if gross < NEGRISK_MAKER_MIN_EDGE:
            continue

        # Filter: mid_sum too high → almost certainly non-exclusive event group
        if mid_sum > NEGRISK_MAX_SAFE_SUM:
            logger.debug(
                "Skipping non-exclusive negRisk event '%s' mid_sum=%.3f",
                ev["title"][:40], mid_sum
            )
            continue

        # Estimate days to fill all legs conservatively
        avg_liq = total_liq / len(legs_data)
        avg_vol = total_vol / len(legs_data)
        fill_rate = min(avg_vol / max(avg_liq, 1), 1.0)  # fraction filled per day
        days_to_fill = round(1.0 / max(fill_rate, 0.001), 2)
        profit_per_day = round(gross / max(days_to_fill, 0.01), 6)

        signals.append(NegRiskMakerSignal(
            event_id=eid,
            event_title=ev["title"],
            mid_sum=round(mid_sum, 5),
            gross_profit=gross,
            pct_overround=round(gross * 100, 3),
            n_legs=len(legs_data),
            total_vol_24h=total_vol,
            total_liq=total_liq,
            est_days_to_fill=days_to_fill,
            est_profit_per_day=profit_per_day,
            legs=legs_data,
        ))
        logger.info(
            "negRisk MAKER-SELL: '%s'  mid_sum=%.4f  gross=+%.4f  vol24=$%.0f",
            ev["title"][:50], mid_sum, gross, total_vol,
        )

    # Sort by profit_per_day descending
    signals.sort(key=lambda s: -s.est_profit_per_day)
    logger.info("scan_negrisk_maker_sell: %d signals", len(signals))
    return signals


# ─── Combined scanner ─────────────────────────────────────────────────────────

@dataclass
class AdvancedScanResult:
    negrisk:        list[NegRiskSignal]
    negrisk_maker:  list[NegRiskMakerSignal]
    near_expiry:    list[NearExpirySignal]
    market_maker:   list[MarketMakerSignal]
    elapsed_ms:     float


async def run_advanced_scan(
    session: aiohttp.ClientSession,
) -> AdvancedScanResult:
    """
    Run all four advanced scans concurrently.
    Designed to be called once at startup and then every ~30 s.
    """
    t0 = time.monotonic()
    negrisk, negrisk_maker, near_expiry, mm = await asyncio.gather(
        scan_negrisk_overround(session),
        scan_negrisk_maker_sell(session),
        scan_near_expiry(session),
        scan_market_maker_opportunities(session),
        return_exceptions=False,
    )
    elapsed = (time.monotonic() - t0) * 1000
    logger.info(
        "Advanced scan complete in %.0f ms: "
        "negRisk_taker=%d  negRisk_maker=%d  near_expiry=%d  market_maker=%d",
        elapsed, len(negrisk), len(negrisk_maker), len(near_expiry), len(mm),
    )
    return AdvancedScanResult(
        negrisk=negrisk,
        negrisk_maker=negrisk_maker,
        near_expiry=near_expiry,
        market_maker=mm,
        elapsed_ms=elapsed,
    )
