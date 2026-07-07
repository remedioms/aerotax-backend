-- aircraft_live — Last-Known-Position + Route pro Airframe (reg-keyed).
--
-- Owner-Idee 2026-07-08: „Supabase-Speicher der Live-Map — geht ein Flug offline
-- (kein ADS-B über Russland/Ozean), simulieren wir aus dem letzten Snapshot bis
-- er wieder online ist. Gratis, kein verlorener Flieger; und weil wir speichern,
-- haben wir sogar die Route."
--
-- Gefüllt vom NAS-Harvester (nas_harvester/ingest.py) via FR24-gRPC-live_feed
-- (umgeht den feed.js-Soft-Block der alten Version). Gelesen vom Backend
-- (_aircraft_live_pos_for_reg) als Positions-Tier VOR dem on-demand-Korridor.
--
-- Key = NORMALISIERTE Reg (ohne Bindestrich, upper: „DABYM"), damit Roster-Tail
-- (mal „D-ABYM", mal „DABYM") verlässlich matcht. Ein Airframe = eine Zeile
-- (upsert on_conflict=reg, freshester Stand gewinnt).

create table if not exists public.aircraft_live (
    reg         text primary key,          -- normalisiert: replace('-','')|upper
    reg_display text,                       -- Original mit Bindestrich (Anzeige)
    callsign    text,                       -- z.B. DLH716
    flight      text,                       -- IATA/OP-Flugnr, z.B. LH716
    lat         double precision,
    lon         double precision,
    track       double precision,           -- Grad
    gs_kt       double precision,           -- Knoten
    alt_ft      double precision,           -- Fuß
    origin      text,                        -- Route-Start (IATA), z.B. FRA
    dest        text,                        -- Route-Ziel (IATA), z.B. HND
    ac_type     text,                        -- ICAO-Typ, z.B. B748
    flightid    bigint,                      -- FR24-Flight-ID (Debug/Dedup)
    on_ground   boolean default false,
    source      text default 'fr24_grpc',
    seen_ts     timestamptz,                 -- echte FR24-Beobachtungszeit
    updated_at  timestamptz default now()    -- Upsert-Zeit (Freshness-Gate)
);

create index if not exists idx_aircraft_live_callsign on public.aircraft_live (callsign);
create index if not exists idx_aircraft_live_dest     on public.aircraft_live (dest);
create index if not exists idx_aircraft_live_updated  on public.aircraft_live (updated_at desc);

-- PostgREST: service_role liest/schreibt ohnehin (RLS-bypass). Falls anon je
-- lesen soll, hier RLS + policy ergänzen — aktuell NICHT nötig (nur Backend liest
-- mit service key, Harvester schreibt mit service key).
