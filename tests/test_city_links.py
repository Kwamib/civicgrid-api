"""Fix-a-city: edit a city's links, and fix a leader's spelling in place.

Oct 11, 2026: Kansas City, MO showed Kansas City, KANSAS's website
(wycokck.org) and "Quiton Lucas"; Fix-a-city could change neither safely.
Uses the review-integration fixtures (LOCAL Postgres only).
"""

import pytest

from tests.test_review_integration import AUTH, DB_URL, _sql, client  # noqa: F401

pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture
def kc(client):  # noqa: F811
    cid = _sql(
        "insert into cities (city, state_code, state_name, population, url, mayor_url, "
        "fetch_status, page_hash, last_check_outcome, last_check_at) "
        "values ('Kansas City', 'MO', 'Missouri', 508394, 'https://wycokck.org', null, "
        "'no_mayor_pg', 'oldhash', 'notfound', now()) returning id"
    )[0]["id"]
    lid = _sql(
        "insert into leaders (city_id, full_name, last_name, is_current, role) "
        "values (%s, 'Quiton Lucas', 'Lucas', true, 'chief_executive') returning id",
        (cid,),
    )[0]["id"]
    return cid, lid


def _links(client, cid, **body):  # noqa: F811
    return client.patch(f"/admin/cities/{cid}/links", headers=AUTH, json=body)


def test_fix_website_and_leadership_page(client, kc):  # noqa: F811
    cid, _ = kc
    page = "https://www.kcmo.gov/city-hall/city-officials/mayor-quinton-lucas"
    r = _links(client, cid, url="www.kcmo.gov", mayor_url=page)
    assert r.status_code == 200, r.text
    assert r.json()["before"]["url"] == "https://wycokck.org"
    row = _sql(
        "select url, mayor_url, fetch_status, page_hash, last_check_outcome, last_check_at "
        "from cities where id = %s",
        (cid,),
    )[0]
    assert row == {
        "url": "https://www.kcmo.gov",
        "mayor_url": page,
        "fetch_status": "ok",
        "page_hash": None,
        "last_check_outcome": None,
        "last_check_at": None,
    }, "new page is readable and will be checked fresh"


def test_website_only_leaves_page_state_alone(client, kc):  # noqa: F811
    cid, _ = kc
    assert _links(client, cid, url="https://www.kcmo.gov").status_code == 200
    row = _sql("select fetch_status, page_hash from cities where id = %s", (cid,))[0]
    assert row == {"fetch_status": "no_mayor_pg", "page_hash": "oldhash"}


@pytest.mark.parametrize(
    "bad",
    ["https://www.kansascity.com/mayor", "https://kcmo.example.org", "not a url"],
)
def test_non_government_address_needs_confirmation(client, kc, bad):  # noqa: F811
    cid, _ = kc
    r = _links(client, cid, url=bad)
    assert r.status_code == 422, r.text
    assert _sql("select url from cities where id = %s", (cid,))[0]["url"] == "https://wycokck.org"


def test_state_us_domain_and_confirmed_official_are_allowed(client, kc):  # noqa: F811
    cid, _ = kc
    assert _links(client, cid, url="https://www.kansascity.mo.us").status_code == 200
    assert _links(client, cid, url="https://www.kansascity.ks.us").status_code == 422, (
        "another state's .us domain is not this city's"
    )
    assert _links(client, cid, url="https://dsm.city", confirm_official=True).status_code == 200


def test_links_need_a_field_and_a_real_city(client, kc):  # noqa: F811
    cid, _ = kc
    assert _links(client, cid).status_code == 422
    assert _links(client, 999999, url="https://x.gov").status_code == 404
    assert client.patch(
        f"/admin/cities/{cid}/links", json={"url": "https://x.gov"}
    ).status_code in (
        401,
        403,
    )


def _leaders(cid):
    return _sql(
        "select id, full_name, last_name, is_current, last_verified_at is not null as verified "
        "from leaders where city_id = %s and role = 'chief_executive' order by id",
        (cid,),
    )


def test_same_person_flag_fixes_spelling_in_place(client, kc):  # noqa: F811
    cid, lid = kc
    r = client.post(
        f"/admin/cities/{cid}/leaders",
        headers=AUTH,
        json={
            "full_name": "Quinton Lucas",
            "leader_title": "Mayor",
            "source": "https://www.kcmo.gov/city-hall/city-officials/mayor-quinton-lucas",
            "same_person": True,
            # kcmo.gov doesn't contain "Kansas City" and the stored website is
            # wrong (wycokck.org), so the reviewer confirms the source.
            "confirm_source": True,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["previous_current"] == []
    assert _leaders(cid) == [
        {
            "id": lid,
            "full_name": "Quinton Lucas",
            "last_name": "Lucas",
            "is_current": True,
            "verified": True,
        }
    ], "one row, same id, new spelling, verified"
    manual = _sql(
        "select count(*) as n from verification_results where city_id = %s and verdict = 'MANUAL'",
        (cid,),
    )[0]["n"]
    assert manual == 1


def test_without_the_flag_a_different_name_still_rotates(client, kc):  # noqa: F811
    cid, _ = kc
    r = client.post(
        f"/admin/cities/{cid}/leaders",
        headers=AUTH,
        json={"full_name": "Quinton Lucas", "leader_title": "Mayor"},
    )
    assert r.status_code == 201, r.text
    assert [(x["full_name"], x["is_current"]) for x in _leaders(cid)] == [
        ("Quiton Lucas", False),
        ("Quinton Lucas", True),
    ]


def test_same_person_flag_needs_a_current_leader(client):  # noqa: F811
    cid = _sql(
        "insert into cities (city, state_code, state_name, population) "
        "values ('Nobodyville', 'MO', 'Missouri', 100) returning id"
    )[0]["id"]
    r = client.post(
        f"/admin/cities/{cid}/leaders",
        headers=AUTH,
        json={"full_name": "Pat Lee", "same_person": True},
    )
    assert r.status_code == 409, r.text


def test_off_site_source_is_refused_without_confirmation(client, kc):  # noqa: F811
    """Oct 15, 2026: Thomasville GA was 'verified' from rose.net."""
    cid, lid = kc
    body = {"full_name": "Quinton Lucas", "leader_title": "Mayor", "source": "https://www.rose.net"}
    r = client.post(f"/admin/cities/{cid}/leaders", headers=AUTH, json=body)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "source_not_official"
    assert _leaders(cid)[0]["full_name"] == "Quiton Lucas", "nothing saved"
    n = _sql("select count(*) as n from verification_results where city_id = %s", (cid,))[0]["n"]
    assert n == 0, "no verification recorded"


def test_confirmed_source_saves_and_administrator_is_not_checked(client, kc):  # noqa: F811
    cid, _ = kc
    r = client.post(
        f"/admin/cities/{cid}/leaders",
        headers=AUTH,
        json={
            "full_name": "Quinton Lucas",
            "leader_title": "Mayor",
            "same_person": True,
            "source": "https://www.kcmo.gov/mayor",
            "confirm_source": True,
        },
    )
    assert r.status_code == 201, r.text
    r = client.post(
        f"/admin/cities/{cid}/leaders",
        headers=AUTH,
        json={
            "full_name": "Brian Platt",
            "leader_title": "City Manager",
            "role": "chief_administrator",
            "source": "https://www.kcmo.gov/manager",
        },
    )
    assert r.status_code == 201, "administrator saves record no verification, so no check"
