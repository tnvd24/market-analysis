"""Shared token-bucket rate limiter.

Every outbound data source gets one. Upstox publishes a limit; NSE does not, and it is a
public exchange site we are a guest on — so we throttle ourselves there on principle, not
because an error code forced us to.
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, rate_per_sec: float = 8.0, burst: int = 8):
        self.rate = rate_per_sec
        self.burst = burst
        self._tokens = float(burst)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._tokens = min(self.burst, self._tokens + (now - self._updated) * self.rate)
            self._updated = now
            if self._tokens < 1.0:
                time.sleep((1.0 - self._tokens) / self.rate)
                self._tokens = 0.0
                self._updated = time.monotonic()
            else:
                self._tokens -= 1.0
