-- 010_scheduled_job_roles.sql
--
-- Least-privilege logins for the scheduled jobs on Anansi, which until now
-- connected as `postgres`.
--
-- civicgrid_verifier (verify_mayors_v7.py, weekly):
--   reads cities, leaders, leader_proposals, runs;
--   may change ONLY cities.page_hash / last_fetch_at and leaders.last_verified_at
--   (column-level grants: it cannot rename a city or change a leader's name);
--   may add proposals, verification records and runs, and close its own run.
--   No DELETE anywhere, no DDL.
--
-- civicgrid_readonly (qc_check.py and backup_db.sh, weekly):
--   SELECT on every table and sequence in public, including tables created
--   later (default privileges), and BYPASSRLS so pg_dump sees every row.
--   Forced read-only at login (default_transaction_read_only), and it has no
--   INSERT/UPDATE/DELETE grants anyway.
--
-- NO PASSWORDS HERE. After applying, set them interactively:
--     psql "$DB_URL" -c '\password civicgrid_verifier'
--     psql "$DB_URL" -c '\password civicgrid_readonly'
-- Connect through the session pooler as <role>.<project-ref>.
--
-- Manual fixes, migrations and discovery keep using `postgres`, from a separate
-- .env.admin that cron never reads.
--
-- Safe to run more than once, and on plain Postgres (CI and local tests).

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'civicgrid_verifier') then
        create role civicgrid_verifier login noinherit connection limit 5;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'civicgrid_readonly') then
        create role civicgrid_readonly login noinherit bypassrls connection limit 5;
    end if;
end $$;

alter role civicgrid_readonly set default_transaction_read_only = on;

-- Columns the verifier and discovery scripts use. They were added to production
-- outside the migrations; declaring them here (no-op where they exist) lets the
-- column-level grants below run on a fresh database too.
alter table cities add column if not exists page_hash     text;
alter table cities add column if not exists page_etag     text;
alter table cities add column if not exists fetch_status  text;
alter table cities add column if not exists last_fetch_at timestamptz;

grant usage on schema public to civicgrid_verifier, civicgrid_readonly;

-- ---------------------------------------------------------------- verifier
grant select on cities, leaders, leader_proposals, runs to civicgrid_verifier;
grant update (page_hash, last_fetch_at) on cities to civicgrid_verifier;
grant update (last_verified_at) on leaders to civicgrid_verifier;
grant insert on leader_proposals, verification_results, runs to civicgrid_verifier;
grant update (finished_at, status, cities_checked, proposals_created, breaker_tripped)
    on runs to civicgrid_verifier;
grant usage on all sequences in schema public to civicgrid_verifier;

do $$
declare
    t text;
begin
    foreach t in array array[
        'cities', 'leaders', 'leader_proposals', 'verification_results', 'runs'
    ] loop
        execute format('drop policy if exists civicgrid_verifier_all on %I', t);
        execute format(
            'create policy civicgrid_verifier_all on %I for all to civicgrid_verifier '
            'using (true) with check (true)', t);
    end loop;
end $$;

-- ---------------------------------------------------------------- readonly
grant select on all tables in schema public to civicgrid_readonly;
grant select on all sequences in schema public to civicgrid_readonly;
alter default privileges for role postgres in schema public
    grant select on tables to civicgrid_readonly;
alter default privileges for role postgres in schema public
    grant select on sequences to civicgrid_readonly;
