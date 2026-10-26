"""Shared response cache for expensive, rarely changing reads (e.g. /cities/all).

Why: every /cities/all call pulls the full dataset from Supabase, and Supabase
bills egress per GB. The web app calls it from many short-lived serverless
instances, so without a shared cache Supabase sees one full pull per caller.
After the Oct 7 redesign that added up to ~300 MB/day and pushed the Free plan
over its 5 GB egress quota.

Design (mirrors app/rate_limit.py):
- Uses REDIS_URL; the redis import is lazy so dev mode does not require it.
- Fails open: if REDIS_URL is unset or Redis errors, callers get None and
  fall through to the database exactly as before.
- Stores the final JSON bytes, so a hit skips both the query and the encode.
- A TTL is the backstop for writes that bypass the API (the verifier scripts
  write to the database directly). Admin writes through the API invalidate
  immediately via CacheInvalidationMiddleware in main.py.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("civicgrid.cache")

CITIES_ALL_KEY = "cache:cities:all:v1"
CITIES_ALL_TTL = 3600  # seconds; data changes weekly at most

_client = None
_disabled = False


def _redis():
    """Return a shared Redis client, or None when caching is unavailable."""
    global _client, _disabled
    if _disabled:
        return None
    if _client is not None:
        return _client
    url = os.environ.get("REDIS_URL")
    if not url:
        log.warning("REDIS_URL not set: response cache disabled")
        _disabled = True
        return None
    try:
        import redis  # imported lazily so dev mode doesn't require it

        _client = redis.from_url(
            url,
            decode_responses=False,  # values are raw JSON bytes
            socket_timeout=0.5,
            socket_connect_timeout=0.5,
        )
        return _client
    except Exception as e:  # noqa: BLE001
        log.warning("Redis init failed, response cache disabled: %s", e)
        _disabled = True
        return None


def reset() -> None:
    """Forget the client and disabled flag (tests, or after REDIS_URL changes)."""
    global _client, _disabled
    _client = None
    _disabled = False


def get(key: str) -> bytes | None:
    r = _redis()
    if r is None:
        return None
    try:
        return r.get(key)
    except Exception as e:  # noqa: BLE001
        log.warning("cache get failed for %s, falling back to DB: %s", key, e)
        return None


def set(key: str, value: bytes, ttl: int) -> None:  # noqa: A001 (module API)
    r = _redis()
    if r is None:
        return
    try:
        r.set(key, value, ex=ttl)
    except Exception as e:  # noqa: BLE001
        log.warning("cache set failed for %s: %s", key, e)


def invalidate(*keys: str) -> None:
    """Drop cached entries. Call after any write that changes published data."""
    r = _redis()
    if r is None or not keys:
        return
    try:
        r.delete(*keys)
    except Exception as e:  # noqa: BLE001
        log.warning("cache invalidate failed for %s: %s", keys, e)


def invalidate_cities() -> None:
    invalidate(CITIES_ALL_KEY)
