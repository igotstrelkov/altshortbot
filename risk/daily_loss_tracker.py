"""
Daily loss tracking with kill switch and 24h disable.
See PRD Section 9.5.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

import structlog

from shared.constants import DAILY_LOSS_DISABLE_PCT, DAILY_LOSS_KILL_PCT

log = structlog.get_logger()


class DailyLossTracker:
    """
    Tracks realised P&L within the UTC day. Resets at midnight UTC.
    The 24h disable persists across midnight; kill_active does not.
    """

    def __init__(self, account_equity: float) -> None:
        self.equity = account_equity
        self.daily_pnl = 0.0
        self.reset_date = datetime.utcnow().date()
        self.kill_active = False
        self.disable_until: datetime | None = None

    def record_close(self, pnl_usd: float) -> Literal["OK", "KILL", "DISABLE"]:
        """Call after every position close. Returns 'OK' | 'KILL' | 'DISABLE'."""
        self._maybe_reset()
        self.daily_pnl += pnl_usd
        loss_pct = -self.daily_pnl / self.equity

        if loss_pct >= DAILY_LOSS_DISABLE_PCT:
            self.disable_until = datetime.utcnow() + timedelta(hours=24)
            log.warning(
                "daily_loss_disable",
                loss_pct=f"{loss_pct:.2%}",
                msg="trading off for 24h",
            )
            return "DISABLE"

        if loss_pct >= DAILY_LOSS_KILL_PCT:
            self.kill_active = True
            log.warning("daily_loss_kill", loss_pct=f"{loss_pct:.2%}")
            return "KILL"

        return "OK"

    def is_trading_allowed(self) -> bool:
        """Check at the top of every scanner cycle."""
        self._maybe_reset()
        if self.disable_until and datetime.utcnow() < self.disable_until:
            return False
        return not self.kill_active

    def _maybe_reset(self) -> None:
        today = datetime.utcnow().date()
        if today > self.reset_date:
            self.daily_pnl = 0.0
            self.kill_active = False
            self.reset_date = today
            # disable_until intentionally NOT reset — 24h ban persists across midnight
