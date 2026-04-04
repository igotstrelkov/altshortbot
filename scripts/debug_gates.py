"""
Diagnostic script: shows why Gate 1 passes or fails for a given coin and period.
Useful for finding backtest windows where the strategy actually fires.

Usage:
  python scripts/debug_gates.py --coin ETH --start 2024-01-01 --end 2024-04-01
  python scripts/debug_gates.py --coin WIF --start 2024-03-01 --end 2024-06-01
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import deque
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.data_loader import load_funding_history
from shared.constants import (
    GATE1_ANNUALISE_MULTIPLIER,
    GATE1_FUNDING_APR_THRESHOLD,
    GATE1_MIN_POSITIVE_HOURS,
    GATE1_PREMIUM_FLOOR,
)
from strategy.scanner.gate1 import gate1_passes


def _date_to_ms(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


async def run(coin: str, start: str, end: str) -> None:
    start_ms = _date_to_ms(start)
    end_ms = _date_to_ms(end)

    print(f"Loading funding for {coin} {start} → {end}...")
    df = await load_funding_history(coin, start_ms, end_ms)
    print(f"  {len(df)} funding entries\n")

    if df.empty:
        print("No funding data — check coin name or date range.")
        return

    # Replay the simulation cadence (one entry per 8h from Binance)
    funding_series: deque[float] = deque(maxlen=48)
    premium_series: deque[float] = deque(maxlen=12)

    gate1_fire_count = 0
    total_periods = 0

    for _, row in df.iterrows():
        funding_series.append(float(row["funding_rate"]))
        premium_series.append(float(row["premium"]))
        total_periods += 1

        if len(funding_series) < 8:
            continue

        if gate1_passes(funding_series, premium_series):
            gate1_fire_count += 1

    # Summary stats over the full period
    all_rates = list(df["funding_rate"])
    all_premiums = list(df["premium"])
    max_apr = max(r * GATE1_ANNUALISE_MULTIPLIER for r in all_rates)
    avg_apr = sum(r * GATE1_ANNUALISE_MULTIPLIER for r in all_rates) / len(all_rates)
    max_premium = max(all_premiums)

    print(f"{'─'*55}")
    print("  Gate 1 thresholds:")
    print(f"    APR threshold:     {GATE1_FUNDING_APR_THRESHOLD*100:.0f}%")
    print(f"    Premium floor:     {GATE1_PREMIUM_FLOOR:.4f}")
    print(f"    Min positive hrs:  {GATE1_MIN_POSITIVE_HOURS}/8")
    print(f"{'─'*55}")
    print(f"  Period stats for {coin}:")
    print(f"    Max annualised APR:  {max_apr*100:.2f}%")
    print(f"    Avg annualised APR:  {avg_apr*100:.2f}%")
    print(f"    Max premium:         {max_premium:.6f}")
    print(f"{'─'*55}")
    print(f"  Gate 1 fired:  {gate1_fire_count} / {total_periods - 7} periods")
    fire_rate = gate1_fire_count / max(1, total_periods - 7)
    print(f"  Fire rate:     {fire_rate*100:.1f}%")
    print(f"{'─'*55}")

    if gate1_fire_count == 0:
        if max_apr < GATE1_FUNDING_APR_THRESHOLD:
            print(f"\n  ✗ APR never reached threshold ({max_apr*100:.1f}% peak vs {GATE1_FUNDING_APR_THRESHOLD*100:.0f}% required)")
        if max_premium < GATE1_PREMIUM_FLOOR:
            print(f"  ✗ Premium never reached floor ({max_premium:.6f} peak vs {GATE1_PREMIUM_FLOOR:.4f} required)")
        print("\n  Try a different period or coin with more elevated funding.")
    else:
        print("\n  ✓ Gate 1 fires — this period has usable signal for backtesting.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Debug Gate 1 for a coin/period")
    parser.add_argument("--coin", required=True)
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    args = parser.parse_args()
    asyncio.run(run(args.coin.upper(), args.start, args.end))


if __name__ == "__main__":
    main()
