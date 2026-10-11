"""Manual-check queue (migration 012 + GET /admin/queue).

Uses the review-integration fixtures (LOCAL Postgres only).
"""

import psycopg2
import pytest

from tests.test_review_integration import AUTH, DB_URL, _sql, client  # noqa: F401

pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")


def _city(name, pop, *, outcome=None, mayor_url="https://x.gov/mayor", fetch_status="ok",
          leader=True, verified_days_ago=None):  # fmt: skip
    cid = _sql(
        "insert into cities (city, state_code, state_name, population, url, mayor_url, "
        "fetch_status, last_check_outcome, last_check_at) "
        "values (%s, 'QQ', 'Queueland', %s, 'https://x.gov', %s, %s, %s, "
        "case when %s::text is null then null else now() end) returning id",
        (name, pop, mayor_url, fetch_status, outcome, outcome),
    )[0]["id"]
    if leader:
        _sql(
            "insert into leaders (city_id, full_name, last_name, is_current, role, last_verified_at) "
            "values (%s, 'Pat Lee', 'Lee', true, 'chief_executive', "
            "case when %s::int is null then null else now() - make_interval(days => %s::int) end)",
            (cid, verified_days_ago, verified_days_ago),
        )
    return cid


@pytest.fixture
def queue(client):  # noqa: F811
    qq = "(select id from cities where state_code = 'QQ')"
    for table in ("verification_results", "leader_proposals", "leaders"):
        _sql(f"delete from {table} where city_id in {qq}")
    _sql("delete from cities where state_code = 'QQ'")
    ids = {
        "big_unverified": _city("Bigtown", 900_000),
        "no_leader": _city("Emptyville", 50_000, leader=False),
        "blocked": _city("Wallton", 80_000, outcome="blocked"),
        "soft_blocked": _city("Captcha City", 70_000, outcome="soft_blocked"),
        "no_name": _city("Blankburg", 60_000, outcome="no_name"),
        "dead_link": _city("Gonesville", 40_000, outcome="notfound"),
        "no_page": _city("Nopage", 30_000, mayor_url=None),
        "no_page_status": _city("Badfetch", 20_000, fetch_status="no_mayor_pg"),
        "fresh_match": _city("Goodtown", 500_000, outcome="match", verified_days_ago=3),
        "fresh_but_blocked": _city("Okayblock", 400_000, outcome="blocked", verified_days_ago=10),
        "stale": _city("Oldtown", 300_000, outcome="match", verified_days_ago=400),
    }
    return ids


def _get(client, kind, **params):  # noqa: F811
    r = client.get("/admin/queue", headers=AUTH, params={"kind": kind, "limit": 100, **params})
    assert r.status_code == 200, r.text
    body = r.json()
    return [c["city"] for c in body["cities"] if c["state_code"] == "QQ"], body


def test_each_tab_holds_the_right_cities(client, queue):  # noqa: F811
    assert _get(client, "unverified")[0] == ["Bigtown", "Oldtown", "Emptyville"]
    assert _get(client, "blocked")[0] == ["Wallton", "Captcha City"]
    assert _get(client, "no_name")[0] == ["Blankburg"]
    assert _get(client, "wrong_link")[0] == ["Gonesville", "Nopage", "Badfetch"]


def test_recently_verified_cities_are_in_no_tab(client, queue):  # noqa: F811
    everything = [
        c for k in ("unverified", "blocked", "no_name", "wrong_link") for c in _get(client, k)[0]
    ]
    assert "Goodtown" not in everything
    assert "Okayblock" not in everything, "verified 10 days ago: a blocked site can wait"
    assert len(everything) == len(set(everything)), "tabs are disjoint"


def test_counts_match_the_lists(client, queue):  # noqa: F811
    _, body = _get(client, "blocked")
    for kind in ("unverified", "blocked", "no_name", "wrong_link"):
        rows, _ = _get(client, kind)
        assert body["counts"][kind] >= len(rows)
    assert set(body["counts"]) == {"unverified", "blocked", "no_name", "wrong_link"}


def test_a_reviewer_save_clears_the_city(client, queue):  # noqa: F811
    r = client.post(
        f"/admin/cities/{queue['blocked']}/leaders",
        headers=AUTH,
        json={"full_name": "Pat Lee", "leader_title": "Mayor", "source": "https://x.gov/mayor"},
    )
    assert r.status_code == 201, r.text
    assert "Wallton" not in _get(client, "blocked")[0]


def test_bad_kind_and_auth(client):  # noqa: F811
    assert client.get("/admin/queue", headers=AUTH, params={"kind": "nope"}).status_code == 422
    assert client.get("/admin/queue").status_code in (401, 403)


def test_verifier_may_write_only_the_outcome_columns(queue):
    def as_verifier(sql):
        conn = psycopg2.connect(DB_URL)
        try:
            with conn.cursor() as cur:
                cur.execute("set role civicgrid_verifier")
                cur.execute(sql, (queue["big_unverified"],))
                return cur.rowcount
        finally:
            conn.rollback()
            conn.close()

    assert (
        as_verifier(
            "update cities set last_check_outcome = 'blocked', last_check_at = now() where id = %s"
        )
        == 1
    )
    with pytest.raises(psycopg2.errors.InsufficientPrivilege):
        as_verifier("update cities set mayor_url = 'https://evil.example' where id = %s")
