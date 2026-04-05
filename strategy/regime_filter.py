"""
Regime filter: BTC trend + alt breadth gating.
See PRD Section 6.
"""
from __future__ import annotations

import asyncio
import time

import structlog

from market_data.universe_snapshotter import rest_post
from shared.constants import (
    ALT_BREADTH_DISABLE_THRESHOLD,
    ALT_BREADTH_UP_PCT,
    BTC_SLOPE_DISABLE_THRESHOLD,
    BTC_SLOPE_REDUCE_THRESHOLD,
    REGIME_CANDLE_HISTORY_HOURS,
    REGIME_MIN_BTC_HISTORY,
)
from shared.helpers import ema

log = structlog.get_logger()


async def refresh_1h_closes(universe_coins: list[str]) -> dict[str, list[float]]:
    """
    Call every 60 minutes. Returns {coin: [1h closes]} for last
    REGIME_CANDLE_HISTORY_HOURS hours. BTC is always included.
    """
    coin_closes: dict[str, list[float]] = {}
    coins = list(set(universe_coins + ["BTC"]))
    for i, coin in enumerate(coins):
        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": "1h",
                "startTime": int((time.time() - REGIME_CANDLE_HISTORY_HOURS * 3600) * 1000),
            },
        }
        candles = await rest_post("/info", payload)
        if candles:
            coin_closes[coin] = [float(c["c"]) for c in candles]
        if i < len(coins) - 1:
            await asyncio.sleep(0.1)
    return coin_closes


def regime_filter(
    btc_closes_1h: list[float],
    watch_list_coins: list[str],
    coin_closes_1h: dict[str, list[float]],
) -> str:
    """
    Returns 'NORMAL' | 'REDUCED' | 'DISABLED'.
    btc_closes_1h: coin_closes_1h['BTC'] from refresh_1h_closes().
    """
    if len(btc_closes_1h) < REGIME_MIN_BTC_HISTORY:
        log.info("regime_filter_result", result="DISABLED", reason="insufficient_btc_history",
                 btc_closes=len(btc_closes_1h), required=REGIME_MIN_BTC_HISTORY)
        return "DISABLED"

    btc_ema_20 = ema(btc_closes_1h, 20)
    btc_ema_50 = ema(btc_closes_1h, 50)
    btc_slope = (btc_ema_20[-1] - btc_ema_20[-6]) / btc_ema_20[-6]

    if btc_ema_20[-1] > btc_ema_50[-1] and btc_slope > BTC_SLOPE_DISABLE_THRESHOLD:
        log.info("regime_filter_result", result="DISABLED", reason="btc_uptrend_strong",
                 btc_ema20=round(btc_ema_20[-1], 2), btc_ema50=round(btc_ema_50[-1], 2),
                 btc_slope_pct=round(btc_slope * 100, 3),
                 disable_threshold_pct=BTC_SLOPE_DISABLE_THRESHOLD * 100)
        return "DISABLED"

    if btc_ema_20[-1] > btc_ema_50[-1] and btc_slope > BTC_SLOPE_REDUCE_THRESHOLD:
        log.info("regime_filter_result", result="REDUCED", reason="btc_uptrend_mild",
                 btc_ema20=round(btc_ema_20[-1], 2), btc_ema50=round(btc_ema_50[-1], 2),
                 btc_slope_pct=round(btc_slope * 100, 3))
        return "REDUCED"

    coins_up = sum(
        1
        for coin in watch_list_coins
        if len(coin_closes_1h.get(coin, [])) >= 2
        and (coin_closes_1h[coin][-1] - coin_closes_1h[coin][-2])
        / coin_closes_1h[coin][-2]
        > ALT_BREADTH_UP_PCT
    )
    alt_breadth_pct = (coins_up / len(watch_list_coins)) if watch_list_coins else 0.0
    if watch_list_coins and alt_breadth_pct > ALT_BREADTH_DISABLE_THRESHOLD:
        log.info("regime_filter_result", result="DISABLED", reason="alt_breadth",
                 coins_up=coins_up, watch_list_size=len(watch_list_coins),
                 breadth_pct=round(alt_breadth_pct * 100, 1),
                 threshold_pct=ALT_BREADTH_DISABLE_THRESHOLD * 100)
        return "DISABLED"

    log.info("regime_filter_result", result="NORMAL",
             btc_ema20=round(btc_ema_20[-1], 2), btc_ema50=round(btc_ema_50[-1], 2),
             btc_slope_pct=round(btc_slope * 100, 3))
    return "NORMAL"
