-- ====================================================================
--  Crewbus-Durability (2026-07-02).
--
--  Bug: die crowdsourcte Crewbus-Transferzeit (Flughafen <-> Crew-Hotel) lebte
--  faktisch nur In-Memory (_CREWBUS_MEM) plus ein best-effort-Rollup-Upsert in
--  `ax_crewbus` (iata PK, payload jsonb). Auf Cloud Run ist der Speicher
--  PER-INSTANCE und wird bei jedem Restart/Deploy geleert -> der Pool
--  akkumuliert nie, die App zieht immer einen leeren Schnitt.
--
--  Fix: eine APPEND-ONLY Beobachtungs-Tabelle als SOURCE OF TRUTH. Jede
--  Crew-Eingabe = eine Zeile. Der Schnitt wird ueber alle Zeilen einer Station
--  aus Supabase berechnet (Memory-Cache nur noch als kurzlebiger Accelerator).
--  So ueberlebt der Pool Restarts und aggregiert ueber alle Instanzen.
--
--  Idempotent (`create ... if not exists`), ASCII-only. Backend schreibt mit
--  dem Service-Role-Key (umgeht RLS) -> RLS bleibt an, keine Policies noetig.
-- ====================================================================

create table if not exists public.ax_crewbus_obs (
    id          bigint generated always as identity primary key,
    iata        text        not null,
    anon_id     text,                       -- gehashtes Token (keine Klartext-Identitaet)
    direction   text        not null default 'transfer',
    minutes     integer     not null,       -- gemeldete Transferzeit (1..240)
    created_at  timestamptz not null default now()
);

-- Aggregations-Pfad: alle Zeilen einer Station, juengste zuerst.
create index if not exists ax_crewbus_obs_iata_idx
    on public.ax_crewbus_obs (iata, created_at desc);

-- Light-Dedup: schnelles Nachschlagen der letzten Eingabe je (anon_id, iata).
create index if not exists ax_crewbus_obs_anon_idx
    on public.ax_crewbus_obs (anon_id, iata, created_at desc);

alter table public.ax_crewbus_obs enable row level security;
