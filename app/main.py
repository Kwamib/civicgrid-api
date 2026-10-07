import os
from contextlib import contextmanager
from urllib.parse import unquote, urlparse

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool

from app.access import clamp_page_limit, require_bulk_export
from app.admin import attach_admin_routes
from app.auth import AuthMiddleware
from app.export import attach_export_routes
from app.me import attach_me_routes
from app.rate_limit import RateLimitMiddleware
from app.review import attach_review_routes
from app.states import attach_state_routes
from app.webhooks_admin import attach_webhook_admin_routes

app = FastAPI(
    title="CivicGrid API",
    description=(
        "US mayors and city managers. 3,063+ records.\n\n"
        "Most endpoints require authentication. Pass your API key via:\n"
        "`Authorization: Bearer cg_live_...`\n\n"
        "Public endpoints: `/`, `/health`, `/docs`."
    ),
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# Prometheus instrumentation: exposes /metrics for scraping.
Instrumentator().instrument(app).expose(app, include_in_schema=False)


_pool: SimpleConnectionPool | None = None


def _conn_kwargs():
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL not set")
    p = urlparse(db_url)
    return {
        "host": p.hostname,
        "port": p.port or 5432,
        "user": unquote(p.username) if p.username else None,
        "password": unquote(p.password) if p.password else None,
        "dbname": p.path.lstrip("/") or "postgres",
    }


@app.on_event("startup")
def startup():
    global _pool
    _pool = SimpleConnectionPool(minconn=1, maxconn=5, **_conn_kwargs())


@app.on_event("shutdown")
def shutdown():
    if _pool:
        _pool.closeall()


@contextmanager
def get_cursor():
    conn = _pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


# Middleware: AuthMiddleware runs FIRST (outermost), then RateLimitMiddleware.
# Starlette runs middleware in reverse-add order, so add rate_limit first.
app.add_middleware(RateLimitMiddleware)
app.add_middleware(AuthMiddleware, get_cursor=get_cursor)

# Admin routes attached after middleware so they get the cursor closure.
attach_admin_routes(app, get_cursor)
attach_review_routes(app, get_cursor)
attach_me_routes(app, get_cursor)
attach_export_routes(app, get_cursor)
attach_state_routes(app, get_cursor)
attach_webhook_admin_routes(app, get_cursor)


# Verification fields exposed on public city rows (additive; see migration 007).
#   leader_last_verified_at: when the current leader was last confirmed against a source
#   last_verified_method:    "human" (reviewer) or "automated" (verifier match)
#   verification_source_url: source used for that confirmation
#   last_checked_at:         latest check ATTEMPT of any outcome
#   last_check_result:       confirmed | under_review | check_failed (never the unpublished
#                            finding itself)
#   official_leader_page:    the city's official leadership page found by discovery
VERIFICATION_CTES = """
    with last_check as (
        select distinct on (city_id) city_id, verdict, verified_at
        from verification_results
        order by city_id, verified_at desc
    ),
    last_confirm as (
        select distinct on (city_id) city_id, verdict, source, verified_at
        from verification_results
        where verdict in ('MATCH', 'MANUAL')
        order by city_id, verified_at desc
    )
"""
VERIFICATION_COLUMNS = """
            c.governance_type,
            c.mayor_url        as official_leader_page,
            l.last_verified_at as leader_last_verified_at,
            case when l.last_verified_at is null then null
                 when cf.verdict = 'MANUAL' then 'human'
                 when cf.verdict = 'MATCH' then 'automated' end as last_verified_method,
            case when l.last_verified_at is null then null
                 else cf.source end as verification_source_url,
            lc.verified_at     as last_checked_at,
            case lc.verdict
                 when 'MATCH' then 'confirmed'
                 when 'MANUAL' then 'confirmed'
                 when 'ERROR' then 'check_failed'
                 when 'MISMATCH' then 'under_review'
                 when 'UNSURE' then 'under_review' end as last_check_result"""
VERIFICATION_JOINS = """
        left join last_check lc on lc.city_id = c.id
        left join last_confirm cf on cf.city_id = c.id
"""


@app.get("/")
def root():
    return {
        "service": "civicgrid-api",
        "version": "0.2.0",
        "endpoints": [
            "/health",
            "/cities",
            "/cities/{id}",
            "/leaders/current",
            "/stats",
        ],
        "authentication": "API key required for /cities, /leaders, /stats. "
        "Pass as: Authorization: Bearer cg_live_...",
        "docs": "/docs",
    }


@app.get("/health")
def health():
    try:
        with get_cursor() as cur:
            cur.execute("select 1 as ok")
            row = cur.fetchone()
        return {"status": "ok", "db": row["ok"] == 1}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"db unavailable: {e}") from e


@app.get("/cities")
def list_cities(
    request: Request,
    state: str | None = Query(None),
    city_type: str | None = Query(None),
    min_pop: int | None = Query(None, ge=0),
    max_pop: int | None = Query(None, ge=0),
    search: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    limit = clamp_page_limit(request, limit)
    where = []
    params: list = []

    if state:
        where.append("c.state_code = %s")
        params.append(state.upper())
    if city_type:
        where.append("c.city_type = %s")
        params.append(city_type)
    if min_pop is not None:
        where.append("c.population >= %s")
        params.append(min_pop)
    if max_pop is not None:
        where.append("c.population <= %s")
        params.append(max_pop)
    if search:
        where.append("c.city ilike %s")
        params.append(f"%{search}%")

    where_clause = ("where " + " and ".join(where)) if where else ""
    params.extend([limit, offset])

    sql = f"""
        {VERIFICATION_CTES}
        select
            c.id, c.city, c.state_code, c.state_name, c.county, c.metro_area,
            c.city_type, c.population, c.median_household_income, c.median_age,
            c.land_area_sq_mi, c.population_density,
            c.city_budget_text, c.city_budget_numeric, c.city_hall_phone, c.url,
            l.full_name        as leader_name,
            l.leader_title     as leader_title,
            l.political_party  as leader_party,
            l.year_elected     as leader_year_elected,
            l.next_election_year as leader_next_election,
{VERIFICATION_COLUMNS}
        from cities c
        left join leaders l on l.city_id = c.id and l.is_current = true
{VERIFICATION_JOINS}        {where_clause}
        order by c.population desc nulls last, c.city asc
        limit %s offset %s
    """
    with get_cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

        count_sql = f"select count(*) as n from cities c {where_clause}"
        cur.execute(count_sql, params[:-2])
        total = cur.fetchone()["n"]

    return {
        "data": rows,
        "pagination": {"total": total, "limit": limit, "offset": offset},
    }


@app.get("/cities/all")
def all_cities(request: Request, response: Response):
    """Bulk fetch endpoint - returns every city with current leader in one query.

    Designed for clients that need the full dataset (e.g. the landing page's
    client-side search). Replaces 30+ paginated calls with a single round trip.

    Response is intentionally NOT paginated and NOT filtered - clients filter
    locally. Total payload is ~500KB JSON for 3,063 cities.

    Paid tiers only (see app/access.py). Because access now depends on the
    caller's key, the response is cacheable by the client only ("private"),
    never by a shared/edge cache that could serve it to a free key.
    """
    require_bulk_export(request)
    sql = f"""
        {VERIFICATION_CTES}
        select
            c.id, c.city, c.state_code, c.state_name, c.county, c.metro_area,
            c.city_type, c.population, c.median_household_income, c.median_age,
            c.land_area_sq_mi, c.population_density,
            c.city_budget_text, c.city_budget_numeric, c.city_hall_phone, c.url,
            l.full_name        as leader_name,
            l.leader_title     as leader_title,
            l.political_party  as leader_party,
            l.year_elected     as leader_year_elected,
            l.next_election_year as leader_next_election,
{VERIFICATION_COLUMNS}
        from cities c
        left join leaders l on l.city_id = c.id and l.is_current = true
{VERIFICATION_JOINS}        order by c.population desc nulls last, c.city asc
    """
    with get_cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()

    response.headers["Cache-Control"] = "private, max-age=300"
    return {"data": rows, "count": len(rows)}


@app.get("/cities/{city_id}")
def get_city(city_id: int):
    with get_cursor() as cur:
        cur.execute(
            """
            select
                c.*,
                l.full_name        as leader_name,
                l.leader_title     as leader_title,
                l.political_party  as leader_party,
                l.year_elected     as leader_year_elected,
                l.next_election_year as leader_next_election,
                l.tenure_years     as leader_tenure
            from cities c
            left join leaders l on l.city_id = c.id and l.is_current = true
            where c.id = %s
        """,
            (city_id,),
        )
        city = cur.fetchone()
        if not city:
            raise HTTPException(status_code=404, detail="city not found")

        cur.execute(
            """
            select data_source, last_verified_date, created_at
            from provenance
            where city_id = %s
            order by created_at desc
        """,
            (city_id,),
        )
        prov = cur.fetchall()

        # Published leadership history only: leader rows (current and former)
        # and reviewer-approved changes. Unpublished findings and reviewer
        # identities/notes are never exposed here.
        cur.execute(
            """
            select id, full_name, leader_title, is_current,
                   created_at as recorded_at, last_verified_at
            from leaders
            where city_id = %s
            order by is_current desc, created_at desc nulls last, id desc
            limit 25
        """,
            (city_id,),
        )
        leadership_history = cur.fetchall()

        cur.execute(
            """
            select action, before_name, before_title, after_name, after_title,
                   source_url, created_at
            from review_events
            where city_id = %s and action in ('accept', 'correct')
            order by created_at desc
            limit 25
        """,
            (city_id,),
        )
        published_changes = cur.fetchall()

    return {
        "city": city,
        "provenance": prov,
        "leadership_history": leadership_history,
        "published_changes": published_changes,
    }


@app.get("/leaders/current")
def list_current_leaders(
    request: Request,
    party: str | None = Query(None),
    state: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    limit = clamp_page_limit(request, limit)
    where = ["l.is_current = true"]
    params: list = []

    if party:
        where.append("l.political_party = %s")
        params.append(party)
    if state:
        where.append("c.state_code = %s")
        params.append(state.upper())

    where_clause = "where " + " and ".join(where)
    params.extend([limit, offset])

    sql = f"""
        select
            l.id, l.full_name, l.last_name, l.leader_title, l.political_party,
            l.year_elected, l.next_election_year, l.tenure_years, l.term_length_years,
            c.id as city_id, c.city, c.state_code, c.state_name, c.population
        from leaders l
        join cities c on c.id = l.city_id
        {where_clause}
        order by c.population desc nulls last
        limit %s offset %s
    """
    with get_cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return {"data": rows}


@app.get("/stats")
def stats():
    with get_cursor() as cur:
        cur.execute("select count(*) as n from cities")
        total_cities = cur.fetchone()["n"]

        cur.execute("""
            select state_code, count(*) as n
            from cities
            group by state_code
            order by n desc
            limit 10
        """)
        cities_by_state_top10 = cur.fetchall()

        cur.execute("""
            select city_type, count(*) as n
            from cities
            group by city_type
            order by n desc
        """)
        cities_by_type = cur.fetchall()

        cur.execute("""
            select coalesce(political_party, 'Unknown') as party, count(*) as n
            from leaders
            where is_current = true
            group by political_party
            order by n desc
        """)
        leaders_by_party = cur.fetchall()

    return {
        "total_cities": total_cities,
        "cities_by_state_top10": cities_by_state_top10,
        "cities_by_type": cities_by_type,
        "leaders_by_party": leaders_by_party,
    }
