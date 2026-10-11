"""Verified-key cache: bcrypt once per key per minute, not on every request.

Oct 10, 2026: bcrypt on every request cost ~0.1 CPU-second per call, so one
looping client pushed the API to its pod limit. Uses the review-integration
fixtures (LOCAL Postgres only).
"""

import pytest

import app.auth as auth
from tests.test_review_integration import AUTH, DB_URL, _sql, client  # noqa: F401

pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
def key(client, monkeypatch):  # noqa: F811
    auth.clear_key_cache()
    calls = {"bcrypt": 0}
    real = auth.verify_key

    def counting(full_key, stored_hash):
        calls["bcrypt"] += 1
        return real(full_key, stored_hash)

    monkeypatch.setattr(auth, "verify_key", counting)
    r = client.post(
        "/admin/keys", headers=AUTH, json={"user_email": "cache@example.com", "tier": "pro"}
    )
    assert r.status_code in (200, 201), r.text
    full = r.json()["key"]
    yield {"full": full, "prefix": full[: auth.PREFIX_LENGTH], "calls": calls}
    auth.clear_key_cache()


def _get(client, full_key):  # noqa: F811
    return client.get("/stats", headers={"Authorization": f"Bearer {full_key}"})


def test_bcrypt_runs_once_for_repeat_requests(client, key):  # noqa: F811
    for _ in range(5):
        assert _get(client, key["full"]).status_code == 200
    assert key["calls"]["bcrypt"] == 1


def test_wrong_key_with_same_prefix_is_still_rejected(client, key):  # noqa: F811
    assert _get(client, key["full"]).status_code == 200
    forged = key["prefix"] + "x" * (len(key["full"]) - auth.PREFIX_LENGTH)
    assert _get(client, forged).status_code == 401


def test_revoked_key_stops_working_immediately_on_this_pod(client, key):  # noqa: F811
    assert _get(client, key["full"]).status_code == 200
    r = client.delete(f"/admin/keys/{key['prefix']}", headers=AUTH)
    assert r.status_code == 200, r.text
    assert _get(client, key["full"]).status_code == 401


def test_cache_expires(client, key, monkeypatch):  # noqa: F811
    assert _get(client, key["full"]).status_code == 200
    now = auth.time.monotonic()
    monkeypatch.setattr(auth.time, "monotonic", lambda: now + auth.KEY_CACHE_SECONDS + 1)
    assert _get(client, key["full"]).status_code == 200
    assert key["calls"]["bcrypt"] == 2


def test_usage_counts_are_batched_but_not_lost(client, key):  # noqa: F811
    for _ in range(3):
        assert _get(client, key["full"]).status_code == 200
    first = _sql("select request_count from api_keys where key_prefix = %s", (key["prefix"],))
    assert first == [{"request_count": 1}], "first request writes; the next two wait for the batch"
    key_id = _sql("select id from api_keys where key_prefix = %s", (key["prefix"],))[0]["id"]
    assert auth._pending_touch[key_id][0] == 2
