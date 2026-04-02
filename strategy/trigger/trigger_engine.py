"""
Trigger engine: primary signal + confirmation logic.
See PRD Sections 7.1, 7.2, 7.3.
"""
from __future__ import annotations

from typing import Any

from shared.constants import (
    BID_DEPTH_THIN_THRESHOLD,
    DELTA_ZSCORE_EXPIRY,
    DELTA_ZSCORE_TRIGGER,
    TRIGGER_STALE_DRIFT_MAX,
)
from strategy.trigger.delta_aggregator import get_delta_z_score


def trigger_is_valid(
    trigger_price: float,
    current_mid: float,
    delta_z_score: float,
) -> bool:
    """
    Called before primary IOC placement and before aggressive IOC fallback.
    Returns False if conditions have deteriorated since the trigger fired.
    """
    price_drift = abs(current_mid - trigger_price) / trigger_price
    if price_drift > TRIGGER_STALE_DRIFT_MAX:
        return False

    if delta_z_score >= DELTA_ZSCORE_EXPIRY:
        return False

    return True


def evaluate_trigger(
    state: dict[str, Any],
    trigger_price: float,
    current_mid: float,
) -> bool:
    """
    Returns True only if primary fires AND at least one confirmation is True.

    Primary: delta z-score < DELTA_ZSCORE_TRIGGER (-2.0)

    Confirmations (at least one required):
      a. Bid depth thinning: depth dropped > 25% vs 30s ago
      b. Structure break: price below 15-sample swing low
      c. VWAP break: price below 5m VWAP
    """
    delta_z = get_delta_z_score(state)
    if delta_z >= DELTA_ZSCORE_TRIGGER:
        return False

    confirmed = False

    # a. Bid depth thinning
    if state["bid_depth_t_minus_30s"] > 0:
        depth_drop = (
            state["bid_depth_t_minus_30s"] - state["bid_depth_now"]
        ) / state["bid_depth_t_minus_30s"]
        if depth_drop > BID_DEPTH_THIN_THRESHOLD:
            confirmed = True

    # b. Structure break: current price below swing low of previous 14 samples
    if not confirmed:
        price_series = state["price_series"]
        if len(price_series) >= 15:
            prices = list(price_series)
            if prices[-1] < min(prices[-15:-1]):
                confirmed = True

    # c. VWAP break
    if not confirmed:
        vwap = state["vwap_buffer"].get_vwap()
        price_series = state["price_series"]
        if vwap > 0 and price_series and list(price_series)[-1] < vwap:
            confirmed = True

    return confirmed
