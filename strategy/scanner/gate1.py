"""Gate 1 — Funding pressure. See PRD Section 4.2."""
from __future__ import annotations

from collections import deque

from shared.constants import (
    GATE1_ANNUALISE_MULTIPLIER,
    GATE1_FUNDING_APR_THRESHOLD,
    GATE1_MIN_POSITIVE_HOURS,
    GATE1_PREMIUM_FLOOR,
)


def gate1_passes(funding_series: deque[float], premium_series: deque[float]) -> bool:
    """
    PASS if ALL of:
    - At least 8 hourly readings available
    - Latest reading annualised > 50% APR
    - At least 6 of last 8 readings are positive
    - Latest oracle premium > 0.02%
    """
    recent_8h = list(funding_series)[-8:]
    if len(recent_8h) < 8:
        return False

    annualised = recent_8h[-1] * GATE1_ANNUALISE_MULTIPLIER
    positive_count = sum(1 for f in recent_8h if f > 0)
    current_premium = premium_series[-1] if premium_series else 0.0

    return (
        annualised > GATE1_FUNDING_APR_THRESHOLD
        and positive_count >= GATE1_MIN_POSITIVE_HOURS
        and current_premium > GATE1_PREMIUM_FLOOR
    )
