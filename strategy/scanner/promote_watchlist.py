"""Stage 2 scanner: Gate 3 evaluation and watch list promotion. See PRD Section 4.1."""
from __future__ import annotations

from typing import Any

from shared.constants import GATE3_WARM_UP_S
from shared.logging_config import log
from strategy.scanner.gate3 import gate3_score
from strategy.scanner.seed_rest import seed_gate3_series_from_rest
from strategy.trigger.delta_aggregator import DeltaAggregator
from strategy.trigger.vwap_buffer import VwapBuffer


async def promote_to_watch_list(
    gate12_candidates: list[str],
    current_watch_list: list[str],
    all_states: dict[str, Any],
    now: float,
) -> list[str]:
    """
    Stage 2 of 2 — called immediately after run_universe_scanner().

    New candidates: seed REST data, set ws_subscribed_at, skip (warm-up starts).
    Warming up: skip until GATE3_WARM_UP_S elapsed.
    Ready: evaluate gate3_score >= 2 → promote to watch list.

    Coins not in gate12_candidates are implicitly dropped (Gate 1 or 2 failed).
    Caller is responsible for WS subscription/unsubscription.
    On Gate 3 failure, caller must call reset_warmup_state() and unsubscribe feeds.
    """
    new_watch_list: list[str] = []

    for coin in gate12_candidates:
        state = all_states[coin]

        if coin not in current_watch_list:
            if state.get("ws_subscribed_at", 0) == 0:
                await seed_gate3_series_from_rest(coin, state)
                state["ws_subscribed_at"] = now
                log.info("gate3_warmup_start", coin=coin, warm_up_s=GATE3_WARM_UP_S)
                continue

            elapsed = now - state["ws_subscribed_at"]
            if elapsed < GATE3_WARM_UP_S:
                log.debug("gate3_warming_up", coin=coin, elapsed=f"{elapsed:.0f}s")
                continue

        vwap_5m = state["vwap_buffer"].get_vwap()
        score = gate3_score(
            state["price_series"],
            state["high_series_5m"],
            state["close_series_5m"],
            vwap_5m,
            coin=coin,
        )
        if score >= 2:
            new_watch_list.append(coin)
        else:
            log.info("gate3_fail", coin=coin, score=score)

    return new_watch_list


def reset_warmup_state(coin: str, state: dict[str, Any]) -> None:
    """
    Clears all warm-up state for a coin that failed Gate 3 or dropped from Gates 1+2.
    Does NOT clear funding_series or oi_series — those remain valid.
    """
    state["ws_subscribed_at"] = 0.0
    state["is_on_watchlist"] = False
    state["delta_ready"] = False

    for series_key in ("price_series", "high_series_5m", "low_series_5m", "close_series_5m"):
        state[series_key].clear()

    state["vwap_buffer"] = VwapBuffer()
    state["delta_aggregator"] = DeltaAggregator()
    state["delta_history"].clear()
    state["trade_delta_60s"] = 0.0
    state["delta_mean_10m"] = 0.0
    state["delta_std_10m"] = 0.0
