"""Unit tests for shared/helpers.py"""
from collections import deque

import pytest

from shared.helpers import compute_atr, compute_vwap, ema, format_price


class TestEma:
    def test_basic_values(self) -> None:
        result = ema([10.0, 20.0, 30.0], period=2)
        assert len(result) == 3
        assert result[0] == 10.0
        # k = 2/(2+1) = 0.6667
        assert result[1] == pytest.approx(10.0 + 2 / 3 * (20.0 - 10.0))
        assert result[-1] == pytest.approx(result[1] + 2 / 3 * (30.0 - result[1]))

    def test_single_element(self) -> None:
        assert ema([42.0], period=5) == [42.0]

    def test_empty_input(self) -> None:
        assert ema([], period=5) == []

    def test_result_length_matches_input(self) -> None:
        closes = [float(i) for i in range(20)]
        assert len(ema(closes, period=10)) == 20

    def test_latest_is_last(self) -> None:
        closes = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = ema(closes, period=3)
        assert result[-1] != result[-2]  # values should differ
        assert result[-6 + 5] == result[-1]  # result[-6] would be result[-1] only if len==1


class TestComputeVwap:
    def test_weighted_correctly(self) -> None:
        # 100 USD at price 10, 200 USD at price 20 → VWAP = (10*100 + 20*200) / 300 = 5000/300
        trades = [(10.0, 100.0), (20.0, 200.0)]
        assert compute_vwap(trades) == pytest.approx(5000.0 / 300.0)

    def test_empty_input(self) -> None:
        assert compute_vwap([]) == 0.0

    def test_zero_volume(self) -> None:
        assert compute_vwap([(10.0, 0.0), (20.0, 0.0)]) == 0.0

    def test_single_trade(self) -> None:
        assert compute_vwap([(50.0, 1000.0)]) == pytest.approx(50.0)


class TestComputeAtr:
    def _make_series(self, values: list[float]) -> deque[float]:
        return deque(values)

    def test_correct_true_range(self) -> None:
        # 15 candles so period=14 has enough data
        highs = self._make_series([11.0] * 15)
        lows = self._make_series([9.0] * 15)
        closes = self._make_series([10.0] * 15)
        # Each TR = max(11-9, |11-10|, |9-10|) = max(2, 1, 1) = 2.0
        assert compute_atr(highs, lows, closes, period=14) == pytest.approx(2.0)

    def test_returns_zero_when_insufficient_data(self) -> None:
        highs = self._make_series([11.0] * 5)
        lows = self._make_series([9.0] * 5)
        closes = self._make_series([10.0] * 5)
        assert compute_atr(highs, lows, closes, period=14) == 0.0

    def test_exactly_period_plus_one_candles(self) -> None:
        n = 15  # period+1
        highs = self._make_series([10.5] * n)
        lows = self._make_series([9.5] * n)
        closes = self._make_series([10.0] * n)
        result = compute_atr(highs, lows, closes, period=14)
        assert result > 0.0


class TestFormatPrice:
    def test_prd_example_large_price(self) -> None:
        assert format_price(12345.678, 2) == "12345"

    def test_prd_example_mid_price(self) -> None:
        assert format_price(1.23456, 2) == "1.2345"

    def test_prd_example_small_price(self) -> None:
        assert format_price(0.001234, 2) == "0.0012"

    def test_raises_on_zero(self) -> None:
        with pytest.raises(ValueError):
            format_price(0.0, 2)

    def test_raises_on_negative(self) -> None:
        with pytest.raises(ValueError):
            format_price(-1.0, 2)

    def test_no_trailing_zeros(self) -> None:
        result = format_price(100.0, 2)
        assert "." not in result or not result.endswith("0")

    def test_no_scientific_notation(self) -> None:
        result = format_price(0.000123, 0)
        assert "e" not in result.lower()

    def test_returns_string(self) -> None:
        assert isinstance(format_price(1234.5, 2), str)
