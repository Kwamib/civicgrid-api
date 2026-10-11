"""Migration 009: the civicgrid_api login can do what the API needs, and no more.

Runs against the same LOCAL Postgres as the other integration tests (the guard in
test_review_integration refuses anything else). The test connects as the
superuser and uses SET ROLE civicgrid_api, so no password is needed, and both
the GRANTs and the row-level security policies are exercised as that role.
"""

import psycopg2
import pytest

from tests.test_review_integration import BASE_SCHEMA, DB_URL, MIGRATIONS, _sql

pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")


@pytest.fixture(scope="module")
def schema():
    _sql("drop schema public cascade; create schema public;")
    _sql(BASE_SCHEMA)
    for path in MIGRATIONS:
        _sql(path.read_text())
    # RLS is on for every table in production (done outside the migrations).
    # Turn it on here too, so the tests prove the policies let the role through.
    _sql(
        "do $$ declare t text; begin "
        "for t in select tablename from pg_tables where schemaname = 'public' loop "
        "execute format('alter table %I enable row level security', t); "
        "end loop; end $$;"
    )
    city_id = _sql(
        "insert into cities (city, state_code, state_name, population) "
        "values ('Roleville', 'OH', 'Ohio', 1000) returning id"
    )[0]["id"]
    _sql(
        "insert into leaders (city_id, full_name, last_name, is_current) "
        "values (%s, 'Pat Doe', 'Doe', true)",
        (city_id,),
    )
    return city_id


def _as_api(statement, params=None, fetch=False):
    """Run one statement as civicgrid_api inside a transaction that is rolled back."""
    conn = psycopg2.connect(DB_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("set role civicgrid_api")
            cur.execute(statement, params)
            return cur.fetchall() if fetch else cur.rowcount
    finally:
        conn.rollback()
        conn.close()


def test_role_exists_and_cannot_bypass_rls(schema):
    row = _sql(
        "select rolcanlogin, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb "
        "from pg_roles where rolname = 'civicgrid_api'"
    )[0]
    assert row["rolcanlogin"] is True
    assert not any([row["rolsuper"], row["rolbypassrls"], row["rolcreaterole"], row["rolcreatedb"]])


def test_reads_what_the_api_reads(schema):
    rows = _as_api(
        "select c.city, l.full_name from cities c "
        "join leaders l on l.city_id = c.id and l.is_current where c.id = %s",
        (schema,),
        fetch=True,
    )
    assert rows == [("Roleville", "Pat Doe")], "RLS policy or SELECT grant missing"


def test_writes_what_the_api_writes(schema):
    assert _as_api(
        "insert into verification_results (city_id, verdict, web_mayor, source, model) "
        "values (%s, 'MANUAL', 'Pat Doe', 'https://example.gov', 'human')",
        (schema,),
    ) == 1
    assert _as_api(
        "update leaders set leader_title = 'Mayor' where city_id = %s", (schema,)
    ) == 1
    assert _as_api(
        "insert into api_keys (key_hash, key_prefix, user_email, tier, label) "
        "values ('x', 'cg_live_test0000', 'a@example.com', 'free', 'role test')"
    ) == 1


@pytest.mark.parametrize(
    "statement",
    [
        "drop table cities",
        "alter table leaders add column hacked text",
        "create table evil (id int)",
        "truncate leaders",
        "delete from leaders",
        "delete from cities",
        "delete from api_keys",
        "select * from runs",
    ],
)
def test_cannot_do_anything_else(schema, statement):
    with pytest.raises((psycopg2.errors.InsufficientPrivilege, psycopg2.errors.UndefinedTable)):
        _as_api(statement)
