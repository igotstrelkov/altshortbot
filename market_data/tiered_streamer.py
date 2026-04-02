"""
WebSocket subscription management for the three asset tiers.
See PRD Section 14.
"""
from __future__ import annotations

import json
from typing import Any

from shared.constants import WS_URL  # noqa: F401 — re-exported for callers


async def _send_sub(ws: Any, sub: dict[str, Any]) -> None:
    """Send a single subscription with the required method wrapper."""
    await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))


async def subscribe_warmup_feeds(ws: Any, coin: str) -> None:
    """
    Tier: Warm-up candidates (passed Gates 1+2, Gate 3 not yet evaluated).
    Subscribes: trades, activeAssetCtx, 5m candle. Does NOT include l2Book.
    """
    for sub in [
        {"type": "trades", "coin": coin},
        {"type": "activeAssetCtx", "coin": coin},
        {"type": "candle", "coin": coin, "interval": "5m"},
    ]:
        await _send_sub(ws, sub)


async def subscribe_watchlist_feeds(ws: Any, coin: str) -> None:
    """
    Tier: Active watch list (passed all three gates).
    Sends ONLY the incremental l2Book subscription.
    Warm-up feeds are already active — do NOT re-subscribe them.
    """
    await _send_sub(ws, {"type": "l2Book", "coin": coin})


async def unsubscribe_warmup_feeds(ws: Any, coin: str) -> None:
    """Unsubscribe warm-up feeds when a coin is demoted from warm-up."""
    for sub in [
        {"type": "trades", "coin": coin},
        {"type": "activeAssetCtx", "coin": coin},
        {"type": "candle", "coin": coin, "interval": "5m"},
    ]:
        await ws.send(json.dumps({"method": "unsubscribe", "subscription": sub}))


async def unsubscribe_watchlist_feeds(ws: Any, coin: str) -> None:
    """Unsubscribe all feeds (warmup + l2Book) when removed from watch list."""
    for sub in [
        {"type": "trades", "coin": coin},
        {"type": "activeAssetCtx", "coin": coin},
        {"type": "candle", "coin": coin, "interval": "5m"},
        {"type": "l2Book", "coin": coin},
    ]:
        await ws.send(json.dumps({"method": "unsubscribe", "subscription": sub}))
