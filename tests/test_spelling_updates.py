"""Same person, different spelling: update in place, never a fake new mayor.

Reproduces the Oct 9-10, 2026 bug: approving "Beto Lopez" -> "J. Beto Lopez"
(Lee's Summit MO) and "Don DeGraff" -> "Don A. De Graff" (South Holland IL)
demoted the mayor and inserted the same person again as a new mayor.

Uses the review-integration fixtures (LOCAL Postgres only).
"""

import pytest

from app.names import same_person_spelling
from tests.test_review_integration import AUTH, DB_URL, _sql, city, client  # noqa: F401

pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")

REVIEWER = "me@example.com"


def _leaders(cid):
    return _sql(
        "select full_name, last_name, is_current from leaders "
        "where city_id = %s and role = 'chief_executive' order by id",
        (cid,),
    )


@pytest.mark.parametrize(
    "a, b, same",
    [
        ("Don DeGraff", "Don A. De Graff", True),
        ("Beto Lopez", "J. Beto Lopez", True),
        ("Brian A. DePeña", "Brian A. DePena", True),
        ("Mayor Don McDaniel", "Don McDaniel", True),
        ("Jim Smith", "James Smith", False),
        ("John A. Smith", "John B. Smith", False),
        ("Frank Scott", "Frank Scott Jr.", False),
        ("Jessica Ancona", "Julia Ruedas", False),
        ("", "Smith", False),
    ],
)
def test_same_person_spelling(a, b, same):
    assert same_person_spelling(a, b) is same
    assert same_person_spelling(b, a) is same


def test_approving_a_spelling_variant_updates_in_place(client, city):  # noqa: F811
    cid, lid, pid = city(current="Beto Lopez", title="Mayor", web="J. Beto Lopez")
    r = client.post(
        f"/admin/proposals/{pid}/approve",
        headers=AUTH,
        json={"actor": REVIEWER, "expected_leader_id": lid},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["spelling_update"] is True
    assert body["demoted"] == []
    assert body["new_leader"]["id"] == lid
    assert _leaders(cid) == [
        {"full_name": "J. Beto Lopez", "last_name": "Lopez", "is_current": True}
    ], "same person must stay one row, with the new spelling"


def test_approving_a_different_person_still_rotates(client, city):  # noqa: F811
    cid, lid, pid = city(current="Jim Smith", title="Mayor", web="James Smith")
    r = client.post(
        f"/admin/proposals/{pid}/approve",
        headers=AUTH,
        json={"actor": REVIEWER, "expected_leader_id": lid},
    )
    assert r.status_code == 200, r.text
    assert r.json()["spelling_update"] is False
    rows = _leaders(cid)
    assert [(x["full_name"], x["is_current"]) for x in rows] == [
        ("Jim Smith", False),
        ("James Smith", True),
    ]


def test_correcting_to_a_spelling_variant_updates_in_place(client, city):  # noqa: F811
    cid, lid, pid = city(current="Don DeGraff", title="Mayor", web="Someone Else")
    r = client.post(
        f"/admin/proposals/{pid}/correct",
        headers=AUTH,
        json={
            "actor": REVIEWER,
            "expected_leader_id": lid,
            "full_name": "Don A. De Graff",
            "reason": "Official page spells it Don A. De Graff",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["demoted"] == []
    assert _leaders(cid) == [
        {"full_name": "Don A. De Graff", "last_name": "Graff", "is_current": True}
    ]


def test_fix_a_city_spelling_variant_updates_in_place(client, city):  # noqa: F811
    cid, lid, _ = city(current="Beto Lopez", title="Mayor")
    r = client.post(
        f"/admin/cities/{cid}/leaders",
        headers=AUTH,
        json={"full_name": "J. Beto Lopez", "leader_title": "Mayor"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["previous_current"] == []
    assert [x["full_name"] for x in _leaders(cid)] == ["J. Beto Lopez"]
