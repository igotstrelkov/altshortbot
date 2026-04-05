"""
Trigger engine: primary signal + confirmation logic.
See PRD Sections 7.1, 7.2, 7.3.
"""
from __future__ import annotations

from typing import Any

import structlog

from shared.constants import (
    BID_DEPTH_THIN_THRESHOLD,
    DELTA_ZSCORE_EXPIRY,
    DELTA_ZSCORE_TRIGGER,
    TRIGGER_STALE_DRIFT_MAX,
)
from strategy.trigger.delta_aggregator import get_delta_z_score

log = structlog.get_logger()


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
    coin: str = "",
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
        log.debug("trigger_primary_miss", coin=coin, delta_z=round(delta_z, 3),
                  threshold=DELTA_ZSCORE_TRIGGER)
        return False

    # Primary fired — check confirmations
    conf_a = conf_b = conf_c = False
    depth_drop = None

    # a. Bid depth thinning
    if state["bid_depth_t_minus_30s"] > 0:
        depth_drop = (
            state["bid_depth_t_minus_30s"] - state["bid_depth_now"]
        ) / state["bid_depth_t_minus_30s"]
        conf_a = depth_drop > BID_DEPTH_THIN_THRESHOLD

    # b. Structure break
    price_series = state["price_series"]
    if len(price_series) >= 15:
        prices = list(price_series)
        conf_b = prices[-1] < min(prices[-15:-1])

    # c. VWAP break
    vwap = state["vwap_buffer"].get_vwap()
    if vwap > 0 and price_series:
        conf_c = list(price_series)[-1] < vwap

    confirmed = conf_a or conf_b or conf_c

    log.debug(
        "trigger_eval",
        coin=coin,
        delta_z=round(delta_z, 3),
        conf_a_bid_depth=conf_a,
        conf_b_structure=conf_b,
        conf_c_vwap=conf_c,
        depth_drop_pct=round(depth_drop * 100, 2) if depth_drop is not None else None,
        confirmed=confirmed,
    )
    return confirmed
