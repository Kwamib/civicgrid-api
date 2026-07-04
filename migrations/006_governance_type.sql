-- Migration 006: governance_type on cities
--
-- Not every US municipality has a mayor. Many (especially New England towns) use
-- select-board / town-meeting government, or council-manager government. Forcing
-- a "mayor" onto these fabricates data. This column records how a city is
-- actually governed, so mayorless towns are represented correctly with the real
-- top official + title (in leaders.leader_title) instead of a fake mayor.

alter table cities add column if not exists governance_type text
    check (governance_type in (
        'mayor', 'council_manager', 'select_board',
        'town_administrator', 'commission', 'unknown'
    ));

comment on column cities.governance_type is
    'How the city is governed: mayor, council_manager, select_board, town_administrator, commission, or unknown. Null = not yet classified.';
