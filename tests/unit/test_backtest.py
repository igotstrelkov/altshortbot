"""Unit tests for the backtester components."""
from __future__ import annotations

import pytest

from backtest.metrics import compute_metrics
from backtest.slippage_model import apply_entry_slippage, apply_exit_slippage
from shared.constants import SLIPPAGE_MODEL_PCT


# ── Slippage model ────────────────────────────────────────────────────────────

class TestSlippageModel:
    def test_entry_fill_below_mid(self) -> None:
        mid = 1000.0
        fill = apply_entry_slippage(mid)
        assert fill < mid
        assert fill == pytest.approx(mid * (1 - SLIPPAGE_MODEL_PCT))

    def test_exit_fill_above_mid(self) -> None:
        mid = 1000.0
        fill = apply_exit_slippage(mid)
        assert fill > mid
        assert fill == pytest.approx(mid * (1 + SLIPPAGE_MODEL_PCT))

    def test_entry_and_exit_symmetric(self) -> None:
        mid = 2000.0
        assert apply_exit_slippage(mid) - mid == pytest.approx(mid - apply_entry_slippage(mid))


# ── Metrics ───────────────────────────────────────────────────────────────────

def _make_trade(
    entry_px: float,
    exit_px: float,
    size_coins: float = 1.0,
    funding: float = 0.0,
    stop_pct: float = 0.02,
    entry_time: int = 0,
    exit_time: int = 86_400_000,  # 1 day later in ms
) -> dict:
    return {
        "entry_px": entry_px,
        "exit_px": exit_px,
        "size_coins": size_coins,
        "funding_collected_usd": funding,
        "entry_time": entry_time,
        "exit_time": exit_time,
        "stop_distance_pct": stop_pct,
    }


class TestComputeMetrics:
    def test_empty_trades(self) -> None:
        m = compute_metrics([], 10_000.0)
        assert m["total_trades"] == 0
        assert m["sharpe_ratio"] == 0.0
        assert m["win_rate"] == 0.0

    def test_single_trade_sharpe_zero(self) -> None:
        trades = [_make_trade(100.0, 90.0)]
        m = compute_metrics(trades, 10_000.0)
        assert m["sharpe_ratio"] == 0.0  # fewer than 2 trades

    def test_win_rate_correct(self) -> None:
        trades = [
            _make_trade(100.0, 90.0),   # winner (short: exit < entry)
            _make_trade(100.0, 110.0),  # loser
            _make_trade(100.0, 100.0),  # breakeven
        ]
        m = compute_metrics(trades, 10_000.0)
        assert m["total_trades"] == 3
        assert m["win_rate"] == pytest.approx(1 / 3)

    def test_total_pnl_pct_correct(self) -> None:
        # 1 coin short from 100 to 90: pnl = $10; initial equity = $1000
        trades = [_make_trade(100.0, 90.0, size_coins=1.0)]
        m = compute_metrics(trades, 1_000.0)
        assert m["total_pnl_pct"] == pytest.approx(10.0 / 1000.0)

    def test_funding_added_to_pnl(self) -> None:
        # Break-even price move but $5 funding collected
        trades = [_make_trade(100.0, 100.0, size_coins=1.0, funding=5.0)]
        m = compute_metrics(trades, 1_000.0)
        assert m["total_pnl_pct"] == pytest.approx(5.0 / 1_000.0)

    def test_max_drawdown_peak_to_trough(self) -> None:
        # Sequence: +$100, -$200, +$50
        # Cumulative:  100,  -100,  -50
        # Peak = 100, trough = -100 → drawdown = 200
        trades = [
            _make_trade(200.0, 100.0, size_coins=1.0, entry_time=0,       exit_time=86_400_000),
            _make_trade(100.0, 300.0, size_coins=1.0, entry_time=86_400_000, exit_time=172_800_000),
            _make_trade(200.0, 150.0, size_coins=1.0, entry_time=172_800_000, exit_time=259_200_000),
        ]
        m = compute_metrics(trades, 10_000.0)
        assert m["max_drawdown"] == pytest.approx(200.0)

    def test_max_drawdown_monotone_gains(self) -> None:
        trades = [
            _make_trade(100.0, 90.0, entry_time=0, exit_time=86_400_000),
            _make_trade(100.0, 90.0, entry_time=86_400_000, exit_time=172_800_000),
        ]
        m = compute_metrics(trades, 10_000.0)
        assert m["max_drawdown"] == 0.0  # no drawdown on consecutive wins

    def test_expectancy_r_correct(self) -> None:
        # +2R and -1R → expectancy = 0.5R
        trades = [
            _make_trade(100.0, 96.0, stop_pct=0.02,  # pnl_pct=0.04, r=2.0
                        entry_time=0, exit_time=86_400_000),
            _make_trade(100.0, 102.0, stop_pct=0.02,  # pnl_pct=-0.02, r=-1.0
                        entry_time=86_400_000, exit_time=172_800_000),
        ]
        m = compute_metrics(trades, 10_000.0)
        assert m["expectancy_r"] == pytest.approx(0.5)
