"""API key authentication for CivicGrid.

API keys are generated as cg_live_<32 url-safe chars>. The full key is only
shown to the user at creation time. The DB stores a bcrypt hash and a prefix
(first 16 chars) for identification.

Auth flow on each request:
  1. Read Authorization: Bearer <key> header
  2. Verify prefix matches a non-revoked DB row
  3. bcrypt.checkpw the full key against the stored hash
  4. Attach user context (tier, email, key_id) to request.state
  5. Update last_used_at and increment request_count (best-effort, batched)

Steps 2 and 3 are skipped for a key verified in the last KEY_CACHE_SECONDS.

Public routes bypass auth via the PUBLIC_PATHS set.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
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


# ---------------------------------------------------------------------------
# Verified-key cache
# ---------------------------------------------------------------------------
# bcrypt is deliberately slow (~0.1 s of CPU). Checking it on every request
# made each API call cost a tenth of a CPU second, enough for one looping
# client to push the API to its pod limit (Oct 10, 2026). After a key passes
# bcrypt once, remember a SHA-256 of it for a short time and skip the database
# lookup and bcrypt on repeat requests. A revoked or re-tiered key takes effect
# within KEY_CACHE_SECONDS.

KEY_CACHE_SECONDS = int(os.environ.get("KEY_CACHE_SECONDS", "60"))
KEY_CACHE_MAX = 5000

_key_cache: dict[str, tuple[bytes, dict, float]] = {}
_key_cache_lock = threading.Lock()


def _digest(full_key: str) -> bytes:
    return hashlib.sha256(full_key.encode("utf-8")).digest()


def cached_key_row(full_key: str, prefix: str) -> dict | None:
    """Row for a recently verified key, or None if not cached / expired."""
    if KEY_CACHE_SECONDS <= 0:
        return None
    with _key_cache_lock:
        hit = _key_cache.get(prefix)
        if not hit:
            return None
        digest, row, expires = hit
        if time.monotonic() >= expires:
            _key_cache.pop(prefix, None)
            return None
    if not hmac.compare_digest(digest, _digest(full_key)):
        return None
    return row


def remember_key(full_key: str, row: dict) -> None:
    if KEY_CACHE_SECONDS <= 0:
        return
    with _key_cache_lock:
        if len(_key_cache) >= KEY_CACHE_MAX:
            _key_cache.clear()
        _key_cache[row["key_prefix"]] = (
            _digest(full_key),
            row,
            time.monotonic() + KEY_CACHE_SECONDS,
        )


def forget_key(prefix: str) -> None:
    """Drop one key from this pod's cache (call after revoking or re-tiering)."""
    with _key_cache_lock:
        _key_cache.pop(prefix, None)


def clear_key_cache() -> None:
    with _key_cache_lock:
        _key_cache.clear()


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


# Usage counts are written at most once per TOUCH_EVERY_SECONDS per key (with
# the requests counted in between), not on every request.
TOUCH_EVERY_SECONDS = 10

_pending_touch: dict[int, tuple[int, float]] = {}
_touch_lock = threading.Lock()


def touch_key(get_cursor, key_id: int) -> None:
    """Update last_used_at and increment request_count. Best-effort, batched."""
    now = time.monotonic()
    with _touch_lock:
        count, last_flush = _pending_touch.get(key_id, (0, 0.0))
        count += 1
        if now - last_flush < TOUCH_EVERY_SECONDS:
            _pending_touch[key_id] = (count, last_flush)
            return
        _pending_touch[key_id] = (0, now)
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                update api_keys
                set last_used_at = now(),
                    request_count = request_count + %s
                where id = %s
                """,
                (count, key_id),
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
        if (
            path in PUBLIC_PATHS
            or path.startswith(ADMIN_PATH_PREFIX)
            or path.startswith(ME_PATH_PREFIX)
        ):
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

        row = cached_key_row(full_key, prefix)
        if row is None:
            # Look up by prefix, then bcrypt verify the full key.
            row = lookup_key_by_prefix(self.get_cursor, prefix)
            if not row or not verify_key(full_key, row["key_hash"]):
                return _auth_error(401, "invalid_key", "API key not found or revoked.")
            remember_key(full_key, row)

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
