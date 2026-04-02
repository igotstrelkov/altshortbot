"""
Core math helpers. Stdlib-only imports.
See PRD Sections 3.1–3.4.
"""
from __future__ import annotations

import math
from collections import deque
from decimal import ROUND_DOWN, Decimal


def ema(closes: list[float], period: int) -> list[float]:
    """
    Exponential Moving Average. Returns list same length as input.
    result[-1] = latest value, result[-6] = 5 periods ago.
    """
    if not closes:
        return []
    k = 2 / (period + 1)
    result = [closes[0]]
    for price in closes[1:]:
        result.append(price * k + result[-1] * (1 - k))
    return result


def compute_vwap(trades: list[tuple[float, float]]) -> float:
    """
    trades: list of (price, volume_usd) tuples.
    Returns 0.0 if empty or zero volume.
    """
    total_vol = sum(v for _, v in trades)
    if total_vol == 0:
        return 0.0
    return sum(p * v for p, v in trades) / total_vol


def compute_atr(
    high_series: deque[float],
    low_series: deque[float],
    close_series: deque[float],
    period: int = 14,
) -> float:
    """
    Average True Range on 5m candles.
    Requires at least period+1 candles. Returns 0.0 if insufficient data.
    """
    highs = list(high_series)
    lows = list(low_series)
    closes = list(close_series)

    if len(closes) < period + 1:
        return 0.0

    true_ranges = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges.append(tr)

    return sum(true_ranges[-period:]) / period


def format_price(price: float, sz_decimals: int) -> str:
    """
    Format a price as a canonical string for Hyperliquid order signing.

    Both constraints must hold; stricter wins:
      1. At most 5 significant figures
      2. At most (6 - sz_decimals) decimal places

    Trailing zeros stripped. Raises ValueError if price <= 0.

    Examples (sz_decimals=2):
      format_price(12345.678, 2) -> '12345'
      format_price(1.23456,   2) -> '1.2345'
      format_price(0.001234,  2) -> '0.0012'
    """
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")

    magnitude = int(math.floor(math.log10(abs(price))))
    sig_fig_decimals = max(0, 5 - magnitude - 1)
    max_decimal_places = max(0, 6 - sz_decimals)
    decimals = min(sig_fig_decimals, max_decimal_places)

    quantizer = Decimal(10) ** -decimals
    rounded = Decimal(str(price)).quantize(quantizer, rounding=ROUND_DOWN)
    canonical = f"{rounded:f}"
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")

    return canonical
