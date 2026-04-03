# AltShortBot — LLM Implementation Plan
**Feed each stage in order. Complete and validate before proceeding to the next.**

Each stage contains:
- The exact prompt to send to the LLM
- Files to create or modify
- Validation criteria to confirm the stage is complete before moving on

Assumptions:
- The project scaffold in `altshortbot/` already exists (see project structure)
- `shared/constants.py`, `shared/types.py`, `shared/state_factory.py` are already populated
- The PRD (`AltShortBot_PRD_v3.md`) is available for reference
- Python 3.11+, `pip install -e ".[dev]"` has been run

---

## Stage 1 — Core Math Helpers

### Prompt
```
You are implementing the AltShortBot trading system. Your task is Stage 1: core math helpers.

Reference: PRD Sections 3.1, 3.2, 3.3, 3.4 (format_price).

Implement the following functions in `shared/helpers.py`:

1. `ema(closes: list, period: int) -> list`
   - Exponential Moving Average
   - Returns list of same length as input
   - result[-1] = latest, result[-6] = 5 periods ago

2. `compute_vwap(trades: list) -> float`
   - trades: list of (price: float, volume_usd: float) tuples
   - Returns 0.0 if empty

3. `compute_atr(high_series: deque, low_series: deque, close_series: deque, period: int = 14) -> float`
   - Average True Range on 5m candles
   - Requires at least period+1 candles
   - Returns 0.0 if insufficient data

4. `format_price(price: float, sz_decimals: int) -> str`
   - Returns a canonical STRING (not float) for Hyperliquid order signing
   - Rule 1: at most 5 significant figures
   - Rule 2: at most (6 - sz_decimals) decimal places
   - Stricter constraint wins
   - Uses Decimal with ROUND_DOWN
   - Strips trailing zeros via .normalize()
   - Guards against scientific notation output
   - Raises ValueError if price <= 0

All imports must come from stdlib only (math, decimal, collections).

After implementing, write unit tests in `tests/unit/test_helpers.py` covering:
- ema: basic values, single element, empty input
- compute_vwap: weighted correctly, empty input
- compute_atr: correct true range calculation, returns 0.0 when insufficient data
- format_price: the three examples from PRD (format_price(12345.678, 2) == '12345',
  format_price(1.23456, 2) == '1.2345', format_price(0.001234, 2) == '0.0012'),
  raises ValueError on non-positive price
```

### Files
- `shared/helpers.py` — implement all four functions
- `tests/unit/test_helpers.py` — unit tests

### Validation
```bash
pytest tests/unit/test_helpers.py -v
# All tests must pass. No imports from outside stdlib.
python -c "from shared.helpers import format_price; assert format_price(12345.678, 2) == '12345'; assert format_price(0.001234, 2) == '0.0012'; print('format_price OK')"
```

---

## Stage 2 — VwapBuffer and DeltaAggregator

### Prompt
```
You are implementing the AltShortBot trading system. Your task is Stage 2: VwapBuffer and DeltaAggregator.

Reference: PRD Sections 3.4, 3.5.

Implement in `strategy/trigger/vwap_buffer.py`:

class VwapBuffer:
  - WINDOW_S = 300 (import VWAP_BUFFER_WINDOW_S from shared.constants)
  - __init__(self): stores list of (timestamp, price, volume_usd) tuples
  - on_trade(self, price: float, size_base: float, now: float) -> None
    - appends (now, price, size_base * price) and trims entries older than WINDOW_S
  - get_vwap(self) -> float
    - delegates to compute_vwap() from shared.helpers

Implement in `strategy/trigger/delta_aggregator.py`:

class DeltaAggregator:
  - WINDOW_S = 60 (import DELTA_WINDOW_S from shared.constants)
  - Tracks sell_vol_usd and buy_vol_usd for current 60s window
  - Hyperliquid WS trade 'side' field: 'A' = taker SELL, 'B' = taker BUY
  - on_trade(self, side: str, size_base: float, price: float) -> None
  - flush_if_ready(self, state: dict, now: float) -> bool
    - if window elapsed: calls update_delta_state(state, net_delta), resets counters, returns True
    - otherwise returns False
  - update_delta_state(state, new_delta_60s) is a module-level function (NOT a method):
    - appends to state['delta_history'] (deque maxlen=10)
    - sets state['trade_delta_60s']
    - if len(delta_history) >= DELTA_COLD_START_PERIODS (10): sets delta_ready=True,
      computes mean and stdev (use statistics module), guards stdev > 0 to avoid /0
    - else: sets delta_ready=False, zeros mean and std

Also add module-level function get_delta_z_score(state: dict) -> float:
  - returns 0.0 if not state['delta_ready'] or delta_std_10m == 0
  - otherwise returns (trade_delta_60s - delta_mean_10m) / delta_std_10m

Write unit tests in `tests/unit/test_vwap_delta.py`:
- VwapBuffer: correct VWAP, window trimming, empty buffer returns 0.0
- DeltaAggregator: side classification correct (A=sell, B=buy), flush fires after 60s,
  net delta passed to update_delta_state correctly
- get_delta_z_score: returns 0.0 before cold-start complete, correct z-score after
```

### Files
- `strategy/trigger/vwap_buffer.py`
- `strategy/trigger/delta_aggregator.py`
- `tests/unit/test_vwap_delta.py`

### Validation
```bash
pytest tests/unit/test_vwap_delta.py -v
python -c "
from strategy.trigger.vwap_buffer import VwapBuffer
buf = VwapBuffer()
buf.on_trade(100.0, 1.0, 0.0)
buf.on_trade(200.0, 1.0, 10.0)
assert buf.get_vwap() == 150.0, f'expected 150.0 got {buf.get_vwap()}'
print('VwapBuffer OK')
"
```

---

## Stage 3 — Liquidation Model

### Prompt
```
You are implementing the AltShortBot trading system. Your task is Stage 3: LiquidationModel and squeeze risk scoring.

Reference: PRD Sections 5.1, 5.2, 5.3.

Implement in `strategy/liq_model.py`:

class LiquidationModel:
  - MAX_ENTRIES = 1440 (LIQ_MODEL_MAX_ENTRIES from constants)
  - __init__: long_entries and short_entries as deque(maxlen=MAX_ENTRIES)
    each entry is (liq_price: float, notional_usd: float, timestamp: float)
  - update(prev_oi, curr_oi, candle_open, candle_close, notional, timestamp):
    - returns immediately if curr_oi <= prev_oi
    - bullish candle (close > open) + rising OI → new longs → liq at close * 0.90
    - bearish candle + rising OI → new shorts → liq at close * 1.10
  - cluster_above(price, pct=0.03) -> float: short liq notional within pct above price
  - cluster_below(price, pct=0.03) -> float: long liq notional within pct below price
  - new_positions_1h(now) -> tuple[float, float]: (short_notional, long_notional) in last 3600s

Module-level functions:

def squeeze_risk_ratio(liq_above: float, liq_below: float) -> float

def calculate_squeeze_score(liq_model, current_price, funding_series, now=None) -> int:
  Returns 0–10. Scoring:
  - short_1h > long_1h: +3
  - liq_above > liq_below: +2
  - funding dropped >30% from elevated baseline (>20% APR in per-hour terms): +3
    (SQUEEZE_FUNDING_ELEVATED_APR / 8760 for per-hour floor)
  - squeeze_risk_ratio > SQUEEZE_RISK_RATIO_MAX (0.45): +2
  - capped at 10

def update_liq_model_from_candle(state: dict, mark_price: float, now: float) -> None:
  Called inside ingest_asset_ctx() after every 1-min OI append.
  Reads last two oi_series and price_series values.
  Only fires when OI increased.
  After updating model, recalculates and caches state['squeeze_score'].

Wire LiquidationModel into shared/state_factory.py:
  - Import LiquidationModel, VwapBuffer, DeltaAggregator
  - Replace the three None placeholders with actual instances

Write unit tests in `tests/unit/test_liq_model.py`:
- update() ignores flat/falling OI
- bullish candle creates long entry at 0.90× close
- bearish candle creates short entry at 1.10× close
- cluster_above/below returns correct notional sum
- new_positions_1h filters by cutoff correctly
- calculate_squeeze_score returns 0 when no entries, correct score on synthetic data
- update_liq_model_from_candle skips when OI unchanged
```

### Files
- `strategy/liq_model.py`
- `shared/state_factory.py` — wire in the three class instances

### Validation
```bash
pytest tests/unit/test_liq_model.py -v
python -c "
from shared.state_factory import create_asset_state
state = create_asset_state()
assert state['liq_model'] is not None, 'liq_model is None'
assert state['vwap_buffer'] is not None, 'vwap_buffer is None'
assert state['delta_aggregator'] is not None, 'delta_aggregator is None'
print('State factory wired OK')
"
```

---

## Stage 4 — Data Ingestion

### Prompt
```
You are implementing the AltShortBot trading system. Your task is Stage 4: data ingestion functions.

Reference: PRD Sections 2.6 (Rules 1–3, Path A, Path B).

Context: All API numeric fields should be wrapped in float() at ingestion even when
documented as numbers. The funding field is always the 8-hour basis rate — divide by 8
before storing. price_series semantics = 1-min sampled mark price (markPx).

Create `market_data/universe_snapshotter.py` with these functions:

1. async def rest_post(path: str, payload: dict) -> any
   - POST to https://api.hyperliquid.xyz + path
   - Uses aiohttp, returns parsed JSON
   - Raises on non-200 or network error

2. async def refresh_funding_from_rest(coin: str, state: dict) -> None
   - THE ONLY function that writes to state['funding_series']
   - Fetches fundingHistory for last 48 hours
   - payload: {"type": "fundingHistory", "coin": coin, "startTime": int((time.time()-48*3600)*1000)}
   - Appends float(entry['fundingRate']) / 8 for each entry (last 48)
   - Never call from a WS message handler

3. async def bootstrap_universe_funding(universe_coins: list, all_states: dict) -> None
   - Calls refresh_funding_from_rest for every coin
   - Sleeps FUNDING_BOOTSTRAP_STAGGER_S between requests
   - Log progress: "{i+1}/{total} funded"
   - Call at startup and repeat every FUNDING_REFRESH_INTERVAL_S

4. def ingest_asset_ctx(ctx: dict, state: dict, now: float, rest_premium=None) -> None
   - Called from WS activeAssetCtx handler: pass message["data"]["ctx"]
   - Called from REST metaAndAssetCtxs poll: pass ctx element + rest_premium=float(ctx['premium'])
   - 60s throttle on OI + price:
     - appends float(ctx['openInterest']) to oi_series
     - appends float(ctx['markPx']) to price_series  (markPx = sampled mark price)
     - calls update_liq_model_from_candle(state, mark_px, now)
   - 300s throttle on premium:
     - if rest_premium is not None: use it directly
     - else: derive (markPx - oraclePx) / oraclePx from ctx['markPx'] and ctx['oraclePx']
   - NEVER writes funding_series

Import all thresholds from shared.constants.
Use structlog for logging (import from shared.logging_config).

Write unit tests in `tests/unit/test_ingestion.py`:
- ingest_asset_ctx: respects 60s throttle (no append before 60s elapsed)
- ingest_asset_ctx: never writes funding_series
- ingest_asset_ctx: REST path uses rest_premium directly
- ingest_asset_ctx: WS path derives premium from markPx/oraclePx
- refresh_funding_from_rest: mocked response → correct ÷8 conversion
```

### Files
- `market_data/universe_snapshotter.py`
- `shared/logging_config.py` — configure structlog with JSON output, level from env

### Validation
```bash
pytest tests/unit/test_ingestion.py -v
python -c "
from shared.state_factory import create_asset_state
from market_data.universe_snapshotter import ingest_asset_ctx
import time
state = create_asset_state()
ctx = {'openInterest': '100.0', 'markPx': '1800.0', 'oraclePx': '1799.0',
       'funding': '0.0001', 'midPx': '1800.5', 'dayNtlVlm': '1000000',
       'prevDayPx': '1750.0'}
ingest_asset_ctx(ctx, state, time.time())
assert len(state['oi_series']) == 1
assert len(state['funding_series']) == 0, 'funding_series must not be written here'
print('ingest_asset_ctx OK')
"
```

---

## Stage 5 — Three-Gate Scanner

### Prompt
```
You are implementing the AltShortBot trading system. Your task is Stage 5: the three-gate scanner.

Reference: PRD Sections 4.1, 4.2, 4.3, 4.4.

Implement gate functions:

`strategy/scanner/gate1.py` — gate1_passes(funding_series, premium_series) -> bool
  PASS if ALL of:
  - len(last 8 hourly readings) == 8
  - latest reading * GATE1_ANNUALISE_MULTIPLIER > GATE1_FUNDING_APR_THRESHOLD
  - count of positive readings >= GATE1_MIN_POSITIVE_HOURS
  - latest premium > GATE1_PREMIUM_FLOOR

`strategy/scanner/gate2.py` — gate2_passes(oi_series, price_series) -> bool
  PASS if ALL of:
  - len(oi_series) >= 245, len(price_series) >= 240
  - 5-min smoothed OI now vs 5-min smoothed OI 240 samples ago: change > GATE2_OI_CHANGE_THRESHOLD
  - abs price change over same window < GATE2_PRICE_CHANGE_MAX

`strategy/scanner/gate3.py`:
  - gate3_score(price_series, high_series_5m, close_series_5m, vwap_5m) -> int (0-3)
    Condition 1 (+1): current price within GATE3_PRICE_FROM_HIGH_MAX of 4h max sampled mark price
                      (max of last 240 price_series entries — NOT candle highs)
    Condition 2 (+1): vwap_5m > 0 and current_price < vwap_5m
    Condition 3 (+1): failed_breakout_detected(high_series_5m, close_series_5m)
  - failed_breakout_detected(high_series_5m, close_series_5m, lookback=24) -> bool
    - Requires len(highs) == lookback
    - peak_idx = index of max high in window
    - if peak_idx >= lookback - 3: return False (too recent)
    - return (highs[peak_idx] - closes[-1]) / highs[peak_idx] > FAILED_BREAKOUT_RECOVERY_THRESHOLD

`strategy/scanner/seed_rest.py`:
  - async def seed_gate3_series_from_rest(coin, state) -> None
    Seeds price_series (245 × 1m candle closes as markPx proxy) and
    high/low/close_series_5m (24 × 5m candles) from candleSnapshot REST.
    payload: {"type": "candleSnapshot", "req": {"coin": coin, "interval": "Xm", "startTime": ..., "endTime": ...}}
    Close ('c') from 1m candles → price_series (close ≈ markPx at interval end, approximation is acceptable)
    h/l/c from 5m candles → high/low/close_series_5m

`strategy/scanner/universe_scanner.py`:
  - async def run_universe_scanner(universe_coins, all_states) -> list
    Stage 1: Gates 1+2 only, full universe via metaAndAssetCtxs REST
    For each coin: ingest_asset_ctx (with rest_premium), then gate1_passes + gate2_passes
    Returns gate12_candidates list

`strategy/scanner/promote_watchlist.py`:
  - async def promote_to_watch_list(gate12_candidates, current_watch_list, all_states, now) -> list
    Stage 2: Gate 3 with warm-up logic
    New candidates (ws_subscribed_at == 0): call seed_gate3_series_from_rest, set ws_subscribed_at=now, skip
    Warming up (elapsed < GATE3_WARM_UP_S): skip
    Ready: evaluate gate3_score >= 2 → add to new_watch_list
    On Gate 3 failure: log that caller must call reset_warmup_state + unsubscribe

  - def reset_warmup_state(coin, state) -> None
    Clears: ws_subscribed_at, is_on_watchlist, delta_ready
    Clears series: price_series, high/low/close_series_5m
    Resets VwapBuffer and DeltaAggregator to fresh instances
    Clears delta state fields

Write unit tests in `tests/unit/test_gates.py`:
- gate1: fails on empty series, fails if < 6 positive, passes on valid data
- gate2: fails on short series, fails if OI flat, fails if price moved too much, passes correctly
- gate3_score: each condition independently
- failed_breakout_detected: returns False if peak too recent, correct detection otherwise
- reset_warmup_state: all target fields cleared, oi_series and funding_series untouched
```

### Files
- `strategy/scanner/gate1.py`
- `strategy/scanner/gate2.py`
- `strategy/scanner/gate3.py`
- `strategy/scanner/seed_rest.py`
- `strategy/scanner/universe_scanner.py`
- `strategy/scanner/promote_watchlist.py`

### Validation
```bash
pytest tests/unit/test_gates.py -v
python -c "
from collections import deque
from strategy.scanner.gate1 import gate1_passes
# Create a valid 8-reading funding series: 6 positive, annualised > 50%
fs = deque([-0.00001]*2 + [0.00006]*6, maxlen=48)  # 6 pos, last = 0.00006 * 8760 = 0.5256 APR (negatives must be older/first)
ps = deque([0.0003], maxlen=12)
assert gate1_passes(fs, ps) == True
print('gate1 OK')
"
```

---

## Stage 6 — Regime Filter

### Prompt
```
You are implementing the AltShortBot trading system. Your task is Stage 6: regime filter.

Reference: PRD Section 6.

Implement in `strategy/regime_filter.py`:

async def refresh_1h_closes(universe_coins: list) -> dict:
  - Adds 'BTC' to the list if not present
  - For each coin: POST candleSnapshot with interval "1h",
    startTime = now_ms - REGIME_CANDLE_HISTORY_HOURS * 3600 * 1000
  - Returns {coin: [float closes]} — float(c['c']) for each candle
  - Uses rest_post from market_data.universe_snapshotter
  - Stagger requests by 0.1s to avoid burst

def regime_filter(btc_closes_1h: list, watch_list_coins: list, coin_closes_1h: dict) -> str:
  Returns 'NORMAL' | 'REDUCED' | 'DISABLED'

  1. If len(btc_closes_1h) < REGIME_MIN_BTC_HISTORY: return 'DISABLED'
  2. Compute ema(btc_closes_1h, 20) and ema(btc_closes_1h, 50) using shared.helpers.ema
  3. btc_slope = (ema20[-1] - ema20[-6]) / ema20[-6]   (5-period slope)
  4. If ema20[-1] > ema50[-1] and btc_slope > BTC_SLOPE_DISABLE_THRESHOLD: return 'DISABLED'
  5. If ema20[-1] > ema50[-1] and btc_slope > BTC_SLOPE_REDUCE_THRESHOLD: return 'REDUCED'
  6. Alt breadth check:
     coins_up = count of watch_list_coins where last 1h move > ALT_BREADTH_UP_PCT
     If watch_list_coins non-empty and coins_up/len > ALT_BREADTH_DISABLE_THRESHOLD: return 'DISABLED'
  7. return 'NORMAL'

All thresholds from shared.constants.

Write unit tests in `tests/unit/test_regime.py`:
- Returns DISABLED when insufficient BTC history
- Returns DISABLED on strong BTC uptrend (ema20 > ema50, slope > 1.5%)
- Returns REDUCED on mild uptrend (slope between 0.5% and 1.5%)
- Returns DISABLED when >60% of alts up >2%
- Returns NORMAL when BTC flat and breadth low
```

### Files
- `strategy/regime_filter.py`

### Validation
```bash
pytest tests/unit/test_regime.py -v
```

---

## Stage 7 — Trigger Engine

### Prompt
```
You are implementing the AltShortBot trading system. Your task is Stage 7: trigger engine and message handler.

Reference: PRD Sections 7.1, 7.2, 7.3.

Implement in `strategy/trigger/trigger_engine.py`:

1. def trigger_is_valid(trigger_price, current_mid, delta_z_score) -> bool
   - Returns False if abs(current_mid - trigger_price) / trigger_price > TRIGGER_STALE_DRIFT_MAX
   - Returns False if delta_z_score >= DELTA_ZSCORE_EXPIRY
   - Returns True otherwise

2. def evaluate_trigger(state: dict, trigger_price: float, current_mid: float) -> bool
   - Primary: get_delta_z_score(state) < DELTA_ZSCORE_TRIGGER
   - If primary fails: return False immediately
   - Confirmation (at least ONE must be True):
     a. bid_depth_thinning: (bid_depth_t_minus_30s - bid_depth_now) / bid_depth_t_minus_30s > BID_DEPTH_THIN_THRESHOLD
        (only check if bid_depth_t_minus_30s > 0)
     b. structure_break: price_series[-1] < min(list(price_series)[-15:]) if len >= 15
     c. vwap_break: price_series[-1] < state['vwap_buffer'].get_vwap() if get_vwap() > 0
   - Returns True only if primary fires AND at least one confirmation is True

Implement in `market_data/tiered_streamer.py`:

All WebSocket subscription functions (from PRD Section 14):

async def _send_sub(ws, sub: dict) -> None

async def subscribe_warmup_feeds(ws, coin: str) -> None
  - trades, activeAssetCtx, 5m candle
  - Does NOT include l2Book

async def subscribe_watchlist_feeds(ws, coin: str) -> None
  - ONLY sends incremental l2Book subscription
  - Warm-up feeds already active; do NOT call subscribe_warmup_feeds here
  - Exchange does not document idempotent re-subscription behavior

async def unsubscribe_warmup_feeds(ws, coin: str) -> None

async def unsubscribe_watchlist_feeds(ws, coin: str) -> None
  - Unsubscribes ALL feeds (warmup + l2Book)

Implement in `market_data/state_normaliser.py`:

def handle_message(message: dict, state: dict) -> None
  Central dispatcher for all WS messages for a single coin.
  Channels to handle:
  - 'trades': for each trade → delta_aggregator.on_trade() + flush_if_ready() + vwap_buffer.on_trade()
    px and sz are strings → float(). side is 'A'=sell, 'B'=buy
  - 'l2Book': compute bid depth within 0.5% of mid from state['price_series'][-1],
    update bid_depth_t_minus_30s and bid_depth_now
    px and sz in levels are strings → float()
  - 'activeAssetCtx': pass message["data"]["ctx"] to ingest_asset_ctx (no rest_premium)
  - 'candle': append float(c['h']), float(c['l']), float(c['c']) to 5m series
    (candle fields are numbers but float() applied defensively)
  - 'pong': ignore silently

Write unit tests in `tests/unit/test_trigger.py`:
- trigger_is_valid: fails on price drift, fails on z-score recovery, passes otherwise
- evaluate_trigger: no trigger if z-score not negative enough
- evaluate_trigger: requires at least one confirmation
- handle_message trades: correct side classification, both aggregators updated
- handle_message candle: 5m series updated correctly
- handle_message activeAssetCtx: ingest_asset_ctx called with ctx sub-object
```

### Files
- `strategy/trigger/trigger_engine.py`
- `market_data/tiered_streamer.py`
- `market_data/state_normaliser.py`

### Validation
```bash
pytest tests/unit/test_trigger.py -v
python -c "
from market_data.state_normaliser import handle_message
from shared.state_factory import create_asset_state
import time
state = create_asset_state()
msg = {'channel': 'trades', 'data': [{'px': '1800.0', 'sz': '0.5', 'side': 'A', 'time': int(time.time()*1000), 'tid': 1}]}
handle_message(msg, state)
print('handle_message trades OK')
"
```

---

## Stage 8 — Price Formatter and IOC Execution

### Prompt
```
You are implementing the AltShortBot trading system. Your task is Stage 8: price formatter and IOC execution engine.

Reference: PRD Section 8 (all subsections).

Implement in `oms/price_formatter.py`:

format_price is already implemented in shared/helpers.py.
This module re-exports it and adds:

def validate_size(size_coins: float, sz_decimals: int) -> float:
  - Rounds size to sz_decimals decimal places
  - Returns rounded value

Implement in `oms/order_parser.py`:

def parse_order_status(raw_response: dict) -> ParsedOrderStatus | None:
  Import ParsedOrderStatus from shared.types.
  Parses raw Hyperliquid order placement response:
    raw_response["response"]["data"]["statuses"][0]
  Returns:
    ParsedOrderStatus(status='filled', avg_px=float, total_sz=float, oid=int)
    ParsedOrderStatus(status='resting', oid=int)
    ParsedOrderStatus(status='error', reason=str)
    None — on malformed/empty response (log the raw response)
  KeyError, IndexError, TypeError, ValueError all return None.

Implement in `oms/ioc_entry.py`:

async def place_ioc_aggressive(coin, side, size_coins, reference_price,
                                sz_decimals, slippage_pct=None) -> dict:
  - slippage_pct defaults to IOC_AGGRESSIVE_SLIPPAGE_PCT from constants
  - sell: raw_px = reference_price * (1 - slippage_pct)
  - buy:  raw_px = reference_price * (1 + slippage_pct)
  - limit_px_str = format_price(raw_px, sz_decimals)
  - Returns raw exchange response (caller passes through parse_order_status)

async def execute_entry(coin, size_usd, trigger_price, state) -> ParsedOrderStatus | None:
  Full two-step IOC entry from PRD Section 8.2:

  Step 0: trigger_is_valid check, size check (size_coins * mid < MIN_ORDER_NOTIONAL_USD)

  Step 1: Primary passive IOC
  - limit_px_str = format_price(mid * (1 + LIMIT_ORDER_OFFSET), sz_decimals)
  - raw = await place_limit_order(...)  with tif='Ioc'
  - primary = parse_order_status(raw)

  None handling: if primary is None → ABORT, log "parse_order_status returned None —
    exchange response malformed. Reconcile order/fill state before next entry." → return None

  Error handling (specific to each rejection):
  - 'minTradeNtlRejected' → log "Fatal: minTradeNtlRejected" → return None
  - 'tickRejected' → log "Fatal: tickRejected — price formatting bug" → return None
  - 'oracleRejected' → log "Fatal: oracleRejected — price too far from oracle" → return None
  - 'iocCancelRejected' → log "benign unfilled" → fall through to step 2
  - 'resting' → log "WARNING: unexpected resting for IOC" → fall through
  - other errors → log → fall through

  Step 2: Aggressive IOC fallback (only if trigger_is_valid still passes)
  - raw_fb = await place_ioc_aggressive(coin, 'sell', size_coins, current_mid, sz_decimals)
  - fallback = parse_order_status(raw_fb)
  - Any error on fallback is final — log and return None

  Slippage check on fills:
  - slippage = (float(limit_px_str) - fill_px) / float(limit_px_str)
  - > ABORT_SLIPPAGE: flatten with place_ioc_aggressive buy, return None
  - > MAX_SLIPPAGE: log warning, return filled result

NOTE: place_limit_order(coin, side, size_coins, price_str, tif) is a stub that you
must define — it will be wired to the real exchange in Stage 10. For now, accept it
as an injected dependency or import from oms.execution_adapter.

Write unit tests in `tests/unit/test_execution.py`:
- parse_order_status: correct parsing of filled/resting/error responses
- parse_order_status: returns None on malformed input (missing keys, empty statuses)
- execute_entry: returns None when trigger invalid
- execute_entry: returns None on minTradeNtlRejected, tickRejected, oracleRejected
- execute_entry: proceeds to fallback on iocCancelRejected
- execute_entry: None from parse_order_status triggers abort (no fallback sent)
```

### Files
- `oms/price_formatter.py`
- `oms/order_parser.py`
- `oms/ioc_entry.py`
- `tests/unit/test_execution.py`

### Validation
```bash
pytest tests/unit/test_execution.py -v
python -c "
from oms.order_parser import parse_order_status
raw = {'response': {'data': {'statuses': [{'filled': {'totalSz': '0.5', 'avgPx': '1800.0', 'oid': 12345}}]}}}
result = parse_order_status(raw)
assert result.status == 'filled'
assert result.avg_px == 1800.0
assert result.oid == 12345
print('parse_order_status OK')
"
```

---

## Stage 9 — Risk Engine

### Prompt
```
You are implementing the AltShortBot trading system. Your task is Stage 9: risk engine.

Reference: PRD Sections 9.1–9.7.

Implement in `risk/daily_loss_tracker.py`:

class DailyLossTracker:
  __init__(self, account_equity: float)
  record_close(self, pnl_usd: float) -> Literal['OK', 'KILL', 'DISABLE']
    - Tracks daily_pnl, resets at midnight UTC
    - >= DAILY_LOSS_DISABLE_PCT → disable_until = now + 24h, return 'DISABLE'
    - >= DAILY_LOSS_KILL_PCT → kill_active = True, return 'KILL'
    - _maybe_reset(): resets pnl and kill_active at midnight, NOT disable_until
  is_trading_allowed(self) -> bool
    - Returns False if disable_until is active or kill_active is True

Implement in `risk/correlation_filter.py`:

SECTOR_MAP = { ... }  (copy from PRD Section 9.6 — all coins and their sectors)
Coins not in map → 'Other'

def correlation_check_passes(new_coin: str, open_positions: list) -> bool
  Returns False if adding new_coin puts > MAX_POSITIONS_PER_SECTOR in one sector

Implement in `risk/watchdog.py`:

class HeartbeatMonitor:
  beat(self) -> None: update last_beat (thread-safe with threading.Lock)
  is_dead(self) -> bool: (time.time() - last_beat) > HEARTBEAT_TIMEOUT_S

def start_watchdog(monitor: HeartbeatMonitor, exchange) -> threading.Thread:
  OS thread (not asyncio task).
  Sleeps HEARTBEAT_BEAT_INTERVAL_S each loop.
  On monitor.is_dead():
    - Log "PROCESS DEAD-MAN TRIGGERED"
    - Call asyncio.run(emergency_flatten_all(exchange))
    - Log any exception, break

async def emergency_flatten_all(exchange) -> None:
  For each open position from exchange.get_open_positions():
    - side = 'buy' if pos['szi'] < 0 else 'sell'
    - await place_ioc_aggressive with IOC_EMERGENCY_SLIPPAGE_PCT
  This is a coroutine run in a fresh event loop from the watchdog thread.
  Construct a fresh REST client if needed — do not share the main-loop client.

Implement in `risk/portfolio_controller.py`:

def calculate_stop_distance(entry_price, atr_14, swing_high_price, high_volatility) -> float:
  ATR multiplier: 2.0 if high_volatility else 3.0
  atr_stop = entry_price + (multiplier * atr_14)
  if swing_high <= entry or atr_14 == 0: stop = atr_stop
  else: stop = min(atr_stop, swing_high)
  distance = (stop - entry) / entry
  if distance < MIN_STOP_DISTANCE_PCT: distance = MIN_STOP_DISTANCE_PCT (log warning)
  return distance

def calculate_position_size(account_equity, regime, squeeze_score, stop_distance_pct) -> float:
  Returns NOTIONAL in USD (not risk budget). Formula: risk_budget / stop_distance_pct.
  - Raises ValueError if stop_distance_pct <= 0
  - regime multipliers: NORMAL=1.0, REDUCED=0.5, DISABLED=0.0
  - squeeze >= SQUEEZE_HARD_BLOCK_SCORE: return 0.0
  - squeeze >= SQUEEZE_REDUCE_SCORE: risk_budget *= SQUEEZE_REDUCE_MULTIPLIER

def check_funding_exit(current_funding_rate: float, current_pnl_r: float) -> bool:
  Returns True if funding_rate < 0 and pnl_r < FUNDING_EXIT_PNL_THRESHOLD_R

Write unit tests in `tests/unit/test_risk.py`:
- DailyLossTracker: kill at 3%, disable at 5%, reset at midnight, disable persists across midnight
- correlation_check_passes: blocks when sector full, passes otherwise
- calculate_stop_distance: ATR stop used when swing_high <= entry, floor applied
- calculate_position_size: correct notional, zero on DISABLED regime, reduced on squeeze
- check_funding_exit: correct threshold behaviour
```

### Files
- `risk/daily_loss_tracker.py`
- `risk/correlation_filter.py`
- `risk/watchdog.py`
- `risk/portfolio_controller.py`
- `tests/unit/test_risk.py`

### Validation
```bash
pytest tests/unit/test_risk.py -v
python -c "
from risk.portfolio_controller import calculate_position_size
result = calculate_position_size(10000, 'NORMAL', 0, 0.02)
assert result == 5000.0, f'expected 5000.0 got {result}'
print('position sizing OK')
"
```

---

## Stage 10 — OMS Core (Nonce, Batching, Exchange Adapter)

### Prompt
```
You are implementing the AltShortBot trading system. Your task is Stage 10: Order Management Service core.

Reference: PRD Sections 2.9, 2.10, 2.11.

Implement in `oms/nonce_manager.py`:

class NonceManager:
  Thread-safe monotonic nonce counter.
  __init__(self): self._nonce = 0, self._lock = threading.Lock()
  next_nonce(self) -> int: atomically increments and returns nonce
  NOTE: In production, initialise from current timestamp ms to avoid reuse after restart.

Implement in `oms/execution_adapter.py`:

class ExchangeAdapter:
  __init__(self, wallet_address: str, private_key: str, testnet: bool = True):
    - Stores credentials
    - Creates NonceManager
    - Creates aiohttp.ClientSession (to be created in async context)
    - base_url: mainnet or testnet endpoint

  async def place_limit_order(self, coin, side, size_coins, price_str, tif='Gtc') -> dict:
    Builds Hyperliquid exchange action:
    {
      "action": {
        "type": "order",
        "orders": [{
          "a": asset_index,  # integer index from universe metadata
          "b": side == 'buy',
          "p": price_str,    # canonical string from format_price
          "s": str(round(size_coins, sz_decimals)),
          "r": False,        # reduce-only
          "t": {"limit": {"tif": tif}}
        }],
        "grouping": "na"
      },
      "nonce": self.nonce_manager.next_nonce(),
      "signature": <EIP-712 signature>
    }
    POST to /exchange
    Returns raw response dict.

    IMPORTANT: Signature implementation is out of scope for this stage.
    Use a placeholder that raises NotImplementedError with message
    "EIP-712 signing not yet implemented — wire in hyperliquid-python-sdk or implement directly".

  async def get_open_positions(self) -> list:
    POST /info {"type": "clearinghouseState", "user": wallet_address}
    Returns assetPositions list filtered to szi != '0'

  async def close(self) -> None:
    Close aiohttp session.

  @property
  def heartbeat_monitor(self) -> HeartbeatMonitor:
    Returns the monitor instance (set in __init__)

Wire place_limit_order into `oms/ioc_entry.py`:
  Import ExchangeAdapter. Accept it as a parameter or module-level singleton.
  Replace the stub with a real call: await exchange.place_limit_order(...)

Write integration test stub in `tests/integration/test_exchange_adapter.py`:
  Mark all tests with @pytest.mark.skip(reason="requires live credentials")
  Document what each test would verify:
  - place_limit_order returns correct response shape
  - get_open_positions parses positions correctly
  - Nonce increments correctly across calls

Note on signing: The EIP-712 signature is the hardest exchange-specific piece.
Options in order of effort:
  1. Use the official hyperliquid-python-sdk (pip install hyperliquid-python)
  2. Implement EIP-712 directly using eth_account library
  3. Use a reference implementation from Hyperliquid's GitHub examples
Whichever you choose, wire it in before live/paper trading (Stage 12).
```

### Files
- `oms/nonce_manager.py`
- `oms/execution_adapter.py`
- `tests/integration/test_exchange_adapter.py`

### Validation
```bash
pytest tests/unit/ -v  # all prior unit tests still pass
python -c "
from oms.nonce_manager import NonceManager
nm = NonceManager()
assert nm.next_nonce() == 1
assert nm.next_nonce() == 2
print('NonceManager OK')
"
```

---

## Stage 11 — WebSocket Connection Manager

### Prompt
```
You are implementing the AltShortBot trading system. Your task is Stage 11: WebSocket connection manager.

Reference: PRD Section 14.

Implement in `market_data/ws_manager.py`:

WS_URL and WS_PING_INTERVAL_S from shared.constants.

async def ws_connection_manager(coin: str, state: dict, exchange) -> None:
  Retry loop with exponential backoff (1s → doubles → max WS_RECONNECT_MAX_DELAY_S).

  On each connection:
    1. await subscribe_warmup_feeds(ws, coin)
    2. await refresh_funding_from_rest(coin, state)
    3. state['has_data_gap'] = False; state['delta_ready'] = False
    4. Inner loop with asyncio.wait_for(ws.recv(), timeout=WS_PING_INTERVAL_S):
       - On message: json.loads → if channel == 'pong': continue, else handle_message + exchange.heartbeat_monitor.beat()
       - On asyncio.TimeoutError: send {"method": "ping"}, log "Ping sent for {coin}"

  On ConnectionClosed or OSError:
    - state['has_data_gap'] = True; state['delta_ready'] = False
    - await asyncio.sleep(retry_delay)
    - retry_delay = min(retry_delay * 2, WS_RECONNECT_MAX_DELAY_S)

  ARCHITECTURE NOTE: ws_connection_manager is stateless with respect to tier.
  It always starts in warm-up mode (subscribe_warmup_feeds only).
  The main loop calls subscribe_watchlist_feeds(ws, coin) separately when
  a coin is promoted to the active watch list.

Also add to `market_data/ws_manager.py`:

async def run_ws_for_coin(coin: str, all_states: dict, exchange) -> None:
  Thin wrapper: ensures state exists in all_states, then calls ws_connection_manager.

Write integration test stub in `tests/integration/test_ws_manager.py`:
  All marked @pytest.mark.skip(reason="requires network")
  Document:
  - subscribe_warmup_feeds sends correct JSON for all 3 subscriptions
  - subscribe_watchlist_feeds sends ONLY l2Book (not warmup feeds again)
  - Ping is sent after WS_PING_INTERVAL_S of silence
  - has_data_gap set True on disconnect
```

### Files
- `market_data/ws_manager.py`

### Validation
```bash
pytest tests/unit/ -v   # all prior tests still pass
python -c "
import asyncio, json
from market_data.tiered_streamer import subscribe_warmup_feeds, subscribe_watchlist_feeds
# Verify subscribe_watchlist_feeds does NOT call subscribe_warmup_feeds
import inspect
src = inspect.getsource(subscribe_watchlist_feeds)
assert 'subscribe_warmup_feeds' not in src, 'subscribe_watchlist_feeds must not call subscribe_warmup_feeds'
print('subscribe_watchlist_feeds is incremental-only OK')
"
```

---

## Stage 12 — Main Loop and EIP-712 Signing

### Prompt
```
You are implementing the AltShortBot trading system. Your task is Stage 12: main loop and exchange signing.

Reference: PRD Sections 2.6, 2.8, 2.9.

Part A — EIP-712 Signing

Wire in signing to `oms/execution_adapter.py`.
Use: pip install hyperliquid-python (official SDK) or eth_account + manual EIP-712.

The Hyperliquid exchange action must be signed with the wallet's private key.
Replace the NotImplementedError placeholder in place_limit_order with a real signature.

Signing must:
  - Use the wallet's private key from config
  - Produce a valid EIP-712 signature accepted by the exchange
  - Set the nonce to prevent replay

Test signing works in testnet BEFORE enabling mainnet.

Part B — Main Loop

Implement `main.py`:

async def main():
  1. Load config from environment (dotenv)
  2. Initialise ExchangeAdapter (testnet by default)
  3. Create all_states = {coin: create_asset_state() for coin in universe_coins}
  4. Start HeartbeatMonitor and watchdog thread
  5. await bootstrap_universe_funding(universe_coins, all_states)
  6. Start ws_connection_manager tasks for all coins (asyncio.gather)
  7. Start regime refresh loop (refresh_1h_closes every 60 min)
  8. Main scanner loop every 30s:
     a. if not daily_loss_tracker.is_trading_allowed(): continue
     b. gate12 = await run_universe_scanner(universe_coins, all_states)
     c. new_watchlist = await promote_to_watch_list(gate12, current_watchlist, all_states, now)
     d. Handle promotions: subscribe_watchlist_feeds for new entries
     e. Handle demotions: reset_warmup_state + unsubscribe for removed coins
     f. regime = regime_filter(cached_1h_closes['BTC'], current_watchlist, cached_1h_closes)
     g. For each active watch-list coin: evaluate_trigger → if fires → execute_entry

Implement `config/settings.py`:
  Load all settings from environment variables with sensible defaults.
  Expose as a Settings dataclass or simple module-level constants.

Implement `scripts/paper_trade.py`:
  Thin wrapper that sets HL_TESTNET=true and calls main().

Implement `scripts/live_trade.py`:
  Same but requires explicit confirmation prompt before starting.
```

### Files
- `oms/execution_adapter.py` — wire signing
- `main.py`
- `config/settings.py`
- `scripts/paper_trade.py`
- `scripts/live_trade.py`

### Validation
```bash
# Testnet smoke test (requires .env with testnet credentials)
python -c "
import asyncio
from oms.execution_adapter import ExchangeAdapter
import os; from dotenv import load_dotenv; load_dotenv()
async def test():
    ex = ExchangeAdapter(os.getenv('HL_API_WALLET_ADDRESS'), os.getenv('HL_PRIVATE_KEY'), testnet=True)
    positions = await ex.get_open_positions()
    print(f'Open positions: {positions}')
    await ex.close()
asyncio.run(test())
"
```

---

## Stage 13 — Backtester

### Prompt
```
You are implementing the AltShortBot trading system. Your task is Stage 13: backtester.

Reference: PRD Sections 11.1–11.4.

Implement in `backtest/data_loader.py`:

async def load_candles(coin: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
  - Fetches candleSnapshot in batches (max 5000 candles per request)
  - Returns DataFrame with columns: time, open, high, low, close, volume
  - All numeric

async def load_funding_history(coin: str, start_ms: int, end_ms: int) -> pd.DataFrame:
  - Fetches fundingHistory in batches
  - Returns DataFrame with columns: time, funding_rate (÷8 applied), premium
  - funding_rate is per-hour

Implement in `backtest/slippage_model.py`:

def apply_entry_slippage(mid_price: float) -> float:
  Entry (short): fill_price = mid * (1 - SLIPPAGE_MODEL_PCT)  ← worse for short

def apply_exit_slippage(mid_price: float) -> float:
  Exit (cover): fill_price = mid * (1 + SLIPPAGE_MODEL_PCT)   ← worse for buyback

Implement in `backtest/metrics.py`:

def compute_metrics(trades: list) -> dict:
  trades: list of dicts with keys: entry_px, exit_px, size_coins, funding_collected, entry_time, exit_time
  Returns:
    sharpe_ratio, max_drawdown, win_rate, expectancy_r, total_trades, total_pnl_pct

Implement in `backtest/engine.py`:

class BacktestEngine:
  __init__(self, coins: list, start_date: str, end_date: str)

  async def run(self) -> dict:
    For each coin:
    1. Load 1m + 5m candles and hourly funding
    2. Simulate 1-min bars:
       a. Build oi_series and price_series from 1m closes (proxy for markPx)
       b. Build funding_series from hourly funding (÷8 applied)
       c. Apply gate1_passes, gate2_passes, gate3_score at each bar
       d. If all gates pass: simulate entry with apply_entry_slippage
       e. Simulate hold: track funding collected/paid (per-hour rate × hours held)
       f. Exit on stop-loss, take-profit, or funding exit condition
       g. Apply apply_exit_slippage on exit
    3. Collect all trades → compute_metrics()
    Return metrics per coin and aggregate

  BIAS RULES (from PRD Section 11.3):
  - Gate 3 uses only data available at bar close (no look-ahead)
  - VWAP computed from trades up to that bar (approximate from volume)
  - Use per-hour funding rate active at entry time
  - Slippage hurts shorts on both entry AND exit

Implement `scripts/bootstrap.py`:
  CLI entrypoint for running the backtester.
  Arguments: --coins ETH,SOL,... --start 2024-01-01 --end 2024-12-31
  Prints metrics table. Saves trades CSV to logs/.

Write basic tests in `tests/unit/test_backtest.py`:
- apply_entry_slippage: fill < mid (worse for short)
- apply_exit_slippage: fill > mid (worse for cover)
- compute_metrics: correct sharpe and drawdown on synthetic trade list
```

### Files
- `backtest/data_loader.py`
- `backtest/slippage_model.py`
- `backtest/metrics.py`
- `backtest/engine.py`
- `scripts/bootstrap.py`
- `tests/unit/test_backtest.py`

### Validation
```bash
pytest tests/unit/test_backtest.py -v
# Run backtest on one coin, short window (requires network)
# python scripts/bootstrap.py --coins ETH --start 2024-06-01 --end 2024-09-01
# Expect: gate fire rate 5–15% of universe at any time
# Expect: Sharpe > 1.0, max DD < 20% to proceed to paper trading
```

---

## Stage 14 — Validation Gates Before Live Capital

**These are not code stages. They are go/no-go checkpoints.**

### Gate 1: Unit test coverage
```bash
pytest tests/unit/ -v --tb=short
# Every stage's tests must pass. Zero failures permitted.
```

### Gate 2: Funding cadence validation
```bash
python -c "
import asyncio, time
from market_data.universe_snapshotter import rest_post
async def check():
    r = await rest_post('/info', {'type': 'fundingHistory', 'coin': 'ETH',
        'startTime': int((time.time()-49*3600)*1000)})
    times = [e['time'] for e in r[-10:]]
    gaps = [times[i+1]-times[i] for i in range(len(times)-1)]
    print(f'Cadence gaps (ms): {gaps}')
    print(f'Expected: ~3600000ms (hourly)')
asyncio.run(check())
"
# Confirm hourly cadence. If different, update GATE1_MIN_POSITIVE_HOURS
# and the bootstrap logic before proceeding.
```

### Gate 3: Backtest metrics
```
Run scripts/bootstrap.py on at least 6 months of data for 5+ coins.
Required before paper trading:
  - Sharpe Ratio > 1.0
  - Max Drawdown < 20%
  - Gate fire rate 5–15% of universe per cycle
  - Signal frequency looks plausible (not firing every 30s or never firing)
```

### Gate 4: Paper trade (2 weeks minimum)
```
Run scripts/paper_trade.py (testnet) for 2 weeks.
Required before live capital:
  - Live signal frequency within ±30% of backtest frequency
  - No obvious logic bugs in logs
  - WS reconnections handled cleanly
  - Watchdog fires and recovers correctly (test by killing the process)
```

### Gate 5: Live Phase 3 criteria
```
Max 3 positions, $100–$200 each, isolated margin only.
Required before scaling:
  - 50+ completed trades
  - Sharpe > 1.0
  - Max DD < 15%
```

---

## Quick Reference: PRD Section Mapping

| Stage | PRD Sections |
|---|---|
| 1 — Helpers | 3.1, 3.2, 3.3, 3.4 |
| 2 — VwapBuffer, DeltaAggregator | 3.4, 3.5, 7.1 |
| 3 — LiquidationModel | 5.1, 5.2, 5.3 |
| 4 — Data Ingestion | 2.6 (Rules + Path A + Path B) |
| 5 — Gates 1, 2, 3 | 4.1, 4.2, 4.3, 4.4 |
| 6 — Regime Filter | 6 |
| 7 — Trigger Engine + Message Handler | 7.1, 7.2, 7.3 |
| 8 — IOC Execution | 8.1, 8.2, 8.3, 8.4 |
| 9 — Risk Engine | 9.1–9.7 |
| 10 — OMS Core | 2.9, 2.10, 2.11 |
| 11 — WS Manager | 14 |
| 12 — Main Loop | 2.8, 2.3 |
| 13 — Backtester | 11 |
