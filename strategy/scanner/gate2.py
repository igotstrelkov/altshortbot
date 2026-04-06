"""Gate 2 — OI divergence. See PRD Section 4.3."""
from __future__ import annotations

from collections import deque

import structlog

from shared.constants import GATE2_OI_CHANGE_THRESHOLD, GATE2_PRICE_CHANGE_MAX

log = structlog.get_logger()


def gate2_passes(oi_series: deque[float], price_series: deque[float], coin: str = "") -> bool:
    """
    PASS if ALL of:
    - oi_series >= 245 entries, price_series >= 240 entries
    - 5-sample smoothed OI increased > 5% over last 240 samples (~4h)
    - Absolute price change over same window < 0.5%
    """
    if len(oi_series) < 245 or len(price_series) < 240:
        log.debug(
            "gate2_skip",
            coin=coin,
            reason="insufficient_data",
            have_oi=len(oi_series),
            have_price=len(price_series),
            need_oi=245,
            need_price=240,
        )
        return False

    oi_now_window = list(oi_series)[-5:]
    oi_4h_window = list(oi_series)[-240:-235]

    if len(oi_4h_window) < 5:
        log.debug("gate2_skip", coin=coin, reason="oi_4h_window_too_small", have=len(oi_4h_window))
        return False

    oi_now = sum(oi_now_window) / 5
    oi_4h = sum(oi_4h_window) / 5
    if oi_4h == 0:
        log.debug("gate2_skip", coin=coin, reason="oi_4h_zero")
        return False
    oi_change = (oi_now - oi_4h) / oi_4h
    px_change = abs(
        (list(price_series)[-1] - list(price_series)[-240]) / list(price_series)[-240]
    )

    passed = oi_change > GATE2_OI_CHANGE_THRESHOLD and px_change < GATE2_PRICE_CHANGE_MAX
    log.debug(
        "gate2_eval",
        coin=coin,
        oi_change_pct=round(oi_change * 100, 3),
        px_change_pct=round(px_change * 100, 3),
        oi_threshold_pct=GATE2_OI_CHANGE_THRESHOLD * 100,
        px_max_pct=GATE2_PRICE_CHANGE_MAX * 100,
        passed=passed,
    )
    return passed
