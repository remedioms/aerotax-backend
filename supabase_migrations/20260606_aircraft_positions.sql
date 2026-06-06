-- Aircraft-Position-Persistence (Live-ADS-B-Fallback, 2026-06-06).
--
-- Hält die letzte bekannte Position pro Registration vor, damit die App
-- (Cabin-Crew) Registration/Position/Route-Start/Aircraft-Type auch dann
-- anzeigen kann, wenn Live-ADS-B (OpenSky/adsb.lol) gerade nichts liefert.
--
-- Befüllt aus zwei Quellen:
--  · POST /api/adsb/persist-position  (iOS pusht aktiv die zuletzt gesehene Pos)
--  · best-effort Upsert im Live-Fetch-Pfad von /api/adsb/state (Tabelle bleibt
--    auch ohne iOS-POST warm).
--
-- Schema-Entscheidungen:
--  · PK registration (text) statt uuid — Lookup geht 1:1 nach Reg, eine Reg =
--    ein Flugzeug. Upsert on_conflict='registration' überschreibt die alte Pos.
--  · `fetched_at` ist der einzige Frische-Zeitstempel — Blueprint prüft
--    < 24h vor dem Return und ignoriert stale Rows (kein TRIGGER nötig).
--  · `last_seen_unix` ist das ADS-B-Signal-Alter (von der Quelle), separat von
--    `fetched_at` (wann WIR das gespeichert haben).

create table if not exists public.aircraft_positions (
    registration       text                primary key,
    hex24              text,
    callsign           text,
    latitude           double precision,
    longitude          double precision,
    altitude_m         double precision,
    ground_speed_kts   double precision,
    heading_deg        double precision,
    on_ground          boolean,
    squawk             text,
    last_seen_unix     double precision,
    route_start_iata   text,
    aircraft_type      text,
    fetched_at         timestamptz         not null default now()
);

-- Freshness-/TTL-Cleanup-Helfer: Index auf fetched_at desc damit Cron-Jobs
-- alte Rows schnell finden (`delete from … where fetched_at < now() - 30d`).
create index if not exists idx_aircraft_positions_fetched_at
    on public.aircraft_positions(fetched_at desc);

-- Service-Role-Key umgeht RLS. Anon-Client bleibt geblockt (Tabelle wird nur
-- via Blueprint-Endpoint befüllt/gelesen, nie direkt vom Client).
alter table public.aircraft_positions enable row level security;
