"""Webhook delivery drain (PR 4 of the webhook system).

A standalone command that delivers queued webhook events. Run it with:

    python -m app.webhooks.drain

It does not depend on the FastAPI app: it opens its own DB connection from
DATABASE_URL, so the CronJob (PR 5) can run it without the web server. Each run
does three phases (see docs/design/webhooks.md §4):

  1. Reclaim  - reset deliveries stuck in_flight from a crashed prior run.
  2. Fan-out  - turn each new event into one delivery row per matching active
                subscription, then mark the event fanned_out.
  3. Deliver  - claim due deliveries, sign + POST them, classify the response,
                and either mark delivered, schedule a backoff retry, or dead.

Delivery is at-least-once: a consumer may receive the same event_id twice and
must dedupe on it. HTTP calls happen OUTSIDE any open DB transaction (claim is
committed first), so a slow consumer never holds a row lock.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import random
import time
import urllib.error
import urllib.request
from urllib.parse import unquote, urlparse

import psycopg2
from psycopg2.extras import RealDictCursor

log = logging.getLogger("webhooks.drain")

# --- tunables (mirror the design doc §6) ---------------------------------
MAX_ATTEMPTS = 7
# Delay (seconds) BEFORE attempts 2..7. Index 0 = delay before attempt 2.
BACKOFF_SECONDS = [60, 300, 900, 3600, 21600, 86400]  # 1m, 5m, 15m, 1h, 6h, 24h
JITTER = 0.20  # +/- 20% randomization on each backoff
REQUEST_TIMEOUT = 10  # seconds per delivery HTTP call
RECLAIM_AFTER = "5 minutes"  # in_flight rows older than this are reclaimed
FANOUT_BATCH = 500  # events fanned out per run
DELIVER_BATCH = 200  # deliveries attempted per run

RETRYABLE_STATUS = {408, 429}
USER_AGENT = "CivicGrid-Webhooks/1"


# --- connection ----------------------------------------------------------
def _connect():
    """Open a standalone DB connection from DATABASE_URL (no FastAPI)."""
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise SystemExit("DATABASE_URL not set")
    p = urlparse(db_url)
    return psycopg2.connect(
        host=p.hostname,
        port=p.port or 5432,
        user=unquote(p.username) if p.username else None,
        password=unquote(p.password) if p.password else None,
        dbname=p.path.lstrip("/") or "postgres",
        cursor_factory=RealDictCursor,
    )


# --- phase 1: reclaim ----------------------------------------------------
def reclaim_stuck(conn) -> int:
    """Reset deliveries left in_flight by a crashed prior run back to pending."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            update webhook_deliveries
               set status = 'pending'
             where status = 'in_flight'
               and claimed_at < now() - interval '{RECLAIM_AFTER}'
            returning id
            """
        )
        n = len(cur.fetchall())
    conn.commit()
    return n


# --- phase 2: fan-out ----------------------------------------------------
def fan_out(conn) -> dict:
    """Create delivery rows for new events, one per matching active subscription."""
    with conn.cursor() as cur:
        cur.execute(
            """
            select id, event_type
              from webhook_events
             where fanned_out = false
             order by created_at
             limit %s
            """,
            (FANOUT_BATCH,),
        )
        events = cur.fetchall()

        created = 0
        for ev in events:
            # One delivery per active subscription that wants this event type.
            # on conflict makes re-running a fan-out a no-op (idempotent).
            cur.execute(
                """
                insert into webhook_deliveries (event_id, subscription_id)
                select %s, s.id
                  from webhook_subscriptions s
                 where s.is_active = true
                   and %s = any(s.events)
                on conflict (event_id, subscription_id) do nothing
                """,
                (ev["id"], ev["event_type"]),
            )
            created += cur.rowcount
            cur.execute(
                "update webhook_events set fanned_out = true where id = %s",
                (ev["id"],),
            )
    conn.commit()
    return {"events": len(events), "deliveries_created": created}


# --- phase 3: deliver ----------------------------------------------------
def _sign(secret: str, timestamp: str, body: str) -> str:
    """HMAC-SHA256 over '{timestamp}.{body}', Stripe-style."""
    msg = f"{timestamp}.{body}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def _post(url: str, data: bytes, headers: dict, timeout: int):
    """POST data to url. Returns (status_code | None, network_error | None)."""
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, None
    except urllib.error.HTTPError as e:
        # The server responded, just with a 4xx/5xx.
        return e.code, None
    except urllib.error.URLError as e:
        return None, f"network: {e.reason}"
    except TimeoutError:
        return None, "timeout"
    except Exception as e:  # noqa: BLE001 - any transport failure is a network error
        return None, f"error: {e}"


def _classify(status_code, net_err) -> str:
    """Map a delivery outcome to: delivered | retry | dead | dead_gone."""
    if net_err is not None:
        return "retry"
    if 200 <= status_code < 300:
        return "delivered"
    if status_code in RETRYABLE_STATUS or 500 <= status_code < 600:
        return "retry"
    if status_code == 410:
        return "dead_gone"  # consumer asked to stop; deactivate the subscription
    return "dead"  # other 4xx: retrying a rejected request won't help


def _next_delay(attempts_done: int) -> float:
    """Backoff delay (seconds) after `attempts_done` attempts, with jitter."""
    idx = min(attempts_done - 1, len(BACKOFF_SECONDS) - 1)
    base = BACKOFF_SECONDS[idx]
    return base + random.uniform(-base * JITTER, base * JITTER)


def _process_one(conn, row, counts) -> None:
    """Sign, POST, and record the result for a single claimed delivery."""
    body = json.dumps(row["payload"])
    timestamp = str(int(time.time()))
    signature = _sign(row["secret"], timestamp, body)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-CivicGrid-Event": row["event_type"],
        "X-CivicGrid-Event-Id": str(row["event_id"]),
        "X-CivicGrid-Delivery": str(row["id"]),
        "X-CivicGrid-Timestamp": timestamp,
        "X-CivicGrid-Signature": f"sha256={signature}",
    }

    status_code, net_err = _post(row["target_url"], body.encode(), headers, REQUEST_TIMEOUT)
    attempts_new = row["attempts"] + 1
    outcome = _classify(status_code, net_err)
    err_text = net_err or (f"HTTP {status_code}" if status_code else None)

    with conn.cursor() as cur:
        if outcome == "delivered":
            cur.execute(
                """
                update webhook_deliveries
                   set status='delivered', delivered_at=now(), attempts=%s,
                       last_status_code=%s, last_error=null, last_attempt_at=now()
                 where id=%s
                """,
                (attempts_new, status_code, row["id"]),
            )
            counts["delivered"] += 1
        elif outcome == "retry" and attempts_new < MAX_ATTEMPTS:
            delay = _next_delay(attempts_new)
            cur.execute(
                """
                update webhook_deliveries
                   set status='pending', attempts=%s,
                       next_attempt_at = now() + make_interval(secs => %s),
                       last_status_code=%s, last_error=%s, last_attempt_at=now()
                 where id=%s
                """,
                (attempts_new, delay, status_code, err_text, row["id"]),
            )
            counts["retried"] += 1
        else:
            # permanent failure, or retries exhausted -> dead-letter
            cur.execute(
                """
                update webhook_deliveries
                   set status='dead', attempts=%s,
                       last_status_code=%s, last_error=%s, last_attempt_at=now()
                 where id=%s
                """,
                (attempts_new, status_code, err_text, row["id"]),
            )
            counts["dead"] += 1
            if outcome == "dead_gone":
                cur.execute(
                    "update webhook_subscriptions set is_active=false, updated_at=now() where id=%s",
                    (row["subscription_id"],),
                )
    conn.commit()

    log.info(
        "delivery id=%s sub=%s event=%s attempt=%s outcome=%s status=%s",
        row["id"],
        row["subscription_id"],
        row["event_type"],
        attempts_new,
        outcome,
        status_code if status_code else err_text,
    )


def deliver_due(conn) -> dict:
    """Claim due deliveries (committing the claim), then POST each one."""
    with conn.cursor() as cur:
        # Atomic claim: flip to in_flight so no other run picks the same rows.
        # FOR UPDATE SKIP LOCKED + CronJob concurrencyPolicy Forbid = belt and braces.
        cur.execute(
            """
            update webhook_deliveries
               set status='in_flight', claimed_at=now()
             where id in (
                select id from webhook_deliveries
                 where status='pending' and next_attempt_at <= now()
                 order by next_attempt_at
                 limit %s
                 for update skip locked
             )
            returning id
            """,
            (DELIVER_BATCH,),
        )
        claimed_ids = [r["id"] for r in cur.fetchall()]
    conn.commit()  # commit the claim BEFORE any HTTP call

    counts = {"delivered": 0, "retried": 0, "dead": 0, "claimed": len(claimed_ids)}
    if not claimed_ids:
        return counts

    # Pull everything needed to sign + send for the claimed rows.
    with conn.cursor() as cur:
        cur.execute(
            """
            select d.id, d.attempts, d.event_id, d.subscription_id,
                   e.event_type, e.payload,
                   s.target_url, s.secret
              from webhook_deliveries d
              join webhook_events e on e.id = d.event_id
              join webhook_subscriptions s on s.id = d.subscription_id
             where d.id = any(%s)
            """,
            (claimed_ids,),
        )
        rows = cur.fetchall()

    for row in rows:
        _process_one(conn, row, counts)
    return counts


# --- entrypoint ----------------------------------------------------------
def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    conn = _connect()
    try:
        reclaimed = reclaim_stuck(conn)
        fan = fan_out(conn)
        counts = deliver_due(conn)
        log.info(
            "drain complete: reclaimed=%d fanned_events=%d deliveries_created=%d "
            "claimed=%d delivered=%d retried=%d dead=%d",
            reclaimed,
            fan["events"],
            fan["deliveries_created"],
            counts["claimed"],
            counts["delivered"],
            counts["retried"],
            counts["dead"],
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
