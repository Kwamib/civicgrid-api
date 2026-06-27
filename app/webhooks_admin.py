"""Admin endpoints for managing webhook subscriptions and inspecting deliveries.

All endpoints require the admin token (reuses require_admin from app.admin),
mirroring the /admin/keys management surface.

Endpoints:
  POST   /admin/webhooks                              Create a subscription (returns signing secret ONCE)
  GET    /admin/webhooks                              List subscriptions (never returns secrets)
  GET    /admin/webhooks/{id}                         Subscription detail (no secret)
  PATCH  /admin/webhooks/{id}                         Update target_url / events / is_active / label
  DELETE /admin/webhooks/{id}                         Delete a subscription (cascades deliveries)
  POST   /admin/webhooks/{id}/rotate-secret           Issue a new signing secret (returned ONCE)
  GET    /admin/webhooks/deliveries                   Inspect deliveries (filter by status/subscription)
  POST   /admin/webhooks/deliveries/{id}/redeliver    Re-queue a delivery (typically a dead one)
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from app.admin import require_admin

# The event types a subscription may listen for. Kept in sync with the
# producer (app/webhook_events.py) and the design doc (§5).
KNOWN_EVENTS = {"leader.rotated", "leader.updated"}

# Delivery lifecycle statuses, used to validate the inspection filter.
KNOWN_DELIVERY_STATUSES = {"pending", "in_flight", "delivered", "dead"}

SECRET_PREFIX = "whsec_"


def _generate_secret() -> str:
    """Generate a webhook signing secret.

    Unlike API keys (which are stored hashed), this secret is stored retrievable
    because it is needed to compute the HMAC signature at delivery time. It is
    shown to the owner once at creation / rotation.
    """
    return SECRET_PREFIX + secrets.token_urlsafe(32)


def _validate_events(events: list[str]) -> list[str]:
    """Normalize + validate a list of event types against KNOWN_EVENTS."""
    if not events:
        raise HTTPException(status_code=422, detail="At least one event type is required.")
    deduped = sorted(set(events))
    unknown = [e for e in deduped if e not in KNOWN_EVENTS]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown event type(s): {', '.join(unknown)}. "
            f"Valid: {', '.join(sorted(KNOWN_EVENTS))}.",
        )
    return deduped


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CreateSubscriptionRequest(BaseModel):
    target_url: str = Field(min_length=1, max_length=2000)
    events: list[str] = Field(min_length=1)
    label: str | None = Field(default=None, max_length=100)

    @field_validator("target_url")
    @classmethod
    def _https_only(cls, v: str) -> str:
        v = v.strip()
        if not (v.startswith("https://") or v.startswith("http://")):
            raise ValueError("target_url must start with http:// or https://")
        return v


class PatchSubscriptionRequest(BaseModel):
    """Partial update. Only fields present in the body are changed."""

    target_url: str | None = Field(default=None, min_length=1, max_length=2000)
    events: list[str] | None = Field(default=None)
    is_active: bool | None = None
    label: str | None = Field(default=None, max_length=100)

    @field_validator("target_url")
    @classmethod
    def _https_only(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.strip()
        if not (v.startswith("https://") or v.startswith("http://")):
            raise ValueError("target_url must start with http:// or https://")
        return v


# Public (non-secret) columns returned by list/detail/patch.
_PUBLIC_COLS = "id, target_url, events, is_active, label, created_at, updated_at"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def attach_webhook_admin_routes(app, get_cursor):
    """Register webhook subscription admin routes. Called from main.py."""

    @app.post("/admin/webhooks", status_code=201, tags=["admin"])
    def create_subscription(
        req: CreateSubscriptionRequest,
        authorization: str | None = Header(None),
    ):
        """Create a webhook subscription. The signing secret is returned ONCE."""
        require_admin(authorization)
        events = _validate_events(req.events)
        secret = _generate_secret()

        with get_cursor() as cur:
            cur.execute(
                """
                insert into webhook_subscriptions (target_url, secret, events, label)
                values (%s, %s, %s, %s)
                returning id, target_url, events, is_active, label, created_at, updated_at
                """,
                (req.target_url, secret, events, req.label),
            )
            row = cur.fetchone()

        return {
            "subscription": row,
            "secret": secret,
            "notice": (
                "Store this signing secret now. It will not be shown again. "
                "If lost, rotate it via POST /admin/webhooks/{id}/rotate-secret."
            ),
        }

    @app.get("/admin/webhooks", tags=["admin"])
    def list_subscriptions(authorization: str | None = Header(None)):
        require_admin(authorization)
        with get_cursor() as cur:
            cur.execute(
                f"select {_PUBLIC_COLS} from webhook_subscriptions order by created_at desc"
            )
            rows = cur.fetchall()
        return {"data": rows, "count": len(rows)}

    # NOTE: declared before /admin/webhooks/{sub_id} so the literal "deliveries"
    # segment is matched first. (sub_id is typed int, so "deliveries" wouldn't
    # bind to it anyway, but explicit ordering keeps intent clear.)
    @app.get("/admin/webhooks/deliveries", tags=["admin"])
    def list_deliveries(
        authorization: str | None = Header(None),
        status: str | None = None,
        subscription_id: int | None = None,
        limit: int = 100,
    ):
        """Inspect deliveries, optionally filtered by status and/or subscription.

        Primary use is surfacing dead-letter rows: ?status=dead.
        """
        require_admin(authorization)
        if status is not None and status not in KNOWN_DELIVERY_STATUSES:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown status '{status}'. "
                f"Valid: {', '.join(sorted(KNOWN_DELIVERY_STATUSES))}.",
            )
        limit = max(1, min(limit, 500))

        clauses = []
        params: list = []
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        if subscription_id is not None:
            clauses.append("subscription_id = %s")
            params.append(subscription_id)
        where = ("where " + " and ".join(clauses)) if clauses else ""
        params.append(limit)

        with get_cursor() as cur:
            cur.execute(
                f"""
                select id, event_id, subscription_id, status, attempts,
                       next_attempt_at, last_status_code, last_error,
                       last_attempt_at, delivered_at, created_at
                from webhook_deliveries
                {where}
                order by id desc
                limit %s
                """,
                params,
            )
            rows = cur.fetchall()
        return {"data": rows, "count": len(rows)}

    @app.post("/admin/webhooks/deliveries/{delivery_id}/redeliver", tags=["admin"])
    def redeliver(delivery_id: int, authorization: str | None = Header(None)):
        """Re-queue a delivery (typically a dead one) for the next drain run."""
        require_admin(authorization)
        with get_cursor() as cur:
            cur.execute(
                """
                update webhook_deliveries
                   set status='pending', attempts=0, next_attempt_at=now(),
                       last_error=null, last_status_code=null
                 where id=%s
                returning id, status, subscription_id, event_id
                """,
                (delivery_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Delivery {delivery_id} not found.")
        return {"requeued": True, "delivery": row}

    @app.get("/admin/webhooks/{sub_id}", tags=["admin"])
    def get_subscription(sub_id: int, authorization: str | None = Header(None)):
        require_admin(authorization)
        with get_cursor() as cur:
            cur.execute(
                f"select {_PUBLIC_COLS} from webhook_subscriptions where id = %s",
                (sub_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Subscription {sub_id} not found.")
        return row

    @app.patch("/admin/webhooks/{sub_id}", tags=["admin"])
    def patch_subscription(
        sub_id: int,
        req: PatchSubscriptionRequest,
        authorization: str | None = Header(None),
    ):
        """Partial update of a subscription. Only fields present are changed."""
        require_admin(authorization)

        updates = req.model_dump(exclude_unset=True)
        if not updates:
            raise HTTPException(status_code=422, detail="No fields provided to update.")

        if "events" in updates:
            updates["events"] = _validate_events(updates["events"])

        allowed = {"target_url", "events", "is_active", "label"}
        cols = [c for c in updates if c in allowed]
        if not cols:
            raise HTTPException(status_code=422, detail="No updatable fields provided.")

        set_clauses = [f"{c} = %s" for c in cols]
        set_clauses.append("updated_at = now()")
        values = [updates[c] for c in cols]
        values.append(sub_id)

        sql = f"""
            update webhook_subscriptions
            set {", ".join(set_clauses)}
            where id = %s
            returning {_PUBLIC_COLS}
        """
        with get_cursor() as cur:
            cur.execute(sql, values)
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Subscription {sub_id} not found.")
        return {"updated_fields": cols, "subscription": row}

    @app.delete("/admin/webhooks/{sub_id}", tags=["admin"])
    def delete_subscription(sub_id: int, authorization: str | None = Header(None)):
        """Delete a subscription. Its delivery rows cascade (FK on delete cascade)."""
        require_admin(authorization)
        with get_cursor() as cur:
            cur.execute(
                "delete from webhook_subscriptions where id = %s returning id",
                (sub_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Subscription {sub_id} not found.")
        return {"deleted": True, "id": row["id"]}

    @app.post("/admin/webhooks/{sub_id}/rotate-secret", tags=["admin"])
    def rotate_secret(sub_id: int, authorization: str | None = Header(None)):
        """Issue a new signing secret for a subscription. Returned ONCE."""
        require_admin(authorization)
        secret = _generate_secret()
        with get_cursor() as cur:
            cur.execute(
                """
                update webhook_subscriptions
                set secret = %s, updated_at = now()
                where id = %s
                returning id
                """,
                (secret, sub_id),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Subscription {sub_id} not found.")
        return {
            "id": row["id"],
            "secret": secret,
            "notice": "Store this signing secret now. It will not be shown again.",
        }
