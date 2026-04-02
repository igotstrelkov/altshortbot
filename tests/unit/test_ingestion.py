"""Unit tests for market_data/universe_snapshotter.py"""
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from market_data.universe_snapshotter import ingest_asset_ctx, refresh_funding_from_rest
from shared.state_factory import create_asset_state


def _ctx(
    mark_px: str = "1800.0",
    oracle_px: str = "1799.0",
    oi: str = "100.0",
    funding: str = "0.0001",
) -> dict[str, Any]:
    return {
        "markPx": mark_px,
        "oraclePx": oracle_px,
        "openInterest": oi,
        "funding": funding,
    }


class TestIngestAssetCtx:
    def test_appends_oi_and_price_on_first_call(self) -> None:
        state = create_asset_state()
        ingest_asset_ctx(_ctx(), state, now=1000.0)
        assert len(state["oi_series"]) == 1
        assert len(state["price_series"]) == 1
        assert state["oi_series"][-1] == pytest.approx(100.0)
        assert state["price_series"][-1] == pytest.approx(1800.0)

    def test_respects_60s_oi_throttle(self) -> None:
        state = create_asset_state()
        ingest_asset_ctx(_ctx(), state, now=1000.0)
        ingest_asset_ctx(_ctx(), state, now=1059.9)  # <60s later
        assert len(state["oi_series"]) == 1

    def test_appends_after_60s_elapsed(self) -> None:
        state = create_asset_state()
        ingest_asset_ctx(_ctx(), state, now=1000.0)
        ingest_asset_ctx(_ctx(), state, now=1060.0)  # exactly 60s
        assert len(state["oi_series"]) == 2

    def test_never_writes_funding_series(self) -> None:
        state = create_asset_state()
        ingest_asset_ctx(_ctx(), state, now=1000.0)
        ingest_asset_ctx(_ctx(), state, now=2000.0)
        ingest_asset_ctx(_ctx(), state, now=3000.0)
        assert len(state["funding_series"]) == 0

    def test_rest_path_uses_rest_premium_directly(self) -> None:
        state = create_asset_state()
        ingest_asset_ctx(_ctx(), state, now=1000.0, rest_premium=0.005)
        assert len(state["premium_series"]) == 1
        assert state["premium_series"][-1] == pytest.approx(0.005)

    def test_ws_path_derives_premium_from_prices(self) -> None:
        state = create_asset_state()
        # mark=1800, oracle=1800 → premium = (1800-1800)/1800 = 0.0
        ingest_asset_ctx(_ctx(mark_px="1800.0", oracle_px="1800.0"), state, now=1000.0)
        assert len(state["premium_series"]) == 1
        assert state["premium_series"][-1] == pytest.approx(0.0)

    def test_ws_path_nonzero_premium(self) -> None:
        state = create_asset_state()
        # mark=1818, oracle=1800 → premium = 18/1800 = 0.01
        ingest_asset_ctx(_ctx(mark_px="1818.0", oracle_px="1800.0"), state, now=1000.0)
        assert state["premium_series"][-1] == pytest.approx(0.01)

    def test_ws_path_zero_oracle_px_safe(self) -> None:
        state = create_asset_state()
        ingest_asset_ctx(_ctx(mark_px="1800.0", oracle_px="0.0"), state, now=1000.0)
        assert state["premium_series"][-1] == pytest.approx(0.0)

    def test_respects_300s_premium_throttle(self) -> None:
        state = create_asset_state()
        ingest_asset_ctx(_ctx(), state, now=1000.0)
        ingest_asset_ctx(_ctx(), state, now=1299.9)  # <300s later (but >60s for OI)
        assert len(state["premium_series"]) == 1

    def test_float_coercion_on_string_fields(self) -> None:
        state = create_asset_state()
        ingest_asset_ctx(_ctx(mark_px="2000.5", oi="50.25"), state, now=1000.0)
        assert state["price_series"][-1] == pytest.approx(2000.5)
        assert state["oi_series"][-1] == pytest.approx(50.25)


class TestRefreshFundingFromRest:
    @pytest.mark.asyncio
    async def test_correct_division_by_8(self) -> None:
        mock_response = [{"fundingRate": "0.0008"} for _ in range(10)]
        with patch(
            "market_data.universe_snapshotter.rest_post",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            state = create_asset_state()
            await refresh_funding_from_rest("ETH", state)
        assert len(state["funding_series"]) == 10
        for rate in state["funding_series"]:
            assert rate == pytest.approx(0.0001)  # 0.0008 / 8

    @pytest.mark.asyncio
    async def test_clears_existing_series_before_fill(self) -> None:
        state = create_asset_state()
        state["funding_series"].append(99.9)  # stale data
        mock_response = [{"fundingRate": "0.0008"}]
        with patch(
            "market_data.universe_snapshotter.rest_post",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            await refresh_funding_from_rest("ETH", state)
        assert len(state["funding_series"]) == 1
        assert state["funding_series"][-1] == pytest.approx(0.0001)

    @pytest.mark.asyncio
    async def test_takes_last_48_entries(self) -> None:
        # 50 entries returned, only last 48 stored
        mock_response = [{"fundingRate": "0.0001"} for _ in range(50)]
        with patch(
            "market_data.universe_snapshotter.rest_post",
            new_callable=AsyncMock,
            return_value=mock_response,
        ):
            state = create_asset_state()
            await refresh_funding_from_rest("BTC", state)
        assert len(state["funding_series"]) == 48
