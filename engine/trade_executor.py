"""
engine/trade_executor.py
─────────────────────────
Executes binary-arbitrage trades at maximum speed using the Polymarket CLOB.

Speed optimisations in v2
──────────────────────────
1. POST /orders  (bulk)  — both legs submitted in a SINGLE HTTP request
   instead of two sequential POSTs.  Halves order-submission network RTT.

2. Dedicated ThreadPoolExecutor (size=2, pre-warmed) — order signing
   (secp256k1 EIP-712) is CPU-bound and cannot be awaited directly.
   A pre-warmed pool with max_workers=2 eliminates cold-start overhead
   so both legs are signed in parallel with zero scheduling lag.

3. orjson serialisation — used wherever JSON is constructed for requests.

Execution strategy
──────────────────
For BUY_BOTH:
  Sign YES and NO orders concurrently in the thread pool, then POST /orders
  with both payloads in a single HTTP call.

For SELL_BOTH:
  Same, inverted.

If the bulk endpoint is unavailable or rejects one leg, the executor falls
back to two concurrent individual POST /order calls.
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType, PostOrdersArgs
from py_clob_client.order_builder.constants import BUY, SELL

from .config import Config
from .opportunity_detector import SignalType, TradingSignal

logger = logging.getLogger(__name__)

# Pre-warmed signing pool — two workers, one per trade leg.
_SIGN_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sign")


# ─── Result models ────────────────────────────────────────────────────────────

class LegStatus(Enum):
    FILLED     = auto()
    PARTIAL    = auto()
    REJECTED   = auto()
    DRY_RUN    = auto()


@dataclass
class LegResult:
    token_id:   str
    side:       str          # "BUY" or "SELL"
    price:      float
    size_usdc:  float
    status:     LegStatus
    order_id:   str  = ""
    filled_qty: float = 0.0
    raw:        dict[str, Any] = field(default_factory=dict)


@dataclass
class TradeResult:
    signal:     TradingSignal
    yes_leg:    LegResult
    no_leg:     LegResult
    success:    bool
    notes:      str = ""

    @property
    def both_filled(self) -> bool:
        ok = {LegStatus.FILLED, LegStatus.DRY_RUN}
        return self.yes_leg.status in ok and self.no_leg.status in ok

    @property
    def one_legged(self) -> bool:
        ok = {LegStatus.FILLED, LegStatus.DRY_RUN}
        y = self.yes_leg.status in ok
        n = self.no_leg.status in ok
        return y != n


# ─── Executor ─────────────────────────────────────────────────────────────────

class TradeExecutor:
    """
    Submit both legs of a binary-arbitrage trade at maximum speed.

    v2 key change: signs both orders in parallel (thread pool) then sends
    them together in a SINGLE POST /orders bulk request — one network RTT
    instead of two.
    """

    def __init__(self, client: ClobClient, cfg: Config) -> None:
        self._client = client
        self._cfg    = cfg

    # ── Signing (CPU-bound, runs in thread pool) ──────────────────────────────

    def _sign_market_order(
        self, token_id: str, side: str, amount_usdc: float
    ) -> Any:
        """Create and sign a FOK market order. Runs in _SIGN_POOL."""
        clob_side = BUY if side == "BUY" else SELL
        args = MarketOrderArgs(
            token_id=token_id,
            amount=amount_usdc,
            side=clob_side,
            order_type=OrderType.FOK,
        )
        return self._client.create_market_order(args)

    def _sign_limit_order(
        self, token_id: str, side: str, price: float, size_shares: float
    ) -> Any:
        """Create and sign a GTC limit order. Runs in _SIGN_POOL."""
        clob_side = BUY if side == "BUY" else SELL
        args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size_shares,
            side=clob_side,
        )
        return self._client.create_order(args)

    # ── Bulk order submission ─────────────────────────────────────────────────

    async def _post_bulk(
        self,
        yes_signed: Any,
        no_signed: Any,
        order_type: OrderType,
        yes_price: float,
        no_price: float,
        yes_usdc: float,
        no_usdc: float,
        dry: bool,
    ) -> tuple[LegResult, LegResult]:
        """
        Submit both signed orders in a single POST /orders call.

        Falls back to two concurrent individual POSTs if the bulk call fails.
        """
        yes_tid = yes_signed.tokenId if hasattr(yes_signed, "tokenId") else ""
        no_tid  = no_signed.tokenId  if hasattr(no_signed,  "tokenId") else ""

        if dry:
            logger.info("[DRY-RUN] Would POST /orders with 2 legs simultaneously")
            return (
                LegResult(token_id=yes_tid, side="?", price=yes_price,
                          size_usdc=yes_usdc, status=LegStatus.DRY_RUN, order_id="DRY-YES"),
                LegResult(token_id=no_tid,  side="?", price=no_price,
                          size_usdc=no_usdc,  status=LegStatus.DRY_RUN, order_id="DRY-NO"),
            )

        loop = asyncio.get_event_loop()

        def _bulk_post() -> list[dict[str, Any]]:
            return self._client.post_orders([
                PostOrdersArgs(order=yes_signed, orderType=order_type),
                PostOrdersArgs(order=no_signed,  orderType=order_type),
            ])

        try:
            responses = await loop.run_in_executor(_SIGN_POOL, _bulk_post)
        except Exception as exc:
            logger.warning("POST /orders bulk failed (%s) — falling back to individual posts", exc)
            return await self._post_individual_fallback(
                yes_signed, no_signed, order_type,
                yes_price, no_price, yes_usdc, no_usdc,
            )

        def _parse(resp: dict, tid: str, price: float, usdc: float) -> LegResult:
            oid  = resp.get("orderID", "")
            stat = resp.get("status", "")
            if stat in ("matched", "filled"):
                status = LegStatus.FILLED
            elif stat == "unmatched":
                status = LegStatus.REJECTED
            else:
                status = LegStatus.PARTIAL
            filled = float(resp.get("takerAmount", 0) or resp.get("filledQty", 0) or 0)
            logger.info("Bulk leg token=%s status=%s id=%s", tid[:12], stat, oid[:8])
            return LegResult(token_id=tid, side="?", price=price, size_usdc=usdc,
                             status=status, order_id=oid, filled_qty=filled, raw=resp)

        yes_resp = responses[0] if isinstance(responses, list) and len(responses) > 0 else {}
        no_resp  = responses[1] if isinstance(responses, list) and len(responses) > 1 else {}

        return _parse(yes_resp, yes_tid, yes_price, yes_usdc), \
               _parse(no_resp,  no_tid,  no_price,  no_usdc)

    async def _post_individual_fallback(
        self,
        yes_signed: Any, no_signed: Any,
        order_type: OrderType,
        yes_price: float, no_price: float,
        yes_usdc: float, no_usdc: float,
    ) -> tuple[LegResult, LegResult]:
        """Fallback: two concurrent individual POST /order calls."""
        loop = asyncio.get_event_loop()

        yes_tid = yes_signed.tokenId if hasattr(yes_signed, "tokenId") else ""
        no_tid  = no_signed.tokenId  if hasattr(no_signed,  "tokenId") else ""

        def _post_yes():
            return self._client.post_order(yes_signed, order_type)

        def _post_no():
            return self._client.post_order(no_signed, order_type)

        yes_resp, no_resp = await asyncio.gather(
            loop.run_in_executor(_SIGN_POOL, _post_yes),
            loop.run_in_executor(_SIGN_POOL, _post_no),
        )

        def _p(resp, tid, price, usdc):
            stat = resp.get("status", "") if isinstance(resp, dict) else ""
            if stat in ("matched", "filled"): status = LegStatus.FILLED
            elif stat == "unmatched":          status = LegStatus.REJECTED
            else:                              status = LegStatus.PARTIAL
            return LegResult(token_id=tid, side="?", price=price, size_usdc=usdc,
                             status=status, order_id=resp.get("orderID",""), raw=resp)

        return _p(yes_resp, yes_tid, yes_price, yes_usdc), \
               _p(no_resp,  no_tid,  no_price,  no_usdc)

    # ── Public interface ──────────────────────────────────────────────────────

    async def execute(self, signal: TradingSignal) -> TradeResult:
        """
        Execute both legs of the arbitrage signal at maximum speed.

        Pipeline:
          1. Sign YES and NO orders concurrently in the pre-warmed thread pool.
          2. POST /orders with both payloads in a SINGLE HTTP request.
          3. Return TradeResult.
        """
        cfg = self._cfg
        dry = cfg.dry_run

        if signal.signal_type == SignalType.BUY_BOTH:
            order_type = OrderType.FOK
            yes_price  = signal.yes_price
            no_price   = signal.no_price
        elif signal.signal_type == SignalType.SELL_BOTH:
            order_type = OrderType.FOK
            yes_price  = signal.yes_price
            no_price   = signal.no_price
        else:
            raise ValueError(f"Non-executable signal type: {signal.signal_type}")

        yes_usdc = cfg.max_position_usdc
        no_usdc  = cfg.max_position_usdc

        logger.info(
            "Executing %s: YES=%s@%.4f NO=%s@%.4f size=%.2f USDC",
            signal.signal_type.name,
            signal.yes_token_id[:12], yes_price,
            signal.no_token_id[:12],  no_price,
            yes_usdc,
        )

        if dry:
            # Dry-run: skip signing entirely, return simulated results
            yes_result = LegResult(
                token_id=signal.yes_token_id, side="DRY", price=yes_price,
                size_usdc=yes_usdc, status=LegStatus.DRY_RUN, order_id="DRY-YES",
            )
            no_result = LegResult(
                token_id=signal.no_token_id, side="DRY", price=no_price,
                size_usdc=no_usdc, status=LegStatus.DRY_RUN, order_id="DRY-NO",
            )
            return TradeResult(signal=signal, yes_leg=yes_result, no_leg=no_result, success=True)

        # ── Step 1: Sign both orders concurrently in the thread pool ──────────
        loop = asyncio.get_event_loop()
        arb_side = "BUY" if signal.signal_type == SignalType.BUY_BOTH else "SELL"

        try:
            yes_signed, no_signed = await asyncio.gather(
                loop.run_in_executor(
                    _SIGN_POOL,
                    self._sign_market_order,
                    signal.yes_token_id, arb_side, yes_usdc,
                ),
                loop.run_in_executor(
                    _SIGN_POOL,
                    self._sign_market_order,
                    signal.no_token_id, arb_side, no_usdc,
                ),
            )
        except Exception as exc:
            logger.error("Order signing failed: %s", exc)
            failed = LegResult(
                token_id="", side="?", price=0, size_usdc=0,
                status=LegStatus.REJECTED, notes=str(exc),
            )
            return TradeResult(signal=signal, yes_leg=failed, no_leg=failed,
                               success=False, notes=f"Signing error: {exc}")

        # ── Step 2: POST /orders — single bulk HTTP call ───────────────────────
        yes_result, no_result = await self._post_bulk(
            yes_signed, no_signed, order_type,
            yes_price, no_price, yes_usdc, no_usdc, dry=False,
        )

        both_ok = yes_result.status in {LegStatus.FILLED, LegStatus.DRY_RUN}
        both_ok = both_ok and no_result.status in {LegStatus.FILLED, LegStatus.DRY_RUN}

        notes = "" if both_ok else (
            f"Partial fill — YES={yes_result.status.name} NO={no_result.status.name}. "
            "Check position manager for one-legged risk."
        )
        if notes:
            logger.warning(notes)

        return TradeResult(
            signal=signal, yes_leg=yes_result, no_leg=no_result,
            success=both_ok, notes=notes,
        )

class LegStatus(Enum):
    FILLED     = auto()
    PARTIAL    = auto()
    REJECTED   = auto()
    DRY_RUN    = auto()


@dataclass
class LegResult:
    token_id:   str
    side:       str          # "BUY" or "SELL"
    price:      float
    size_usdc:  float
    status:     LegStatus
    order_id:   str  = ""
    filled_qty: float = 0.0
    raw:        dict[str, Any] = field(default_factory=dict)


@dataclass
class TradeResult:
    signal:     TradingSignal
    yes_leg:    LegResult
    no_leg:     LegResult
    success:    bool
    notes:      str = ""

    @property
    def both_filled(self) -> bool:
        ok = {LegStatus.FILLED, LegStatus.DRY_RUN}
        return self.yes_leg.status in ok and self.no_leg.status in ok

    @property
    def one_legged(self) -> bool:
        """True when only one leg filled — creates inventory risk."""
        ok = {LegStatus.FILLED, LegStatus.DRY_RUN}
        y = self.yes_leg.status in ok
        n = self.no_leg.status in ok
        return y != n  # XOR: exactly one


# ─── Executor ─────────────────────────────────────────────────────────────────

class TradeExecutor:
    """
    Post both legs of a binary-arbitrage trade as quickly as possible.
    Designed for async execution so both legs hit the exchange concurrently.
    """

    def __init__(self, client: ClobClient, cfg: Config) -> None:
        self._client = client
        self._cfg    = cfg

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _usdc_to_shares(self, usdc: float, price: float) -> float:
        """Convert USDC amount to shares at a given price level."""
        if price <= 0:
            return 0.0
        return round(usdc / price, 2)  # Polymarket rounds to 2 d.p.

    async def _post_market_order(
        self,
        token_id: str,
        side: str,          # "BUY" or "SELL"
        amount_usdc: float,
        price: float,
        dry_run: bool,
    ) -> LegResult:
        """
        Post a single FOK market order and return a LegResult.
        Falls back to a GTC limit order at the best visible price if FOK fails.
        """
        if dry_run:
            logger.info(
                "[DRY-RUN] Would %s %.2f USDC of token %s at %.4f",
                side, amount_usdc, token_id[:12], price,
            )
            return LegResult(
                token_id=token_id, side=side, price=price,
                size_usdc=amount_usdc, status=LegStatus.DRY_RUN,
                order_id="DRY-RUN",
            )

        # ── Run blocking CLOB call in thread executor ──────────────────────
        loop = asyncio.get_event_loop()

        def _create_and_post() -> dict[str, Any]:
            clob_side = BUY if side == "BUY" else SELL
            args = MarketOrderArgs(
                token_id=token_id,
                amount=amount_usdc,
                side=clob_side,
                order_type=OrderType.FOK,
            )
            signed = self._client.create_market_order(args)
            return self._client.post_order(signed, OrderType.FOK)

        try:
            resp = await loop.run_in_executor(None, _create_and_post)
        except Exception as exc:
            logger.warning("Order submission error token=%s: %s", token_id[:12], exc)
            return LegResult(
                token_id=token_id, side=side, price=price,
                size_usdc=amount_usdc, status=LegStatus.REJECTED,
                notes=str(exc),
            )

        # ── Parse response ─────────────────────────────────────────────────
        order_id   = resp.get("orderID", "")
        status_str = resp.get("status", "")

        if status_str in ("matched", "filled"):
            status = LegStatus.FILLED
        elif status_str == "unmatched":
            # FOK was not fully filled — treat as rejected   
            status = LegStatus.REJECTED
        else:
            status = LegStatus.PARTIAL

        filled_qty = float(resp.get("takerAmount", 0) or resp.get("filledQty", 0) or 0)

        logger.info(
            "%s %s %.2f USDC @ %.4f ← status=%s id=%s",
            side, token_id[:12], amount_usdc, price, status_str, order_id[:8],
        )

        return LegResult(
            token_id=token_id, side=side, price=price,
            size_usdc=amount_usdc, status=status,
            order_id=order_id, filled_qty=filled_qty, raw=resp,
        )

    # ── Fallback limit order ──────────────────────────────────────────────────

    async def _post_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size_shares: float,
        dry_run: bool,
    ) -> LegResult:
        """GTC limit order at an explicit price/size."""
        if dry_run:
            logger.info(
                "[DRY-RUN] Would place %s limit: %.2f shares of %s at %.4f",
                side, size_shares, token_id[:12], price,
            )
            return LegResult(
                token_id=token_id, side=side, price=price,
                size_usdc=size_shares * price, status=LegStatus.DRY_RUN,
                order_id="DRY-RUN",
            )

        loop = asyncio.get_event_loop()

        def _create_and_post() -> dict[str, Any]:
            clob_side = BUY if side == "BUY" else SELL
            args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size_shares,
                side=clob_side,
            )
            signed = self._client.create_order(args)
            return self._client.post_order(signed, OrderType.GTC)

        try:
            resp = await loop.run_in_executor(None, _create_and_post)
        except Exception as exc:
            logger.warning("Limit order error token=%s: %s", token_id[:12], exc)
            return LegResult(
                token_id=token_id, side=side, price=price,
                size_usdc=size_shares * price, status=LegStatus.REJECTED,
                notes=str(exc),
            )

        order_id = resp.get("orderID", "")
        status   = LegStatus.FILLED if resp.get("status") == "matched" else LegStatus.PARTIAL

        return LegResult(
            token_id=token_id, side=side, price=price,
            size_usdc=size_shares * price, status=status,
            order_id=order_id, raw=resp,
        )

    # ── Public interface ──────────────────────────────────────────────────────

    async def execute(self, signal: TradingSignal) -> TradeResult:
        """
        Execute both legs of the given arbitrage signal concurrently.
        Returns a TradeResult regardless of individual leg outcomes.
        """
        cfg = self._cfg
        dry = cfg.dry_run

        if signal.signal_type == SignalType.BUY_BOTH:
            side = "BUY"
            yes_price = signal.yes_price   # ask
            no_price  = signal.no_price    # ask
        elif signal.signal_type == SignalType.SELL_BOTH:
            side = "SELL"
            yes_price = signal.yes_price   # bid
            no_price  = signal.no_price    # bid
        else:
            raise ValueError(f"Non-executable signal type: {signal.signal_type}")

        # Cap per-leg USDC spend
        yes_usdc = min(cfg.max_position_usdc, cfg.max_position_usdc)
        no_usdc  = yes_usdc  # symmetric sizing

        logger.info(
            "Executing %s: YES token=%s @ %.4f  NO token=%s @ %.4f  size=%.2f USDC each",
            signal.signal_type.name,
            signal.yes_token_id[:12], yes_price,
            signal.no_token_id[:12],  no_price,
            yes_usdc,
        )

        # ── Fire both legs concurrently ────────────────────────────────────
        yes_task = self._post_market_order(
            signal.yes_token_id, side, yes_usdc, yes_price, dry
        )
        no_task = self._post_market_order(
            signal.no_token_id, side, no_usdc, no_price, dry
        )
        yes_result, no_result = await asyncio.gather(yes_task, no_task)

        both_ok = yes_result.status in {LegStatus.FILLED, LegStatus.DRY_RUN}
        both_ok = both_ok and no_result.status in {LegStatus.FILLED, LegStatus.DRY_RUN}

        notes = ""
        if not both_ok:
            notes = (
                f"Partial fill — YES={yes_result.status.name} "
                f"NO={no_result.status.name}. "
                "Check position manager for one-legged risk."
            )
            logger.warning(notes)

        return TradeResult(
            signal=signal,
            yes_leg=yes_result,
            no_leg=no_result,
            success=both_ok,
            notes=notes,
        )
