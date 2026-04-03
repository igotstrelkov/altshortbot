"""
Price and size formatting for OMS.
Re-exports format_price from shared.helpers and adds validate_size.
See PRD Section 3.4.
"""
from __future__ import annotations

from shared.helpers import format_price as format_price  # noqa: F401 — re-export


def validate_size(size_coins: float, sz_decimals: int) -> float:
    """Round size to sz_decimals decimal places."""
    return round(size_coins, sz_decimals)
