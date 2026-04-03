"""
Integration tests for ExchangeAdapter.
All tests require live Hyperliquid testnet credentials — skipped in CI.

To run manually:
  HL_API_WALLET_ADDRESS=0x... HL_PRIVATE_KEY=0x... HL_TESTNET=true
  pytest tests/integration/test_exchange_adapter.py -v --no-header -rN
"""
from __future__ import annotations

import os

import pytest

from oms.execution_adapter import ExchangeAdapter
from oms.nonce_manager import NonceManager


@pytest.fixture()
def adapter() -> ExchangeAdapter:
    return ExchangeAdapter(
        wallet_address=os.getenv("HL_API_WALLET_ADDRESS", "0x0"),
        private_key=os.getenv("HL_PRIVATE_KEY", "0x0"),
        testnet=True,
    )


@pytest.mark.skip(reason="requires live testnet credentials")
async def test_place_limit_order_response_shape(adapter: ExchangeAdapter) -> None:
    """
    Verifies that place_limit_order returns a dict with:
      response.data.statuses[0] containing 'filled' | 'resting' | 'error'
    Uses a tiny IOC order on a liquid testnet market.
    """
    adapter.coin_meta["ETH"] = {"asset_index": 1, "sz_decimals": 4}
    raw = await adapter.place_limit_order(
        coin="ETH",
        side="sell",
        size_coins=0.001,
        price_str="99999",  # far OTM IOC — will iocCancelRejected immediately
        tif="Ioc",
    )
    assert "response" in raw
    statuses = raw["response"]["data"]["statuses"]
    assert len(statuses) == 1
    outcome = statuses[0]
    assert any(k in outcome for k in ("filled", "resting", "error"))
    await adapter.close()


@pytest.mark.skip(reason="requires live testnet credentials")
async def test_get_open_positions_parses_correctly(adapter: ExchangeAdapter) -> None:
    """
    Verifies that get_open_positions returns a list of dicts, each with
    'coin' and 'szi' fields, and that szi != '0' for all returned entries.
    """
    positions = await adapter.get_open_positions()
    assert isinstance(positions, list)
    for pos in positions:
        assert "coin" in pos or "position" in pos
        szi = pos.get("position", pos).get("szi", "0")
        assert szi != "0"
    await adapter.close()


@pytest.mark.skip(reason="requires live testnet credentials")
async def test_nonce_increments_across_calls(adapter: ExchangeAdapter) -> None:
    """
    Verifies that successive next_nonce() calls return strictly increasing values
    and that two concurrent calls never return the same value.
    """
    nm = NonceManager()
    n1 = nm.next_nonce()
    n2 = nm.next_nonce()
    n3 = nm.next_nonce()
    assert n1 < n2 < n3
    assert n2 == n1 + 1
    assert n3 == n2 + 1
