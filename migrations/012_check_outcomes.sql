-- 012_check_outcomes.sql
--
-- Remember each city's latest verifier outcome, so Fix-a-city can show a
-- manual-check queue (Unverified / Blocked / No name / Wrong link).
--
-- last_check_outcome is the raw outcome from verify_mayors_v7.py, e.g.
--   match, change, ambiguous, rejected_before                    (page read fine)
--   blocked, soft_blocked, robots_deny                           (site refuses scripts)
--   no_name, bad_extract, guard_blocked                          (no usable name on the page)
--   notfound, off_domain, wrong_state, bad_page                  (wrong or dead link)
--   fetch_error, model_error                                     (transient; retried next run)
-- "unchanged" (page hash same as last time) is never written: the previous
-- outcome still describes the page.
--
-- The verifier may write ONLY these two columns of cities (plus page_hash and
-- last_fetch_at from migration 010). The API and QC read them with their
-- existing table-level SELECT grants.
--
-- Safe to run more than once, and on plain Postgres (CI and local tests).

alter table cities add column if not exists last_check_outcome text;
alter table cities add column if not exists last_check_at      timestamptz;

create index if not exists idx_cities_last_check_outcome on cities (last_check_outcome);

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'civicgrid_verifier') then
        grant update (last_check_outcome, last_check_at) on cities to civicgrid_verifier;
    end if;
end $$;
