"""Small, thread-safe in-process sliding-window rate limiter."""

import math
import time
from collections import deque
from collections.abc import Callable
from threading import Lock


class RateLimiter:
    """Limit each key to a fixed number of requests in a sliding window."""

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        if limit < 1:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if max_keys < 1:
            raise ValueError("max_keys must be positive")
        self.limit = limit
        self.window_seconds = window_seconds
        self._clock = clock
        self._max_keys = max_keys
        self._requests: dict[str, deque[float]] = {}
        self._checks_since_cleanup = 0
        self._lock = Lock()

    def check(self, key: str) -> tuple[bool, int]:
        """Consume one request, returning ``(allowed, retry_after_seconds)``."""
        with self._lock:
            # Read the clock under the same lock as deque mutation so concurrent
            # callers cannot append timestamps out of order.
            now = self._clock()
            cutoff = now - self.window_seconds

            # Periodic cleanup avoids an O(number-of-clients) scan on every
            # request. The hard cap bounds memory even during a rotating-IP
            # flood inside one window.
            self._checks_since_cleanup += 1
            if self._checks_since_cleanup >= 100:
                self._prune_stale(cutoff)
                self._checks_since_cleanup = 0

            if key not in self._requests and len(self._requests) >= self._max_keys:
                # Recheck expiry before rejecting an unseen key. Never evict an
                # active counter: doing so would let a rotating-key flood reset
                # offenders and defeat the limiter under abuse.
                self._prune_stale(cutoff)
                if len(self._requests) >= self._max_keys:
                    next_capacity = min(
                        timestamps[-1] + self.window_seconds
                        for timestamps in self._requests.values()
                    )
                    return False, max(1, math.ceil(next_capacity - now))

            timestamps = self._requests.setdefault(key, deque())
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()

            if len(timestamps) >= self.limit:
                retry_after = math.ceil(timestamps[0] + self.window_seconds - now)
                return False, max(1, retry_after)

            timestamps.append(now)
            return True, 0

    def _prune_stale(self, cutoff: float) -> None:
        stale_keys = [
            key
            for key, timestamps in self._requests.items()
            if not timestamps or timestamps[-1] <= cutoff
        ]
        for key in stale_keys:
            del self._requests[key]

    def reset(self) -> None:
        """Clear all counters (primarily useful for isolated tests)."""
        with self._lock:
            self._requests.clear()
            self._checks_since_cleanup = 0
