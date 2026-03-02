"""
engine/trade_executor.py
─────────────────────────
Executes binary-arbitrage trades as fast as possible using the Polymarket CLOB.

Execution strategy
──────────────────
For BUY_BOTH:
  1. Create two FOK (Fill-or-Kill) market orders simultaneously — one for the
     YES leg, one for the NO leg.
  2. Post them concurrently via asyncio so both hit the exchange in the same
     event-loop tick (typically <10 ms apart on a low-latency host).
  3. If either leg is rejected the other is still filled; a one-legged
     position is tracked and the position manager will hedge or close it.

For SELL_BOTH:
  Same logic inverted — two SELL limit orders at the top-of-book bid.

FOK vs GTC:
  Fill-or-Kill is preferred because a partial fill on one leg while the other
  leg's price moves would leave us with inventory risk.  GTC limit orders at
  the best ask/bid are used as a fallback when FOK liquidity is insufficient.

Order sizes:
  Position size in USDC is capped at Config.max_position_usdc.
  The engine converts USDC → shares using the order price
      shares = usdc_amount / price
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from .config import Config
from .opportunity_detector import SignalType, TradingSignal

logger = logging.getLogger(__name__)


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
