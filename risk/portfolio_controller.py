"""
Stop distance calculation, position sizing, and funding exit logic.
See PRD Sections 9.1–9.3.
"""
from __future__ import annotations

import structlog

import config.settings as settings
from shared.constants import (
    ATR_MULTIPLIER_HIGH_VOL,
    ATR_MULTIPLIER_NORMAL,
    FUNDING_EXIT_PNL_THRESHOLD_R,
    MIN_STOP_DISTANCE_PCT,
    SQUEEZE_HARD_BLOCK_SCORE,
    SQUEEZE_REDUCE_MULTIPLIER,
    SQUEEZE_REDUCE_SCORE,
)

log = structlog.get_logger()


def calculate_stop_distance(
    entry_price: float,
    atr_14: float,
    swing_high_price: float,
    high_volatility: bool,
) -> float:
    """
    For a short, stop is above entry. Returns fractional distance from entry.
    Uses the tighter of ATR stop vs swing high stop.
    """
    multiplier = ATR_MULTIPLIER_HIGH_VOL if high_volatility else ATR_MULTIPLIER_NORMAL
    atr_stop_price = entry_price + (multiplier * atr_14)

    if swing_high_price <= entry_price or atr_14 == 0:
        stop_price = atr_stop_price
    else:
        stop_price = min(atr_stop_price, swing_high_price)

    distance = (stop_price - entry_price) / entry_price

    if distance < MIN_STOP_DISTANCE_PCT:
        log.warning(
            "stop_distance_floor_applied",
            computed=f"{distance:.3%}",
            floor=f"{MIN_STOP_DISTANCE_PCT:.3%}",
        )
        distance = MIN_STOP_DISTANCE_PCT

    return distance


def calculate_position_size(
    account_equity: float,
    regime: str,
    squeeze_score: int,
    stop_distance_pct: float,
    risk_pct: float | None = None,
) -> float:
    """
    Returns position NOTIONAL in USD (not risk budget).
    Formula: notional = risk_budget / stop_distance_pct
    Example: $10k equity, 1% risk, 2% stop → $100 / 0.02 = $5,000 notional
    """
    if stop_distance_pct <= 0:
        raise ValueError(f"stop_distance_pct must be positive, got {stop_distance_pct}")

    risk_budget = account_equity * (risk_pct if risk_pct is not None else settings.RISK_PER_TRADE_PCT)

    regime_multipliers = {"NORMAL": 1.0, "REDUCED": 0.5, "DISABLED": 0.0}
    risk_budget *= regime_multipliers.get(regime, 0.0)

    if squeeze_score >= SQUEEZE_HARD_BLOCK_SCORE:
        log.info("position_size_calculated", action="HARD_BLOCK",
                 squeeze_score=squeeze_score, threshold=SQUEEZE_HARD_BLOCK_SCORE,
                 regime=regime, notional_usd=0.0)
        return 0.0

    squeeze_reduced = False
    if squeeze_score >= SQUEEZE_REDUCE_SCORE:
        risk_budget *= SQUEEZE_REDUCE_MULTIPLIER
        squeeze_reduced = True

    notional = risk_budget / stop_distance_pct
    log.info(
        "position_size_calculated",
        action="ALLOW",
        account_equity=round(account_equity, 2),
        regime=regime,
        regime_multiplier=regime_multipliers.get(regime, 0.0),
        squeeze_score=squeeze_score,
        squeeze_reduced=squeeze_reduced,
        stop_distance_pct=round(stop_distance_pct * 100, 3),
        risk_budget_usd=round(risk_budget, 4),
        notional_usd=round(notional, 2),
    )
    return notional


def check_funding_exit(current_funding_rate: float, current_pnl_r: float) -> bool:
    """
    Returns True if position should exit due to adverse funding carry.
    current_funding_rate: per-hour rate. Negative = you are paying (bad for a short).
    current_pnl_r: unrealised profit in R multiples.
    """
    if current_funding_rate < 0 and current_pnl_r < FUNDING_EXIT_PNL_THRESHOLD_R:
        log.info(
            "funding_exit_triggered",
            funding_rate=f"{current_funding_rate:.6f}",
            pnl_r=f"{current_pnl_r:.2f}",
        )
        return True
    return False
