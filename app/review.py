"""Leadership review workflow: the restricted queue for verifier findings.

The verifier writes findings to leader_proposals (status='pending'). A finding
is NOT a confirmed change of mayor. Nothing here publishes anything until a
reviewer acts, and every action is recorded in review_events (append-only).

Endpoints (all require Authorization: Bearer <ADMIN_TOKEN>):
  GET  /admin/proposals                     findings + the current published record
  POST /admin/proposals/{id}/approve        Accept: publish the proposed name
  POST /admin/proposals/{id}/correct        Correct: publish a reviewer-edited name/title
  POST /admin/proposals/{id}/reject         Dismiss: keep the published record
  POST /admin/proposals/{id}/retry          Retry: re-check the source later, change nothing
  GET  /admin/review-events                 the audit log

Concurrency: every write locks the proposal and the city's current leader row
(SELECT ... FOR UPDATE) and then checks that the record the reviewer saw is still
the published one:
  * expected_leader_id (sent by the UI) must equal the current leader's id, and
  * the finding must have been made against the current record (db_leader_id, or
    for older rows a name comparison of db_mayor). A stale finding is refused
    with 409 unless the reviewer passes acknowledge_stale=true after seeing the
    current record.
So two reviewers, or a reviewer racing the Fix-a-city tool, cannot silently
overwrite a newer record.

approve and reject still work with no request body, for backward
compatibility. The actor is then recorded as "admin-token" and no concurrency
token is checked beyond the finding's own staleness.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated

from fastapi import Body, Header, HTTPException
from pydantic import BaseModel, Field

from app.admin import require_admin
from app.webhook_events import EVENT_LEADER_ROTATED, EVENT_LEADER_UPDATED, emit_event

REVIEWABLE_STATUSES = {"pending", "retry", "approved", "corrected", "rejected"}
NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested without a database)
# ---------------------------------------------------------------------------


def norm_name(value: str | None) -> str:
    """Comparison key for a person's name. Never stored, only compared.

    Case-, accent-, punctuation- and whitespace-insensitive, so
    "L'Heureux, Gary" style noise doesn't read as a different record, while any
    real difference in the letters still does.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\s]", "", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def derive_last_name(full_name: str) -> str:
    """Last name for leaders.last_name (NOT NULL). Skips Jr/Sr/III suffixes."""
    parts = full_name.replace(",", " ").split()
    if not parts:
        return full_name.strip()
    last = parts[-1]
    if len(parts) > 1 and last.lower().rstrip(".") in NAME_SUFFIXES:
        last = parts[-2]
    return last


def is_stale(proposal: dict, current: dict | None) -> bool:
    """True when the finding was made against a record that is no longer current."""
    current_id = current["id"] if current else None
    if proposal.get("db_leader_id") is not None:
        return proposal["db_leader_id"] != current_id
    current_name = current["full_name"] if current else None
    return norm_name(proposal.get("db_mayor")) != norm_name(current_name)


def already_published(proposal: dict, current: dict | None) -> bool:
    """True when the proposed name is already the current leader."""
    return bool(
        current
        and proposal.get("web_mayor")
        and norm_name(proposal["web_mayor"]) == norm_name(current["full_name"])
    )


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class ReviewBase(BaseModel):
    actor: str = Field(min_length=3, max_length=200, description="Reviewer identity (email).")
    reason: str | None = Field(default=None, max_length=2000)
    expected_leader_id: int | None = Field(
        default=None,
        description="Current leader id the reviewer saw. Mismatch -> 409.",
    )
    acknowledge_stale: bool = False


class AcceptRequest(ReviewBase):
    leader_title: str | None = Field(default=None, max_length=100)


class CorrectRequest(ReviewBase):
    full_name: str = Field(min_length=1, max_length=200)
    leader_title: str | None = Field(default=None, max_length=100)
    source_url: str | None = Field(default=None, max_length=500)
    reason: str = Field(min_length=3, max_length=2000)


class DismissRequest(ReviewBase):
    confirm_current: bool = Field(
        default=False,
        description="Reviewer checked the source and the CURRENT record is correct: "
        "stamp it verified.",
    )


class RetryRequest(ReviewBase):
    pass


# ---------------------------------------------------------------------------
# Transaction helpers (run inside the caller's get_cursor() transaction)
# ---------------------------------------------------------------------------


def _lock_for_review(cur, proposal_id: int) -> tuple[dict, dict | None]:
    cur.execute("select * from leader_proposals where id = %s for update", (proposal_id,))
    prop = cur.fetchone()
    if not prop:
        raise HTTPException(status_code=404, detail="Finding not found.")
    if prop["status"] not in ("pending", "retry"):
        raise HTTPException(status_code=409, detail=f"Finding already {prop['status']}.")
    cur.execute(
        """
        select id, full_name, leader_title, last_verified_at
        from leaders
        where city_id = %s and is_current = true
        order by id desc
        for update
        """,
        (prop["city_id"],),
    )
    rows = cur.fetchall()
    return prop, (rows[0] if rows else None)


def _check_fresh(prop: dict, current: dict | None, req: ReviewBase | None) -> None:
    current_id = current["id"] if current else None
    if req is not None and req.expected_leader_id is not None:
        if req.expected_leader_id != current_id:
            raise HTTPException(
                status_code=409,
                detail="The published record changed after you loaded this finding. "
                "Reload the queue and review it again.",
            )
    if is_stale(prop, current) and not (req is not None and req.acknowledge_stale):
        raise HTTPException(
            status_code=409,
            detail="This finding was made against an older record. Compare it with the "
            "current record, then retry the check or confirm you still want to act.",
        )


def _publish_leader(
    cur, city_id: int, full_name: str, title: str, source: str | None
) -> tuple[dict, list[dict]]:
    """Demote the current leader and insert the new one, verified now. History kept."""
    cur.execute(
        """
        update leaders set is_current = false, updated_at = now()
        where city_id = %s and is_current = true
        returning id, full_name, leader_title
        """,
        (city_id,),
    )
    demoted = [dict(r) for r in cur.fetchall()]
    cur.execute(
        """
        insert into leaders (
            city_id, full_name, last_name, leader_title, political_party,
            year_elected, next_election_year, tenure_years, term_length_years,
            is_current, created_at, updated_at, last_verified_at
        )
        values (%s, %s, %s, %s, null, null, null, null, null, true, now(), now(), now())
        returning id, city_id, full_name, last_name, leader_title,
                  political_party, is_current, created_at, updated_at, last_verified_at
        """,
        (city_id, full_name, derive_last_name(full_name), title),
    )
    new_leader = dict(cur.fetchone())
    _record_manual_verification(cur, city_id, full_name, source)
    return new_leader, demoted


def _record_manual_verification(cur, city_id: int, name: str, source: str | None) -> None:
    cur.execute(
        """
        insert into verification_results (city_id, verdict, web_mayor, source, model)
        values (%s, 'MANUAL', %s, %s, 'human-review')
        """,
        (city_id, name, source),
    )


def _audit(
    cur,
    *,
    prop: dict,
    action: str,
    actor: str,
    reason: str | None,
    before: dict | None,
    after: dict | None,
    source_url: str | None,
    confirmed_current: bool = False,
) -> int:
    cur.execute(
        """
        insert into review_events (
            proposal_id, city_id, action, actor, reason,
            before_leader_id, before_name, before_title,
            after_leader_id, after_name, after_title,
            confirmed_current, source_url, evidence
        )
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        returning id
        """,
        (
            prop["id"],
            prop["city_id"],
            action,
            actor,
            reason,
            before["id"] if before else None,
            before["full_name"] if before else None,
            before["leader_title"] if before else None,
            after["id"] if after else None,
            after["full_name"] if after else None,
            after["leader_title"] if after else None,
            confirmed_current,
            source_url,
            prop.get("evidence"),
        ),
    )
    return cur.fetchone()["id"]


def _close(
    cur, prop_id: int, status: str, actor: str, reason: str | None, applied: dict | None = None
) -> None:
    cur.execute(
        """
        update leader_proposals
        set status = %s, reviewed_at = now(), reviewed_by = %s, review_reason = %s,
            applied_name = %s, applied_title = %s
        where id = %s
        """,
        (
            status,
            actor,
            reason,
            applied["full_name"] if applied else None,
            applied["leader_title"] if applied else None,
            prop_id,
        ),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def attach_review_routes(app, get_cursor):
    @app.get("/admin/proposals", tags=["admin"])
    def list_proposals(
        authorization: str | None = Header(None),
        status: str = "pending",
        limit: int = 100,
        offset: int = 0,
    ):
        """Findings with the CURRENT published record beside each one.

        Each row carries `stale` (finding made against an older record) and
        `already_published` (the proposed name is already current), so the
        reviewer sees conflicts before acting. Biggest cities first.
        """
        require_admin(authorization)
        if status not in REVIEWABLE_STATUSES:
            raise HTTPException(status_code=422, detail=f"Unknown status '{status}'.")
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        with get_cursor() as cur:
            cur.execute(
                """
                select p.*,
                       c.governance_type, c.mayor_url, c.url as city_url,
                       l.id               as current_leader_id,
                       l.full_name        as current_name,
                       l.leader_title     as current_title,
                       l.last_verified_at as current_last_verified_at
                from leader_proposals p
                join cities c on c.id = p.city_id
                left join leaders l on l.city_id = p.city_id and l.is_current = true
                where p.status = %s
                order by p.population desc nulls last, p.city, p.id
                limit %s offset %s
                """,
                (status, limit, offset),
            )
            rows = [dict(r) for r in cur.fetchall()]
            cur.execute("select status, count(*) as n from leader_proposals group by status")
            counts = {r["status"]: r["n"] for r in cur.fetchall()}

        for r in rows:
            current = (
                {"id": r["current_leader_id"], "full_name": r["current_name"]}
                if r["current_leader_id"] is not None
                else None
            )
            r["stale"] = is_stale(r, current)
            r["already_published"] = already_published(r, current)

        return {
            "proposals": rows,
            "total": counts.get(status, 0),
            "counts": counts,
            "limit": limit,
            "offset": offset,
        }

    @app.post("/admin/proposals/{proposal_id}/approve", tags=["admin"])
    def accept_finding(
        proposal_id: int,
        authorization: str | None = Header(None),
        req: Annotated[AcceptRequest | None, Body()] = None,
    ):
        """Accept: publish the proposed name as the current leader.

        Title precedence: reviewer's leader_title > the title the verifier saw >
        the current leader's title > 'Mayor'. (Previously this was hardcoded to
        'Mayor', which mislabelled village presidents and select-board chairs.)
        """
        require_admin(authorization)
        actor = req.actor if req else "admin-token"
        with get_cursor() as cur:
            prop, current = _lock_for_review(cur, proposal_id)
            _check_fresh(prop, current, req)
            if already_published(prop, current):
                raise HTTPException(
                    status_code=409,
                    detail="The proposed leader is already the published record. "
                    "Dismiss the finding instead.",
                )
            full_name = (prop["web_mayor"] or "").strip()
            if not full_name:
                raise HTTPException(
                    status_code=422,
                    detail="This finding has no proposed name. Use Correct, Dismiss, or Retry.",
                )
            title = (
                (req.leader_title if req else None)
                or prop.get("proposed_title")
                or (current["leader_title"] if current else None)
                or "Mayor"
            ).strip()

            new_leader, demoted = _publish_leader(
                cur, prop["city_id"], full_name, title, prop["source_url"]
            )
            _close(cur, proposal_id, "approved", actor, req.reason if req else None, new_leader)
            event_id = _audit(
                cur,
                prop=prop,
                action="accept",
                actor=actor,
                reason=req.reason if req else None,
                before=current,
                after=new_leader,
                source_url=prop["source_url"],
            )
            emit_event(
                cur,
                EVENT_LEADER_ROTATED,
                {
                    "city": {
                        "id": prop["city_id"],
                        "city": prop["city"],
                        "state_code": prop["state_code"],
                    },
                    "leader": new_leader,
                    "demoted": demoted,
                    "source": "proposal_approval",
                    "proposal_id": proposal_id,
                    "review_event_id": event_id,
                },
            )

        return {
            "approved": True,
            "proposal_id": proposal_id,
            "city_id": prop["city_id"],
            "new_leader": new_leader,
            "demoted": demoted,
            "review_event_id": event_id,
        }

    @app.post("/admin/proposals/{proposal_id}/correct", tags=["admin"])
    def correct_finding(
        proposal_id: int,
        req: CorrectRequest,
        authorization: str | None = Header(None),
    ):
        """Correct: publish what the reviewer verified instead of the proposal.

        Requires a reason. If the corrected person is already the current
        leader, the row is updated in place (title, verified-now) instead of
        rotated, so history doesn't gain a fake transition.
        """
        require_admin(authorization)
        full_name = req.full_name.strip()
        if not full_name:
            raise HTTPException(status_code=422, detail="full_name cannot be blank.")
        with get_cursor() as cur:
            prop, current = _lock_for_review(cur, proposal_id)
            _check_fresh(prop, current, req)
            source = (req.source_url or "").strip() or prop["source_url"]
            title = (
                (req.leader_title or "").strip()
                or prop.get("proposed_title")
                or (current["leader_title"] if current else None)
                or "Mayor"
            )

            if current and norm_name(current["full_name"]) == norm_name(full_name):
                cur.execute(
                    """
                    update leaders
                    set leader_title = %s, last_verified_at = now(), updated_at = now()
                    where id = %s
                    returning id, city_id, full_name, last_name, leader_title,
                              political_party, is_current, created_at, updated_at,
                              last_verified_at
                    """,
                    (title, current["id"]),
                )
                new_leader = dict(cur.fetchone())
                _record_manual_verification(cur, prop["city_id"], new_leader["full_name"], source)
                demoted: list[dict] = []
                event_type = EVENT_LEADER_UPDATED
            else:
                new_leader, demoted = _publish_leader(
                    cur, prop["city_id"], full_name, title, source
                )
                event_type = EVENT_LEADER_ROTATED

            _close(cur, proposal_id, "corrected", req.actor, req.reason, new_leader)
            event_id = _audit(
                cur,
                prop=prop,
                action="correct",
                actor=req.actor,
                reason=req.reason,
                before=current,
                after=new_leader,
                source_url=source,
            )
            emit_event(
                cur,
                event_type,
                {
                    "city": {
                        "id": prop["city_id"],
                        "city": prop["city"],
                        "state_code": prop["state_code"],
                    },
                    "leader": new_leader,
                    "demoted": demoted,
                    "source": "proposal_correction",
                    "proposal_id": proposal_id,
                    "review_event_id": event_id,
                },
            )

        return {
            "corrected": True,
            "proposal_id": proposal_id,
            "city_id": prop["city_id"],
            "new_leader": new_leader,
            "demoted": demoted,
            "review_event_id": event_id,
        }

    @app.post("/admin/proposals/{proposal_id}/reject", tags=["admin"])
    def dismiss_finding(
        proposal_id: int,
        authorization: str | None = Header(None),
        req: Annotated[DismissRequest | None, Body()] = None,
    ):
        """Dismiss: keep the published record exactly as it is.

        With confirm_current=true the reviewer also attests that the current
        record matches the source (e.g. Wausau, where the verifier was wrong
        and the DB was right), so it is stamped verified. Without it, a
        dismissal says nothing about the current record's accuracy.
        """
        require_admin(authorization)
        actor = req.actor if req else "admin-token"
        reason = req.reason if req else None
        confirm = bool(req and req.confirm_current)
        with get_cursor() as cur:
            prop, current = _lock_for_review(cur, proposal_id)
            if req is not None and req.expected_leader_id is not None:
                # Dismiss changes no leader, but confirming one must be the one seen.
                _check_fresh(prop, current, req.model_copy(update={"acknowledge_stale": True}))
            if confirm:
                if not current:
                    raise HTTPException(
                        status_code=422, detail="There is no current record to confirm."
                    )
                cur.execute(
                    "update leaders set last_verified_at = now() where id = %s",
                    (current["id"],),
                )
                _record_manual_verification(
                    cur, prop["city_id"], current["full_name"], prop["source_url"]
                )
            _close(cur, proposal_id, "rejected", actor, reason)
            event_id = _audit(
                cur,
                prop=prop,
                action="dismiss",
                actor=actor,
                reason=reason,
                before=current,
                after=current,
                source_url=prop["source_url"],
                confirmed_current=confirm,
            )
        return {
            "rejected": True,
            "proposal_id": proposal_id,
            "confirmed_current": confirm,
            "review_event_id": event_id,
        }

    @app.post("/admin/proposals/{proposal_id}/retry", tags=["admin"])
    def retry_finding(
        proposal_id: int,
        req: RetryRequest,
        authorization: str | None = Header(None),
    ):
        """Retry: ask the verifier to re-check this city. Publishes nothing and
        does not mark anything verified. The verifier picks up status='retry'.
        """
        require_admin(authorization)
        with get_cursor() as cur:
            prop, current = _lock_for_review(cur, proposal_id)
            cur.execute(
                """
                update leader_proposals
                set status = 'retry', retry_requested_at = now(), reviewed_by = %s,
                    review_reason = %s
                where id = %s
                """,
                (req.actor, req.reason, proposal_id),
            )
            event_id = _audit(
                cur,
                prop=prop,
                action="retry",
                actor=req.actor,
                reason=req.reason,
                before=current,
                after=current,
                source_url=prop["source_url"],
            )
        return {"retry_queued": True, "proposal_id": proposal_id, "review_event_id": event_id}

    @app.get("/admin/review-events", tags=["admin"])
    def list_review_events(
        authorization: str | None = Header(None),
        city_id: int | None = None,
        proposal_id: int | None = None,
        limit: int = 50,
    ):
        """The audit log, newest first. Filter by city or finding."""
        require_admin(authorization)
        limit = max(1, min(limit, 500))
        where, params = [], []
        if city_id is not None:
            where.append("e.city_id = %s")
            params.append(city_id)
        if proposal_id is not None:
            where.append("e.proposal_id = %s")
            params.append(proposal_id)
        clause = ("where " + " and ".join(where)) if where else ""
        params.append(limit)
        with get_cursor() as cur:
            cur.execute(
                f"""
                select e.*, c.city, c.state_code
                from review_events e join cities c on c.id = e.city_id
                {clause}
                order by e.created_at desc, e.id desc
                limit %s
                """,
                params,
            )
            rows = cur.fetchall()
        return {"events": rows, "count": len(rows)}
