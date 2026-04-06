# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install (editable + dev dependencies)
pip install -e ".[dev]"

# Lint
ruff check .

# Type-check (strict mode)
mypy .

# Run all tests (asyncio_mode=auto — no @pytest.mark.asyncio needed)
pytest

# Run a single test file
pytest tests/test_foo.py

# Run with backtesting extras
pip install -e ".[dev,backtest]"

# Entry points
altshortbot-backtest   # scripts/bootstrap.py:main
altshortbot-paper      # scripts/paper_trade.py:main
altshortbot-live       # scripts/live_trade.py:main
```

## Implementation Status

All packages are complete. The bot is fully implemented and running in paper trade.

| Package | Status |
|---|---|
| `shared/` (constants, types, state_factory, helpers, logging_config) | ✅ Complete |
| `strategy/trigger/` (vwap_buffer, delta_aggregator, trigger_engine) | ✅ Complete |
| `strategy/liq_model.py` | ✅ Complete |
| `strategy/scanner/` (gate1, gate2, gate3, seed_rest, universe_scanner, promote_watchlist) | ✅ Complete |
| `strategy/regime_filter.py` | ✅ Complete |
| `market_data/` (universe_snapshotter, tiered_streamer, state_normaliser, ws_manager, all_mids_ws, user_ws) | ✅ Complete |
| `oms/` (price_formatter, order_parser, ioc_entry, execution_adapter, protection_manager) | ✅ Complete |
| `risk/` (daily_loss_tracker, correlation_filter, watchdog, portfolio_controller) | ✅ Complete |
| `main.py`, `config/settings.py` | ✅ Complete |
| `scripts/` (bootstrap, paper_trade, live_trade, analyze_logs, debug_gates, smoke_test) | ✅ Complete |
| `tests/unit/` (test_helpers, test_vwap_delta, test_liq_model, test_ingestion, test_gates, test_regime, test_trigger, test_execution) | ✅ Complete |

The authoritative specification is `AltShortBot_PRD_v3.md`. Section 15 is the LLM implementation guide.

## Architecture

Four services running in a single asyncio process. Each service has a strict ownership rule:

| Service | Package | Owns |
|---|---|---|
| Market Data | `market_data/` | All WS/REST ingestion, per-coin state updates |
| Strategy | `strategy/` | Scanner gates, regime filter, trigger engine — emits `TradeIntent` only |
| OMS | `oms/` | All signed exchange actions, nonce counter, order batching |
| Risk | `risk/` | Reconciliation, kill switches, daily loss tracking |

**Strategy never calls the exchange.** It emits `TradeIntent` (defined in `shared/types.py`), which OMS consumes.

### TradeIntent — the Strategy→OMS contract

```python
@dataclass
class TradeIntent:
    coin: str
    side: OrderSide          # "buy" | "sell"
    size_usd: float
    trigger_price: float
    stop_distance_pct: float
    squeeze_score: int
    regime: Regime           # "NORMAL" | "REDUCED" | "DISABLED"
    issued_at: float         # unix timestamp
```

### Asset tiering

Three tiers drive subscription decisions in the Market Data service:

1. **Universe** — all liquid perps; receives `allMids` + periodic `metaAndAssetCtxs` REST polls
2. **Warm-up candidates** — passed Gates 1+2; receives `trades`, `candle`, `activeAssetCtx` WS subscriptions + `candleSnapshot` REST seed
3. **Active watch list** — passed all three gates; receives warm-up feeds + `l2Book`

Downgrade/unsubscribe logic mirrors upgrade logic exactly.

### Per-asset state

All mutable state for a coin lives in the dict returned by `shared/state_factory.create_asset_state()`. **Never construct this dict manually.**

### Shared layer (`shared/`)

- `constants.py` — every numeric threshold; import from here, never hardcode inline
- `types.py` — all type definitions (`TradeIntent`, `ParsedOrderStatus`, `Regime`, etc.)
- `state_factory.py` — `create_asset_state()` factory
- `helpers.py` — `ema()`, `compute_vwap()`, `compute_atr()`, `format_price()`

### Strategy pipeline

Gate evaluation is sequential (pass/fail, no weighted scores):

1. **Gate 1** (`strategy/scanner/gate1.py`) — Funding pressure: APR ≥ 50%, 6 of last 8 hours positive, oracle premium ≥ 0.02%
2. **Gate 2** (`strategy/scanner/gate2.py`) — OI divergence: OI up ≥5% over 4h while price flat (<0.5%)
3. **Gate 3** (`strategy/scanner/gate3.py`) — Price structure: within 1% of 4h max sampled mark price; failed-breakout confirmation

After all three gates: **Regime filter** (`strategy/regime_filter.py`) sets `Regime` = `NORMAL | REDUCED | DISABLED` based on BTC EMA20 slope and alt breadth.

**Trigger engine** fires when:
- Delta z-score < −2.0 (trade flow imbalance)
- Bid depth drops >25% in 30s
- Price still ≤1.5% drift from trigger price

Squeeze score from `LiquidationModel` can block (score ≥5) or size-reduce (score ≥3, 40% size) entries.

### OMS / execution rules

- One API wallet, one atomic nonce counter per process
- Batch outbound actions every 100 ms; ALO-only batches separate from IOC/GTC
- Primary entry: passive IOC limit 0.05% above mid; fallback: aggressive IOC 0.5% below mid
- TP at 1.5R (close 50%), final exit at 2.5R; stop is ATR-based (2× high-vol, 3× normal)
- `scheduleCancel` is the exchange-native dead-man switch; refresh it regularly

### Key invariants

- `MAX_POSITIONS_PER_SECTOR = 2`; `DAILY_LOSS_KILL_PCT = 3%`; `DAILY_LOSS_DISABLE_PCT = 5%`
- Price formatting: ≤5 significant figures, respects Hyperliquid tick/lot rules
- Exchange limits: 10 WS connections per IP, 1,000 subscriptions, 2,000 messages/min

### Data ingestion rules (PRD Section 2.6) — violations are silent and live-critical

**Rule 1 — Type coercion**: Always wrap REST/WS numeric fields in `float()` or `int()` at the ingestion boundary. Exchange responses mix strings and numbers inconsistently.

**Rule 2 — Funding division**: The exchange `fundingRate` field is an 8-hour basis rate. Divide by 8 before appending to `funding_series`. Gate 1 APR annualises from the per-hour value.

**Rule 3 — Two separate ingestion paths; never mix them**:
- **Path A** (funding): REST only via `refresh_funding_from_rest()`. Never write to `funding_series` from a WS message.
- **Path B** (OI / price / premium): `ingest_asset_ctx()`, throttled at 60s for OI+price, 300s for premium.

**Premium field sourcing**:
- `metaAndAssetCtxs` REST: use `float(ctx["premium"])` directly.
- `activeAssetCtx` WS: field absent — derive as `(markPx - oraclePx) / oraclePx`.

### Trigger expiry (PRD Section 7.3)

A trigger is invalid (skip entry, do not fallback) if either condition holds:
- Price has drifted > 1.5% from `trigger_price` (`TRIGGER_STALE_DRIFT_MAX`)
- Delta z-score has recovered to ≥ −1.5 (`DELTA_ZSCORE_EXPIRY`)

Check before primary IOC **and** again before fallback IOC.

### IOC rejection handling (PRD Section 8.3)

Each rejection reason requires different handling — do not treat all equally:

| reason | action |
|---|---|
| `iocCancelRejected` | benign — proceed to fallback |
| `minTradeNtlRejected` | fatal — abort signal (sizing bug) |
| `tickRejected` | fatal — abort signal (price formatting bug) |
| `oracleRejected` | fatal — skip signal (price too far from oracle) |
| anything else | log warning — fallback may still work |

### WS connection rules (PRD Section 14)

- **Ping interval**: send every 45s. The server closes connections silent after 60s with no activity.
- **On promotion** from warm-up → active watch list: add `l2Book` subscription only. Do NOT re-subscribe warm-up feeds (trades, candle, activeAssetCtx) — duplicate subscriptions count against the 1,000-subscription limit.
- **On demotion**: unsubscribe ALL feeds for the coin.
- **On reconnect**: set `has_data_gap = True` immediately → resubscribe → refresh funding → set `has_data_gap = False`. Also reset `delta_ready = False` so the trigger engine rebuilds its 10-window baseline before firing.

### Ambiguity resolution (PRD Section 15.2)

When the PRD is ambiguous, apply in order:
1. Safety > accuracy — if unsure whether to enter, do not.
2. Use most conservative threshold for entry gates.
3. Use most generous threshold for exit and stop rules.
4. If `has_data_gap is True`, skip all entry decisions.
5. Never infer missing data — raise or skip.
6. When in doubt about data type, always `float()` before arithmetic.

## Runtime Configuration

Copy `.env.example` to `.env`. Variables:

```
# Required
HL_API_WALLET_ADDRESS, HL_PRIVATE_KEY
HL_TESTNET          # true | false (default false)

# Risk
ACCOUNT_EQUITY_USD           # default 10000
MAX_CONCURRENT_POSITIONS     # default 3
RISK_PER_TRADE_PCT           # default 0.01
DAILY_LOSS_KILL_PCT          # default 0.03
DAILY_LOSS_DISABLE_PCT       # default 0.05

# Safety
DRY_RUN             # default true — logs triggers but places NO orders.
                    # Only set false after 48-72h dry run confirms signal frequency.

# Operational
LOG_LEVEL           # default INFO (set DEBUG for per-coin gate detail)
LOG_DIR             # default logs/
```

## Scripts

```bash
# Verify exchange signing + order parsing (no real orders placed)
python scripts/smoke_test.py

# Debug Gate 1 for a specific coin and date range
python scripts/debug_gates.py --coin WIF --start 2024-03-01 --end 2024-06-01

# Analyse pm2 logs from stdin or file
pm2 logs altshortbot --lines 10000 --nocolor | python scripts/analyze_logs.py
python scripts/analyze_logs.py logs/paper_trade.log
```

## Development Phases

- **Phase 1:** Backtest gates only — `scripts/bootstrap.py` ✅
- **Phase 2:** Paper trade — `scripts/paper_trade.py` ✅ (current)
- **Phase 3:** Small live (max 3 positions) — `scripts/live_trade.py`
- **Phase 4:** Scale
