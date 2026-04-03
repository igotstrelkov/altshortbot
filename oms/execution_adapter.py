"""
Stub exchange adapter — wired to the real Hyperliquid client in Stage 10.
Import place_limit_order from here so it can be patched in tests.
"""
from __future__ import annotations

from typing import Any


async def place_limit_order(
    coin: str,
    side: str,
    size_coins: float,
    price_str: str,
    tif: str,
) -> dict[str, Any]:
    """Stub — overridden in Stage 10 with the real exchange client."""
    raise NotImplementedError("place_limit_order not wired — see Stage 10")
