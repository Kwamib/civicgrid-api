"""Rate limits must hold across API pods, and a Redis outage must not take the API down.

Oct 10, 2026: production ran without Redis, so each pod counted on its own. A
Starter key sent ~123 requests/minute (limit 100) split over 3 pods and never
saw a 429.

The Redis tests need a reachable Redis (REDIS_TEST_URL, e.g.
redis://localhost:6379/15); they are skipped otherwise. They FLUSH that database.
"""

import os
import uuid

import pytest

from app.rate_limit import FailOpenBackend, InMemoryBackend, RedisBackend, get_backend

REDIS_URL = os.environ.get("REDIS_TEST_URL")
needs_redis = pytest.mark.skipif(not REDIS_URL, reason="REDIS_TEST_URL not set")


def _fresh_key():
    return f"cg_live_test_{uuid.uuid4().hex[:8]}"


@needs_redis
def test_two_pods_share_one_limit():
    pod_a, pod_b = RedisBackend(REDIS_URL), RedisBackend(REDIS_URL)
    key = _fresh_key()
    results = [(pod_a if i % 2 else pod_b).check_and_increment(key, "free") for i in range(14)]
    allowed = [r.allowed for r in results]
    assert allowed == [True] * 10 + [False] * 4, "free = 10/minute across ALL pods"
    assert results[-1].retry_after_seconds >= 1


def test_per_pod_counting_is_what_went_wrong():
    """Documents the old behaviour: separate in-memory counters double the limit."""
    pod_a, pod_b = InMemoryBackend(), InMemoryBackend()
    key = _fresh_key()
    allowed = [
        (pod_a if i % 2 else pod_b).check_and_increment(key, "free").allowed for i in range(20)
    ]
    assert all(allowed), "two pods x 10/minute each: nothing is refused"


class _Broken:
    def check_and_increment(self, key, tier):
        raise ConnectionError("redis down")


def test_redis_outage_fails_open_and_still_limits_per_pod():
    backend = FailOpenBackend(_Broken())
    key = _fresh_key()
    allowed = [backend.check_and_increment(key, "free").allowed for _ in range(12)]
    assert allowed == [True] * 10 + [False] * 2, "no exception; per-pod limit still applies"


def test_unreachable_redis_url_does_not_break_requests(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    backend = get_backend()
    assert isinstance(backend, FailOpenBackend)
    assert backend.check_and_increment(_fresh_key(), "pro").allowed


def test_no_redis_url_counts_per_pod(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert isinstance(get_backend(), InMemoryBackend)
