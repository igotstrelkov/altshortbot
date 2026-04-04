"""
Live trading entry point — requires explicit confirmation before starting.
Only run after 48-72h paper trading confirms signal frequency is plausible.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> None:
    print("=" * 60)
    print("  AltShortBot — LIVE TRADING")
    print("  WARNING: Real capital will be at risk.")
    print("  Ensure you have completed 48-72h paper trading first.")
    print("=" * 60)
    confirmation = input("\nType CONFIRM to start live trading with real capital: ")
    if confirmation.strip() != "CONFIRM":
        print("Aborted.")
        sys.exit(0)

    os.environ["DRY_RUN"] = "false"

    print("\nStarting live trading...")

    from main import main as _main
    asyncio.run(_main())


if __name__ == "__main__":
    main()
