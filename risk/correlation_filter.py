"""
Sector-based correlation filter to limit concentration risk.
See PRD Section 9.6.
"""
from __future__ import annotations

import structlog

from shared.constants import MAX_POSITIONS_PER_SECTOR

log = structlog.get_logger()

SECTOR_MAP: dict[str, str] = {
    "BTC": "L1",  "ETH": "L1",  "SOL": "L1",  "AVAX": "L1", "ADA": "L1",
    "SUI": "L1",  "APT": "L1",  "TON": "L1",  "NEAR": "L1", "TRX": "L1",
    "OP":  "L1",  "ARB": "L1",  "SEI": "L1",
    "LINK": "Oracle", "BAND": "Oracle",
    "UNI": "DeFi",  "AAVE": "DeFi",  "CRV": "DeFi",
    "GMX": "DeFi",  "JUP": "DeFi",   "PENDLE": "DeFi",
    "DOGE": "Meme", "SHIB": "Meme",  "PEPE": "Meme", "WIF": "Meme", "BONK": "Meme",
    "FET": "AI",  "RNDR": "AI", "TAO": "AI",
}


def correlation_check_passes(new_coin: str, open_positions: list[str]) -> bool:
    """
    Returns False if adding new_coin would put > MAX_POSITIONS_PER_SECTOR
    positions in one sector. Coins not in SECTOR_MAP default to 'Other'.
    open_positions: list of coin strings currently held short.
    """
    sector = SECTOR_MAP.get(new_coin, "Other")
    count = sum(1 for c in open_positions if SECTOR_MAP.get(c, "Other") == sector)
    if count >= MAX_POSITIONS_PER_SECTOR:
        log.info(
            "correlation_block",
            coin=new_coin,
            sector=sector,
            count=count,
        )
        return False
    return True
