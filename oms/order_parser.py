"""
Normalise raw Hyperliquid order placement responses.
See PRD Section 8.2.
"""
from __future__ import annotations

from typing import Any

import structlog

from shared.types import ParsedOrderStatus

log = structlog.get_logger()


def parse_order_status(raw_response: dict[str, Any]) -> ParsedOrderStatus | None:
    """
    Parse raw Hyperliquid order placement response into a normalised dataclass.

    Returns:
      ParsedOrderStatus(status='filled', avg_px=float, total_sz=float, oid=int)
      ParsedOrderStatus(status='resting', oid=int)
      ParsedOrderStatus(status='error', reason=str)
      None — malformed or empty response (logs the raw response)
    """
    try:
        statuses = raw_response["response"]["data"]["statuses"]
        if not statuses:
            log.warning("parse_order_status_empty_statuses", raw=str(raw_response))
            return None
        outcome = statuses[0]

        if "filled" in outcome:
            f = outcome["filled"]
            return ParsedOrderStatus(
                status="filled",
                avg_px=float(f["avgPx"]),
                total_sz=float(f["totalSz"]),
                oid=int(f["oid"]),
            )
        if "resting" in outcome:
            return ParsedOrderStatus(
                status="resting",
                oid=int(outcome["resting"]["oid"]),
            )
        if "error" in outcome:
            return ParsedOrderStatus(
                status="error",
                reason=str(outcome["error"]),
            )

        log.warning("parse_order_status_unknown_outcome", outcome=str(outcome))
        return None

    except (KeyError, IndexError, TypeError, ValueError) as exc:
        log.warning(
            "parse_order_status_malformed",
            error=str(exc),
            raw=str(raw_response),
        )
        return None
