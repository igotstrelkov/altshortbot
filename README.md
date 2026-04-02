# AltShortBot

Automated short-selling bot on Hyperliquid perpetuals.

## Architecture

Four services, all running in a single process for v1:

| Service | Package | Role |
|---|---|---|
| Market Data | `market_data/` | WS streams + REST polling |
| Strategy | `strategy/` | Scanner gates, regime filter, trigger engine |
| OMS | `oms/` | All signed exchange actions |
| Risk | `risk/` | Reconciliation, kill switches, portfolio controls |

Shared utilities (state factory, constants, types, helpers) live in `shared/`.

## Implementation Order

Follow PRD Section 15.1 exactly. Start with the backtester.

```
shared/constants.py      ← already populated
shared/types.py          ← already populated
shared/state_factory.py  ← already populated (stubs for classes)
shared/helpers.py        ← ema(), compute_vwap(), compute_atr(), format_price()
strategy/trigger/vwap_buffer.py
strategy/trigger/delta_aggregator.py
strategy/liq_model.py
market_data/universe_snapshotter.py  ← bootstrap_universe_funding(), refresh_funding_from_rest()
strategy/scanner/gate1.py gate2.py gate3.py seed_rest.py
strategy/scanner/universe_scanner.py  + promote_watchlist.py
strategy/regime_filter.py
market_data/tiered_streamer.py  ← subscribe_warmup_feeds(), subscribe_watchlist_feeds()
...
```

## Setup

```bash
cp .env.example .env   # fill in wallet address + private key
pip install -e ".[dev]"
pytest
```

## Phases

- **Phase 1:** Backtest gates only (`scripts/bootstrap.py`)
- **Phase 2:** Paper trade (`scripts/paper_trade.py`)
- **Phase 3:** Small live (`scripts/live_trade.py`, max 3 positions)
- **Phase 4:** Scale
