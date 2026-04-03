"""
Paper trading entry point — DRY_RUN=true, testnet by default.
No real orders are placed. Use this for signal validation before going live.
"""
from __future__ import annotations

import asyncio
import os


def main() -> None:
    os.environ.setdefault("DRY_RUN", "true")
    os.environ.setdefault("HL_TESTNET", "true")

    print("=" * 60)
    print("  AltShortBot — PAPER TRADING MODE")
    print("  DRY_RUN=true  |  No real orders will be placed")
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
