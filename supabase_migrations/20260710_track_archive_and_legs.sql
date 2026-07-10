-- Permanenz-DB (docs/data-permanence-plan.md, 2026-07-10) — Migration M1.
-- NICHT automatisch anwenden: Owner spielt sie zentral ein (SQL-Editor oder
-- DATABASE_URL wie bei der aircraft_live-DDL). Idempotent (Re-Run safe).
--
--  (1) flight_tracks_archive — Plan (c): Track-VERDICHTUNG statt Prune-Löschung.
--      /api/internal/track-compact verdichtet aircraft_track-Breadcrumbs, die
--      älter als RETENTION-2 Tage sind, per Douglas-Peucker auf ≤80 Punkte und
--      archiviert sie hier DAUERHAFT — erst danach darf track-prune die
--      Rohdaten löschen. Die geflogene Route geht nie mehr verloren.
--
--  (2) legs — Plan (b): kanonisches best-known Leg je (flight, service_date).
--      Tabelle jetzt anlegen; Write-Through (Phase 1) und legs-first-Reads
--      (Phase 2) folgen als separate Code-Schritte.

-- ── (1) Verdichtete geflogene Routen (permanent) ─────────────────────────────
create table if not exists public.flight_tracks_archive (
    reg          text        not null,          -- normalisiert wie aircraft_track (A-Z0-9)
    service_date date        not null,          -- UTC-Tag des ersten Leg-Punkts
    flight       text        not null default '',
                                                -- IATA-Flugnr (LH1558); Fallback wenn
                                                -- unbekannt: 'DEP-ARR' (z.B. 'FRA-JFK'),
                                                -- sonst '' — PK-Spalte darf nicht NULL sein
    dep          text,                          -- IATA Abflug (aus Breadcrumb origin)
    arr          text,                          -- IATA Ziel (aus Breadcrumb dest)
    points       jsonb       not null,          -- [[epoch,lat,lon,alt_ft,gs_kt], …]
                                                -- Douglas-Peucker ≤80 Punkte,
                                                -- lat/lon auf 4 Dezimalen (~11 m)
    pt_count     integer     not null default 0,  -- Punkte nach Verdichtung
    created_at   timestamptz not null default now(),
    primary key (reg, service_date, flight)     -- idempotent: Re-Run upsertet, dupliziert nie
);

create index if not exists idx_fta_flight_date
    on public.flight_tracks_archive (flight, service_date);
create index if not exists idx_fta_date
    on public.flight_tracks_archive (service_date);

alter table public.flight_tracks_archive enable row level security;
-- keine Policies: nur der Service-Role-Key (Backend/Cron) liest+schreibt.

-- ── (2) Kanonische Legs (best-known Fakten je flight+date, Plan (b)) ─────────
create table if not exists public.legs (
    flight        text not null,               -- IATA/OP-Flugnr normalisiert (LH1558)
    service_date  date not null,               -- Betriebstag station-lokal (Abflug)
    origin        text,
    dest          text,
    sched_dep     timestamptz,
    est_dep       timestamptz,
    act_dep       timestamptz,
    sched_arr     timestamptz,
    est_arr       timestamptz,
    act_arr       timestamptz,
    gate_dep      text,
    terminal_dep  text,
    gate_arr      text,
    status        text,
    cancelled     boolean default false,
    tail          text,
    hex           text,
    ac_type       text,
    delay_dep_min integer,
    delay_arr_min integer,
    -- Herkunft/Vertrauen PRO FAKT-GRUPPE:
    src_times     text,   -- 'board_obs' | 'warehouse' | 'fr24' | 'adsb_selfcomputed' | 'schedule'
    src_route     text,
    src_tail      text,
    confidence    smallint default 0,          -- 3=Board-IST, 2=Board-Soll/FR24, 1=inferiert
    updated_at    timestamptz default now(),
    primary key (flight, service_date)
);

create index if not exists idx_legs_tail_date on public.legs (tail, service_date);
create index if not exists idx_legs_date      on public.legs (service_date);

alter table public.legs enable row level security;
-- keine Policies: nur der Service-Role-Key (Backend/Cron) liest+schreibt.
