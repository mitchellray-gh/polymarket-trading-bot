"""
engine/opportunity_detector.py
────────────────────────────────
Classifies each MarketSnapshot into one of four signal types:

  BUY_BOTH    — combined ask price < 1.00 by at least min_profit_threshold
                buy YES at ask + buy NO at ask → guaranteed $1 payout
                net profit = 1.00 − (ask_yes + ask_no)

  SELL_BOTH   — combined bid price > 1.00 by at least min_profit_threshold
                sell YES at bid + sell NO at bid → receive > $1, cost $1 to settle
                net profit = (bid_yes + bid_no) − 1.00

  EQUAL_MONEY — both legs priced ≈ $0.50; no edge but useful for monitoring

  NO_EDGE     — market is efficiently priced; skip

Why can this happen on Polymarket?
────────────────────────────────────
• Thin liquidity leaves stale resting orders far from fair value.
• Large one-sided trades temporarily move a single leg's book.
• Automated market makers reprice slowly after news events.
• The CLOB matches instantaneously, so a resting order at 0.48 YES + 0.48 NO
  lets you buy both for $0.96 and collect $1.00 at resolution — 4 cent profit.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from .market_scanner import MarketSnapshot

logger = logging.getLogger(__name__)


# ─── Signal taxonomy ──────────────────────────────────────────────────────────

class SignalType(Enum):
    BUY_BOTH    = auto()   # arbitrage: buy YES + buy NO
    SELL_BOTH   = auto()   # arbitrage: sell YES + sell NO
    EQUAL_MONEY = auto()   # fair value ~$0.50 each — no edge
    NO_EDGE     = auto()   # efficiently priced, skip


@dataclass
class TradingSignal:
    """
    A detected opportunity attached to a specific market snapshot.

    Fields
    ──────
    snapshot        — the market that triggered the signal
    signal_type     — one of the four classes above
    estimated_profit — net USDC profit per $1 notional (0.00–1.00)
    yes_price       — price used for YES leg (ask for BUY_BOTH, bid for SELL_BOTH)
    no_price        — price used for NO leg
    notes           — human-readable explanation
    """
    snapshot:          MarketSnapshot
    signal_type:       SignalType
    estimated_profit:  float
    yes_price:         float
    no_price:          float
    notes:             str = ""

    # ── Convenience properties ────────────────────────────────────────────────

    @property
    def is_actionable(self) -> bool:
        """True for BUY_BOTH and SELL_BOTH — these trigger real orders."""
        return self.signal_type in (SignalType.BUY_BOTH, SignalType.SELL_BOTH)

    @property
    def yes_token_id(self) -> str:
        return self.snapshot.yes_token_id

    @property
    def no_token_id(self) -> str:
        return self.snapshot.no_token_id

    def profit_for_size(self, size_usdc: float) -> float:
        """Expected profit in USDC for a given position size."""
        return round(self.estimated_profit * size_usdc, 6)

    def __str__(self) -> str:  # pragma: no cover
        return (
            f"[{self.signal_type.name}] "
            f"{self.snapshot.question[:60]!r} "
            f"YES={self.yes_price:.4f} NO={self.no_price:.4f} "
            f"profit/notional={self.estimated_profit:.4f}"
        )


# ─── Opportunity detector ─────────────────────────────────────────────────────

class OpportunityDetector:
    """
    Stateless detector: given a MarketSnapshot and a minimum profit threshold,
    classify the market and return a TradingSignal.

    min_profit_threshold
        Minimum net profit (in USDC per $1 notional) to consider a trade
        worthwhile.  Must be high enough to cover taker fees (~0.1 % on
        Polymarket) and Polygon gas costs (negligible but non-zero).
        A safe value is 0.005 (0.5 cents per dollar).

    equal_money_tolerance
        How close to 0.50 each leg must be to be called "equal money".
        Default 0.01 (within ±1 cent of 50 c).
    """

    def __init__(
        self,
        min_profit_threshold: float = 0.005,
        equal_money_tolerance: float = 0.01,
    ) -> None:
        self.min_profit = min_profit_threshold
        self.equal_money_tol = equal_money_tolerance

    # ── Internal classifiers ──────────────────────────────────────────────────

    def _classify_buy_both(self, snap: MarketSnapshot) -> Optional[TradingSignal]:
        """
        BUY_BOTH: ask(YES) + ask(NO) < 1.00 − threshold
        Pay less than $1 for a combined position that settles at exactly $1.
        """
        combined = snap.combined_ask
        if combined is None:
            return None
        net_profit = round(1.0 - combined, 6)
        if net_profit < self.min_profit:
            return None

        logger.debug(
            "BUY_BOTH candidate: %s  combined_ask=%.4f  profit=%.4f",
            snap.question[:50], combined, net_profit,
        )
        return TradingSignal(
            snapshot=snap,
            signal_type=SignalType.BUY_BOTH,
            estimated_profit=net_profit,
            yes_price=snap.yes_best_ask,   # type: ignore[arg-type]
            no_price=snap.no_best_ask,      # type: ignore[arg-type]
            notes=(
                f"Buy YES@{snap.yes_best_ask:.4f} + NO@{snap.no_best_ask:.4f} "
                f"= {combined:.4f}. Settle at $1. Net +{net_profit:.4f}."
            ),
        )

    def _classify_sell_both(self, snap: MarketSnapshot) -> Optional[TradingSignal]:
        """
        SELL_BOTH: bid(YES) + bid(NO) > 1.00 + threshold
        Receive more than $1 up-front; the settlement cost is exactly $1.
        """
        combined = snap.combined_bid
        if combined is None:
            return None
        net_profit = round(combined - 1.0, 6)
        if net_profit < self.min_profit:
            return None

        logger.debug(
            "SELL_BOTH candidate: %s  combined_bid=%.4f  profit=%.4f",
            snap.question[:50], combined, net_profit,
        )
        return TradingSignal(
            snapshot=snap,
            signal_type=SignalType.SELL_BOTH,
            estimated_profit=net_profit,
            yes_price=snap.yes_best_bid,   # type: ignore[arg-type]
            no_price=snap.no_best_bid,      # type: ignore[arg-type]
            notes=(
                f"Sell YES@{snap.yes_best_bid:.4f} + NO@{snap.no_best_bid:.4f} "
                f"= {combined:.4f}. Cost $1. Net +{net_profit:.4f}."
            ),
        )

    def _classify_equal_money(self, snap: MarketSnapshot) -> Optional[TradingSignal]:
        """
        EQUAL_MONEY: both legs price near $0.50 → fair-value reference.
        No actionable edge, but tracked for logging/monitoring.
        """
        ya = snap.yes_best_ask
        na = snap.no_best_ask
        if ya is None or na is None:
            return None
        if abs(ya - 0.5) <= self.equal_money_tol and abs(na - 0.5) <= self.equal_money_tol:
            return TradingSignal(
                snapshot=snap,
                signal_type=SignalType.EQUAL_MONEY,
                estimated_profit=0.0,
                yes_price=ya,
                no_price=na,
                notes=f"Fair value: YES≈{ya:.4f} NO≈{na:.4f}",
            )
        return None

    # ── Public interface ──────────────────────────────────────────────────────

    def evaluate(self, snap: MarketSnapshot) -> TradingSignal:
        """
        Evaluate a single market snapshot and return the best TradingSignal.
        Priority: BUY_BOTH > SELL_BOTH > EQUAL_MONEY > NO_EDGE.
        """
        signal = (
            self._classify_buy_both(snap)
            or self._classify_sell_both(snap)
            or self._classify_equal_money(snap)
        )
        if signal is not None:
            return signal

        # No actionable edge
        yes_mid = snap.yes_book.midpoint if snap.yes_book else None
        no_mid  = snap.no_book.midpoint  if snap.no_book  else None
        return TradingSignal(
            snapshot=snap,
            signal_type=SignalType.NO_EDGE,
            estimated_profit=0.0,
            yes_price=yes_mid or 0.0,
            no_price=no_mid or 0.0,
            notes="Efficiently priced; no arbitrage detected.",
        )

    def evaluate_many(
        self, snapshots: list[MarketSnapshot]
    ) -> list[TradingSignal]:
        """
        Evaluate a list of snapshots and return *only the actionable signals*,
        sorted by descending estimated_profit (highest edge first).
        """
        actionable: list[TradingSignal] = []
        for snap in snapshots:
            sig = self.evaluate(snap)
            if sig.is_actionable:
                actionable.append(sig)

        actionable.sort(key=lambda s: s.estimated_profit, reverse=True)
        logger.info(
            "Evaluated %d markets → %d actionable signals found",
            len(snapshots), len(actionable),
        )
        return actionable
