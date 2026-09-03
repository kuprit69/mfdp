from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock


class RateLimiter:
    """Thread-safe sliding-window rate limiter.

    Tracks recent "attempts" per key (e.g. an IP address or "ip:username" pair)
    and reports whether a new attempt is allowed. Used to slow down brute-force
    login attempts without needing Redis or any other external dependency -
    the state is kept in memory, so it resets on process restart and is only
    shared within a single process (fine for this project's scale).
    """

    def __init__(self, max_attempts: int = 5, window_seconds: float = 60.0) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._lock = Lock()
        self._attempts: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        bucket = self._attempts[key]
        cutoff = now - self.window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        return bucket

    def is_allowed(self, key: str) -> bool:
        """Return True if `key` still has attempts left in the current window.

        Does not itself record an attempt - call `record()` after checking to
        register this attempt (some callers only want to record failed
        attempts, so the two are kept separate).
        """
        now = time.monotonic()
        with self._lock:
            bucket = self._prune(key, now)
            return len(bucket) < self.max_attempts

    def record(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            bucket = self._prune(key, now)
            bucket.append(now)

    def retry_after_seconds(self, key: str) -> float:
        """How long until the oldest attempt in the window expires (0 if not limited)."""
        now = time.monotonic()
        with self._lock:
            bucket = self._prune(key, now)
            if len(bucket) < self.max_attempts:
                return 0.0
            return max(0.0, self.window_seconds - (now - bucket[0]))

    def reset(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)
