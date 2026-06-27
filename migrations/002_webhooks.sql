-- migrations/002_webhooks.sql
-- Adds the webhook system: per-event subscriptions, a transactional outbox of
-- events, and a per-(event x subscription) delivery ledger with retry state.
-- Run this in your Supabase SQL editor against the civicgrid database.
--
-- See docs/design/webhooks.md for the full design. Delivery is at-least-once
-- via the outbox pattern, drained by a CronJob; consumers dedupe on event_id.

-- gen_random_uuid() is in Postgres core (13+); pgcrypto guard is harmless if
-- the function already exists.
create extension if not exists pgcrypto;


-- ---------------------------------------------------------------------------
-- Subscriptions: who wants notified, for which events, and the signing secret.
-- ---------------------------------------------------------------------------
create table if not exists webhook_subscriptions (
    id              bigserial   primary key,
    target_url      text        not null,            -- where deliveries are POSTed
    secret          text        not null,            -- HMAC signing secret (retrievable; see design §7)
    events          text[]      not null,            -- subscribed event types, e.g. {'leader.rotated'}
    is_active       boolean     not null default true,
    label           text,                            -- optional friendly name
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    check (array_length(events, 1) >= 1)             -- must subscribe to at least one event
);

create index if not exists idx_webhook_subs_active on webhook_subscriptions(id) where is_active;

comment on table  webhook_subscriptions is 'Registered webhook endpoints and the event types each one receives.';
comment on column webhook_subscriptions.secret is 'HMAC-SHA256 signing secret. Stored retrievable (needed to sign); shown to the owner once at creation. At-rest encryption is a documented follow-up.';
comment on column webhook_subscriptions.events is 'Array of subscribed event types, e.g. {leader.rotated, leader.updated}.';


-- ---------------------------------------------------------------------------
-- Events outbox: one immutable row per thing-that-happened, written in the
-- SAME transaction as the data change so an event can never be lost.
-- id is the event_id consumers dedupe on.
-- ---------------------------------------------------------------------------
create table if not exists webhook_events (
    id              uuid        primary key default gen_random_uuid(),
    event_type      text        not null,            -- 'leader.rotated' | 'leader.updated'
    payload         jsonb       not null,            -- full event body (see design §5)
    fanned_out      boolean     not null default false,
    created_at      timestamptz not null default now()
);

create index if not exists idx_webhook_events_pending on webhook_events(created_at) where not fanned_out;

comment on table  webhook_events is 'Transactional outbox. One row per leader-change event; id is the event_id sent to consumers.';
comment on column webhook_events.fanned_out is 'True once delivery rows have been created for all matching subscriptions.';


-- ---------------------------------------------------------------------------
-- Delivery ledger: one row per (event x subscription). Holds per-subscriber
-- retry/backoff state. Created during fan-out, not by the producer.
-- ---------------------------------------------------------------------------
create table if not exists webhook_deliveries (
    id               bigserial   primary key,
    event_id         uuid        not null references webhook_events(id) on delete cascade,
    subscription_id  bigint      not null references webhook_subscriptions(id) on delete cascade,
    status           text        not null default 'pending'
                     check (status in ('pending', 'in_flight', 'delivered', 'dead')),
    attempts         int         not null default 0,
    next_attempt_at  timestamptz not null default now(),
    claimed_at       timestamptz,                     -- set while in_flight; used to reclaim stuck rows
    last_status_code int,
    last_error       text,
    last_attempt_at  timestamptz,
    delivered_at     timestamptz,
    created_at       timestamptz not null default now(),
    unique (event_id, subscription_id)                -- makes fan-out idempotent
);

-- The drain's claim query: pending rows whose next_attempt_at has passed.
create index if not exists idx_webhook_deliveries_due on webhook_deliveries(status, next_attempt_at);

comment on table  webhook_deliveries is 'Per-(event x subscription) delivery attempts and retry state.';
comment on column webhook_deliveries.status is 'pending -> in_flight (claimed) -> delivered | dead (exhausted retries or permanent failure).';
comment on column webhook_deliveries.claimed_at is 'Set when a drain run claims the row. Rows stuck in_flight past a timeout are reclaimed.';
