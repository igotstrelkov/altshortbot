"""
Rolling 5-minute VWAP from live trade ticks.
One instance per watch-list coin, stored in state['vwap_buffer'].
Fed by handle_message() on every trades WS message.
See PRD Section 3.5.
"""
from __future__ import annotations

from collections import deque

from shared.constants import VWAP_BUFFER_WINDOW_S
from shared.helpers import compute_vwap


class VwapBuffer:
    WINDOW_S = VWAP_BUFFER_WINDOW_S  # 300s

    def __init__(self) -> None:
        # (timestamp, price, size_base) — deque for O(1) head eviction
        self._trades: deque[tuple[float, float, float]] = deque()

    def on_trade(self, price: float, size_base: float, now: float) -> None:
        self._trades.append((now, price, size_base))
        cutoff = now - self.WINDOW_S
        while self._trades and self._trades[0][0] < cutoff:
            self._trades.popleft()

    def get_vwap(self) -> float:
        if not self._trades:
            return 0.0
        return compute_vwap([(p, v) for _, p, v in self._trades])
