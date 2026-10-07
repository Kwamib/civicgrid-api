"""Integration tests for the review workflow against a REAL Postgres.

Skipped unless TEST_DATABASE_URL points at a disposable database. The test
DROPS AND RECREATES the public schema there, so never point it at production.

    createdb civicgrid_test
    TEST_DATABASE_URL=postgresql://postgres@localhost:5432/civicgrid_test pytest tests/

The base tables (cities, leaders, provenance) predate the migrations folder,
so a minimal version of them is created here; migrations 001-007 then run on
top of it exactly as they would in production.
"""

from __future__ import annotations

import os
from pathlib import Path

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

DB_URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")

ADMIN = "test-admin-token"
AUTH = {"Authorization": f"Bearer {ADMIN}"}
MIGRATIONS = sorted((Path(__file__).parent.parent / "migrations").glob("*.sql"))

BASE_SCHEMA = """
create table cities (
    id bigint generated always as identity primary key,
    city text not null, state_code text not null, state_name text,
    county text, metro_area text, city_type text, population integer,
    median_household_income integer, median_age numeric, land_area_sq_mi numeric,
    population_density numeric, city_budget_text text, city_budget_numeric numeric,
    city_hall_phone text, url text
);
create table leaders (
    id bigint generated always as identity primary key,
    city_id bigint not null references cities(id),
    full_name text not null, last_name text not null,
    leader_title text, political_party text,
    year_elected integer, next_election_year integer,
    tenure_years integer, term_length_years integer,
    is_current boolean not null default false,
    created_at timestamptz default now(), updated_at timestamptz default now()
);
create table provenance (
    city_id bigint references cities(id), data_source text,
    last_verified_date date, created_at timestamptz default now()
);
"""


def _sql(statement: str, params: tuple = ()) -> list[dict]:
    conn = psycopg2.connect(DB_URL)
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(statement, params or None)
            rows = cur.fetchall() if cur.description else []
        conn.commit()
        return rows
    finally:
        conn.close()


@pytest.fixture(scope="module")
def client():
    _sql("drop schema public cascade; create schema public;")
    _sql(BASE_SCHEMA)
    for path in MIGRATIONS:
        _sql(path.read_text())
    # Migration 007 must be safe to run twice.
    _sql((Path(__file__).parent.parent / "migrations" / "007_review_workflow.sql").read_text())

    os.environ["DATABASE_URL"] = DB_URL
    os.environ["ADMIN_TOKEN"] = ADMIN
    os.environ.pop("REDIS_URL", None)
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def city(client):
    """A fresh city with a current Village President and one pending finding."""

    def make(current="Doug Diny", title="Village President", web="Karen Dallman",
             db_mayor=None, with_leader_id=True, proposed_title=None):  # fmt: skip
        cid = _sql(
            "insert into cities (city, state_code, state_name, population, url) "
            "values ('Testville', 'WI', 'Wisconsin', 12000, 'https://testville.gov') "
            "returning id"
        )[0]["id"]
        lid = None
        if current:
            lid = _sql(
                "insert into leaders (city_id, full_name, last_name, leader_title, is_current) "
                "values (%s, %s, %s, %s, true) returning id",
                (cid, current, current.split()[-1], title),
            )[0]["id"]
        pid = _sql(
            "insert into leader_proposals (city_id, city, state_code, population, db_mayor, "
            "web_mayor, source_url, confidence, db_leader_id, proposed_title, evidence) "
            "values (%s, 'Testville', 'WI', 12000, %s, %s, 'https://testville.gov/mayor', "
            "'high', %s, %s, 'Karen Dallman serves as village president.') returning id",
            (cid, db_mayor if db_mayor is not None else current, web,
             lid if with_leader_id else None, proposed_title),
        )[0]["id"]  # fmt: skip
        return cid, lid, pid

    return make


def _current(cid):
    return _sql(
        "select id, full_name, leader_title, last_verified_at from leaders "
        "where city_id = %s and is_current",
        (cid,),
    )


def _events(pid):
    return _sql("select * from review_events where proposal_id = %s order by id", (pid,))


def test_accept_publishes_keeps_title_and_audits(client, city):
    cid, lid, pid = city()
    r = client.post(
        f"/admin/proposals/{pid}/approve", headers=AUTH,
        json={"actor": "me@example.com", "reason": "Matches official page",
              "expected_leader_id": lid},
    )  # fmt: skip
    assert r.status_code == 200, r.text
    cur = _current(cid)
    assert len(cur) == 1
    assert cur[0]["full_name"] == "Karen Dallman"
    # Title is inherited from the current record, NOT hardcoded to 'Mayor'.
    assert cur[0]["leader_title"] == "Village President"
    assert cur[0]["last_verified_at"] is not None
    old = _sql("select is_current from leaders where id = %s", (lid,))
    assert old[0]["is_current"] is False  # demoted, not deleted

    prop = _sql("select * from leader_proposals where id = %s", (pid,))[0]
    assert prop["status"] == "approved"
    assert prop["reviewed_by"] == "me@example.com"
    assert prop["applied_name"] == "Karen Dallman"

    ev = _events(pid)
    assert [e["action"] for e in ev] == ["accept"]
    assert ev[0]["before_name"] == "Doug Diny" and ev[0]["after_name"] == "Karen Dallman"
    assert ev[0]["evidence"].startswith("Karen Dallman serves")

    ver = _sql("select verdict, model from verification_results where city_id = %s", (cid,))
    assert ver == [{"verdict": "MANUAL", "model": "human-review"}]
    assert _sql("select count(*) as n from webhook_events")[0]["n"] >= 1


def test_proposed_title_wins_over_current_title(client, city):
    cid, lid, pid = city(proposed_title="Mayor")
    r = client.post(
        f"/admin/proposals/{pid}/approve",
        headers=AUTH,
        json={"actor": "me@example.com", "expected_leader_id": lid},
    )
    assert r.status_code == 200, r.text
    assert _current(cid)[0]["leader_title"] == "Mayor"


def test_accept_twice_is_refused(client, city):
    cid, lid, pid = city()
    body = {"actor": "me@example.com", "expected_leader_id": lid}
    assert (
        client.post(f"/admin/proposals/{pid}/approve", headers=AUTH, json=body).status_code == 200
    )
    r = client.post(f"/admin/proposals/{pid}/approve", headers=AUTH, json=body)
    assert r.status_code == 409


def test_changed_record_blocks_accept_and_rolls_back(client, city):
    cid, lid, pid = city()
    r = client.post(
        f"/admin/proposals/{pid}/approve",
        headers=AUTH,
        json={"actor": "me@example.com", "expected_leader_id": lid + 999},
    )
    assert r.status_code == 409
    assert _current(cid)[0]["full_name"] == "Doug Diny"
    assert (
        _sql("select status from leader_proposals where id = %s", (pid,))[0]["status"] == "pending"
    )
    assert _events(pid) == []


def test_stale_legacy_finding_needs_acknowledgement(client, city):
    # Legacy row: no db_leader_id, and the DB was fixed after the verifier ran.
    cid, lid, pid = city(db_mayor="Tom Brady", with_leader_id=False)
    listing = client.get("/admin/proposals", headers=AUTH).json()
    row = next(p for p in listing["proposals"] if p["id"] == pid)
    assert row["stale"] is True and row["current_name"] == "Doug Diny"

    body = {"actor": "me@example.com", "expected_leader_id": lid}
    assert (
        client.post(f"/admin/proposals/{pid}/approve", headers=AUTH, json=body).status_code == 409
    )
    body["acknowledge_stale"] = True
    assert (
        client.post(f"/admin/proposals/{pid}/approve", headers=AUTH, json=body).status_code == 200
    )
    assert _current(cid)[0]["full_name"] == "Karen Dallman"


def test_correct_same_person_updates_in_place(client, city):
    cid, lid, pid = city(current="Gary L'Heureux", title="Mayor", web="Gary LHeureux")
    r = client.post(f"/admin/proposals/{pid}/correct", headers=AUTH, json={
        "actor": "me@example.com", "full_name": "Gary L'Heureux",
        "leader_title": "Village President", "reason": "Title is Village President",
        "expected_leader_id": lid,
    })  # fmt: skip
    assert r.status_code == 200, r.text
    cur = _current(cid)
    assert cur[0]["id"] == lid  # same row, no fake transition
    assert cur[0]["leader_title"] == "Village President"
    assert cur[0]["last_verified_at"] is not None
    assert _sql("select count(*) as n from leaders where city_id = %s", (cid,))[0]["n"] == 1
    assert (
        _sql("select status from leader_proposals where id = %s", (pid,))[0]["status"]
        == "corrected"
    )


def test_correct_requires_reason_and_rotates(client, city):
    cid, lid, pid = city(web="Sarah Brohawn")
    no_reason = {"actor": "me@example.com", "full_name": "Ralph Dance", "expected_leader_id": lid}
    assert (
        client.post(f"/admin/proposals/{pid}/correct", headers=AUTH, json=no_reason).status_code
        == 422
    )
    body = {**no_reason, "reason": "Verifier read a fabricated article; city site says Ralph Dance",
            "source_url": "https://testville.gov/council"}  # fmt: skip
    r = client.post(f"/admin/proposals/{pid}/correct", headers=AUTH, json=body)
    assert r.status_code == 200, r.text
    assert _current(cid)[0]["full_name"] == "Ralph Dance"
    ev = _events(pid)[0]
    assert ev["action"] == "correct" and ev["source_url"] == "https://testville.gov/council"


def test_dismiss_with_confirm_stamps_current_record(client, city):
    cid, lid, pid = city(web="Sarah Brohawn")
    r = client.post(f"/admin/proposals/{pid}/reject", headers=AUTH, json={
        "actor": "me@example.com", "reason": "nationaltoday.com is fabricated",
        "confirm_current": True, "expected_leader_id": lid,
    })  # fmt: skip
    assert r.status_code == 200, r.text
    cur = _current(cid)
    assert cur[0]["id"] == lid and cur[0]["full_name"] == "Doug Diny"
    assert cur[0]["last_verified_at"] is not None
    assert _events(pid)[0]["confirmed_current"] is True


def test_dismiss_without_confirm_changes_nothing(client, city):
    cid, lid, pid = city()
    r = client.post(
        f"/admin/proposals/{pid}/reject", headers=AUTH, json={"actor": "me@example.com"}
    )
    assert r.status_code == 200
    assert _current(cid)[0]["last_verified_at"] is None


def test_retry_changes_nothing_then_can_be_reviewed(client, city):
    cid, lid, pid = city()
    r = client.post(
        f"/admin/proposals/{pid}/retry",
        headers=AUTH,
        json={"actor": "me@example.com", "reason": "Page looked mid-update"},
    )
    assert r.status_code == 200
    assert _current(cid)[0]["full_name"] == "Doug Diny"
    assert _current(cid)[0]["last_verified_at"] is None
    prop = _sql("select status, retry_requested_at from leader_proposals where id = %s", (pid,))[0]
    assert prop["status"] == "retry" and prop["retry_requested_at"] is not None
    retry_list = client.get("/admin/proposals?status=retry", headers=AUTH).json()
    assert any(p["id"] == pid for p in retry_list["proposals"])


def test_legacy_bodyless_calls_still_work(client, city):
    cid, lid, pid = city()
    assert client.post(f"/admin/proposals/{pid}/approve", headers=AUTH).status_code == 200
    assert _events(pid)[0]["actor"] == "admin-token"
    cid2, lid2, pid2 = city()
    assert client.post(f"/admin/proposals/{pid2}/reject", headers=AUTH).status_code == 200


def test_review_events_are_append_only(client, city):
    cid, lid, pid = city()
    client.post(f"/admin/proposals/{pid}/retry", headers=AUTH, json={"actor": "me@example.com"})
    with pytest.raises(psycopg2.Error):
        _sql("update review_events set actor = 'someone-else' where proposal_id = %s", (pid,))
    with pytest.raises(psycopg2.Error):
        _sql("delete from review_events where proposal_id = %s", (pid,))


def test_admin_routes_require_token(client, city):
    cid, lid, pid = city()
    assert client.get("/admin/proposals").status_code == 401
    r = client.post(
        f"/admin/proposals/{pid}/approve",
        headers={"Authorization": "Bearer wrong"},
        json={"actor": "x@y.z"},
    )
    assert r.status_code == 403
    assert _current(cid)[0]["full_name"] == "Doug Diny"


def test_public_rows_expose_verification_but_not_findings(client, city):
    cid, lid, pid = city()
    client.post(
        f"/admin/proposals/{pid}/approve",
        headers=AUTH,
        json={"actor": "me@example.com", "expected_leader_id": lid},
    )
    # A later failed check attempt must not erase "last verified".
    _sql(
        "insert into verification_results (city_id, verdict, model, verified_at) "
        "values (%s, 'ERROR', 'qwen', now() + interval '1 minute')",
        (cid,),
    )

    key = client.post("/admin/keys", headers=AUTH, json={"user_email": "dev@example.com"}).json()[
        "key"
    ]
    api = {"Authorization": f"Bearer {key}"}
    rows = client.get("/cities/all", headers=api).json()["data"]
    row = next(r for r in rows if r["id"] == cid)
    assert row["leader_name"] == "Karen Dallman"
    assert row["leader_last_verified_at"] is not None
    assert row["last_verified_method"] == "human"
    assert row["verification_source_url"] == "https://testville.gov/mayor"
    assert row["last_check_result"] == "check_failed"

    listed = client.get("/cities?state=WI&limit=500", headers=api).json()["data"]
    assert any(r["id"] == cid and r["last_check_result"] == "check_failed" for r in listed)

    detail = client.get(f"/cities/{cid}", headers=api).json()
    names = [h["full_name"] for h in detail["leadership_history"]]
    assert names[0] == "Karen Dallman" and "Doug Diny" in names
    change = detail["published_changes"][0]
    assert change["after_name"] == "Karen Dallman"
    assert "actor" not in change and "reason" not in change


def test_unverified_city_has_null_verification_fields(client, city):
    cid, lid, pid = city()
    key = client.post("/admin/keys", headers=AUTH, json={"user_email": "dev2@example.com"}).json()[
        "key"
    ]
    rows = client.get("/cities/all", headers={"Authorization": f"Bearer {key}"}).json()["data"]
    row = next(r for r in rows if r["id"] == cid)
    assert row["leader_last_verified_at"] is None
    assert row["last_verified_method"] is None
    assert row["verification_source_url"] is None
    assert row["last_checked_at"] is None
