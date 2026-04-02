# ALTCOIN SHORTBOT — Production System PRD
**Version 3.0 · Platform: Hyperliquid · Language: Python 3.11+**
*LLM-Optimised Reference Document — All thresholds are concrete and codeable*
*v3.9 final polish — seed comment clarified (close as historical markPx approximation); Gate 3 "4h high" renamed to "4h max sampled mark price"; primary IOC fill-rate expectation documented*

---

| Field | Value |
|---|---|
| Document Type | Production PRD — LLM-Ready |
| Strategy Class | Automated Short-Selling on Perpetuals |
| Exchange | Hyperliquid (native API) |
| Language & Runtime | Python 3.11+ / asyncio |
| Backtesting Framework | Custom vectorised pandas (NOT Freqtrade) |
| Target Latency | 50–300 ms (reaction-based, not HFT) |
| Capital Per Trade | $100–$250 (paper → live scaling) |
| Max Concurrent Positions | 3–5 |

---

## Table of Contents

1. [Core Philosophy](#1-core-philosophy)
2. [System Architecture](#2-system-architecture)
3. [Helper Functions and Shared Classes](#3-helper-functions-and-shared-classes)
4. [Scanner Layer — Three-Gate Filter](#4-scanner-layer--three-gate-filter)
5. [Liquidation Intelligence Layer](#5-liquidation-intelligence-layer)
6. [Regime Filter](#6-regime-filter)
7. [Trigger Layer](#7-trigger-layer)
8. [Execution Engine](#8-execution-engine)
9. [Risk Engine](#9-risk-engine)
10. [Asset Universe](#10-asset-universe)
11. [Backtesting Specification](#11-backtesting-specification)
12. [Deployment Plan](#12-deployment-plan)
13. [Known Failure Modes](#13-known-failure-modes)
14. [WebSocket Reconnection Protocol](#14-websocket-reconnection-protocol)
15. [LLM Implementation Guide](#15-llm-implementation-guide)

---

## 1. Core Philosophy

This system is **not** a price-prediction engine. It is a **market-structure breakdown detector**. The bot enters short positions only when the structural conditions holding price up are measurably deteriorating. Every module, threshold, and rule follows from this principle.

| Principle | Implication |
|---|---|
| Reaction > Prediction | No forecast of where price goes. Only react when breakdown is confirmed. |
| Liquidations > Indicators | Liquidation mechanics do the work. Lagging indicators are confirmation only. |
| Execution > Complexity | A simple, fast, reliable system beats a complex slow one. |
| Survival > Returns | Capital preservation is primary. All risk rules are hard, not soft. |
| Sequential Gates > Composite Scores | Pass/fail gates eliminate false precision of weighted scoring. |

---

## 2. System Architecture

### 2.1 Architecture Goal

The system is a stateful, event-driven short-selling platform for Hyperliquid — not a monolithic loop that scans, decides, and trades in one process. The architecture separates slow market selection from fast trigger detection and from order management. This matches the exchange interface: universe-wide data comes from `allMids` and `metaAndAssetCtxs`; per-coin fast paths from `trades`, `l2Book`, `candle`, and `activeAssetCtx`; user truth from `orderUpdates`, `userEvents`, `userFills`, and account-state endpoints.

---

### 2.2 Design Principles

**P1 — Slow filter, fast trigger.**
Universe selection runs on a coarse cadence using REST endpoints. Trigger logic runs only on a small watch list using WebSocket market data. No full-universe trigger logic runs in the hot path.

**P2 — Single writer for orders.**
All signed exchange actions flow through exactly one Order Management Service (OMS) per trading process. Hyperliquid stores nonces per signer and recommends one API wallet per process, a batching task every 100 ms, and an atomic nonce counter.

**P3 — Exchange-native protections first.**
Protective behaviour uses exchange-native primitives: IOC/GTC/ALO order behaviour, trigger orders for TP/SL, `modify` / `batchModify` for order updates, and `scheduleCancel` as the primary dead-man switch.

**P4 — Local state is authoritative between reconciliations.**
The live engine maintains local book, signal, order, and position state in memory and reconciles against exchange endpoints after reconnects or restarts. Hyperliquid explicitly recommends constructing exchange state locally for latency-sensitive workflows.

**P5 — Precision rules are exchange rules.**
Price and size formatting must be validated against Hyperliquid's tick/lot rules before signing. Prices may have up to 5 significant figures and no more than `MAX_DECIMALS - szDecimals` decimal places for perps; sizes are rounded to `szDecimals`.

---

### 2.3 Service Topology

The system is split into four services.

**A. Market Data Service**

Ingests all public market data.

| Input | Scope | Cadence |
|---|---|---|
| `allMids` | Full universe price awareness | Every block ~2s |
| `metaAndAssetCtxs` | Universe-wide context scans | Every 30s |
| `trades`, `candle`, `activeAssetCtx` | **Warm-up candidates** (passed Gates 1+2, warming up for Gate 3) AND active watch-list | Tick / ~1s |
| `l2Book` | Active watch-list only (trigger confirmation) | Tick |
| REST `fundingHistory` | All scan-eligible coins | Startup (staggered bootstrap) + hourly refresh + per-coin reconnect |
| REST `candleSnapshot` | New warm-up candidates (seed) + full universe 1h closes | On seed + every 60 min |

There are three asset tiers with different subscription sets:

| Tier | Criteria | Subscriptions |
|---|---|---|
| **Universe** | All liquid perps | `allMids` (one subscription), `metaAndAssetCtxs` REST |
| **Warm-up candidates** | Passed Gates 1+2, not yet Gate3-evaluated | `trades`, `candle`, `activeAssetCtx` WS; `candleSnapshot` REST seed |
| **Active watch list** | Passed all three gates | All warm-up subscriptions + `l2Book` |

Responsibilities: maintain universe-level snapshots; maintain per-coin live state for warm-up and watch-list assets; normalise WebSocket and REST payloads into a common internal model; enforce subscription and connection budgets.

> **Exchange limits:** 10 WebSocket connections per IP, 1,000 subscriptions, 2,000 messages per minute across WebSocket connections, 100 simultaneous inflight WebSocket post messages.

**B. Strategy Service**

Runs market selection and trigger generation. Produces trade intents — never direct exchange calls.

- Run the slow crowding scanner (Gates 1–3)
- Maintain the active watch list
- Evaluate trigger conditions only for active watch-list assets (not warm-up candidates)
- Emit entry/exit intents to OMS

This service is stateless with respect to exchange-side nonces. It may request an entry or exit but must not sign or send orders. This isolates strategy logic from signer state, motivated by Hyperliquid's signer-specific nonce model.

**C. Order Management Service (OMS)**

Owns every signed action sent to Hyperliquid.

- Own one API wallet per process
- Own the atomic nonce counter
- Batch outbound actions every 100 ms
- Separate ALO-only batches from IOC/GTC batches (validator prioritisation differs)
- Place, modify, cancel, and schedule-cancel orders
- Attach TP/SL triggers after entry fills

**D. Reconciliation and Risk Service**

Handles truth recovery, kill conditions, and portfolio controls.

- Reconcile local orders against exchange state after reconnects
- Maintain open-position truth
- Track fills, realised PnL, and funding
- Enforce daily kill switches, sector caps, and stale-state blocks
- Refresh `scheduleCancel` (the exchange-native dead-man switch)

---

### 2.4 Module Responsibilities

| Module | Service | Responsibility |
|---|---|---|
| Universe Snapshotter | Market Data | Poll `metaAndAssetCtxs` every 30s; bootstrap `fundingHistory` for all scan-eligible coins at startup; refresh funding hourly; poll hourly candles for regime filter |
| Tiered Streamer | Market Data | Manage per-tier WS subscriptions: warmup feeds (trades/candle/activeAssetCtx) for Gate12 candidates; watchlist feeds (+ l2Book) for active watch-list assets; unsubscribe on demotion |
| State Normaliser | Market Data | Convert exchange payloads into typed internal records |
| Crowding Scanner | Strategy | Apply funding / OI / premium gates (Sections 4–5) |
| Regime Filter | Strategy | Apply BTC and market-breadth gating (Section 6) |
| Trigger Engine | Strategy | Detect tape shift, bid depletion, VWAP loss, structure break (Section 7) |
| Intent Router | Strategy | Send entry/exit intents to OMS — never signed orders |
| Nonce Manager | OMS | Monotonic atomic nonce assignment per signer |
| Batch Scheduler | OMS | Batch and prioritise ALO vs IOC/GTC action sets |
| Execution Adapter | OMS | Build exchange actions: order, modify, batchModify, cancel, trigger orders |
| Protection Manager | OMS | Maintain TP/SL orders and `scheduleCancel` |
| Reconciler | Risk | Recover state from user/order endpoints after gaps |
| Portfolio Controller | Risk | Enforce max positions, daily loss, sector limits, stale-data blocks |
| Funding & PnL Ledger | Risk | Record funding carry, closed PnL, and realised trade outcomes |

---

### 2.5 Internal State Model

Each asset maintains four distinct state layers:

| Layer | Cadence | Content |
|---|---|---|
| Universe State | Coarse (30s–60m) | Scanner eligibility data — funding, OI, premium, 1h candles |
| Watchlist State | Fast (tick / 1s) | Live market data for subscribed assets |
| Intent State | On-demand | Latest strategy decision, trigger timestamp, expiry conditions |
| Execution State | On-event | Live orders, pending actions, fills, position size, stop state, reconciliation status |

Every asset state object must include:

```python
{
    # ... market data series (see Section 2.6) ...

    # Liveness
    'last_ws_ts':              0.0,     # last successful WS message timestamp
    'last_reconcile_ts':       0.0,     # last successful reconciliation timestamp
    'has_data_gap':            False,   # blocks entry promotion when True

    # Intent
    'trigger_valid_until':     0.0,     # unix ts after which trigger expires

    # Position
    'position_state':          None,    # None | 'open' | 'closing'
    'pending_action_count':    0,
}
```

If `has_data_gap is True`, no new entry intent may be promoted to OMS.

---

### 2.6 Data Ingestion Rules

**Critical rules — no exceptions:**

**Rule 1 — Numeric field types vary by endpoint; do not assume strings.**
Many REST and WebSocket payloads return numerics as strings, but not all. Apply the table below at ingestion:

| Endpoint / Channel | Fields returned as strings | Fields returned as numbers |
|---|---|---|
| `fundingHistory` REST | `fundingRate`, `premium` | — |
| `metaAndAssetCtxs` REST | `funding`, `openInterest`, `markPx`, `oraclePx`, `midPx`, `dayNtlVlm`, `prevDayPx` | — |
| `activeAssetCtx` WS (`ctx` sub-object) | — | `funding`, `openInterest`, `oraclePx`, `markPx` |
| `trades` WS (`WsTrade`) | `px`, `sz` | `time`, `tid` |
| `candle` WS (`Candle`) | — | `o`, `c`, `h`, `l`, `v`, `t`, `T`, `n` |
| `l2Book` WS (`WsLevel`) | `px`, `sz` | `n` |

The safe implementation strategy is to **always wrap fields in `float()` or `int()` at the ingestion boundary**, even for fields documented as numbers, because the documentation may not reflect all code paths and future schema changes. Never propagate raw strings into series or arithmetic.

**Rule 2 — The `funding` field is the 8-hour basis rate.** Divide by 8 before storing so `funding_series` holds per-hour rates. Multiplying the raw value by 8,760 would overstate APR by 8×. This applies to both the REST `fundingHistory` response and the `activeAssetCtx` WebSocket `ctx.funding` field.

**Rule 3 — Two separate ingestion paths for funding vs OI.** `activeAssetCtx` fires every ~1 second. Naively appending every message to `funding_series` fills it in 48 seconds instead of 48 hours. Gate 1 would read 8 seconds of funding instead of 8 hours; Gate 2 would compare OI 4 minutes apart instead of 4 hours.

**Path A — Funding: REST only, on startup and after every reconnect**

```python
async def refresh_funding_from_rest(coin: str, state: dict) -> None:
    """
    The ONLY function that writes to funding_series.
    Fetches funding history for the last 48 hours. The fundingHistory endpoint
    returns one record per funding interval; Hyperliquid pays funding hourly,
    so this yields up to 48 records. Validate with a live sample if exact
    cadence matters — the docs confirm the endpoint exists and the fields,
    but do not explicitly state the per-hour cadence.
    Never call from a WebSocket message handler.
    """
    payload  = {
        "type": "fundingHistory",
        "coin": coin,
        "startTime": int((time.time() - 48 * 3600) * 1000)
    }
    response = await rest_post("/info", payload)
    state['funding_series'].clear()
    for entry in response[-48:]:
        state['funding_series'].append(float(entry['fundingRate']) / 8)
```

**Path B — OI / price / premium: WebSocket or REST, throttled**

```python
def ingest_asset_ctx(ctx: dict, state: dict, now: float,
                     rest_premium: float | None = None) -> None:
    """
    Receives the PerpsAssetCtx sub-object — NOT the raw WS message.

    For activeAssetCtx WebSocket messages, the caller must extract the ctx
    sub-object before calling this function:
        ingest_asset_ctx(message["data"]["ctx"], state, now)

    For metaAndAssetCtxs REST results, the ctx object is each element of
    the second list in the response (index-aligned with universe metadata).

    Premium sourcing differs by source:

    - metaAndAssetCtxs REST: includes a documented `premium` field (string).
      Use float(ctx['premium']) directly when calling from the REST poll path.
    - activeAssetCtx WS ctx sub-object: does NOT include a `premium` field.
      Derive it as (markPx - oraclePx) / oraclePx for the WS path.

    This function handles both cases via the `rest_premium` parameter.
    Pass `rest_premium=float(ctx['premium'])` from REST callers;
    leave it as None from WS callers and it will be derived.

    Documented PerpsAssetCtx fields used here:
        funding       — 8h basis rate (string on REST, number on WS — always float())
        openInterest  — base units, e.g. ETH for ETH-PERP (same typing note)
        markPx        — mark price
        oraclePx      — oracle spot price

    Throttled: OI + price appended at most once per 60s.
    Oracle premium appended at most once per 5m.
    NEVER writes to funding_series — Path A's exclusive responsibility.
    """
    mark_px   = float(ctx['markPx'])
    oracle_px = float(ctx['oraclePx'])

    if now - state['last_oi_append_ts'] >= 60:
        state['oi_series'].append(float(ctx['openInterest']))
        # price_series semantics: 1-min sampled mark price.
        # markPx is the authoritative source for price_series in all paths.
        # This is an explicit design choice: mark price is consistent between
        # REST (metaAndAssetCtxs) and WS (activeAssetCtx), and approximates
        # the 1m close closely enough for Gate 2 and Gate 3 purposes.
        # Backtest note: use 1m markPx snapshots (not candle closes) to match
        # live series semantics and avoid backtest-to-live divergence.
        state['price_series'].append(mark_px)
        state['last_oi_append_ts'] = now
        update_liq_model_from_candle(state, mark_px, now)

    if now - state['last_premium_append_ts'] >= 300:
        if rest_premium is not None:
            # REST callers (metaAndAssetCtxs): use the documented premium field directly
            oracle_premium = rest_premium
        else:
            # WS callers (activeAssetCtx): derive from markPx and oraclePx
            # The activeAssetCtx ctx sub-object does not expose a premium field
            oracle_premium = (mark_px - oracle_px) / oracle_px if oracle_px > 0 else 0.0
        state['premium_series'].append(oracle_premium)
        state['last_premium_append_ts'] = now
```

---

### 2.7 State Factory

Always use `create_asset_state()`. Never construct state dicts manually — missing fields cause AttributeErrors that are hard to trace at runtime.

```python
from collections import deque

def create_asset_state() -> dict:
    return {
        # Time series — all floats, never raw API strings
        'funding_series':         deque(maxlen=48),    # per-hour rate (8h ÷ 8), 48h rolling
        'oi_series':              deque(maxlen=245),    # 1-min OI, 4h + 5-sample smoothing buffer
        'price_series':           deque(maxlen=245),    # 1-min sampled mark price (markPx from activeAssetCtx/metaAndAssetCtxs)
        'premium_series':         deque(maxlen=12),     # 5-min oracle premium (markPx-oraclePx)/oraclePx

        # 5m candle series (from candle WS subscription)
        'high_series_5m':         deque(maxlen=24),     # 2h of 5m highs
        'low_series_5m':          deque(maxlen=24),     # 2h of 5m lows — needed for ATR
        'close_series_5m':        deque(maxlen=24),     # 2h of 5m closes

        # Trigger state
        'delta_history':          deque(maxlen=10),     # last 10 × 60s delta values
        'trade_delta_60s':        0.0,
        'delta_mean_10m':         0.0,
        'delta_std_10m':          0.0,

        # Order book
        'bid_depth_now':          0.0,
        'bid_depth_t_minus_30s':  0.0,

        # Helper objects — always initialised, never None
        'liq_model':              LiquidationModel(),
        'delta_aggregator':       DeltaAggregator(),
        'vwap_buffer':            VwapBuffer(),

        # Computed
        'squeeze_score':          0,

        # Throttle timestamps
        'last_oi_append_ts':      0.0,
        'last_premium_append_ts': 0.0,

        # Liveness and control
        'last_ws_ts':             0.0,
        'last_reconcile_ts':      0.0,
        'has_data_gap':           False,
        'trigger_valid_until':    0.0,
        'position_state':         None,
        'pending_action_count':   0,
        # is_on_watchlist: convenience flag mirroring external watch list membership.
        # Ownership rule: the main loop sets it True when promote_to_watch_list()
        # returns a coin, and False when the coin is demoted or unsubscribed.
        # Does not drive logic directly — always use the caller's list as source of truth.
        'is_on_watchlist':        False,
        'delta_ready':            False,
        'ws_subscribed_at':        0.0,     # unix ts when WS feeds were subscribed for this coin
    }
```

---

### 2.8 Event Flows

**Universe Selection Flow**
```
metaAndAssetCtxs + fundingHistory  →  run_universe_scanner()  →  Gate12 candidates
                                    →  promote_to_watch_list()  →  Gate 3 (with WS warm-up)
                                    →  Watch List

1h candles (refresh_1h_closes())  →  regime_filter()  (separate 60-min cadence)
```

**Trigger Flow**
```
trades + l2Book + candle + activeAssetCtx
  → Trigger Engine (delta z-score + confirmation)
  → Intent Router
  → OMS (if has_data_gap is False and squeeze_score < 5)
```

**Execution Flow**
```
Intent → OMS validation (nonce, risk, signer)
  → IOC marketable limit (primary)
  → Optional retry IOC if trigger still valid
  → orderUpdates / userFills → update Execution State
  → TP/SL trigger order placed after fill
```

**Recovery Flow**
```
disconnect → has_data_gap = True
  → exponential backoff reconnect
  → resubscribe market-data + user feeds
  → refresh_funding_from_rest()
  → reconcile open orders + recent fills
  → clear has_data_gap
  → re-enable entries
```

The WebSocket server closes connections with no messages for 60 seconds. The client must send `{"method": "ping"}` and handle `pong` explicitly as a liveness control loop.

---

### 2.9 Order Management Policy

**Entry:** Two-step pure-IOC sequence — no resting orders, no timeout windows, no cancel calls. Step 1: passive IOC sell at `mid + LIMIT_ORDER_OFFSET` (priced above the best bid — non-marketable with allMids as the only price reference); the exchange returns the result immediately (filled or not). Step 2 (if unfilled and trigger still valid): aggressive IOC via `place_ioc_aggressive()`, priced `IOC_AGGRESSIVE_SLIPPAGE_PCT` through the book. If both return unfilled, no position is opened on this signal. Follows Hyperliquid's documented order model: all orders are limits with TIF behaviour (`Alo`, `Ioc`, `Gtc`). Rejection state policy: `iocCancelRejected` → benign unfilled, fallback proceeds. `minTradeNtlRejected` → fatal, signal skipped. `tickRejected` → fatal local bug, signal skipped. `oracleRejected` → price too far from oracle, signal skipped. `marketOrderNoLiquidityRejected` on fallback → log and skip.

**Maintenance:** Prefer `modify` → `batchModify` → `cancel` + re-place for live resting orders. Reduces churn and stays close to the exchange's native action model.

**Protection:** Immediately after an entry fill, OMS places a reduce-only stop trigger and optionally a reduce-only take-profit trigger.

`scheduleCancel` is maintained on the account as the **exchange-native dead-man switch** — it cancels open orders if the account stops refreshing. Note: `scheduleCancel` cancels orders; it does not flatten positions. The application-level watchdog (Section 9.7) handles position flattening via `emergency_flatten_all()` if the process itself fails.

---

### 2.10 Nonce, Batching, and Signer Rules

- One API wallet per trading process
- One atomic nonce manager per API wallet
- One batching task per process — target cadence 100 ms
- ALO-only batches separated from IOC/GTC batches
- No signed actions from strategy code — only OMS signs

These follow Hyperliquid's documented recommendations. Nonces are stored per signer; separate signing keys avoid collisions; 100 ms batching is explicitly suggested.

---

### 2.11 Precision and Validation Rules

Before any signed order action leaves OMS:

- Validate price against Hyperliquid's 5-significant-figure rule and the `6 - szDecimals` decimal-place cap
- Validate size against `szDecimals`
- Strip invalid trailing formatting
- Reject any order that would fail tick/lot rules before signing — never let an invalid order reach the exchange

---

### 2.12 Reliability Layers

Three liveness layers are required:

| Layer | Mechanism | Source |
|---|---|---|
| WebSocket liveness | Send `ping` before 60s quiet threshold; handle `pong` | Exchange requirement |
| Order liveness | Refresh `scheduleCancel` periodically | Exchange-native dead-man switch |
| Process liveness | Local watchdog thread for asyncio freeze or stalled reconciliation | Application safeguard (Section 9.7) |

On any reconnect, perform in order: resubscribe feeds → fetch open-order truth → fetch recent fills → refresh account/position state → clear stale pending intents → allow new entries.

---

### 2.13 Storage

The live engine keeps rolling market state in memory. The following must be persisted to durable storage:

- Order intents and signed action metadata
- Exchange acknowledgements and fills
- Funding payments
- Position snapshots
- Daily loss state
- Reconciliation checkpoints

---

### 2.14 Out of Scope for v1

- Multi-process shared signer execution
- Cross-exchange arbitrage
- Fully passive market making
- Portfolio-margin complexity
- Social or on-chain sentiment ingestion
- Node-based local book reconstruction (lowest-latency path — later optimisation)


## 3. Helper Functions and Shared Classes

Define all of these before implementing any gate or module.

### 3.1 EMA

```python
def ema(closes: list, period: int) -> list:
    """
    Returns a list the same length as the input.
    result[-1] = latest value. result[-6] = 5 periods ago.
    Used by: regime_filter() (Section 6).
    """
    if not closes:
        return []
    k = 2 / (period + 1)
    result = [closes[0]]
    for price in closes[1:]:
        result.append(price * k + result[-1] * (1 - k))
    return result
```

### 3.2 VWAP

```python
def compute_vwap(trades: list) -> float:
    """
    trades: list of (price: float, volume_usd: float) tuples.
    Returns 0.0 if empty.
    Used by: VwapBuffer.get_vwap(), trigger confirmation (Section 7).
    """
    total_vol = sum(v for _, v in trades)
    if total_vol == 0:
        return 0.0
    return sum(p * v for p, v in trades) / total_vol
```

### 3.3 ATR

```python
def compute_atr(high_series: deque, low_series: deque,
                close_series: deque, period: int = 14) -> float:
    """
    Average True Range on 5m candles.
    Reads high_series_5m, low_series_5m, close_series_5m from state.
    Requires at least period+1 candles. Returns 0.0 if insufficient data.
    Used by: calculate_stop_distance() (Section 9.1).
    """
    highs  = list(high_series)
    lows   = list(low_series)
    closes = list(close_series)

    if len(closes) < period + 1:
        return 0.0

    true_ranges = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i]  - closes[i-1])
        )
        true_ranges.append(tr)

    return sum(true_ranges[-period:]) / period
```

### 3.4 Price Formatter

Hyperliquid perp prices must obey two rules simultaneously:
- At most **5 significant figures**
- At most **`6 - szDecimals` decimal places**

These are independent constraints — both must be satisfied. `round()` to `sz_decimals` alone is wrong: it satisfies only the decimal-place constraint and ignores the significant-figure cap.

```python
import math
from decimal import Decimal, ROUND_DOWN

def format_price(price: float, sz_decimals: int) -> str:
    """
    Format a price as a canonical string for Hyperliquid order signing.

    Returns a STRING, not a float. Prices must be strings when submitted
    to the exchange action schema. Using a Python float risks trailing
    representation noise (e.g. 1234.5000000000002) which would fail
    tick/lot validation or produce an unexpected order price after JSON
    serialisation.

    Rules applied (both must hold; stricter wins):
      1. At most 5 significant figures
      2. At most (6 - sz_decimals) decimal places

    Trailing zeros are stripped per exchange canonical format.
    Raises ValueError if price <= 0.

    sz_decimals: from metaAndAssetCtxs universe metadata.

    Examples (sz_decimals=2):
      format_price(12345.678, 2) -> '12345'       (5 sig figs, 0 dp; ROUND_DOWN truncates .678)
      format_price(1.23456,   2) -> '1.2346'      (5 sig figs, 4 dp)
      format_price(0.001234,  2) -> '0.0012'      (4 dp cap from 6-2=4; 0.001234 truncates to 0.0012)
    """
    if price <= 0:
        raise ValueError(f'price must be positive, got {price}')

    # Constraint 1: 5 significant figures → decimal places allowed
    magnitude          = int(math.floor(math.log10(abs(price))))
    sig_fig_decimals   = max(0, 5 - magnitude - 1)

    # Constraint 2: 6 - sz_decimals decimal places
    max_decimal_places = max(0, 6 - sz_decimals)

    # Stricter constraint wins
    decimals = min(sig_fig_decimals, max_decimal_places)

    # Round using Decimal for exact representation, then strip trailing zeros
    quantizer = Decimal(10) ** -decimals
    rounded   = Decimal(str(price)).quantize(quantizer, rounding=ROUND_DOWN)
    canonical = str(rounded.normalize())

    # normalize() may produce scientific notation for very small numbers;
    # convert back to fixed-point if needed
    if 'E' in canonical or 'e' in canonical:
        canonical = f'{rounded:f}'

    return canonical
```

`format_price()` returns a string. Pass it directly to the order signing layer — do not convert back to float. Use it for every price that leaves OMS: entry limits, stop triggers, take-profit triggers.

### 3.5 VwapBuffer

```python
class VwapBuffer:
    """
    Rolling 5-minute VWAP from live trade ticks.
    One instance per watch-list coin, stored in state['vwap_buffer'].
    Fed by handle_message() on every trades WS message.
    """
    WINDOW_S = 300    # VWAP_BUFFER_WINDOW_S

    def __init__(self):
        self._trades: list = []    # (timestamp, price, volume_usd)

    def on_trade(self, price: float, size_base: float, now: float) -> None:
        self._trades.append((now, price, size_base * price))
        cutoff = now - self.WINDOW_S
        self._trades = [(t, p, v) for t, p, v in self._trades if t >= cutoff]

    def get_vwap(self) -> float:
        if not self._trades:
            return 0.0
        return compute_vwap([(p, v) for _, p, v in self._trades])
```

### 3.6 DeltaAggregator

```python
class DeltaAggregator:
    """
    Aggregates tick-level trades into 60-second net sell volume deltas,
    then calls update_delta_state() once per completed window.
    One instance per watch-list coin, stored in state['delta_aggregator'].

    Hyperliquid trade WS 'side' field:
      'A' = ask aggressor = taker SELL
      'B' = bid aggressor = taker BUY
    """
    WINDOW_S = 60    # DELTA_WINDOW_S

    def __init__(self):
        self.sell_vol_usd = 0.0
        self.buy_vol_usd  = 0.0
        self.window_start = time.time()

    def on_trade(self, side: str, size_base: float, price: float) -> None:
        usd = size_base * price
        if side == 'A':
            self.sell_vol_usd += usd
        elif side == 'B':
            self.buy_vol_usd  += usd

    def flush_if_ready(self, state: dict, now: float) -> bool:
        """Call after every trade tick. Returns True when a window is flushed."""
        if now - self.window_start >= self.WINDOW_S:
            update_delta_state(state, self.sell_vol_usd - self.buy_vol_usd)
            self.sell_vol_usd = 0.0
            self.buy_vol_usd  = 0.0
            self.window_start = now
            return True
        return False
```

---

## 4. Scanner Layer — Three-Gate Filter

Runs every 30 seconds. Assets passing all three gates join the Active Watch List for trigger monitoring.

### 4.1 Universe Scanner Entry Point

The scanner runs in two stages every 30 seconds.

**Stage 1 — `run_universe_scanner()`:** Gates 1 and 2 on the full universe using REST data only. Returns Gate12 candidates. Gate 3 is intentionally excluded because it requires live WebSocket data (`vwap_buffer`, `high_series_5m`, `close_series_5m`) that is only available after a coin has active WS subscriptions and a warm-up period.

**Stage 2 — `promote_to_watch_list()`:** Gate 3 for each candidate. For new candidates, `seed_gate3_series_from_rest()` fetches the last 24 × 5m candles and last 245 × 1m closes from `candleSnapshot`, seeding the candle and price series so Gate 3 conditions 1 and 3 are evaluable immediately. WS subscriptions are then triggered and the coin enters a warm-up window (`GATE3_WARM_UP_S = 360s`) sized to cover the `VwapBuffer` fill time (VWAP window = 300s). After warm-up, all three Gate 3 conditions can be evaluated. Coins already on the watch list are re-evaluated every cycle using live data.

`cached_1h_closes` is the output of `refresh_1h_closes()` (refreshed every 60 minutes) and is used by `regime_filter()` directly — it is not a parameter of either scanner function.

```python
# On first subscription, seed_gate3_series_from_rest() is called to pre-populate
# high_series_5m, close_series_5m, low_series_5m, and price_series from REST
# candleSnapshot. After seeding, the only missing data is the live VwapBuffer,
# whose window is VWAP_BUFFER_WINDOW_S = 300s.
# GATE3_WARM_UP_S covers the VWAP window plus a small buffer.
GATE3_WARM_UP_S = 360    # 6 minutes — covers VWAP_BUFFER_WINDOW_S (300s) + 1 min buffer


async def bootstrap_universe_funding(universe_coins: list,
                                    all_states: dict) -> None:
    """
    Called ONCE at startup (and repeated every FUNDING_REFRESH_INTERVAL_S = 3600s).

    Hydrates funding_series for ALL scan-eligible coins so Gate 1 can evaluate
    them correctly in run_universe_scanner(). Without this, Gate 1 fails for
    any coin whose funding_series is empty — not because funding conditions failed,
    but simply because the data was never loaded.

    Requests are staggered by FUNDING_BOOTSTRAP_STAGGER_S to avoid hitting the
    info endpoint with a burst of parallel requests. The fundingHistory endpoint
    carries additional response-weight cost; staggering keeps the system within
    documented rate limits.
    """
    log(f'Bootstrapping fundingHistory for {len(universe_coins)} coins...')
    for i, coin in enumerate(universe_coins):
        state = all_states[coin]
        await refresh_funding_from_rest(coin, state)
        if i < len(universe_coins) - 1:
            await asyncio.sleep(FUNDING_BOOTSTRAP_STAGGER_S)
    log('Funding bootstrap complete')


async def run_universe_scanner(universe_coins: list,
                                all_states: dict) -> list:
    """
    STAGE 1 of 2 — called every 30 seconds by the main loop.

    Applies Gates 1 and 2 to the full universe using REST data only.
    Returns the list of coins that pass both gates (Gate12 candidates).

    Gate 3 is NOT applied here. It requires live WebSocket data
    (vwap_buffer, high_series_5m, close_series_5m) that is only
    available after a coin has active WS subscriptions and a warm-up
    period. Gate 3 is applied in promote_to_watch_list() (Stage 2).

    cached_1h_closes is used only by regime_filter(); pass it there
    directly — it is not needed inside this function.
    """
    response         = await rest_post("/info", {"type": "metaAndAssetCtxs"})
    meta, asset_ctxs = response[0], response[1]
    now              = time.time()
    gate12_candidates = []

    for i, ctx in enumerate(asset_ctxs):
        coin = meta['universe'][i]['name']
        if coin not in universe_coins:
            continue
        state = all_states[coin]
        if state['has_data_gap']:
            continue

        # REST path: metaAndAssetCtxs includes a documented 'premium' field
        rest_premium = float(ctx['premium']) if 'premium' in ctx else None
        ingest_asset_ctx(ctx, state, now, rest_premium=rest_premium)

        if not gate1_passes(state['funding_series'], state['premium_series']):
            continue
        if not gate2_passes(state['oi_series'], state['price_series']):
            continue

        gate12_candidates.append(coin)

    return gate12_candidates


async def seed_gate3_series_from_rest(coin: str, state: dict) -> None:
    """
    Fetches historical candle data from REST to pre-populate the Gate 3 series.
    Called once when a coin first becomes a Gate12 candidate, before WS subscription.

    Seeds:
      high_series_5m / low_series_5m / close_series_5m  — from 5m candleSnapshot (last 24 candles = 2h)
      price_series                                        — from 1m candleSnapshot (last 245 candles ~4h)

    After seeding, only the VwapBuffer needs live trade data.
    The warm-up period (GATE3_WARM_UP_S) is sized to cover the VWAP window.
    """
    now_ms = int(time.time() * 1000)

    # Seed 5m candles — needed by failed_breakout_detected() (24 candles) and
    # the "within 1% of 4h high" condition (uses price_series, not 5m series directly).
    payload_5m = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": "5m",
                "startTime": now_ms - 24 * 5 * 60 * 1000,
                "endTime":   now_ms}
    }
    candles_5m = await rest_post("/info", payload_5m)
    for c in candles_5m[-24:]:
        state['high_series_5m'].append(float(c['h']))
        state['low_series_5m'].append(float(c['l']))
        state['close_series_5m'].append(float(c['c']))

    # Seed price_series (1m sampled mark price) from 1m candleSnapshot.
    # price_series semantics = 1m sampled markPx. The 1m candle close ('c') is
    # used here as the best available REST proxy for markPx at each 1m boundary.
    # Live updates come from ingest_asset_ctx() which appends actual markPx.
    # This is consistent with the design choice documented in ingest_asset_ctx().
    payload_1m = {
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": "1m",
                "startTime": now_ms - 245 * 60 * 1000,
                "endTime":   now_ms}
    }
    candles_1m = await rest_post("/info", payload_1m)
    for c in candles_1m[-245:]:
        state['price_series'].append(float(c['c']))    # 1m candle close used as historical markPx approximation;
                                                            # small discrepancies are expected and acceptable

    log(f'{coin}: seeded {len(state["high_series_5m"])} 5m candles and '
        f'{len(state["price_series"])} 1m mark-price points from REST')


async def promote_to_watch_list(gate12_candidates: list,
                                current_watch_list: list,
                                all_states: dict,
                                now: float) -> list:
    """
    STAGE 2 of 2 — called immediately after run_universe_scanner().

    For each Gate12 candidate:

    NEW candidate (not yet on watch list, ws_subscribed_at == 0):
      1. Call seed_gate3_series_from_rest() to pre-populate price_series,
         high/low/close_series_5m from REST candleSnapshot. This makes
         Gate 3 conditions 1 and 3 (price-vs-4h-high, failed_breakout_detected)
         evaluable once warm-up completes.
      2. Subscribe WS feeds (caller's responsibility — set ws_subscribed_at = now).
      3. Enter warm-up period (GATE3_WARM_UP_S = 360s). During this time the
         VwapBuffer accumulates live trades. Gate 3 is not evaluated.

    WARMING UP (ws_subscribed_at > 0, elapsed < GATE3_WARM_UP_S):
      Skip this cycle. VwapBuffer is still filling.

    WARM-UP COMPLETE or ALREADY ON WATCH LIST:
      Evaluate Gate 3 with live VwapBuffer + seeded/live candle series.

    Coins currently on the watch list that are NOT in gate12_candidates are
    removed (they failed Gate 1 or Gate 2 on re-evaluation).

    Returns the new watch list.
    """
    new_watch_list = []

    for coin in gate12_candidates:
        state = all_states[coin]

        if coin not in current_watch_list:
            if state.get('ws_subscribed_at', 0) == 0:
                # First time this coin has passed Gates 1+2.
                # Seed historical candle data so Gate 3 is evaluable after warm-up.
                await seed_gate3_series_from_rest(coin, state)
                # Caller subscribes WS feeds; record the subscription timestamp.
                state['ws_subscribed_at'] = now
                log(f'{coin} seeded + WS subscribed — warming up {GATE3_WARM_UP_S}s for VWAP')
                continue    # do not evaluate Gate 3 yet

            elapsed = now - state['ws_subscribed_at']
            if elapsed < GATE3_WARM_UP_S:
                log(f'{coin} warming up: {elapsed:.0f}s / {GATE3_WARM_UP_S}s')
                continue

        # Evaluate Gate 3: price_series seeded from REST + vwap from live WS trades
        vwap_5m = state['vwap_buffer'].get_vwap()
        score   = gate3_score(
            state['price_series'],
            state['high_series_5m'],
            state['close_series_5m'],
            vwap_5m
        )
        if score >= 2:
            new_watch_list.append(coin)
        else:
            log(f'{coin} Gate 3 score={score} — not promoted')
            # Caller must call reset_warmup_state(coin, state) and
            # unsubscribe_warmup_feeds(ws, coin) to reclaim subscription slots.

    return new_watch_list
```

```python
def reset_warmup_state(coin: str, state: dict) -> None:
    """
    Clears all warm-up state for a coin that failed Gate 3 or dropped out of
    Gates 1+2 during warm-up. Must be called before unsubscribe_warmup_feeds().

    Clears:
      - ws_subscribed_at (so the coin re-enters full warm-up if it qualifies again)
      - Seeded series: price_series, high/low/close_series_5m
      - VwapBuffer (so stale trades do not persist)
      - delta state (delta_aggregator, delta_history, delta_ready)
      - watch_list flag

    Does NOT clear funding_series or oi_series — those are REST-populated
    and remain valid regardless of warm-up status.
    """
    state['ws_subscribed_at']  = 0.0
    state['is_on_watchlist']   = False
    state['delta_ready']       = False

    # Clear live-data series so stale seeded data does not carry forward
    for series_key in ('price_series', 'high_series_5m', 'low_series_5m',
                        'close_series_5m'):
        state[series_key].clear()

    # Reset VwapBuffer and DeltaAggregator to empty state
    state['vwap_buffer']       = VwapBuffer()
    state['delta_aggregator']  = DeltaAggregator()
    state['delta_history'].clear()
    state['trade_delta_60s']   = 0.0
    state['delta_mean_10m']    = 0.0
    state['delta_std_10m']     = 0.0

    log(f'{coin}: warm-up state reset — will re-enter warm-up if Gates 1+2 pass again')
```

**Lifecycle rule:** The main loop calls `reset_warmup_state()` and `unsubscribe_warmup_feeds()` for any coin that:
- Fails Gate 3 after completing warm-up
- Fails Gate 1 or Gate 2 while in warm-up (before reaching active watch list)
- Is removed from the active watch list (additionally call `unsubscribe_watchlist_feeds()`)

This prevents subscription leaks and stale series accumulation within Hyperliquid's hard websocket limits.

---

### 4.2 Gate 1 — Funding Pressure `[REQUIRED — Pass/Fail]`

**PASS if:** annualised funding > 50% APR **AND** positive for ≥ 6 of last 8 hourly readings **AND** premium > 0.02%.

`funding_series` holds per-hour rates (÷8 applied at ingestion). Multiply by 8,760 to annualise. At 50% APR, longs pay ~$5.71/day per $10k — extreme enough to signal genuine crowding. The 6-of-8 rule filters single-hour spikes. The 0.02% premium floor confirms the perp is genuinely bid above spot oracle.

```python
def gate1_passes(funding_series: deque, premium_series: deque) -> bool:
    """
    funding_series: per-hour rates (÷8 at ingestion, REST only via refresh_funding_from_rest).
    premium_series: derived oracle premium — (markPx - oraclePx) / oraclePx,
                    computed in ingest_asset_ctx(). Not a raw API field.
    """
    recent_8h = list(funding_series)[-8:]
    if len(recent_8h) < 8:
        return False

    annualised     = recent_8h[-1] * GATE1_ANNUALISE_MULTIPLIER    # per-hour * 8760
    positive_count = sum(1 for f in recent_8h if f > 0)
    current_premium = premium_series[-1] if premium_series else 0.0

    return (annualised > GATE1_FUNDING_APR_THRESHOLD and
            positive_count >= GATE1_MIN_POSITIVE_HOURS and
            current_premium > GATE1_PREMIUM_FLOOR)
```

---

### 4.3 Gate 2 — OI Divergence `[REQUIRED — Pass/Fail]`

**PASS if:** OI up > 5% over last 4 hours **AND** price moved < ±0.5% over the same window.

New leveraged longs entering but failing to push price higher — the ceiling is close. The minimum length is 245 (240 lookback + 5 smoothing buffer). 5-minute rolling averages on both ends filter liquidation-driven OI spikes.

```python
def gate2_passes(oi_series: deque, price_series: deque) -> bool:
    if len(oi_series) < 245 or len(price_series) < 240:
        return False

    oi_now_window = list(oi_series)[-5:]
    oi_4h_window  = list(oi_series)[-240:-235]

    if len(oi_4h_window) < 5:
        return False

    oi_now    = sum(oi_now_window) / 5
    oi_4h     = sum(oi_4h_window)  / 5
    oi_change = (oi_now - oi_4h) / oi_4h
    px_change = abs((price_series[-1] - price_series[-240]) / price_series[-240])

    return oi_change > GATE2_OI_CHANGE_THRESHOLD and px_change < GATE2_PRICE_CHANGE_MAX
```

---

### 4.4 Gate 3 — Price Structure `[SCORED 0–3, requires ≥ 2]`

| Condition | Score | Logic |
|---|---|---|
| Price within 1% of 4h max sampled mark price | +1 | Near recent peak — maximum long exposure |
| Price below 5m VWAP | +1 | Intraday sellers already in control |
| Failed breakout in last 2h | +1 | Structural exhaustion confirmed |

```python
def gate3_score(price_series: deque,
                high_series_5m: deque,
                close_series_5m: deque,
                vwap_5m: float) -> int:
    """vwap_5m: state['vwap_buffer'].get_vwap()"""
    prices = list(price_series)
    if not prices:
        return 0

    score         = 0
    current_price = prices[-1]

    if len(prices) >= 240:
        # 4h max of sampled mark prices (price_series = 1m markPx samples, not candle highs)
        high_4h = max(prices[-240:])
        if high_4h > 0 and (high_4h - current_price) / high_4h < GATE3_PRICE_FROM_HIGH_MAX:
            score += 1

    if vwap_5m > 0 and current_price < vwap_5m:
        score += 1

    if failed_breakout_detected(high_series_5m, close_series_5m):
        score += 1

    return score


def failed_breakout_detected(high_series_5m: deque,
                              close_series_5m: deque,
                              lookback: int = 24) -> bool:
    """
    24 × 5m candles = 2h. Detects: new N-candle high formed, then price closed
    >0.5% below that peak, with the peak not in the last 3 candles.
    """
    highs  = list(high_series_5m)[-lookback:]
    closes = list(close_series_5m)[-lookback:]
    if len(highs) < lookback:
        return False

    peak_idx = highs.index(max(highs))
    if peak_idx >= lookback - 3:
        return False

    return (highs[peak_idx] - closes[-1]) / highs[peak_idx] > FAILED_BREAKOUT_RECOVERY_THRESHOLD
```

A coin passing Gate 3 ≥ 2 joins the Active Watch List via `promote_to_watch_list()`. For new candidates: REST data is seeded via `seed_gate3_series_from_rest()` on first subscription, then a `GATE3_WARM_UP_S = 360s` warm-up allows the VwapBuffer to fill before Gate 3 is first evaluated. Existing watch-list coins are re-evaluated every 30 seconds and removed if Gate 1 or Gate 2 fails.

---

### 4.5 Excluded Signals (v1)

| Signal | Reason |
|---|---|
| Social velocity (X / Telegram) | Expensive; legally murky scraping; slower than price |
| On-chain exchange inflows | 30–60 min lag; duplicates funding + OI signal |
| RSI divergence alone | Confirmation only — captured indirectly via Gate 3 |

---

## 5. Liquidation Intelligence Layer

The most critical safety module. Entering a short into a pending squeeze is the primary failure mode of short-selling bots. This layer must block that scenario before any trigger fires.

### 5.1 LiquidationModel

Always initialised in `create_asset_state()` — never `None`.

```python
from collections import deque

class LiquidationModel:
    """
    Reconstructs estimated liquidation price levels from OI changes + candle direction.
    Assumes 10x average leverage (LIQ_MODEL_AVG_LEVERAGE).
    Bounded to 1440 entries per side (24h of 1-min candles) to prevent memory growth.
    """
    MAX_ENTRIES = 1440

    def __init__(self):
        # Each entry: (liq_price, notional_usd, unix_timestamp)
        self.long_entries  = deque(maxlen=self.MAX_ENTRIES)
        self.short_entries = deque(maxlen=self.MAX_ENTRIES)

    def update(self, prev_oi: float, curr_oi: float,
               candle_open: float, candle_close: float,
               notional: float, timestamp: float) -> None:
        """
        Called once per 1-min candle when OI increased.
        notional = delta_oi * mark_price (USD). See update_liq_model_from_candle().
        """
        if curr_oi <= prev_oi:
            return

        if candle_close > candle_open:
            # Bullish candle + rising OI = new longs entering
            self.long_entries.append((candle_close * 0.90, notional, timestamp))
        else:
            # Bearish candle + rising OI = new shorts entering
            self.short_entries.append((candle_close * 1.10, notional, timestamp))

    def cluster_above(self, price: float, pct: float = 0.03) -> float:
        """USD notional of short liq levels within pct above price (squeeze risk)."""
        upper = price * (1 + pct)
        return sum(n for p, n, _ in self.short_entries if price < p <= upper)

    def cluster_below(self, price: float, pct: float = 0.03) -> float:
        """USD notional of long liq levels within pct below price (cascade potential)."""
        lower = price * (1 - pct)
        return sum(n for p, n, _ in self.long_entries if lower <= p < price)

    def new_positions_1h(self, now: float) -> tuple:
        """
        Returns (short_notional_1h, long_notional_1h) — new positions opened in last hour.
        Measures position entries by creation timestamp, not actual liquidation events.
        Used as a proxy for directional crowding.
        """
        cutoff = now - 3600
        return (
            sum(n for _, n, t in self.short_entries if t >= cutoff),
            sum(n for _, n, t in self.long_entries  if t >= cutoff)
        )
```

---

### 5.2 LiquidationModel Caller

The model is fed by this function, called inside `ingest_asset_ctx()` on every 1-min OI append. Without it the model is never updated and squeeze scores stay at 0.

```python
def update_liq_model_from_candle(state: dict, mark_price: float, now: float) -> None:
    """
    OI from Hyperliquid is in base units (e.g. ETH). Multiply by mark_price for USD notional.
    Also recalculates and caches squeeze_score after each update.
    """
    oi = list(state['oi_series'])
    px = list(state['price_series'])

    if len(oi) < 2 or len(px) < 2:
        return

    delta_oi = oi[-1] - oi[-2]
    if delta_oi <= 0:
        return

    state['liq_model'].update(
        prev_oi      = oi[-2],
        curr_oi      = oi[-1],
        candle_open  = px[-2],
        candle_close = px[-1],
        notional     = delta_oi * mark_price,
        timestamp    = now
    )
    state['squeeze_score'] = calculate_squeeze_score(
        state['liq_model'], px[-1], state['funding_series'], now
    )
```

---

### 5.3 Squeeze Risk Score (0–10)

| Condition | Points | What It Detects |
|---|---|---|
| New short positions opened in last 1h > new long positions | +3 | Fresh short crowding |
| Total short liq notional within 3% above price > long liq notional within 3% below | +2 | More upward squeeze pressure than downward cascade |
| Funding dropped >30% from an elevated baseline (>20% APR) in last 1h | +3 | Rapid new short entry |
| squeeze_risk_ratio > 0.45 | +2 | Structural squeeze confirmed |

```python
def squeeze_risk_ratio(liq_above: float, liq_below: float) -> float:
    total = liq_above + liq_below
    return 0.0 if total == 0 else liq_above / total


def calculate_squeeze_score(liq_model: LiquidationModel,
                             current_price: float,
                             funding_series: deque,
                             now: float = None) -> int:
    """
    Returns 0–10. Cached in state['squeeze_score'] after each update.
    score >= 5  → HARD BLOCK — no entry
    score 3–4   → reduce size (multiply notional by 0.40)
    score 0–2   → normal size
    """
    if liq_model is None:
        return 0
    if now is None:
        now = time.time()

    score = 0

    short_1h, long_1h = liq_model.new_positions_1h(now)
    if short_1h > long_1h:
        score += 3

    liq_above = liq_model.cluster_above(current_price)
    liq_below = liq_model.cluster_below(current_price)
    if liq_above > liq_below:
        score += 2

    f_series = list(funding_series)
    if len(f_series) >= 2:
        f_prev, f_now = f_series[-2], f_series[-1]
        elevated_floor = SQUEEZE_FUNDING_ELEVATED_APR / 8760
        if f_prev > elevated_floor and f_prev > 0:
            if (f_prev - f_now) / f_prev > SQUEEZE_FUNDING_DROP_MIN_PCT:
                score += 3

    if squeeze_risk_ratio(liq_above, liq_below) > SQUEEZE_RISK_RATIO_MAX:
        score += 2

    return min(score, 10)
```

**Squeeze Risk Score ≥ 5 is a hard block. It cannot be overridden by any other module.**

---

### 5.4 External Data for Backtesting

- **CoinGlass API** — liquidation heatmap export for major alts
- **CoinAPI** — historical funding + OI for Hyperliquid (validate start date — limited coverage)
- **Custom reconstruction** — reconstruct liq levels from OI + candle direction where external data is unavailable

---

## 6. Regime Filter

Prevents trading into broad macro uptrends. Returns `NORMAL`, `REDUCED`, or `DISABLED`.

`coin_closes_1h` must come from `candleSnapshot` REST, not from `allMids`. `allMids` gives mid prices at ~2s intervals — not 1h candle closes. Using mid prices would make the breadth check fire on intraday noise rather than genuine 1-hour moves.

```python
async def refresh_1h_closes(universe_coins: list) -> dict:
    """
    Call every 60 minutes. Cache and pass the result to regime_filter() each scanner cycle.
    Returns {coin: [1h_close, ...]} for last REGIME_CANDLE_HISTORY_HOURS hours.
    BTC is always included.
    """
    coin_closes = {}
    for coin in list(set(universe_coins + ['BTC'])):
        payload = {
            "type": "candleSnapshot",
            "req": {
                "coin":      coin,
                "interval":  "1h",
                "startTime": int((time.time() - REGIME_CANDLE_HISTORY_HOURS * 3600) * 1000)
            }
        }
        candles = await rest_post("/info", payload)
        if candles:
            coin_closes[coin] = [float(c['c']) for c in candles]
    return coin_closes


def regime_filter(btc_closes_1h: list,
                  watch_list_coins: list,
                  coin_closes_1h: dict) -> str:
    """
    btc_closes_1h: coin_closes_1h['BTC'] from refresh_1h_closes().
    Returns: 'NORMAL' | 'REDUCED' | 'DISABLED'
    """
    if len(btc_closes_1h) < REGIME_MIN_BTC_HISTORY:
        return 'DISABLED'

    btc_ema_20 = ema(btc_closes_1h, 20)
    btc_ema_50 = ema(btc_closes_1h, 50)
    btc_slope  = (btc_ema_20[-1] - btc_ema_20[-6]) / btc_ema_20[-6]

    if btc_ema_20[-1] > btc_ema_50[-1] and btc_slope > BTC_SLOPE_DISABLE_THRESHOLD:
        return 'DISABLED'
    if btc_ema_20[-1] > btc_ema_50[-1] and btc_slope > BTC_SLOPE_REDUCE_THRESHOLD:
        return 'REDUCED'

    coins_up = sum(
        1 for coin in watch_list_coins
        if len(coin_closes_1h.get(coin, [])) >= 2 and
           (coin_closes_1h[coin][-1] - coin_closes_1h[coin][-2])
           / coin_closes_1h[coin][-2] > ALT_BREADTH_UP_PCT
    )
    if watch_list_coins and coins_up / len(watch_list_coins) > ALT_BREADTH_DISABLE_THRESHOLD:
        return 'DISABLED'

    return 'NORMAL'
```

| BTC EMA State | BTC 5h Slope | Alt Breadth | Output | Size |
|---|---|---|---|---|
| EMA20 > EMA50 | > +1.5% | Any | DISABLED | 0% |
| EMA20 > EMA50 | +0.5% to +1.5% | Any | REDUCED | 50% |
| Any | Any | > 60% up >2% | DISABLED | 0% |
| EMA20 ≤ EMA50 | < +0.5% | < 60% | NORMAL | 100% |

---

## 7. Trigger Layer

Trigger evaluation begins only after a coin has passed all three gates and `squeeze_score < 5`. Order flow is the primary trigger — structure breaks are confirmation only.

### 7.1 Primary Trigger — Tape Aggression Shift

```python
import statistics

def update_delta_state(state: dict, new_delta_60s: float) -> None:
    """Called by DeltaAggregator.flush_if_ready() every 60 seconds."""
    state['trade_delta_60s'] = new_delta_60s
    state['delta_history'].append(new_delta_60s)

    if len(state['delta_history']) >= DELTA_COLD_START_PERIODS:
        state['delta_ready']    = True
        state['delta_mean_10m'] = statistics.mean(state['delta_history'])
        std = statistics.stdev(state['delta_history'])
        state['delta_std_10m']  = std if std > 0 else 1e-9
    else:
        state['delta_ready']    = False
        state['delta_mean_10m'] = 0.0
        state['delta_std_10m']  = 0.0


def get_delta_z_score(state: dict) -> float:
    """
    Returns 0.0 (neutral) if delta_ready is False or std is zero.
    Primary trigger fires when this returns < DELTA_ZSCORE_TRIGGER (-2.0).
    Fires before the candle closes — earlier than any OHLCV-based signal.
    """
    if not state['delta_ready'] or state['delta_std_10m'] == 0:
        return 0.0
    return (state['trade_delta_60s'] - state['delta_mean_10m']) / state['delta_std_10m']
```

**How trades feed delta and VWAP state — `handle_message()`:**

```python
def handle_message(message: dict, state: dict) -> None:
    """Central dispatcher for all WS messages for a single watch-list coin."""
    channel = message.get('channel')
    now     = time.time()

    if channel == 'trades':
        for trade in message.get('data', []):
            price     = float(trade['px'])
            size_base = float(trade['sz'])
            side      = trade['side']
            state['delta_aggregator'].on_trade(side, size_base, price)
            state['delta_aggregator'].flush_if_ready(state, now)
            state['vwap_buffer'].on_trade(price, size_base, now)

    elif channel == 'l2Book':
        bids = message['data']['levels'][0]
        mid  = state['price_series'][-1] if state['price_series'] else 0.0
        if mid > 0:
            threshold = mid * (1 - 0.005)
            depth = sum(float(b['sz']) * float(b['px'])
                        for b in bids if float(b['px']) >= threshold)
            state['bid_depth_t_minus_30s'] = state['bid_depth_now']
            state['bid_depth_now']         = depth

    elif channel == 'activeAssetCtx':
        # activeAssetCtx WS message shape: { "channel": "activeAssetCtx",
        #   "data": { "coin": "ETH", "ctx": PerpsAssetCtx } }
        # Pass only the ctx sub-object to ingest_asset_ctx.
        ingest_asset_ctx(message['data']['ctx'], state, now)

    elif channel == 'candle':
        # Candle WS fields o/c/h/l/v are documented as numbers, not strings.
        # float() is applied anyway for defensive consistency (Rule 1).
        c = message['data']
        state['high_series_5m'].append(float(c['h']))
        state['low_series_5m'].append(float(c['l']))
        state['close_series_5m'].append(float(c['c']))
        # price_series is maintained exclusively by ingest_asset_ctx() (markPx every 60s).
        # A 1m candle WS subscription is not used; 5m candles here are for Gate 3 only.
```

---

### 7.2 Confirmation — At Least ONE Required

| Signal | Threshold | How to Compute |
|---|---|---|
| Bid depth thinning | Bid depth within 0.5% of mid drops > 25% vs 30s ago | `(bid_depth_t_minus_30s - bid_depth_now) / bid_depth_t_minus_30s > 0.25` |
| Structure break | Price below 15m swing low | `price_series[-1] < min(list(price_series)[-15:])` |
| VWAP break | Price below 5m VWAP | `price_series[-1] < state['vwap_buffer'].get_vwap()` |

Do not enter on candlestick rejection patterns alone (shooting stars, upper wicks) without tape confirmation.

---

### 7.3 Trigger Validity Window

```python
def trigger_is_valid(trigger_price: float, current_mid: float,
                     delta_z_score: float) -> bool:
    """
    Called before primary IOC placement and before aggressive IOC fallback.
    Returns False if conditions have deteriorated since the trigger fired.
    """
    price_drift = abs(current_mid - trigger_price) / trigger_price
    if price_drift > TRIGGER_STALE_DRIFT_MAX:
        log(f'TRIGGER STALE: drifted {price_drift:.2%}')
        return False

    if delta_z_score >= DELTA_ZSCORE_EXPIRY:
        log(f'TRIGGER EXPIRED: z-score recovered to {delta_z_score:.2f}')
        return False

    return True
```

---

## 8. Execution Engine

Hyperliquid does not expose a native "market order" primitive in its API schema. All orders are limit orders with a TIF (Time-In-Force) parameter: `Alo` (Add Liquidity Only), `Ioc` (Immediate or Cancel), or `Gtc` (Good Till Cancelled). What traders call a "market order" is implemented here as an **aggressive IOC limit** — a limit price set far enough through the book to guarantee a fill at prevailing liquidity, subject to Hyperliquid's own rejection states (`marketOrderNoLiquidityRejected`, `oracleRejected`, `tickRejected`). Every function in this section that would have used a market primitive uses this abstraction instead.

```python
async def place_ioc_aggressive(coin: str, side: str,
                                size_coins: float, reference_price: float,
                                sz_decimals: int,
                                slippage_pct: float = 0.005) -> dict | None:
    """
    Submit an IOC limit priced aggressively through the book.
    For sells (shorts): price = reference_price * (1 - slippage_pct)
    For buys  (covers): price = reference_price * (1 + slippage_pct)

    This is the ONLY 'market-like' abstraction in the system.
    There is no native market order primitive on Hyperliquid.

    slippage_pct: how far through the book to price (default 0.5%).
                  Increase if rejections are observed during high volatility.
    """
    if side == 'sell':
        raw_px = reference_price * (1 - slippage_pct)
    else:
        raw_px = reference_price * (1 + slippage_pct)

    limit_px_str = format_price(raw_px, sz_decimals)    # canonical string — Section 3.4

    # Returns the raw exchange response dict.
    # Callers should pass the result through parse_order_status() (Section 8.2).
    return await place_limit_order(
        coin, side, size_coins, limit_px_str, tif='Ioc'
    )
```

---

### 8.1 IOC Semantics

An IOC (Immediate or Cancel) order on Hyperliquid either executes immediately at the specified price — fully or partially — or is cancelled by the exchange on the same request cycle. It never rests on the book. There is therefore no timeout window to manage, no cancel call required before a fallback, and no polling loop with a deadline. The response to an IOC order placement IS the final state of that order.

This means the entry flow is two sequential order actions, not a place-wait-cancel loop:

1. **Primary IOC** — priced just above mid (`mid × (1 + LIMIT_ORDER_OFFSET)`), placing the sell above the best bid. A sell is only marketable when it crosses the best bid; pricing above mid keeps it non-marketable and will only fill if the book moves into it. This is the passive attempt. **In wide-spread conditions this will almost always return unfilled; the aggressive IOC fallback is the primary execution path in such markets.** This is an intentional design trade-off: the passive attempt costs one extra round-trip but captures better pricing when the book is tight.
2. **Fallback aggressive IOC** — priced through the book via `place_ioc_aggressive()`, sent only if the primary returned unfilled AND the trigger is still valid.

If neither fills, no position is opened on this signal. There is no GTC resting order and no cancellation step.

---

### 8.2 Order Response Shape and Parser

The raw Hyperliquid order placement response has this structure:

```json
{
  "status": "ok",
  "response": {
    "type": "order",
    "data": {
      "statuses": [
        {"filled": {"totalSz": "0.5", "avgPx": "1850.20", "oid": 12345}}
      ]
    }
  }
}
```

Each element of `statuses` is one of:

| Key | Meaning |
|---|---|
| `"filled"` | Fully or partially filled. Fields: `totalSz` (string), `avgPx` (string), `oid` (int). |
| `"resting"` | Order is resting on book (GTC/ALO). Fields: `oid`, `cloid`. IOC orders should not rest, but guard for it. |
| `"error"` | Rejection. Value is an error string. Known values include `iocCancelRejected`, `minTradeNtlRejected`, `marketOrderNoLiquidityRejected`, `oracleRejected`, `tickRejected`. |

`place_limit_order()` is a thin OMS wrapper that submits the action and returns the raw response dict. `parse_order_status()` converts it to a normalised shape used throughout the entry logic.

```python
def parse_order_status(raw_response: dict) -> dict | None:
    """
    Parses the raw Hyperliquid order placement response into a normalised dict.

    Returns one of:
      {"status": "filled",  "avg_px": float, "total_sz": float, "oid": int}
      {"status": "resting", "oid": int}
      {"status": "error",   "reason": str}
      None — if the response is malformed or statuses list is empty

    Callers should treat "resting" as unfilled for IOC paths (IOC orders should
    not rest; log a warning if this is returned for an IOC order).
    """
    try:
        statuses = raw_response["response"]["data"]["statuses"]
        if not statuses:
            return None
        outcome = statuses[0]

        if "filled" in outcome:
            f = outcome["filled"]
            return {
                "status":   "filled",
                "avg_px":   float(f["avgPx"]),
                "total_sz": float(f["totalSz"]),
                "oid":      int(f["oid"]),
            }
        if "resting" in outcome:
            return {"status": "resting", "oid": int(outcome["resting"]["oid"])}
        if "error" in outcome:
            return {"status": "error", "reason": str(outcome["error"])}

        return None
    except (KeyError, IndexError, TypeError, ValueError) as e:
        log(f'parse_order_status: malformed response — {e}: {raw_response}')
        return None
```

---

### 8.3 Entry Logic

```python
async def execute_entry(coin: str, size_usd: float,
                        trigger_price: float, state: dict):
    """
    size_usd: position NOTIONAL in USD — output of calculate_position_size().
    This is not the risk budget. It already incorporates stop distance.

    Entry sequence (pure IOC — no resting orders, no timeouts):
      1. Primary IOC at mid + LIMIT_ORDER_OFFSET (sell priced just above mid, near ask)
         → passive attempt; fills if spread is thin or book is moving through us
         → returns unfilled immediately if price does not cross the book
      2. If unfilled and trigger still valid: aggressive IOC via place_ioc_aggressive()
         → sell priced IOC_AGGRESSIVE_SLIPPAGE_PCT below mid to cross the book
      3. If that also unfilled: no position opened on this signal
    """
    mid     = get_mid_price(coin)
    delta_z = get_delta_z_score(state)

    if not trigger_is_valid(trigger_price, mid, delta_z):
        return None

    sz_decimals = get_sz_decimals(coin)
    size_coins  = round(size_usd / mid, sz_decimals)
    if size_coins * mid < MIN_ORDER_NOTIONAL_USD:
        log(f'Position too small: ${size_coins * mid:.2f} — skip')
        return None

    # Step 1: primary passive IOC — priced just above mid.
    # A sell is marketable when it crosses the best bid; pricing above mid keeps
    # it non-marketable. With allMids (no live ask available) mid*(1+offset)
    # is a reasonable passive proxy. It may be above the ask on wide spreads,
    # but will still be non-marketable and cancelled immediately if unfilled.
    raw_limit_px = mid * (1 + LIMIT_ORDER_OFFSET)
    limit_px_str = format_price(raw_limit_px, sz_decimals)   # canonical string — Section 3.4
    raw      = await place_limit_order(coin, 'sell', size_coins, limit_px_str, tif='Ioc')
    # IOC response is final — either filled or not. No wait, no cancel needed.
    primary  = parse_order_status(raw)    # normalise from raw exchange shape

    if primary and primary['status'] == 'filled':
        fill_px  = primary['avg_px']
        limit_px = float(limit_px_str)
        slippage = (limit_px - fill_px) / limit_px
        # For a short: fill below limit_px = you sold lower = adverse slippage (positive)
        if slippage > ABORT_SLIPPAGE:
            log(f'ABORT: slippage {slippage:.3%} — flattening with aggressive IOC')
            current_mid = get_mid_price(coin)
            await place_ioc_aggressive(coin, 'buy', size_coins, current_mid, sz_decimals)
            return None
        if slippage > MAX_SLIPPAGE:
            log(f'WARNING: slippage {slippage:.3%}')
        return primary

    if primary is None:
        # parse_order_status() returned None — malformed or empty exchange response.
        # We do NOT know whether the order filled. Sending a fallback IOC could
        # double-enter if the primary actually filled silently.
        # Safe path: abort this signal, trigger reconciliation to establish truth.
        log('ABORT: parse_order_status returned None — exchange response malformed. '
            'Reconcile order/fill state before next entry.')
        # The caller should query open-order state via the reconciler before retrying.
        return None

    if primary['status'] == 'error':
        reason = primary['reason']
        log(f'Primary IOC rejected: {reason}')

        if reason == 'minTradeNtlRejected':
            # Size is below the exchange minimum — not a transient condition.
            # Abort: sizing logic is wrong, not worth retrying on this signal.
            log('Fatal: minTradeNtlRejected — check MIN_ORDER_NOTIONAL_USD vs exchange minimum')
            return None

        if reason == 'tickRejected':
            # Price failed tick/lot validation — indicates a bug in format_price().
            # Do not retry: the same price would be rejected again.
            log('Fatal: tickRejected — price formatting bug; inspect format_price()')
            return None

        if reason == 'oracleRejected':
            # Our price was too far from the oracle — aggressive pricing or stale mid.
            # Do not blindly escalate: an aggressive IOC would likely also be rejected.
            # Skip the signal and wait for a fresher market quote.
            log('Fatal: oracleRejected — price too far from oracle; skipping signal')
            return None

        if reason == 'iocCancelRejected':
            # IOC found no matching quantity at our price — benign unfilled.
            # Fall through to the aggressive IOC fallback.
            log('iocCancelRejected — no fill at passive price; proceeding to fallback')

        # All other errors: log and fall through; aggressive IOC may still work.
        # e.g. marketOrderNoLiquidityRejected on primary (passive) is a book-depth issue
        # that the aggressive IOC may overcome with a wider price.

    if primary and primary['status'] == 'resting':
        log('WARNING: primary IOC returned resting — unexpected; treating as unfilled')

    # Step 2: primary unfilled or benign rejection — aggressive IOC fallback
    log('Primary IOC returned unfilled — evaluating fallback')
    current_mid = get_mid_price(coin)
    current_z   = get_delta_z_score(state)

    if not trigger_is_valid(trigger_price, current_mid, current_z):
        log('Trigger expired — no fallback')
        return None

    log('Sending aggressive IOC fallback')
    raw_fb   = await place_ioc_aggressive(coin, 'sell', size_coins, current_mid, sz_decimals)
    fallback = parse_order_status(raw_fb)

    if fallback and fallback['status'] == 'filled':
        return fallback

    if fallback and fallback['status'] == 'error':
        reason = fallback['reason']
        log(f'Aggressive IOC rejected: {reason}')
        # Any rejection on the aggressive IOC is final — do not retry further.
        # tickRejected / oracleRejected here also indicate a local bug or stale price.

    log('Both IOC attempts unfilled — no position opened')
    return None
```

---

### 8.4 Slippage Rules

For a short, adverse slippage means filling lower than the limit (you entered at a worse price). The check is `(limit_px - fill_px) / limit_px`.

- **> 0.3%** (`MAX_SLIPPAGE`): log and keep position
- **> 0.5%** (`ABORT_SLIPPAGE`): close immediately with `place_ioc_aggressive(..., side='buy')` — do not re-enter on same signal

---

## 9. Risk Engine

### 9.1 Stop Distance and Position Sizing

```python
def calculate_stop_distance(entry_price: float, atr_14: float,
                             swing_high_price: float,
                             high_volatility: bool) -> float:
    """
    For a short, stop is above entry. Returns fractional distance from entry.
    Uses the lower (tighter) of ATR stop vs swing high stop.
    atr_14: output of compute_atr() on 5m candles.
    swing_high_price: max(price_series[-15:]).
    high_volatility: True if 1h range / entry_price > HIGH_VOL_1H_RANGE_PCT.
    """
    multiplier     = ATR_MULTIPLIER_HIGH_VOL if high_volatility else ATR_MULTIPLIER_NORMAL
    atr_stop_price = entry_price + (multiplier * atr_14)

    if swing_high_price <= entry_price or atr_14 == 0:
        stop_price = atr_stop_price
    else:
        stop_price = min(atr_stop_price, swing_high_price)

    distance = (stop_price - entry_price) / entry_price

    if distance < MIN_STOP_DISTANCE_PCT:
        log(f'Stop {distance:.3%} below floor — using {MIN_STOP_DISTANCE_PCT:.3%}')
        distance = MIN_STOP_DISTANCE_PCT

    return distance


def calculate_position_size(account_equity: float, regime: str,
                             squeeze_score: int,
                             stop_distance_pct: float) -> float:
    """
    Returns position NOTIONAL in USD — not the risk budget.
    Formula: notional = risk_budget / stop_distance_pct
    Example: $10k equity, 1% risk, 2% stop → $100 / 0.02 = $5,000 notional
    """
    if stop_distance_pct <= 0:
        raise ValueError(f'stop_distance_pct must be positive, got {stop_distance_pct}')

    risk_budget = account_equity * RISK_PER_TRADE_PCT

    regime_multipliers = {'NORMAL': 1.0, 'REDUCED': 0.5, 'DISABLED': 0.0}
    risk_budget *= regime_multipliers.get(regime, 0.0)

    if squeeze_score >= SQUEEZE_HARD_BLOCK_SCORE:
        return 0.0
    if squeeze_score >= SQUEEZE_REDUCE_SCORE:
        risk_budget *= SQUEEZE_REDUCE_MULTIPLIER

    return risk_budget / stop_distance_pct
```

---

### 9.2 Stop Loss and Take Profit

For a short, the stop is above entry and "tighter" means the lower of the two stop prices (closer to entry = less risk per trade).

| Parameter | Value | Notes |
|---|---|---|
| Stop Loss | entry + (2 or 3) × ATR(14) | 5m candles. 2× if high volatility, 3× otherwise. |
| Alternative Stop | Last 15m swing high | Use the **lower** price of the two stop levels. |
| Minimum stop distance | 0.5% from entry | Floor applied in `calculate_stop_distance()`. |
| Take Profit 1 | 1.5R — close 50% | TP1 price = entry − (1.5 × stop_distance × entry) |
| Take Profit 2 | 2.5–3R — trail remaining 50% | 1.5×ATR trailing stop after reaching 1R profit |
| Breakeven move | Move stop to entry after 1R | Eliminates loss risk on remaining position |

---

### 9.3 Funding Cost Tracker

```python
def check_funding_exit(current_funding_rate: float, current_pnl_r: float) -> bool:
    """
    Returns True if position should exit due to adverse funding carry.
    current_funding_rate: per-hour rate from funding_series[-1].
                          Negative = you are paying (bad for a short).
    current_pnl_r: unrealised profit in R multiples.
    Rationale: 0.1%/hr against you = 0.5% carry over 5h — meaningful on a 2R target.
    """
    if current_funding_rate < 0 and current_pnl_r < FUNDING_EXIT_PNL_THRESHOLD_R:
        log(f'Funding exit: {current_funding_rate:.6f}/hr, PnL={current_pnl_r:.2f}R')
        return True
    return False
```

---

### 9.4 Portfolio Controls

| Rule | Value | Type |
|---|---|---|
| Maximum concurrent positions | 3–5 | Hard |
| Daily loss kill switch | 3% of equity | Hard |
| Daily loss 24h disable | 5% of equity | Hard |
| Sector concentration | Max 2 positions per sector | Hard |
| No averaging down | Never add to a losing position | Hard |
| Isolated margin only | Never cross-margin | Hard |
| Exchange dead-man switch | `scheduleCancel` maintained on account — cancels all open orders | Hard (exchange-native) |
| Process dead-man switch | Emergency position flatten via aggressive IOC after 5 min no heartbeat | Hard (application-level) |

---

### 9.5 Daily Loss Tracker

```python
from datetime import datetime, timedelta

class DailyLossTracker:
    """
    Tracks realised P&L within the UTC day. Resets at midnight UTC.
    The 24h disable persists across midnight.
    """
    def __init__(self, account_equity: float):
        self.equity        = account_equity
        self.daily_pnl     = 0.0
        self.reset_date    = datetime.utcnow().date()
        self.kill_active   = False
        self.disable_until = None

    def record_close(self, pnl_usd: float) -> str:
        """Call after every position close. Returns 'OK' | 'KILL' | 'DISABLE'."""
        self._maybe_reset()
        self.daily_pnl += pnl_usd
        loss_pct = -self.daily_pnl / self.equity

        if loss_pct >= DAILY_LOSS_DISABLE_PCT:
            self.disable_until = datetime.utcnow() + timedelta(hours=24)
            log(f'DAILY DISABLE: {loss_pct:.2%} loss — trading off 24h')
            return 'DISABLE'
        if loss_pct >= DAILY_LOSS_KILL_PCT:
            self.kill_active = True
            log(f'DAILY KILL: {loss_pct:.2%} loss')
            return 'KILL'
        return 'OK'

    def is_trading_allowed(self) -> bool:
        """Check at the top of every scanner cycle."""
        self._maybe_reset()
        if self.disable_until and datetime.utcnow() < self.disable_until:
            return False
        return not self.kill_active

    def _maybe_reset(self) -> None:
        today = datetime.utcnow().date()
        if today > self.reset_date:
            self.daily_pnl   = 0.0
            self.kill_active = False
            self.reset_date  = today
            # disable_until intentionally NOT reset — 24h ban persists across midnight
```

---

### 9.6 Correlation Filter

```python
SECTOR_MAP = {
    'BTC': 'L1',  'ETH': 'L1',  'SOL': 'L1',  'AVAX': 'L1', 'ADA': 'L1',
    'SUI': 'L1',  'APT': 'L1',  'TON': 'L1',  'NEAR': 'L1', 'TRX': 'L1',
    'OP':  'L1',  'ARB': 'L1',  'SEI': 'L1',
    'LINK': 'Oracle', 'BAND': 'Oracle',
    'UNI': 'DeFi',  'AAVE': 'DeFi',  'CRV': 'DeFi',
    'GMX': 'DeFi',  'JUP': 'DeFi',   'PENDLE': 'DeFi',
    'DOGE': 'Meme', 'SHIB': 'Meme',  'PEPE': 'Meme', 'WIF': 'Meme', 'BONK': 'Meme',
    'FET': 'AI',  'RNDR': 'AI', 'TAO': 'AI',
}

def correlation_check_passes(new_coin: str, open_positions: list) -> bool:
    """
    open_positions: list of coin strings currently held short.
    Returns False if adding new_coin would put > MAX_POSITIONS_PER_SECTOR in one sector.
    Coins not in SECTOR_MAP default to 'Other'.
    """
    sector = SECTOR_MAP.get(new_coin, 'Other')
    count  = sum(1 for c in open_positions if SECTOR_MAP.get(c, 'Other') == sector)
    if count >= MAX_POSITIONS_PER_SECTOR:
        log(f'Correlation block: {count} positions in {sector}')
        return False
    return True
```

---

### 9.7 Dead-Man Switch

The watchdog runs in a separate OS thread, not an asyncio task. If the asyncio event loop freezes, an asyncio watchdog also freezes. Only an OS thread can reliably detect an asyncio hang.

```python
import asyncio
import threading
import time

class HeartbeatMonitor:
    def __init__(self, timeout_s: int = HEARTBEAT_TIMEOUT_S):
        self.last_beat = time.time()
        self.timeout_s = timeout_s
        self._lock     = threading.Lock()

    def beat(self) -> None:
        with self._lock:
            self.last_beat = time.time()

    def is_dead(self) -> bool:
        with self._lock:
            return (time.time() - self.last_beat) > self.timeout_s


def start_watchdog(monitor: HeartbeatMonitor, exchange) -> threading.Thread:
    """
    Two-layer dead-man protection:

    Layer 1 — Exchange-native (scheduleCancel):
        Maintained by the OMS on a periodic refresh cadence.
        Cancels all open orders if the account stops refreshing.
        This is the primary and most reliable protection layer.
        It does NOT flatten open positions — only cancels orders.

    Layer 2 — Application-level emergency flatten (this watchdog):
        Fires if the local process heartbeat goes silent for HEARTBEAT_TIMEOUT_S.
        Calls emergency_flatten_all(), which submits aggressive IOC orders
        for each open position. This is NOT a native market order — it is
        the same place_ioc_aggressive() abstraction used throughout Section 8.
        This layer handles the case where scheduleCancel fired (orders gone)
        but positions remain open and the process cannot recover.
    """
    def watchdog():
        while True:
            time.sleep(HEARTBEAT_BEAT_INTERVAL_S)
            if monitor.is_dead():
                log('PROCESS DEAD-MAN TRIGGERED — attempting emergency position flatten')
                try:
                    # emergency_flatten_all is a coroutine. The watchdog runs in a
                    # plain OS thread that has no access to the main event loop.
                    # asyncio.run() creates a new event loop in this thread, runs
                    # the coroutine to completion, then tears it down.
                    asyncio.run(emergency_flatten_all(exchange))
                except Exception as e:
                    log(f'Emergency flatten failed: {e} — manual intervention required')
                break

    t = threading.Thread(target=watchdog, daemon=True)
    t.start()
    return t


async def emergency_flatten_all(exchange) -> None:
    """
    Application-level emergency position closer.
    For each open position, submits an aggressive IOC order (place_ioc_aggressive)
    on the opposite side at current mid price.
    This is NOT a native market order — see Section 8 for IOC semantics.
    Called by start_watchdog() via asyncio.run() — safe to call from a non-async thread.
    The thread creates a fresh event loop; do not call from within a running event loop.

    Thread-safety caveat: client objects passed to exchange (e.g. HTTP sessions,
    connection pools) must be safe to use from a separate event loop. Prefer
    constructing a fresh REST client inside this function rather than reusing the
    main-loop client, to avoid cross-loop sharing issues.
    """
    positions = await exchange.get_open_positions()
    for pos in positions:
        coin        = pos['coin']
        size        = abs(float(pos['szi']))
        side        = 'buy' if float(pos['szi']) < 0 else 'sell'   # close shorts with buy
        mid         = get_mid_price(coin)
        sz_decimals = get_sz_decimals(coin)
        try:
            await place_ioc_aggressive(coin, side, size, mid, sz_decimals,
                                       slippage_pct=IOC_EMERGENCY_SLIPPAGE_PCT)
            log(f'Emergency flatten sent: {coin} {side} {size}')
        except Exception as e:
            log(f'Emergency flatten failed for {coin}: {e}')
```

---

## 10. Asset Universe

Only trade assets meeting all criteria simultaneously:

- Active perpetual on Hyperliquid (in `metaAndAssetCtxs` response)
- `float(dayNtlVlm) > 5,000,000` USD daily volume
- `float(openInterest) > 2,000,000` USD OI
- Max leverage ≥ 5× (lower-leverage assets have weaker liquidation dynamics)
- Funding history ≥ 7 days (required for Gate 1 baseline)
- Not added to universe in last 48 hours

All `metaAndAssetCtxs` numeric fields are strings — always convert with `float()`. Re-evaluate universe membership weekly. Target: top 20–50 liquid perps by volume.

---

## 11. Backtesting Specification

### 11.1 Framework

Use custom vectorised pandas/numpy. Do not use Freqtrade — it was not built for funding-rate-aware perp strategies.

The backtest must simulate:
- All three scanner gates with real historical OI (1-min throttled) and hourly funding
- Funding collected or paid per position per hour
- Slippage applied in the direction that hurts shorts:
  - Entry: `fill_price = mid * (1 - SLIPPAGE_MODEL_PCT)` (lower = worse short entry)
  - Exit: `fill_price = mid * (1 + SLIPPAGE_MODEL_PCT)` (higher = worse buyback)
- Compound position sizing (equity-based, not fixed notional)
- Liquidation model running in parallel with squeeze blocks applied retroactively

### 11.2 Data Sources

| Data Type | Source | Notes |
|---|---|---|
| OHLCV 1m + 5m | Hyperliquid REST `candleSnapshot` | Request in batches |
| Funding (hourly) | Hyperliquid REST `fundingHistory` | Returns 8h basis — divide by 8 |
| OI historical | CoinAPI HYPERLIQUID endpoint | Validate start date — limited coverage |
| Liquidation heatmap | CoinGlass API export | For squeeze validation only |

### 11.3 Biases to Avoid

- **Look-ahead bias:** Gate 3 uses only data available at bar close. VWAP from trades up to that point only.
- **price_series backtest alignment:** Use 1-min markPx snapshots (not candle closes) to match live series semantics defined in `ingest_asset_ctx()`. Using candle closes would create systematic backtest-to-live divergence.
- **Survivorship bias:** Include delisted assets where data exists.
- **Funding period bias:** Use the per-hour rate active at entry time, not the end-of-8h computed rate.
- **Slippage direction bias:** Slippage must hurt shorts on both entry and exit — not applied symmetrically.

### 11.4 Validation Metrics

| Metric | Minimum |
|---|---|
| Sharpe Ratio (annualised) | > 1.0 |
| Max Drawdown | < 20% |
| Expectancy per trade | > 0.3R |
| Win rate | > 35% |
| Live signal freq vs backtest | Within ±30% |

---

## 12. Deployment Plan

| Phase | Duration | Activity | Pass Criteria |
|---|---|---|---|
| Phase 1: Backtest | Weeks 1–2 | Backtest all three gates. Measure gate fire rate, squeeze block rate, regime disable rate. | Sharpe > 1.0, max DD < 20% |
| Phase 2: Paper Trade | Weeks 3–4 | Full system live, paper trades. Log every signal. Compare frequency to backtest. | Live freq within ±30% of backtest, no logic bugs |
| Phase 3: Small Live | Weeks 5–8 | Real capital. Max 3 positions, $100–$200 each. Isolated margin. Kill switches active. | 50+ trades, Sharpe > 1.0, max DD < 15% |
| Phase 4: Scale | Ongoing | Increase size 25% at a time. Add assets one at a time. Never scale in a drawdown. | 30+ trades at current size before each increase |

---

## 13. Known Failure Modes

| Failure Mode | Cause | Mitigation |
|---|---|---|
| Continuous short squeeze | BTC sustained bull trend | Regime filter DISABLED. Dead-man switch. |
| Series timing corruption | Appending at 1s WS cadence instead of 1-min throttle | Two-path ingestion with throttles. REST-only funding. |
| Gate 1 structurally empty | funding_series never hydrated for universe coins | `bootstrap_universe_funding()` called at startup and every hour. |
| Scanner blind to full universe | No REST→gate ingestion path | `run_universe_scanner()` reads `metaAndAssetCtxs`. |
| Unfilled entry | Both IOC attempts unfilled or fatally rejected | `iocCancelRejected` → fallback proceeds; `tickRejected` / `oracleRejected` / `minTradeNtlRejected` → signal aborted. |
| iocCancelRejected | IOC found no matching quantity | Expected on passive IOC in wide-spread conditions — fallback aggressive IOC handles it. |
| Funding data gap on reconnect | WS disconnect | `refresh_funding_from_rest()` called on every reconnect. |
| OI inflation from liquidations | Forced liqs spike OI transiently | 5-min smoothed OI in Gate 2. |
| Cold-start spurious trigger | Delta z-score undefined at startup | `delta_ready` flag — inactive until 10 full 60s windows. |
| LiquidationModel memory growth | Unbounded list over 24/7 operation | `deque(maxlen=1440)` on all model entry lists. |
| liq_model None crash | State dict constructed without factory | `create_asset_state()` always initialises `LiquidationModel()`. |
| Zero stop distance | Swing high equals entry price | `MIN_STOP_DISTANCE_PCT` floor in `calculate_stop_distance()`. |
| Funding carry bleed | Negative funding on open position | `check_funding_exit()` closes if < 0.5R profit. |
| scheduleCancel scope confusion | scheduleCancel cancels orders but not open positions | Two-layer design: scheduleCancel for open orders; emergency_flatten_all() for open positions. |

---

## 14. WebSocket Reconnection Protocol

```python
import asyncio, json, time, websockets

WS_URL = "wss://api.hyperliquid.xyz/ws"

async def _send_sub(ws, sub: dict) -> None:
    """Helper: send a single subscription with the required method wrapper."""
    await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))


async def subscribe_warmup_feeds(ws, coin: str) -> None:
    """
    Tier: Warm-up candidates (passed Gates 1+2, Gate 3 not yet evaluated).
    Subscribes: trades, activeAssetCtx, 5m candle.
    Does NOT include l2Book — that is only needed for trigger confirmation
    on active watch-list assets.
    """
    for sub in [
        {"type": "trades",         "coin": coin},
        {"type": "activeAssetCtx", "coin": coin},
        {"type": "candle",         "coin": coin, "interval": "5m"},
    ]:
        await _send_sub(ws, sub)


async def subscribe_watchlist_feeds(ws, coin: str) -> None:
    """
    Tier: Active watch list (passed all three gates).
    Sends ONLY the incremental l2Book subscription.

    Warm-up feeds (trades, candle, activeAssetCtx) are already active
    from subscribe_warmup_feeds() called at initial subscription.
    Do NOT call subscribe_warmup_feeds() here — the exchange does not
    document idempotent re-subscription behavior, and duplicate subscriptions
    waste message budget and may produce duplicate messages.

    Call this once when a coin is promoted from warm-up to active watch list.
    """
    await _send_sub(ws, {"type": "l2Book", "coin": coin})


async def unsubscribe_warmup_feeds(ws, coin: str) -> None:
    """
    Unsubscribe warm-up feeds when a coin is demoted from warm-up
    (failed Gate 3, or Gate 1/2 failure during warm-up period).
    See also: reset_warmup_state().
    """
    for sub in [
        {"type": "trades",         "coin": coin},
        {"type": "activeAssetCtx", "coin": coin},
        {"type": "candle",         "coin": coin, "interval": "5m"},
    ]:
        await ws.send(json.dumps({"method": "unsubscribe", "subscription": sub}))


async def unsubscribe_watchlist_feeds(ws, coin: str) -> None:
    """
    Unsubscribe all feeds when a coin is removed from the active watch list.
    """
    for sub in [
        {"type": "trades",         "coin": coin},
        {"type": "activeAssetCtx", "coin": coin},
        {"type": "candle",         "coin": coin, "interval": "5m"},
        {"type": "l2Book",         "coin": coin},
    ]:
        await ws.send(json.dumps({"method": "unsubscribe", "subscription": sub}))


# Server closes connections idle for 60s unless client sends {"method":"ping"}.
# We use a 45s timeout — well inside the 60s window — so a quiet channel
# (e.g. an illiquid coin overnight) never triggers a server-side close.
WS_PING_INTERVAL_S = 45


async def ws_connection_manager(coin: str, state: dict, exchange) -> None:
    retry_delay = 1
    while True:
        try:
            async with websockets.connect(WS_URL) as ws:
                retry_delay = 1
                # Tier transitions are orchestrated by the main loop;
                # ws_connection_manager is stateless and always starts in warm-up mode.
                # The main loop calls subscribe_watchlist_feeds() separately
                # when a coin is promoted to the active watch list.
                await subscribe_warmup_feeds(ws, coin)

                # Restore funding series from REST after any gap
                await refresh_funding_from_rest(coin, state)
                state['has_data_gap'] = False
                state['delta_ready']  = False    # rebuild baseline from fresh live data

                while True:
                    try:
                        # Wait up to WS_PING_INTERVAL_S for the next message.
                        # If the channel is quiet, send a ping to prevent server close.
                        raw = await asyncio.wait_for(ws.recv(), timeout=WS_PING_INTERVAL_S)
                        msg = json.loads(raw)
                        if msg.get('channel') == 'pong':
                            continue    # ignore pong responses — they confirm liveness only
                        handle_message(msg, state)
                        exchange.heartbeat_monitor.beat()

                    except asyncio.TimeoutError:
                        # No message received within the window — send ping to keep alive
                        await ws.send(json.dumps({"method": "ping"}))
                        log(f'Ping sent for {coin} (quiet channel)')

        except (websockets.ConnectionClosed, OSError) as e:
            log(f'WS disconnected for {coin}: {e}. Retry in {retry_delay}s')
            state['has_data_gap'] = True
            state['delta_ready']  = False
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, WS_RECONNECT_MAX_DELAY_S)
```

`ws_connection_manager()` is stateless with respect to tier — it always reconnects in warm-up mode. The main loop is responsible for calling `subscribe_watchlist_feeds()` to add `l2Book` after promotion, and the appropriate unsubscribe function on demotion.

While `state['has_data_gap'] is True` the scanner must not advance the coin to the watch list and the trigger engine must not fire.

`delta_ready` resets to `False` on every reconnect. The trigger engine stays dormant until 10 full 60-second delta windows have been collected on fresh data.

---

## 15. LLM Implementation Guide

### 15.1 Implementation Order

Complete each step and validate before proceeding.

1. Create `constants.py` with all values from Section 15.3. Never hardcode values inline.
2. Implement `ema()`, `compute_vwap()`, `compute_atr()` (Section 3.1–3.3). Unit test all three.
3. Implement `VwapBuffer` and `DeltaAggregator` (Section 3.4–3.5). Unit test flush logic.
4. Implement `LiquidationModel` and `calculate_squeeze_score()` (Section 5.1–5.3). Unit test with synthetic OI sequences.
5. Implement `create_asset_state()` (Section 2.5). Verify no None values.
6. Implement `refresh_funding_from_rest()` (Section 2.4 Path A). Verify hourly resolution and ÷8 conversion.
7. Implement `ingest_asset_ctx()` with throttles (Section 2.4 Path B). Verify it never writes `funding_series`.
8. Implement `update_liq_model_from_candle()` (Section 5.2). Verify model updates only on OI growth.
9. Implement `run_universe_scanner()` (Section 4.1). Verify REST→gate ingestion for full universe.
10. Implement Gates 1, 2, 3 (Sections 4.2–4.4). Backtest fire frequency — expect 5–15% of universe at any time.
11. Implement `refresh_1h_closes()` and `regime_filter()` (Section 6). Validate it blocks during a BTC bull run.
12. Implement `handle_message()` (Section 7.1). Verify both trade→delta and trade→VWAP pipelines.
13. Implement `update_delta_state()` and `get_delta_z_score()` with cold-start guard (Section 7.1).
14. Implement `format_price()` (Section 3.4) and `place_ioc_aggressive()` (Section 8). Implement `execute_entry()` (Section 8.1). Verify IOC-only path, no market orders. Verify slippage check. Paper trade only.
15. Implement `calculate_stop_distance()` and `calculate_position_size()` (Section 9.1). Verify notional = risk_budget / stop_distance.
16. Implement `DailyLossTracker` (Section 9.5) and `correlation_check_passes()` (Section 9.6).
17. Implement `HeartbeatMonitor` and `start_watchdog()` in a separate OS thread (Section 9.7).
18. Implement `subscribe_warmup_feeds()`, `subscribe_watchlist_feeds()`, and unsubscribe counterparts (Section 14). Implement `ws_connection_manager()`. Test disconnect/reconnect and tier promotion explicitly.
19. Run full backtester. Validate all metrics from Section 11.4 before any live capital.

---

### 15.2 Ambiguity Resolution Rules

When this PRD is ambiguous, apply these rules in order:

1. Safety > accuracy. If unsure whether to enter, do not.
2. Use the most conservative threshold for entry gates.
3. Use the most generous threshold for exit and stop rules.
4. If `has_data_gap is True`, skip all entry decisions.
5. Never infer missing data. Raise an error or skip.
6. When in doubt about data type, always `float()` before arithmetic.

---

### 15.3 Constants Reference

Define all constants before writing any module code. Keep them in a single `constants.py`.

| Constant | Value | Module |
|---|---|---|
| `FUNDING_API_TO_HOURLY_DIVISOR` | `8` | Ingestion — critical |
| `GATE1_FUNDING_APR_THRESHOLD` | `0.50` | Gate 1 |
| `GATE1_ANNUALISE_MULTIPLIER` | `8760` | Gate 1 |
| `GATE1_MIN_POSITIVE_HOURS` | `6` | Gate 1 |
| `GATE1_PREMIUM_FLOOR` | `0.0002` | Gate 1 |
| `GATE2_OI_CHANGE_THRESHOLD` | `0.05` | Gate 2 |
| `GATE2_PRICE_CHANGE_MAX` | `0.005` | Gate 2 |
| `GATE2_LOOKBACK_MINUTES` | `245` | Gate 2 |
| `GATE2_OI_SMOOTH_PERIODS` | `5` | Gate 2 |
| `GATE3_PRICE_FROM_HIGH_MAX` | `0.01` | Gate 3 — max distance from 4h max sampled mark price |
| `FAILED_BREAKOUT_RECOVERY_THRESHOLD` | `0.005` | Gate 3 |
| `FAILED_BREAKOUT_LOOKBACK_CANDLES` | `24` | Gate 3 |
| `GATE3_WARM_UP_S` | `360` (6 min) | Gate 3 — warm-up after seeding, covers VwapBuffer fill |
| `FUNDING_REFRESH_INTERVAL_S` | `3600` (1 hour) | Funding bootstrap repeat cadence |
| `FUNDING_BOOTSTRAP_STAGGER_S` | `0.2` (200 ms) | Delay between per-coin fundingHistory requests at bootstrap |
| `SQUEEZE_HARD_BLOCK_SCORE` | `5` | Liq Intelligence |
| `SQUEEZE_REDUCE_SCORE` | `3` | Liq Intelligence |
| `SQUEEZE_REDUCE_MULTIPLIER` | `0.40` | Liq Intelligence |
| `SQUEEZE_RISK_RATIO_MAX` | `0.45` | Liq Intelligence |
| `SQUEEZE_FUNDING_ELEVATED_APR` | `0.20` | Liq Intelligence |
| `SQUEEZE_FUNDING_DROP_MIN_PCT` | `0.30` | Liq Intelligence |
| `LIQ_MODEL_AVG_LEVERAGE` | `10.0` | Liq Intelligence |
| `LIQ_MODEL_MAX_ENTRIES` | `1440` | Liq Intelligence |
| `LIQ_CLUSTER_RANGE_PCT` | `0.03` | Liq Intelligence |
| `BTC_SLOPE_DISABLE_THRESHOLD` | `0.015` | Regime Filter |
| `BTC_SLOPE_REDUCE_THRESHOLD` | `0.005` | Regime Filter |
| `ALT_BREADTH_DISABLE_THRESHOLD` | `0.60` | Regime Filter |
| `ALT_BREADTH_UP_PCT` | `0.02` | Regime Filter |
| `REGIME_MIN_BTC_HISTORY` | `55` | Regime Filter |
| `REGIME_CANDLE_HISTORY_HOURS` | `60` | Regime Filter |
| `DELTA_ZSCORE_TRIGGER` | `-2.0` | Trigger Engine |
| `DELTA_ZSCORE_EXPIRY` | `-1.5` | Trigger Engine |
| `DELTA_COLD_START_PERIODS` | `10` | Trigger Engine |
| `DELTA_WINDOW_S` | `60` | DeltaAggregator |
| `VWAP_BUFFER_WINDOW_S` | `300` | VwapBuffer |
| `BID_DEPTH_THIN_THRESHOLD` | `0.25` | Trigger Engine |
| `BID_DEPTH_WINDOW_S` | `30` | Trigger Engine |
| `TRIGGER_STALE_DRIFT_MAX` | `0.015` | Trigger + Execution |
| `LIMIT_ORDER_OFFSET` | `0.0005` | Execution Engine |
| `IOC_AGGRESSIVE_SLIPPAGE_PCT` | `0.005` (0.5%) | Execution Engine |
| `IOC_EMERGENCY_SLIPPAGE_PCT` | `0.010` (1.0%) | Emergency Flatten — wider to ensure fills |
| `MAX_SLIPPAGE` | `0.003` | Execution Engine |
| `ABORT_SLIPPAGE` | `0.005` | Execution Engine |
| `MIN_ORDER_NOTIONAL_USD` | `10.0` | Execution Engine |
| `RISK_PER_TRADE_PCT` | `0.01` | Risk Engine |
| `MIN_STOP_DISTANCE_PCT` | `0.005` | Risk Engine |
| `ATR_PERIOD` | `14` | Risk Engine |
| `ATR_LOOKBACK_CANDLES` | `15` | Risk Engine |
| `ATR_MULTIPLIER_HIGH_VOL` | `2.0` | Risk Engine |
| `ATR_MULTIPLIER_NORMAL` | `3.0` | Risk Engine |
| `HIGH_VOL_1H_RANGE_PCT` | `0.03` | Risk Engine |
| `TP1_R_TARGET` | `1.5` | Risk Engine |
| `TP2_R_TARGET` | `2.5` | Risk Engine |
| `TP1_CLOSE_FRACTION` | `0.50` | Risk Engine |
| `DAILY_LOSS_KILL_PCT` | `0.03` | Risk Engine |
| `DAILY_LOSS_DISABLE_PCT` | `0.05` | Risk Engine |
| `FUNDING_EXIT_PNL_THRESHOLD_R` | `0.5` | Risk Engine |
| `MAX_POSITIONS_PER_SECTOR` | `2` | Correlation Filter |
| `MIN_UNIVERSE_DAILY_VOL_USD` | `5_000_000` | Asset Universe |
| `MIN_UNIVERSE_OI_USD` | `2_000_000` | Asset Universe |
| `MIN_UNIVERSE_MIN_LEVERAGE` | `5` | Asset Universe |
| `MIN_UNIVERSE_FUNDING_HISTORY_DAYS` | `7` | Asset Universe |
| `NEW_ASSET_BLACKOUT_HOURS` | `48` | Asset Universe |
| `SLIPPAGE_MODEL_PCT` | `0.001` | Backtesting |
| `WS_RECONNECT_MAX_DELAY_S` | `60` | WebSocket |
| `WS_PING_INTERVAL_S` | `45` | WebSocket — keep-alive before 60s server close |
| `HEARTBEAT_TIMEOUT_S` | `300` | Kill Switch |
| `HEARTBEAT_BEAT_INTERVAL_S` | `30` | Kill Switch |

---

*AltShortBot PRD v3.9 — 15 sections, 34 functions and classes, 67 named constants. Every threshold is concrete, every data flow is specified, every function is complete. Direct implementation reference for an LLM coding agent.*
