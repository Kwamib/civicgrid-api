"""Discovery's link notes and suggestions in Fix-a-city (migration 013).

Oct 11, 2026: discovery v12 looks cities up in CISA's official .gov list. What
it can't fix itself (Columbus, NE stored Columbus, OHIO's columbus.gov; a
city's .gov refused our script) it leaves as cities.link_note and
cities.suggested_url for a person. Uses the review-integration fixtures
(LOCAL Postgres only).
"""

import pytest

from tests.test_review_integration import AUTH, DB_URL, _sql, client  # noqa: F401

pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")


def _city(name, pop, *, note=None, suggested=None, verified_days_ago=None):
    cid = _sql(
        "insert into cities (city, state_code, state_name, population, url, mayor_url, "
        "fetch_status, link_note, suggested_url) "
        "values (%s, 'LQ', 'Linkland', %s, 'https://wrong.gov', 'https://wrong.gov/mayor', "
        "'ok', %s, %s) returning id",
        (name, pop, note, suggested),
    )[0]["id"]
    _sql(
        "insert into leaders (city_id, full_name, last_name, is_current, role, last_verified_at) "
        "values (%s, 'Pat Lee', 'Lee', true, 'chief_executive', "
        "case when %s::int is null then null else now() - make_interval(days => %s::int) end)",
        (cid, verified_days_ago, verified_days_ago),
    )
    return cid


@pytest.fixture
def cities(client):  # noqa: F811
    lq = "(select id from cities where state_code = 'LQ')"
    for table in ("verification_results", "leader_proposals", "leaders"):
        _sql(f"delete from {table} where city_id in {lq}")
    _sql("delete from cities where state_code = 'LQ'")
    return {
        "foreign": _city(
            "Columbus", 23_000, note="website belongs to City of Columbus (OH)", verified_days_ago=5
        ),
        "suggested": _city(
            "Smalltown",
            5_000,
            note="the city's .gov refused our script",
            suggested="https://www.smalltownlq.gov",
        ),
        "plain": _city("Plainville", 90_000),
    }


def _wrong_link(client):  # noqa: F811
    r = client.get("/admin/queue", headers=AUTH, params={"kind": "wrong_link", "limit": 100})
    assert r.status_code == 200, r.text
    return [c for c in r.json()["cities"] if c["state_code"] == "LQ"]


def test_noted_cities_are_wrong_link_even_when_recently_verified(client, cities):  # noqa: F811
    rows = _wrong_link(client)
    assert [c["city"] for c in rows] == ["Smalltown", "Columbus"], "suggestions first"
    assert rows[0]["suggested_url"] == "https://www.smalltownlq.gov"
    assert rows[1]["link_note"] == "website belongs to City of Columbus (OH)"
    unverified = client.get("/admin/queue", headers=AUTH, params={"kind": "unverified"}).json()
    assert "Plainville" in [c["city"] for c in unverified["cities"] if c["state_code"] == "LQ"]


def test_applying_a_website_clears_the_note(client, cities):  # noqa: F811
    r = client.patch(
        f"/admin/cities/{cities['suggested']}/links",
        headers=AUTH,
        json={"url": "https://www.smalltownlq.gov"},
    )
    assert r.status_code == 200, r.text
    row = _sql(
        "select url, link_note, suggested_url from cities where id = %s", (cities["suggested"],)
    )[0]
    assert row == {"url": "https://www.smalltownlq.gov", "link_note": None, "suggested_url": None}
    assert "Smalltown" not in [c["city"] for c in _wrong_link(client)]


def test_leadership_page_alone_keeps_the_note(client, cities):  # noqa: F811
    r = client.patch(
        f"/admin/cities/{cities['foreign']}/links",
        headers=AUTH,
        json={"mayor_url": "https://www.columbusne.us/mayor", "confirm_official": True},
    )
    assert r.status_code == 200, r.text
    assert (
        _sql("select link_note from cities where id = %s", (cities["foreign"],))[0]["link_note"]
        == "website belongs to City of Columbus (OH)"
    ), "the website itself is still wrong"


def test_search_returns_the_note(client, cities):  # noqa: F811
    r = client.get("/admin/cities/search", headers=AUTH, params={"q": "Columbus"})
    assert r.status_code == 200, r.text
    row = next(c for c in r.json()["cities"] if c["state_code"] == "LQ")
    assert row["link_note"].startswith("website belongs to")
