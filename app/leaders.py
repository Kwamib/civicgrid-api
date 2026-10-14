"""Bring a former leader back instead of recording them a second time.

Simi Valley CA, Oct 13 2026: an old source made Fred Thomas mayor again in
July (he died in office in January 2025) and marked Dee Dee Cavanaugh as
former. Restoring her in Fix-a-city inserted a second Cavanaugh row, so her
history showed two separate terms that never happened and QC failed on
"same person recorded more than once".

When the incoming leader is already in this city's history for this role,
that row becomes current again: one person, one row.
"""

from app.names import same_person_spelling


def former_leader_id(cur, city_id: int, role: str, full_name: str) -> int | None:
    """The most recent former row for this city and role naming the same person."""
    cur.execute(
        "select id, full_name from leaders "
        "where city_id = %s and role = %s and is_current = false "
        "order by updated_at desc nulls last, id desc",
        (city_id, role),
    )
    for row in cur.fetchall():
        if same_person_spelling(row["full_name"], full_name):
            return row["id"]
    return None


def reinstate(
    cur,
    leader_id: int,
    *,
    full_name: str,
    last_name: str,
    leader_title: str | None,
    returning: str,
    political_party=None,
    year_elected=None,
    next_election_year=None,
    tenure_years=None,
    term_length_years=None,
) -> dict:
    """Make a former row current again, verified now. Call after demoting the incumbent."""
    cur.execute(
        f"""
        update leaders set
            is_current = true,
            ended_at = null,
            full_name = %s,
            last_name = %s,
            leader_title = coalesce(%s, leader_title),
            political_party = coalesce(%s, political_party),
            year_elected = coalesce(%s, year_elected),
            next_election_year = coalesce(%s, next_election_year),
            tenure_years = coalesce(%s, tenure_years),
            term_length_years = coalesce(%s, term_length_years),
            updated_at = now(),
            last_verified_at = now()
        where id = %s
        returning {returning}
        """,
        (
            full_name,
            last_name,
            leader_title,
            political_party,
            year_elected,
            next_election_year,
            tenure_years,
            term_length_years,
            leader_id,
        ),
    )
    return dict(cur.fetchone())
