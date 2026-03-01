"""
Alerting system: console + optional Telegram notifications.
"""

from __future__ import annotations

from typing import Any

from config import settings
from bot.utils.logger import get_logger

logger = get_logger(__name__)


def _send_telegram(message: str) -> None:
    """Send *message* to the configured Telegram chat (best-effort)."""
    if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
        return
    try:
        import requests  # noqa: PLC0415

        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(
            url,
            json={"chat_id": settings.TELEGRAM_CHAT_ID, "text": message},
            timeout=5,
        )
    except Exception as exc:  # pragma: no cover
        logger.debug("Telegram send failed: %s", exc)


def alert_opportunity(opportunity: Any) -> None:
    """Alert when a trading opportunity is detected."""
    msg = (
        f"🎯 Opportunity detected: {getattr(opportunity, 'question', '')[:60]}\n"
        f"Strategy: {getattr(opportunity, 'strategy', 'unknown')}\n"
        f"Expected profit: {getattr(opportunity, 'expected_profit', 0):.4f}"
    )
    logger.info(msg)
    _send_telegram(msg)


def alert_order_placed(order: dict[str, Any]) -> None:
    """Alert when an order is placed."""
    paper = "[PAPER] " if order.get("paper") else ""
    msg = (
        f"{paper}📝 Order placed: {order.get('order_id', '')[:16]}\n"
        f"Side: {order.get('side')} | Size: {order.get('size'):.2f} "
        f"| Price: {order.get('price')}"
    )
    logger.info(msg)
    _send_telegram(msg)


def alert_order_filled(order: dict[str, Any]) -> None:
    """Alert when an order is filled."""
    paper = "[PAPER] " if order.get("paper") else ""
    msg = (
        f"{paper}✅ Order filled: {order.get('order_id', '')[:16]}\n"
        f"Side: {order.get('side')} | Size: {order.get('size'):.2f}"
    )
    logger.info(msg)
    _send_telegram(msg)


def alert_error(context: str, error: Exception) -> None:
    """Alert on errors / halts."""
    msg = f"🚨 Error in {context}: {error}"
    logger.error(msg)
    _send_telegram(msg)


def alert_stop_loss(balance: float, starting_balance: float) -> None:
    """Alert when the stop-loss is triggered."""
    loss_pct = (starting_balance - balance) / starting_balance * 100 if starting_balance else 0
    msg = (
        f"🛑 Stop-loss triggered!\n"
        f"Balance: ${balance:.2f} (started: ${starting_balance:.2f}, "
        f"loss: {loss_pct:.1f}%)"
    )
    logger.error(msg)
    _send_telegram(msg)
