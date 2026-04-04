"""
Historical data loader for the backtester.

Uses Binance Futures public API for candles and funding history.
Hyperliquid's candleSnapshot only retains ~7 days; Binance has years of history.

Coin name mapping: Hyperliquid "ETH" → Binance "ETHUSDT".

Funding note: Binance does not expose oracle premium. The raw 8h fundingRate
is used as a premium proxy (gate1 premium check: 8h rate > GATE1_PREMIUM_FLOOR).
This is reasonable since elevated funding correlates with positive oracle premium.
"""
from __future__ import annotations

import asyncio

import aiohttp
import pandas as pd
import structlog

log = structlog.get_logger()

_BINANCE_BASE = "https://fapi.binance.com"
_CANDLE_BATCH_SIZE = 1500   # Binance max per request
_FUNDING_BATCH_SIZE = 1000  # Binance max per request


def _symbol(coin: str) -> str:
    """Convert Hyperliquid coin name to Binance futures symbol."""
    return f"{coin}USDT"


async def _get(session: aiohttp.ClientSession, path: str, params: dict) -> list:
    url = _BINANCE_BASE + path
    async with session.get(url, params=params) as resp:
        resp.raise_for_status()
        return await resp.json()


async def load_candles(
    coin: str, interval: str, start_ms: int, end_ms: int
) -> pd.DataFrame:
    """
    Fetch klines from Binance Futures in batches of 1500.
    Returns DataFrame with columns: time, open, high, low, close, volume.

    Interval format matches Binance: '1m', '5m', '1h', etc.
    """
    rows: list[dict] = []
    batch_start = start_ms
    batch_n = 0

    async with aiohttp.ClientSession() as session:
        while batch_start < end_ms:
            raw = await _get(
                session,
                "/fapi/v1/klines",
                {
                    "symbol": _symbol(coin),
                    "interval": interval,
                    "startTime": batch_start,
                    "endTime": end_ms,
                    "limit": _CANDLE_BATCH_SIZE,
                },
            )
            if not raw:
                break

            for c in raw:
                # Binance kline: [open_time, open, high, low, close, volume, ...]
                rows.append(
                    {
                        "time": int(c[0]),
                        "open": float(c[1]),
                        "high": float(c[2]),
                        "low": float(c[3]),
                        "close": float(c[4]),
                        "volume": float(c[5]),
                    }
                )

            batch_n += 1
            log.info(
                "candle_batch_loaded",
                coin=coin,
                interval=interval,
                batch=batch_n,
                total=len(rows),
            )

            if len(raw) < _CANDLE_BATCH_SIZE:
                break

            batch_start = rows[-1]["time"] + 1
            await asyncio.sleep(0.1)

    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(rows).drop_duplicates("time").sort_values("time").reset_index(drop=True)
    return df


async def load_funding_history(
    coin: str, start_ms: int, end_ms: int
) -> pd.DataFrame:
    """
    Fetch funding rate history from Binance Futures in batches of 1000.
    Returns DataFrame with columns: time, funding_rate (per-hour), premium.

    funding_rate = raw 8h rate / 8  (matches how Hyperliquid rates are stored)
    premium      = raw 8h rate      (proxy for oracle premium — elevated funding
                                     correlates with positive oracle premium)
    """
    rows: list[dict] = []
    batch_start = start_ms

    async with aiohttp.ClientSession() as session:
        while batch_start < end_ms:
            raw = await _get(
                session,
                "/fapi/v1/fundingRate",
                {
                    "symbol": _symbol(coin),
                    "startTime": batch_start,
                    "endTime": end_ms,
                    "limit": _FUNDING_BATCH_SIZE,
                },
            )
            if not raw:
                break

            for entry in raw:
                raw_rate = float(entry["fundingRate"])
                rows.append(
                    {
                        "time": int(entry["fundingTime"]),
                        "funding_rate": raw_rate / 8,   # per-hour
                        "premium": raw_rate,            # 8h rate as premium proxy
                    }
                )

            if len(raw) < _FUNDING_BATCH_SIZE:
                break

            batch_start = rows[-1]["time"] + 1
            await asyncio.sleep(0.1)

    if not rows:
        return pd.DataFrame(columns=["time", "funding_rate", "premium"])

    df = pd.DataFrame(rows).drop_duplicates("time").sort_values("time").reset_index(drop=True)
    return df
