"""Gate 3 — Price structure. See PRD Section 4.4."""
from __future__ import annotations

from collections import deque

from shared.constants import FAILED_BREAKOUT_RECOVERY_THRESHOLD, GATE3_PRICE_FROM_HIGH_MAX


def gate3_score(
    price_series: deque[float],
    high_series_5m: deque[float],
    close_series_5m: deque[float],
    vwap_5m: float,
) -> int:
    """
    Returns 0–3. Requires >= 2 to promote to watch list.

    Condition 1 (+1): price within 1% of 4h max sampled mark price
    Condition 2 (+1): price below 5m VWAP
    Condition 3 (+1): failed breakout detected in last 2h
    """
    prices = list(price_series)
    if not prices:
        return 0

    score = 0
    current_price = prices[-1]

    if len(prices) >= 240:
        high_4h = max(prices[-240:])
        if high_4h > 0 and (high_4h - current_price) / high_4h < GATE3_PRICE_FROM_HIGH_MAX:
            score += 1

    if vwap_5m > 0 and current_price < vwap_5m:
        score += 1

    if failed_breakout_detected(high_series_5m, close_series_5m):
        score += 1

    return score


def failed_breakout_detected(
    high_series_5m: deque[float],
    close_series_5m: deque[float],
    lookback: int = 24,
) -> bool:
    """
    24 × 5m candles = 2h. Detects: new N-candle high formed, then price closed
    > 0.5% below that peak, with the peak not in the last 3 candles.
    """
    highs = list(high_series_5m)[-lookback:]
    closes = list(close_series_5m)[-lookback:]
    if len(highs) < lookback:
        return False

    peak_idx = highs.index(max(highs))
    if peak_idx >= lookback - 3:
        return False

    return (highs[peak_idx] - closes[-1]) / highs[peak_idx] > FAILED_BREAKOUT_RECOVERY_THRESHOLD
