"""
Daily loss tracking with kill switch and 24h disable.
See PRD Section 9.5.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

import structlog

import config.settings as settings

log = structlog.get_logger()


class DailyLossTracker:
    """
    Tracks realised P&L within the UTC day. Resets at midnight UTC.
    The 24h disable persists across midnight; kill_active does not.
    """

    def __init__(
        self,
        account_equity: float,
        kill_pct: float | None = None,
        disable_pct: float | None = None,
    ) -> None:
        self.equity = account_equity
        self.kill_pct = kill_pct if kill_pct is not None else settings.DAILY_LOSS_KILL_PCT
        self.disable_pct = disable_pct if disable_pct is not None else settings.DAILY_LOSS_DISABLE_PCT
        self.daily_pnl = 0.0
        self.reset_date = datetime.utcnow().date()
        self.kill_active = False
        self.disable_until: datetime | None = None

    def record_close(self, pnl_usd: float) -> Literal["OK", "KILL", "DISABLE"]:
        """Call after every position close. Returns 'OK' | 'KILL' | 'DISABLE'."""
        self._maybe_reset()
        self.daily_pnl += pnl_usd
        loss_pct = -self.daily_pnl / self.equity

        if loss_pct >= self.disable_pct:
            self.disable_until = datetime.utcnow() + timedelta(hours=24)
            log.warning(
                "daily_loss_disable",
                loss_pct=f"{loss_pct:.2%}",
                msg="trading off for 24h",
            )
            return "DISABLE"

        if loss_pct >= self.kill_pct:
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
