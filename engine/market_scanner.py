"""
engine/market_scanner.py
─────────────────────────
Fetch active Polymarket markets and their live order-book data.

How Polymarket binary markets work
───────────────────────────────────
Every market has exactly two outcome tokens: YES and NO.
- YES token redeems for $1.00 if the event resolves YES, $0 otherwise.
- NO  token redeems for $1.00 if the event resolves NO,  $0 otherwise.
- Prices live in [0.00 … 1.00] USDC (fractions of a dollar).

In a perfectly efficient market:
    price(YES) + price(NO) == 1.00

Any deviation from 1.00 can be exploited:
  < 1.00  → buy both legs  (guaranteed $1 payout, pay < $1)
  > 1.00  → sell both legs (guaranteed $1 cost,    receive > $1)

This module discovers tradable markets and enriches them with live order-book
snapshots so the opportunity detector can act immediately.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .config import GAMMA_API_HOST, CLOB_HOST

logger = logging.getLogger(__name__)


# ─── Data models ─────────────────────────────────────────────────────────────

@dataclass
class OrderLevel:
    """A single price level in the order book."""
    price: float
    size: float


@dataclass
class OrderBook:
    """
    Snapshot of one side of a market's order book.

    bids  – buyers' resting limit orders (descending price)
    asks  – sellers' resting limit orders (ascending price)
    """
    token_id: str
    bids: list[OrderLevel] = field(default_factory=list)
    asks: list[OrderLevel] = field(default_factory=list)

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def midpoint(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return round((self.best_bid + self.best_ask) / 2, 6)
        return None

    @property
    def spread(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return round(self.best_ask - self.best_bid, 6)
        return None


@dataclass
class MarketSnapshot:
    """
    A tradable binary market with live order-book data for both legs.

    yes_token_id / no_token_id  — ERC1155 token IDs on Polygon
    question                    — human-readable description
    yes_book / no_book          — live order books (may be None if illiquid)
    """
    condition_id:  str
    question:      str
    yes_token_id:  str
    no_token_id:   str
    yes_book:      OrderBook | None = None
    no_book:       OrderBook | None = None
    fetched_at:    float = field(default_factory=time.monotonic)

    @property
    def yes_best_ask(self) -> float | None:
        return self.yes_book.best_ask if self.yes_book else None

    @property
    def no_best_ask(self) -> float | None:
        return self.no_book.best_ask if self.no_book else None

    @property
    def yes_best_bid(self) -> float | None:
        return self.yes_book.best_bid if self.yes_book else None

    @property
    def no_best_bid(self) -> float | None:
        return self.no_book.best_bid if self.no_book else None

    @property
    def combined_ask(self) -> float | None:
        """Cost to buy both YES and NO (should be ≈ 1.00 in equilibrium)."""
        a, b = self.yes_best_ask, self.no_best_ask
        return round(a + b, 6) if a is not None and b is not None else None

    @property
    def combined_bid(self) -> float | None:
        """Revenue from selling both YES and NO (should be ≈ 1.00 in equilibrium)."""
        a, b = self.yes_best_bid, self.no_best_bid
        return round(a + b, 6) if a is not None and b is not None else None


# ─── Gamma API helpers ────────────────────────────────────────────────────────

async def _fetch_active_markets(
    session: aiohttp.ClientSession,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """
    Pull active/open binary markets from the Gamma Markets API.
    Returns a list of raw market dicts, each containing token metadata.
    """
    url = f"{GAMMA_API_HOST}/markets"
    params = {
        "active": "true",
        "closed": "false",
        "enableOrderBook": "true",
        "limit": limit,
        "offset": offset,
    }
    async with session.get(url, params=params) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return data if isinstance(data, list) else data.get("data", [])


# ─── CLOB order-book helpers ─────────────────────────────────────────────────

def _parse_book(raw: dict[str, Any], token_id: str) -> OrderBook:
    """Convert raw CLOB /book response into an OrderBook dataclass."""
    bids = [
        OrderLevel(price=float(lvl["price"]), size=float(lvl["size"]))
        for lvl in raw.get("bids", [])
    ]
    asks = [
        OrderLevel(price=float(lvl["price"]), size=float(lvl["size"]))
        for lvl in raw.get("asks", [])
    ]
    # bids descending, asks ascending
    bids.sort(key=lambda x: x.price, reverse=True)
    asks.sort(key=lambda x: x.price)
    return OrderBook(token_id=token_id, bids=bids, asks=asks)


async def _fetch_order_book(
    session: aiohttp.ClientSession,
    token_id: str,
) -> OrderBook | None:
    """Fetch a single order book from the CLOB REST API."""
    url = f"{CLOB_HOST}/book"
    params = {"token_id": token_id}
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                return None
            raw = await resp.json()
            return _parse_book(raw, token_id)
    except Exception as exc:
        logger.debug("Order-book fetch failed for %s: %s", token_id[:12], exc)
        return None


# ─── Market scanner ───────────────────────────────────────────────────────────

class MarketScanner:
    """
    Continuously discovers active Polymarket markets and attaches live
    order-book data to each one.

    Designed for high-throughput async execution: all order-book requests
    for a batch of markets are launched concurrently.
    """

    def __init__(self, batch_size: int = 20):
        self.batch_size = batch_size
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "MarketScanner":
        connector = aiohttp.TCPConnector(
            limit=100,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
        self._session = aiohttp.ClientSession(connector=connector)
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._session:
            await self._session.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("Use MarketScanner as an async context manager.")
        return self._session

    # ── private helpers ──────────────────────────────────────────────────────

    def _parse_market_tokens(self, raw: dict[str, Any]) -> tuple[str, str] | None:
        """
        Extract (yes_token_id, no_token_id) from a Gamma market dict.
        Returns None if the market is not binary / tokens are absent.
        """
        tokens = raw.get("clob_token_ids") or raw.get("clobTokenIds") or []
        # Gamma returns a JSON-encoded list or a real list
        if isinstance(tokens, str):
            import json
            try:
                tokens = json.loads(tokens)
            except Exception:
                return None

        if len(tokens) != 2:
            return None

        # Gamma convention: index 0 = YES, index 1 = NO
        return str(tokens[0]), str(tokens[1])

    async def _enrich_market(
        self, raw: dict[str, Any]
    ) -> MarketSnapshot | None:
        """Attach order books to a raw market dict and return a MarketSnapshot."""
        token_pair = self._parse_market_tokens(raw)
        if token_pair is None:
            return None

        yes_id, no_id = token_pair
        condition_id = raw.get("conditionId") or raw.get("condition_id", "")
        question     = raw.get("question", raw.get("description", "Unknown"))

        yes_book, no_book = await asyncio.gather(
            _fetch_order_book(self.session, yes_id),
            _fetch_order_book(self.session, no_id),
        )

        if yes_book is None or no_book is None:
            return None  # skip illiquid / errored markets

        return MarketSnapshot(
            condition_id=condition_id,
            question=question,
            yes_token_id=yes_id,
            no_token_id=no_id,
            yes_book=yes_book,
            no_book=no_book,
        )

    # ── public interface ─────────────────────────────────────────────────────

    async def scan_batch(
        self,
        limit: int = 200,
        offset: int = 0,
    ) -> list[MarketSnapshot]:
        """
        Fetch up to *limit* active markets (offset by *offset*) and return
        enriched MarketSnapshot objects.

        All order-book requests within each sub-batch are concurrent.
        """
        raw_markets = await _fetch_active_markets(self.session, limit=limit, offset=offset)
        logger.info("Fetched %d raw markets from Gamma API", len(raw_markets))

        snapshots: list[MarketSnapshot] = []

        # Process in sub-batches to avoid hammering the CLOB
        for i in range(0, len(raw_markets), self.batch_size):
            chunk = raw_markets[i : i + self.batch_size]
            results = await asyncio.gather(
                *[self._enrich_market(m) for m in chunk],
                return_exceptions=False,
            )
            for snap in results:
                if snap is not None:
                    snapshots.append(snap)

            logger.debug(
                "Enriched batch %d–%d → %d liquid markets",
                i, i + len(chunk), len(snapshots),
            )

        return snapshots

    async def scan_markets(self, max_markets: int = 500) -> list[MarketSnapshot]:
        """
        Top-level scanner: fetches markets in pages and returns all liquid snapshots.
        Caps at *max_markets* total to keep latency predictable.
        """
        all_snapshots: list[MarketSnapshot] = []
        offset = 0
        page_size = 100

        while len(all_snapshots) < max_markets:
            batch = await self.scan_batch(limit=page_size, offset=offset)
            all_snapshots.extend(batch)
            if len(batch) < page_size:
                break  # no more pages
            offset += page_size

        return all_snapshots[:max_markets]
