"""
Backtesting engine.
Simulates the Gate 1 → Gate 3 pipeline on historical candle + funding data.

NOTE: Gate 2 (OI divergence) is intentionally skipped. Hyperliquid does not
expose historical open-interest data via any candle or snapshot endpoint —
it is only available in real-time via metaAndAssetCtxs. The backtest pipeline
is therefore: Gate 1 (funding pressure) → Gate 3 (price structure).

Regime filter IS included: BTC 1h closes loaded from Binance. Alt-breadth
component is skipped (no historical multi-coin breadth available).
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

import structlog

from backtest.data_loader import load_candles, load_funding_history
from backtest.metrics import compute_metrics
from backtest.slippage_model import apply_entry_slippage, apply_exit_slippage
from risk.portfolio_controller import (
    calculate_position_size,
    calculate_stop_distance,
    check_funding_exit,
)
from shared.constants import (
    TP1_CLOSE_FRACTION,
    TP1_R_TARGET,
    TP2_R_TARGET,
)
from shared.helpers import compute_atr
from strategy.regime_filter import regime_filter
from strategy.scanner.gate1 import gate1_passes
from strategy.scanner.gate3 import gate3_score

log = structlog.get_logger()

_HOUR_MS = 3_600_000


def _floor_to_hour(ts_ms: int) -> int:
    return (ts_ms // _HOUR_MS) * _HOUR_MS


def _date_to_ms(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


class BacktestEngine:
    def __init__(
        self,
        coins: list[str],
        start_date: str,
        end_date: str,
        initial_equity: float = 10_000.0,
        risk_pct: float = 0.01,
    ) -> None:
        self.coins = coins
        self.start_ms = _date_to_ms(start_date)
        self.end_ms = _date_to_ms(end_date)
        self.initial_equity = initial_equity
        self.risk_pct = risk_pct

    async def run(self) -> dict:
        # Load BTC 1h closes once — shared regime filter across all coins
        log.info("backtest_btc_load_start")
        df_btc_1h = await load_candles("BTC", "1h", self.start_ms, self.end_ms)
        btc_1h_by_hour: dict[int, float] = {}
        if not df_btc_1h.empty:
            for _, row in df_btc_1h.iterrows():
                btc_1h_by_hour[_floor_to_hour(int(row["time"]))] = float(row["close"])
        log.info("backtest_btc_load_done", hours=len(btc_1h_by_hour))

        per_coin: dict[str, dict] = {}
        all_trades: list[dict] = []

        for coin in self.coins:
            log.info("backtest_coin_start", coin=coin)
            trades = await self._simulate_coin(coin, btc_1h_by_hour)
            per_coin[coin] = compute_metrics(trades, self.initial_equity)
            all_trades.extend(trades)
            log.info(
                "backtest_coin_done",
                coin=coin,
                trades=per_coin[coin]["total_trades"],
                sharpe=round(per_coin[coin]["sharpe_ratio"], 2),
            )

        return {
            "per_coin": per_coin,
            "aggregate": compute_metrics(all_trades, self.initial_equity),
        }

    async def _simulate_coin(self, coin: str, btc_1h_by_hour: dict[int, float]) -> list[dict]:
        # ── Load data ────────────────────────────────────────────────
        df_1m = await load_candles(coin, "1m", self.start_ms, self.end_ms)
        df_5m = await load_candles(coin, "5m", self.start_ms, self.end_ms)
        df_fund = await load_funding_history(coin, self.start_ms, self.end_ms)

        if df_1m.empty:
            log.warning("no_candles", coin=coin)
            return []

        # ── Build lookup structures ───────────────────────────────────
        funding_by_hour: dict[int, float] = {}
        premium_by_hour: dict[int, float] = {}
        for _, row in df_fund.iterrows():
            key = _floor_to_hour(int(row["time"]))
            funding_by_hour[key] = float(row["funding_rate"])
            premium_by_hour[key] = float(row["premium"])

        candles_5m_by_time: dict[int, dict] = {
            int(row["time"]): row.to_dict() for _, row in df_5m.iterrows()
        }

        # ── Rolling series ────────────────────────────────────────────
        price_series: deque[float] = deque(maxlen=245)
        high_series_5m: deque[float] = deque(maxlen=24)
        low_series_5m: deque[float] = deque(maxlen=24)
        close_series_5m: deque[float] = deque(maxlen=24)
        funding_series: deque[float] = deque(maxlen=48)
        premium_series: deque[float] = deque(maxlen=12)
        btc_closes_1h: deque[float] = deque(maxlen=60)

        # ── Simulation state ──────────────────────────────────────────
        position: dict | None = None
        trades: list[dict] = []
        last_hour_key: int = -1
        current_regime: str = "DISABLED"  # conservative until BTC history builds

        bars = df_1m.to_dict("records")

        for i, bar in enumerate(bars):
            t = int(bar["time"])
            close = float(bar["close"])

            # a. Price series
            price_series.append(close)

            # b. 5m series — append on every completed 5m candle
            if i % 5 == 4:
                candle_open_time = t - 4 * 60 * 1000
                c5 = candles_5m_by_time.get(candle_open_time)
                if c5:
                    high_series_5m.append(float(c5["high"]))
                    low_series_5m.append(float(c5["low"]))
                    close_series_5m.append(float(c5["close"]))

            # c/d/e. Funding, premium, BTC close + regime — once per hour
            hour_key = _floor_to_hour(t)
            if hour_key != last_hour_key:
                if hour_key in funding_by_hour:
                    funding_series.append(funding_by_hour[hour_key])
                    premium_series.append(premium_by_hour.get(hour_key, 0.0))
                if hour_key in btc_1h_by_hour:
                    btc_closes_1h.append(btc_1h_by_hour[hour_key])
                    current_regime = regime_filter(list(btc_closes_1h), [], {})
                last_hour_key = hour_key

            # ── Exit check (before entry) ─────────────────────────────
            if position is not None:
                pos = position
                current_px = close
                size_coins = pos["size_coins"]

                # Accumulate funding (per-hour rate × size × price)
                pos["funding_collected_usd"] += (
                    funding_by_hour.get(hour_key, 0.0) * size_coins * current_px
                )

                # P&L in R for funding exit check
                pnl_pct = (pos["entry_px"] - current_px) / pos["entry_px"]
                pnl_r = pnl_pct / pos["stop_distance_pct"] if pos["stop_distance_pct"] > 0 else 0.0

                # TP1 — close half at 1.5R
                if not pos["tp1_closed"] and current_px <= pos["tp1_px"]:
                    pos["tp1_closed"] = True
                    pos["size_coins"] *= TP1_CLOSE_FRACTION
                    log.debug("tp1_hit", coin=coin, px=current_px)

                # Exit conditions (full close)
                exit_reason: str | None = None
                if current_px >= pos["stop_px"]:
                    exit_reason = "stop"
                elif current_px <= pos["tp2_px"]:
                    exit_reason = "tp2"
                elif check_funding_exit(funding_by_hour.get(hour_key, 0.0), pnl_r):
                    exit_reason = "funding_exit"

                if exit_reason:
                    exit_px = apply_exit_slippage(current_px)
                    trades.append(
                        {
                            "entry_px": pos["entry_px"],
                            "exit_px": exit_px,
                            "size_coins": pos["size_coins"],
                            "funding_collected_usd": pos["funding_collected_usd"],
                            "entry_time": pos["entry_time"],
                            "exit_time": t,
                            "stop_distance_pct": pos["stop_distance_pct"],
                        }
                    )
                    log.debug("trade_closed", coin=coin, reason=exit_reason, px=exit_px)
                    position = None
                    continue

            # ── Gate evaluation ───────────────────────────────────────
            if position is not None:
                continue  # already in a position

            if current_regime == "DISABLED":
                continue

            gate1 = gate1_passes(funding_series, premium_series)
            if not gate1:
                continue

            # Gate 2 skipped — no historical OI available
            g3 = gate3_score(price_series, high_series_5m, close_series_5m, vwap_5m=0.0)
            if g3 < 2:
                continue

            # ── Entry ─────────────────────────────────────────────────
            entry_px = apply_entry_slippage(close)

            atr_14 = compute_atr(high_series_5m, low_series_5m, close_series_5m)
            prices = list(price_series)
            swing_high = max(prices[-15:]) if len(prices) >= 15 else close

            stop_distance_pct = calculate_stop_distance(
                entry_px, atr_14, swing_high, high_volatility=False
            )
            size_usd = calculate_position_size(
                self.initial_equity, current_regime, squeeze_score=0,
                stop_distance_pct=stop_distance_pct,
                risk_pct=self.risk_pct,
            )
            if size_usd == 0:
                continue

            size_coins = size_usd / entry_px
            stop_px = entry_px * (1 + stop_distance_pct)
            tp1_px = entry_px * (1 - TP1_R_TARGET * stop_distance_pct)
            tp2_px = entry_px * (1 - TP2_R_TARGET * stop_distance_pct)

            position = {
                "entry_px": entry_px,
                "size_coins": size_coins,
                "stop_px": stop_px,
                "tp1_px": tp1_px,
                "tp2_px": tp2_px,
                "entry_time": t,
                "stop_distance_pct": stop_distance_pct,
                "tp1_closed": False,
                "funding_collected_usd": 0.0,
            }
            log.debug("trade_opened", coin=coin, entry_px=entry_px, stop_pct=round(stop_distance_pct, 4))

        # ── Force-close any open position at end of data ──────────────
        if position is not None and bars:
            last_close = float(bars[-1]["close"])
            exit_px = apply_exit_slippage(last_close)
            trades.append(
                {
                    "entry_px": position["entry_px"],
                    "exit_px": exit_px,
                    "size_coins": position["size_coins"],
                    "funding_collected_usd": position["funding_collected_usd"],
                    "entry_time": position["entry_time"],
                    "exit_time": int(bars[-1]["time"]),
                    "stop_distance_pct": position["stop_distance_pct"],
                }
            )

        return trades
