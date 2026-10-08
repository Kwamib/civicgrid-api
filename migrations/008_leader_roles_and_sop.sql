-- Migration 008: leader roles + SOP history/run columns
--
-- ADDITIVE and idempotent. Existing rows become role = 'chief_executive', so
-- behaviour is unchanged until the API starts filtering and writing by role.
--
--   1. leaders.role: 'chief_executive' (mayor, select board chair, village president…)
--      or 'chief_administrator' (town administrator, city manager…).
--      One current leader per city PER ROLE replaces one per city.
--   2. leaders history columns from the SOP: source_url, term_start, ended_at, changed_via.
--   3. leader_proposals: run_id, effective_date, recheck_at (Needs Verification date).
--   4. runs: one row per pipeline run (weekly verifier, QC, bucket retries).
--
-- Run BEFORE deploying the API that reads/writes these columns.

-- 1. Roles
alter table leaders add column if not exists role text not null default 'chief_executive';

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'leaders_role_check') then
    alter table leaders add constraint leaders_role_check
      check (role in ('chief_executive', 'chief_administrator'));
  end if;
end $$;

-- New rule first, then drop the old one, so there is never a moment with no guard.
create unique index if not exists leaders_one_current_per_city_role
  on leaders (city_id, role) where is_current;
drop index if exists leaders_one_current_per_city;

-- 2. History and evidence on the record itself
alter table leaders
  add column if not exists source_url  text,
  add column if not exists term_start  date,
  add column if not exists ended_at    timestamptz,
  add column if not exists changed_via text;

do $$
begin
  if not exists (select 1 from pg_constraint where conname = 'leaders_changed_via_check') then
    alter table leaders add constraint leaders_changed_via_check
      check (changed_via is null or changed_via in
             ('election', 'appointment', 'resignation', 'death', 'correction'));
  end if;
end $$;

-- 3. Runs (created before proposals reference it)
create table if not exists runs (
    id                bigint generated always as identity primary key,
    kind              text not null,              -- weekly_verify | qc | bucket_retry | forced_reverify
    started_at        timestamptz not null default now(),
    finished_at       timestamptz,
    status            text not null default 'running',  -- running | ok | halted | failed
    cities_checked    integer,
    proposals_created integer,
    approvals         integer,
    rejections        integer,
    breaker_tripped   boolean not null default false,
    qc_failures       integer,
    notes             text
);

alter table leader_proposals
  add column if not exists run_id         bigint references runs(id) on delete set null,
  add column if not exists effective_date date,
  add column if not exists recheck_at     timestamptz;

-- Not part of the public Data API. Skip role revokes on plain Postgres (local tests).
alter table runs enable row level security;
do $$
declare r text;
begin
    foreach r in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = r) then
            execute format('revoke all on table runs from %I', r);
            execute format('revoke all on sequence runs_id_seq from %I', r);
        end if;
    end loop;
end $$;
