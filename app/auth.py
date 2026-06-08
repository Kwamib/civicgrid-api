"""API key authentication for CivicGrid.

API keys are generated as cg_live_<32 url-safe chars>. The full key is only
shown to the user at creation time. The DB stores a bcrypt hash and a prefix
(first 16 chars) for identification.

Auth flow on each request:
  1. Read Authorization: Bearer <key> header
  2. Verify prefix matches a non-revoked DB row
  3. bcrypt.checkpw the full key against the stored hash
  4. Attach user context (tier, email, key_id) to request.state
  5. Update last_used_at and increment request_count (best-effort, non-blocking)

Public routes bypass auth via the PUBLIC_PATHS set.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

import bcrypt
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

# Routes that do not require authentication.
PUBLIC_PATHS: set[str] = {
    "/",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/metrics",
}

# Routes that require admin auth (separate from API key auth).
ADMIN_PATH_PREFIX = "/admin"
ME_PATH_PREFIX = "/me"

KEY_PREFIX = "cg_live_"
KEY_RANDOM_LENGTH = 32  # url-safe chars after the prefix
PREFIX_LENGTH = 16  # first N chars stored for lookup (cg_live_ + 8 chars)


@dataclass
class AuthContext:
    """Authentication info attached to request.state.auth on success."""

    key_id: int
    key_prefix: str
    user_email: str
    tier: str


# ---------------------------------------------------------------------------
# Key generation & verification
# ---------------------------------------------------------------------------


def generate_key() -> tuple[str, str, str]:
    """Generate a new API key.

    Returns:
        (full_key, prefix, bcrypt_hash)
        - full_key: shown to the user once, never stored
        - prefix: first 16 chars, stored in DB for lookup
        - bcrypt_hash: stored in DB for verification
    """
    random_part = secrets.token_urlsafe(KEY_RANDOM_LENGTH)[:KEY_RANDOM_LENGTH]
    full_key = f"{KEY_PREFIX}{random_part}"
    prefix = full_key[:PREFIX_LENGTH]
    hashed = bcrypt.hashpw(full_key.encode("utf-8"), bcrypt.gensalt(rounds=10))
    return full_key, prefix, hashed.decode("utf-8")


def verify_key(full_key: str, stored_hash: str) -> bool:
    """Constant-time bcrypt verification of a key against its stored hash."""
    try:
        return bcrypt.checkpw(full_key.encode("utf-8"), stored_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def extract_prefix(full_key: str) -> str | None:
    """Extract the lookup prefix from a key. Returns None if malformed."""
    if not full_key or not full_key.startswith(KEY_PREFIX):
        return None
    if len(full_key) < PREFIX_LENGTH:
        return None
    return full_key[:PREFIX_LENGTH]


# ---------------------------------------------------------------------------
# DB lookups (uses the same connection pool as the rest of the app)
# ---------------------------------------------------------------------------


def lookup_key_by_prefix(get_cursor, prefix: str) -> dict | None:
    """Find an active (non-revoked) API key by its prefix.

    Args:
        get_cursor: the get_cursor() context manager from main.py
        prefix: 16-char prefix to look up
    Returns:
        Row dict, or None if not found / revoked.
    """
    with get_cursor() as cur:
        cur.execute(
            """
            select id, key_hash, key_prefix, user_email, tier
            from api_keys
            where key_prefix = %s and revoked_at is null
            limit 1
            """,
            (prefix,),
        )
        return cur.fetchone()


def touch_key(get_cursor, key_id: int) -> None:
    """Update last_used_at and increment request_count. Best-effort."""
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                update api_keys
                set last_used_at = now(),
                    request_count = request_count + 1
                where id = %s
                """,
                (key_id,),
            )
    except Exception:
        # Don't fail the request if usage tracking has a hiccup.
        pass


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class AuthMiddleware(BaseHTTPMiddleware):
    """Verifies API key on protected routes.

    Public routes (PUBLIC_PATHS) and admin routes (ADMIN_PATH_PREFIX) bypass
    this middleware. Admin auth is handled separately in admin.py.
    """

    def __init__(self, app, get_cursor):
        super().__init__(app)
        self.get_cursor = get_cursor

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Bypass for public routes and admin routes (admin has its own auth).
        if path in PUBLIC_PATHS or path.startswith(ADMIN_PATH_PREFIX) or path.startswith(ME_PATH_PREFIX):
            return await call_next(request)

        # Extract bearer token.
        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return _auth_error(
                401,
                "missing_api_key",
                "Authorization header required. Use: Authorization: Bearer cg_live_...",
            )

        full_key = auth_header[7:].strip()
        prefix = extract_prefix(full_key)
        if not prefix:
            return _auth_error(
                401, "invalid_key_format", "Malformed API key. Keys start with 'cg_live_'."
            )

        # Look up by prefix.
        row = lookup_key_by_prefix(self.get_cursor, prefix)
        if not row:
            return _auth_error(401, "invalid_key", "API key not found or revoked.")

        # bcrypt verify the full key.
        if not verify_key(full_key, row["key_hash"]):
            return _auth_error(401, "invalid_key", "API key not found or revoked.")

        # Attach context for downstream middleware / handlers.
        request.state.auth = AuthContext(
            key_id=row["id"],
            key_prefix=row["key_prefix"],
            user_email=row["user_email"],
            tier=row["tier"],
        )

        # Update usage tracking (non-blocking on failure).
        touch_key(self.get_cursor, row["id"])

        return await call_next(request)


def _auth_error(status: int, code: str, message: str) -> Response:
    """JSON error response for auth failures."""
    import json

    body = json.dumps({"error": code, "message": message})
    return Response(
        content=body,
        status_code=status,
        media_type="application/json",
        headers={"WWW-Authenticate": "Bearer"},
    )
