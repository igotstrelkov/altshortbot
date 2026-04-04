"""
Paper trading entry point — DRY_RUN=true, testnet by default.
No real orders are placed. Use this for signal validation before going live.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    os.environ.setdefault("DRY_RUN", "true")
    # Do NOT force HL_TESTNET — paper trading connects to mainnet for real signals.
    # Testnet has no balance and no real market activity.

    print("=" * 60)
    print("  AltShortBot — PAPER TRADING MODE")
    print("  DRY_RUN=true  |  No real orders will be placed")
    print("  Connects to MAINNET for real signal validation.")
    print("  Run for 48-72h and verify:")
    print("    - funding_series populated after bootstrap")
    print("    - Gate 1 fires for some coins")
    print("    - 2-5 coins on watch list at any time")
    print("    - 'trigger_fired dry_run=True' appears in logs")
    print("=" * 60)

    from main import main as _main
    asyncio.run(_main())


if __name__ == "__main__":
    main()
