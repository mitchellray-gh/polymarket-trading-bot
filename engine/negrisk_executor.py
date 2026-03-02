"""
engine/negrisk_executor.py
───────────────────────────
Places GTC limit SELL orders for every YES leg of a negRisk maker-sell signal.

HOW IT WORKS
────────────
1.  Receive a NegRiskMakerSignal (event with mid_sum > 1.00).
2.  For each YES leg, sign a limit SELL order at midprice with GTC.
3.  Post all legs concurrently (thread pool for signing, single POST /orders
    bulk call for submission).
4.  Track open bundles so the same event is not re-posted until:
      - All legs are cancelled / filled, OR
      - BUNDLE_TTL_HOURS have passed (stale price protection)

COLLATERAL MODEL
────────────────
Polymarket negRisk sell: your maximum liability is $1.00 per bundle
(exactly one leg pays out).  The broker holds $1.00 USDC as collateral
for the full bundle, NOT size × legs.  So posting $5 on each of 59 Masters
legs ties up only $5 collateral — not $295.

ORDER SIZING
────────────
Controlled by MAKER_LEG_USDC in .env.  Default = $5.
Each leg is posted as:
    size  = MAKER_LEG_USDC / midprice   (shares)
    price = midprice
    type  = GTC (good-till-cancelled limit order)
"""
from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PostOrdersArgs
from py_clob_client.order_builder.constants import SELL

from .advanced_detector import NegRiskMakerSignal
from .config import Config

logger = logging.getLogger(__name__)

# Dedicated signing pool for maker orders (more workers for large leg counts)
_MAKER_SIGN_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="maker_sign")

BUNDLE_TTL_HOURS  = 24.0   # drop stale bundle tracking after this many hours


# ─── Result dataclasses ───────────────────────────────────────────────────────

@dataclass
class LegOrderResult:
    question:    str
    token_id:    str
    price:       float
    size_shares: float
    order_id:    str  = ""
    status:      str  = ""   # "live", "filled", "failed", "dry_run"
    error:       str  = ""


@dataclass
class BundleResult:
    event_title:     str
    n_legs:          int
    legs:            list[LegOrderResult] = field(default_factory=list)
    placed:          int = 0
    failed:          int = 0
    dry_run:         bool = False
    elapsed_ms:      float = 0.0

    @property
    def success(self) -> bool:
        return self.failed == 0 and self.placed > 0


# ─── Bundle tracker ───────────────────────────────────────────────────────────

class _BundleTracker:
    """
    Prevents re-posting the same negRisk event before existing orders clear.
    An entry expires after BUNDLE_TTL_HOURS regardless of fill status.
    """
    def __init__(self) -> None:
        self._active: dict[str, float] = {}   # event_id → posted_at timestamp

    def is_active(self, event_id: str) -> bool:
        ts = self._active.get(event_id)
        if ts is None:
            return False
        if time.monotonic() - ts > BUNDLE_TTL_HOURS * 3600:
            del self._active[event_id]
            return False
        return True

    def mark(self, event_id: str) -> None:
        self._active[event_id] = time.monotonic()

    def clear(self, event_id: str) -> None:
        self._active.pop(event_id, None)


_tracker = _BundleTracker()


# ─── Executor ─────────────────────────────────────────────────────────────────

class NegRiskExecutor:
    """
    Place a full maker-sell bundle for a NegRiskMakerSignal.

    Usage:
        executor = NegRiskExecutor(client, cfg)
        result   = await executor.execute(signal)
    """

    def __init__(self, client: ClobClient, cfg: Config) -> None:
        self._client = client
        self._cfg    = cfg
        self._leg_usdc = cfg.maker_leg_usdc

    # ── Signing (CPU-bound, runs in thread pool) ──────────────────────────────

    def _sign_leg(self, token_id: str, price: float, size_shares: float) -> Any:
        """Sign one GTC limit SELL order. Blocking — runs in _MAKER_SIGN_POOL."""
        args = OrderArgs(
            token_id=token_id,
            price=round(price, 4),
            size=round(size_shares, 4),
            side=SELL,
        )
        return self._client.create_order(args)

    # ── Submission ────────────────────────────────────────────────────────────

    async def _submit_bulk(
        self, signed_orders: list[tuple[Any, dict]]
    ) -> list[dict[str, Any]]:
        """
        POST all signed orders in a single /orders bulk call.
        Falls back to individual POSTs if the bulk call fails.
        """
        loop = asyncio.get_event_loop()

        def _bulk():
            return self._client.post_orders([
                PostOrdersArgs(order=order, orderType=OrderType.GTC)
                for order, _ in signed_orders
            ])

        try:
            results = await loop.run_in_executor(_MAKER_SIGN_POOL, _bulk)
            if isinstance(results, list):
                return results
        except Exception as exc:
            logger.warning("Bulk POST failed (%s) — falling back to individual posts", exc)

        # Individual fallback
        async def _post_one(order: Any) -> dict:
            def _send():
                return self._client.post_order(order, OrderType.GTC)
            try:
                return await loop.run_in_executor(_MAKER_SIGN_POOL, _send)
            except Exception as e:
                return {"error": str(e)}

        responses = await asyncio.gather(
            *[_post_one(order) for order, _ in signed_orders],
            return_exceptions=False,
        )
        return list(responses)

    # ── Main entry point ──────────────────────────────────────────────────────

    async def execute(self, signal: NegRiskMakerSignal) -> BundleResult:
        """
        Place limit SELL orders on all YES legs of the bundle.
        Returns immediately after submission — orders sit in the book as GTC.
        """
        t0  = time.monotonic()
        dry = self._cfg.dry_run

        if _tracker.is_active(signal.event_id) and not dry:
            logger.info(
                "Skipping '%s' — bundle already posted (TTL not expired)",
                signal.event_title[:50],
            )
            return BundleResult(
                event_title=signal.event_title,
                n_legs=signal.n_legs,
                dry_run=dry,
            )

        logger.info(
            "%s Posting maker-sell bundle: '%s'  legs=%d  overround=%.2f%%  leg_size=$%.2f",
            "[DRY-RUN]" if dry else "[LIVE]",
            signal.event_title[:55],
            signal.n_legs,
            signal.pct_overround,
            self._leg_usdc,
        )

        # ── Dry-run: log and return immediately ──────────────────────────────
        if dry:
            legs = []
            for leg in signal.legs:
                mid        = leg["midprice"]
                size_shares = self._leg_usdc / mid if mid > 0 else 0
                legs.append(LegOrderResult(
                    question=leg["question"],
                    token_id=leg["yes_token_id"],
                    price=mid,
                    size_shares=round(size_shares, 4),
                    order_id="DRY-RUN",
                    status="dry_run",
                ))
                logger.info(
                    "  [DRY-RUN] Would SELL %s  price=%.4f  size=%.4f shares  '%s'",
                    leg["yes_token_id"][:12], mid, size_shares, leg["question"][:50],
                )
            elapsed = (time.monotonic() - t0) * 1000
            return BundleResult(
                event_title=signal.event_title,
                n_legs=signal.n_legs,
                legs=legs,
                placed=len(legs),
                failed=0,
                dry_run=True,
                elapsed_ms=elapsed,
            )

        # ── Live: sign all legs in parallel ──────────────────────────────────
        loop = asyncio.get_event_loop()

        async def _sign_async(leg: dict) -> tuple[Any | None, dict]:
            mid         = leg["midprice"]
            size_shares = self._leg_usdc / mid if mid > 0 else 0
            def _sign():
                return self._sign_leg(leg["yes_token_id"], mid, size_shares)
            try:
                signed = await loop.run_in_executor(_MAKER_SIGN_POOL, _sign)
                return signed, leg
            except Exception as exc:
                logger.error("Sign failed for leg '%s': %s", leg["question"][:45], exc)
                return None, leg

        sign_tasks    = [_sign_async(leg) for leg in signal.legs]
        signed_pairs  = await asyncio.gather(*sign_tasks)

        good   = [(s, leg) for s, leg in signed_pairs if s is not None]
        failed = [(s, leg) for s, leg in signed_pairs if s is None]

        if not good:
            logger.error("All legs failed to sign for '%s'", signal.event_title[:50])
            return BundleResult(
                event_title=signal.event_title, n_legs=signal.n_legs,
                failed=len(failed), elapsed_ms=(time.monotonic()-t0)*1000,
            )

        # ── Submit ────────────────────────────────────────────────────────────
        responses = await self._submit_bulk(good)

        leg_results = []
        n_placed   = 0
        n_failed   = len(failed)

        for (signed, leg), resp in zip(good, responses):
            mid        = leg["midprice"]
            size_shares = self._leg_usdc / mid if mid > 0 else 0
            if isinstance(resp, dict) and not resp.get("error"):
                oid    = resp.get("orderID", resp.get("id", ""))
                status = resp.get("status", "live")
                n_placed += 1
                logger.info(
                    "  Order placed: id=%s  status=%s  price=%.4f  '%s'",
                    oid[:10], status, mid, leg["question"][:45],
                )
                leg_results.append(LegOrderResult(
                    question=leg["question"], token_id=leg["yes_token_id"],
                    price=mid, size_shares=round(size_shares, 4),
                    order_id=oid, status=status,
                ))
            else:
                err = (resp or {}).get("error", "unknown") if isinstance(resp, dict) else str(resp)
                n_failed += 1
                logger.warning("  Order failed: '%s'  error=%s", leg["question"][:45], err)
                leg_results.append(LegOrderResult(
                    question=leg["question"], token_id=leg["yes_token_id"],
                    price=mid, size_shares=round(size_shares, 4),
                    status="failed", error=err,
                ))

        elapsed = (time.monotonic() - t0) * 1000

        if n_placed > 0:
            _tracker.mark(signal.event_id)
            logger.info(
                "Bundle '%s': %d/%d legs placed  (%.0f ms)",
                signal.event_title[:50], n_placed, signal.n_legs, elapsed,
            )

        return BundleResult(
            event_title=signal.event_title,
            n_legs=signal.n_legs,
            legs=leg_results,
            placed=n_placed,
            failed=n_failed,
            dry_run=False,
            elapsed_ms=elapsed,
        )
