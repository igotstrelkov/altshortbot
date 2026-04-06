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

Only `shared/` exists. Everything else is greenfield:

| Package | Status |
|---|---|
| `shared/constants.py`, `types.py`, `state_factory.py` | ✅ Complete |
| `shared/helpers.py` | ✅ Complete |
| `strategy/trigger/vwap_buffer.py`, `delta_aggregator.py` | ✅ Complete |
| `strategy/liq_model.py` | ✅ Complete |
| `market_data/universe_snapshotter.py` | ✅ Complete |
| `shared/logging_config.py` | ✅ Complete |
| `strategy/scanner/` (gate1, gate2, gate3, seed_rest, universe_scanner, promote_watchlist) | ✅ Complete |
| `strategy/regime_filter.py` | ✅ Complete |
| `strategy/trigger/trigger_engine.py` | ✅ Complete |
| `market_data/tiered_streamer.py` | ✅ Complete |
| `market_data/state_normaliser.py` | ✅ Complete |
| `oms/price_formatter.py`, `order_parser.py`, `ioc_entry.py`, `execution_adapter.py` | ✅ Complete |
| `risk/daily_loss_tracker.py`, `correlation_filter.py`, `watchdog.py`, `portfolio_controller.py` | ✅ Complete |
| `oms/execution_adapter.py` (ExchangeAdapter + stub) | ✅ Complete |
| `market_data/ws_manager.py` | ✅ Complete |
| `main.py`, `config/settings.py`, `scripts/paper_trade.py`, `scripts/live_trade.py` | ✅ Complete |
| `scripts/` | ❌ Not yet created |
| `tests/unit/` | ✅ Exists (test_helpers.py, test_vwap_delta.py, test_liq_model.py, test_ingestion.py, test_gates.py, test_regime.py, test_trigger.py, test_execution.py) |

All three helper objects (`VwapBuffer`, `DeltaAggregator`, `LiquidationModel`) are now wired into `state_factory.py` as real instances.
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
- `helpers.py` — to be implemented: `ema()`, `compute_vwap()`, `compute_atr()`, `format_price()`

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

## Runtime Configuration

Copy `.env.example` to `.env`. Required variables:

```
HL_API_WALLET_ADDRESS, HL_PRIVATE_KEY, HL_TESTNET
ACCOUNT_EQUITY_USD, MAX_CONCURRENT_POSITIONS
RISK_PER_TRADE_PCT, DAILY_LOSS_KILL_PCT, DAILY_LOSS_DISABLE_PCT
LOG_LEVEL, LOG_DIR
```

## Implementation Order

Follow PRD Section 15.1 exactly:

```
shared/helpers.py
strategy/trigger/vwap_buffer.py
strategy/trigger/delta_aggregator.py
strategy/liq_model.py
market_data/universe_snapshotter.py
strategy/scanner/gate1.py, gate2.py, gate3.py, seed_rest.py
strategy/scanner/universe_scanner.py + promote_watchlist.py
strategy/regime_filter.py
market_data/tiered_streamer.py
...
```

## Development Phases

- **Phase 1:** Backtest gates only — `scripts/bootstrap.py`
- **Phase 2:** Paper trade — `scripts/paper_trade.py`
- **Phase 3:** Small live (max 3 positions) — `scripts/live_trade.py`
- **Phase 4:** Scale
