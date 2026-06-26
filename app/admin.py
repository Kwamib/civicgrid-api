"""Admin endpoints for API key management and leader rotation/correction.

All endpoints require Authorization: Bearer <ADMIN_TOKEN> where ADMIN_TOKEN is
set as an env var. This is a deliberately simple admin auth — full user
accounts come in Step 14 with the frontend.

Endpoints:
  POST   /admin/keys                                  Create a new API key for a user
  GET    /admin/keys                                  List all keys (prefix only — never recoverable)
  DELETE /admin/keys/{prefix}                         Revoke a key by prefix
  POST   /admin/cities/{city_id}/leaders             Rotate a city's current leader (demote + insert, atomic)
  PATCH  /admin/cities/{city_id}/leaders/{leader_id} Correct a leader's fields in place (partial update)
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, EmailStr, Field

from app.auth import generate_key

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


class RotateLeaderRequest(BaseModel):
    """New current leader for a city. Existing current leader is demoted, not deleted."""

    full_name: str = Field(min_length=1, max_length=200)
    # Optional: derived from the last whitespace-separated token of full_name if omitted.
    last_name: str | None = Field(default=None, max_length=100)
    leader_title: str | None = Field(default=None, max_length=100)
    political_party: str | None = Field(default=None, max_length=100)
    year_elected: int | None = Field(default=None, ge=1700, le=2100)
    next_election_year: int | None = Field(default=None, ge=1700, le=2100)
    tenure_years: int | None = Field(default=None, ge=0, le=100)
    term_length_years: int | None = Field(default=None, ge=1, le=20)


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
        return {"revoked": True, "key_prefix": row["key_prefix"], "user_email": row["user_email"]}

    @app.post("/admin/cities/{city_id}/leaders", status_code=201, tags=["admin"])
    def rotate_leader(
        city_id: int,
        req: RotateLeaderRequest,
        authorization: str | None = Header(None),
    ):
        """Set a new current leader for a city.

        Demotes whoever is currently marked is_current=true (history preserved,
        rows are never deleted) and inserts the new leader as current. The whole
        operation runs in ONE transaction via get_cursor(), which commits on a
        clean exit and rolls back on any exception. So the demotion and the
        insert either both land or neither does — there is no window where the
        city has zero current leaders.
        """
        require_admin(authorization)

        full_name = req.full_name.strip()
        if not full_name:
            raise HTTPException(status_code=422, detail="full_name cannot be blank.")
        # Derive last_name from the final token if the caller didn't supply one.
        last_name = (req.last_name or full_name.split()[-1]).strip()

        with get_cursor() as cur:
            # 1. Confirm the city exists; also grab display fields for the response.
            cur.execute(
                "select id, city, state_code from cities where id = %s",
                (city_id,),
            )
            city = cur.fetchone()
            if not city:
                raise HTTPException(status_code=404, detail=f"City {city_id} not found.")

            # 2. Demote the current incumbent(s). Zero rows is fine (no incumbent
            #    yet); more than one row self-heals a data anomaly.
            cur.execute(
                """
                update leaders
                set is_current = false, updated_at = now()
                where city_id = %s and is_current = true
                returning id, full_name
                """,
                (city_id,),
            )
            demoted = cur.fetchall()

            # 3. Insert the new current leader.
            cur.execute(
                """
                insert into leaders (
                    city_id, full_name, last_name, leader_title, political_party,
                    year_elected, next_election_year, tenure_years, term_length_years,
                    is_current, created_at, updated_at
                )
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, true, now(), now())
                returning
                    id, city_id, full_name, last_name, leader_title, political_party,
                    year_elected, next_election_year, tenure_years, term_length_years,
                    is_current, created_at, updated_at
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
                ),
            )
            new_leader = cur.fetchone()

        return {
            "city_id": city["id"],
            "city": city["city"],
            "state_code": city["state_code"],
            "previous_current": demoted,
            "new_current": new_leader,
        }

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
                is_current, created_at, updated_at
        """

        with get_cursor() as cur:
            cur.execute(sql, values)
            row = cur.fetchone()
            if not row:
                raise HTTPException(
                    status_code=404,
                    detail=f"Leader {leader_id} not found for city {city_id}.",
                )

        return {"updated_fields": cols, "leader": row}
