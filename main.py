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
from risk.correlation_filter import correlation_check_passes
from risk.daily_loss_tracker import DailyLossTracker
from risk.portfolio_controller import calculate_position_size, calculate_stop_distance
from risk.watchdog import start_watchdog
from shared.constants import (
    HIGH_VOL_1H_RANGE_PCT,
    REGIME_CANDLE_HISTORY_HOURS,
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

def setup_signal_handlers(loop: asyncio.AbstractEventLoop, exchange: ExchangeAdapter) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig,
            lambda: asyncio.create_task(shutdown(exchange)),
        )


async def shutdown(exchange: ExchangeAdapter) -> None:
    log.info("shutdown_signal_received", msg="cancelling open orders")
    try:
        await exchange.cancel_all_orders()
    except Exception as exc:
        log.error("shutdown_cancel_failed", error=str(exc))
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    log.info("shutdown_complete")


# ── Regime refresh loop ───────────────────────────────────────────────────────

async def regime_refresh_loop(
    universe_coins: list[str],
    cached_closes: dict[str, list[float]],
) -> None:
    """Refresh 1h closes for regime filter every ~60 min."""
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
    equity: float,
    exchange: ExchangeAdapter,
) -> list[str]:
    """
    Run one 30s scanner cycle. Returns updated watchlist.
    """
    now = time.time()

    # a. Kill switch
    if not daily_loss_tracker.is_trading_allowed():
        log.info("scanner_trading_disabled")
        return current_watchlist

    # b-c. Gate 1+2 scan → watchlist promotion
    gate12 = await run_universe_scanner(universe_coins, all_states)
    new_watchlist = await promote_to_watch_list(gate12, current_watchlist, all_states, now)

    # d. Promote new coins to active watch list
    for coin in new_watchlist:
        if coin not in current_watchlist:
            state = all_states[coin]
            state["ws_command_queue"].put_nowait(("subscribe", "l2Book"))
            state["is_on_watchlist"] = True
            log.info("coin_promoted", coin=coin)

    # e. Demote coins removed from watch list
    for coin in current_watchlist:
        if coin not in new_watchlist:
            state = all_states[coin]
            reset_warmup_state(coin, state)
            for feed in ("trades", "activeAssetCtx", "candle", "l2Book"):
                state["ws_command_queue"].put_nowait(("unsubscribe", feed))
            state["is_on_watchlist"] = False
            log.info("coin_demoted", coin=coin)

    # f. Unsubscribe warm-up coins that dropped out of gate12
    prev_gate12_candidates = {
        c for c in universe_coins if all_states[c].get("is_on_watchlist") is False
        and all_states[c].get("ws_subscribed_at", 0) > 0
        and c not in new_watchlist
    }
    for coin in prev_gate12_candidates:
        if coin not in gate12 and coin not in new_watchlist:
            state = all_states[coin]
            reset_warmup_state(coin, state)
            for feed in ("trades", "activeAssetCtx", "candle"):
                state["ws_command_queue"].put_nowait(("unsubscribe", feed))

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
            continue
        price_series = state["price_series"]
        if not price_series:
            continue

        current_mid = float(list(price_series)[-1])
        if current_mid == 0:
            continue

        trigger_price = current_mid  # snapshot at detection time
        if not evaluate_trigger(state, trigger_price, current_mid):
            continue

        # Compute stop distance
        atr_14 = compute_atr(
            state["high_series_5m"],
            state["low_series_5m"],
            state["close_series_5m"],
        )
        prices = list(price_series)
        swing_high = max(prices[-15:]) if len(prices) >= 15 else current_mid
        prices_60 = prices[-60:] if len(prices) >= 60 else prices
        high_vol = (
            (max(prices_60) - min(prices_60)) / current_mid > HIGH_VOL_1H_RANGE_PCT
            if prices_60 else False
        )
        stop_distance = calculate_stop_distance(current_mid, atr_14, swing_high, high_vol)

        size_usd = calculate_position_size(
            equity, regime, state["squeeze_score"], stop_distance
        )
        if size_usd == 0:
            continue

        if not correlation_check_passes(coin, open_positions):
            continue

        if len(open_positions) >= settings.MAX_CONCURRENT_POSITIONS:
            log.info("scanner_max_positions_reached")
            break

        log.info(
            "trigger_fired",
            coin=coin,
            mid=current_mid,
            regime=regime,
            size_usd=size_usd,
            dry_run=settings.DRY_RUN,
        )

        if not settings.DRY_RUN:
            result = await execute_entry(coin, size_usd, trigger_price, state)
            if result and result.status == "filled":
                state["position_state"] = "open"
                open_positions.append(coin)
                log.info("entry_filled", coin=coin, avg_px=result.avg_px)

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

    # 6. Equity (cached — refreshed after each closed position in full impl)
    user_state = await exchange.get_user_state()
    equity = float(user_state["marginSummary"]["accountValue"])
    log.info("equity_loaded", equity_usd=equity)

    # 7. Daily loss tracker
    daily_loss_tracker = DailyLossTracker(equity)

    # 8. Heartbeat monitor + watchdog OS thread
    start_watchdog(exchange.heartbeat_monitor, exchange)

    # 9. Signal handlers
    loop = asyncio.get_running_loop()
    setup_signal_handlers(loop, exchange)

    # 10. Funding bootstrap
    log.info("bootstrapping_funding")
    await bootstrap_universe_funding(universe_coins, all_states)

    # 11. WS tasks (one per coin)
    ws_tasks = [
        asyncio.create_task(run_ws_for_coin(coin, all_states, exchange))
        for coin in universe_coins
    ]
    log.info("ws_tasks_started", count=len(ws_tasks))

    # 12. Regime refresh cache + background task
    cached_closes: dict[str, list[float]] = {}
    asyncio.create_task(regime_refresh_loop(universe_coins, cached_closes))

    # 13. Scanner loop
    current_watchlist: list[str] = []
    open_positions: list[str] = []

    while True:
        loop_start = time.time()
        try:
            current_watchlist = await run_one_cycle(
                universe_coins=universe_coins,
                all_states=all_states,
                current_watchlist=current_watchlist,
                cached_closes=cached_closes,
                open_positions=open_positions,
                daily_loss_tracker=daily_loss_tracker,
                equity=equity,
                exchange=exchange,
            )
        except Exception as exc:
            log.error("scanner_cycle_failed", error=str(exc))
        elapsed = time.time() - loop_start
        await asyncio.sleep(max(0.0, _SCANNER_INTERVAL_S - elapsed))


if __name__ == "__main__":
    asyncio.run(main())
