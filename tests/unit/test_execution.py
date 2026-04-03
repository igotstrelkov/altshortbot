"""Unit tests for OMS price formatter, order parser, and IOC entry engine."""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from oms.order_parser import parse_order_status
from oms.price_formatter import format_price, validate_size
from shared.state_factory import create_asset_state


# ── helpers ───────────────────────────────────────────────────────────────────

def _filled_raw(avg_px: str = "1800.0", total_sz: str = "0.5", oid: int = 1) -> dict[str, Any]:
    return {"response": {"data": {"statuses": [{"filled": {"avgPx": avg_px, "totalSz": total_sz, "oid": oid}}]}}}


def _resting_raw(oid: int = 42) -> dict[str, Any]:
    return {"response": {"data": {"statuses": [{"resting": {"oid": oid, "cloid": "abc"}}]}}}


def _error_raw(reason: str) -> dict[str, Any]:
    return {"response": {"data": {"statuses": [{"error": reason}]}}}


def _state_with_mid(mid: float = 1800.0, sz_decimals: int = 2) -> dict[str, Any]:
    """State with trigger conditions satisfied and a valid mid price."""
    state = create_asset_state()
    state["price_series"].append(mid)
    state["sz_decimals"] = sz_decimals
    state["delta_ready"] = True
    state["trade_delta_60s"] = -3.0
    state["delta_mean_10m"] = 0.0
    state["delta_std_10m"] = 1.0
    return state


# ── price_formatter ───────────────────────────────────────────────────────────

class TestPriceFormatter:
    def test_format_price_rounds_to_sig_figs(self) -> None:
        # 5 sig figs: 1,8,0,0,1 → "1800.1"  (6-2=4 max dp, but sig figs binds first)
        assert format_price(1800.123456, 2) == "1800.1"
        # format_price truncates (ROUND_DOWN): 1.23456789 → "1.2345"
        assert format_price(1.23456789, 2) == "1.2345"

    def test_validate_size_rounds_correctly(self) -> None:
        assert validate_size(1.23456, 3) == pytest.approx(1.235)

    def test_validate_size_zero_decimals(self) -> None:
        assert validate_size(1.9, 0) == 2.0


# ── parse_order_status ────────────────────────────────────────────────────────

class TestParseOrderStatus:
    def test_parses_filled(self) -> None:
        result = parse_order_status(_filled_raw("1850.5", "1.0", 999))
        assert result is not None
        assert result.status == "filled"
        assert result.avg_px == pytest.approx(1850.5)
        assert result.total_sz == pytest.approx(1.0)
        assert result.oid == 999

    def test_parses_resting(self) -> None:
        result = parse_order_status(_resting_raw(42))
        assert result is not None
        assert result.status == "resting"
        assert result.oid == 42

    def test_parses_error(self) -> None:
        result = parse_order_status(_error_raw("iocCancelRejected"))
        assert result is not None
        assert result.status == "error"
        assert result.reason == "iocCancelRejected"

    def test_returns_none_on_empty_statuses(self) -> None:
        raw: dict[str, Any] = {"response": {"data": {"statuses": []}}}
        assert parse_order_status(raw) is None

    def test_returns_none_on_missing_response_key(self) -> None:
        assert parse_order_status({}) is None

    def test_returns_none_on_missing_data_key(self) -> None:
        assert parse_order_status({"response": {}}) is None

    def test_returns_none_on_type_error(self) -> None:
        raw: dict[str, Any] = {"response": {"data": {"statuses": None}}}
        assert parse_order_status(raw) is None

    def test_returns_none_on_bad_float(self) -> None:
        raw = {"response": {"data": {"statuses": [{"filled": {"avgPx": "not_a_float", "totalSz": "1.0", "oid": 1}}]}}}
        assert parse_order_status(raw) is None

    def test_filled_field_types(self) -> None:
        result = parse_order_status(_filled_raw())
        assert result is not None
        assert isinstance(result.avg_px, float)
        assert isinstance(result.total_sz, float)
        assert isinstance(result.oid, int)


# ── execute_entry ─────────────────────────────────────────────────────────────

class TestExecuteEntry:
    """Tests use patched place_limit_order via oms.execution_adapter."""

    @pytest.fixture()
    def state(self) -> dict[str, Any]:
        return _state_with_mid(1800.0)

    async def test_returns_none_when_trigger_invalid(self, state: dict[str, Any]) -> None:
        # Trigger price far from current mid → drift > 1.5%
        from oms.ioc_entry import execute_entry
        result = await execute_entry("ETH", 500.0, 1850.0, state)
        assert result is None

    async def test_returns_none_when_size_too_small(self, state: dict[str, Any]) -> None:
        from oms.ioc_entry import execute_entry
        # size_usd=0.01 → size_coins*mid = 0.01 < MIN_ORDER_NOTIONAL_USD=10
        result = await execute_entry("ETH", 0.01, 1800.0, state)
        assert result is None

    async def test_returns_none_on_min_trade_ntl_rejected(self, state: dict[str, Any]) -> None:
        from oms.ioc_entry import execute_entry
        with patch("oms.ioc_entry.place_limit_order", new_callable=AsyncMock) as mock_place:
            mock_place.return_value = _error_raw("minTradeNtlRejected")
            result = await execute_entry("ETH", 500.0, 1800.0, state)
        assert result is None
        mock_place.assert_called_once()  # no fallback

    async def test_returns_none_on_tick_rejected(self, state: dict[str, Any]) -> None:
        from oms.ioc_entry import execute_entry
        with patch("oms.ioc_entry.place_limit_order", new_callable=AsyncMock) as mock_place:
            mock_place.return_value = _error_raw("tickRejected")
            result = await execute_entry("ETH", 500.0, 1800.0, state)
        assert result is None
        mock_place.assert_called_once()

    async def test_returns_none_on_oracle_rejected(self, state: dict[str, Any]) -> None:
        from oms.ioc_entry import execute_entry
        with patch("oms.ioc_entry.place_limit_order", new_callable=AsyncMock) as mock_place:
            mock_place.return_value = _error_raw("oracleRejected")
            result = await execute_entry("ETH", 500.0, 1800.0, state)
        assert result is None
        mock_place.assert_called_once()

    async def test_proceeds_to_fallback_on_ioc_cancel_rejected(self, state: dict[str, Any]) -> None:
        from oms.ioc_entry import execute_entry
        fallback_raw = _filled_raw("1795.0", "0.28", 2)
        with patch("oms.ioc_entry.place_limit_order", new_callable=AsyncMock) as mock_place:
            mock_place.side_effect = [_error_raw("iocCancelRejected"), fallback_raw]
            result = await execute_entry("ETH", 500.0, 1800.0, state)
        assert result is not None
        assert result.status == "filled"
        assert mock_place.call_count == 2

    async def test_proceeds_to_fallback_on_unexpected_resting(self, state: dict[str, Any]) -> None:
        from oms.ioc_entry import execute_entry
        fallback_raw = _filled_raw("1794.0", "0.28", 3)
        with patch("oms.ioc_entry.place_limit_order", new_callable=AsyncMock) as mock_place:
            mock_place.side_effect = [_resting_raw(10), fallback_raw]
            result = await execute_entry("ETH", 500.0, 1800.0, state)
        assert result is not None
        assert result.status == "filled"
        assert mock_place.call_count == 2

    async def test_returns_none_when_parse_returns_none(self, state: dict[str, Any]) -> None:
        """None from parse_order_status triggers abort — no fallback sent."""
        from oms.ioc_entry import execute_entry
        with patch("oms.ioc_entry.place_limit_order", new_callable=AsyncMock) as mock_place:
            mock_place.return_value = {}  # malformed → parse returns None
            result = await execute_entry("ETH", 500.0, 1800.0, state)
        assert result is None
        mock_place.assert_called_once()  # no fallback

    async def test_returns_filled_on_primary_fill(self, state: dict[str, Any]) -> None:
        from oms.ioc_entry import execute_entry
        with patch("oms.ioc_entry.place_limit_order", new_callable=AsyncMock) as mock_place:
            mock_place.return_value = _filled_raw("1800.5", "0.28", 7)
            result = await execute_entry("ETH", 500.0, 1800.0, state)
        assert result is not None
        assert result.status == "filled"
        assert result.avg_px == pytest.approx(1800.5)
        mock_place.assert_called_once()

    async def test_abort_on_excessive_slippage(self, state: dict[str, Any]) -> None:
        """Fill at 1790 vs limit ~1800.9 = ~0.6% slippage > ABORT_SLIPPAGE."""
        from oms.ioc_entry import execute_entry
        fill_raw = _filled_raw("1790.0", "0.28", 8)
        flatten_raw = _filled_raw("1800.0", "0.28", 9)
        with patch("oms.ioc_entry.place_limit_order", new_callable=AsyncMock) as mock_place:
            mock_place.side_effect = [fill_raw, flatten_raw]
            result = await execute_entry("ETH", 500.0, 1800.0, state)
        assert result is None
        assert mock_place.call_count == 2  # primary + flatten buy

    async def test_returns_none_when_both_unfilled(self, state: dict[str, Any]) -> None:
        from oms.ioc_entry import execute_entry
        with patch("oms.ioc_entry.place_limit_order", new_callable=AsyncMock) as mock_place:
            mock_place.side_effect = [
                _error_raw("iocCancelRejected"),
                _error_raw("marketOrderNoLiquidityRejected"),
            ]
            result = await execute_entry("ETH", 500.0, 1800.0, state)
        assert result is None
