"""Unit tests for LiquidationModel and squeeze scoring."""
from collections import deque

import pytest

from shared.state_factory import create_asset_state
from strategy.liq_model import (
    LiquidationModel,
    calculate_squeeze_score,
    squeeze_risk_ratio,
    update_liq_model_from_candle,
)


class TestLiquidationModelUpdate:
    def test_ignores_flat_oi(self) -> None:
        m = LiquidationModel()
        m.update(100.0, 100.0, 10.0, 11.0, 1000.0, 0.0)
        assert len(m.long_entries) == 0
        assert len(m.short_entries) == 0

    def test_ignores_falling_oi(self) -> None:
        m = LiquidationModel()
        m.update(100.0, 90.0, 10.0, 11.0, 1000.0, 0.0)
        assert len(m.long_entries) == 0
        assert len(m.short_entries) == 0

    def test_bullish_candle_creates_long_entry(self) -> None:
        m = LiquidationModel()
        m.update(100.0, 110.0, 10.0, 12.0, 5000.0, 1000.0)
        assert len(m.long_entries) == 1
        liq_price, notional, ts = m.long_entries[0]
        assert liq_price == pytest.approx(12.0 * 0.90)
        assert notional == pytest.approx(5000.0)
        assert ts == pytest.approx(1000.0)

    def test_bearish_candle_creates_short_entry(self) -> None:
        m = LiquidationModel()
        m.update(100.0, 110.0, 12.0, 10.0, 5000.0, 1000.0)
        assert len(m.short_entries) == 1
        liq_price, notional, ts = m.short_entries[0]
        assert liq_price == pytest.approx(10.0 * 1.10)
        assert notional == pytest.approx(5000.0)

    def test_equal_open_close_treated_as_bearish(self) -> None:
        # close == open is not > open, so falls to short path
        m = LiquidationModel()
        m.update(100.0, 110.0, 10.0, 10.0, 1000.0, 0.0)
        assert len(m.short_entries) == 1
        assert len(m.long_entries) == 0


class TestClusterAboveBelow:
    def _model_with_shorts(self, prices: list[float], notional: float = 1000.0) -> LiquidationModel:
        m = LiquidationModel()
        for p in prices:
            m.short_entries.append((p, notional, 0.0))
        return m

    def _model_with_longs(self, prices: list[float], notional: float = 1000.0) -> LiquidationModel:
        m = LiquidationModel()
        for p in prices:
            m.long_entries.append((p, notional, 0.0))
        return m

    def test_cluster_above_sums_within_range(self) -> None:
        # price=100, pct=0.03 → window (100, 103]
        m = self._model_with_shorts([101.0, 102.0, 104.0])  # 104 is outside
        assert m.cluster_above(100.0, pct=0.03) == pytest.approx(2000.0)

    def test_cluster_above_excludes_at_price(self) -> None:
        m = self._model_with_shorts([100.0])  # exactly at price, excluded (price < p)
        assert m.cluster_above(100.0) == 0.0

    def test_cluster_below_sums_within_range(self) -> None:
        # price=100, pct=0.03 → window [97, 100)
        m = self._model_with_longs([97.0, 98.0, 96.0])  # 96 is outside
        assert m.cluster_below(100.0, pct=0.03) == pytest.approx(2000.0)

    def test_cluster_below_excludes_at_price(self) -> None:
        m = self._model_with_longs([100.0])  # exactly at price, excluded (p < price)
        assert m.cluster_below(100.0) == 0.0

    def test_empty_model_returns_zero(self) -> None:
        m = LiquidationModel()
        assert m.cluster_above(100.0) == 0.0
        assert m.cluster_below(100.0) == 0.0


class TestNewPositions1h:
    def test_filters_by_cutoff(self) -> None:
        m = LiquidationModel()
        now = 10000.0
        m.short_entries.append((110.0, 500.0, now - 3601))  # too old
        m.short_entries.append((110.0, 300.0, now - 3599))  # within 1h
        m.long_entries.append((90.0, 200.0, now - 1000))    # within 1h
        short_1h, long_1h = m.new_positions_1h(now)
        assert short_1h == pytest.approx(300.0)
        assert long_1h == pytest.approx(200.0)

    def test_empty_model(self) -> None:
        m = LiquidationModel()
        short_1h, long_1h = m.new_positions_1h(0.0)
        assert short_1h == 0.0
        assert long_1h == 0.0


class TestSqueezeRiskRatio:
    def test_zero_when_both_zero(self) -> None:
        assert squeeze_risk_ratio(0.0, 0.0) == 0.0

    def test_correct_ratio(self) -> None:
        assert squeeze_risk_ratio(300.0, 700.0) == pytest.approx(0.3)

    def test_one_when_only_above(self) -> None:
        assert squeeze_risk_ratio(1000.0, 0.0) == pytest.approx(1.0)


class TestCalculateSqueezeScore:
    def test_zero_when_no_entries(self) -> None:
        m = LiquidationModel()
        score = calculate_squeeze_score(m, 100.0, deque(), now=0.0)
        assert score == 0

    def test_short_crowding_adds_3(self) -> None:
        m = LiquidationModel()
        now = 1000.0
        m.short_entries.append((110.0, 500.0, now - 100))
        m.long_entries.append((90.0, 100.0, now - 100))
        score = calculate_squeeze_score(m, 100.0, deque(), now=now)
        assert score >= 3

    def test_liq_above_greater_adds_2(self) -> None:
        m = LiquidationModel()
        now = 1000.0
        # short liq above price (squeeze risk)
        m.short_entries.append((102.0, 1000.0, now - 100))
        # long liq below price (cascade) — less
        m.long_entries.append((98.0, 100.0, now - 100))
        score = calculate_squeeze_score(m, 100.0, deque(), now=now)
        assert score >= 2

    def test_funding_drop_adds_3(self) -> None:
        m = LiquidationModel()
        # funding_series: elevated prev, then dropped >30%
        elevated = 0.20 / 8760 * 2   # 2× the floor
        dropped = elevated * 0.60     # 40% drop → triggers
        funding = deque([elevated, dropped])
        score = calculate_squeeze_score(m, 100.0, funding, now=1000.0)
        assert score >= 3

    def test_score_capped_at_10(self) -> None:
        m = LiquidationModel()
        now = 1000.0
        # max out all conditions
        m.short_entries.append((101.0, 10000.0, now - 100))  # +3 short crowding, +2 liq above, +2 ratio
        m.long_entries.append((99.0, 100.0, now - 100))
        elevated = 0.20 / 8760 * 2
        dropped = elevated * 0.60
        funding = deque([elevated, dropped])
        score = calculate_squeeze_score(m, 100.0, funding, now=now)
        assert score <= 10


class TestUpdateLiqModelFromCandle:
    def test_skips_when_oi_unchanged(self) -> None:
        state = create_asset_state()
        state["oi_series"].append(100.0)
        state["oi_series"].append(100.0)
        state["price_series"].append(50.0)
        state["price_series"].append(51.0)
        update_liq_model_from_candle(state, 51.0, 1000.0)
        liq_model = state["liq_model"]
        assert isinstance(liq_model, LiquidationModel)
        assert len(liq_model.long_entries) == 0
        assert len(liq_model.short_entries) == 0

    def test_skips_when_insufficient_data(self) -> None:
        state = create_asset_state()
        state["oi_series"].append(100.0)
        update_liq_model_from_candle(state, 50.0, 1000.0)
        liq_model = state["liq_model"]
        assert isinstance(liq_model, LiquidationModel)
        assert len(liq_model.long_entries) == 0

    def test_updates_model_on_oi_growth(self) -> None:
        state = create_asset_state()
        state["oi_series"].append(100.0)
        state["oi_series"].append(110.0)
        state["price_series"].append(50.0)
        state["price_series"].append(52.0)  # bullish
        update_liq_model_from_candle(state, 52.0, 1000.0)
        liq_model = state["liq_model"]
        assert isinstance(liq_model, LiquidationModel)
        assert len(liq_model.long_entries) == 1

    def test_caches_squeeze_score(self) -> None:
        state = create_asset_state()
        state["oi_series"].append(100.0)
        state["oi_series"].append(110.0)
        state["price_series"].append(50.0)
        state["price_series"].append(52.0)
        update_liq_model_from_candle(state, 52.0, 1000.0)
        assert isinstance(state["squeeze_score"], int)
