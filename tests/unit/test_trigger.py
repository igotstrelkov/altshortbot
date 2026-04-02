"""Unit tests for trigger engine and message handler."""
import time
from typing import Any
from unittest.mock import patch

import pytest

from market_data.state_normaliser import handle_message
from shared.state_factory import create_asset_state
from strategy.trigger.trigger_engine import evaluate_trigger, trigger_is_valid


# ── trigger_is_valid ──────────────────────────────────────────────────────────

class TestTriggerIsValid:
    def test_passes_on_valid_conditions(self) -> None:
        assert trigger_is_valid(1800.0, 1800.0, -2.5) is True

    def test_fails_on_price_drift_exceeds_threshold(self) -> None:
        # 1800 → 1830 = 1.67% drift > 1.5% threshold
        assert trigger_is_valid(1800.0, 1830.0, -2.5) is False

    def test_fails_on_z_score_recovery(self) -> None:
        # z-score >= -1.5 (DELTA_ZSCORE_EXPIRY) means trigger expired
        assert trigger_is_valid(1800.0, 1800.0, -1.5) is False
        assert trigger_is_valid(1800.0, 1800.0, 0.0) is False

    def test_passes_when_drift_just_below_threshold(self) -> None:
        # 1.4% drift < 1.5%
        assert trigger_is_valid(1800.0, 1825.2, -2.5) is True

    def test_fails_symmetrically_on_upward_drift(self) -> None:
        assert trigger_is_valid(1800.0, 1770.0, -2.5) is False  # -1.67%


# ── evaluate_trigger ──────────────────────────────────────────────────────────

class TestEvaluateTrigger:
    def _state_with_z(self, z: float, ready: bool = True) -> dict[str, Any]:
        state = create_asset_state()
        state["delta_ready"] = ready
        state["trade_delta_60s"] = z  # when mean=0, std=1 → z=value
        state["delta_mean_10m"] = 0.0
        state["delta_std_10m"] = 1.0
        return state

    def test_no_trigger_if_z_score_not_negative_enough(self) -> None:
        state = self._state_with_z(-1.5)
        assert evaluate_trigger(state, 1800.0, 1800.0) is False

    def test_no_trigger_if_delta_not_ready(self) -> None:
        state = self._state_with_z(-3.0, ready=False)
        assert evaluate_trigger(state, 1800.0, 1800.0) is False

    def test_requires_at_least_one_confirmation(self) -> None:
        # z-score fires but no confirmations → False
        state = self._state_with_z(-3.0)
        state["bid_depth_t_minus_30s"] = 0.0  # no bid depth data
        # price_series too short for structure break
        # vwap = 0 (empty buffer)
        assert evaluate_trigger(state, 1800.0, 1800.0) is False

    def test_bid_depth_thinning_confirms(self) -> None:
        state = self._state_with_z(-3.0)
        state["bid_depth_t_minus_30s"] = 1000.0
        state["bid_depth_now"] = 700.0  # 30% drop > 25% threshold
        assert evaluate_trigger(state, 1800.0, 1800.0) is True

    def test_structure_break_confirms(self) -> None:
        state = self._state_with_z(-3.0)
        # 15 prices, last one is below the min of the window
        prices = [100.0] * 14 + [99.0]
        state["price_series"].extend(prices)
        assert evaluate_trigger(state, 1800.0, 1800.0) is True

    def test_vwap_break_confirms(self) -> None:
        state = self._state_with_z(-3.0)
        state["price_series"].append(95.0)
        # Patch vwap_buffer to return a value above current price
        state["vwap_buffer"].on_trade(100.0, 1.0, time.time())
        assert evaluate_trigger(state, 1800.0, 1800.0) is True

    def test_structure_break_requires_15_samples(self) -> None:
        state = self._state_with_z(-3.0)
        # Only 14 samples — structure break cannot fire
        state["price_series"].extend([100.0] * 13 + [99.0])
        assert evaluate_trigger(state, 1800.0, 1800.0) is False


# ── handle_message ────────────────────────────────────────────────────────────

class TestHandleMessageTrades:
    def test_side_a_updates_sell_volume(self) -> None:
        state = create_asset_state()
        msg = {
            "channel": "trades",
            "data": [{"px": "1800.0", "sz": "1.0", "side": "A",
                      "time": 1000, "tid": 1}],
        }
        handle_message(msg, state)
        assert state["delta_aggregator"].sell_vol_usd == pytest.approx(1800.0)

    def test_side_b_updates_buy_volume(self) -> None:
        state = create_asset_state()
        msg = {
            "channel": "trades",
            "data": [{"px": "2000.0", "sz": "0.5", "side": "B",
                      "time": 1000, "tid": 2}],
        }
        handle_message(msg, state)
        assert state["delta_aggregator"].buy_vol_usd == pytest.approx(1000.0)

    def test_vwap_buffer_updated(self) -> None:
        state = create_asset_state()
        msg = {
            "channel": "trades",
            "data": [{"px": "1800.0", "sz": "1.0", "side": "A",
                      "time": 1000, "tid": 1}],
        }
        handle_message(msg, state)
        assert state["vwap_buffer"].get_vwap() == pytest.approx(1800.0)

    def test_multiple_trades_processed(self) -> None:
        state = create_asset_state()
        msg = {
            "channel": "trades",
            "data": [
                {"px": "100.0", "sz": "1.0", "side": "A", "time": 1000, "tid": 1},
                {"px": "200.0", "sz": "1.0", "side": "B", "time": 1001, "tid": 2},
            ],
        }
        handle_message(msg, state)
        assert state["delta_aggregator"].sell_vol_usd == pytest.approx(100.0)
        assert state["delta_aggregator"].buy_vol_usd == pytest.approx(200.0)


class TestHandleMessageCandle:
    def test_5m_series_updated(self) -> None:
        state = create_asset_state()
        msg = {
            "channel": "candle",
            "data": {"h": 1850.0, "l": 1780.0, "c": 1820.0,
                     "o": 1800.0, "v": 500.0, "t": 1000, "T": 2000, "n": 10},
        }
        handle_message(msg, state)
        assert state["high_series_5m"][-1] == pytest.approx(1850.0)
        assert state["low_series_5m"][-1] == pytest.approx(1780.0)
        assert state["close_series_5m"][-1] == pytest.approx(1820.0)

    def test_string_fields_coerced(self) -> None:
        state = create_asset_state()
        msg = {
            "channel": "candle",
            "data": {"h": "1850.0", "l": "1780.0", "c": "1820.0",
                     "o": "1800.0", "v": "500.0", "t": 1000, "T": 2000, "n": 10},
        }
        handle_message(msg, state)
        assert isinstance(state["high_series_5m"][-1], float)


class TestHandleMessageActiveAssetCtx:
    def test_ingest_asset_ctx_called_with_ctx_subobject(self) -> None:
        state = create_asset_state()
        ctx = {"markPx": "1800.0", "oraclePx": "1799.0",
               "openInterest": "100.0", "funding": "0.0001"}
        msg = {"channel": "activeAssetCtx", "data": {"coin": "ETH", "ctx": ctx}}

        with patch("market_data.state_normaliser.ingest_asset_ctx") as mock_ingest:
            handle_message(msg, state)
            mock_ingest.assert_called_once()
            call_args = mock_ingest.call_args
            assert call_args[0][0] is ctx  # first positional arg is the ctx subobject

    def test_pong_ignored_silently(self) -> None:
        state = create_asset_state()
        msg = {"channel": "pong"}
        handle_message(msg, state)  # should not raise


class TestHandleMessageL2Book:
    def test_bid_depth_computed_from_levels(self) -> None:
        state = create_asset_state()
        state["price_series"].append(1000.0)  # mid price
        # Bid at 998 (within 0.5% of 1000), bid at 990 (outside)
        msg = {
            "channel": "l2Book",
            "data": {
                "levels": [
                    [{"px": "998.0", "sz": "2.0", "n": 1},
                     {"px": "990.0", "sz": "5.0", "n": 1}],
                    [],
                ]
            },
        }
        handle_message(msg, state)
        # Only px=998 is within 0.5%: depth = 998 * 2 = 1996
        assert state["bid_depth_now"] == pytest.approx(1996.0)

    def test_previous_depth_shifted_to_t_minus_30s(self) -> None:
        state = create_asset_state()
        state["price_series"].append(1000.0)
        state["bid_depth_now"] = 5000.0

        msg = {
            "channel": "l2Book",
            "data": {"levels": [[{"px": "999.0", "sz": "1.0", "n": 1}], []]},
        }
        handle_message(msg, state)
        assert state["bid_depth_t_minus_30s"] == pytest.approx(5000.0)

    def test_skips_depth_calc_when_no_price_series(self) -> None:
        state = create_asset_state()
        msg = {
            "channel": "l2Book",
            "data": {"levels": [[{"px": "999.0", "sz": "1.0", "n": 1}], []]},
        }
        handle_message(msg, state)  # should not raise
        assert state["bid_depth_now"] == 0.0
