"""
Backtest performance metrics.
See PRD Section 11.4.
"""
from __future__ import annotations

import math
import statistics


def compute_metrics(trades: list[dict], initial_equity: float) -> dict:
    """
    Compute performance metrics from a list of closed trades.

    Each trade dict must have:
        entry_px, exit_px, size_coins, funding_collected_usd,
        entry_time, exit_time, stop_distance_pct

    Returns:
        sharpe_ratio, max_drawdown, win_rate, expectancy_r,
        total_trades, total_pnl_pct
    """
    total_trades = len(trades)

    if total_trades == 0:
        return {
            "sharpe_ratio": 0.0,
            "max_drawdown": 0.0,
            "win_rate": 0.0,
            "expectancy_r": 0.0,
            "total_trades": 0,
            "total_pnl_pct": 0.0,
        }

    pnl_usd_list: list[float] = []
    pnl_pct_list: list[float] = []
    r_list: list[float] = []

    for t in trades:
        entry_px: float = t["entry_px"]
        exit_px: float = t["exit_px"]
        size_coins: float = t["size_coins"]
        funding: float = t["funding_collected_usd"]
        stop_pct: float = t["stop_distance_pct"]

        # Short P&L: profit when price falls
        pnl_usd = (entry_px - exit_px) * size_coins + funding
        position_value = entry_px * size_coins
        pnl_pct = pnl_usd / position_value if position_value > 0 else 0.0
        r_multiple = pnl_pct / stop_pct if stop_pct > 0 else 0.0

        pnl_usd_list.append(pnl_usd)
        pnl_pct_list.append(pnl_pct)
        r_list.append(r_multiple)

    # Win rate
    win_rate = sum(1 for p in pnl_usd_list if p > 0) / total_trades

    # Expectancy in R
    expectancy_r = statistics.mean(r_list)

    # Total P&L as fraction of initial equity
    total_pnl_pct = sum(pnl_usd_list) / initial_equity

    # Max drawdown: peak-to-trough of cumulative pnl_usd curve
    max_drawdown = _max_drawdown(pnl_usd_list)

    # Annualised Sharpe (0 risk-free rate)
    sharpe_ratio = _sharpe(pnl_pct_list, trades)

    return {
        "sharpe_ratio": sharpe_ratio,
        "max_drawdown": max_drawdown,
        "win_rate": win_rate,
        "expectancy_r": expectancy_r,
        "total_trades": total_trades,
        "total_pnl_pct": total_pnl_pct,
    }


def _max_drawdown(pnl_usd_list: list[float]) -> float:
    """Peak-to-trough of the cumulative P&L curve, expressed as positive USD."""
    peak = 0.0
    max_dd = 0.0
    cumulative = 0.0
    for pnl in pnl_usd_list:
        cumulative += pnl
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _sharpe(pnl_pct_list: list[float], trades: list[dict]) -> float:
    """
    Annualised Sharpe ratio from per-trade returns.
    trades_per_year = total_trades / years_in_backtest
    """
    if len(pnl_pct_list) < 2:
        return 0.0

    std = statistics.stdev(pnl_pct_list)
    if std == 0:
        return 0.0

    mean = statistics.mean(pnl_pct_list)

    # Infer backtest duration from first/last trade timestamps (ms)
    start_ms = min(t["entry_time"] for t in trades)
    end_ms = max(t["exit_time"] for t in trades)
    years = (end_ms - start_ms) / (1000 * 3600 * 24 * 365.25)
    if years <= 0:
        return 0.0

    trades_per_year = len(trades) / years
    return (mean / std) * math.sqrt(trades_per_year)
