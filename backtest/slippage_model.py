"""
Slippage model for the backtester.
Hurts shorts on both entry (lower fill) and exit (higher fill).
See PRD Section 11.2.
"""
from __future__ import annotations

from shared.constants import SLIPPAGE_MODEL_PCT


def apply_entry_slippage(mid_price: float) -> float:
    """Short entry: fill below mid — worse for the short seller."""
    return mid_price * (1 - SLIPPAGE_MODEL_PCT)


def apply_exit_slippage(mid_price: float) -> float:
    """Short exit (cover): fill above mid — worse for the buyer-to-cover."""
    return mid_price * (1 + SLIPPAGE_MODEL_PCT)
