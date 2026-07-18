-- Per-Airline Crew-Hotel-Verzeichnis (dauerhafter Weg, ersetzt die im iOS-Bundle
-- hartcodierte, rein-Lufthansa Standardliste). Jede Airline hat eigene Crewhotels;
-- Defaults kommen ab jetzt vom Server → falsche/veraltete Hotels (YUL Delta→Sofitel,
-- BOM alt) sind live korrigierbar OHNE App-Update. Crew meldet Korrekturen als
-- `suggested`, der Owner bestätigt sie zu `approved` (kein Vandalismus-Risiko).
create table if not exists crew_hotel_directory (
    id           uuid primary key default gen_random_uuid(),
    airline      text not null,                    -- UPPER, z.B. 'LUFTHANSA' / 'SWISS'
    iata         text not null,                    -- UPPER, 3 Buchstaben
    base         text,                             -- 'FRA' | 'MUC' | null (alle Bases)
    hotel        text not null,
    transfer_min int  not null default 0,          -- 0 = fußläufig
    status       text not null default 'suggested',-- 'approved' | 'suggested'
    suggested_by text,                             -- Token-Hash (nie roher Token)
    votes        int  not null default 1,
    active       boolean not null default true,    -- false = vom neueren Eintrag abgelöst
    created_at   timestamptz default now(),
    updated_at   timestamptz default now()
);

-- Serve-Pfad: approve+active pro Airline.
create index if not exists idx_crew_hotel_dir_serve
    on crew_hotel_directory (airline, status, active);
-- Suggest-Dedup + Approve-Supersede: (airline, iata, base).
create index if not exists idx_crew_hotel_dir_station
    on crew_hotel_directory (airline, iata);
