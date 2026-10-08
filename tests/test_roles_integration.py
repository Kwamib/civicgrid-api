"""Leader roles (migration 008): one current leader per city PER ROLE.

Runs against a LOCAL Postgres only (same TEST_DATABASE_URL guard and schema
bootstrap as test_review_integration.py). Covers:
  * the database allows one current chief_executive AND one chief_administrator,
    but never two current holders of the same role;
  * Fix-a-city with role=chief_administrator leaves the mayor alone;
  * public reads show the leader once, plus the administrator in separate fields;
  * the review queue only ever sees the chief executive.
"""

import os
import uuid

import psycopg2
import pytest

from tests.test_review_integration import BASE_SCHEMA, DB_URL, MIGRATIONS, _sql

pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")

ADMIN = {"Authorization": "Bearer test-admin-token-roles"}


def _one(query, params=None):
    conn = psycopg2.connect(DB_URL)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchone()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def client():
    _sql("drop schema public cascade; create schema public;")
    _sql(BASE_SCHEMA)
    for path in MIGRATIONS:
        _sql(path.read_text())

    os.environ["DATABASE_URL"] = DB_URL
    os.environ["ADMIN_TOKEN"] = ADMIN["Authorization"].split()[1]

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        key = c.post(
            "/admin/keys",
            headers=ADMIN,
            json={"user_email": "roles@example.com", "tier": "pro", "label": "roles-test"},
        )
        assert key.status_code == 200, key.text
        c.headers.update({"Authorization": f"Bearer {key.json()['key']}"})
        yield c


@pytest.fixture
def city_id(client):
    """A fresh city per test, so tests don't depend on each other."""
    name = f"Roletown {uuid.uuid4().hex[:6]}"
    row = _one(
        "insert into cities (city, state_code, state_name, population) "
        "values (%s, 'MA', 'Massachusetts', 20000) returning id",
        (name,),
    )
    return row[0]


def _rotate(client, city_id, name, role=None, title=None):
    body = {"full_name": name, "leader_title": title}
    if role:
        body["role"] = role
    return client.post(f"/admin/cities/{city_id}/leaders", headers=ADMIN, json=body)


def test_db_allows_one_current_per_role_not_two(city_id):
    _sql(
        "insert into leaders (city_id, full_name, last_name, role, is_current) values "
        f"({city_id}, 'Rick Smith', 'Smith', 'chief_executive', true), "
        f"({city_id}, 'Ted Langill', 'Langill', 'chief_administrator', true)"
    )
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _sql(
            "insert into leaders (city_id, full_name, last_name, role, is_current) values "
            f"({city_id}, 'Someone Else', 'Else', 'chief_administrator', true)"
        )


def test_role_check_rejects_unknown_roles(city_id):
    with pytest.raises(psycopg2.errors.CheckViolation):
        _sql(
            "insert into leaders (city_id, full_name, last_name, role) values "
            f"({city_id}, 'Jo Park', 'Park', 'city_council')"
        )


def test_default_role_is_chief_executive(client, city_id):
    r = _rotate(client, city_id, "Rick Smith", title="Select Board Chair")
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "chief_executive"
    assert r.json()["new_current"]["role"] == "chief_executive"


def test_administrator_does_not_demote_the_leader(client, city_id):
    assert _rotate(client, city_id, "Rick Smith", title="Select Board Chair").status_code == 201
    r = _rotate(client, city_id, "Ted Langill", role="chief_administrator",
                title="Town Administrator")
    assert r.status_code == 201, r.text
    assert r.json()["previous_current"] == []

    current = _one(
        "select count(*) filter (where role = 'chief_executive'), "
        "count(*) filter (where role = 'chief_administrator') "
        "from leaders where city_id = %s and is_current",
        (city_id,),
    )
    assert current == (1, 1)


def test_new_administrator_replaces_only_the_old_administrator(client, city_id):
    _rotate(client, city_id, "Rick Smith", title="Select Board Chair")
    _rotate(client, city_id, "Peter Morin", role="chief_administrator",
            title="Interim Town Administrator")
    r = _rotate(client, city_id, "Ted Langill", role="chief_administrator",
                title="Town Administrator")
    assert r.status_code == 201, r.text
    assert [d["full_name"] for d in r.json()["previous_current"]] == ["Peter Morin"]

    leader = _one(
        "select full_name from leaders where city_id = %s and is_current "
        "and role = 'chief_executive'",
        (city_id,),
    )
    assert leader == ("Rick Smith",)


def test_same_administrator_updates_in_place(client, city_id):
    _rotate(client, city_id, "Ted Langill", role="chief_administrator",
            title="Interim Town Administrator")
    r = _rotate(client, city_id, "Ted Langill", role="chief_administrator",
                title="Town Administrator")
    assert r.status_code == 201, r.text
    assert r.json()["previous_current"] == []
    rows = _one(
        "select count(*) from leaders where city_id = %s and role = 'chief_administrator'",
        (city_id,),
    )
    assert rows == (1,)


def test_administrator_is_not_recorded_as_a_verification(client, city_id):
    _rotate(client, city_id, "Ted Langill", role="chief_administrator",
            title="Town Administrator")
    n = _one("select count(*) from verification_results where city_id = %s", (city_id,))
    assert n == (0,)


def test_public_city_shows_leader_once_and_administrator_separately(client, city_id):
    _rotate(client, city_id, "Rick Smith", title="Select Board Chair")
    _rotate(client, city_id, "Ted Langill", role="chief_administrator",
            title="Town Administrator")

    city = client.get(f"/cities/{city_id}").json()["city"]
    assert city["leader_name"] == "Rick Smith"
    assert city["leader_title"] == "Select Board Chair"
    assert city["administrator_name"] == "Ted Langill"
    assert city["administrator_title"] == "Town Administrator"

    name = city["city"]
    rows = client.get("/cities", params={"search": name}).json()["data"]
    assert len(rows) == 1, "a city with an administrator must not appear twice"
    assert rows[0]["leader_name"] == "Rick Smith"
    assert rows[0]["administrator_name"] == "Ted Langill"


def test_city_without_administrator_has_null_fields(client, city_id):
    _rotate(client, city_id, "Jane Doe", title="Mayor")
    city = client.get(f"/cities/{city_id}").json()["city"]
    assert city["leader_name"] == "Jane Doe"
    assert city["administrator_name"] is None
    assert city["administrator_title"] is None


def test_history_labels_each_row_with_its_role(client, city_id):
    _rotate(client, city_id, "Rick Smith", title="Select Board Chair")
    _rotate(client, city_id, "Ted Langill", role="chief_administrator",
            title="Town Administrator")
    history = client.get(f"/cities/{city_id}").json()["leadership_history"]
    assert {(h["full_name"], h["role"]) for h in history} == {
        ("Rick Smith", "chief_executive"),
        ("Ted Langill", "chief_administrator"),
    }
    assert history[0]["role"] == "chief_executive"


def test_leaders_current_defaults_to_chief_executive(client, city_id):
    _rotate(client, city_id, "Rick Smith", title="Select Board Chair")
    _rotate(client, city_id, "Ted Langill", role="chief_administrator",
            title="Town Administrator")
    ours = lambda rows: [r["full_name"] for r in rows if r["city_id"] == city_id]  # noqa: E731

    execs = client.get("/leaders/current", params={"state": "MA", "limit": 500}).json()["data"]
    assert ours(execs) == ["Rick Smith"]

    admins = client.get(
        "/leaders/current",
        params={"state": "MA", "role": "chief_administrator", "limit": 500},
    ).json()["data"]
    assert ours(admins) == ["Ted Langill"]


def test_unknown_role_is_rejected(client, city_id):
    r = _rotate(client, city_id, "Jo Park", role="city_council")
    assert r.status_code == 422
