-- Aircraft-Info-Cache (Worker USP-bonus, 2026-06-01).
--
-- Cached Aircraft-Metadaten (Manufacturer/Model/Build-Year/Seats/Operator)
-- pro Registration. TTL 30 Tage — Backend-Blueprint prüft `fetched_at`
-- vor dem Return und ignoriert stale Entries (kein TRIGGER nötig).
--
-- Schema-Entscheidungen:
--  · PK reg (text) statt uuid — Lookup geht 1:1 nach Registration,
--    composite-Key macht keinen Sinn (eine Reg eine Aircraft).
--  · `payload jsonb` für optionale Felder die wir nicht als Top-Level-
--    Spalten vorhalten wollen (last_seen_date, serial_number, engines,
--    first_flight_date) — so flexibel ohne Schema-Migrationen pro
--    Field-Addition.
--  · `hex24` als Sekundär-Spalte + Index für inverse-Lookup (z.B. wenn
--    ADS-B uns einen Hex liefert und wir die Reg dazu wollen).
--  · KEINE updated_at — `fetched_at` ist der einzige Zeitstempel den wir
--    brauchen (upsert überschreibt ihn jedes Mal).

create table if not exists public.aircraft_info_cache (
    reg            text          primary key,
    hex24          text,
    manufacturer   text,
    model          text,
    type_code      text,
    build_year     int,
    seats          int,
    operator       text,
    country        text,
    payload        jsonb,
    fetched_at     timestamptz   not null default now()
);

-- Inverse-Lookup (Hex → Reg) — selten gebraucht aber günstig:
create index if not exists idx_aircraft_info_hex
    on public.aircraft_info_cache(hex24)
    where hex24 is not null;

-- TTL-Cleanup-Helfer: Index auf fetched_at damit Cron-Jobs alte Rows
-- schnell finden können (`delete from … where fetched_at < now() - 90d`).
create index if not exists idx_aircraft_info_fetched_at
    on public.aircraft_info_cache(fetched_at);

-- Service-Role-Key umgeht RLS. Anon-Client bleibt geblockt (Cache wird
-- nur via Blueprint-Endpoint befüllt, nie direkt vom Client).
alter table public.aircraft_info_cache enable row level security;
