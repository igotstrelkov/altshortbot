"""Gate 1 — Funding pressure. See PRD Section 4.2."""
from __future__ import annotations

from collections import deque

import structlog

from shared.constants import (
    GATE1_ANNUALISE_MULTIPLIER,
    GATE1_FUNDING_APR_THRESHOLD,
    GATE1_MIN_POSITIVE_HOURS,
    GATE1_PREMIUM_FLOOR,
)

log = structlog.get_logger()


def gate1_passes(
    funding_series: deque[float], premium_series: deque[float], coin: str = ""
) -> bool:
    """
    PASS if ALL of:
    - At least 8 hourly readings available
    - Latest reading annualised > 50% APR
    - At least 6 of last 8 readings are positive
    - Latest oracle premium > 0.02%
    """
    recent_8h = list(funding_series)[-8:]
    if len(recent_8h) < 8:
        log.debug("gate1_skip", coin=coin, reason="insufficient_data", have=len(recent_8h), need=8)
        return False

    annualised = recent_8h[-1] * GATE1_ANNUALISE_MULTIPLIER
    positive_count = sum(1 for f in recent_8h if f > 0)
    current_premium = premium_series[-1] if premium_series else 0.0

    passed = (
        annualised > GATE1_FUNDING_APR_THRESHOLD
        and positive_count >= GATE1_MIN_POSITIVE_HOURS
        and current_premium > GATE1_PREMIUM_FLOOR
    )
    log.debug(
        "gate1_eval",
        coin=coin,
        annualised_apr_pct=round(annualised * 100, 2),
        positive_count=positive_count,
        current_premium_pct=round(current_premium * 100, 4),
        apr_threshold_pct=GATE1_FUNDING_APR_THRESHOLD * 100,
        min_positive_hours=GATE1_MIN_POSITIVE_HOURS,
        premium_floor_pct=GATE1_PREMIUM_FLOOR * 100,
        passed=passed,
    )
    return passed
