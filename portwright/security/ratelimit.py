"""Rate limiting behind a small interface so the backend can be swapped
(in-memory for a single process; implement ``RateLimiter`` over Redis for a cluster)."""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from collections import deque


class RateLimiter(ABC):
    @abstractmethod
    def hit(self, key: str, limit: int, window: int) -> tuple[bool, int]:
        """Record a hit. Return ``(allowed, retry_after_seconds)``."""


class InMemoryRateLimiter(RateLimiter):
    """Sliding-window log limiter (accurate, O(limit) memory per key)."""

    def __init__(self, max_keys: int = 100_000):
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys

    def hit(self, key: str, limit: int, window: int) -> tuple[bool, int]:
        now = time.monotonic()
        with self._lock:
            q = self._hits.get(key)
            if q is None:
                if len(self._hits) >= self._max_keys:   # bound memory under key-spraying
                    self._hits.pop(next(iter(self._hits)))
                q = self._hits[key] = deque()
            while q and q[0] <= now - window:
                q.popleft()
            if len(q) >= limit:
                return False, max(1, int(q[0] + window - now) + 1)
            q.append(now)
            return True, 0
