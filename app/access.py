"""Tier-based access rules for dataset reads.

The full dataset in one call (/cities/all, /leaders/export) is a paid feature.
Free keys can still look things up, but list pages are capped, so a free key
can't pull the whole dataset in a handful of requests.

Both rules are configurable so pricing can change without a code change:
  BULK_EXPORT_TIERS  comma-separated tiers allowed to bulk export (default "starter,pro")
  FREE_PAGE_LIMIT    max rows per page for the free tier (default 25)
"""

from __future__ import annotations

import os

from fastapi import HTTPException, Request


def bulk_export_tiers() -> set[str]:
    raw = os.environ.get("BULK_EXPORT_TIERS", "starter,pro")
    return {t.strip().lower() for t in raw.split(",") if t.strip()}


def free_page_limit() -> int:
    try:
        return max(1, int(os.environ.get("FREE_PAGE_LIMIT", "25")))
    except ValueError:
        return 25


def request_tier(request: Request) -> str | None:
    auth = getattr(request.state, "auth", None)
    return getattr(auth, "tier", None)


def require_bulk_export(request: Request) -> None:
    """403 unless the caller's tier includes full-dataset export."""
    tier = (request_tier(request) or "").lower()
    if tier not in bulk_export_tiers():
        raise HTTPException(
            status_code=403,
            detail={
                "error": "upgrade_required",
                "message": "Full dataset export is available on paid tiers. On the free "
                "tier, use /cities or /leaders/current with pagination.",
                "tier": tier or None,
            },
        )


def clamp_page_limit(request: Request, limit: int) -> int:
    """Cap page size for the free tier; paid tiers keep the endpoint's own maximum."""
    if (request_tier(request) or "").lower() == "free":
        return min(limit, free_page_limit())
    return limit
