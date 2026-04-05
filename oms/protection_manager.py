"""
Protection orders: stop loss and take profit after a short entry fill.
See PRD Sections 2.9, 9.2.
"""
from __future__ import annotations

from typing import Any

import structlog

from oms.order_parser import parse_order_status
from oms.price_formatter import format_price
from shared.constants import (
    TP1_CLOSE_FRACTION,
    TP1_R_TARGET,
    TP2_R_TARGET,
)

log = structlog.get_logger()

# Worst-acceptable-price buffers for the limit order submitted when a trigger fires.
# SL fires in adverse conditions (price moving fast against the position) — wider buffer.
# TP fires in our favour — tight buffer is sufficient.
_SL_TRIGGER_SLIPPAGE = 0.05   # 5%: SL buy limit = stop_price * 1.05
_TP_TRIGGER_SLIPPAGE = 0.01   # 1%: TP buy limit = tp_price  * 1.01


async def attach_protection(
    coin: str,
    entry_price: float,
    size_coins: float,
    stop_distance_pct: float,
    sz_decimals: int,
    exchange: Any,
    state: dict[str, Any],
) -> None:
    """
    Place reduce-only stop loss and take profit trigger orders after a short entry fill.

    For a short, stop is above entry and TPs are below entry:
      stop_price = entry * (1 + stop_distance_pct)
      tp1_price  = entry * (1 - 1.5 * stop_distance_pct)   [1.5R, 50% of size]
      tp2_price  = entry * (1 - 2.5 * stop_distance_pct)   [2.5R, remaining 50%]

    All orders are reduce_only=True — cannot flip position.
    OIDs are stored in state['sl_oid'], state['tp1_oid'], state['tp2_oid'].

    Failures on individual orders are logged but do not raise — a partial protection
    set is better than no entry. The caller should verify OIDs are non-None.
    """
    stop_price = entry_price * (1 + stop_distance_pct)
    tp1_price  = entry_price * (1 - TP1_R_TARGET * stop_distance_pct)
    tp2_price  = entry_price * (1 - TP2_R_TARGET * stop_distance_pct)

    tp1_size = round(size_coins * TP1_CLOSE_FRACTION,       sz_decimals)
    tp2_size = round(size_coins * (1 - TP1_CLOSE_FRACTION), sz_decimals)

    # If rounding collapses tp2_size to zero, route everything through TP1
    if tp2_size <= 0:
        tp1_size = size_coins

    log.info(
        "protection_attaching",
        coin=coin,
        entry=round(entry_price, 6),
        stop=round(stop_price, 6),
        tp1=round(tp1_price, 6),
        tp2=round(tp2_price, 6),
        stop_pct=round(stop_distance_pct * 100, 3),
        size_coins=size_coins,
    )

    # ── Stop loss (full position size) ────────────────────────────────────────
    try:
        stop_trigger_str = format_price(stop_price, sz_decimals)
        stop_limit_str   = format_price(stop_price * (1 + _SL_TRIGGER_SLIPPAGE), sz_decimals)
        resp   = await exchange.place_trigger_order(
            coin=coin,
            side="buy",            # cover the short
            size_coins=size_coins,
            trigger_price_str=stop_trigger_str,
            limit_price_str=stop_limit_str,
            is_market=True,
            tpsl="sl",
            reduce_only=True,
        )
        parsed = parse_order_status(resp)
        state["sl_oid"] = parsed.oid if parsed else None
        log.info("stop_loss_placed", coin=coin, trigger=stop_trigger_str, oid=state["sl_oid"])
    except Exception as exc:
        log.error("stop_loss_failed", coin=coin, error=str(exc))

    # ── Take profit 1 (50% at 1.5R) ───────────────────────────────────────────
    if tp1_size > 0:
        try:
            tp1_trigger_str = format_price(tp1_price, sz_decimals)
            tp1_limit_str   = format_price(tp1_price * (1 + _TP_TRIGGER_SLIPPAGE), sz_decimals)
            resp   = await exchange.place_trigger_order(
                coin=coin,
                side="buy",
                size_coins=tp1_size,
                trigger_price_str=tp1_trigger_str,
                limit_price_str=tp1_limit_str,
                is_market=True,
                tpsl="tp",
                reduce_only=True,
            )
            parsed = parse_order_status(resp)
            state["tp1_oid"] = parsed.oid if parsed else None
            log.info(
                "tp1_placed",
                coin=coin,
                trigger=tp1_trigger_str,
                r=TP1_R_TARGET,
                size=tp1_size,
                oid=state["tp1_oid"],
            )
        except Exception as exc:
            log.error("tp1_failed", coin=coin, error=str(exc))

    # ── Take profit 2 (remaining 50% at 2.5R) ────────────────────────────────
    if tp2_size > 0:
        try:
            tp2_trigger_str = format_price(tp2_price, sz_decimals)
            tp2_limit_str   = format_price(tp2_price * (1 + _TP_TRIGGER_SLIPPAGE), sz_decimals)
            resp   = await exchange.place_trigger_order(
                coin=coin,
                side="buy",
                size_coins=tp2_size,
                trigger_price_str=tp2_trigger_str,
                limit_price_str=tp2_limit_str,
                is_market=True,
                tpsl="tp",
                reduce_only=True,
            )
            parsed = parse_order_status(resp)
            state["tp2_oid"] = parsed.oid if parsed else None
            log.info(
                "tp2_placed",
                coin=coin,
                trigger=tp2_trigger_str,
                r=TP2_R_TARGET,
                size=tp2_size,
                oid=state["tp2_oid"],
            )
        except Exception as exc:
            log.error("tp2_failed", coin=coin, error=str(exc))
