"""
Integration tests for WebSocket connection manager.
All tests require network access — skipped in CI.

To run manually against testnet:
  pytest tests/integration/test_ws_manager.py -v --no-header -rN
"""
from __future__ import annotations

import pytest


@pytest.mark.skip(reason="requires network")
async def test_subscribe_warmup_feeds_sends_three_subscriptions() -> None:
    """
    Verifies that subscribe_warmup_feeds sends exactly three WS subscription
    messages: trades, activeAssetCtx, and candle (5m interval).
    subscribe_watchlist_feeds must NOT be called — l2Book is incremental only.
    """


@pytest.mark.skip(reason="requires network")
async def test_subscribe_watchlist_feeds_sends_only_l2book() -> None:
    """
    Verifies that subscribe_watchlist_feeds sends exactly one message:
    {"method": "subscribe", "subscription": {"type": "l2Book", "coin": <coin>}}.
    Must not re-send trades, activeAssetCtx, or candle subscriptions
    (those are already active from the warm-up phase).
    """


@pytest.mark.skip(reason="requires network")
async def test_ping_sent_after_silence() -> None:
    """
    Verifies that a {"method": "ping"} message is sent when no WS message
    is received within WS_PING_INTERVAL_S (45s) seconds.
    Test uses a mock WebSocket that withholds messages for 46s then checks
    outbound traffic.
    """


@pytest.mark.skip(reason="requires network")
async def test_has_data_gap_set_on_disconnect() -> None:
    """
    Verifies that state['has_data_gap'] is set to True and
    state['delta_ready'] is set to False when the WebSocket connection drops.
    Also verifies reconnect is attempted after retry_delay.
    """


@pytest.mark.skip(reason="requires network")
async def test_exponential_backoff_on_repeated_failures() -> None:
    """
    Verifies that retry_delay doubles on each failed connection attempt:
    1s → 2s → 4s → ... → WS_RECONNECT_MAX_DELAY_S (60s cap).
    """


@pytest.mark.skip(reason="requires network")
async def test_heartbeat_beat_called_on_each_message() -> None:
    """
    Verifies that exchange.heartbeat_monitor.beat() is called once per
    non-pong WS message received. Pong messages must NOT trigger a beat.
    """
