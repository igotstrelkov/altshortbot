"""Gate 2 — OI divergence. See PRD Section 4.3."""
from __future__ import annotations

from collections import deque

from shared.constants import GATE2_OI_CHANGE_THRESHOLD, GATE2_PRICE_CHANGE_MAX


def gate2_passes(oi_series: deque[float], price_series: deque[float]) -> bool:
    """
    PASS if ALL of:
    - oi_series >= 245 entries, price_series >= 240 entries
    - 5-sample smoothed OI increased > 5% over last 240 samples (~4h)
    - Absolute price change over same window < 0.5%
    """
    if len(oi_series) < 245 or len(price_series) < 240:
        return False

    oi_now_window = list(oi_series)[-5:]
    oi_4h_window = list(oi_series)[-240:-235]

    if len(oi_4h_window) < 5:
        return False

    oi_now = sum(oi_now_window) / 5
    oi_4h = sum(oi_4h_window) / 5
    oi_change = (oi_now - oi_4h) / oi_4h
    px_change = abs(
        (list(price_series)[-1] - list(price_series)[-240]) / list(price_series)[-240]
    )

    return oi_change > GATE2_OI_CHANGE_THRESHOLD and px_change < GATE2_PRICE_CHANGE_MAX
