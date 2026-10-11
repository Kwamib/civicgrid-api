"""Self-serve keys are Free only; paid tiers are issued by request.

Before Oct 11, 2026 any signed-in user could create a Starter or Pro key
(including full export) from the dashboard dropdown or by calling the API.

Uses the review-integration fixtures (LOCAL Postgres only). The Supabase JWT
check is replaced with a fixed test user.
"""

import pytest

import app.me as me
from tests.test_review_integration import AUTH, DB_URL, _sql, client  # noqa: F401

pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")

USER = {"Authorization": "Bearer test-jwt"}


@pytest.fixture
def signed_in(client, monkeypatch):  # noqa: F811
    # api_keys.user_id exists in production (added outside the migrations).
    _sql("alter table api_keys add column if not exists user_id text")
    _sql("delete from api_keys where user_id = 'user-1' or user_email = 'paid@example.com'")
    monkeypatch.setattr(
        me,
        "verify_supabase_jwt",
        lambda token: me.JWTUser(user_id="user-1", email="dev@example.com"),
    )
    monkeypatch.delenv("SELF_SERVE_TIERS", raising=False)
    return client


def _tiers():
    return [r["tier"] for r in _sql("select tier from api_keys where user_id = 'user-1'")]


def test_free_key_is_created(signed_in):
    r = signed_in.post("/me/keys", headers=USER, json={"label": "mine"})
    assert r.status_code == 200, r.text
    assert r.json()["tier"] == "free"
    assert _tiers() == ["free"]


@pytest.mark.parametrize("tier", ["starter", "pro"])
def test_paid_tiers_are_by_request(signed_in, tier):
    r = signed_in.post("/me/keys", headers=USER, json={"tier": tier})
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert detail["error"] == "tier_by_request"
    assert "hello@civicgrid.org" in detail["message"]
    assert _tiers() == [], "no key may be created"


def test_admin_can_still_issue_paid_keys(signed_in):
    r = signed_in.post(
        "/admin/keys", headers=AUTH, json={"user_email": "paid@example.com", "tier": "starter"}
    )
    assert r.status_code in (200, 201), r.text
    assert _sql("select tier from api_keys where user_email = 'paid@example.com'") == [
        {"tier": "starter"}
    ]


def test_setting_can_reopen_a_tier(signed_in, monkeypatch):
    monkeypatch.setenv("SELF_SERVE_TIERS", "free,starter")
    r = signed_in.post("/me/keys", headers=USER, json={"tier": "starter"})
    assert r.status_code == 200, r.text
    assert r.json()["tier"] == "starter"
