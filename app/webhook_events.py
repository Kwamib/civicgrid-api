"""Webhook event production (the outbox write).

emit_event() inserts one row into the webhook_events outbox using the cursor
the caller is ALREADY using, so the event lands in the same transaction as the
data change. Because get_cursor() commits on clean exit and rolls back on any
exception, the data change and its event either both commit or neither does.
This atomicity is the whole point of the transactional outbox: there is no
window where the data changed but the event was lost (or vice versa).

This is PR 3 of the webhook system (see docs/design/webhooks.md). Delivery of
these events happens later, in the drain command (PR 4).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

from psycopg2.extras import Json

# Event types this producer can emit. Kept in sync with the admin API's
# KNOWN_EVENTS (app/webhooks_admin.py) and the design doc (§5).
EVENT_LEADER_ROTATED = "leader.rotated"
EVENT_LEADER_UPDATED = "leader.updated"


def _json_default(value):
    """JSON fallback for types psycopg2 rows carry that json can't encode.

    Leader/city rows from RealDictCursor may contain datetimes (created_at,
    updated_at) and Decimals (numeric columns). Stringify them so the payload
    serializes into jsonb cleanly.
    """
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def emit_event(cur, event_type: str, payload: dict) -> str:
    """Insert one event into the outbox using the caller's open cursor.

    Args:
        cur: an open cursor inside the caller's get_cursor() transaction.
        event_type: e.g. EVENT_LEADER_ROTATED.
        payload: the event body (will be stored as jsonb).

    Returns the generated event_id (uuid) as a string.

    Note: this does NOT manage its own transaction. It rides the caller's, so
    the event commits atomically with whatever data change the caller made.
    """
    cur.execute(
        """
        insert into webhook_events (event_type, payload)
        values (%s, %s)
        returning id
        """,
        (event_type, Json(payload, dumps=lambda d: json.dumps(d, default=_json_default))),
    )
    return str(cur.fetchone()["id"])
