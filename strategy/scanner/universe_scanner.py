"""Stage 1 scanner: Gates 1+2 over full universe. See PRD Section 4.1."""
from __future__ import annotations

import time
from typing import Any

from market_data.universe_snapshotter import ingest_asset_ctx, rest_post
from shared.constants import (
    MIN_UNIVERSE_DAILY_VOL_USD,
    MIN_UNIVERSE_MIN_LEVERAGE,
    MIN_UNIVERSE_OI_USD,
)
from shared.logging_config import log
from strategy.scanner.gate1 import gate1_passes
from strategy.scanner.gate2 import gate2_passes


def _passes_universe_filter(
    ctx: dict[str, Any],
    meta_coin: dict[str, Any],
) -> bool:
    """
    PRD Section 10 static eligibility checks.

    Returns True only when all three pass:
      - 24h notional volume  > MIN_UNIVERSE_DAILY_VOL_USD  ($5M)
      - OI in USD            > MIN_UNIVERSE_OI_USD          ($2M)
      - maxLeverage          >= MIN_UNIVERSE_MIN_LEVERAGE   (5×)

    All metaAndAssetCtxs numeric fields are strings — always wrap in float()/int().
    OI in base units (e.g. ETH) is converted to USD by multiplying by markPx.
    Missing or unparseable fields are treated as 0 (coin fails the filter).
    """
    try:
        day_vol = float(ctx.get("dayNtlVlm") or 0)
    except (TypeError, ValueError):
        day_vol = 0.0

    try:
        oi_usd = float(ctx.get("openInterest") or 0) * float(ctx.get("markPx") or 0)
    except (TypeError, ValueError):
        oi_usd = 0.0

    try:
        max_lev = int(meta_coin.get("maxLeverage") or 0)
    except (TypeError, ValueError):
        max_lev = 0

    return (
        day_vol >= MIN_UNIVERSE_DAILY_VOL_USD
        and oi_usd >= MIN_UNIVERSE_OI_USD
        and max_lev >= MIN_UNIVERSE_MIN_LEVERAGE
    )


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
    filtered_count = 0

    for i, ctx in enumerate(asset_ctxs):
        coin = meta["universe"][i]["name"]
        if coin not in universe_coins:
            continue
        state = all_states[coin]
        if state["has_data_gap"]:
            continue

        # Universe eligibility — skip before any state mutation
        if not _passes_universe_filter(ctx, meta["universe"][i]):
            filtered_count += 1
            continue

        try:
            rest_premium = float(ctx["premium"]) if ctx.get("premium") is not None else None
        except (TypeError, ValueError):
            rest_premium = None
        ingest_asset_ctx(ctx, state, now, rest_premium=rest_premium)

        if not gate1_passes(state["funding_series"], state["premium_series"], coin=coin):
            continue
        if not gate2_passes(state["oi_series"], state["price_series"], coin=coin):
            continue

        gate12_candidates.append(coin)

    log.info(
        "universe_scan_complete",
        candidates=len(gate12_candidates),
        filtered_by_universe=filtered_count,
    )
    return gate12_candidates
