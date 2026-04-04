"""
Runtime configuration loaded from environment variables.
Copy .env.example to .env and fill in values before running.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Required environment variable {name!r} is not set")
    return value


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in ("true", "1", "yes")


# ── Exchange credentials ──────────────────────────────────────────────────────
HL_API_WALLET_ADDRESS: str = _require("HL_API_WALLET_ADDRESS")
HL_PRIVATE_KEY: str = _require("HL_PRIVATE_KEY")
HL_TESTNET: bool = _bool("HL_TESTNET", False)

# ── Risk ──────────────────────────────────────────────────────────────────────
ACCOUNT_EQUITY_USD: float = _float("ACCOUNT_EQUITY_USD", 10_000.0)
MAX_CONCURRENT_POSITIONS: int = _int("MAX_CONCURRENT_POSITIONS", 3)

# ── Risk overrides ────────────────────────────────────────────────────────────
# These default to the values in shared/constants.py.
# Override in .env to tune without touching constants.
RISK_PER_TRADE_PCT: float = _float("RISK_PER_TRADE_PCT", 0.01)
DAILY_LOSS_KILL_PCT: float = _float("DAILY_LOSS_KILL_PCT", 0.03)
DAILY_LOSS_DISABLE_PCT: float = _float("DAILY_LOSS_DISABLE_PCT", 0.05)

# ── Safety ────────────────────────────────────────────────────────────────────
# DRY_RUN=true: log triggers but place no orders.
# Only set false after 48-72h dry run confirms signal frequency is plausible.
DRY_RUN: bool = _bool("DRY_RUN", True)

# ── Operational ───────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_DIR: str = os.getenv("LOG_DIR", "logs")
