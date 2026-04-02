"""Unit tests for VwapBuffer and DeltaAggregator."""
import pytest

from shared.state_factory import create_asset_state
from strategy.trigger.delta_aggregator import (
    DeltaAggregator,
    get_delta_z_score,
    update_delta_state,
)
from strategy.trigger.vwap_buffer import VwapBuffer


class TestVwapBuffer:
    def test_correct_vwap(self) -> None:
        buf = VwapBuffer()
        buf.on_trade(100.0, 1.0, 0.0)   # size_base = 1.0
        buf.on_trade(200.0, 1.0, 10.0)  # size_base = 1.0
        # standard VWAP: (100*1 + 200*1) / (1+1) = 150.0
        assert buf.get_vwap() == pytest.approx(150.0)

    def test_empty_buffer_returns_zero(self) -> None:
        buf = VwapBuffer()
        assert buf.get_vwap() == 0.0

    def test_window_trimming(self) -> None:
        buf = VwapBuffer()
        buf.on_trade(100.0, 1.0, 0.0)    # at t=0, will be trimmed at t=301
        buf.on_trade(200.0, 1.0, 200.0)  # at t=200
        # advance to t=310: trade at t=0 is outside 300s window
        buf.on_trade(300.0, 1.0, 310.0)
        # only trades at t=200 and t=310 remain; VWAP = (200+300)/2 = 250
        assert buf.get_vwap() == pytest.approx(250.0)

    def test_single_trade(self) -> None:
        buf = VwapBuffer()
        buf.on_trade(50.0, 2.0, 0.0)  # volume_usd = 100
        assert buf.get_vwap() == pytest.approx(50.0)

    def test_window_boundary_inclusive(self) -> None:
        buf = VwapBuffer()
        buf.on_trade(100.0, 1.0, 0.0)
        # exactly at cutoff edge (t=300, cutoff = 300-300 = 0, t >= cutoff → kept)
        buf.on_trade(200.0, 1.0, 300.0)
        assert len(buf._trades) == 2


class TestDeltaAggregator:
    def test_side_a_is_sell(self) -> None:
        agg = DeltaAggregator()
        agg.on_trade("A", 1.0, 100.0)
        assert agg.sell_vol_usd == pytest.approx(100.0)
        assert agg.buy_vol_usd == 0.0

    def test_side_b_is_buy(self) -> None:
        agg = DeltaAggregator()
        agg.on_trade("B", 2.0, 50.0)
        assert agg.buy_vol_usd == pytest.approx(100.0)
        assert agg.sell_vol_usd == 0.0

    def test_unknown_side_ignored(self) -> None:
        agg = DeltaAggregator()
        agg.on_trade("X", 1.0, 100.0)
        assert agg.sell_vol_usd == 0.0
        assert agg.buy_vol_usd == 0.0

    def test_flush_does_not_fire_before_window(self) -> None:
        agg = DeltaAggregator()
        agg.window_start = 0.0
        state = create_asset_state()
        result = agg.flush_if_ready(state, 59.9)
        assert result is False

    def test_flush_fires_after_window(self) -> None:
        agg = DeltaAggregator()
        agg.window_start = 0.0
        agg.on_trade("A", 1.0, 300.0)  # sell 300 USD
        agg.on_trade("B", 1.0, 100.0)  # buy 100 USD
        state = create_asset_state()
        result = agg.flush_if_ready(state, 60.0)
        assert result is True
        # net delta = 300 - 100 = 200
        assert state["trade_delta_60s"] == pytest.approx(200.0)

    def test_flush_resets_counters(self) -> None:
        agg = DeltaAggregator()
        agg.window_start = 0.0
        agg.on_trade("A", 1.0, 100.0)
        state = create_asset_state()
        agg.flush_if_ready(state, 60.0)
        assert agg.sell_vol_usd == 0.0
        assert agg.buy_vol_usd == 0.0
        assert agg.window_start == pytest.approx(60.0)

    def test_net_delta_passed_correctly(self) -> None:
        agg = DeltaAggregator()
        agg.window_start = 0.0
        agg.on_trade("A", 2.0, 100.0)  # sell 200
        agg.on_trade("B", 3.0, 100.0)  # buy 300
        state = create_asset_state()
        agg.flush_if_ready(state, 60.0)
        assert state["trade_delta_60s"] == pytest.approx(-100.0)  # net sell is negative


class TestGetDeltaZScore:
    def test_returns_zero_before_cold_start(self) -> None:
        state = create_asset_state()
        # feed fewer than DELTA_COLD_START_PERIODS (10) windows
        for i in range(9):
            update_delta_state(state, float(i))
        assert get_delta_z_score(state) == 0.0

    def test_returns_zero_when_not_ready(self) -> None:
        state = create_asset_state()
        state["delta_ready"] = False
        assert get_delta_z_score(state) == 0.0

    def test_correct_z_score_after_cold_start(self) -> None:
        state = create_asset_state()
        # Feed 10 identical values so mean=5, std≈0 triggers 1e-9 guard,
        # then use distinct values instead
        values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
        for v in values:
            update_delta_state(state, v)

        assert state["delta_ready"] is True
        import statistics
        expected_mean = statistics.mean(values)
        expected_std = statistics.stdev(values)
        last = float(state["trade_delta_60s"])
        expected_z = (last - expected_mean) / expected_std
        assert get_delta_z_score(state) == pytest.approx(expected_z)

    def test_delta_ready_false_after_few_samples(self) -> None:
        state = create_asset_state()
        update_delta_state(state, 1.0)
        assert state["delta_ready"] is False
        assert state["delta_mean_10m"] == 0.0
        assert state["delta_std_10m"] == 0.0
