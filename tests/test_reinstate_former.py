"""A former leader who becomes current again keeps ONE row.

Simi Valley CA, Oct 13 2026: Dee Dee Cavanaugh was wrongly marked former in
July; restoring her in Fix-a-city inserted a second Cavanaugh row and QC
failed on "same person recorded more than once for a city and role".

Uses the review-integration fixtures (LOCAL Postgres only).
"""

import pytest

from tests.test_review_integration import AUTH, DB_URL, _sql, city, client  # noqa: F401

pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")


def _leaders(cid):
    return _sql(
        "select id, full_name, is_current from leaders "
        "where city_id = %s and role = 'chief_executive' order by id",
        (cid,),
    )


def _fix_a_city(client, cid, name):  # noqa: F811
    r = client.post(f"/admin/cities/{cid}/leaders", headers=AUTH,
                    json={"full_name": name, "leader_title": "Mayor"})
    assert r.status_code == 201, r.text
    return r.json()


def test_fix_a_city_restores_a_former_leader_in_place(client, city):  # noqa: F811
    cid, lid, _ = city(current="Dee Dee Cavanaugh", title="Mayor")
    _fix_a_city(client, cid, "Fred Thomas")           # the bad July change
    body = _fix_a_city(client, cid, "Dee Dee Cavanaugh")  # the October fix
    assert body["new_current"]["id"] == lid, "her original row comes back"
    assert [(x["full_name"], x["is_current"]) for x in _leaders(cid)] == [
        ("Dee Dee Cavanaugh", True),
        ("Fred Thomas", False),
    ]


def test_fix_a_city_new_person_still_gets_a_new_row(client, city):  # noqa: F811
    cid, _, _ = city(current="Dee Dee Cavanaugh", title="Mayor")
    _fix_a_city(client, cid, "Jesse Zwick")
    assert [(x["full_name"], x["is_current"]) for x in _leaders(cid)] == [
        ("Dee Dee Cavanaugh", False),
        ("Jesse Zwick", True),
    ]


def test_approving_a_former_leader_restores_their_row(client, city):  # noqa: F811
    cid, lid, pid = city(current="Fred Thomas", title="Mayor", web="Dee Dee Cavanaugh")
    old = _sql(
        "insert into leaders (city_id, full_name, last_name, leader_title, is_current) "
        "values (%s, 'Dee Dee Cavanaugh', 'Cavanaugh', 'Mayor', false) returning id",
        (cid,),
    )[0]["id"]
    r = client.post(f"/admin/proposals/{pid}/approve", headers=AUTH,
                    json={"actor": "me@example.com", "expected_leader_id": lid})
    assert r.status_code == 200, r.text
    rows = _leaders(cid)
    assert [(x["id"], x["is_current"]) for x in rows if x["full_name"] == "Dee Dee Cavanaugh"] \
        == [(old, True)], "one Cavanaugh row, current again"
    assert sum(1 for x in rows if x["is_current"]) == 1
