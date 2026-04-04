"""
Exchange adapter: builds, signs, and submits Hyperliquid order actions.
See PRD Sections 2.9, 2.10, 2.11.

HTTP is handled via aiohttp (non-blocking).
EIP-712 signing uses hyperliquid-python-sdk utilities + eth_account.
"""
from __future__ import annotations

from typing import Any

import aiohttp
import structlog
from eth_account import Account
from eth_account.signers.local import LocalAccount
from hyperliquid.utils.constants import MAINNET_API_URL  # type: ignore[import-untyped]
from hyperliquid.utils.signing import get_timestamp_ms, sign_l1_action  # type: ignore[import-untyped]

from market_data.universe_snapshotter import rest_post
from oms.nonce_manager import NonceManager
from risk.watchdog import HeartbeatMonitor

log = structlog.get_logger()

_HL_TESTNET = "https://api.hyperliquid-testnet.xyz"


# ── Module-level stub (injection point patched by unit tests) ─────────────────

async def place_limit_order(
    coin: str,
    side: str,
    size_coins: float,
    price_str: str,
    tif: str,
) -> dict[str, Any]:
    """Stub — patched in unit tests. In production use ExchangeAdapter."""
    raise NotImplementedError(
        "place_limit_order not wired — instantiate ExchangeAdapter"
    )


# ── ExchangeAdapter ───────────────────────────────────────────────────────────

class ExchangeAdapter:
    """
    Async exchange adapter for Hyperliquid perp trading.

    Signing: uses eth_account + hyperliquid.utils.signing.sign_l1_action.
    HTTP:    uses aiohttp (non-blocking — does not freeze the event loop).

    Call build_coin_meta() once at startup before any orders.
    """

    def __init__(
        self,
        wallet_address: str,
        private_key: str,
        testnet: bool = True,
    ) -> None:
        self._wallet_address = wallet_address
        self._wallet: LocalAccount = Account.from_key(private_key)
        self.base_url = _HL_TESTNET if testnet else MAINNET_API_URL
        self._is_mainnet = not testnet
        self.nonce_manager = NonceManager()
        self._heartbeat_monitor = HeartbeatMonitor()
        self.coin_meta: dict[str, dict[str, Any]] = {}
        self._session: aiohttp.ClientSession | None = None

    # ── Startup ───────────────────────────────────────────────────────────────

    async def build_coin_meta(self) -> None:
        """
        Fetch universe metadata and cache asset_index + sz_decimals per coin.
        Must be called once at startup before any orders or subscriptions.
        """
        response = await rest_post("/info", {"type": "meta"})
        self.coin_meta = {
            asset["name"]: {
                "asset_index": i,
                "sz_decimals": asset["szDecimals"],
            }
            for i, asset in enumerate(response["universe"])
        }
        log.info("coin_meta_built", num_coins=len(self.coin_meta))

    def get_sz_decimals(self, coin: str) -> int:
        return int(self.coin_meta[coin]["sz_decimals"])

    def get_asset_index(self, coin: str) -> int:
        return int(self.coin_meta[coin]["asset_index"])

    @property
    def heartbeat_monitor(self) -> HeartbeatMonitor:
        return self._heartbeat_monitor

    # ── Signing ───────────────────────────────────────────────────────────────

    def _sign_action(self, action: dict[str, Any]) -> dict[str, Any]:
        """
        Sign an exchange action with EIP-712 and return the full payload.
        Uses get_timestamp_ms() as nonce (Hyperliquid convention).
        """
        nonce = get_timestamp_ms()
        signature = sign_l1_action(
            self._wallet,
            action,
            None,           # vault_address — None for regular accounts
            nonce,
            None,           # expires_after — None for no expiry
            self._is_mainnet,
        )
        return {
            "action": action,
            "nonce": nonce,
            "signature": signature,
            "vaultAddress": None,
            "expiresAfter": None,
        }

    # ── HTTP ──────────────────────────────────────────────────────────────────

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        url = self.base_url + path
        async with self._session.post(url, json=payload) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"POST {path} returned {resp.status}: {text}")
            result: dict[str, Any] = await resp.json()
            return result

    # ── Orders ────────────────────────────────────────────────────────────────

    async def place_limit_order(
        self,
        coin: str,
        side: str,
        size_coins: float,
        price_str: str,
        tif: str = "Gtc",
    ) -> dict[str, Any]:
        """
        Build, sign, and submit a Hyperliquid limit order.
        price_str must be pre-formatted via format_price().
        """
        asset_idx = self.get_asset_index(coin)
        sz_decimals = self.get_sz_decimals(coin)

        action: dict[str, Any] = {
            "type": "order",
            "orders": [
                {
                    "a": asset_idx,
                    "b": side == "buy",
                    "p": price_str,
                    "s": str(round(size_coins, sz_decimals)),
                    "r": False,
                    "t": {"limit": {"tif": tif}},
                }
            ],
            "grouping": "na",
        }
        payload = self._sign_action(action)
        return await self._post("/exchange", payload)

    async def cancel_all_orders(self) -> None:
        """Cancel all resting orders. Called on clean shutdown."""
        open_orders: list[dict[str, Any]] = await rest_post(
            "/info", {"type": "openOrders", "user": self._wallet_address}
        )
        if not open_orders:
            return
        cancel_action: dict[str, Any] = {
            "type": "cancel",
            "cancels": [
                {
                    "a": self.get_asset_index(order["coin"]),
                    "o": order["oid"],
                }
                for order in open_orders
            ],
        }
        payload = self._sign_action(cancel_action)
        await self._post("/exchange", payload)
        log.info("cancel_all_orders_done", count=len(open_orders))

    # ── Account state ─────────────────────────────────────────────────────────

    async def get_user_state(self) -> dict[str, Any]:
        result: dict[str, Any] = await rest_post(
            "/info",
            {"type": "clearinghouseState", "user": self._wallet_address},
        )
        # For unified accounts, USDC lives in spot until a perps position is opened.
        # Supplement accountValue with spot USDC if clearinghouse shows 0.
        if float(result.get("marginSummary", {}).get("accountValue", 0)) == 0:
            spot = await rest_post(
                "/info",
                {"type": "spotClearinghouseState", "user": self._wallet_address},
            )
            usdc_balance = next(
                (float(b["total"]) for b in spot.get("balances", []) if b["coin"] == "USDC"),
                0.0,
            )
            if usdc_balance > 0:
                result.setdefault("marginSummary", {})["accountValue"] = str(usdc_balance)
        return result

    async def get_open_positions(self) -> list[dict[str, Any]]:
        state = await self.get_user_state()
        return [
            p["position"]
            for p in state.get("assetPositions", [])
            if float(p["position"]["szi"]) != 0
        ]

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
