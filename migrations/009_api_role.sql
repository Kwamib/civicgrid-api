-- 009_api_role.sql
--
-- A dedicated, least-privilege database login for the API and the
-- webhook-drain CronJob, which until now connected as `postgres` (the owner of
-- every table: it could drop or rewrite anything).
--
-- civicgrid_api can read and write exactly what the code uses, and nothing else:
--   * no DROP / ALTER / CREATE (it owns nothing and gets no CREATE on public)
--   * no DELETE on leaders, cities, proposals, verifications, review events or keys
--   * no access to runs or the backup tables
--
-- Row-level security is on for every public table with no policies, which is
-- what keeps Supabase's anon role out. That would block this role too, so each
-- table below gets one policy for civicgrid_api only. What the role may actually
-- do is still limited by the GRANTs.
--
-- NO PASSWORD HERE (this file is in Git). After applying, set it interactively:
--     psql "$DB_URL" -c '\password civicgrid_api'
-- Connect through the Supabase session pooler as user civicgrid_api.<project-ref>.
--
-- RULE FOR FUTURE MIGRATIONS: a new table the API uses needs its own GRANT and
-- civicgrid_api_all policy in that migration, or the API gets "permission denied".
--
-- Safe to run more than once, and on plain Postgres (CI and local tests).

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'civicgrid_api') then
        create role civicgrid_api login noinherit connection limit 30;
    end if;
end $$;

grant usage on schema public to civicgrid_api;

grant select                         on cities, provenance           to civicgrid_api;
grant update                         on cities                       to civicgrid_api;
grant select, insert, update         on leaders                      to civicgrid_api;
grant select, update                 on leader_proposals             to civicgrid_api;
grant select, insert                 on verification_results         to civicgrid_api;
grant select, insert                 on review_events                to civicgrid_api;
grant select, insert, update         on api_keys                     to civicgrid_api;
grant select, insert, update, delete on webhook_subscriptions        to civicgrid_api;
grant select, insert, update         on webhook_events               to civicgrid_api;
grant select, insert, update         on webhook_deliveries           to civicgrid_api;

-- bigserial ids (api_keys, webhooks, verification_results, review_events).
grant usage on all sequences in schema public to civicgrid_api;

do $$
declare
    t text;
begin
    foreach t in array array[
        'cities', 'provenance', 'leaders', 'leader_proposals',
        'verification_results', 'review_events', 'api_keys',
        'webhook_subscriptions', 'webhook_events', 'webhook_deliveries'
    ] loop
        execute format('drop policy if exists civicgrid_api_all on %I', t);
        execute format(
            'create policy civicgrid_api_all on %I for all to civicgrid_api '
            'using (true) with check (true)', t);
    end loop;
end $$;
