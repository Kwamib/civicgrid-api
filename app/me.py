"""User-facing endpoints for self-service API key management.

These endpoints are called by the Next.js dashboard after a user signs in
via Supabase Auth (Google, GitHub, or Magic Link).

Auth flow:
  1. Frontend sends: Authorization: Bearer <supabase_jwt>
  2. We verify JWT signature using SUPABASE_JWT_SECRET
  3. Extract user_id (UUID) and email from claims
  4. Endpoint operates on the authenticated user's data

This is separate from API key auth (which is for end users hitting /cities, /leaders).
JWT auth is for users managing their account; API key auth is for using the API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import jwt
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from app.auth import generate_key

router = APIRouter(prefix="/me", tags=["me"])


# ---------------------------------------------------------------------------
# JWT validation
# ---------------------------------------------------------------------------


@dataclass
class JWTUser:
    """User identity extracted from a verified Supabase JWT."""
    user_id: str  # Supabase auth.users.id (UUID)
    email: str


def verify_supabase_jwt(token: str) -> JWTUser:
    """Verify Supabase JWT signature and extract user identity.

    Raises HTTPException(401) on any verification failure.
    """
    secret = os.environ.get("SUPABASE_JWT_SECRET")
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="JWT auth not configured: SUPABASE_JWT_SECRET missing.",
        )

    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            audience="authenticated",  # Supabase JWT audience claim
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired. Please sign in again.") from None
    except jwt.InvalidAudienceError:
        raise HTTPException(status_code=401, detail="Invalid token audience.") from None
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}") from e

    user_id = payload.get("sub")
    email = payload.get("email")

    if not user_id or not email:
        raise HTTPException(
            status_code=401,
            detail="Token missing required claims (sub, email).",
        )

    return JWTUser(user_id=user_id, email=email)


def require_jwt_user(authorization: str | None = Header(None)) -> JWTUser:
    """FastAPI dependency: verify JWT, return user identity.

    Use in endpoints as: user: JWTUser = Depends(require_jwt_user)
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authorization header required: Bearer <jwt>",
        )

    token = authorization[7:].strip()
    return verify_supabase_jwt(token)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class MeResponse(BaseModel):
    user_id: str
    email: str
    key_count: int
    keys: list[dict]


class CreateKeyRequest(BaseModel):
    label: str | None = Field(default=None, max_length=100)
    tier: str = Field(default="free", pattern="^(free|starter|pro)$")


class CreateKeyResponse(BaseModel):
    key: str  # Full key, shown ONCE
    key_prefix: str
    tier: str
    label: str | None
    notice: str = (
        "Store this key now. It will not be shown again. "
        "If lost, revoke it and create a new one."
    )


# ---------------------------------------------------------------------------
# Routes (bound to get_cursor in main.py)
# ---------------------------------------------------------------------------


def attach_me_routes(app, get_cursor):
    """Register /me routes on the FastAPI app. Called from main.py."""

    @app.get("/me", response_model=MeResponse, tags=["me"])
    def get_me(authorization: str | None = Header(None)):
        """Get current authenticated user info + their API keys."""
        user = require_jwt_user(authorization)

        with get_cursor() as cur:
            cur.execute(
                """
                select id, key_prefix, tier, label,
                       created_at, last_used_at, revoked_at, request_count
                from api_keys
                where user_id = %s
                order by created_at desc
                """,
                (user.user_id,),
            )
            keys = cur.fetchall()

        return MeResponse(
            user_id=user.user_id,
            email=user.email,
            key_count=len([k for k in keys if k["revoked_at"] is None]),
            keys=keys,
        )

    @app.get("/me/keys", tags=["me"])
    def list_my_keys(authorization: str | None = Header(None)):
        """List all API keys belonging to the authenticated user."""
        user = require_jwt_user(authorization)

        with get_cursor() as cur:
            cur.execute(
                """
                select id, key_prefix, tier, label,
                       created_at, last_used_at, revoked_at, request_count
                from api_keys
                where user_id = %s
                order by created_at desc
                """,
                (user.user_id,),
            )
            rows = cur.fetchall()

        return {"data": rows, "count": len(rows)}

    @app.post("/me/keys", response_model=CreateKeyResponse, tags=["me"])
    def create_my_key(req: CreateKeyRequest, authorization: str | None = Header(None)):
        """Generate a new API key for the authenticated user."""
        user = require_jwt_user(authorization)

        full_key, prefix, hashed = generate_key()

        with get_cursor() as cur:
            cur.execute(
                """
                insert into api_keys (
                    key_hash, key_prefix, user_email, user_id, tier, label
                )
                values (%s, %s, %s, %s, %s, %s)
                returning key_prefix, tier, label
                """,
                (hashed, prefix, user.email, user.user_id, req.tier, req.label),
            )
            row = cur.fetchone()

        return CreateKeyResponse(
            key=full_key,
            key_prefix=row["key_prefix"],
            tier=row["tier"],
            label=row["label"],
        )

    @app.delete("/me/keys/{prefix}", tags=["me"])
    def revoke_my_key(prefix: str, authorization: str | None = Header(None)):
        """Revoke one of the authenticated user's API keys."""
        user = require_jwt_user(authorization)

        with get_cursor() as cur:
            cur.execute(
                """
                update api_keys
                set revoked_at = now()
                where key_prefix = %s
                  and user_id = %s
                  and revoked_at is null
                returning key_prefix
                """,
                (prefix, user.user_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(
                    status_code=404,
                    detail="Key not found, not yours, or already revoked.",
                )

        return {"revoked": True, "key_prefix": row["key_prefix"]}
