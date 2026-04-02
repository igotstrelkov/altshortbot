"""
Rolling 5-minute VWAP from live trade ticks.
One instance per watch-list coin, stored in state['vwap_buffer'].
Fed by handle_message() on every trades WS message.
See PRD Section 3.5.
"""
from __future__ import annotations

from shared.constants import VWAP_BUFFER_WINDOW_S
from shared.helpers import compute_vwap


class VwapBuffer:
    WINDOW_S = VWAP_BUFFER_WINDOW_S  # 300s

    def __init__(self) -> None:
        self._trades: list[tuple[float, float, float]] = []  # (timestamp, price, volume_usd)

    def on_trade(self, price: float, size_base: float, now: float) -> None:
        self._trades.append((now, price, size_base))
        cutoff = now - self.WINDOW_S
        self._trades = [(t, p, v) for t, p, v in self._trades if t >= cutoff]

    def get_vwap(self) -> float:
        if not self._trades:
            return 0.0
        return compute_vwap([(p, v) for _, p, v in self._trades])
