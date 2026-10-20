"""Admin endpoints for API key management and leader rotation/correction.

All endpoints require Authorization: Bearer <ADMIN_TOKEN> where ADMIN_TOKEN is
set as an env var. This is a deliberately simple admin auth — full user
accounts come in Step 14 with the frontend.

Endpoints:
  POST   /admin/keys                                  Create a new API key for a user
  GET    /admin/keys                                  List all keys (prefix only — never recoverable)
  DELETE /admin/keys/{prefix}                         Revoke a key by prefix
  POST   /admin/cities/{city_id}/leaders             Rotate a city's current leader for one role (demote + insert, atomic)
  PATCH  /admin/cities/{city_id}/leaders/{leader_id} Correct a leader's fields in place (partial update)

Leader rotation and correction emit webhook events (leader.rotated /
leader.updated) into the outbox in the SAME transaction as the data change.
"""

from __future__ import annotations

import logging
import os
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, EmailStr, Field

from app.auth import forget_key, generate_key
from app.leaders import former_leader_id, reinstate
from app.names import NameNeedsReview, clean_full_name, derive_last_name, same_person_spelling
from app.sources import source_problem
from app.webhook_events import EVENT_LEADER_ROTATED, EVENT_LEADER_UPDATED, emit_event

LEADER_RETURNING = (
    "id, city_id, full_name, last_name, leader_title, political_party, "
    "year_elected, next_election_year, tenure_years, term_length_years, "
    "role, is_current, created_at, updated_at, last_verified_at"
)

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Admin auth (separate from API key auth)
# ---------------------------------------------------------------------------


def require_admin(authorization: str | None = Header(None)) -> None:
    """Verifies the request carries the admin token. Raises 401/403 on failure."""
    admin_token = os.environ.get("ADMIN_TOKEN")
    if not admin_token:
        raise HTTPException(
            status_code=503, detail="Admin endpoints disabled: ADMIN_TOKEN not configured."
        )

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Admin authorization required.")

    presented = authorization[7:].strip()
    # Constant-time comparison.
    import hmac

    if not hmac.compare_digest(presented, admin_token):
        raise HTTPException(status_code=403, detail="Invalid admin token.")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CreateKeyRequest(BaseModel):
    user_email: EmailStr
    tier: str = Field(default="free", pattern="^(free|starter|pro)$")
    label: str | None = Field(default=None, max_length=100)


class CreateKeyResponse(BaseModel):
    key: str  # Full key, shown ONCE
    key_prefix: str
    user_email: str
    tier: str
    label: str | None
    notice: str = (
        "Store this key now. It will not be shown again. If lost, revoke it and create a new one."
    )


class KeyInfo(BaseModel):
    id: int
    key_prefix: str
    user_email: str
    tier: str
    label: str | None
    created_at: str
    last_used_at: str | None
    revoked_at: str | None
    request_count: int


LeaderRole = Literal["chief_executive", "chief_administrator"]


class RotateLeaderRequest(BaseModel):
    """New current leader for a city and role. The existing current holder of THAT
    role is demoted, not deleted; the other role is never touched.

    role: chief_executive (mayor, select board chair, village president: the
    default, and what every public "leader" field means) or chief_administrator
    (town administrator, city manager).
    """

    role: LeaderRole = "chief_executive"

    full_name: str = Field(min_length=1, max_length=200)
    # Optional: derived from the last whitespace-separated token of full_name if omitted.
    last_name: str | None = Field(default=None, max_length=100)
    leader_title: str | None = Field(default=None, max_length=100)
    political_party: str | None = Field(default=None, max_length=100)
    year_elected: int | None = Field(default=None, ge=1700, le=2100)
    next_election_year: int | None = Field(default=None, ge=1700, le=2100)
    tenure_years: int | None = Field(default=None, ge=0, le=100)
    term_length_years: int | None = Field(default=None, ge=1, le=20)
    source: str | None = Field(default=None, max_length=500)
    governance_type: str | None = Field(default=None, max_length=50)
    # Reviewer says this is the CURRENT leader with a corrected spelling
    # (e.g. "Quiton Lucas" -> "Quinton Lucas"): update the row in place even
    # when the names differ by more than spacing or an initial.
    same_person: bool = False
    # A chief-executive save is recorded as a human verification with `source`
    # as its evidence. A source that isn't plausibly the city's own site
    # (app/sources.py) is refused unless the reviewer confirms they checked it.
    confirm_source: bool = False


class CityLinksRequest(BaseModel):
    """Fix a city's website and/or official leadership page (Fix-a-city).

    Only fields present are changed. Addresses must be on a .gov or
    .<state>.us domain; anything else needs confirm_official=true (the
    reviewer has checked it is the city's own site, e.g. dsm.city).
    """

    url: str | None = Field(default=None, max_length=500)
    mayor_url: str | None = Field(default=None, max_length=500)
    confirm_official: bool = False


log = logging.getLogger("civicgrid.admin")


def _official_url(value: str, state_code: str, confirm_official: bool, field: str) -> str:
    """Normalise and check a city link; raise 422 if it fails the domain rule."""
    v = value.strip()
    if not v.lower().startswith(("http://", "https://")):
        v = "https://" + v
    host = (urlparse(v).hostname or "").lower()
    if not host or "." not in host or " " in v:
        raise HTTPException(status_code=422, detail=f"{field}: not a valid web address.")
    state_suffix = f".{state_code.lower()}.us"
    if not (host.endswith(".gov") or host.endswith(state_suffix) or confirm_official):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "not_government_domain",
                "message": (
                    f"{field}: {host} is not a .gov or {state_suffix} address. Tick "
                    "'confirmed official' if you have checked it is the city's own site."
                ),
            },
        )
    return v


class PatchLeaderRequest(BaseModel):
    """Partial update of a leader's attributes.

    Only fields PRESENT in the request body are modified; omitted fields are
    left untouched. Nullable fields (leader_title, political_party, election
    years, tenure) may be explicitly set to null to clear them. is_current is
    intentionally NOT patchable — use the rotation endpoint to change who is
    current, so the single-current-leader invariant stays owned by one place.
    """

    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    leader_title: str | None = Field(default=None, max_length=100)
    political_party: str | None = Field(default=None, max_length=100)
    year_elected: int | None = Field(default=None, ge=1700, le=2100)
    next_election_year: int | None = Field(default=None, ge=1700, le=2100)
    tenure_years: int | None = Field(default=None, ge=0, le=100)
    term_length_years: int | None = Field(default=None, ge=1, le=20)


# Columns a PATCH is allowed to touch. is_current and identity/timestamp
# columns are deliberately excluded. Used as a defense-in-depth whitelist when
# building the dynamic SET clause (column names never come from raw user input).
PATCHABLE_COLUMNS = {
    "full_name",
    "last_name",
    "leader_title",
    "political_party",
    "year_elected",
    "next_election_year",
    "tenure_years",
    "term_length_years",
}

# Columns that must never be set to NULL (NOT NULL in the schema).
NON_NULLABLE_COLUMNS = {"full_name", "last_name"}


# ---------------------------------------------------------------------------
# Routes (bound to a get_cursor dependency in main.py)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Manual-check queue (migration 012: cities.last_check_outcome / last_check_at)
# ---------------------------------------------------------------------------

QUEUE_KINDS = ("unverified", "blocked", "no_name", "wrong_link")

# Verifier outcomes (verify_mayors_v7.py) that put a city in each tab.
QUEUE_OUTCOMES = {
    "blocked": ("blocked", "soft_blocked", "robots_deny"),
    "no_name": ("no_name", "bad_extract", "guard_blocked"),
    "wrong_link": ("notfound", "off_domain", "wrong_state", "bad_page"),
}

# Discovery's cities.fetch_status for cities the verifier never reads (it only
# reads fetch_status = 'ok'). 'skip' is a deliberate exclusion: in no tab.
FETCH_STATUS_QUEUE = {
    "blocked": ("blocked", "robots_deny"),
    "wrong_link": ("notfound", "no_mayor_pg", "unresolved", "no_site", "wrong_state"),
}


def _sql_list(values) -> str:
    return ", ".join("'" + v + "'" for v in values)  # constants above, never user input


# A city whose leader was verified (by the verifier or a reviewer) within
# this many days is resolved and appears in no tab. Saving in Fix-a-city
# stamps a verification, so the city drops off at once.
QUEUE_FRESH_DAYS = 180

# One classification for every city, so tab counts and tab contents agree.
QUEUE_SQL = f"""
    with base as (
        select c.id as city_id, c.city, c.state_code, c.population,
               c.governance_type, c.url, c.mayor_url, c.fetch_status,
               c.last_check_outcome, c.last_check_at,
               c.link_note, c.suggested_url,
               l.id as leader_id, l.full_name, l.leader_title, l.last_verified_at,
               coalesce(l.last_verified_at > now() - interval '{QUEUE_FRESH_DAYS} days', false)
                   as resolved
        from cities c
        left join leaders l on l.city_id = c.id and l.is_current = true
                             and l.role = 'chief_executive'
    ),
    classified as (
        select *,
            case
                when fetch_status = 'skip' then null
                -- discovery found the website itself wrong (migration 013):
                -- worth fixing even when the leader was verified recently
                when link_note is not null or suggested_url is not null then 'wrong_link'
                when resolved then null
                when last_check_outcome in ({_sql_list(QUEUE_OUTCOMES["blocked"])}) then 'blocked'
                when last_check_outcome in ({_sql_list(QUEUE_OUTCOMES["no_name"])}) then 'no_name'
                when last_check_outcome in ({_sql_list(QUEUE_OUTCOMES["wrong_link"])}) then 'wrong_link'
                when fetch_status in ({_sql_list(FETCH_STATUS_QUEUE["blocked"])}) then 'blocked'
                when fetch_status is null
                     or fetch_status in ({_sql_list(FETCH_STATUS_QUEUE["wrong_link"])})
                     or mayor_url is null then 'wrong_link'
                else 'unverified'
            end as queue
        from base
    )
"""


def attach_admin_routes(app, get_cursor):
    """Register admin routes on the FastAPI app. Called from main.py."""

    @app.post("/admin/keys", response_model=CreateKeyResponse, tags=["admin"])
    def create_key(req: CreateKeyRequest, authorization: str | None = Header(None)):
        require_admin(authorization)
        full_key, prefix, hashed = generate_key()

        with get_cursor() as cur:
            cur.execute(
                """
                insert into api_keys (key_hash, key_prefix, user_email, tier, label)
                values (%s, %s, %s, %s, %s)
                returning id, key_prefix, user_email, tier, label
                """,
                (hashed, prefix, req.user_email, req.tier, req.label),
            )
            row = cur.fetchone()

        return CreateKeyResponse(
            key=full_key,
            key_prefix=row["key_prefix"],
            user_email=row["user_email"],
            tier=row["tier"],
            label=row["label"],
        )

    @app.get("/admin/keys", tags=["admin"])
    def list_keys(authorization: str | None = Header(None)):
        require_admin(authorization)
        with get_cursor() as cur:
            cur.execute(
                """
                select id, key_prefix, user_email, tier, label,
                       created_at, last_used_at, revoked_at, request_count
                from api_keys
                order by created_at desc
                """
            )
            rows = cur.fetchall()
        return {"data": rows, "count": len(rows)}

    @app.delete("/admin/keys/{prefix}", tags=["admin"])
    def revoke_key(prefix: str, authorization: str | None = Header(None)):
        require_admin(authorization)
        with get_cursor() as cur:
            cur.execute(
                """
                update api_keys
                set revoked_at = now()
                where key_prefix = %s and revoked_at is null
                returning id, key_prefix, user_email
                """,
                (prefix,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Key not found or already revoked.")
        forget_key(row["key_prefix"])
        return {"revoked": True, "key_prefix": row["key_prefix"], "user_email": row["user_email"]}

    @app.post("/admin/cities/{city_id}/leaders", status_code=201, tags=["admin"])
    def rotate_leader(
        city_id: int,
        req: RotateLeaderRequest,
        authorization: str | None = Header(None),
    ):
        """Set a new current leader for a city and role.

        Demotes whoever currently holds req.role (history preserved,
        rows are never deleted) and inserts the new leader as current. The whole
        operation runs in ONE transaction via get_cursor(), which commits on a
        clean exit and rolls back on any exception. So the demotion and the
        insert either both land or neither does — there is no window where the
        city has zero current leaders.
        """
        require_admin(authorization)

        # Strip titles ("Mayor", ", Mayor", "Hon.") and refuse role titles
        # (Vice/Acting/Interim/Pro Tem) that mean a different office.
        try:
            full_name = clean_full_name(req.full_name)
        except NameNeedsReview as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        last_name = (req.last_name or derive_last_name(full_name)).strip()

        with get_cursor() as cur:
            # 1. Confirm the city exists; also grab display fields for the response.
            cur.execute(
                "select id, city, state_code, url from cities where id = %s",
                (city_id,),
            )
            city = cur.fetchone()
            if not city:
                raise HTTPException(status_code=404, detail=f"City {city_id} not found.")
            if req.role == "chief_executive" and req.source and not req.confirm_source:
                problem = source_problem(req.source, city["city"], city["state_code"], city["url"])
                if problem:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "error": "source_not_official",
                            "message": (
                                f"Source: {problem}. This save records the leader as "
                                "verified from that page. Use the city's own page, or tick "
                                "'I checked this source is the city's own official page'."
                            ),
                        },
                    )

            new_leader = None
            # 2. Same person already current? Update that row in place instead of
            #    rotating, so history doesn't gain a fake transition.
            cur.execute(
                "select id, full_name from leaders "
                "where city_id = %s and is_current = true and role = %s",
                (city_id, req.role),
            )
            incumbents = cur.fetchall()
            if req.same_person and len(incumbents) != 1:
                raise HTTPException(
                    status_code=409,
                    detail="Spelling fix needs exactly one current leader to correct.",
                )
            if len(incumbents) == 1 and (
                req.same_person or same_person_spelling(incumbents[0]["full_name"], full_name)
            ):
                demoted = []
                cur.execute(
                    """
                    update leaders set
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
                    returning
                        id, city_id, full_name, last_name, leader_title, political_party,
                        year_elected, next_election_year, tenure_years, term_length_years,
                        role, is_current, created_at, updated_at, last_verified_at
                    """,
                    (
                        full_name,
                        last_name,
                        req.leader_title,
                        req.political_party,
                        req.year_elected,
                        req.next_election_year,
                        req.tenure_years,
                        req.term_length_years,
                        incumbents[0]["id"],
                    ),
                )
                new_leader = cur.fetchone()
            else:
                # 2. Demote the current incumbent(s). Zero rows is fine (no incumbent
                #    yet); more than one row self-heals a data anomaly.
                cur.execute(
                    """
                    update leaders
                    set is_current = false, updated_at = now()
                    where city_id = %s and is_current = true and role = %s
                    returning id, full_name, role
                    """,
                    (city_id, req.role),
                )
                demoted = cur.fetchall()

                # 3. Already in this city's history (Simi Valley CA, Oct 2026)?
                #    Make that row current again rather than adding a second one.
                former_id = former_leader_id(cur, city_id, req.role, full_name)
                if former_id is not None:
                    new_leader = reinstate(
                        cur,
                        former_id,
                        full_name=full_name,
                        last_name=last_name,
                        leader_title=req.leader_title,
                        political_party=req.political_party,
                        year_elected=req.year_elected,
                        next_election_year=req.next_election_year,
                        tenure_years=req.tenure_years,
                        term_length_years=req.term_length_years,
                        returning=LEADER_RETURNING,
                    )

            if new_leader is None:
                # 4. Insert the new current leader.
                cur.execute(
                    """
                    insert into leaders (
                        city_id, full_name, last_name, leader_title, political_party,
                        year_elected, next_election_year, tenure_years, term_length_years,
                        role, is_current, created_at, updated_at, last_verified_at
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, true, now(), now(), now())
                    returning
                        id, city_id, full_name, last_name, leader_title, political_party,
                        year_elected, next_election_year, tenure_years, term_length_years,
                        role, is_current, created_at, updated_at, last_verified_at
                    """,
                    (
                        city_id,
                        full_name,
                        last_name,
                        req.leader_title,
                        req.political_party,
                        req.year_elected,
                        req.next_election_year,
                        req.tenure_years,
                        req.term_length_years,
                        req.role,
                    ),
                )
                new_leader = cur.fetchone()

            # Record this manual edit as a verification (human-confirmed). Only for
            # the chief executive: verification_results drives the public
            # "last verified" fields, which describe the leader, not the administrator.
            if req.role == "chief_executive":
                cur.execute(
                    """
                    insert into verification_results
                        (city_id, verdict, web_mayor, source, model)
                    values (%s, 'MANUAL', %s, %s, 'human')
                    """,
                    (city_id, full_name, req.source),
                )

            # Set governance type on the city if provided (e.g. select_board).
            if req.governance_type:
                cur.execute(
                    "update cities set governance_type = %s where id = %s",
                    (req.governance_type, city_id),
                )

            # Outbox write, in the SAME transaction as the rotation above.
            # If anything here raises, get_cursor() rolls back the whole thing
            # (demote + insert + event), so the event can't outlive a failed
            # rotation, nor a rotation outlive a failed event.
            emit_event(
                cur,
                EVENT_LEADER_ROTATED,
                {
                    "city": {
                        "id": city["id"],
                        "city": city["city"],
                        "state_code": city["state_code"],
                    },
                    "new_current": new_leader,
                    "previous_current": demoted,
                },
            )

        return {
            "city_id": city["id"],
            "city": city["city"],
            "state_code": city["state_code"],
            "role": req.role,
            "previous_current": demoted,
            "new_current": new_leader,
        }

    @app.get("/admin/unsure", tags=["admin"])
    def list_unsure(
        authorization: str | None = Header(None),
        limit: int = 50,
        offset: int = 0,
    ):
        """UNSURE worklist: cities the verifier couldn't confidently determine.

        Pulls the latest UNSURE verdict per city from verification_results, joined
        to the current leader + governance_type. Excludes cities that have since
        been verified (last_verified_at set) — those are already resolved, so they
        drop off the worklist automatically. Ordered by population desc.
        """
        require_admin(authorization)
        limit = max(1, min(limit, 200))

        sql = """
            with latest_unsure as (
                select distinct on (city_id) city_id, verified_at, source
                from verification_results
                where verdict = 'UNSURE'
                order by city_id, verified_at desc
            )
            select c.id as city_id, c.city, c.state_code, c.population,
                   c.governance_type,
                   l.id as leader_id, l.full_name, l.leader_title,
                   l.last_verified_at, c.url,
                   u.verified_at as flagged_at
            from latest_unsure u
            join cities c on c.id = u.city_id
            left join leaders l on l.city_id = c.id and l.is_current = true
                                 and l.role = 'chief_executive'
            where l.last_verified_at is null       -- not yet resolved
            order by c.population desc nulls last, c.city
            limit %s offset %s
        """
        with get_cursor() as cur:
            cur.execute(sql, (limit, offset))
            rows = cur.fetchall()
            # total remaining
            cur.execute("""
                with latest_unsure as (
                    select distinct on (city_id) city_id
                    from verification_results
                    where verdict = 'UNSURE'
                    order by city_id, verified_at desc
                )
                select count(*) as n
                from latest_unsure u
                join cities c on c.id = u.city_id
                left join leaders l on l.city_id = c.id and l.is_current = true
                                 and l.role = 'chief_executive'
                where l.last_verified_at is null
            """)
            total = cur.fetchone()["n"]
        return {"cities": rows, "count": len(rows), "total_remaining": total}

    @app.get("/admin/cities/search", tags=["admin"])
    def search_cities(
        authorization: str | None = Header(None),
        q: str = "",
        only_unverified: bool = False,
        limit: int = 25,
        city_id: int | None = None,
    ):
        """Search cities by name for the admin fix-a-city UI.

        Returns each match with its current leader and verification status.
        q matches city name (prefix or contained, case-insensitive). If
        only_unverified is true, restricts to cities whose current leader has
        no last_verified_at (the 'needs attention' set).

        city_id returns that one city instead (q is ignored), for Fix-a-city
        links like /admin/cities?id=2140. City name plus state isn't unique;
        the id is.
        """
        require_admin(authorization)
        q = (q or "").strip()
        limit = max(1, min(limit, 100))

        if city_id is not None:
            where = ["c.id = %s"]
            params: list = [city_id]
        else:
            where = ["c.city ilike %s"]
            params = [f"%{q}%"]
        if only_unverified:
            where.append("l.last_verified_at is null")

        sql = f"""
            select c.id as city_id, c.city, c.state_code, c.population,
                   l.id as leader_id, l.full_name, l.leader_title,
                   l.political_party, l.last_verified_at, c.url,
                   c.mayor_url, c.last_check_outcome, c.last_check_at,
                   c.link_note, c.suggested_url
            from cities c
            left join leaders l on l.city_id = c.id and l.is_current = true
                                 and l.role = 'chief_executive'
            where {" and ".join(where)}
            order by c.population desc nulls last, c.city
            limit %s
        """
        params.append(limit)
        with get_cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return {"cities": rows, "count": len(rows)}

    @app.patch("/admin/cities/{city_id}/links", tags=["admin"])
    def update_city_links(
        city_id: int,
        req: CityLinksRequest,
        authorization: str | None = Header(None),
    ):
        """Set a city's website (url) and/or official leadership page (mayor_url).

        A new leadership page is marked readable (fetch_status 'ok') and its
        stored hash and last outcome are cleared, so the next verifier run
        reads it fresh. Before/after values are logged.
        """
        require_admin(authorization)
        if req.url is None and req.mayor_url is None:
            raise HTTPException(status_code=422, detail="Provide url and/or mayor_url.")

        with get_cursor() as cur:
            cur.execute(
                "select id, city, state_code, url, mayor_url, fetch_status from cities "
                "where id = %s",
                (city_id,),
            )
            before = cur.fetchone()
            if not before:
                raise HTTPException(status_code=404, detail=f"City {city_id} not found.")

            sets, values = [], []
            if req.url is not None:
                # a person fixed the website: discovery's note no longer applies
                sets += ["link_note = null", "suggested_url = null"]
                sets.append("url = %s")
                values.append(
                    _official_url(req.url, before["state_code"], req.confirm_official, "Website")
                )
            if req.mayor_url is not None:
                new_page = _official_url(
                    req.mayor_url, before["state_code"], req.confirm_official, "Leadership page"
                )
                sets += [
                    "mayor_url = %s",
                    "fetch_status = 'ok'",
                    "page_hash = null",
                    "page_etag = null",
                    "last_check_outcome = null",
                    "last_check_at = null",
                ]
                values.append(new_page)
            values.append(city_id)
            cur.execute(
                f"update cities set {', '.join(sets)} where id = %s "
                "returning id, city, state_code, url, mayor_url, fetch_status",
                values,
            )
            after = cur.fetchone()

        log.info(
            "city links updated: id=%s %s, %s | url %r -> %r | mayor_url %r -> %r",
            city_id,
            before["city"],
            before["state_code"],
            before["url"],
            after["url"],
            before["mayor_url"],
            after["mayor_url"],
        )
        return {"before": before, "city": after}

    @app.get("/admin/queue", tags=["admin"])
    def manual_check_queue(
        authorization: str | None = Header(None),
        kind: str = "unverified",
        limit: int = 25,
        offset: int = 0,
    ):
        """Manual-check queue for Fix-a-city, largest cities first.

        kind:
          unverified  no traceable verification yet (or no leader on record)
          blocked     the city's site refuses scripts: check it by hand
          no_name     the page loaded but no leader name could be read
          wrong_link  the official page is missing, dead, moved or off-site, or
                      discovery found the website itself wrong (link_note,
                      suggested_url; listed first when there is a suggestion)

        Tabs are disjoint (a city is in at most one). A city whose leader was
        verified in the last QUEUE_FRESH_DAYS days is in none.
        Returns the page of cities plus the size of every tab.
        """
        require_admin(authorization)
        if kind not in QUEUE_KINDS:
            raise HTTPException(
                status_code=422, detail=f"kind must be one of {', '.join(QUEUE_KINDS)}"
            )
        limit = max(1, min(limit, 100))
        offset = max(0, offset)

        with get_cursor() as cur:
            cur.execute(
                QUEUE_SQL
                + """
                select city_id, city, state_code, population, governance_type,
                       leader_id, full_name, leader_title, last_verified_at, url,
                       mayor_url, last_check_outcome, last_check_at,
                       link_note, suggested_url
                from classified
                where queue = %s
                order by (suggested_url is null), population desc nulls last, city
                limit %s offset %s
                """,
                (kind, limit, offset),
            )
            rows = cur.fetchall()
            cur.execute(
                QUEUE_SQL + "select queue, count(*) as n from classified "
                "where queue is not null group by queue"
            )
            counts = {k: 0 for k in QUEUE_KINDS}
            counts.update({r["queue"]: r["n"] for r in cur.fetchall()})
        return {"kind": kind, "cities": rows, "count": len(rows), "counts": counts}

    # Leader proposals (the review queue) live in app/review.py.

    @app.patch("/admin/cities/{city_id}/leaders/{leader_id}", tags=["admin"])
    def patch_leader(
        city_id: int,
        leader_id: int,
        req: PatchLeaderRequest,
        authorization: str | None = Header(None),
    ):
        """Correct a leader's fields in place without rotating.

        Partial update: only the fields present in the request body are written;
        everything else is left untouched. Does NOT touch is_current and does
        NOT create a history row — use the POST rotation endpoint to change who
        is current. Scoped by both city_id and leader_id so a leader can only be
        edited under the city it actually belongs to.
        """
        require_admin(authorization)

        # exclude_unset => only the fields the caller actually sent. This is what
        # makes it a partial update: an omitted field is absent here (untouched),
        # while a field explicitly set to null is present here (cleared).
        updates = req.model_dump(exclude_unset=True)
        if not updates:
            raise HTTPException(status_code=422, detail="No fields provided to update.")

        # Reject explicit nulls on NOT NULL columns before hitting the DB.
        for col in NON_NULLABLE_COLUMNS:
            if col in updates and updates[col] is None:
                raise HTTPException(status_code=422, detail=f"{col} cannot be set to null.")

        # Build the SET clause from the whitelist only. Column names come from
        # PATCHABLE_COLUMNS (never from raw user input); values are parameterized.
        cols = [c for c in updates if c in PATCHABLE_COLUMNS]
        if not cols:
            raise HTTPException(status_code=422, detail="No updatable fields provided.")

        set_clauses = [f"{c} = %s" for c in cols]
        set_clauses.append("updated_at = now()")
        values = [updates[c] for c in cols]
        values.extend([leader_id, city_id])

        sql = f"""
            update leaders
            set {", ".join(set_clauses)}
            where id = %s and city_id = %s
            returning
                id, city_id, full_name, last_name, leader_title, political_party,
                year_elected, next_election_year, tenure_years, term_length_years,
                role, is_current, created_at, updated_at
        """

        with get_cursor() as cur:
            cur.execute(sql, values)
            row = cur.fetchone()
            if not row:
                raise HTTPException(
                    status_code=404,
                    detail=f"Leader {leader_id} not found for city {city_id}.",
                )

            # Outbox write, in the SAME transaction as the update above.
            emit_event(
                cur,
                EVENT_LEADER_UPDATED,
                {
                    "city": {"id": row["city_id"]},
                    "leader": row,
                    "updated_fields": cols,
                },
            )

        return {"updated_fields": cols, "leader": row}
