"""Migration 011: untraceable pre-Sep-17 checks leave the public profile.

LOCAL Postgres only (same fixtures as the review-integration tests).
"""

from pathlib import Path

import pytest

from tests.test_review_integration import DB_URL, _sql, city, client  # noqa: F401

pytestmark = pytest.mark.skipif(not DB_URL, reason="TEST_DATABASE_URL not set")

MIGRATION = (
    Path(__file__).parent.parent / "migrations" / "011_archive_untrusted_verifications.sql"
).read_text()


def _check(cid, verdict, when):
    _sql(
        "insert into verification_results (city_id, verdict, model, source, verified_at) "
        "values (%s, %s, 'm', 'https://x.gov', %s)",
        (cid, verdict, when),
    )


def _verdicts(table, cid):
    return [
        r["verdict"]
        for r in _sql(
            f"select verdict from {table} where city_id = %s order by verified_at", (cid,)
        )
    ]


def test_archives_old_automated_checks_only(client, city):  # noqa: F811
    cid, _, _ = city()
    _sql("delete from verification_results where city_id = %s", (cid,))
    _check(cid, "MATCH", "2026-07-05")
    _check(cid, "MANUAL", "2026-08-02")
    _check(cid, "UNSURE", "2026-09-16 23:59")
    _check(cid, "MATCH", "2026-10-09")

    _sql(MIGRATION)

    assert _verdicts("verification_results", cid) == ["MANUAL", "MATCH"]
    assert _verdicts("verification_results_archive", cid) == ["MATCH", "UNSURE"]

    _sql(MIGRATION)  # second run moves nothing and duplicates nothing
    assert _verdicts("verification_results", cid) == ["MANUAL", "MATCH"]
    assert _verdicts("verification_results_archive", cid) == ["MATCH", "UNSURE"]


def test_public_profile_no_longer_shows_old_check(client, city):  # noqa: F811
    cid, _, _ = city()
    _sql("delete from verification_results where city_id = %s", (cid,))
    _check(cid, "MATCH", "2026-07-05")
    _sql(MIGRATION)
    assert (
        _sql("select count(*) as n from latest_verification where city_id = %s", (cid,))[0]["n"]
        == 0
    )


def test_archive_is_closed_to_supabase_api_roles():
    _sql(MIGRATION)
    rls = _sql("select relrowsecurity from pg_class where relname = 'verification_results_archive'")
    assert rls == [{"relrowsecurity": True}]
