"""
Aggregates tick-level trades into 60-second net sell volume deltas.
One instance per watch-list coin, stored in state['delta_aggregator'].
See PRD Sections 3.6, 7.1.

Hyperliquid trade WS 'side' field:
  'A' = ask aggressor = taker SELL
  'B' = bid aggressor = taker BUY
"""
from __future__ import annotations

import statistics
import time

from shared.constants import DELTA_COLD_START_PERIODS, DELTA_WINDOW_S


class DeltaAggregator:
    WINDOW_S = DELTA_WINDOW_S  # 60s

    def __init__(self) -> None:
        self.sell_vol_usd: float = 0.0
        self.buy_vol_usd: float = 0.0
        self.window_start: float = time.time()

    def on_trade(self, side: str, size_base: float, price: float) -> None:
        usd = size_base * price
        if side == "A":
            self.sell_vol_usd += usd
        elif side == "B":
            self.buy_vol_usd += usd

    def flush_if_ready(self, state: dict[str, object], now: float) -> bool:
        """Call after every trade tick. Returns True when a window is flushed."""
        if now - self.window_start >= self.WINDOW_S:
            update_delta_state(state, self.sell_vol_usd - self.buy_vol_usd)
            self.sell_vol_usd = 0.0
            self.buy_vol_usd = 0.0
            self.window_start = now
            return True
        return False


def update_delta_state(state: dict[str, object], new_delta_60s: float) -> None:
    """Called by DeltaAggregator.flush_if_ready() every 60 seconds."""
    from collections import deque

    state["trade_delta_60s"] = new_delta_60s
    delta_history = state["delta_history"]
    assert isinstance(delta_history, deque)
    delta_history.append(new_delta_60s)

    if len(delta_history) >= DELTA_COLD_START_PERIODS:
        state["delta_ready"] = True
        state["delta_mean_10m"] = statistics.mean(delta_history)
        std = statistics.stdev(delta_history)
        state["delta_std_10m"] = std if std > 0 else 1e-9
    else:
        state["delta_ready"] = False
        state["delta_mean_10m"] = 0.0
        state["delta_std_10m"] = 0.0


def get_delta_z_score(state: dict[str, object]) -> float:
    """
    Returns 0.0 if delta_ready is False or std is zero.
    Primary trigger fires when this returns < DELTA_ZSCORE_TRIGGER (-2.0).
    """
    if not state["delta_ready"] or state["delta_std_10m"] == 0:
        return 0.0
    delta = state["trade_delta_60s"]
    mean = state["delta_mean_10m"]
    std = state["delta_std_10m"]
    assert isinstance(delta, float)
    assert isinstance(mean, float)
    assert isinstance(std, float)
    return (delta - mean) / std
