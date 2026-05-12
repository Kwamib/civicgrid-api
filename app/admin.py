"""Admin endpoints for API key management.

All endpoints require Authorization: Bearer <ADMIN_TOKEN> where ADMIN_TOKEN is
set as an env var. This is a deliberately simple admin auth — full user
accounts come in Step 14 with the frontend.

Endpoints:
  POST   /admin/keys           Create a new API key for a user
  GET    /admin/keys           List all keys (prefix only — full keys are never recoverable)
  DELETE /admin/keys/{prefix}  Revoke a key by prefix
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
        raise HTTPException(status_code=503,
                            detail="Admin endpoints disabled: ADMIN_TOKEN not configured.")

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
    key: str                  # Full key, shown ONCE
    key_prefix: str
    user_email: str
    tier: str
    label: str | None
    notice: str = (
        "Store this key now. It will not be shown again. "
        "If lost, revoke it and create a new one."
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
                raise HTTPException(status_code=404,
                                    detail="Key not found or already revoked.")
        return {"revoked": True, "key_prefix": row["key_prefix"],
                "user_email": row["user_email"]}
