"""
AltShortBot main entry point.
Wires all services together and runs the scanner + WS event loop.
See PRD Sections 2.6, 2.8, 2.9.
"""
from __future__ import annotations

import asyncio
import signal
import time
from typing import Any

import structlog

import config.settings as settings
from market_data.universe_snapshotter import bootstrap_universe_funding
from market_data.ws_manager import run_ws_for_coin
from oms.execution_adapter import ExchangeAdapter
from oms.ioc_entry import execute_entry
from oms.protection_manager import attach_protection
from risk.correlation_filter import correlation_check_passes
from risk.daily_loss_tracker import DailyLossTracker
from risk.portfolio_controller import calculate_position_size, calculate_stop_distance
from risk.watchdog import start_watchdog
from shared.constants import (
    FUNDING_REFRESH_INTERVAL_S,
    HIGH_VOL_1H_RANGE_PCT,
    REGIME_CANDLE_HISTORY_HOURS,
    SCHEDULE_CANCEL_REFRESH_S,
)
from shared.helpers import compute_atr
from shared.state_factory import create_asset_state
from strategy.regime_filter import refresh_1h_closes, regime_filter
from strategy.scanner.promote_watchlist import promote_to_watch_list, reset_warmup_state
from strategy.scanner.universe_scanner import run_universe_scanner
from strategy.trigger.trigger_engine import evaluate_trigger

log = structlog.get_logger()

_SCANNER_INTERVAL_S = 30.0
_REGIME_REFRESH_INTERVAL_S = REGIME_CANDLE_HISTORY_HOURS * 60.0


# ── Shutdown ──────────────────────────────────────────────────────────────────

_shutdown_started = False


def setup_signal_handlers(loop: asyncio.AbstractEventLoop, exchange: ExchangeAdapter) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig,
            lambda: asyncio.create_task(shutdown(exchange)),
        )


async def shutdown(exchange: ExchangeAdapter) -> None:
    global _shutdown_started
    if _shutdown_started:
        return
    _shutdown_started = True
    log.info("shutdown_signal_received", msg="cancelling open orders")
    try:
        await exchange.cancel_all_orders()
    except Exception as exc:
        log.error("shutdown_cancel_failed", error=str(exc))
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    log.info("shutdown_complete")


# ── Funding refresh loop ──────────────────────────────────────────────────────

async def funding_refresh_loop(
    universe_coins: list[str],
    all_states: dict[str, Any],
) -> None:
    """Re-bootstrap funding series for all coins every FUNDING_REFRESH_INTERVAL_S."""
    await asyncio.sleep(FUNDING_REFRESH_INTERVAL_S)  # skip first cycle — bootstrap already ran
    while True:
        try:
            log.info("funding_refresh_start", total=len(universe_coins))
            await bootstrap_universe_funding(universe_coins, all_states)
            log.info("funding_refresh_complete", total=len(universe_coins))
        except Exception as exc:
            log.error("funding_refresh_failed", error=str(exc))
        await asyncio.sleep(FUNDING_REFRESH_INTERVAL_S)


# ── Regime refresh loop ───────────────────────────────────────────────────────

async def schedule_cancel_loop(exchange: ExchangeAdapter) -> None:
    """Refresh scheduleCancel every SCHEDULE_CANCEL_REFRESH_S seconds."""
    while True:
        await asyncio.sleep(SCHEDULE_CANCEL_REFRESH_S)
        try:
            await exchange.schedule_cancel()
        except Exception as exc:
            log.error("schedule_cancel_refresh_failed", error=str(exc))


async def _handle_position_closed(
    coin: str,
    state: dict[str, Any],
    exchange: ExchangeAdapter,
    daily_loss_tracker: DailyLossTracker,
) -> None:
    """
    Called once when a position disappears from exchange assetPositions.

    Retrieves realized P&L from fills since the entry fill timestamp.
    Falls back to a mark-price estimate when the fills query fails or returns
    nothing for this coin (e.g. position pre-dates our tracking window).

    'closedPnl' in Hyperliquid fills is zero for opening fills, non-zero for
    closing fills — summing all fills for the coin since entry_ts is safe.

    Clears all position tracking fields in state after recording.
    """
    entry_price = state.get("entry_price")
    size_coins  = state.get("position_size_coins")
    opened_at   = state.get("position_opened_at") or 0.0

    pnl_usd    = 0.0
    fills_found = False
    pnl_source  = "unknown"

    # Primary: fills since entry — accurate closedPnl per fill from exchange
    if opened_at > 0:
        try:
            fills = await exchange.get_recent_fills(since_ms=int(opened_at * 1000))
            for fill in fills:
                if fill.get("coin") == coin:
                    pnl_usd    += float(fill.get("closedPnl") or 0)
                    fills_found = True
            pnl_source = "fills" if fills_found else "no_fills"
        except Exception as exc:
            log.warning("position_close_fills_failed", coin=coin, error=str(exc))
            pnl_source = "fills_error"

    # Fallback: mark-price estimate when fills unavailable or empty
    if not fills_found and entry_price and size_coins:
        price_series = state.get("price_series")
        if price_series:
            exit_px = float(list(price_series)[-1])
            pnl_usd    = (entry_price - exit_px) * size_coins   # short: profit when price fell
            pnl_source = "price_estimate"

    result = daily_loss_tracker.record_close(pnl_usd)
    log.info(
        "position_closed",
        coin=coin,
        pnl_usd=round(pnl_usd, 4),
        pnl_source=pnl_source,
        daily_tracker_result=result,
        daily_pnl_usd=round(daily_loss_tracker.daily_pnl, 4),
    )

    # Clear position tracking fields
    state["position_state"]      = None
    state["entry_price"]         = None
    state["position_size_coins"] = None
    state["stop_distance_pct"]   = None
    state["position_opened_at"]  = 0.0


async def regime_refresh_loop(
    universe_coins: list[str],
    cached_closes: dict[str, list[float]],
) -> None:
    """Refresh 1h closes for regime filter every ~60 min."""
    await asyncio.sleep(_REGIME_REFRESH_INTERVAL_S)  # skip first cycle — bootstrap already ran
    while True:
        try:
            fresh = await refresh_1h_closes(universe_coins)
            cached_closes.update(fresh)
            log.info("regime_closes_refreshed", coins=len(fresh))
        except Exception as exc:
            log.error("regime_refresh_failed", error=str(exc))
        await asyncio.sleep(_REGIME_REFRESH_INTERVAL_S)


# ── Scanner cycle ─────────────────────────────────────────────────────────────

async def run_one_cycle(
    universe_coins: list[str],
    all_states: dict[str, dict[str, Any]],
    current_watchlist: list[str],
    cached_closes: dict[str, list[float]],
    open_positions: list[str],
    daily_loss_tracker: DailyLossTracker,
    equity_ref: list[float],
    exchange: ExchangeAdapter,
    ws_tasks: dict[str, asyncio.Task],
) -> list[str]:
    """
    Run one 30s scanner cycle. Returns updated watchlist.

    WS connections are opened only for Gate 1+2 candidates (warm-up + watchlist),
    not for the full universe. Hyperliquid allows 10 WS connections per IP.

    equity_ref is a single-element list so equity can be updated in-place each cycle.
    """
    now = time.time()

    # a0. Sync open_positions and equity from exchange each cycle
    try:
        user_state = await exchange.get_user_state()
        new_equity = float(user_state["marginSummary"]["accountValue"])
        if new_equity != equity_ref[0]:
            log.info("equity_updated", prev_usd=round(equity_ref[0], 4), new_usd=round(new_equity, 4))
            equity_ref[0] = new_equity
        live_set = {
            p["position"]["coin"]
            for p in user_state.get("assetPositions", [])
            if float(p["position"]["szi"]) != 0
        }
        # Detect each position that closed since the last cycle
        for coin in list(open_positions):
            if coin not in live_set:
                await _handle_position_closed(
                    coin, all_states[coin], exchange, daily_loss_tracker
                )
        if live_set != set(open_positions):
            log.info("open_positions_synced", was=list(open_positions), now=list(live_set))
            open_positions.clear()
            open_positions.extend(live_set)
    except Exception as exc:
        log.warning("account_state_sync_failed", error=str(exc))

    # a. Kill switch
    if not daily_loss_tracker.is_trading_allowed():
        log.info("scanner_trading_disabled")
        return current_watchlist

    # b-c. Gate 1+2 scan → watchlist promotion
    gate12 = await run_universe_scanner(universe_coins, all_states)
    new_watchlist = await promote_to_watch_list(gate12, current_watchlist, all_states, now)

    # d. Start WS tasks for new Gate 1+2 candidates; cancel tasks for coins that dropped out
    gate12_set = set(gate12)
    for coin in gate12_set:
        if coin not in ws_tasks or ws_tasks[coin].done():
            ws_tasks[coin] = asyncio.create_task(
                run_ws_for_coin(coin, all_states, exchange)
            )
            log.info("ws_task_started", coin=coin)
    for coin in list(ws_tasks):
        if coin not in gate12_set:
            ws_tasks[coin].cancel()
            del ws_tasks[coin]
            reset_warmup_state(coin, all_states[coin])
            log.info("ws_task_cancelled", coin=coin)

    # e. Promote new coins to active watch list
    for coin in new_watchlist:
        if coin not in current_watchlist:
            state = all_states[coin]
            state["ws_command_queue"].put_nowait(("subscribe", "l2Book"))
            state["is_on_watchlist"] = True
            log.info("coin_promoted", coin=coin)

    # f. Demote coins removed from watch list
    for coin in current_watchlist:
        if coin not in new_watchlist:
            state = all_states[coin]
            reset_warmup_state(coin, state)
            state["is_on_watchlist"] = False
            log.info("coin_demoted", coin=coin)

    # g. Regime filter
    regime = regime_filter(
        cached_closes.get("BTC", []),
        new_watchlist,
        cached_closes,
    )

    # h. Skip trigger evaluation if regime is DISABLED
    if regime == "DISABLED":
        log.info("scanner_regime_disabled")
        return new_watchlist

    # i-j. Trigger evaluation for each active watch-list coin
    for coin in new_watchlist:
        state = all_states[coin]

        if state["has_data_gap"]:
            log.debug("data_gap_blocking_entry", coin=coin)
            continue
        price_series = state["price_series"]
        if not price_series:
            continue

        current_mid = float(list(price_series)[-1])
        if current_mid == 0:
            continue

        trigger_price = current_mid  # snapshot at detection time
        if not evaluate_trigger(state, trigger_price, current_mid, coin=coin):
            continue

        # Compute stop distance
        atr_14 = compute_atr(
            state["high_series_5m"],
            state["low_series_5m"],
            state["close_series_5m"],
        )
        if atr_14 == 0:
            log.debug("atr_unavailable_skip", coin=coin)
            continue
        prices = list(price_series)
        swing_high = max(prices[-15:]) if len(prices) >= 15 else current_mid
        prices_60 = prices[-60:] if len(prices) >= 60 else prices
        high_vol = (
            (max(prices_60) - min(prices_60)) / current_mid > HIGH_VOL_1H_RANGE_PCT
            if prices_60 else False
        )
        stop_distance = calculate_stop_distance(current_mid, atr_14, swing_high, high_vol)

        size_usd = calculate_position_size(
            equity_ref[0], regime, state["squeeze_score"], stop_distance
        )
        if size_usd == 0:
            continue

        if not correlation_check_passes(coin, open_positions):
            continue

        if len(open_positions) >= settings.MAX_CONCURRENT_POSITIONS:
            log.info("scanner_max_positions_reached",
                     open_positions=len(open_positions),
                     max_allowed=settings.MAX_CONCURRENT_POSITIONS,
                     rejected_coin=coin)
            break

        log.info(
            "trigger_fired",
            coin=coin,
            mid=current_mid,
            regime=regime,
            squeeze_score=state["squeeze_score"],
            stop_distance_pct=round(stop_distance * 100, 3),
            atr_14=round(atr_14, 6),
            high_vol=high_vol,
            size_usd=round(size_usd, 4),
            dry_run=settings.DRY_RUN,
        )

        if not settings.DRY_RUN:
            result = await execute_entry(coin, size_usd, trigger_price, state, exchange)
            if result and result.status == "filled":
                assert result.avg_px is not None
                assert result.total_sz is not None
                state["position_state"] = "open"
                state["entry_price"] = result.avg_px
                state["position_size_coins"] = result.total_sz
                state["stop_distance_pct"] = stop_distance
                state["position_opened_at"] = time.time()
                open_positions.append(coin)
                log.info(
                    "entry_filled",
                    coin=coin,
                    avg_px=result.avg_px,
                    size_coins=result.total_sz,
                    stop_pct=round(stop_distance * 100, 3),
                )
                await attach_protection(
                    coin=coin,
                    entry_price=result.avg_px,
                    size_coins=result.total_sz,
                    stop_distance_pct=stop_distance,
                    sz_decimals=state["sz_decimals"],
                    exchange=exchange,
                    state=state,
                )

    return new_watchlist


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    # 1. Config already loaded via settings module import

    # 2. Exchange adapter
    exchange = ExchangeAdapter(
        wallet_address=settings.HL_API_WALLET_ADDRESS,
        private_key=settings.HL_PRIVATE_KEY,
        testnet=settings.HL_TESTNET,
    )

    # 3. Build coin metadata (asset index + sz_decimals)
    await exchange.build_coin_meta()
    universe_coins = list(exchange.coin_meta.keys())
    log.info("universe_loaded", num_coins=len(universe_coins))

    # 4. Initialise per-coin state
    all_states: dict[str, dict[str, Any]] = {
        coin: create_asset_state() for coin in universe_coins
    }

    # 5. Wire asyncio queues and sz_decimals into state
    for coin in universe_coins:
        all_states[coin]["ws_command_queue"] = asyncio.Queue()
        all_states[coin]["sz_decimals"] = exchange.get_sz_decimals(coin)

    # 6. Equity — stored as single-element list so run_one_cycle can update in-place
    user_state = await exchange.get_user_state()
    equity_ref: list[float] = [float(user_state["marginSummary"]["accountValue"])]
    log.info("equity_loaded", equity_usd=equity_ref[0])
    log.info(
        "config_loaded",
        dry_run=settings.DRY_RUN,
        testnet=settings.HL_TESTNET,
        max_concurrent_positions=settings.MAX_CONCURRENT_POSITIONS,
        risk_per_trade_pct=settings.RISK_PER_TRADE_PCT,
        daily_loss_kill_pct=settings.DAILY_LOSS_KILL_PCT,
        daily_loss_disable_pct=settings.DAILY_LOSS_DISABLE_PCT,
    )

    # 7. Daily loss tracker
    daily_loss_tracker = DailyLossTracker(equity_ref[0])

    # 8. Heartbeat monitor + watchdog OS thread
    start_watchdog(exchange.heartbeat_monitor, exchange)

    # 9. Signal handlers
    loop = asyncio.get_running_loop()
    setup_signal_handlers(loop, exchange)

    # 10. Funding bootstrap
    log.info("bootstrapping_funding")
    exchange.heartbeat_monitor.beat()
    await bootstrap_universe_funding(universe_coins, all_states)
    exchange.heartbeat_monitor.beat()

    # 11. WS tasks — started dynamically per Gate 1+2 candidate (not for full universe)
    # Hyperliquid limit: 10 WS connections per IP. Universe tier uses REST only.
    ws_tasks: dict[str, asyncio.Task] = {}

    # 12. Regime refresh cache + background task
    cached_closes: dict[str, list[float]] = {}
    await asyncio.sleep(10)  # brief pause after funding bootstrap to avoid 429
    log.info("bootstrapping_regime_closes")
    exchange.heartbeat_monitor.beat()
    cached_closes.update(await refresh_1h_closes(universe_coins))
    exchange.heartbeat_monitor.beat()
    log.info("regime_closes_bootstrapped", coins=len(cached_closes))
    asyncio.create_task(regime_refresh_loop(universe_coins, cached_closes))
    asyncio.create_task(funding_refresh_loop(universe_coins, all_states))

    # 13. scheduleCancel — initial set + background refresh every 30 min
    try:
        await exchange.schedule_cancel()
    except Exception as exc:
        log.error("schedule_cancel_initial_failed", error=str(exc))
    asyncio.create_task(schedule_cancel_loop(exchange))

    # 14. Scanner loop
    current_watchlist: list[str] = []
    open_positions: list[str] = []

    while True:
        loop_start = time.time()
        exchange.heartbeat_monitor.beat()
        try:
            current_watchlist = await run_one_cycle(
                universe_coins=universe_coins,
                all_states=all_states,
                current_watchlist=current_watchlist,
                cached_closes=cached_closes,
                open_positions=open_positions,
                daily_loss_tracker=daily_loss_tracker,
                equity_ref=equity_ref,
                exchange=exchange,
                ws_tasks=ws_tasks,
            )
        except Exception as exc:
            log.error("scanner_cycle_failed", error=str(exc))
        elapsed = time.time() - loop_start
        await asyncio.sleep(max(0.0, _SCANNER_INTERVAL_S - elapsed))


if __name__ == "__main__":
    asyncio.run(main())
