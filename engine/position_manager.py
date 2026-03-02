"""
engine/position_manager.py
───────────────────────────
Tracks open positions so the engine can:

  1. Enforce the max_open_positions cap before taking new trades.
  2. Detect and handle "one-legged" fills (only YES or NO filled).
  3. Record P&L for completed (settled/closed) positions.
  4. Prevent re-entering a market that already has a live position.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Iterator

from .opportunity_detector import TradingSignal
from .trade_executor import LegStatus, TradeResult

logger = logging.getLogger(__name__)


# ─── Position state machine ───────────────────────────────────────────────────

class PositionState(Enum):
    OPEN       = auto()   # both legs filled; awaiting resolution
    ONE_LEGGED = auto()   # only one leg filled; exposed to directional risk
    CLOSED     = auto()   # manually closed or resolved
    FAILED     = auto()   # both legs rejected; no exposure


# ─── Position dataclass ───────────────────────────────────────────────────────

@dataclass
class Position:
    """
    Represents one round-trip arbitrage trade (both legs combined).
    """
    id:                 int
    condition_id:       str
    question:           str
    signal_type_name:   str    # "BUY_BOTH" or "SELL_BOTH"

    yes_token_id:   str
    no_token_id:    str

    yes_price:      float
    no_price:       float
    yes_order_id:   str
    no_order_id:    str

    size_usdc_each: float
    state:          PositionState
    opened_at:      float = field(default_factory=time.monotonic)
    closed_at:      float | None = None

    realised_pnl:   float = 0.0   # filled in on close/settlement

    def open_duration_seconds(self) -> float:
        end = self.closed_at or time.monotonic()
        return end - self.opened_at

    def __str__(self) -> str:
        return (
            f"Position#{self.id} [{self.state.name}] "
            f"{self.question[:50]!r} "
            f"YES@{self.yes_price:.4f} NO@{self.no_price:.4f} "
            f"${self.size_usdc_each:.2f}×2 pnl={self.realised_pnl:+.4f}"
        )


# ─── Position manager ─────────────────────────────────────────────────────────

class PositionManager:
    """
    In-memory store of all positions for this engine session.

    Thread-safety note: the engine is single-threaded async, so no locking
    is required.  If you adapt this for a multi-process setup, add a mutex.
    """

    def __init__(self, max_open_positions: int = 10) -> None:
        self._max_open = max_open_positions
        self._positions: dict[int, Position] = {}
        self._next_id: int = 1
        # Condition IDs of markets with active positions → avoid re-entry
        self._active_conditions: set[str] = set()

    # ── Queries ───────────────────────────────────────────────────────────────

    @property
    def open_count(self) -> int:
        return sum(
            1 for p in self._positions.values()
            if p.state in (PositionState.OPEN, PositionState.ONE_LEGGED)
        )

    def can_open_new(self) -> bool:
        return self.open_count < self._max_open

    def has_position_for(self, condition_id: str) -> bool:
        return condition_id in self._active_conditions

    def all_positions(self) -> Iterator[Position]:
        yield from self._positions.values()

    def open_positions(self) -> Iterator[Position]:
        for p in self._positions.values():
            if p.state in (PositionState.OPEN, PositionState.ONE_LEGGED):
                yield p

    # ── Mutations ─────────────────────────────────────────────────────────────

    def record_trade(
        self,
        signal: TradingSignal,
        result: TradeResult,
    ) -> Position:
        """
        Create a new Position record from a completed TradeResult.
        Called immediately after execute() returns.
        """
        ok_statuses = {LegStatus.FILLED, LegStatus.DRY_RUN}
        yes_ok = result.yes_leg.status in ok_statuses
        no_ok  = result.no_leg.status  in ok_statuses

        if yes_ok and no_ok:
            state = PositionState.OPEN
        elif yes_ok or no_ok:
            state = PositionState.ONE_LEGGED
            logger.warning(
                "One-legged fill on market %s — manual hedging required!",
                signal.snapshot.condition_id,
            )
        else:
            state = PositionState.FAILED

        pos = Position(
            id=self._next_id,
            condition_id=signal.snapshot.condition_id,
            question=signal.snapshot.question,
            signal_type_name=signal.signal_type.name,
            yes_token_id=signal.yes_token_id,
            no_token_id=signal.no_token_id,
            yes_price=result.yes_leg.price,
            no_price=result.no_leg.price,
            yes_order_id=result.yes_leg.order_id,
            no_order_id=result.no_leg.order_id,
            size_usdc_each=result.yes_leg.size_usdc,
            state=state,
        )

        self._positions[pos.id] = pos
        self._next_id += 1

        if state in (PositionState.OPEN, PositionState.ONE_LEGGED):
            self._active_conditions.add(pos.condition_id)

        logger.info("New position: %s", pos)
        return pos

    def close_position(self, position_id: int, realised_pnl: float = 0.0) -> None:
        """Mark a position as closed (e.g. after manual resolution or settlement)."""
        pos = self._positions.get(position_id)
        if pos is None:
            logger.warning("close_position: unknown id %d", position_id)
            return
        pos.state        = PositionState.CLOSED
        pos.closed_at    = time.monotonic()
        pos.realised_pnl = realised_pnl
        self._active_conditions.discard(pos.condition_id)
        logger.info("Closed %s", pos)

    # ── Summary ───────────────────────────────────────────────────────────────

    def summary(self) -> dict[str, object]:
        all_pos  = list(self._positions.values())
        closed   = [p for p in all_pos if p.state == PositionState.CLOSED]
        total_pnl = sum(p.realised_pnl for p in closed)

        return {
            "total_positions":    len(all_pos),
            "open_positions":     self.open_count,
            "closed_positions":   len(closed),
            "failed_positions":   sum(1 for p in all_pos if p.state == PositionState.FAILED),
            "one_legged":         sum(1 for p in all_pos if p.state == PositionState.ONE_LEGGED),
            "realised_pnl_usdc":  round(total_pnl, 4),
        }
