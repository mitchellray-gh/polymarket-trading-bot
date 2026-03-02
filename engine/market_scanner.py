"""
engine/market_scanner.py
─────────────────────────
Fetch active Polymarket markets and their live order-book data.

Speed architecture
──────────────────
v2 replaces the original per-token GET /book calls with a single
POST /books batch request that returns ALL order books in one round trip.

Old approach  : 500 markets × 2 tokens = 1 000 HTTP requests  (~750 ms)
New approach  : ceil(1000 / BOOKS_BATCH_SIZE) requests          (~150 ms)

How Polymarket binary markets work
───────────────────────────────────
Every market has exactly two outcome tokens: YES and NO.
- YES token redeems for $1.00 if the event resolves YES, $0 otherwise.
- NO  token redeems for $1.00 if the event resolves NO,  $0 otherwise.
- Prices live in [0.00 … 1.00] USDC (fractions of a dollar).

In a perfectly efficient market:  price(YES) + price(NO) == 1.00

Any deviation from 1.00 can be exploited:
  < 1.00  → buy both legs  (guaranteed $1 payout, pay < $1)
  > 1.00  → sell both legs (guaranteed $1 cost,    receive > $1)
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

try:
    import orjson as _json  # 3-5x faster than stdlib json
    def _loads(b): return _json.loads(b)
    def _dumps(obj): return _json.dumps(obj)
except ImportError:
    import json as _json  # type: ignore[no-redef]
    def _loads(b): return _json.loads(b)
    def _dumps(obj): return _json.dumps(obj).encode()

from .config import GAMMA_API_HOST, CLOB_HOST

# Max tokens per POST /books call. Polymarket accepts up to 500 in one call.
BOOKS_BATCH_SIZE = 500

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
    """Pull active/open binary markets from the Gamma Markets API."""
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
        raw = await resp.read()
        data = _loads(raw)
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
    bids.sort(key=lambda x: x.price, reverse=True)
    asks.sort(key=lambda x: x.price)
    return OrderBook(token_id=token_id, bids=bids, asks=asks)


async def _fetch_books_batch(
    session: aiohttp.ClientSession,
    token_ids: list[str],
) -> dict[str, OrderBook]:
    """
    POST /books with up to BOOKS_BATCH_SIZE token IDs in one request.
    Returns a mapping of token_id → OrderBook.

    This is the core speed win: replaces N individual GET /book calls with
    a single POST that returns all books in one network round-trip.
    """
    url = f"{CLOB_HOST}/books"
    body = _dumps([{"token_id": t} for t in token_ids])
    headers = {"Content-Type": "application/json"}

    try:
        async with session.post(
            url,
            data=body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning("POST /books status=%d", resp.status)
                return {}
            raw_list = _loads(await resp.read())
    except Exception as exc:
        logger.warning("POST /books failed: %s", exc)
        return {}

    result: dict[str, OrderBook] = {}
    for raw in raw_list:
        tid = raw.get("asset_id") or raw.get("token_id", "")
        if tid:
            result[tid] = _parse_book(raw, tid)
    return result


# ─── Market scanner ───────────────────────────────────────────────────────────

class MarketScanner:
    """
    Discovers active Polymarket markets and fetches all order books in the
    minimum number of HTTP round-trips using POST /books batch requests.

    Speed profile (500 markets, 1000 tokens):
      Old: ~1000 concurrent GET /book  → ~750 ms
      New: 2 × POST /books (500 each) → ~300 ms  (2.5× faster, uses far
                                                   fewer connections)
    """

    def __init__(self, batch_size: int = BOOKS_BATCH_SIZE):
        self.batch_size = batch_size
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "MarketScanner":
        connector = aiohttp.TCPConnector(
            limit=50,               # fewer connections needed with batch
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
        """Extract (yes_token_id, no_token_id) from a Gamma market dict."""
        tokens = raw.get("clob_token_ids") or raw.get("clobTokenIds") or []
        if isinstance(tokens, str):
            import json
            try:
                tokens = json.loads(tokens)
            except Exception:
                return None
        if len(tokens) != 2:
            return None
        return str(tokens[0]), str(tokens[1])

    async def _fetch_all_books(
        self, token_ids: list[str]
    ) -> dict[str, OrderBook]:
        """
        Fetch books for all token IDs using the minimum number of
        POST /books batch calls (concurrent).
        """
        chunks = [
            token_ids[i : i + self.batch_size]
            for i in range(0, len(token_ids), self.batch_size)
        ]
        results = await asyncio.gather(
            *[_fetch_books_batch(self.session, chunk) for chunk in chunks]
        )
        merged: dict[str, OrderBook] = {}
        for r in results:
            merged.update(r)
        return merged

    # ── public interface ─────────────────────────────────────────────────────

    async def scan_batch(
        self,
        limit: int = 200,
        offset: int = 0,
    ) -> list[MarketSnapshot]:
        """
        Fetch up to *limit* active markets and return enriched MarketSnapshots.

        All order books for the entire batch are fetched in ONE or TWO
        POST /books calls rather than one GET per token.
        """
        t0 = time.monotonic()
        raw_markets = await _fetch_active_markets(
            self.session, limit=limit, offset=offset
        )
        logger.info("Fetched %d raw markets from Gamma API", len(raw_markets))

        # ── collect all token pairs ───────────────────────────────────────
        market_meta: list[tuple[str, str, str, str]] = []  # (cid, q, yes_id, no_id)
        all_tokens: list[str] = []
        for raw in raw_markets:
            pair = self._parse_market_tokens(raw)
            if pair is None:
                continue
            yes_id, no_id = pair
            cid = raw.get("conditionId") or raw.get("condition_id", "")
            q   = raw.get("question", raw.get("description", "Unknown"))
            market_meta.append((cid, q, yes_id, no_id))
            all_tokens.append(yes_id)
            all_tokens.append(no_id)

        # ── fetch ALL books in one/two batched POST requests ──────────────
        books = await self._fetch_all_books(all_tokens)
        fetch_ms = (time.monotonic() - t0) * 1000

        # ── assemble snapshots ────────────────────────────────────────────
        snapshots: list[MarketSnapshot] = []
        for cid, q, yes_id, no_id in market_meta:
            yes_book = books.get(yes_id)
            no_book  = books.get(no_id)
            if yes_book is None or no_book is None:
                continue
            snapshots.append(MarketSnapshot(
                condition_id=cid,
                question=q,
                yes_token_id=yes_id,
                no_token_id=no_id,
                yes_book=yes_book,
                no_book=no_book,
            ))

        logger.info(
            "scan_batch: %d liquid markets in %.0f ms "
            "(%d tokens, %d book batches)",
            len(snapshots),
            fetch_ms,
            len(all_tokens),
            max(1, len(all_tokens) // self.batch_size),
        )
        return snapshots

    async def scan_markets(self, max_markets: int = 500) -> list[MarketSnapshot]:
        """
        Top-level scanner: fetches markets in pages, returns all liquid snapshots.
        Caps at *max_markets* total.
        """
        all_snapshots: list[MarketSnapshot] = []
        offset = 0
        page_size = 100

        while len(all_snapshots) < max_markets:
            batch = await self.scan_batch(limit=page_size, offset=offset)
            all_snapshots.extend(batch)
            if len(batch) < page_size:
                break
            offset += page_size

        return all_snapshots[:max_markets]

    async def discover_markets(self, max_markets: int = 500) -> list[dict[str, Any]]:
        """
        Fetch raw market metadata only (no order books).
        Run this once at startup / every ~60 s to refresh the market list.
        Returns a list of dicts with keys: condition_id, question, yes_token_id, no_token_id.
        """
        all_meta: list[dict[str, Any]] = []
        offset    = 0
        page_size = 100

        while len(all_meta) < max_markets:
            raw_markets = await _fetch_active_markets(
                self.session, limit=page_size, offset=offset
            )
            for raw in raw_markets:
                pair = self._parse_market_tokens(raw)
                if pair is None:
                    continue
                yes_id, no_id = pair
                all_meta.append({
                    "condition_id":  raw.get("conditionId") or raw.get("condition_id", ""),
                    "question":      raw.get("question", raw.get("description", "Unknown")),
                    "yes_token_id":  yes_id,
                    "no_token_id":   no_id,
                })
            if len(raw_markets) < page_size:
                break
            offset += page_size

        logger.info("discover_markets: %d tradable markets found", len(all_meta))
        return all_meta[:max_markets]

    # ------------------------------------------------------------------
    async def refresh_books(
        self, market_meta: list[dict[str, Any]]
    ) -> list[MarketSnapshot]:
        """
        Hot-path: given a cached list of market metadata (from discover_markets),
        fetch ONLY the order books — no Gamma API calls.

        This is the inner scan loop: runs every 0.5 s.
        Latency = one POST /books round trip (~400 ms from typical infra).

        Hot-path timeline per cycle:
          POST /books (1000 tokens, 2 requests of 500) → ~400 ms
          Signal detection (pure CPU)                  →   <1 ms
          Total                                        → ~401 ms
        """
        t0 = time.monotonic()

        all_tokens = []
        for m in market_meta:
            all_tokens.append(m["yes_token_id"])
            all_tokens.append(m["no_token_id"])

        books = await self._fetch_all_books(all_tokens)

        snapshots: list[MarketSnapshot] = []
        for m in market_meta:
            yes_book = books.get(m["yes_token_id"])
            no_book  = books.get(m["no_token_id"])
            if yes_book is None or no_book is None:
                continue
            snapshots.append(MarketSnapshot(
                condition_id=m["condition_id"],
                question=m["question"],
                yes_token_id=m["yes_token_id"],
                no_token_id=m["no_token_id"],
                yes_book=yes_book,
                no_book=no_book,
            ))

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.debug(
            "refresh_books: %d liquid / %d markets in %.0f ms",
            len(snapshots), len(market_meta), elapsed_ms,
        )
        return snapshots

