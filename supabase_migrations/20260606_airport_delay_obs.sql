-- Airport-Delay-Beobachtungen (Pünktlichkeits-Persistenz, 2026-06-06).
--
-- PROBLEM: Die Pünktlichkeits-Quote wurde aus der LIVE-Abflugtafel berechnet —
-- die verschwindet, sobald der Flieger weg ist. Ohne diese Tabelle degradierte
-- die Write-Through-/Load-on-Read-Persistenz still auf rein In-Memory (geht beim
-- Cloud-Run-Restart verloren), also rechnete die Quote effektiv nur die gerade
-- sichtbare Tafel. Diese Tabelle hält die Beobachtungen pro Betriebstag fest, so
-- dass die Tages-Stichprobe einen Restart/Instanz-Wechsel überlebt und wächst.
--
-- Befüllt aus `_merge_into_delay_store` → `_delay_obs_write_through` (best-effort
-- Upsert pro geänderter Beobachtung). Gelesen via `_delay_store_load_from_sb`
-- (Load-on-Read pro Betriebstag+Airport in `_punctuality_stats`).
--
-- Schema entspricht exakt dem Upsert-Payload im Backend. Unique-Key ist
-- (date, airport, flight, sched) — airport MUSS im Key sein, sonst kollidieren
-- MUC/BER/FRA auf demselben Flug+Zeit+Tag und überschreiben sich gegenseitig.

create table if not exists public.airport_delay_obs (
    date          text        not null,   -- Betriebstag 'YYYY-MM-DD' (FRA-lokal)
    airport       text        not null,   -- 'FRA' / 'MUC' / 'BER' …
    flight        text        not null,   -- Flugnummer, z.B. 'LH401'
    sched         text        not null,   -- geplante Abflugzeit 'HH:MM'
    max_delay_min integer     not null default 0,
    cancelled     boolean     not null default false,
    status        text,
    updated_at    timestamptz not null default now(),
    primary key (date, airport, flight, sched)
);

-- Schneller Tages-Load (Load-on-Read filtert date + airport).
create index if not exists idx_airport_delay_obs_date_airport
    on public.airport_delay_obs(date, airport);

-- TTL-Cleanup-Helfer: nach 7 Tagen sind alte Betriebstage uninteressant
-- (`delete from airport_delay_obs where date < to_char(now() - interval '7 days','YYYY-MM-DD')`).
create index if not exists idx_airport_delay_obs_date
    on public.airport_delay_obs(date);

-- Service-Role (Backend) schreibt/liest; Anon-Client bleibt geblockt.
alter table public.airport_delay_obs enable row level security;
