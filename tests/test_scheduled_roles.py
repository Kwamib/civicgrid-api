"""Migration 010: logins for the scheduled jobs on Anansi.

civicgrid_verifier may only do what verify_mayors_v7.py does; civicgrid_readonly
(QC and backups) may read everything and change nothing. Same approach as
test_api_role: LOCAL Postgres only, SET ROLE from the superuser, every statement
rolled back.
"""

import psycopg2
import pytest

from tests.test_review_integration import BASE_SCHEMA, DB_URL, MIGRATIONS, _sql

pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")

DENIED = (psycopg2.errors.InsufficientPrivilege, psycopg2.errors.UndefinedTable)


@pytest.fixture(scope="module")
def ids():
    _sql("drop schema public cascade; create schema public;")
    _sql(BASE_SCHEMA)
    for path in MIGRATIONS:
        _sql(path.read_text())
    _sql(
        "do $$ declare t text; begin "
        "for t in select tablename from pg_tables where schemaname = 'public' loop "
        "execute format('alter table %I enable row level security', t); "
        "end loop; end $$;"
    )
    # A table created after the migration, like the leaders_backup_* tables.
    _sql("create table leaders_backup_test as select * from leaders")
    _sql("alter table leaders_backup_test enable row level security")
    city = _sql(
        "insert into cities (city, state_code, state_name, population, mayor_url) "
        "values ('Jobtown', 'WI', 'Wisconsin', 5000, 'https://jobtown.gov/mayor') "
        "returning id"
    )[0]["id"]
    leader = _sql(
        "insert into leaders (city_id, full_name, last_name, is_current) "
        "values (%s, 'Ann Roe', 'Roe', true) returning id",
        (city,),
    )[0]["id"]
    run = _sql("insert into runs (kind) values ('weekly_verify') returning id")[0]["id"]
    return {"city": city, "leader": leader, "run": run}


def _as(role, statement, params=None, fetch=False):
    conn = psycopg2.connect(DB_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(f"set role {role}")
            cur.execute(statement, params)
            return cur.fetchall() if fetch else cur.rowcount
    finally:
        conn.rollback()
        conn.close()


def test_role_attributes(ids):
    rows = {
        r["rolname"]: r
        for r in _sql(
            "select rolname, rolcanlogin, rolsuper, rolbypassrls, rolcreaterole "
            "from pg_roles where rolname in ('civicgrid_verifier', 'civicgrid_readonly')"
        )
    }
    assert rows["civicgrid_verifier"]["rolcanlogin"] and not rows["civicgrid_verifier"]["rolbypassrls"]
    assert rows["civicgrid_readonly"]["rolcanlogin"] and rows["civicgrid_readonly"]["rolbypassrls"]
    assert not any(r["rolsuper"] or r["rolcreaterole"] for r in rows.values())


# ---------------------------------------------------------------- verifier

def test_verifier_does_its_weekly_job(ids):
    v = "civicgrid_verifier"
    assert _as(v, "select c.city, l.full_name from cities c join leaders l on l.city_id = c.id "
                  "where c.id = %s", (ids["city"],), fetch=True) == [("Jobtown", "Ann Roe")]
    assert _as(v, "update cities set page_hash = 'h', last_fetch_at = now() where id = %s",
               (ids["city"],)) == 1
    assert _as(v, "update leaders set last_verified_at = now() where id = %s", (ids["leader"],)) == 1
    assert _as(v, "insert into verification_results (city_id, verdict, db_mayor, web_mayor, "
                  "source, model, run_id) values (%s, 'MATCH', 'Ann Roe', 'Ann Roe', "
                  "'https://jobtown.gov/mayor', 'qwen2.5:7b', '1')", (ids["city"],)) == 1
    assert _as(v, "insert into leader_proposals (city_id, city, state_code, population, "
                  "db_mayor, web_mayor, source_url, confidence, status, created_at, run_id) "
                  "values (%s, 'Jobtown', 'WI', 5000, 'Ann Roe', 'Bo Lee', "
                  "'https://jobtown.gov/mayor', 'high', 'pending', now(), %s)",
               (ids["city"], ids["run"])) == 1
    assert _as(v, "select 1 from leader_proposals where city_id = %s and status = 'rejected'",
               (ids["city"],), fetch=True) == []
    assert _as(v, "insert into runs (kind, notes) values ('weekly_verify', 'test') returning id",
               fetch=True)
    assert _as(v, "update runs set finished_at = now(), status = 'ok', cities_checked = 1, "
                  "proposals_created = 0, breaker_tripped = false where id = %s",
               (ids["run"],)) == 1


@pytest.mark.parametrize("statement", [
    "update cities set city = 'Renamed'",
    "update cities set mayor_url = 'https://evil.example'",
    "update leaders set full_name = 'Someone Else'",
    "update leaders set is_current = false",
    "delete from leaders",
    "delete from leader_proposals",
    "update leader_proposals set status = 'approved'",
    "insert into leaders (city_id, full_name, last_name) values (1, 'X Y', 'Y')",
    "select * from api_keys",
    "drop table runs",
])
def test_verifier_cannot_do_anything_else(ids, statement):
    with pytest.raises(DENIED):
        _as("civicgrid_verifier", statement)


# ---------------------------------------------------------------- readonly

def test_readonly_reads_everything_including_later_tables(ids):
    r = "civicgrid_readonly"
    for table in ("cities", "leaders", "leader_proposals", "runs", "api_keys",
                  "verification_results", "leaders_backup_test"):
        assert _as(r, f"select count(*) from {table}", fetch=True)[0][0] >= 0, table


@pytest.mark.parametrize("statement", [
    "insert into runs (kind) values ('x')",
    "update leaders set full_name = 'X'",
    "delete from cities",
    "drop table leaders_backup_test",
    "create table evil (id int)",
])
def test_readonly_cannot_change_anything(ids, statement):
    with pytest.raises(DENIED):
        _as("civicgrid_readonly", statement)
