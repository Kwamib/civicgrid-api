-- 011_archive_untrusted_verifications.sql
--
-- Moves automated verification_results rows from before 2026-09-17 into
-- verification_results_archive. Nothing is deleted outright, and human
-- (MANUAL) checks stay where they are.
--
-- Why: those rows came from checks that read wrong-city pages (fixed in the
-- Sep 17 URL cleanup) and recorded no source page, e.g. the July Haiku
-- backfill and the Aug 1 batch that verified Lancaster PA's mayor for Lancaster
-- OH. On Oct 10 the leader stamps from that era were cleared
-- (leaders_backup_20261010_stalestamps), but public city profiles still took
-- "last check result" from these rows, so a city could read "unverified" and
-- "last check: confirmed (July)" at once. After this migration every reader
-- (public profile, stats, the admin UNSURE worklist) sees only checks from the
-- traceable verifier v7 era and human reviews since.
--
-- To undo:  insert into verification_results select * from verification_results_archive;
--
-- Safe to run more than once: a second run finds nothing left to move.

create table if not exists verification_results_archive
    (like verification_results including defaults);

with moved as (
    delete from verification_results
    where verified_at < '2026-09-17'
      and verdict <> 'MANUAL'
    returning *
)
insert into verification_results_archive
select * from moved;

alter table verification_results_archive enable row level security;

-- Same lockdown as every other public table: no access for Supabase's API roles.
do $$
declare
    r text;
begin
    foreach r in array array['anon', 'authenticated'] loop
        if exists (select 1 from pg_roles where rolname = r) then
            execute format('revoke all on table verification_results_archive from %I', r);
        end if;
    end loop;
end $$;
