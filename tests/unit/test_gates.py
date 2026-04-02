"""Unit tests for the three-gate scanner."""
from collections import deque

from shared.state_factory import create_asset_state
from strategy.scanner.gate1 import gate1_passes
from strategy.scanner.gate2 import gate2_passes
from strategy.scanner.gate3 import failed_breakout_detected, gate3_score
from strategy.scanner.promote_watchlist import reset_warmup_state
from strategy.trigger.delta_aggregator import DeltaAggregator
from strategy.trigger.vwap_buffer import VwapBuffer


# ── Gate 1 ────────────────────────────────────────────────────────────────────

class TestGate1:
    def _funding(self, values: list[float]) -> deque[float]:
        return deque(values, maxlen=48)

    def _premium(self, val: float = 0.0005) -> deque[float]:
        return deque([val], maxlen=12)

    def test_fails_on_empty_series(self) -> None:
        assert gate1_passes(deque(), deque()) is False

    def test_fails_with_fewer_than_8_readings(self) -> None:
        fs = self._funding([0.00006] * 7)
        assert gate1_passes(fs, self._premium()) is False

    def test_fails_if_fewer_than_6_positive(self) -> None:
        # 5 positive, 3 negative
        fs = self._funding([0.00006] * 5 + [-0.00001] * 3)
        assert gate1_passes(fs, self._premium()) is False

    def test_fails_if_annualised_below_threshold(self) -> None:
        # 0.00003 * 8760 = 0.2628 APR < 0.50 threshold
        fs = self._funding([0.00003] * 8)
        assert gate1_passes(fs, self._premium()) is False

    def test_fails_if_premium_below_floor(self) -> None:
        fs = self._funding([0.00006] * 6 + [-0.00001] * 2)
        assert gate1_passes(fs, self._premium(val=0.0001)) is False  # below 0.0002

    def test_passes_on_valid_data(self) -> None:
        # Negative readings first (older), positive last (most recent).
        # recent_8h[-1] = 0.00006, annualised = 0.5256 > 0.50, 6 positive, premium ok.
        fs = self._funding([-0.00001] * 2 + [0.00006] * 6)
        assert gate1_passes(fs, self._premium(val=0.0003)) is True

    def test_validation_script_case(self) -> None:
        # Correct ordering: older negative readings first, recent positives last.
        fs = deque([-0.00001] * 2 + [0.00006] * 6, maxlen=48)
        ps = deque([0.0003], maxlen=12)
        assert gate1_passes(fs, ps) is True


# ── Gate 2 ────────────────────────────────────────────────────────────────────

class TestGate2:
    def _series(self, length: int, value: float = 100.0) -> deque[float]:
        return deque([value] * length, maxlen=245)

    def test_fails_on_short_oi_series(self) -> None:
        assert gate2_passes(deque([1.0] * 244), deque([1.0] * 240)) is False

    def test_fails_on_short_price_series(self) -> None:
        assert gate2_passes(deque([1.0] * 245), deque([1.0] * 239)) is False

    def test_fails_if_oi_flat(self) -> None:
        oi = self._series(245, 1000.0)
        px = self._series(240, 100.0)
        assert gate2_passes(oi, px) is False

    def test_fails_if_price_moved_too_much(self) -> None:
        # OI rises 10% but price also rises 2% → fails price constraint
        oi = deque([1000.0] * 240 + [1100.0] * 5, maxlen=245)
        px = deque([100.0] * 240, maxlen=245)
        # overwrite last value to simulate 2% price move
        px_list = [100.0] * 240
        px_list[-1] = 102.0
        px = deque(px_list, maxlen=245)
        assert gate2_passes(oi, px) is False

    def test_passes_correctly(self) -> None:
        # OI: was 1000, now 1060 (+6%), price flat
        oi_vals = [1000.0] * 240 + [1060.0] * 5
        oi = deque(oi_vals, maxlen=245)
        px = deque([100.0] * 240, maxlen=245)
        assert gate2_passes(oi, px) is True


# ── Gate 3 ────────────────────────────────────────────────────────────────────

class TestGate3Score:
    def test_returns_zero_on_empty_price_series(self) -> None:
        assert gate3_score(deque(), deque(), deque(), 0.0) == 0

    def test_condition1_price_near_4h_high(self) -> None:
        # price = 99.5, high = 100 → (100-99.5)/100 = 0.005 < 0.01 → +1
        prices = deque([100.0] * 239 + [99.5], maxlen=245)
        score = gate3_score(prices, deque(), deque(), 0.0)
        assert score >= 1

    def test_condition1_fails_if_too_far_from_high(self) -> None:
        # price = 98, high = 100 → 2% drop > 1% threshold → no +1
        prices = deque([100.0] * 239 + [98.0], maxlen=245)
        score = gate3_score(prices, deque(), deque(), 0.0)
        assert score == 0

    def test_condition2_price_below_vwap(self) -> None:
        prices = deque([50.0], maxlen=245)
        score = gate3_score(prices, deque(), deque(), vwap_5m=55.0)
        assert score >= 1

    def test_condition2_no_score_if_vwap_zero(self) -> None:
        prices = deque([50.0], maxlen=245)
        score = gate3_score(prices, deque(), deque(), vwap_5m=0.0)
        assert score == 0

    def test_condition3_failed_breakout(self) -> None:
        # peak at index 0, current close well below → +1
        highs = deque([110.0] + [100.0] * 23, maxlen=24)
        closes = deque([100.0] * 23 + [99.0], maxlen=24)
        score = gate3_score(deque([99.0]), highs, closes, 0.0)
        assert score >= 1


class TestFailedBreakoutDetected:
    def test_returns_false_if_insufficient_candles(self) -> None:
        highs = deque([110.0] * 20)
        closes = deque([100.0] * 20)
        assert failed_breakout_detected(highs, closes) is False

    def test_returns_false_if_peak_too_recent(self) -> None:
        # peak at index 22 (lookback-2), which is >= lookback-3 = 21
        highs = deque([100.0] * 22 + [110.0] * 2, maxlen=24)
        closes = deque([99.0] * 24, maxlen=24)
        assert failed_breakout_detected(highs, closes) is False

    def test_detects_failed_breakout(self) -> None:
        # peak at index 0: 110. Current close: 99. Drop = 11/110 = 10% > 0.5%
        highs = deque([110.0] + [100.0] * 23, maxlen=24)
        closes = deque([100.0] * 23 + [99.0], maxlen=24)
        assert failed_breakout_detected(highs, closes) is True

    def test_returns_false_if_recovery_too_small(self) -> None:
        # peak at index 0: 100.1, close: 100.0 → drop = 0.1/100.1 < 0.5%
        highs = deque([100.1] + [99.0] * 23, maxlen=24)
        closes = deque([99.0] * 23 + [100.0], maxlen=24)
        assert failed_breakout_detected(highs, closes) is False


# ── reset_warmup_state ────────────────────────────────────────────────────────

class TestResetWarmupState:
    def test_clears_target_fields(self) -> None:
        state = create_asset_state()
        state["ws_subscribed_at"] = 9999.0
        state["is_on_watchlist"] = True
        state["delta_ready"] = True
        state["price_series"].extend([1.0, 2.0, 3.0])
        state["high_series_5m"].extend([1.0, 2.0])
        state["low_series_5m"].extend([1.0, 2.0])
        state["close_series_5m"].extend([1.0, 2.0])
        state["delta_history"].extend([1.0, 2.0])
        state["trade_delta_60s"] = 5.0
        state["delta_mean_10m"] = 3.0
        state["delta_std_10m"] = 1.0

        reset_warmup_state("ETH", state)

        assert state["ws_subscribed_at"] == 0.0
        assert state["is_on_watchlist"] is False
        assert state["delta_ready"] is False
        assert len(state["price_series"]) == 0
        assert len(state["high_series_5m"]) == 0
        assert len(state["low_series_5m"]) == 0
        assert len(state["close_series_5m"]) == 0
        assert len(state["delta_history"]) == 0
        assert state["trade_delta_60s"] == 0.0
        assert state["delta_mean_10m"] == 0.0
        assert isinstance(state["vwap_buffer"], VwapBuffer)
        assert isinstance(state["delta_aggregator"], DeltaAggregator)

    def test_does_not_clear_funding_or_oi(self) -> None:
        state = create_asset_state()
        state["funding_series"].extend([0.0001, 0.0002])
        state["oi_series"].extend([100.0, 200.0])

        reset_warmup_state("ETH", state)

        assert len(state["funding_series"]) == 2
        assert len(state["oi_series"]) == 2
