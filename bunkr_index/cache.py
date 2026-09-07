"""Small bounded time-to-live cache shared by the HTTP API.

Search responses are cheap to produce for typical queries (sub-millisecond to a
few milliseconds), but broad full-text queries and deep prefix scans cost
meaningfully more. The cache holds serialized response bodies for a short TTL
so repeated or revalidated requests skip the SQLite work entirely.

The cache is intentionally conservative: entries expire after ``ttl`` seconds
so new metadata written by the separate ``sync`` process becomes visible
quickly. It never persists across process restarts.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any


class TTLCache:
    """Thread-safe LRU cache with per-entry TTL, keyed by string.

    ``maxsize`` bounds the number of entries; eviction is least-recently-used.
    """

    def __init__(self, maxsize: int = 1024, ttl: float = 60.0) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        if ttl <= 0:
            raise ValueError("ttl must be > 0")
        self.maxsize = maxsize
        self.ttl = ttl
        self._lock = threading.Lock()
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Any | None:
        """Return the cached value for ``key``, or None if absent/expired."""
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires_at, value = item
            if expires_at <= time.monotonic():
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: str, value: Any) -> None:
        """Store ``value`` under ``key``, expiring after ``self.ttl`` seconds."""
        with self._lock:
            self._data[key] = (time.monotonic() + self.ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
