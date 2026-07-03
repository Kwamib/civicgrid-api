-- Migration 004: last_verified_at on leaders
--
-- Timestamp of when this leader record was last confirmed against an external
-- source. Set on proposal approval (an approval IS a verification against a
-- cited source), and by future scheduled re-verification (Phase 6). Exposing
-- this in the API is a differentiator: consumers can see data freshness /
-- provenance, which most civic-data sources do not surface.

alter table leaders add column if not exists last_verified_at timestamptz;

-- Backfill: leaders that came from an approved proposal were verified at the
-- proposal's review time. Stamp the current leader of each approved-proposal
-- city with that proposal's reviewed_at.
update leaders l
set last_verified_at = p.reviewed_at
from leader_proposals p
where p.city_id = l.city_id
  and p.status = 'approved'
  and l.is_current = true
  and l.last_verified_at is null;
