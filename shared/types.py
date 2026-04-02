"""
Shared type definitions. Import from here — never define inline.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal

Regime   = Literal["NORMAL", "REDUCED", "DISABLED"]
OrderSide = Literal["buy", "sell"]
PositionState = Literal["open", "closing"] | None

@dataclass
class ParsedOrderStatus:
    status:   Literal["filled", "resting", "error"]
    avg_px:   float  | None = None
    total_sz: float  | None = None
    oid:      int    | None = None
    reason:   str    | None = None

@dataclass
class TradeIntent:
    """Emitted by Strategy → consumed by OMS. Strategy never signs orders."""
    coin:              str
    side:              OrderSide
    size_usd:          float
    trigger_price:     float
    stop_distance_pct: float
    squeeze_score:     int
    regime:            Regime
    issued_at:         float     # unix timestamp
