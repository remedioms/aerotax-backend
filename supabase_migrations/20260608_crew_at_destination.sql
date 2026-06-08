-- "Crew at my Destination" - layover visibility (opt-in) + manual pins.
-- Feature 2026-06-08. Two modes:
--   A) Layover-based (auto): which friends have a layover at the same station
--      within +/-24h -> avatars on the Crew Map.
--   B) Manual pin: "I want to do something here on <date>" -> visible only to
--      mutual friends (filtered server-side via the user_friends edges).
--
-- Pattern like layover_recs/wall_posts (20260607_layover_recs.sql,
-- 20260601_social.sql): SB-primary, service-role bypasses RLS, idempotent
-- upserts via PK/on_conflict. Frequently filtered fields as columns.
--
-- NOTE: comments kept ASCII-only on purpose - the Supabase SQL editor choked on
-- em-dashes / smart-quotes / arrows / box-drawing chars from a previous version.

-- LAYOVER-VISIBILITY (opt-in, default on)
-- One row per user. enabled=true means my layovers may appear as a match on my
-- friends' Crew Map. Missing row -> default "on" (handled in the backend by
-- _layover_visibility_get); a row is only written when the user flips the toggle.
create table if not exists public.layover_visibility (
    user_token text        primary key,
    enabled    boolean     not null default true,
    updated_at timestamptz not null default now()
);
alter table public.layover_visibility enable row level security;

-- MANUAL-PINS (intent pins "I want to do something here")
-- Visible ONLY to mutual friends (filtered in the backend over the user_friends
-- edges). lat/lng are resolved from iata_code on insert (airports_compact.json)
-- or sent directly by the client (map tap).
create table if not exists public.manual_pins (
    id         text        primary key,
    user_token text        not null,
    iata_code  text        not null default '',
    lat        double precision,
    lng        double precision,
    pin_date   date,
    note       text,
    created_at timestamptz not null default now()
);
create index if not exists idx_manual_pins_user
    on public.manual_pins(user_token);
create index if not exists idx_manual_pins_iata
    on public.manual_pins(iata_code);
alter table public.manual_pins enable row level security;
