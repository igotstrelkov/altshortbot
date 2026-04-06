"""
allMids WebSocket subscription: universe-wide mid price feed.
See PRD Section 2.3 (market data feeds).

Maintains a single persistent connection for the whole process.
Updates state["mid_price"] for every coin in all_states on each
allMids message (~2s cadence from Hyperliquid).

Use state["mid_price"] for order pricing. Fall back to
price_series[-1] if mid_price is still 0.0 (pre-first-message).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
import websockets
import websockets.exceptions

from shared.constants import WS_PING_INTERVAL_S, WS_RECONNECT_MAX_DELAY_S, WS_URL

log = structlog.get_logger()


async def run_all_mids_ws(all_states: dict[str, Any]) -> None:
    """
    Persistent allMids WS connection.

    Subscribes to {"type": "allMids"} and writes the mid price for
    every known coin into all_states[coin]["mid_price"].

    Reconnects with exponential backoff on disconnect (same pattern as
    ws_manager.py).  A ping is sent every WS_PING_INTERVAL_S seconds to
    keep the connection alive past Hyperliquid's 60-second idle close.
    """
    retry_delay = 1.0

    while True:
        try:
            async with websockets.connect(WS_URL) as ws:
                log.info("all_mids_ws_connected")
                retry_delay = 1.0

                await ws.send(json.dumps({
                    "method": "subscribe",
                    "subscription": {"type": "allMids"},
                }))
                log.info("all_mids_ws_subscribed", feed="allMids")

                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=WS_PING_INTERVAL_S)
                    except asyncio.TimeoutError:
                        await ws.send(json.dumps({"method": "ping"}))
                        log.debug("all_mids_ws_ping_sent")
                        continue

                    msg = json.loads(raw)

                    if msg.get("channel") == "pong":
                        continue

                    if msg.get("channel") != "allMids":
                        continue

                    mids: dict[str, str] = msg.get("data", {}).get("mids", {})
                    for coin, mid_str in mids.items():
                        state = all_states.get(coin)
                        if state is None:
                            continue
                        try:
                            state["mid_price"] = float(mid_str)
                        except (TypeError, ValueError):
                            pass

        except (websockets.exceptions.ConnectionClosed, OSError) as exc:
            log.warning(
                "all_mids_ws_disconnected",
                error=str(exc),
                retry_in=retry_delay,
            )
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, WS_RECONNECT_MAX_DELAY_S)
