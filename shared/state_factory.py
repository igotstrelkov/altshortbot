"""
Single source of truth for per-asset state initialisation.
Always call create_asset_state() — never construct state dicts manually.
See PRD Section 2.7.
"""
from collections import deque
from typing import Any

from strategy.liq_model import LiquidationModel
from strategy.trigger.delta_aggregator import DeltaAggregator
from strategy.trigger.vwap_buffer import VwapBuffer


def create_asset_state() -> dict[str, Any]:
    return {
        # ── Time series (all floats, never raw API strings) ──────────
        "funding_series":         deque(maxlen=48),    # per-hour rate (8h ÷ 8), 48h rolling
        "oi_series":              deque(maxlen=245),    # 1-min OI, 4h + 5-sample smoothing buffer
        "price_series":           deque(maxlen=245),    # 1-min sampled mark price (markPx)
        "premium_series":         deque(maxlen=12),     # 5-min oracle premium (markPx-oraclePx)/oraclePx

        # ── 5m candle series (from candle WS subscription) ───────────
        "high_series_5m":         deque(maxlen=24),     # 2h of 5m highs
        "low_series_5m":          deque(maxlen=24),     # 2h of 5m lows — needed for ATR
        "close_series_5m":        deque(maxlen=24),     # 2h of 5m closes
        "last_candle_ts_5m":      0,                    # open-time ms of last appended candle

        # ── Trigger state ─────────────────────────────────────────────
        "delta_history":          deque(maxlen=10),     # last 10 × 60s delta values
        "trade_delta_60s":        0.0,
        "delta_mean_10m":         0.0,
        "delta_std_10m":          0.0,

        # ── Order book ────────────────────────────────────────────────
        "bid_depth_now":          0.0,
        "bid_depth_t_minus_30s":  0.0,

        # ── Helper objects ────────────────────────────────────────────
        "liq_model":              LiquidationModel(),
        "delta_aggregator":       DeltaAggregator(),
        "vwap_buffer":            VwapBuffer(),

        # ── Computed ──────────────────────────────────────────────────
        "squeeze_score":          0,

        # ── Live mid price ────────────────────────────────────────────
        # Updated by allMids WS (~2s cadence). 0.0 until first message.
        # Use for order pricing; fall back to price_series[-1] if still 0.
        "mid_price":              0.0,

        # ── Throttle timestamps ───────────────────────────────────────
        "last_oi_append_ts":      0.0,
        "last_premium_append_ts": 0.0,

        # ── Exchange metadata ─────────────────────────────────────────
        "sz_decimals":            0,         # populated at startup from exchange.coin_meta
        "ws_command_queue":       None,      # set to asyncio.Queue() in main before WS tasks

        # ── Liveness and control ──────────────────────────────────────
        "last_ws_ts":             0.0,
        "last_reconcile_ts":      0.0,
        "has_data_gap":           False,
        "ws_subscribed_at":       0.0,    # unix ts when WS feeds were subscribed
        "trigger_valid_until":    0.0,
        "position_state":         None,   # None | 'open' | 'closing'
        "pending_action_count":   0,

        # ── Watch list membership ─────────────────────────────────────
        # Ownership rule: main loop sets True on promote, False on demotion.
        # Use the caller's external list as source of truth, not this flag.
        "is_on_watchlist":        False,
        "delta_ready":            False,

        # ── Open position tracking ────────────────────────────────────
        # Populated at fill time; cleared when position_state → None.
        "entry_price":            None,   # avg fill price of open short entry
        "position_size_coins":    None,   # filled size in coins (from fill totalSz)
        "stop_distance_pct":      None,   # fractional stop distance used at entry
        "position_opened_at":     0.0,    # unix ts of entry fill — used to bound fills query
        "sl_oid":                 None,   # stop loss trigger order OID
        "tp1_oid":                None,   # take profit 1 trigger order OID
        "tp2_oid":                None,   # take profit 2 trigger order OID
    }
