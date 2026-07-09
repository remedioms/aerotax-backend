-- aircraft_track — APPEND-ONLY Breadcrumb-Verlauf pro Airframe (die ECHTE geflogene Route).
--
-- Owner-Idee 2026-07-09: aircraft_live ist ein SNAPSHOT (1 Zeile/Reg, wird jeden
-- Poll überschrieben) — es gibt keinen Verlauf, also keine echte geflogene Linie.
-- Diese Tabelle sammelt die Positionen ÜBER ZEIT → der geflogene Pfad wächst
-- dauerhaft in der eigenen DB (auch historisch, ohne FR24 live). Passt zur
-- „mehr & mehr aus dem Backend"-Strategie.
--
-- Gefüllt von zwei Quellen (beide idempotent dank PK (reg, seen_ts)):
--   1. NAS-Harvester (nas_harvester/ingest.py) — hängt pro 60s-Poll je airborne,
--      bewegten Airframe einen Punkt an (permanenter Fleet-Verlauf, LH-Group+DE).
--   2. Backend /api/ax/flown-track Tier 2 — schreibt den FR24-`flight_trail_list`
--      eines on-demand geöffneten Flugs zurück (jede Airline, sofort dicht).
--
-- Gelesen vom Backend-Endpoint /api/ax/flown-track (Tier 1) → iOS zeichnet die
-- echte Linie in der Karte (Radar/MyPlane/Suche/alte Flüge/Freunde).
--
-- Ein Leg = SELECT ... WHERE reg=? AND seen_ts BETWEEN <dep> AND <arr> ORDER BY seen_ts.
-- Reg NORMALISIERT (ohne Bindestrich, upper) — gleich wie aircraft_live, damit
-- Roster-Tail verlässlich matcht.

create table if not exists public.aircraft_track (
    reg        text not null,               -- normalisiert: replace('-','')|upper
    seen_ts    timestamptz not null,        -- echte Beobachtungszeit (FR24)
    flight     text,                        -- IATA/OP-Flugnr (Leg-Filter/Kontext)
    origin     text,                        -- Route-Start (IATA)
    dest       text,                        -- Route-Ziel (IATA)
    lat        double precision not null,
    lon        double precision not null,
    alt_ft     integer,
    gs_kt      integer,
    track_deg  integer,
    on_ground  boolean default false,
    source     text default 'fr24_grpc',    -- 'fr24_grpc' (Harvester) | 'fr24_trail' (Rückschreibung)
    primary key (reg, seen_ts)               -- dedupt + ordnet natürlich
);

-- Zeit-Range-Query pro Airframe (Haupt-Lesepfad) und pro Flugnummer.
create index if not exists idx_aircraft_track_reg_ts on public.aircraft_track (reg, seen_ts);
create index if not exists idx_aircraft_track_flt_ts on public.aircraft_track (flight, seen_ts);

-- Retention: rolling 60-Tage-Fenster (Cleanup via /api/internal/track-prune bzw.
-- Hetzner-Cron). Für instant-Cleanup später Monats-Partitionierung denkbar.
--   delete from public.aircraft_track where seen_ts < now() - interval '60 days';

-- RLS: nur service_role liest/schreibt (Backend + Harvester). Kein anon nötig.
