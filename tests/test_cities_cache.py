"""Unit tests for the /cities/all response cache (app/cache.py) and the
middleware that clears it after admin writes. No database or Redis needed:
a dict-backed fake stands in for Redis.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import cache as rcache


class FakeRedis:
    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value
        self.ttls[key] = ex

    def delete(self, *keys):
        for k in keys:
            self.store.pop(k, None)


class BrokenRedis:
    def get(self, key):
        raise ConnectionError("redis down")

    set = delete = get


@pytest.fixture
def fake(monkeypatch):
    f = FakeRedis()
    monkeypatch.setattr(rcache, "_client", f)
    monkeypatch.setattr(rcache, "_disabled", False)
    return f


def test_set_get_and_ttl(fake):
    rcache.set(rcache.CITIES_ALL_KEY, b'{"data":[]}', rcache.CITIES_ALL_TTL)
    assert rcache.get(rcache.CITIES_ALL_KEY) == b'{"data":[]}'
    assert fake.ttls[rcache.CITIES_ALL_KEY] == 3600


def test_invalidate_cities(fake):
    rcache.set(rcache.CITIES_ALL_KEY, b"x", 60)
    rcache.invalidate_cities()
    assert rcache.get(rcache.CITIES_ALL_KEY) is None


def test_no_redis_url_disables_cache(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    rcache.reset()
    assert rcache.get(rcache.CITIES_ALL_KEY) is None
    rcache.set(rcache.CITIES_ALL_KEY, b"x", 60)  # no-op, no error
    rcache.invalidate_cities()
    rcache.reset()


def test_redis_errors_fail_open(monkeypatch):
    monkeypatch.setattr(rcache, "_client", BrokenRedis())
    monkeypatch.setattr(rcache, "_disabled", False)
    assert rcache.get(rcache.CITIES_ALL_KEY) is None
    rcache.set(rcache.CITIES_ALL_KEY, b"x", 60)
    rcache.invalidate_cities()


def _app():
    from app.main import CacheInvalidationMiddleware

    app = FastAPI()
    app.add_middleware(CacheInvalidationMiddleware)

    @app.post("/admin/proposals/{pid}/approve")
    def approve(pid: int):
        return {"ok": True}

    @app.post("/admin/cities/{cid}/leaders")
    def add_leader(cid: int, fail: bool = False):
        from fastapi import HTTPException

        if fail:
            raise HTTPException(status_code=400, detail="bad")
        return {"ok": True}

    @app.get("/admin/cities/{cid}")
    def read_city(cid: int):
        return {"ok": True}

    @app.post("/admin/keys")
    def create_key():
        return {"ok": True}

    return TestClient(app)


@pytest.mark.parametrize(
    "method,path,cleared",
    [
        ("post", "/admin/proposals/1/approve", True),
        ("post", "/admin/cities/1/leaders", True),
        ("post", "/admin/cities/1/leaders?fail=true", False),  # failed write
        ("get", "/admin/cities/1", False),  # reads never clear
        ("post", "/admin/keys", False),  # unrelated admin write
    ],
)
def test_middleware_clears_cache_only_after_successful_city_writes(fake, method, path, cleared):
    client = _app()
    rcache.set(rcache.CITIES_ALL_KEY, b"cached", 60)
    getattr(client, method)(path)
    assert (rcache.get(rcache.CITIES_ALL_KEY) is None) is cleared
