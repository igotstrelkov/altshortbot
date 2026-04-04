"""Stage 1 scanner: Gates 1+2 over full universe. See PRD Section 4.1."""
from __future__ import annotations

import time
from typing import Any

from market_data.universe_snapshotter import ingest_asset_ctx, rest_post
from shared.logging_config import log
from strategy.scanner.gate1 import gate1_passes
from strategy.scanner.gate2 import gate2_passes


async def run_universe_scanner(
    universe_coins: list[str], all_states: dict[str, Any]
) -> list[str]:
    """
    Applies Gates 1 and 2 to the full universe using REST metaAndAssetCtxs.
    Returns the list of coins passing both gates (Gate12 candidates).
    Gate 3 is NOT applied here — it requires live WS data and warm-up.
    """
    response = await rest_post("/info", {"type": "metaAndAssetCtxs"})
    meta, asset_ctxs = response[0], response[1]
    now = time.time()
    gate12_candidates: list[str] = []

    for i, ctx in enumerate(asset_ctxs):
        coin = meta["universe"][i]["name"]
        if coin not in universe_coins:
            continue
        state = all_states[coin]
        if state["has_data_gap"]:
            continue

        try:
            rest_premium = float(ctx["premium"]) if ctx.get("premium") is not None else None
        except (TypeError, ValueError):
            rest_premium = None
        ingest_asset_ctx(ctx, state, now, rest_premium=rest_premium)

        if not gate1_passes(state["funding_series"], state["premium_series"]):
            continue
        if not gate2_passes(state["oi_series"], state["price_series"]):
            continue

        gate12_candidates.append(coin)

    log.info("universe_scan_complete", candidates=len(gate12_candidates))
    return gate12_candidates
