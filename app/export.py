"""Bulk data export endpoints.

Public dataset reads live in main.py (/cities, /cities/all, /leaders/current).
This module adds heavier full-history exports intended for API consumers
rather than the landing page.

Auth: routes here are API-key gated by AuthMiddleware automatically — the
path is not in PUBLIC_PATHS and not under /admin or /me, so every request
must carry a valid API key (same as /cities and /leaders/current).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Response


def attach_export_routes(app, get_cursor):
    """Register export routes on the FastAPI app. Called from main.py."""

    @app.get("/leaders/export", tags=["export"])
    def export_leaders(response: Response):
        """Full leader history export: every leader (current AND historical),
        grouped under their city.

        Unlike /cities/all (which returns only the current leader per city),
        this includes demoted/historical rows (is_current=false) so consumers
        can reconstruct the full timeline of who held office in each city. One
        DB row per leader; grouping into the nested shape happens server-side.

        Within each city, the current leader is listed first, then historical
        leaders in insertion order. Cities are ordered by population (desc) to
        match the other read endpoints.
        """
        sql = """
            select
                c.id as city_id, c.city, c.state_code, c.state_name, c.population,
                l.id as leader_id, l.full_name, l.last_name, l.leader_title,
                l.political_party, l.year_elected, l.next_election_year,
                l.tenure_years, l.term_length_years, l.is_current,
                l.created_at, l.updated_at
            from cities c
            join leaders l on l.city_id = c.id
            order by c.population desc nulls last, c.city asc,
                     l.is_current desc, l.id asc
        """
        with get_cursor() as cur:
            cur.execute(sql)
            rows = cur.fetchall()

        # Group leaders under their city, preserving the query's ordering.
        cities: list[dict] = []
        index: dict[int, dict] = {}
        for r in rows:
            cid = r["city_id"]
            city = index.get(cid)
            if city is None:
                city = {
                    "city_id": cid,
                    "city": r["city"],
                    "state_code": r["state_code"],
                    "state_name": r["state_name"],
                    "population": r["population"],
                    "leaders": [],
                }
                index[cid] = city
                cities.append(city)
            city["leaders"].append(
                {
                    "id": r["leader_id"],
                    "full_name": r["full_name"],
                    "last_name": r["last_name"],
                    "leader_title": r["leader_title"],
                    "political_party": r["political_party"],
                    "year_elected": r["year_elected"],
                    "next_election_year": r["next_election_year"],
                    "tenure_years": r["tenure_years"],
                    "term_length_years": r["term_length_years"],
                    "is_current": r["is_current"],
                    "created_at": r["created_at"],
                    "updated_at": r["updated_at"],
                }
            )

        # History changes infrequently (only when rotation/patch runs), so a
        # short cache is safe. Private because the response is behind API-key
        # auth and must not be cached by shared/edge caches per-key.
        response.headers["Cache-Control"] = "private, max-age=300, stale-while-revalidate=3600"

        return {
            "exported_at": datetime.now(UTC).isoformat(),
            "city_count": len(cities),
            "leader_count": len(rows),
            "cities": cities,
        }
