-- 014_city_slug_index.sql
--
-- Index for GET /cities/by-slug/{slug}. The slug is not stored: it is computed
-- from city and state_code with the same rule as the web app's citySlug()
-- ("St. Louis", "MO" -> "st-louis-mo"), so a renamed city can never carry a
-- stale slug. This expression index lets that lookup use an index instead of
-- scanning every city. The endpoint works without it (about 3,000 rows), it is
-- just slower.
--
-- Not unique: two cities could in principle share a slug. The endpoint returns
-- the most populous one. To check for collisions before relying on slugs:
--
--   select trim(both '-' from regexp_replace(lower(city || ' ' || state_code),
--                                            '[^a-z0-9]+', '-', 'g')) as slug,
--          count(*)
--   from cities group by 1 having count(*) > 1;
--
-- No new grants. Safe to run more than once, and on plain Postgres (CI and
-- local tests).

create index if not exists cities_slug_idx on cities (
    (trim(both '-' from regexp_replace(lower(city || ' ' || state_code), '[^a-z0-9]+', '-', 'g')))
);
