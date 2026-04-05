"""
Market data ingestion: funding bootstrap and asset context parsing.
See PRD Sections 2.6 (Rules 1-3, Path A, Path B).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp

from shared.constants import FUNDING_BOOTSTRAP_STAGGER_S
from shared.logging_config import log
from strategy.liq_model import update_liq_model_from_candle

_HL_BASE = "https://api.hyperliquid.xyz"

# Throttle intervals (seconds)
_OI_THROTTLE_S = 60
_PREMIUM_THROTTLE_S = 300

# Module-level session — reused across all rest_post calls to avoid per-call TCP overhead.
# Lazily created on first use so it binds to the running event loop.
_session: aiohttp.ClientSession | None = None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def rest_post(path: str, payload: dict[str, Any]) -> Any:
    """POST to Hyperliquid API. Returns parsed JSON. Raises on non-200 or network error."""
    url = _HL_BASE + path
    async with _get_session().post(url, json=payload) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"REST {path} returned {resp.status}: {text}")
        return await resp.json()


async def refresh_funding_from_rest(coin: str, state: dict[str, Any]) -> None:
    """
    THE ONLY function that writes to state['funding_series'].
    Fetches funding history for the last 48 hours, stores per-hour rates (÷8).
    Never call from a WebSocket message handler.
    """
    payload = {
        "type": "fundingHistory",
        "coin": coin,
        "startTime": int((time.time() - 48 * 3600) * 1000),
    }
    response = await rest_post("/info", payload)
    state["funding_series"].clear()
    for entry in response[-48:]:
        state["funding_series"].append(float(entry["fundingRate"]) / 8)


async def bootstrap_universe_funding(
    universe_coins: list[str], all_states: dict[str, Any]
) -> None:
    """
    Hydrates funding_series for all scan-eligible coins.
    Call at startup and repeat every FUNDING_REFRESH_INTERVAL_S.
    """
    total = len(universe_coins)
    log.info("funding_bootstrap_start", total=total)
    for i, coin in enumerate(universe_coins):
        for attempt in range(3):
            try:
                await refresh_funding_from_rest(coin, all_states[coin])
                break
            except RuntimeError as exc:
                if "429" in str(exc):
                    wait = 5 * (attempt + 1)
                    log.warning("funding_bootstrap_rate_limited", coin=coin, retry_in_s=wait)
                    await asyncio.sleep(wait)
                else:
                    log.warning("funding_bootstrap_coin_failed", coin=coin, error=str(exc))
                    break
        log.info("funding_bootstrap_progress", progress=f"{i+1}/{total}", coin=coin)
        if i < total - 1:
            await asyncio.sleep(FUNDING_BOOTSTRAP_STAGGER_S)
    log.info("funding_bootstrap_complete", total=total)


def ingest_asset_ctx(
    ctx: dict[str, Any],
    state: dict[str, Any],
    now: float,
    rest_premium: float | None = None,
) -> None:
    """
    Receives the PerpsAssetCtx sub-object — NOT the raw WS message.

    WS callers:  ingest_asset_ctx(message["data"]["ctx"], state, now)
    REST callers: ingest_asset_ctx(ctx, state, now, rest_premium=float(ctx['premium']))

    NEVER writes to funding_series — that is Path A's exclusive responsibility.
    """
    try:
        mark_px = float(ctx["markPx"])
        oracle_px = float(ctx["oraclePx"])
    except (TypeError, ValueError):
        return  # coin has no price data yet (newly listed / inactive)

    if now - state["last_oi_append_ts"] >= _OI_THROTTLE_S:
        try:
            oi = float(ctx["openInterest"])
        except (TypeError, ValueError):
            oi = 0.0
        state["oi_series"].append(oi)
        state["price_series"].append(mark_px)
        state["last_oi_append_ts"] = now
        update_liq_model_from_candle(state, mark_px, now)

    if now - state["last_premium_append_ts"] >= _PREMIUM_THROTTLE_S:
        if rest_premium is not None:
            oracle_premium = rest_premium
        else:
            oracle_premium = (mark_px - oracle_px) / oracle_px if oracle_px > 0 else 0.0
        state["premium_series"].append(oracle_premium)
        state["last_premium_append_ts"] = now
