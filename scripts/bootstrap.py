"""
Backtester CLI entrypoint.

Usage:
  python scripts/bootstrap.py --coins ETH,SOL --start 2024-01-01 --end 2024-06-01
  python scripts/bootstrap.py --coins ETH --start 2024-06-01 --end 2024-09-01 --equity 50000
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest.engine import BacktestEngine


def _fmt(value: float | int, pct: bool = False) -> str:
    if pct:
        return f"{value * 100:.2f}%"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _print_table(results: dict) -> None:
    per_coin = results["per_coin"]
    agg = results["aggregate"]

    header = f"{'Coin':<10} {'Trades':>7} {'Win%':>7} {'Sharpe':>8} {'MaxDD':>10} {'E[R]':>7} {'PnL%':>8}"
    sep = "─" * len(header)
    print(sep)
    print(header)
    print(sep)

    for coin, m in per_coin.items():
        print(
            f"{coin:<10} {m['total_trades']:>7} "
            f"{_fmt(m['win_rate'], pct=True):>7} "
            f"{m['sharpe_ratio']:>8.2f} "
            f"{_fmt(m['max_drawdown']):>10} "
            f"{m['expectancy_r']:>7.2f} "
            f"{_fmt(m['total_pnl_pct'], pct=True):>8}"
        )

    print(sep)
    print(
        f"{'AGGREGATE':<10} {agg['total_trades']:>7} "
        f"{_fmt(agg['win_rate'], pct=True):>7} "
        f"{agg['sharpe_ratio']:>8.2f} "
        f"{_fmt(agg['max_drawdown']):>10} "
        f"{agg['expectancy_r']:>7.2f} "
        f"{_fmt(agg['total_pnl_pct'], pct=True):>8}"
    )
    print(sep)


def _save_trades_csv(engine: BacktestEngine, results: dict) -> None:
    os.makedirs("logs", exist_ok=True)
    filename = f"logs/backtest_trades_{int(time.time())}.csv"

    # Re-run is wasteful; pass trades through results dict would need refactor.
    # Instead collect from per_coin results (metrics only — no raw trades).
    # For raw trade export, BacktestEngine would need to expose them separately.
    # For now write a summary CSV per coin.
    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["coin", "total_trades", "win_rate", "sharpe_ratio",
             "max_drawdown", "expectancy_r", "total_pnl_pct"]
        )
        for coin, m in results["per_coin"].items():
            writer.writerow(
                [coin, m["total_trades"], m["win_rate"], m["sharpe_ratio"],
                 m["max_drawdown"], m["expectancy_r"], m["total_pnl_pct"]]
            )
        agg = results["aggregate"]
        writer.writerow(
            ["AGGREGATE", agg["total_trades"], agg["win_rate"], agg["sharpe_ratio"],
             agg["max_drawdown"], agg["expectancy_r"], agg["total_pnl_pct"]]
        )
    print(f"\nResults saved to {filename}")


async def run(coins: list[str], start: str, end: str, equity: float, risk_pct: float) -> None:
    engine = BacktestEngine(
        coins=coins, start_date=start, end_date=end,
        initial_equity=equity, risk_pct=risk_pct,
    )
    results = await engine.run()
    _print_table(results)
    _save_trades_csv(engine, results)


def main() -> None:
    parser = argparse.ArgumentParser(description="AltShortBot Backtester")
    parser.add_argument("--coins", required=True, help="Comma-separated coin list, e.g. ETH,SOL")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--equity", type=float, default=10_000.0, help="Initial equity in USD")
    parser.add_argument("--risk-pct", type=float, default=0.01, help="Risk per trade as fraction (default 0.01 = 1%%)")
    args = parser.parse_args()

    coins = [c.strip().upper() for c in args.coins.split(",")]
    print(f"Backtesting {coins} from {args.start} to {args.end} (equity=${args.equity:,.0f}, risk={args.risk_pct*100:.1f}% per trade)\n")

    asyncio.run(run(coins, args.start, args.end, args.equity, args.risk_pct))


if __name__ == "__main__":
    main()
