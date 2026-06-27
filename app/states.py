"""Per-state aggregation endpoints.

The global /stats endpoint (in main.py) returns nationwide rollups. This
module adds per-state aggregations: a list of all states with summary metrics,
and a single-state detail view.

Auth: API-key gated by AuthMiddleware automatically — paths are not in
PUBLIC_PATHS and not under /admin or /me, same as /stats and /cities.

Metrics note: median_city_population is the median CITY's population
(percentile_cont over cities), NOT a population-weighted median person.
Income/age are intentionally not aggregated here: averaging per-city medians
without population weighting is misleading, so it is deferred rather than
shipped wrong.
"""

from __future__ import annotations

from fastapi import HTTPException

# Per-state city aggregates. {where} is empty for the list endpoint or a
# "where c.state_code = %s" clause for the single-state detail endpoint.
_CITY_AGG_SQL = """
    select
        c.state_code,
        max(c.state_name) as state_name,
        count(*) as city_count,
        sum(c.population) as total_population,
        round(avg(c.population))::bigint as avg_population,
        round(
            percentile_cont(0.5) within group (order by c.population)
        )::bigint as median_city_population
    from cities c
    {where}
    group by c.state_code
    order by c.state_code
"""

# Current-leader party breakdown per state.
_PARTY_AGG_SQL = """
    select
        c.state_code,
        coalesce(l.political_party, 'Unknown') as party,
        count(*) as n
    from cities c
    join leaders l on l.city_id = c.id and l.is_current = true
    {where}
    group by c.state_code, coalesce(l.political_party, 'Unknown')
    order by c.state_code, n desc
"""


def _city_row_to_state(row: dict, parties: list[dict]) -> dict:
    """Shape a city-aggregate row plus its party breakdown into a state object."""
    return {
        "state_code": row["state_code"],
        "state_name": row["state_name"],
        "city_count": row["city_count"],
        "total_population": row["total_population"],
        "avg_population": row["avg_population"],
        "median_city_population": row["median_city_population"],
        "leaders_by_party": parties,
    }


def attach_state_routes(app, get_cursor):
    """Register per-state aggregation routes on the FastAPI app. Called from main.py."""

    @app.get("/stats/states", tags=["stats"])
    def list_state_aggregations():
        """One row per state with city counts, population aggregates, and the
        current-leader party breakdown. Ordered by state code."""
        with get_cursor() as cur:
            cur.execute(_CITY_AGG_SQL.format(where=""))
            city_rows = cur.fetchall()
            cur.execute(_PARTY_AGG_SQL.format(where=""))
            party_rows = cur.fetchall()

        # Index party breakdowns by state so each state gets its own list.
        parties_by_state: dict[str, list] = {}
        for r in party_rows:
            parties_by_state.setdefault(r["state_code"], []).append(
                {"party": r["party"], "count": r["n"]}
            )

        states = [
            _city_row_to_state(r, parties_by_state.get(r["state_code"], [])) for r in city_rows
        ]
        return {"state_count": len(states), "states": states}

    @app.get("/stats/states/{state_code}", tags=["stats"])
    def state_aggregation_detail(state_code: str):
        """Aggregation rollup for a single state. Case-insensitive state code;
        404 if the state has no cities."""
        code = state_code.upper()
        with get_cursor() as cur:
            cur.execute(
                _CITY_AGG_SQL.format(where="where c.state_code = %s"),
                (code,),
            )
            city = cur.fetchone()
            if not city:
                raise HTTPException(status_code=404, detail=f"No data for state '{code}'.")
            cur.execute(
                _PARTY_AGG_SQL.format(where="where c.state_code = %s"),
                (code,),
            )
            party_rows = cur.fetchall()

        parties = [{"party": r["party"], "count": r["n"]} for r in party_rows]
        return _city_row_to_state(city, parties)
