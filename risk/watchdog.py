"""
Process-level dead-man switch via OS thread heartbeat monitor.
See PRD Section 9.7.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import structlog

from oms.ioc_entry import place_ioc_aggressive
from shared.constants import (
    HEARTBEAT_BEAT_INTERVAL_S,
    HEARTBEAT_TIMEOUT_S,
    IOC_EMERGENCY_SLIPPAGE_PCT,
)

log = structlog.get_logger()


class HeartbeatMonitor:
    def __init__(self, timeout_s: int = HEARTBEAT_TIMEOUT_S) -> None:
        self.last_beat = time.time()
        self.timeout_s = timeout_s
        self._lock = threading.Lock()

    def beat(self) -> None:
        with self._lock:
            self.last_beat = time.time()

    def is_dead(self) -> bool:
        with self._lock:
            return (time.time() - self.last_beat) > self.timeout_s


def start_watchdog(monitor: HeartbeatMonitor, exchange: Any) -> threading.Thread:
    """
    Start OS-thread watchdog. Daemon thread — exits when main process exits.
    On heartbeat timeout: calls emergency_flatten_all via a fresh event loop.
    """

    def _watchdog() -> None:
        while True:
            time.sleep(HEARTBEAT_BEAT_INTERVAL_S)
            if monitor.is_dead():
                log.critical(
                    "watchdog_triggered",
                    msg="PROCESS DEAD-MAN TRIGGERED — attempting emergency position flatten",
                )
                try:
                    asyncio.run(emergency_flatten_all(exchange))
                except Exception as exc:
                    log.error(
                        "watchdog_flatten_failed",
                        error=str(exc),
                        msg="Emergency flatten failed — manual intervention required",
                    )
                break

    t = threading.Thread(target=_watchdog, daemon=True)
    t.start()
    return t


async def emergency_flatten_all(exchange: Any) -> None:
    """
    Flatten all open positions via aggressive IOC orders.
    Called from watchdog thread via asyncio.run() — runs in a fresh event loop.
    Do not reuse the main-loop exchange client; construct a fresh REST client if needed.
    """
    positions: list[dict[str, Any]] = await exchange.get_open_positions()
    for pos in positions:
        coin = pos["coin"]
        size = abs(float(pos["szi"]))
        side = "buy" if float(pos["szi"]) < 0 else "sell"
        mid: float = float(pos.get("markPx", pos.get("entryPx", 0)))
        sz_decimals: int = pos.get("szDecimals", 0)
        try:
            await place_ioc_aggressive(
                coin, side, size, mid, sz_decimals,
                slippage_pct=IOC_EMERGENCY_SLIPPAGE_PCT,
            )
            log.info("watchdog_flatten_sent", coin=coin, side=side, size=size)
        except Exception as exc:
            log.error("watchdog_flatten_order_failed", coin=coin, error=str(exc))
