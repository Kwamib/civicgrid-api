-- Migration 007: review workflow — reviewer actions, evidence, and an audit log
--
-- ADDITIVE ONLY. Nothing here drops, renames, or rewrites existing data, and
-- every statement is idempotent, so it is safe to run more than once.
--
-- What it adds:
--   1. cities.mayor_url: the official leadership page found by discovery. It already
--      exists in production (the discovery scripts added it). It is declared here so
--      a fresh database matches and the API can read it safely.
--   2. leader_proposals: columns for the finding (proposed title, evidence passage,
--      finding type, the leader row the finding was compared against) and for the
--      review (who, why, what was applied, retry time).
--   3. review_events: an APPEND-ONLY audit log of every reviewer action, with
--      before/after values. A trigger blocks UPDATE and DELETE on it.
--
-- Status vocabulary for leader_proposals.status after this migration:
--   pending   : waiting for review (written by the verifier)
--   approved  : Accept. The proposed name was published as the current leader.
--   corrected : Correct. The reviewer published an edited name/title instead.
--   rejected  : Dismiss. The published record was kept unchanged.
--   retry     : Retry. Re-check the source; the published record is unchanged.
-- There is deliberately no CHECK constraint, so older rows and the existing
-- verifier keep working unchanged.
--
-- Run this BEFORE deploying the API version that reads these columns.

alter table cities add column if not exists mayor_url text;

alter table leader_proposals
    add column if not exists proposed_title     text,         -- title the verifier saw (e.g. 'Village President')
    add column if not exists evidence           text,         -- exact supporting passage from source_url, when captured
    add column if not exists finding_type       text,         -- name_change | title_change | vacancy | no_mayor | source_unavailable | conflict
    add column if not exists db_leader_id       bigint,       -- leaders.id the finding was compared against (optimistic concurrency)
    add column if not exists reviewed_by        text,         -- reviewer identity (email)
    add column if not exists review_reason      text,         -- reviewer's reason / note
    add column if not exists applied_name       text,         -- what was actually published on accept/correct
    add column if not exists applied_title      text,
    add column if not exists retry_requested_at timestamptz;

-- One pending finding per city. Production already has an equivalent partial
-- unique index (added by hand after the Santa Ana duplicate); this declares it
-- for fresh databases. If yours has another name, this adds a second identical
-- index, which is harmless. Drop one later if you like.
create unique index if not exists uq_leader_proposals_one_pending
    on leader_proposals (city_id) where status = 'pending';

create table if not exists review_events (
    id               bigint generated always as identity primary key,
    proposal_id      bigint references leader_proposals(id),
    city_id          bigint not null references cities(id),
    action           text   not null check (action in ('accept', 'correct', 'dismiss', 'retry')),
    actor            text   not null,          -- reviewer email
    reason           text,
    before_leader_id bigint,
    before_name      text,
    before_title     text,
    after_leader_id  bigint,
    after_name       text,
    after_title      text,
    confirmed_current boolean not null default false,  -- dismiss + "current record is correct"
    source_url       text,
    evidence         text,
    created_at       timestamptz not null default now()
);

create index if not exists idx_review_events_city on review_events (city_id, created_at desc);
create index if not exists idx_review_events_proposal on review_events (proposal_id);

-- Append-only: audit history can be added to, never edited or deleted.
create or replace function review_events_append_only() returns trigger
language plpgsql as $$
begin
    raise exception 'review_events is append-only (% blocked)', tg_op;
end;
$$;

drop trigger if exists trg_review_events_append_only on review_events;
create trigger trg_review_events_append_only
    before update or delete on review_events
    for each row execute function review_events_append_only();

-- Not part of the public Data API. The API connects to Postgres directly;
-- browsers must never read or write the audit log via /rest/v1.
alter table review_events enable row level security;
revoke all on table review_events from anon, authenticated;
revoke all on sequence review_events_id_seq from anon, authenticated;
