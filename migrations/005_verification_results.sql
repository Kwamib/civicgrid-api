-- Migration 005: verification_results
--
-- Durable, queryable history of every mayor verification check. Replaces the
-- fragile results.jsonl file (which was lost, forcing full $55 re-runs). With
-- this table, re-verification can be INCREMENTAL: only re-check cities whose
-- last check is stale or that were flagged, instead of blindly re-running all
-- 3,064 every time. This is the cost-control foundation for ongoing freshness.
--
-- Full history (one row per check), not upsert — so we can see how a city's
-- verification changed over time and target re-checks intelligently.

create table if not exists verification_results (
    id            bigserial primary key,
    city_id       integer not null references cities(id),
    verdict       text not null check (verdict in ('MATCH','MISMATCH','UNSURE','ERROR','MANUAL')),    db_mayor      text,          -- what was in the DB at check time
    web_mayor     text,          -- what the web search found
    source        text,          -- source URL
    confidence    text,          -- high / medium / low
    model         text,          -- model used (audit trail)
    run_id        text,          -- groups one run's rows together
    verified_at   timestamptz not null default now()
);

-- Fast "when was city X last checked" + "what's stale" queries.
create index if not exists idx_verif_city_time
    on verification_results (city_id, verified_at desc);

-- Fast "show me all MISMATCHes from run Y" / verdict filtering.
create index if not exists idx_verif_verdict
    on verification_results (verdict);

create index if not exists idx_verif_run
    on verification_results (run_id);

-- Convenience view: latest verification per city (the current state).
create or replace view latest_verification as
select distinct on (city_id)
    city_id, verdict, db_mayor, web_mayor, source, confidence, model, run_id, verified_at
from verification_results
order by city_id, verified_at desc;
