from __future__ import annotations

import time
from dataclasses import dataclass

from .errors import rate_limited


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._last_prune = time.monotonic()

    def check(
        self,
        key: str,
        rate_per_second: float,
        burst: int,
        cost: float = 1.0,
    ) -> None:
        now = time.monotonic()
        self._prune(now)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=float(burst), updated=now)
            self._buckets[key] = bucket
        elapsed = max(0.0, now - bucket.updated)
        bucket.tokens = min(float(burst), bucket.tokens + elapsed * rate_per_second)
        bucket.updated = now
        if bucket.tokens < cost:
            missing = cost - bucket.tokens
            retry_after_ms = int((missing / rate_per_second) * 1000) + 1
            raise rate_limited(retry_after_ms)
        bucket.tokens -= cost

    def _prune(self, now: float) -> None:
        if now - self._last_prune < 300:
            return
        self._last_prune = now
        for key, bucket in list(self._buckets.items()):
            if bucket.tokens >= 1 and now - bucket.updated > 900:
                del self._buckets[key]
