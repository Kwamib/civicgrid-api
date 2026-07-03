-- Migration 003: leader_proposals
-- Stores mayor-verification findings for human review before they are applied.
-- The verifier writes proposals here; the /admin/review page reads pending ones;
-- approving a proposal writes the correction via the leader-rotation endpoint.

create table if not exists leader_proposals (
  id            bigint generated always as identity primary key,
  city_id       bigint not null references cities(id),
  city          text   not null,
  state_code    text   not null,
  population    integer,
  db_mayor      text,          -- what the DB currently says
  web_mayor     text,          -- what the verifier found (the proposed correction)
  source_url    text,          -- evidence the verifier used
  confidence    text,          -- high | medium | low
  status        text   not null default 'pending',  -- pending | approved | rejected
  created_at    timestamptz not null default now(),
  reviewed_at   timestamptz
);

create index if not exists idx_leader_proposals_status on leader_proposals (status);
create index if not exists idx_leader_proposals_city on leader_proposals (city_id);
