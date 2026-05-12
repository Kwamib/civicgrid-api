"""Sliding-window rate limiting for CivicGrid.

Two limits enforced per API key:
  - per-minute (burst protection)
  - per-day (tier ceiling)

Backend abstraction lets us start with an in-memory counter (good for single
process dev) and swap to Redis (required for multi-pod production) by flipping
an env var.

Returns 429 Too Many Requests with standard headers:
  X-RateLimit-Limit-Day
  X-RateLimit-Remaining-Day
  X-RateLimit-Reset-Day
  X-RateLimit-Limit-Minute
  X-RateLimit-Remaining-Minute
  Retry-After
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

# ---------------------------------------------------------------------------
# Tier configuration
# ---------------------------------------------------------------------------

TIERS: dict[str, dict[str, int]] = {
    "free": {"per_day": 100, "per_minute": 10},
    "starter": {"per_day": 10_000, "per_minute": 100},
    "pro": {"per_day": 100_000, "per_minute": 500},
}

DEFAULT_TIER = "free"


@dataclass
class LimitCheck:
    allowed: bool
    day_limit: int
    day_used: int
    day_reset_seconds: int
    minute_limit: int
    minute_used: int
    retry_after_seconds: int  # 0 if allowed


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class InMemoryBackend:
    """Thread-safe in-memory sliding window. Single-process only.

    For each key we keep a deque of request timestamps. Old entries get pruned
    on each check. Memory usage is bounded by the per-day limit (~100 entries
    for free tier).
    """

    def __init__(self):
        self._buckets: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check_and_increment(self, key: str, tier: str) -> LimitCheck:
        limits = TIERS.get(tier, TIERS[DEFAULT_TIER])
        day_limit = limits["per_day"]
        minute_limit = limits["per_minute"]

        now = time.time()
        day_window = now - 86400  # 24h sliding window
        minute_window = now - 60

        with self._lock:
            bucket = self._buckets.setdefault(key, deque())

            # Prune entries older than 24h.
            while bucket and bucket[0] < day_window:
                bucket.popleft()

            # Count requests in each window.
            day_used = len(bucket)
            minute_used = sum(1 for ts in bucket if ts >= minute_window)

            # Day limit check.
            if day_used >= day_limit:
                oldest = bucket[0] if bucket else now
                retry = max(int(oldest + 86400 - now), 1)
                return LimitCheck(
                    allowed=False,
                    day_limit=day_limit,
                    day_used=day_used,
                    day_reset_seconds=retry,
                    minute_limit=minute_limit,
                    minute_used=minute_used,
                    retry_after_seconds=retry,
                )

            # Minute limit check.
            if minute_used >= minute_limit:
                oldest_in_minute = next((ts for ts in bucket if ts >= minute_window), now)
                retry = max(int(oldest_in_minute + 60 - now), 1)
                return LimitCheck(
                    allowed=False,
                    day_limit=day_limit,
                    day_used=day_used,
                    day_reset_seconds=86400,
                    minute_limit=minute_limit,
                    minute_used=minute_used,
                    retry_after_seconds=retry,
                )

            # Allowed. Record this request.
            bucket.append(now)
            return LimitCheck(
                allowed=True,
                day_limit=day_limit,
                day_used=day_used + 1,
                day_reset_seconds=86400,
                minute_limit=minute_limit,
                minute_used=minute_used + 1,
                retry_after_seconds=0,
            )


class RedisBackend:
    """Redis sorted-set sliding window. Multi-process safe.

    Uses ZADD/ZREMRANGEBYSCORE/ZCARD pattern. One sorted set per key, scores
    are timestamps. Atomically pruned on each check.
    """

    def __init__(self, redis_url: str):
        import redis  # imported lazily so dev mode doesn't require it

        self._r = redis.from_url(redis_url, decode_responses=True)

    def check_and_increment(self, key: str, tier: str) -> LimitCheck:
        limits = TIERS.get(tier, TIERS[DEFAULT_TIER])
        day_limit = limits["per_day"]
        minute_limit = limits["per_minute"]

        now = time.time()
        day_window = now - 86400
        minute_window = now - 60

        zkey = f"rl:{key}"
        pipe = self._r.pipeline()
        pipe.zremrangebyscore(zkey, 0, day_window)  # prune
        pipe.zcard(zkey)  # day count
        pipe.zcount(zkey, minute_window, "+inf")  # minute count
        _, day_used, minute_used = pipe.execute()

        if day_used >= day_limit:
            oldest = self._r.zrange(zkey, 0, 0, withscores=True)
            oldest_ts = oldest[0][1] if oldest else now
            retry = max(int(oldest_ts + 86400 - now), 1)
            return LimitCheck(
                allowed=False,
                day_limit=day_limit,
                day_used=day_used,
                day_reset_seconds=retry,
                minute_limit=minute_limit,
                minute_used=minute_used,
                retry_after_seconds=retry,
            )

        if minute_used >= minute_limit:
            oldest = self._r.zrangebyscore(
                zkey, minute_window, "+inf", start=0, num=1, withscores=True
            )
            oldest_ts = oldest[0][1] if oldest else now
            retry = max(int(oldest_ts + 60 - now), 1)
            return LimitCheck(
                allowed=False,
                day_limit=day_limit,
                day_used=day_used,
                day_reset_seconds=86400,
                minute_limit=minute_limit,
                minute_used=minute_used,
                retry_after_seconds=retry,
            )

        # Record the request.
        pipe = self._r.pipeline()
        pipe.zadd(zkey, {str(now): now})
        pipe.expire(zkey, 86400 + 60)  # cleanup if key goes idle
        pipe.execute()

        return LimitCheck(
            allowed=True,
            day_limit=day_limit,
            day_used=day_used + 1,
            day_reset_seconds=86400,
            minute_limit=minute_limit,
            minute_used=minute_used + 1,
            retry_after_seconds=0,
        )


def get_backend():
    """Construct the configured backend. Reads REDIS_URL env var; falls back to in-memory."""
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        try:
            return RedisBackend(redis_url)
        except Exception as e:
            print(f"[rate_limit] Redis init failed, falling back to in-memory: {e}")
    return InMemoryBackend()


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Enforces tier-based rate limits on authenticated requests.

    Must be installed AFTER AuthMiddleware so request.state.auth is populated.
    Skips requests with no auth context (public routes, admin routes).
    """

    def __init__(self, app, backend=None):
        super().__init__(app)
        self.backend = backend or get_backend()

    async def dispatch(self, request: Request, call_next) -> Response:
        auth = getattr(request.state, "auth", None)
        if auth is None:
            return await call_next(request)

        check = self.backend.check_and_increment(auth.key_prefix, auth.tier)

        if not check.allowed:
            import json

            body = json.dumps(
                {
                    "error": "rate_limit_exceeded",
                    "message": (
                        f"Rate limit exceeded for tier '{auth.tier}'. "
                        f"Retry after {check.retry_after_seconds} seconds."
                    ),
                    "tier": auth.tier,
                    "retry_after_seconds": check.retry_after_seconds,
                }
            )
            return Response(
                content=body,
                status_code=429,
                media_type="application/json",
                headers=_rate_limit_headers(check)
                | {
                    "Retry-After": str(check.retry_after_seconds),
                },
            )

        response = await call_next(request)
        for k, v in _rate_limit_headers(check).items():
            response.headers[k] = v
        return response


def _rate_limit_headers(check: LimitCheck) -> dict[str, str]:
    return {
        "X-RateLimit-Limit-Day": str(check.day_limit),
        "X-RateLimit-Remaining-Day": str(max(check.day_limit - check.day_used, 0)),
        "X-RateLimit-Reset-Day": str(check.day_reset_seconds),
        "X-RateLimit-Limit-Minute": str(check.minute_limit),
        "X-RateLimit-Remaining-Minute": str(max(check.minute_limit - check.minute_used, 0)),
    }
