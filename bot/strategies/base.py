"""
Abstract base class for all trading strategies.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from bot.utils.logger import get_logger


class BaseStrategy(ABC):
    """All strategies must inherit from this class and implement ``run``."""

    def __init__(self, client: Any, order_manager: Any, risk_manager: Any) -> None:
        self._client = client
        self._order_manager = order_manager
        self._risk_manager = risk_manager
        self.logger = get_logger(self.__class__.__name__)

    @abstractmethod
    def run(self, market: dict[str, Any]) -> None:
        """Evaluate *market* and place orders if appropriate."""

    def name(self) -> str:
        return self.__class__.__name__
