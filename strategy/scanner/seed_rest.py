"""Seeds Gate 3 series from REST candleSnapshot. See PRD Section 4.1."""
from __future__ import annotations

import time
from typing import Any

from market_data.universe_snapshotter import rest_post
from shared.logging_config import log


async def seed_gate3_series_from_rest(coin: str, state: dict[str, Any]) -> None:
    """
    Pre-populates price_series (245 × 1m closes) and high/low/close_series_5m
    (24 × 5m candles) from REST candleSnapshot so Gate 3 is evaluable once
    warm-up completes. Live updates come from ingest_asset_ctx() and WS trades.
    """
    now_ms = int(time.time() * 1000)

    payload_5m = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": "5m",
            "startTime": now_ms - 24 * 5 * 60 * 1000,
            "endTime": now_ms,
        },
    }
    candles_5m = await rest_post("/info", payload_5m)
    for c in candles_5m[-24:]:
        state["high_series_5m"].append(float(c["h"]))
        state["low_series_5m"].append(float(c["l"]))
        state["close_series_5m"].append(float(c["c"]))

    payload_1m = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": "1m",
            "startTime": now_ms - 245 * 60 * 1000,
            "endTime": now_ms,
        },
    }
    candles_1m = await rest_post("/info", payload_1m)
    for c in candles_1m[-245:]:
        state["price_series"].append(float(c["c"]))

    log.info(
        "gate3_seeded",
        coin=coin,
        candles_5m=len(state["high_series_5m"]),
        price_points=len(state["price_series"]),
    )
