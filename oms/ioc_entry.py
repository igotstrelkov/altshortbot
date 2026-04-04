"""
Two-step IOC entry execution engine.
See PRD Sections 8, 8.1, 8.3, 8.4.
"""
from __future__ import annotations

from typing import Any

import structlog

from oms.order_parser import parse_order_status
from oms.price_formatter import format_price
from shared.constants import (
    ABORT_SLIPPAGE,
    IOC_AGGRESSIVE_SLIPPAGE_PCT,
    LIMIT_ORDER_OFFSET,
    MAX_SLIPPAGE,
    MIN_ORDER_NOTIONAL_USD,
)
from shared.types import ParsedOrderStatus
from strategy.trigger.delta_aggregator import get_delta_z_score
from strategy.trigger.trigger_engine import trigger_is_valid

log = structlog.get_logger()


async def place_ioc_aggressive(
    coin: str,
    side: str,
    size_coins: float,
    reference_price: float,
    sz_decimals: int,
    exchange: Any,
    slippage_pct: float | None = None,
) -> dict[str, Any]:
    """
    Submit an IOC limit priced aggressively through the book.
    sells: price = reference_price * (1 - slippage_pct)
    buys:  price = reference_price * (1 + slippage_pct)
    """
    if slippage_pct is None:
        slippage_pct = IOC_AGGRESSIVE_SLIPPAGE_PCT

    if side == "sell":
        raw_px = reference_price * (1 - slippage_pct)
    else:
        raw_px = reference_price * (1 + slippage_pct)

    limit_px_str = format_price(raw_px, sz_decimals)
    return await exchange.place_limit_order(coin, side, size_coins, limit_px_str, tif="Ioc")


async def execute_entry(
    coin: str,
    size_usd: float,
    trigger_price: float,
    state: dict[str, Any],
    exchange: Any,
) -> ParsedOrderStatus | None:
    """
    Full two-step IOC entry (PRD Section 8.3).

    size_usd: position notional in USD — output of calculate_position_size().
    Returns ParsedOrderStatus on fill, None on every abort/skip path.
    """
    price_series = state["price_series"]
    mid: float = list(price_series)[-1]
    sz_decimals: int = state["sz_decimals"]
    delta_z = get_delta_z_score(state)

    if not trigger_is_valid(trigger_price, mid, delta_z):
        log.info("execute_entry_trigger_invalid", coin=coin)
        return None

    size_coins = round(size_usd / mid, sz_decimals)
    if size_coins * mid < MIN_ORDER_NOTIONAL_USD:
        log.info(
            "execute_entry_too_small",
            coin=coin,
            notional_usd=size_coins * mid,
        )
        return None

    # ── Step 1: primary passive IOC ───────────────────────────────────────────
    raw_limit_px = mid * (1 + LIMIT_ORDER_OFFSET)
    limit_px_str = format_price(raw_limit_px, sz_decimals)
    raw = await exchange.place_limit_order(coin, "sell", size_coins, limit_px_str, tif="Ioc")
    primary = parse_order_status(raw)

    if primary is None:
        log.warning(
            "execute_entry_parse_none",
            coin=coin,
            msg=(
                "parse_order_status returned None — exchange response malformed. "
                "Reconcile order/fill state before next entry."
            ),
        )
        return None

    if primary.status == "filled":
        assert primary.avg_px is not None
        fill_px = primary.avg_px
        limit_px = float(limit_px_str)
        slippage = (limit_px - fill_px) / limit_px
        if slippage > ABORT_SLIPPAGE:
            log.warning(
                "execute_entry_abort_slippage",
                coin=coin,
                slippage=f"{slippage:.3%}",
            )
            current_mid: float = list(state["price_series"])[-1]
            await place_ioc_aggressive(coin, "buy", size_coins, current_mid, sz_decimals, exchange)
            return None
        if slippage > MAX_SLIPPAGE:
            log.warning("execute_entry_high_slippage", coin=coin, slippage=f"{slippage:.3%}")
        return primary

    if primary.status == "error":
        reason = primary.reason or ""
        log.info("execute_entry_primary_rejected", coin=coin, reason=reason)

        if reason == "minTradeNtlRejected":
            log.error(
                "execute_entry_fatal_min_ntl",
                coin=coin,
                msg="Fatal: minTradeNtlRejected — check MIN_ORDER_NOTIONAL_USD vs exchange minimum",
            )
            return None
        if reason == "tickRejected":
            log.error(
                "execute_entry_fatal_tick",
                coin=coin,
                msg="Fatal: tickRejected — price formatting bug; inspect format_price()",
            )
            return None
        if reason == "oracleRejected":
            log.error(
                "execute_entry_fatal_oracle",
                coin=coin,
                msg="Fatal: oracleRejected — price too far from oracle; skipping signal",
            )
            return None
        if reason == "iocCancelRejected":
            log.info("execute_entry_ioc_cancel", coin=coin, msg="benign unfilled; proceeding to fallback")
        # all other errors: log and fall through

    if primary.status == "resting":
        log.warning("execute_entry_unexpected_resting", coin=coin)

    # ── Step 2: aggressive IOC fallback ──────────────────────────────────────
    log.info("execute_entry_primary_unfilled", coin=coin, msg="evaluating fallback")
    current_mid = list(state["price_series"])[-1]
    current_z = get_delta_z_score(state)

    if not trigger_is_valid(trigger_price, current_mid, current_z):
        log.info("execute_entry_trigger_expired", coin=coin)
        return None

    log.info("execute_entry_sending_fallback", coin=coin)
    raw_fb = await place_ioc_aggressive(coin, "sell", size_coins, current_mid, sz_decimals, exchange)
    fallback = parse_order_status(raw_fb)

    if fallback and fallback.status == "filled":
        return fallback

    if fallback and fallback.status == "error":
        log.warning("execute_entry_fallback_rejected", coin=coin, reason=fallback.reason)

    log.info("execute_entry_both_unfilled", coin=coin)
    return None
