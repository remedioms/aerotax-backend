-- FR24-Grauzonen-Live-Positionen (verteilte Harvester-Flotte, 2026-07-06).
--
-- Mehrere Harvester (Oracle-Always-Free-VMs, je eigene IP) pollen je eine FR24-
-- Korridor-Kachel und upserten hier ihre normalisierten Rows. Das AeroX-Backend
-- LIEST diese Tabelle warm (kein eigener FR24-Kontakt mehr) und bedient daraus
-- die Positions-Kaskade (Coverage-Loch China/Russland/Ozean). So ist die FR24-
-- Last über viele IPs verteilt (kein Single-IP-Block) und alle Zonen laufen
-- parallel/durchgehend statt Round-Robin.
--
-- `row` = die exakte OpenSky-State-Row (JSON-Array), die das Backend erwartet —
-- der Harvester normalisiert genau wie _fr24_row_to_opensky, damit das Backend
-- 0 Transformation braucht. hex = PK (eine Zeile pro Flieger, letzter Poll gewinnt).

create table if not exists public.fr24_live (
    hex        text        not null,
    callsign   text,
    lat        double precision,
    lon        double precision,
    origin     text,                       -- IATA Start (Route-Enrichment fürs Warehouse)
    dest       text,                       -- IATA Ziel
    flight     text,                       -- IATA-Flugnummer
    row        jsonb       not null,
    tile       text,                       -- welche Kachel/Harvester (Debug)
    updated_at timestamptz not null default now(),
    constraint fr24_live_pkey primary key (hex)
);

-- Nachrüstung, falls die Tabelle schon ohne Route-Spalten existierte.
alter table public.fr24_live add column if not exists origin text;
alter table public.fr24_live add column if not exists dest   text;
alter table public.fr24_live add column if not exists flight text;

-- Warmer-Read-Index: „alle Flieger frischer als 6 min".
create index if not exists idx_fr24_live_updated_at
    on public.fr24_live(updated_at);
-- Callsign-Fallback-Lookup (Coverage-Loch, hex evtl. anders gemappt).
create index if not exists idx_fr24_live_callsign
    on public.fr24_live(callsign);

alter table public.fr24_live enable row level security;

-- Aufräum-Hilfe (optional per Cron): Einträge > 15 min sind tote Karteileichen.
-- delete from public.fr24_live where updated_at < now() - interval '15 minutes';
