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
  1. Use the official hyperliquid-python-sdk (pip install hyperliquid-python-sdk)
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

## Stage 12 — Main Loop and Exchange Signing

### Prompt

```
You are implementing the AltShortBot trading system. Your task is Stage 12: main loop
and exchange signing.

Reference: PRD Sections 2.6, 2.8, 2.9.

─────────────────────────────────────────────────────────────────────
DECISION 1: SIGNING — USE eth_account DIRECTLY, NOT THE SDK
─────────────────────────────────────────────────────────────────────

The hyperliquid-python-sdk uses the blocking `requests` library internally.
Calling Exchange.order() from async code freezes the event loop for the
duration of the HTTP round-trip, blocking all WS message processing and
the ping loop. Do not use Exchange.order() or Info() directly in async code.

Instead:
  pip install hyperliquid-python-sdk eth_account

Use the SDK only for EIP-712 signing utilities, not for HTTP calls.
Keep the existing aiohttp-based place_limit_order from Stage 10 for HTTP.
Add signing to it like this:

  from eth_account import Account
  from hyperliquid.utils.signing import sign_l1_action, get_timestamp_ms
  # Verified import path for hyperliquid-python-sdk — confirmed correct.

  # In __init__: create the Account object ONCE and reuse it.
  self._wallet = Account.from_key(private_key)
  self._is_mainnet = not testnet

  def _sign_action(self, action: dict) -> dict:
      # Nonce must be a current timestamp in ms, NOT a monotonic counter.
      # Hyperliquid rejects nonces it has already seen; a counter starting
      # from 0 would be rejected after any process restart.
      nonce     = get_timestamp_ms()
      # sign_l1_action signature:
      #   sign_l1_action(wallet, action, vault_address, nonce, expires_after, is_mainnet)
      signature = sign_l1_action(self._wallet, action, None, nonce, None, self._is_mainnet)
      return {
          "action":       action,
          "nonce":        nonce,
          "signature":    signature,
          "vaultAddress": None,
          "expiresAfter": None,
      }

  async def place_limit_order(self, coin, side, size_coins, price_str, tif='Gtc') -> dict:
      asset_idx   = self.get_asset_index(coin)
      sz_decimals = self.get_sz_decimals(coin)
      action = {
          "type": "order",
          "orders": [{
              "a": asset_idx,
              "b": side == 'buy',
              "p": price_str,
              "s": str(round(size_coins, sz_decimals)),
              "r": False,
              "t": {"limit": {"tif": tif}},
          }],
          "grouping": "na",
      }
      payload  = self._sign_action(action)
      response = await self._post("/exchange", payload)
      return response

  # _post reuses a single aiohttp.ClientSession stored as self._session.
  # Do NOT create a new ClientSession per call — that opens/tears down a
  # connection pool on every request and causes "Unclosed client session" warnings.
  # Create self._session = aiohttp.ClientSession() in __init__ (or lazily on
  # first call) and close it in close().

  async def _post(self, path: str, payload: dict) -> dict:
      async with self._session.post(self.base_url + path, json=payload) as r:
          return await r.json()

  async def close(self) -> None:
      await self._session.close()

For Info calls (metaAndAssetCtxs, user_state, fundingHistory etc.) keep the
existing rest_post from universe_snapshotter.py — those are already async.

─────────────────────────────────────────────────────────────────────
DECISION 2: WS SUBSCRIPTIONS FROM THE MAIN LOOP
─────────────────────────────────────────────────────────────────────

ws_connection_manager owns the ws object inside async with websockets.connect().
The main loop runs in a separate task and cannot access ws directly.

Use a per-coin asyncio.Queue to pass subscription commands from the main loop
to the WS task:

  # In create_asset_state() — add to state factory:
  "ws_command_queue": None,  # set to asyncio.Queue() at startup

  # In main.py startup, before launching WS tasks:
  for coin in universe_coins:
      all_states[coin]["ws_command_queue"] = asyncio.Queue()

  # In ws_connection_manager — drain the queue on each message loop iteration:
  while True:
      try:
          raw = await asyncio.wait_for(ws.recv(), timeout=WS_PING_INTERVAL_S)
          # ... handle message ...
      except asyncio.TimeoutError:
          await ws.send(json.dumps({"method": "ping"}))

      # Drain pending subscription commands (non-blocking)
      queue = state["ws_command_queue"]
      while not queue.empty():
          cmd, sub_type = queue.get_nowait()
          if cmd == "subscribe":
              await _send_sub(ws, {"type": sub_type, "coin": coin})
          elif cmd == "unsubscribe":
              await ws.send(json.dumps({
                  "method": "unsubscribe",
                  "subscription": {"type": sub_type, "coin": coin}
              }))

  # In main loop — to promote a coin to active watch list:
  state["ws_command_queue"].put_nowait(("subscribe", "l2Book"))

  # To demote — unsubscribe all:
  for feed in ("trades", "activeAssetCtx", "candle", "l2Book"):
      state["ws_command_queue"].put_nowait(("unsubscribe", feed))

This keeps ws entirely owned by the WS task and the main loop fully decoupled.

─────────────────────────────────────────────────────────────────────
DECISION 3: sz_decimals IN STATE, NOT FROM EXCHANGE IN execute_entry
─────────────────────────────────────────────────────────────────────

execute_entry(coin, size_usd, trigger_price, state) must not take an exchange
parameter — it should remain testable without mocking an exchange.

Instead: at WS subscription time, populate sz_decimals in state:

  # In main.py when subscribing a coin's WS feeds:
  all_states[coin]["sz_decimals"] = exchange.get_sz_decimals(coin)

  # Add to create_asset_state():
  "sz_decimals": 0,   # populated at subscription time from exchange coin meta

  # In ioc_entry.py execute_entry:
  sz_decimals = state["sz_decimals"]   # already there — no exchange needed

─────────────────────────────────────────────────────────────────────
MAIN LOOP — CONCRETE VARIABLE DEFINITIONS
─────────────────────────────────────────────────────────────────────

All variables referenced in the scanner loop must be defined. Use these:

  # Equity — fetch once at startup, update after every closed position:
  equity = float(
      (await exchange.get_user_state())["marginSummary"]["accountValue"]
  )
  # Cache and refresh every N cycles rather than fetching on every trade.

  # Open positions — maintain a local set updated on every fill:
  open_positions: list[str] = []   # list of coin names currently held short

  # trigger_price — current mid price at the moment trigger fires:
  current_mid   = float(list(state["price_series"])[-1]) if state["price_series"] else 0.0
  trigger_price = current_mid   # trigger fires at current price

  # stop_distance — must be computed before position sizing:
  atr_14 = compute_atr(
      state["high_series_5m"], state["low_series_5m"], state["close_series_5m"]
  )
  swing_high     = max(list(state["price_series"])[-15:]) if len(state["price_series"]) >= 15 else current_mid
  prices_60      = list(state["price_series"])[-60:] if len(state["price_series"]) >= 60 else []
  high_vol       = (max(prices_60) - min(prices_60)) / current_mid > HIGH_VOL_1H_RANGE_PCT if prices_60 else False
  stop_distance  = calculate_stop_distance(current_mid, atr_14, swing_high, high_vol)

─────────────────────────────────────────────────────────────────────
ASSET INDEX AND sz_decimals — BUILD AND CACHE AT STARTUP
─────────────────────────────────────────────────────────────────────

Add to ExchangeAdapter:

  async def build_coin_meta(self) -> None:
      """Call once at startup before any orders or subscriptions."""
      response = await rest_post("/info", {"type": "meta"})
      self.coin_meta = {
          asset["name"]: {
              "asset_index": i,
              "sz_decimals": asset["szDecimals"],
          }
          for i, asset in enumerate(response["universe"])
      }

  def get_sz_decimals(self, coin: str) -> int:
      return self.coin_meta[coin]["sz_decimals"]

  def get_asset_index(self, coin: str) -> int:
      return self.coin_meta[coin]["asset_index"]

  async def get_user_state(self) -> dict:
      return await rest_post("/info", {
          "type": "clearinghouseState",
          "user": self._wallet_address,
      })

  async def get_open_positions(self) -> list:
      state = await self.get_user_state()
      return [p for p in state["assetPositions"]
              if float(p["position"]["szi"]) != 0]

  async def cancel_all_orders(self) -> None:
      """Called on clean shutdown — cancels all resting orders."""
      open_orders = await rest_post("/info", {
          "type": "openOrders", "user": self._wallet_address
      })
      if not open_orders:
          return
      # Batch all cancels into a single request.
      # "a" must be the integer asset index, NOT the coin name string.
      cancel_action = {
          "type": "cancel",
          "cancels": [
              {"a": self.get_asset_index(order["coin"]), "o": order["oid"]}
              for order in open_orders
          ],
      }
      await self._post("/exchange", self._sign_action(cancel_action))

─────────────────────────────────────────────────────────────────────
DRY RUN FLAG
─────────────────────────────────────────────────────────────────────

  # In config/settings.py
  DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"

  # In scanner loop inside trigger evaluation:
  if evaluate_trigger(state, trigger_price, current_mid):
      log.info("TRIGGER FIRED", coin=coin, trigger_price=trigger_price,
               dry_run=settings.DRY_RUN)
      if not settings.DRY_RUN:
          result = await execute_entry(coin, size_usd, trigger_price, state)

Default DRY_RUN=true. Only set false after 48-72h dry run validates signal frequency.

─────────────────────────────────────────────────────────────────────
SCANNER LOOP TIMING
─────────────────────────────────────────────────────────────────────

  async def scanner_loop(self):
      while True:
          loop_start = time.time()
          try:
              await self.run_one_cycle()
          except Exception as e:
              log.error("Scanner cycle failed", error=str(e))
          elapsed   = time.time() - loop_start
          sleep_for = max(0.0, 30.0 - elapsed)
          await asyncio.sleep(sleep_for)

─────────────────────────────────────────────────────────────────────
GRACEFUL SHUTDOWN
─────────────────────────────────────────────────────────────────────

  import signal

  def setup_signal_handlers(loop, exchange):
      for sig in (signal.SIGINT, signal.SIGTERM):
          loop.add_signal_handler(
              sig, lambda: asyncio.create_task(shutdown(exchange))
          )

  async def shutdown(exchange):
      log.info("Shutdown: cancelling open orders")
      await exchange.cancel_all_orders()
      tasks = [t for t in asyncio.all_tasks()
               if t is not asyncio.current_task()]
      for task in tasks:
          task.cancel()
      log.info("Shutdown complete")

Note: cancel orders on clean shutdown, do NOT flatten positions.
Position flattening only happens on watchdog trigger (process freeze).

─────────────────────────────────────────────────────────────────────
FULL MAIN LOOP STRUCTURE
─────────────────────────────────────────────────────────────────────

Implement `main.py`:

async def main():
  1.  Load config (dotenv)
  2.  Initialise ExchangeAdapter (mainnet or testnet from HL_TESTNET env var)
  3.  await exchange.build_coin_meta()
  4.  all_states = {coin: create_asset_state() for coin in universe_coins}
  5.  For each coin: all_states[coin]["ws_command_queue"] = asyncio.Queue()
  6.  For each coin: all_states[coin]["sz_decimals"] = exchange.get_sz_decimals(coin)
  7.  equity = float((await exchange.get_user_state())["marginSummary"]["accountValue"])
  8.  Start HeartbeatMonitor and watchdog thread
  9.  Setup signal handlers
  10. await bootstrap_universe_funding(universe_coins, all_states)
  11. Launch ws_connection_manager tasks (one per coin, asyncio.gather)
  12. Launch regime refresh loop (refresh_1h_closes every REGIME_CANDLE_HISTORY_HOURS)
  13. Run scanner_loop():
      a. if not daily_loss_tracker.is_trading_allowed(): log, skip cycle
      b. gate12 = await run_universe_scanner(universe_coins, all_states)
      c. new_watchlist = await promote_to_watch_list(
             gate12, current_watchlist, all_states, now)
      d. For newly promoted coins:
           state["ws_command_queue"].put_nowait(("subscribe", "l2Book"))
           state["is_on_watchlist"] = True
      e. For demoted watch-list coins (in current but not in new):
           reset_warmup_state(coin, state)
           for feed in ("trades", "activeAssetCtx", "candle", "l2Book"):
               state["ws_command_queue"].put_nowait(("unsubscribe", feed))
           state["is_on_watchlist"] = False
      f. For warm-up coins that dropped from gate12:
           reset_warmup_state(coin, state)
           for feed in ("trades", "activeAssetCtx", "candle"):
               state["ws_command_queue"].put_nowait(("unsubscribe", feed))
      g. regime = regime_filter(cached_1h_closes.get("BTC", []),
                                current_watchlist, cached_1h_closes)
      h. if regime == "DISABLED": log and skip trigger evaluation this cycle
      i. open_positions = [c for c in universe_coins
                           if all_states[c]["position_state"] == "open"]
      j. For each coin in current_watchlist:
           if state["has_data_gap"]: continue
           if len(state["price_series"]) == 0: continue
           current_mid   = float(list(state["price_series"])[-1])
           if current_mid == 0: continue
           trigger_price = current_mid   # snapshot at detection time; drift checked in execute_entry
           if not evaluate_trigger(state, trigger_price, current_mid): continue
           atr_14        = compute_atr(state["high_series_5m"],
                                       state["low_series_5m"],
                                       state["close_series_5m"])
           swing_high    = max(list(state["price_series"])[-15:])
           prices_60     = list(state["price_series"])[-60:]
           high_vol      = ((max(prices_60) - min(prices_60)) / current_mid
                            > HIGH_VOL_1H_RANGE_PCT) if prices_60 else False
           stop_distance = calculate_stop_distance(
                               current_mid, atr_14, swing_high, high_vol)
           size_usd      = calculate_position_size(
                               equity, regime, state["squeeze_score"], stop_distance)
           if size_usd == 0: continue
           if not correlation_check_passes(coin, open_positions): continue
           if len(open_positions) >= MAX_CONCURRENT_POSITIONS: continue
           log.info("TRIGGER FIRED", coin=coin, dry_run=settings.DRY_RUN)
           if not settings.DRY_RUN:
               result = await execute_entry(coin, size_usd, trigger_price, state)
               if result and result.status == "filled":
                   state["position_state"] = "open"
                   open_positions.append(coin)

Implement `config/settings.py`: Settings dataclass from environment.
Implement `scripts/paper_trade.py`: sets DRY_RUN=true, calls main(), prints banner.
Implement `scripts/live_trade.py`: prompts 'Type CONFIRM to start live trading: ',
  aborts unless user types exactly CONFIRM, sets DRY_RUN=false, calls main().
```

### Files

- `oms/execution_adapter.py` — add signing via eth_account, build_coin_meta,
  get_user_state, cancel_all_orders
- `oms/ioc_entry.py` — use state["sz_decimals"] instead of exchange parameter
- `shared/state_factory.py` — add ws_command_queue and sz_decimals fields
- `market_data/ws_manager.py` — drain ws_command_queue in message loop
- `main.py`
- `config/settings.py`
- `scripts/paper_trade.py`
- `scripts/live_trade.py`

### Validation

**Step 1 — Verify signing import path**

```bash
python -c "
import hyperliquid, os
print('SDK location:', hyperliquid.__file__)
# Find the signing module — check utils/signing.py or similar
import importlib.util, pathlib
sdk_dir = pathlib.Path(hyperliquid.__file__).parent
print('Signing candidates:')
for f in sdk_dir.rglob('sign*.py'): print(' ', f)
for f in sdk_dir.rglob('*signing*'): print(' ', f)
"
```

**Step 2 — Smoke test: account state + order + cancel**

```bash
python - << 'EOF'
import asyncio, os
from dotenv import load_dotenv
from market_data.universe_snapshotter import rest_post
from oms.execution_adapter import ExchangeAdapter

load_dotenv()

async def smoke_test():
    ex = ExchangeAdapter(
        os.getenv("HL_API_WALLET_ADDRESS"),
        os.getenv("HL_PRIVATE_KEY"),
        testnet=False
    )
    await ex.build_coin_meta()

    # 1. Account state
    user_state = await ex.get_user_state()
    print("Account value:", user_state["marginSummary"]["accountValue"])

    # 2. sz_decimals cached correctly
    print("ETH sz_decimals:", ex.get_sz_decimals("ETH"))
    print("ETH asset_index:", ex.get_asset_index("ETH"))

    # 3. Place a limit order far from market, cancel immediately
    result = await ex.place_limit_order("ETH", "sell", 0.01, "1.0", tif="Gtc")
    print("Order result:", result)
    statuses = result["response"]["data"]["statuses"]
    print("statuses[0]:", statuses[0])   # must be {"resting": {"oid": ...}}
    oid = statuses[0]["resting"]["oid"]

    await ex.cancel_all_orders()
    print("Cancelled OK")

asyncio.run(smoke_test())
EOF
```

**Step 3 — Queue wiring test**

```bash
python -c "
import asyncio
from shared.state_factory import create_asset_state
state = create_asset_state()
state['ws_command_queue'] = asyncio.Queue()
state['ws_command_queue'].put_nowait(('subscribe', 'l2Book'))
cmd, feed = state['ws_command_queue'].get_nowait()
assert cmd == 'subscribe' and feed == 'l2Book'
print('Queue wiring OK')
"
```

**Step 4 — 48–72h dry run**

```bash
DRY_RUN=true python scripts/live_trade.py
# Confirm in logs:
# - funding_series populated for all coins after bootstrap
# - Gate 1 fires for some coins (if zero: check funding cadence)
# - 2-5 coins on watch list at any time
# - "TRIGGER FIRED dry_run=True" appears but no orders placed
# - No event loop blocking (WS messages still arriving during order evaluation)

# Funding cadence check:
python - << 'EOF'
import asyncio, time
from market_data.universe_snapshotter import rest_post
async def check():
    r = await rest_post('/info', {'type': 'fundingHistory', 'coin': 'ETH',
        'startTime': int((time.time() - 49 * 3600) * 1000)})
    times = [e['time'] for e in r[-10:]]
    gaps  = [times[i+1] - times[i] for i in range(len(times) - 1)]
    print(f'Cadence gaps (ms): {gaps}')
    print(f'Expected ~3600000 (hourly). If ~28800000: cadence is 8-hourly.')
asyncio.run(check())
EOF
```

**Step 5 — Enable live trading**

```bash
# Only after dry run passes all checks above
DRY_RUN=false python scripts/live_trade.py
# Type CONFIRM at the prompt
```

---

## Stage 13 — Backtester

### Prompt

```
You are implementing the AltShortBot trading system. Your task is Stage 13: backtester.

Reference: PRD Sections 11.1–11.4.

─────────────────────────────────────────────────────────────────────
DATA AVAILABILITY NOTE
─────────────────────────────────────────────────────────────────────

Gate 2 (OI divergence) requires historical open-interest series.
Hyperliquid does not expose historical OI via any candle or snapshot
endpoint — it is only available in real-time via metaAndAssetCtxs.
Therefore: Gate 2 is SKIPPED in the backtester.
The backtest pipeline is: Gate 1 (funding) → Gate 3 (price structure).
Document this clearly in BacktestEngine.run() with a comment.

─────────────────────────────────────────────────────────────────────
Implement in `backtest/data_loader.py`:
─────────────────────────────────────────────────────────────────────

async def load_candles(coin: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
  - Fetches candleSnapshot in batches of 5000 candles per request.
    For 1m interval: 5000 candles = ~3.5 days. Loop until end_ms reached.
  - Sleep 0.1s between batch requests to avoid rate limiting.
  - Log progress: "Loading {coin} {interval}: batch {n}, {total} candles so far"
  - Returns DataFrame with columns: time, open, high, low, close, volume
  - All values cast to float; time cast to int.

async def load_funding_history(coin: str, start_ms: int, end_ms: int) -> pd.DataFrame:
  - Fetches fundingHistory. Hyperliquid returns up to 500 entries per request.
    Batch by startTime, advancing past the last returned timestamp each iteration.
  - Sleep 0.1s between batch requests.
  - Returns DataFrame with columns: time, funding_rate, premium
  - funding_rate = float(entry['fundingRate']) / 8  (convert 8h rate to per-hour)
  - premium = float(entry['premium']) if present, else 0.0

─────────────────────────────────────────────────────────────────────
Implement in `backtest/slippage_model.py`:
─────────────────────────────────────────────────────────────────────

def apply_entry_slippage(mid_price: float) -> float:
  Entry (short): fill_price = mid * (1 - SLIPPAGE_MODEL_PCT)  ← worse for short

def apply_exit_slippage(mid_price: float) -> float:
  Exit (cover): fill_price = mid * (1 + SLIPPAGE_MODEL_PCT)   ← worse for buyback

─────────────────────────────────────────────────────────────────────
Implement in `backtest/metrics.py`:
─────────────────────────────────────────────────────────────────────

def compute_metrics(trades: list[dict], initial_equity: float) -> dict:
  Each trade dict has keys:
    entry_px, exit_px, size_coins, funding_collected_usd,
    entry_time, exit_time, stop_distance_pct

  Definitions:
  - pnl_usd per trade = (entry_px - exit_px) * size_coins + funding_collected_usd
    (short: profit when exit_px < entry_px)
  - pnl_pct per trade = pnl_usd / (entry_px * size_coins)
  - r_multiple per trade = pnl_pct / stop_distance_pct
    (R = 1.0 means the trade hit the stop exactly)
  - win_rate = trades with pnl_usd > 0 / total trades
  - expectancy_r = mean(r_multiple across all trades)
  - total_pnl_pct = sum(pnl_usd) / initial_equity
  - max_drawdown: peak-to-trough of cumulative pnl_usd curve
  - sharpe_ratio: annualised Sharpe of per-trade pnl_pct series,
    assuming 0 risk-free rate.
    sharpe = mean(pnl_pct) / std(pnl_pct) * sqrt(trades_per_year)
    trades_per_year = total_trades / years_in_backtest
    Return 0.0 if fewer than 2 trades or std == 0.

  Returns dict with keys:
    sharpe_ratio, max_drawdown, win_rate, expectancy_r,
    total_trades, total_pnl_pct

─────────────────────────────────────────────────────────────────────
Implement in `backtest/engine.py`:
─────────────────────────────────────────────────────────────────────

class BacktestEngine:
  __init__(self, coins: list[str], start_date: str, end_date: str,
           initial_equity: float = 10_000.0)
  Dates as 'YYYY-MM-DD' strings, converted to ms timestamps internally.

  async def run(self) -> dict:
    For each coin:
    1. Load data:
       - 1m candles via load_candles(coin, '1m', start_ms, end_ms)
       - 5m candles via load_candles(coin, '5m', start_ms, end_ms)
       - hourly funding via load_funding_history(coin, start_ms, end_ms)

    2. Build lookup structures:
       - funding_by_hour: dict mapping hour_timestamp → funding_rate
         (key = timestamp floored to the hour)
       - candles_5m_by_time: dict mapping 5m bar open_time → row

    3. Simulate 1-min bars (iterate over 1m candles in order):
       NOTE: Gate 2 is skipped — Hyperliquid historical OI is not available.

       At each 1m bar (index i, timestamp t):

       a. Append bar.close to price_series (deque maxlen=245).
          This is the markPx proxy.

       b. Build high_series_5m and close_series_5m (deque maxlen=24):
          Every 5 bars (i % 5 == 4), look up the completed 5m candle whose
          open_time == t - 4*60*1000. Append its high and close.
          Use the 5m candle that covers bars [i-4 .. i].

       c. Build funding_series (deque maxlen=48):
          At each bar, look up funding_by_hour[floor(t, hour)].
          Append only when the hour changes (i.e., append once per hour).

       d. Build premium_series (deque maxlen=12):
          Same cadence as funding — append once per hour from funding DataFrame.

       e. Gate evaluation (no look-ahead — use only series as populated above):
          - gate1 = gate1_passes(funding_series, premium_series)
          - gate3 = gate3_score(price_series, high_series_5m, close_series_5m,
                                vwap_5m=0.0)  # VWAP not available in backtest
          - if not (gate1 and gate3 >= 2): continue

       f. Skip if already in a position for this coin.

       g. Entry:
          entry_px = apply_entry_slippage(bar.close)
          atr_14   = compute_atr(high_series_5m, low_series_5m, close_series_5m)
          swing_high = max(list(price_series)[-15:]) if len(price_series) >= 15
                       else bar.close
          stop_distance_pct = calculate_stop_distance(
              entry_px, atr_14, swing_high, high_volatility=False)
          size_usd  = calculate_position_size(
              initial_equity, 'NORMAL', squeeze_score=0, stop_distance_pct)
          size_coins = size_usd / entry_px
          stop_px    = entry_px * (1 + stop_distance_pct)
          tp1_px     = entry_px * (1 - TP1_R_TARGET * stop_distance_pct)
          tp2_px     = entry_px * (1 - TP2_R_TARGET * stop_distance_pct)
          funding_collected_usd = 0.0
          Record open position: {entry_px, size_coins, stop_px, tp1_px, tp2_px,
                                  entry_time: t, stop_distance_pct,
                                  tp1_closed: False}

       h. If in position, check exit conditions on each subsequent bar:
          current_px = bar.close
          hour_key   = floor(t, hour)
          funding_collected_usd += funding_by_hour.get(hour_key, 0.0)
                                   * size_coins * current_px
          (positive funding = shorts receive)

          TP1: if not tp1_closed and current_px <= tp1_px:
               exit half position at apply_exit_slippage(current_px)
               tp1_closed = True; size_coins *= 0.5

          TP2 / stop:
          if current_px >= stop_px:
               exit at apply_exit_slippage(current_px); record trade; close position
          elif current_px <= tp2_px:
               exit at apply_exit_slippage(current_px); record trade; close position

          Funding exit: if check_funding_exit(funding_by_hour.get(hour_key, 0.0),
                                               pnl_r):
               exit at apply_exit_slippage(current_px); record trade; close position

    4. If still in position at end of data: force-close at last bar close.

    5. compute_metrics(trades, initial_equity) → metrics dict for this coin.

    Return {"per_coin": {coin: metrics}, "aggregate": compute_metrics(all_trades, initial_equity)}

─────────────────────────────────────────────────────────────────────
Implement `scripts/bootstrap.py`:
─────────────────────────────────────────────────────────────────────

  CLI entrypoint for running the backtester.
  Arguments: --coins ETH,SOL,... --start 2024-01-01 --end 2024-12-31
             --equity 10000 (optional, default 10000)
  Prints a metrics table per coin and aggregate row.
  Saves trades to logs/backtest_trades_{timestamp}.csv.

─────────────────────────────────────────────────────────────────────
Write unit tests in `tests/unit/test_backtest.py`:
─────────────────────────────────────────────────────────────────────

- apply_entry_slippage: fill < mid (worse for short)
- apply_exit_slippage: fill > mid (worse for cover)
- compute_metrics: correct win_rate, total_pnl_pct, max_drawdown on
  a synthetic 3-trade list (one winner, one loser, one breakeven)
- compute_metrics: returns sharpe=0.0 when fewer than 2 trades
- compute_metrics: max_drawdown is peak-to-trough of cumulative pnl curve
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

| Stage                                | PRD Sections                  |
| ------------------------------------ | ----------------------------- |
| 1 — Helpers                          | 3.1, 3.2, 3.3, 3.4            |
| 2 — VwapBuffer, DeltaAggregator      | 3.4, 3.5, 7.1                 |
| 3 — LiquidationModel                 | 5.1, 5.2, 5.3                 |
| 4 — Data Ingestion                   | 2.6 (Rules + Path A + Path B) |
| 5 — Gates 1, 2, 3                    | 4.1, 4.2, 4.3, 4.4            |
| 6 — Regime Filter                    | 6                             |
| 7 — Trigger Engine + Message Handler | 7.1, 7.2, 7.3                 |
| 8 — IOC Execution                    | 8.1, 8.2, 8.3, 8.4            |
| 9 — Risk Engine                      | 9.1–9.7                       |
| 10 — OMS Core                        | 2.9, 2.10, 2.11               |
| 11 — WS Manager                      | 14                            |
| 12 — Main Loop                       | 2.8, 2.3                      |
| 13 — Backtester                      | 11                            |
