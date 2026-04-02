"""
Liquidation intelligence: reconstructs estimated liq levels from OI changes.
See PRD Sections 5.1, 5.2, 5.3.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Any

from shared.constants import (
    LIQ_MODEL_MAX_ENTRIES,
    SQUEEZE_FUNDING_DROP_MIN_PCT,
    SQUEEZE_FUNDING_ELEVATED_APR,
    SQUEEZE_RISK_RATIO_MAX,
)


class LiquidationModel:
    """
    Reconstructs estimated liquidation price levels from OI changes + candle direction.
    Assumes 10x average leverage. Bounded to 1440 entries per side to prevent memory growth.
    """

    MAX_ENTRIES = LIQ_MODEL_MAX_ENTRIES

    def __init__(self) -> None:
        # Each entry: (liq_price, notional_usd, unix_timestamp)
        self.long_entries: deque[tuple[float, float, float]] = deque(maxlen=self.MAX_ENTRIES)
        self.short_entries: deque[tuple[float, float, float]] = deque(maxlen=self.MAX_ENTRIES)

    def update(
        self,
        prev_oi: float,
        curr_oi: float,
        candle_open: float,
        candle_close: float,
        notional: float,
        timestamp: float,
    ) -> None:
        """Called once per 1-min candle when OI increased."""
        if curr_oi <= prev_oi:
            return

        if candle_close > candle_open:
            # Bullish candle + rising OI = new longs entering
            self.long_entries.append((candle_close * 0.90, notional, timestamp))
        else:
            # Bearish candle + rising OI = new shorts entering
            self.short_entries.append((candle_close * 1.10, notional, timestamp))

    def cluster_above(self, price: float, pct: float = 0.03) -> float:
        """USD notional of short liq levels within pct above price (squeeze risk)."""
        upper = price * (1 + pct)
        return sum(n for p, n, _ in self.short_entries if price < p <= upper)

    def cluster_below(self, price: float, pct: float = 0.03) -> float:
        """USD notional of long liq levels within pct below price (cascade potential)."""
        lower = price * (1 - pct)
        return sum(n for p, n, _ in self.long_entries if lower <= p < price)

    def new_positions_1h(self, now: float) -> tuple[float, float]:
        """Returns (short_notional_1h, long_notional_1h) opened in last 3600s."""
        cutoff = now - 3600
        return (
            sum(n for _, n, t in self.short_entries if t >= cutoff),
            sum(n for _, n, t in self.long_entries if t >= cutoff),
        )


def squeeze_risk_ratio(liq_above: float, liq_below: float) -> float:
    total = liq_above + liq_below
    return 0.0 if total == 0 else liq_above / total


def calculate_squeeze_score(
    liq_model: LiquidationModel,
    current_price: float,
    funding_series: deque[float],
    now: float | None = None,
) -> int:
    """
    Returns 0–10. Cached in state['squeeze_score'] after each update.
    score >= 5  → hard block, no entry
    score 3–4   → reduce size by 40%
    score 0–2   → normal size
    """
    if now is None:
        now = time.time()

    score = 0

    short_1h, long_1h = liq_model.new_positions_1h(now)
    if short_1h > long_1h:
        score += 3

    liq_above = liq_model.cluster_above(current_price)
    liq_below = liq_model.cluster_below(current_price)
    if liq_above > liq_below:
        score += 2

    f_series = list(funding_series)
    if len(f_series) >= 2:
        f_prev, f_now = f_series[-2], f_series[-1]
        elevated_floor = SQUEEZE_FUNDING_ELEVATED_APR / 8760
        if f_prev > elevated_floor and f_prev > 0:
            if (f_prev - f_now) / f_prev > SQUEEZE_FUNDING_DROP_MIN_PCT:
                score += 3

    if squeeze_risk_ratio(liq_above, liq_below) > SQUEEZE_RISK_RATIO_MAX:
        score += 2

    return min(score, 10)


def update_liq_model_from_candle(
    state: dict[str, Any], mark_price: float, now: float
) -> None:
    """
    Called inside ingest_asset_ctx() after every 1-min OI append.
    OI from Hyperliquid is in base units — multiply by mark_price for USD notional.
    Recalculates and caches state['squeeze_score'] after each update.
    """
    oi = list(state["oi_series"])
    px = list(state["price_series"])

    if len(oi) < 2 or len(px) < 2:
        return

    delta_oi = oi[-1] - oi[-2]
    if delta_oi <= 0:
        return

    liq_model: LiquidationModel = state["liq_model"]
    funding_series: deque[float] = state["funding_series"]

    liq_model.update(
        prev_oi=oi[-2],
        curr_oi=oi[-1],
        candle_open=px[-2],
        candle_close=px[-1],
        notional=delta_oi * mark_price,
        timestamp=now,
    )
    state["squeeze_score"] = calculate_squeeze_score(
        liq_model, px[-1], funding_series, now
    )
