"""Unit tests for the risk engine."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from risk.correlation_filter import SECTOR_MAP, correlation_check_passes
from risk.daily_loss_tracker import DailyLossTracker
from risk.portfolio_controller import (
    calculate_position_size,
    calculate_stop_distance,
    check_funding_exit,
)
from shared.constants import (
    MIN_STOP_DISTANCE_PCT,
    SQUEEZE_HARD_BLOCK_SCORE,
    SQUEEZE_REDUCE_MULTIPLIER,
    SQUEEZE_REDUCE_SCORE,
)


# ── DailyLossTracker ──────────────────────────────────────────────────────────

class TestDailyLossTracker:
    # Pass explicit thresholds so tests are independent of .env overrides.
    KILL = 0.03
    DISABLE = 0.05

    def test_ok_on_small_loss(self) -> None:
        tracker = DailyLossTracker(10_000.0, kill_pct=self.KILL, disable_pct=self.DISABLE)
        result = tracker.record_close(-100.0)  # 1% — below KILL threshold
        assert result == "OK"
        assert tracker.is_trading_allowed()

    def test_kill_at_kill_threshold(self) -> None:
        # 3% of 10k = $300
        tracker = DailyLossTracker(10_000.0, kill_pct=self.KILL, disable_pct=self.DISABLE)
        result = tracker.record_close(-300.0)
        assert result == "KILL"
        assert tracker.kill_active
        assert not tracker.is_trading_allowed()

    def test_disable_at_disable_threshold(self) -> None:
        # 5% of 10k = $500
        tracker = DailyLossTracker(10_000.0, kill_pct=self.KILL, disable_pct=self.DISABLE)
        result = tracker.record_close(-500.0)
        assert result == "DISABLE"
        assert tracker.disable_until is not None
        assert not tracker.is_trading_allowed()

    def test_kill_accumulates_across_multiple_closes(self) -> None:
        tracker = DailyLossTracker(10_000.0, kill_pct=self.KILL, disable_pct=self.DISABLE)
        tracker.record_close(-100.0)  # 1%
        tracker.record_close(-100.0)  # 2%
        result = tracker.record_close(-100.0)  # 3% → KILL
        assert result == "KILL"

    def test_kill_resets_at_midnight(self) -> None:
        tracker = DailyLossTracker(10_000.0, kill_pct=self.KILL, disable_pct=self.DISABLE)
        tracker.record_close(-300.0)  # triggers KILL
        assert not tracker.is_trading_allowed()

        tomorrow = datetime.utcnow().date() + timedelta(days=1)
        with patch("risk.daily_loss_tracker.datetime") as mock_dt:
            mock_dt.utcnow.return_value = datetime.combine(tomorrow, datetime.min.time())
            assert tracker.is_trading_allowed()
        assert tracker.daily_pnl == 0.0
        assert not tracker.kill_active

    def test_disable_persists_across_midnight(self) -> None:
        tracker = DailyLossTracker(10_000.0, kill_pct=self.KILL, disable_pct=self.DISABLE)
        tracker.record_close(-500.0)  # triggers DISABLE (24h)
        disable_until = tracker.disable_until
        assert disable_until is not None

        # Simulate midnight passing — 1 second after midnight
        tomorrow = datetime.utcnow().date() + timedelta(days=1)
        just_after_midnight = datetime.combine(tomorrow, datetime.min.time()) + timedelta(seconds=1)
        with patch("risk.daily_loss_tracker.datetime") as mock_dt:
            mock_dt.utcnow.return_value = just_after_midnight
            # disable_until is 24h from original call, so still active after midnight
            assert not tracker.is_trading_allowed()
        # disable_until unchanged
        assert tracker.disable_until == disable_until

    def test_disable_expires_after_24h(self) -> None:
        tracker = DailyLossTracker(10_000.0, kill_pct=self.KILL, disable_pct=self.DISABLE)
        tracker.record_close(-500.0)
        # 25 hours later
        future = datetime.utcnow() + timedelta(hours=25)
        with patch("risk.daily_loss_tracker.datetime") as mock_dt:
            mock_dt.utcnow.return_value = future
            assert tracker.is_trading_allowed()


# ── correlation_filter ────────────────────────────────────────────────────────

class TestCorrelationFilter:
    def test_passes_when_sector_empty(self) -> None:
        assert correlation_check_passes("ETH", []) is True

    def test_passes_when_sector_has_one(self) -> None:
        # MAX_POSITIONS_PER_SECTOR = 2, so 1 existing is fine
        assert correlation_check_passes("ETH", ["BTC"]) is True

    def test_blocks_when_sector_full(self) -> None:
        # BTC + ETH both L1 → adding SOL would be 3rd in L1
        assert correlation_check_passes("SOL", ["BTC", "ETH"]) is False

    def test_passes_for_different_sector(self) -> None:
        # BTC + ETH in L1, adding DOGE (Meme) is fine
        assert correlation_check_passes("DOGE", ["BTC", "ETH"]) is True

    def test_unknown_coin_defaults_to_other(self) -> None:
        assert "UNKNOWN" not in SECTOR_MAP
        # Two unknowns already in 'Other' → blocks third
        assert correlation_check_passes("NEWCOIN", ["COIN_A", "COIN_B"]) is False

    def test_single_unknown_passes(self) -> None:
        assert correlation_check_passes("NEWCOIN", ["COIN_A"]) is True

    def test_sector_map_contains_expected_coins(self) -> None:
        assert SECTOR_MAP["BTC"] == "L1"
        assert SECTOR_MAP["DOGE"] == "Meme"
        assert SECTOR_MAP["LINK"] == "Oracle"
        assert SECTOR_MAP["UNI"] == "DeFi"
        assert SECTOR_MAP["FET"] == "AI"


# ── calculate_stop_distance ───────────────────────────────────────────────────

class TestCalculateStopDistance:
    def test_atr_stop_used_when_swing_high_below_entry(self) -> None:
        # swing_high <= entry → always use ATR stop
        dist = calculate_stop_distance(
            entry_price=1000.0,
            atr_14=10.0,
            swing_high_price=990.0,  # below entry
            high_volatility=False,
        )
        # ATR stop: 1000 + 3*10 = 1030 → dist = 30/1000 = 3%
        assert dist == pytest.approx(0.03)

    def test_atr_stop_used_when_zero_atr(self) -> None:
        dist = calculate_stop_distance(
            entry_price=1000.0,
            atr_14=0.0,
            swing_high_price=1020.0,
            high_volatility=False,
        )
        # atr_14==0 → stop_price = entry + 0 = 1000 → dist = 0 → floored
        assert dist == pytest.approx(MIN_STOP_DISTANCE_PCT)

    def test_swing_high_used_when_tighter(self) -> None:
        # ATR stop: 1000 + 3*20 = 1060 → 6%
        # swing_high: 1010 → 1%
        # min(1060, 1010) = 1010 → dist = 1%
        dist = calculate_stop_distance(
            entry_price=1000.0,
            atr_14=20.0,
            swing_high_price=1010.0,
            high_volatility=False,
        )
        assert dist == pytest.approx(0.01)

    def test_atr_stop_used_when_tighter_than_swing_high(self) -> None:
        # ATR stop: 1000 + 3*5 = 1015 → 1.5%
        # swing_high: 1050 → 5%
        # min(1015, 1050) = 1015 → dist = 1.5%
        dist = calculate_stop_distance(
            entry_price=1000.0,
            atr_14=5.0,
            swing_high_price=1050.0,
            high_volatility=False,
        )
        assert dist == pytest.approx(0.015)

    def test_high_volatility_uses_2x_multiplier(self) -> None:
        # ATR stop: 1000 + 2*10 = 1020 → 2%
        dist = calculate_stop_distance(
            entry_price=1000.0,
            atr_14=10.0,
            swing_high_price=900.0,
            high_volatility=True,
        )
        assert dist == pytest.approx(0.02)

    def test_floor_applied_when_distance_too_small(self) -> None:
        # ATR stop: 1000 + 3*1 = 1003 → 0.3% < MIN_STOP_DISTANCE_PCT (0.5%)
        dist = calculate_stop_distance(
            entry_price=1000.0,
            atr_14=1.0,
            swing_high_price=900.0,
            high_volatility=False,
        )
        assert dist == pytest.approx(MIN_STOP_DISTANCE_PCT)


# ── calculate_position_size ───────────────────────────────────────────────────

class TestCalculatePositionSize:
    # Pass explicit risk_pct so tests are independent of .env overrides.
    R = 0.01

    def test_normal_regime_correct_notional(self) -> None:
        # $10k equity, 1% risk = $100 budget, 2% stop → $100 / 0.02 = $5000 notional
        result = calculate_position_size(10_000.0, "NORMAL", 0, 0.02, risk_pct=self.R)
        assert result == pytest.approx(5000.0)

    def test_reduced_regime_halves_risk_budget(self) -> None:
        # $10k equity, 0.5% effective risk = $50, 2% stop → $2500 notional
        result = calculate_position_size(10_000.0, "REDUCED", 0, 0.02, risk_pct=self.R)
        assert result == pytest.approx(2500.0)

    def test_disabled_regime_returns_zero(self) -> None:
        result = calculate_position_size(10_000.0, "DISABLED", 0, 0.02, risk_pct=self.R)
        assert result == pytest.approx(0.0)

    def test_squeeze_hard_block_returns_zero(self) -> None:
        result = calculate_position_size(10_000.0, "NORMAL", SQUEEZE_HARD_BLOCK_SCORE, 0.02, risk_pct=self.R)
        assert result == pytest.approx(0.0)

    def test_squeeze_reduce_applies_multiplier(self) -> None:
        base = calculate_position_size(10_000.0, "NORMAL", 0, 0.02, risk_pct=self.R)
        reduced = calculate_position_size(10_000.0, "NORMAL", SQUEEZE_REDUCE_SCORE, 0.02, risk_pct=self.R)
        assert reduced == pytest.approx(base * SQUEEZE_REDUCE_MULTIPLIER)

    def test_raises_on_non_positive_stop_distance(self) -> None:
        with pytest.raises(ValueError):
            calculate_position_size(10_000.0, "NORMAL", 0, 0.0, risk_pct=self.R)
        with pytest.raises(ValueError):
            calculate_position_size(10_000.0, "NORMAL", 0, -0.01, risk_pct=self.R)

    def test_unknown_regime_returns_zero(self) -> None:
        result = calculate_position_size(10_000.0, "UNKNOWN", 0, 0.02, risk_pct=self.R)
        assert result == pytest.approx(0.0)


# ── check_funding_exit ────────────────────────────────────────────────────────

class TestCheckFundingExit:
    def test_exits_on_negative_funding_low_pnl(self) -> None:
        # negative funding and PnL < threshold (0.5R)
        assert check_funding_exit(-0.001, 0.3) is True

    def test_no_exit_on_positive_funding(self) -> None:
        assert check_funding_exit(0.001, 0.3) is False

    def test_no_exit_when_pnl_above_threshold(self) -> None:
        # negative funding but PnL >= 0.5R → hold
        assert check_funding_exit(-0.001, 0.6) is False

    def test_no_exit_at_zero_funding(self) -> None:
        assert check_funding_exit(0.0, 0.3) is False

    def test_exits_exactly_at_threshold(self) -> None:
        # pnl_r == FUNDING_EXIT_PNL_THRESHOLD_R (0.5) → NOT < threshold → no exit
        assert check_funding_exit(-0.001, 0.5) is False
