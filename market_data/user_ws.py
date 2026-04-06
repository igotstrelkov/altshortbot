"""
Account-level WebSocket: userFills subscription and fill handler.
See PRD Section 2.3 (user feeds), Section 9.5 (DailyLossTracker).

This runs as a single persistent connection for the whole account, separate
from the per-coin WS connections in ws_manager.py. It subscribes to userFills
which fires for every fill regardless of coin.

Fill handling:
- Closing fills (closedPnl != "0") for tracked coins call record_close() and
  clear position state immediately — no waiting for the 30s polling cycle.
- Opening fills are ignored (closedPnl == "0").
- isSnapshot=True messages (sent on subscribe) are skipped entirely.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
import websockets
import websockets.exceptions

from risk.daily_loss_tracker import DailyLossTracker
from shared.constants import WS_PING_INTERVAL_S, WS_RECONNECT_MAX_DELAY_S, WS_URL

log = structlog.get_logger()


def _handle_fill(
    fill: dict[str, Any],
    all_states: dict[str, Any],
    open_positions: list[str],
    daily_loss_tracker: DailyLossTracker,
) -> None:
    """
    Process a single fill dict from a userFills WS message.

    Closing fills carry a non-zero closedPnl field.  Opening fills have
    closedPnl == "0" and are ignored.  We check position_state == "open"
    as a guard against double-processing if the 30s poller already handled
    the close (or if the fill is for a position we did not open).
    """
    coin = fill.get("coin")
    if not coin:
        return

    state = all_states.get(coin)
    if state is None or state.get("position_state") != "open":
        return

    try:
        closed_pnl = float(fill.get("closedPnl") or 0)
    except (TypeError, ValueError):
        closed_pnl = 0.0

    # Skip opening fills — they have closedPnl == 0 and no dir like "Close Short"
    if closed_pnl == 0.0:
        return

    result = daily_loss_tracker.record_close(closed_pnl)
    log.info(
        "position_closed_ws",
        coin=coin,
        pnl_usd=round(closed_pnl, 4),
        dir=fill.get("dir"),
        daily_tracker_result=result,
        daily_pnl_usd=round(daily_loss_tracker.daily_pnl, 4),
    )

    # Clear position tracking — mirrors _handle_position_closed in main.py
    state["position_state"]      = None
    state["entry_price"]         = None
    state["position_size_coins"] = None
    state["stop_distance_pct"]   = None
    state["position_opened_at"]  = 0.0

    # Remove from open_positions so the 30s poller does not double-process
    if coin in open_positions:
        open_positions.remove(coin)


async def run_user_ws(
    wallet_address: str,
    all_states: dict[str, Any],
    open_positions: list[str],
    daily_loss_tracker: DailyLossTracker,
) -> None:
    """
    Persistent account-level WS connection.  Subscribes to userFills.

    Reconnects with exponential backoff on disconnect (same pattern as
    ws_manager.py).  A ping is sent every WS_PING_INTERVAL_S seconds to
    keep the connection alive past Hyperliquid's 60-second idle close.

    isSnapshot fills (sent on subscribe) are skipped — they are historical
    and would incorrectly re-trigger record_close() for already-closed positions.
    """
    retry_delay = 1.0

    while True:
        try:
            async with websockets.connect(WS_URL) as ws:
                log.info("user_ws_connected", wallet=wallet_address[:8] + "…")
                retry_delay = 1.0

                await ws.send(json.dumps({
                    "method": "subscribe",
                    "subscription": {"type": "userFills", "user": wallet_address},
                }))
                log.info("user_ws_subscribed", feed="userFills")

                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=WS_PING_INTERVAL_S)
                    except asyncio.TimeoutError:
                        await ws.send(json.dumps({"method": "ping"}))
                        log.debug("user_ws_ping_sent")
                        continue

                    msg = json.loads(raw)

                    if msg.get("channel") == "pong":
                        continue

                    if msg.get("channel") != "userFills":
                        continue

                    data = msg.get("data", {})
                    if data.get("isSnapshot"):
                        log.debug("user_ws_snapshot_skipped")
                        continue

                    for fill in data.get("fills", []):
                        _handle_fill(fill, all_states, open_positions, daily_loss_tracker)

        except (websockets.exceptions.ConnectionClosed, OSError) as exc:
            log.warning(
                "user_ws_disconnected",
                error=str(exc),
                retry_in=retry_delay,
            )
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, WS_RECONNECT_MAX_DELAY_S)
