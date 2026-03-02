"""
engine/trading_engine.py
─────────────────────────
Orchestrates the entire trading loop:

  while True:
    1. Scan markets (async, batched)
    2. Detect opportunities
    3. Filter by position limits and avoid re-entry
    4. Execute best signal(s) concurrently
    5. Record results in position manager
    6. Print dashboard summary
    7. Sleep until next scan interval

The engine is designed for maximum speed:
  - Market scanning uses async HTTP with connection pooling.
  - Both legs of each trade fire concurrently (no sequential wait).
  - All blocking CLOB calls are dispatched to a thread pool so as not to
    stall the event loop.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from tabulate import tabulate

import aiohttp

from .advanced_detector import AdvancedScanResult, run_advanced_scan
from .client_manager import get_client
from .config import Config
from .market_scanner import MarketScanner, MarketSnapshot
from .opportunity_detector import OpportunityDetector, TradingSignal
from .position_manager import PositionManager, PositionState
from .trade_executor import TradeExecutor

logger = logging.getLogger(__name__)


# ─── Engine ───────────────────────────────────────────────────────────────────

class TradingEngine:
    """
    High-level trading engine that ties all components together.
    Call run() to start the trading loop.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg      = cfg
        self._detector = OpportunityDetector(
            min_profit_threshold=cfg.min_profit_threshold,
        )
        self._positions = PositionManager(
            max_open_positions=cfg.max_open_positions,
        )

        # Deferred until run() to avoid blocking __init__
        self._executor: TradeExecutor | None = None

    # ── Startup ───────────────────────────────────────────────────────────────

    def _ensure_executor(self) -> TradeExecutor:
        if self._executor is None:
            client = get_client(self._cfg)
            self._executor = TradeExecutor(client, self._cfg)
        return self._executor

    def _ensure_executor_if_needed(self) -> TradeExecutor | None:
        """Return None in dry-run so we never touch the CLOB client."""
        if self._cfg.dry_run:
            return None
        return self._ensure_executor()

    # ── Scan loop helpers ─────────────────────────────────────────────────────

    def _filter_signals(
        self, signals: list[TradingSignal]
    ) -> list[TradingSignal]:
        """
        Remove signals that:
          - We already have an open position in.
          - We cannot open due to the max_open_positions cap.
        """
        filtered: list[TradingSignal] = []
        for sig in signals:
            cid = sig.snapshot.condition_id
            if self._positions.has_position_for(cid):
                logger.debug("Skipping %s — already have position", cid[:12])
                continue
            if not self._positions.can_open_new():
                logger.info(
                    "Position cap reached (%d/%d); skipping remaining signals.",
                    self._positions.open_count,
                    self._cfg.max_open_positions,
                )
                break
            filtered.append(sig)
        return filtered

    async def _execute_signals(
        self, signals: list[TradingSignal], executor: TradeExecutor | None
    ) -> None:
        """Fire all filtered signals concurrently and record results."""
        if not signals:
            return

        if executor is None:
            # Dry-run: log signals but don't touch the CLOB
            for sig in signals:
                logger.info(
                    "[DRY-RUN] Would %s on %s  profit=%.4f",
                    sig.signal_type.name,
                    sig.snapshot.question[:55],
                    sig.estimated_profit,
                )
            return

        logger.info("Firing %d trade(s) concurrently…", len(signals))
        tasks = [executor.execute(sig) for sig in signals]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for sig, result in zip(signals, results):
            if isinstance(result, Exception):
                logger.error("Trade execution raised exception: %s", result)
                continue
            pos = self._positions.record_trade(sig, result)
            if self._cfg.dry_run:
                logger.info("[DRY-RUN] Simulated: %s", pos)

    # ── Advanced scanner (background task) ────────────────────────────────────

    async def _advanced_scan_loop(
        self, session: aiohttp.ClientSession, interval_s: float = 30.0
    ) -> None:
        """Run advanced strategies every `interval_s` seconds; log notable finds."""
        while True:
            try:
                result: AdvancedScanResult = await run_advanced_scan(session)

                if result.negrisk:
                    for sig in result.negrisk:
                        logger.warning(
                            "[NEGRISK ARB] %s  direction=%s  legs=%d  net=%.4f",
                            sig.event_title[:60], sig.direction, sig.n_legs, sig.net_profit,
                        )
                else:
                    logger.debug("Advanced scan: no negRisk arb found")

                if result.near_expiry:
                    logger.info(
                        "[NEAR-EXPIRY] %d market(s) expiring soon with unresolved prices:",
                        len(result.near_expiry),
                    )
                    for sig in result.near_expiry[:5]:
                        logger.info(
                            "  %.1fh left | p=%.3f | $%.0f 24h vol | %s",
                            sig.hours_left, sig.yes_price, sig.volume_24h,
                            sig.question[:60],
                        )

                if result.market_maker:
                    top = result.market_maker[0]
                    logger.info(
                        "[MARKET-MAKE] Best MM: spread=%.3f  vol24=$%.0f  %s",
                        top.spread, top.volume_24h, top.question[:60],
                    )

            except Exception as exc:
                logger.warning("Advanced scan error: %s", exc)

            await asyncio.sleep(interval_s)

    # ── Dashboard ─────────────────────────────────────────────────────────────

    def _print_dashboard(
        self,
        scan_duration: float,
        n_markets: int,
        n_signals: int,
        n_fired: int,
    ) -> None:
        summary = self._positions.summary()
        rows = [
            ["Scan time (s)",         f"{scan_duration:.2f}"],
            ["Markets scanned",        n_markets],
            ["Opportunities found",    n_signals],
            ["Trades fired",           n_fired],
            ["Open positions",         summary["open_positions"]],
            ["Closed positions",       summary["closed_positions"]],
            ["One-legged positions",   summary["one_legged"]],
            ["Failed positions",       summary["failed_positions"]],
            ["Realised P&L (USDC)",    f"{summary['realised_pnl_usdc']:+.4f}"],
            ["Mode",                   "DRY-RUN" if self._cfg.dry_run else "LIVE"],
        ]
        print("\n" + tabulate(rows, headers=["Metric", "Value"], tablefmt="rounded_outline"))

    def _print_signals(self, signals: list[TradingSignal]) -> None:
        if not signals:
            return
        rows = [
            [
                sig.signal_type.name,
                sig.snapshot.question[:55],
                f"{sig.yes_price:.4f}",
                f"{sig.no_price:.4f}",
                f"{sig.estimated_profit:.4f}",
                sig.notes[:60],
            ]
            for sig in signals
        ]
        print(
            tabulate(
                rows,
                headers=["Type", "Market", "YES price", "NO price", "Profit/₵", "Notes"],
                tablefmt="rounded_outline",
            )
        )

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """
        Start the trading loop.  Runs indefinitely until interrupted (Ctrl+C).
        """
        cfg      = self._cfg
        executor = self._ensure_executor_if_needed()

        if not cfg.dry_run and not cfg.has_credentials:
            raise RuntimeError(
                "PRIVATE_KEY and FUNDER_ADDRESS must be set in .env before trading. "
                "Copy .env.example to .env and fill in your wallet credentials."
            )

        logger.info(
            "Trading engine started — mode=%s  interval=%.1fs  max_pos=%d",
            "DRY-RUN" if cfg.dry_run else "LIVE",
            cfg.scan_interval_seconds,
            cfg.max_open_positions,
        )
        if cfg.dry_run:
            logger.warning(
                "DRY-RUN mode is ON.  No real orders will be placed. "
                "Set DRY_RUN=false in .env to trade live."
            )

        # How often to re-query Gamma for new markets (market list is stable).
        # Book prices must be refreshed every cycle; the market list only
        # needs refreshing once a minute.
        DISCOVER_INTERVAL_S = 60.0

        connector = aiohttp.TCPConnector(limit=50)
        async with aiohttp.ClientSession(connector=connector) as adv_session, \
                   MarketScanner(batch_size=cfg.scan_batch_size) as scanner:
            market_meta: list = []
            last_discover_at: float = 0.0

            # Start advanced scanner as a fire-and-forget background task
            adv_task = asyncio.create_task(
                self._advanced_scan_loop(adv_session, interval_s=30.0),
                name="advanced_scanner",
            )

            try:
              while True:
                loop_start = time.monotonic()

                # ── 1a. Refresh market list every 60 s ───────────────────────
                if loop_start - last_discover_at >= DISCOVER_INTERVAL_S:
                    try:
                        market_meta = await scanner.discover_markets(
                            max_markets=cfg.scan_batch_size
                        )
                        last_discover_at = time.monotonic()
                        logger.info(
                            "Market list refreshed: %d tradable markets",
                            len(market_meta),
                        )
                    except Exception as exc:
                        logger.error("Market discovery failed: %s", exc)
                        if not market_meta:
                            await asyncio.sleep(cfg.scan_interval_seconds)
                            continue

                # ── 1b. Hot-path: fetch order books only (no Gamma call) ──────
                try:
                    snapshots: list[MarketSnapshot] = await scanner.refresh_books(
                        market_meta
                    )
                except Exception as exc:
                    logger.error("Book refresh failed: %s", exc)
                    await asyncio.sleep(cfg.scan_interval_seconds)
                    continue

                # ── 2. Detect opportunities ──────────────────────────────────
                signals = self._detector.evaluate_many(snapshots)

                # ── 3. Filter by position limits ─────────────────────────────
                actionable = self._filter_signals(signals)

                # ── 4. Execute concurrently ───────────────────────────────────
                await self._execute_signals(actionable, executor)

                # ── 5. Dashboard ──────────────────────────────────────────────
                scan_duration = time.monotonic() - loop_start
                self._print_signals(actionable)
                self._print_dashboard(
                    scan_duration=scan_duration,
                    n_markets=len(snapshots),
                    n_signals=len(signals),
                    n_fired=len(actionable),
                )

                # ── 6. Wait for next interval ─────────────────────────────────
                elapsed = time.monotonic() - loop_start
                sleep_for = max(0.0, cfg.scan_interval_seconds - elapsed)
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)

            finally:
                adv_task.cancel()
                try:
                    await adv_task
                except asyncio.CancelledError:
                    pass
