"""
WebSocket connection manager with reconnect + exponential backoff.
See PRD Section 14.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
import websockets
import websockets.exceptions

from market_data.state_normaliser import handle_message
from market_data.tiered_streamer import subscribe_warmup_feeds
from market_data.universe_snapshotter import refresh_funding_from_rest
from shared.constants import WS_PING_INTERVAL_S, WS_RECONNECT_MAX_DELAY_S, WS_URL
from shared.state_factory import create_asset_state

log = structlog.get_logger()


async def ws_connection_manager(
    coin: str,
    state: dict[str, Any],
    exchange: Any,
) -> None:
    """
    Persistent WebSocket connection with exponential backoff on failure.

    Always starts in warm-up mode (subscribe_warmup_feeds only).
    The main loop calls subscribe_watchlist_feeds(ws, coin) separately
    when the coin is promoted to the active watch list.

    Retry delays: 1s → 2s → 4s → ... → WS_RECONNECT_MAX_DELAY_S (60s).
    """
    retry_delay = 1.0

    while True:
        try:
            async with websockets.connect(WS_URL) as ws:
                log.info("ws_connected", coin=coin)
                retry_delay = 1.0  # reset on successful connection

                await subscribe_warmup_feeds(ws, coin)
                await refresh_funding_from_rest(coin, state)
                state["has_data_gap"] = False
                state["delta_ready"] = False

                while True:
                    try:
                        raw = await asyncio.wait_for(
                            ws.recv(),
                            timeout=WS_PING_INTERVAL_S,
                        )
                        message = json.loads(raw)
                        if message.get("channel") == "pong":
                            continue
                        handle_message(message, state)
                        exchange.heartbeat_monitor.beat()

                    except asyncio.TimeoutError:
                        await ws.send(json.dumps({"method": "ping"}))
                        log.debug("ws_ping_sent", coin=coin)

        except (websockets.exceptions.ConnectionClosed, OSError) as exc:
            log.warning(
                "ws_disconnected",
                coin=coin,
                error=str(exc),
                retry_in=retry_delay,
            )
            state["has_data_gap"] = True
            state["delta_ready"] = False
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, WS_RECONNECT_MAX_DELAY_S)


async def run_ws_for_coin(
    coin: str,
    all_states: dict[str, dict[str, Any]],
    exchange: Any,
) -> None:
    """
    Ensure state exists for coin, then run the connection manager.
    Intended to be launched as an asyncio Task per coin.
    """
    if coin not in all_states:
        all_states[coin] = create_asset_state()
    await ws_connection_manager(coin, all_states[coin], exchange)
