"""
Central WS message dispatcher for a single watch-list coin.
See PRD Section 7.1.
"""
from __future__ import annotations

import time
from typing import Any

from market_data.universe_snapshotter import ingest_asset_ctx


def handle_message(message: dict[str, Any], state: dict[str, Any]) -> None:
    """
    Dispatch a single WebSocket message to the correct state updater.
    Channels: trades, l2Book, activeAssetCtx, candle, pong.
    """
    channel = message.get("channel")
    now = time.time()

    if channel == "trades":
        for trade in message.get("data", []):
            price = float(trade["px"])
            size_base = float(trade["sz"])
            side = trade["side"]
            state["delta_aggregator"].on_trade(side, size_base, price)
            state["delta_aggregator"].flush_if_ready(state, now)
            state["vwap_buffer"].on_trade(price, size_base, now)

    elif channel == "l2Book":
        bids = message["data"]["levels"][0]
        mid = list(state["price_series"])[-1] if state["price_series"] else 0.0
        if mid > 0:
            threshold = mid * (1 - 0.005)
            depth = sum(
                float(b["sz"]) * float(b["px"])
                for b in bids
                if float(b["px"]) >= threshold
            )
            state["bid_depth_t_minus_30s"] = state["bid_depth_now"]
            state["bid_depth_now"] = depth

    elif channel == "activeAssetCtx":
        ingest_asset_ctx(message["data"]["ctx"], state, now)

    elif channel == "candle":
        c = message["data"]
        candle_ts = int(c["t"])
        if candle_ts != state.get("last_candle_ts_5m", 0):
            state["last_candle_ts_5m"] = candle_ts
            state["high_series_5m"].append(float(c["h"]))
            state["low_series_5m"].append(float(c["l"]))
            state["close_series_5m"].append(float(c["c"]))

    # "pong" and unknown channels are silently ignored
