-- Durable OPEN-LEG state für die self-computed Route-Engine (P1, 2026-07-02).
--
-- Problem: observe_adsb_positions() (blueprints/aerox_data_blueprint.py) erkennt
-- Abflug (Boden→Luft nahe Flughafen X) und Ankunft (Luft→Boden nahe Flughafen Y)
-- und schreibt das fertige Leg X→Y in ax_route_cache (source='aerox_adsb'). Der
-- ZWISCHENZUSTAND ("hex ist von X abgehoben, fliegt noch") lebte NUR in einem
-- In-Memory-Dict (_TRACK_STATE). Cloud Run recycelt Instanzen ständig →
--   · ein vor dem Restart gestarteter Flug verliert seinen Abflug → bei der
--     Landung kein Origin → KEIN Leg.
--   · Langstrecke (Stunden zwischen Start und Landung) schafft es fast NIE, ein
--     Leg zu vollenden → ax_route_cache wächst kaum aus eigenen Beobachtungen.
--
-- Fix: der offene Leg wird hier gespiegelt = Source of Truth. Ein kleiner
-- In-Memory-LRU (_TRACK_STATE) bleibt Front-Cache für Speed, aber Supabase
-- übersteht Restarts. Upsert bei erkanntem Abflug (PK=hex → idempotent),
-- Read+Delete bei erkannter Landung, periodische Eviction verwaister Legs
-- (dep_ts > ~20h = Flug verloren/nie in Sicht gelandet).
--
-- WICHTIG: der Code degradiert sauber auf reines In-Memory solange diese Tabelle
-- NICHT existiert — PostgREST PGRST205 ("Could not find the table ... in the
-- schema cache") wird abgefangen und GENAU EINMAL geloggt (exakt wie die anderen
-- Fallbacks: user_flight_ops, wall_post_counters, ...). Diese Migration einmal im
-- Supabase-SQL-Editor anwenden → offene Legs sind restart-durabel und die eigene
-- Routen-DB wächst zuverlässig aus dem eh schon gepollten ADS-B.
--
-- Service-Role-Key (Backend) umgeht RLS; der Anon-Client bleibt geblockt.

create table if not exists public.ax_open_legs (
    hex          text         primary key,          -- ICAO24 (lowercase)
    origin_iata  text,                              -- beobachteter Abflug (IATA)
    origin_icao  text,
    dep_ts       timestamptz  not null default now(),
    last_lat     double precision,
    last_lon     double precision,
    last_alt     integer,
    last_seen    timestamptz  not null default now(),
    callsign     text,
    reg          text
);

-- Stale-Eviction räumt per dep_ts (delete ... where dep_ts < cutoff).
create index if not exists idx_ax_open_legs_dep_ts
    on public.ax_open_legs(dep_ts);

alter table public.ax_open_legs enable row level security;
