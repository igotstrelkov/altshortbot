"""
Thread-safe monotonic nonce counter for Hyperliquid order signing.
See PRD Section 2.9.
"""
from __future__ import annotations

import threading


class NonceManager:
    """
    Atomically incrementing nonce counter.
    Hyperliquid stores nonces per signer — one NonceManager per process.
    NOTE: In production, initialise _nonce from current timestamp ms
    (int(time.time() * 1000)) to avoid reuse after a process restart.
    """

    def __init__(self) -> None:
        self._nonce = 0
        self._lock = threading.Lock()

    def next_nonce(self) -> int:
        with self._lock:
            self._nonce += 1
            return self._nonce
