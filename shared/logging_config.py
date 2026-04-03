"""
Shared structlog configuration. Import `log` from here everywhere.
Level is read from the LOG_LEVEL environment variable (default: INFO).
"""
from __future__ import annotations

import logging
import os

import structlog

_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, _level_name, logging.INFO))

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        getattr(logging, _level_name, logging.INFO)
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

log = structlog.get_logger()
