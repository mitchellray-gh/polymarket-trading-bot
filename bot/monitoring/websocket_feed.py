"""
WebSocket-based real-time price monitoring.

Connects to the Polymarket WebSocket feed and publishes price updates to
registered callbacks.  Falls back to REST polling when the WebSocket is
unavailable.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable

from bot.utils.logger import get_logger

logger = get_logger(__name__)

# Polymarket WebSocket endpoint
WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

PriceCallback = Callable[[str, float], None]  # (token_id, price)


class WebSocketFeed:
    """
    Subscribes to Polymarket WebSocket and calls *on_price* for each update.

    Parameters
    ----------
    token_ids:
        List of token IDs to subscribe to.
    on_price:
        Callback invoked with ``(token_id, price)`` for each price update.
    poll_client:
        Optional CLOB client used for polling fallback.
    poll_interval:
        Seconds between polling calls in fallback mode.
    """

    def __init__(
        self,
        token_ids: list[str],
        on_price: PriceCallback,
        poll_client: Any | None = None,
        poll_interval: float = 5.0,
    ) -> None:
        self.token_ids = token_ids
        self.on_price = on_price
        self._poll_client = poll_client
        self._poll_interval = poll_interval
        self._running = False

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the feed (blocking). Runs the asyncio event loop."""
        self._running = True
        try:
            asyncio.run(self._run())
        except KeyboardInterrupt:
            logger.info("WebSocketFeed stopped by user")

    def stop(self) -> None:
        self._running = False

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                await self._connect()
                backoff = 1.0  # reset on successful connection
            except Exception as exc:
                logger.warning(
                    "WebSocket error: %s — reconnecting in %.0fs", exc, backoff
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _connect(self) -> None:
        try:
            import websockets  # noqa: PLC0415
        except ImportError:
            logger.warning("websockets library not installed; falling back to polling")
            await self._poll_loop()
            return

        logger.info("Connecting to WebSocket: %s", WS_URL)
        async with websockets.connect(WS_URL) as ws:  # type: ignore[attr-defined]
            subscribe_msg = {
                "auth": {},
                "type": "subscribe",
                "channel": "price_change",
                "markets": self.token_ids,
            }
            await ws.send(json.dumps(subscribe_msg))
            logger.info("Subscribed to %d token(s)", len(self.token_ids))

            while self._running:
                raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                self._handle_message(raw)

    async def _poll_loop(self) -> None:
        """Fallback: poll the REST API for midpoints."""
        if self._poll_client is None:
            logger.error("No poll_client configured; cannot fall back to polling")
            return

        logger.info("Polling %d token(s) every %.1fs", len(self.token_ids), self._poll_interval)
        while self._running:
            for token_id in self.token_ids:
                try:
                    price = self._poll_client.get_midpoint(token_id)
                    self.on_price(token_id, price)
                except Exception as exc:
                    logger.debug("Poll failed for %s: %s", token_id, exc)
            await asyncio.sleep(self._poll_interval)

    def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Polymarket sends a list of price events
        events = data if isinstance(data, list) else [data]
        for event in events:
            token_id = event.get("asset_id") or event.get("token_id")
            price_str = event.get("price") or event.get("mid")
            if token_id and price_str is not None:
                try:
                    self.on_price(token_id, float(price_str))
                except (ValueError, TypeError):
                    pass
