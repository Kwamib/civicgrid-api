-- 013_link_suggestions.sql
--
-- What discovery (discover_mayor_urls_v12.py) could not fix on its own, for
-- Fix-a-city to show next to the city:
--
--   link_note      why the stored website is wrong, e.g.
--                  "website belongs to City of Columbus (OH)"
--   suggested_url  the city's own .gov (from CISA's official .gov list) when
--                  it refused our script, so a person can open it and apply it
--
-- Discovery writes both on every city it processes (null when there is
-- nothing to say). Saving a website in Fix-a-city clears both. A city with a
-- link_note is listed under "Wrong link".
--
-- Discovery writes with the admin login; no new grants. Safe to run more
-- than once, and on plain Postgres (CI and local tests).

alter table cities add column if not exists link_note     text;
alter table cities add column if not exists suggested_url text;
