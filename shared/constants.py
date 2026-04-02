"""
All named constants for altshortbot.
Imported by every module. Never hardcode values inline.
See PRD Section 15.3 for rationale on each value.
"""

# ── Data ingestion ────────────────────────────────────────────────
FUNDING_API_TO_HOURLY_DIVISOR   = 8          # fundingHistory returns 8h basis rate
FUNDING_REFRESH_INTERVAL_S      = 3600       # re-bootstrap funding for full universe hourly
FUNDING_BOOTSTRAP_STAGGER_S     = 0.2        # delay between per-coin fundingHistory requests

# ── Gate 1 — Funding pressure ─────────────────────────────────────
GATE1_FUNDING_APR_THRESHOLD     = 0.50       # 50% annualised
GATE1_ANNUALISE_MULTIPLIER      = 8760       # hours per year (applied to per-hour rate)
GATE1_MIN_POSITIVE_HOURS        = 6          # of last 8 must be positive
GATE1_PREMIUM_FLOOR             = 0.0002     # 0.02% oracle premium minimum

# ── Gate 2 — OI divergence ────────────────────────────────────────
GATE2_OI_CHANGE_THRESHOLD       = 0.05       # 5% OI increase over 4h
GATE2_PRICE_CHANGE_MAX          = 0.005      # 0.5% max price move over same window
GATE2_LOOKBACK_MINUTES          = 245        # 240 lookback + 5 smoothing buffer
GATE2_OI_SMOOTH_PERIODS         = 5          # 5-min rolling average each end

# ── Gate 3 — Price structure ──────────────────────────────────────
GATE3_PRICE_FROM_HIGH_MAX       = 0.01       # within 1% of 4h max sampled mark price
FAILED_BREAKOUT_RECOVERY_THRESHOLD = 0.005   # 0.5% below peak to confirm rejection
FAILED_BREAKOUT_LOOKBACK_CANDLES = 24        # 24 × 5m = 2h lookback
GATE3_WARM_UP_S                 = 360        # 6 min post-seed, covers VwapBuffer fill

# ── Liquidation intelligence ──────────────────────────────────────
SQUEEZE_HARD_BLOCK_SCORE        = 5
SQUEEZE_REDUCE_SCORE            = 3
SQUEEZE_REDUCE_MULTIPLIER       = 0.40
SQUEEZE_RISK_RATIO_MAX          = 0.45
SQUEEZE_FUNDING_ELEVATED_APR    = 0.20       # 20% APR floor for funding-drop condition
SQUEEZE_FUNDING_DROP_MIN_PCT    = 0.30       # 30% relative drop triggers +3 score
LIQ_MODEL_AVG_LEVERAGE          = 10.0
LIQ_MODEL_MAX_ENTRIES           = 1440       # 24h of 1-min candles per side
LIQ_CLUSTER_RANGE_PCT           = 0.03       # 3% above/below price for cluster calc

# ── Regime filter ─────────────────────────────────────────────────
BTC_SLOPE_DISABLE_THRESHOLD     = 0.015      # +1.5% EMA20 5h slope → DISABLED
BTC_SLOPE_REDUCE_THRESHOLD      = 0.005      # +0.5%                 → REDUCED
ALT_BREADTH_DISABLE_THRESHOLD   = 0.60       # >60% of watchlist up >2% → DISABLED
ALT_BREADTH_UP_PCT              = 0.02       # 2% 1h move threshold for breadth check
REGIME_MIN_BTC_HISTORY          = 55         # minimum 1h closes needed for EMA50
REGIME_CANDLE_HISTORY_HOURS     = 60

# ── Trigger engine ────────────────────────────────────────────────
DELTA_ZSCORE_TRIGGER            = -2.0       # primary trigger threshold
DELTA_ZSCORE_EXPIRY             = -1.5       # trigger expires when z-score recovers here
DELTA_COLD_START_PERIODS        = 10         # 60s windows before delta_ready = True
DELTA_WINDOW_S                  = 60
VWAP_BUFFER_WINDOW_S            = 300        # 5-minute rolling VWAP
BID_DEPTH_THIN_THRESHOLD        = 0.25       # 25% depth drop over 30s
BID_DEPTH_WINDOW_S              = 30
TRIGGER_STALE_DRIFT_MAX         = 0.015      # 1.5% price drift since trigger fired

# ── Execution engine ──────────────────────────────────────────────
LIMIT_ORDER_OFFSET              = 0.0005     # 0.05% above mid for passive IOC sell
IOC_AGGRESSIVE_SLIPPAGE_PCT     = 0.005      # 0.5% below mid for aggressive IOC
IOC_EMERGENCY_SLIPPAGE_PCT      = 0.010      # 1.0% for emergency flatten
MAX_SLIPPAGE                    = 0.003      # 0.3% — log warning, keep position
ABORT_SLIPPAGE                  = 0.005      # 0.5% — close immediately
MIN_ORDER_NOTIONAL_USD          = 10.0       # Hyperliquid hard minimum

# ── Risk engine ───────────────────────────────────────────────────
RISK_PER_TRADE_PCT              = 0.01       # 1% of equity per trade
MIN_STOP_DISTANCE_PCT           = 0.005      # 0.5% floor on stop distance
ATR_PERIOD                      = 14
ATR_LOOKBACK_CANDLES            = 15         # period + 1
ATR_MULTIPLIER_HIGH_VOL         = 2.0
ATR_MULTIPLIER_NORMAL           = 3.0
HIGH_VOL_1H_RANGE_PCT           = 0.03       # 3% 1h range triggers high-vol multiplier
TP1_R_TARGET                    = 1.5
TP2_R_TARGET                    = 2.5
TP1_CLOSE_FRACTION              = 0.50
DAILY_LOSS_KILL_PCT             = 0.03       # 3% — kill switch
DAILY_LOSS_DISABLE_PCT          = 0.05       # 5% — 24h disable
FUNDING_EXIT_PNL_THRESHOLD_R    = 0.5        # exit on negative funding if PnL < 0.5R
MAX_POSITIONS_PER_SECTOR        = 2

# ── Asset universe ────────────────────────────────────────────────
MIN_UNIVERSE_DAILY_VOL_USD      = 5_000_000
MIN_UNIVERSE_OI_USD             = 2_000_000
MIN_UNIVERSE_MIN_LEVERAGE       = 5
MIN_UNIVERSE_FUNDING_HISTORY_DAYS = 7
NEW_ASSET_BLACKOUT_HOURS        = 48

# ── Backtesting ───────────────────────────────────────────────────
SLIPPAGE_MODEL_PCT              = 0.001      # 0.1% each side

# ── WebSocket ─────────────────────────────────────────────────────
WS_URL                          = "wss://api.hyperliquid.xyz/ws"
WS_RECONNECT_MAX_DELAY_S        = 60
WS_PING_INTERVAL_S              = 45         # send ping before 60s server close

# ── Process health ────────────────────────────────────────────────
HEARTBEAT_TIMEOUT_S             = 300        # 5 min without beat → emergency flatten
HEARTBEAT_BEAT_INTERVAL_S       = 30
