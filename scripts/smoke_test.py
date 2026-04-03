"""
Smoke test for EIP-712 signing + order response parsing.
No real orders placed — uses a far-OTM IOC that is immediately rejected.

Usage:
  HL_API_WALLET_ADDRESS=0x... HL_PRIVATE_KEY=0x... python scripts/smoke_test.py

Defaults to mainnet. Verifies:
  1. ExchangeAdapter.build_coin_meta() populates coin_meta
  2. place_limit_order() returns a dict with 'response.data.statuses'
  3. parse_order_status() returns a ParsedOrderStatus (likely iocCancelRejected)
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from oms.execution_adapter import ExchangeAdapter
from oms.order_parser import parse_order_status


async def run() -> None:
    wallet = os.environ.get("HL_API_WALLET_ADDRESS", "")
    key = os.environ.get("HL_PRIVATE_KEY", "")

    if not wallet or not key:
        print("ERROR: set HL_API_WALLET_ADDRESS and HL_PRIVATE_KEY env vars")
        sys.exit(1)

    testnet = os.environ.get("HL_TESTNET", "false").lower() == "true"
    adapter = ExchangeAdapter(wallet_address=wallet, private_key=key, testnet=testnet)
    print(f"   network: {'testnet' if testnet else 'mainnet'}")

    try:
        print("1. Building coin meta...")
        await adapter.build_coin_meta()
        print(f"   OK — {len(adapter.coin_meta)} coins loaded")

        print("2. Placing far-OTM IOC sell (expected: rejected)...")
        raw = await adapter.place_limit_order(
            coin="ETH",
            side="sell",
            size_coins=0.001,
            price_str="99999",
            tif="Ioc",
        )
        print(f"   raw response: {raw}")

        print("3. Parsing order status...")
        parsed = parse_order_status(raw)
        print(f"   parsed: {parsed}")

        if parsed is None:
            print("WARN: parse_order_status returned None — check response shape above")
        else:
            print(f"   status={parsed.status}  avg_px={parsed.avg_px}  total_sz={parsed.total_sz}")
    finally:
        await adapter.close()

    print("Done.")


if __name__ == "__main__":
    asyncio.run(run())
