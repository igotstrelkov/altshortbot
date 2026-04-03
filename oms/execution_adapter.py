"""
Exchange adapter: builds and signs Hyperliquid order actions.
See PRD Sections 2.9, 2.10, 2.11.

The module-level place_limit_order stub is kept as the injection point used by
ioc_entry.py — patch it in unit tests or replace it with ExchangeAdapter in production.
"""
from __future__ import annotations

from typing import Any

import aiohttp
import structlog

from oms.nonce_manager import NonceManager
from risk.watchdog import HeartbeatMonitor

log = structlog.get_logger()

_HL_MAINNET = "https://api.hyperliquid.xyz"
_HL_TESTNET = "https://api.hyperliquid-testnet.xyz"


# ── Module-level stub (injection point for ioc_entry.py) ─────────────────────

async def place_limit_order(
    coin: str,
    side: str,
    size_coins: float,
    price_str: str,
    tif: str,
) -> dict[str, Any]:
    """
    Module-level stub. In production, replace by wiring ExchangeAdapter.place_limit_order
    as the callable (e.g. monkey-patch this name, or pass the adapter to execute_entry).
    Patched in unit tests via unittest.mock.patch.
    """
    raise NotImplementedError(
        "place_limit_order not wired — instantiate ExchangeAdapter and wire it in"
    )


# ── ExchangeAdapter ───────────────────────────────────────────────────────────

class ExchangeAdapter:
    """
    Builds Hyperliquid exchange actions, manages nonce, and submits via aiohttp.

    Coin metadata (asset index, sz_decimals) must be loaded via set_coin_meta()
    before place_limit_order is called.

    EIP-712 signing is out of scope for Stage 10. place_limit_order raises
    NotImplementedError at the signing step — wire in hyperliquid-python-sdk or
    implement EIP-712 directly before live/paper trading (Stage 12).
    """

    def __init__(
        self,
        wallet_address: str,
        private_key: str,
        testnet: bool = True,
    ) -> None:
        self.wallet_address = wallet_address
        self._private_key = private_key
        self.base_url = _HL_TESTNET if testnet else _HL_MAINNET
        self.nonce_manager = NonceManager()
        self._heartbeat_monitor = HeartbeatMonitor()
        # Populated by set_coin_meta() after universe scan
        self._coin_meta: dict[str, dict[str, Any]] = {}
        # Session created lazily on first use (must be inside async context)
        self._session: aiohttp.ClientSession | None = None

    def set_coin_meta(self, coin: str, asset_index: int, sz_decimals: int) -> None:
        """Register universe metadata for a coin. Call after metaAndAssetCtxs fetch."""
        self._coin_meta[coin] = {"index": asset_index, "sz_decimals": sz_decimals}

    @property
    def heartbeat_monitor(self) -> HeartbeatMonitor:
        return self._heartbeat_monitor

    async def _session_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        url = self.base_url + path
        async with self._session.post(url, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"POST {path} returned {resp.status}: {text}")
            result: dict[str, Any] = await resp.json()
            return result

    async def place_limit_order(
        self,
        coin: str,
        side: str,
        size_coins: float,
        price_str: str,
        tif: str = "Gtc",
    ) -> dict[str, Any]:
        """
        Build and submit a Hyperliquid limit order action.
        Raises NotImplementedError at EIP-712 signing — wire signing in Stage 12.
        """
        meta = self._coin_meta.get(coin)
        if meta is None:
            raise RuntimeError(f"No metadata for coin {coin!r} — call set_coin_meta() first")

        asset_index: int = meta["index"]
        sz_decimals: int = meta["sz_decimals"]

        action: dict[str, Any] = {  # noqa: F841 — used once signing is wired in Stage 12
            "type": "order",
            "orders": [
                {
                    "a": asset_index,
                    "b": side == "buy",
                    "p": price_str,
                    "s": str(round(size_coins, sz_decimals)),
                    "r": False,
                    "t": {"limit": {"tif": tif}},
                }
            ],
            "grouping": "na",
        }
        nonce = self.nonce_manager.next_nonce()  # noqa: F841 — used once signing is wired in Stage 12

        # ── EIP-712 signing ───────────────────────────────────────────────────
        # Out of scope for Stage 10.
        # Options (in order of effort):
        #   1. pip install hyperliquid-python  — official SDK handles signing
        #   2. eth_account library — implement EIP-712 directly
        #   3. Reference: https://github.com/hyperliquid-dex/hyperliquid-python-sdk
        raise NotImplementedError(
            "EIP-712 signing not yet implemented — "
            "wire in hyperliquid-python-sdk or implement directly"
        )

        # Once signing is implemented, continue with:
        # payload = {"action": action, "nonce": nonce, "signature": signature}
        # return await self._session_post("/exchange", payload)

    async def get_open_positions(self) -> list[dict[str, Any]]:
        """
        Fetch open positions from clearinghouseState.
        Returns assetPositions with szi != '0'.
        """
        data = await self._session_post(
            "/info",
            {"type": "clearinghouseState", "user": self.wallet_address},
        )
        positions: list[dict[str, Any]] = [
            p for p in data.get("assetPositions", [])
            if p.get("position", {}).get("szi", "0") != "0"
        ]
        return positions

    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session is not None:
            await self._session.close()
            self._session = None
