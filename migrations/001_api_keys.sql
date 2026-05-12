-- migrations/001_api_keys.sql
-- Adds api_keys table for authenticated API access with tier-based rate limiting.
-- Run this in your Supabase SQL editor against the civicgrid database.

create table if not exists api_keys (
    id              bigserial primary key,
    key_hash        text        not null,           -- bcrypt hash of the full key
    key_prefix      text        not null unique,    -- first 16 chars, for identification
    user_email      text        not null,           -- contact email for the key owner
    tier            text        not null default 'free'
                    check (tier in ('free', 'starter', 'pro')),
    label           text,                            -- optional friendly name (e.g. "newsroom-prod")
    created_at      timestamptz not null default now(),
    last_used_at    timestamptz,
    revoked_at      timestamptz,
    request_count   bigint      not null default 0
);

create index if not exists idx_api_keys_prefix      on api_keys(key_prefix);
create index if not exists idx_api_keys_user_email  on api_keys(user_email);
create index if not exists idx_api_keys_active      on api_keys(key_prefix) where revoked_at is null;

comment on table  api_keys is 'API keys for authenticated access to CivicGrid endpoints.';
comment on column api_keys.key_hash is 'bcrypt hash of the full key. The raw key is never stored.';
comment on column api_keys.key_prefix is 'First 16 chars of the key, used for identification in logs and DB lookups.';
comment on column api_keys.tier is 'Rate limit tier: free (100/day), starter (10k/day), pro (100k/day).';
comment on column api_keys.revoked_at is 'When set, the key is invalid. Soft-deleted for audit trail.';
