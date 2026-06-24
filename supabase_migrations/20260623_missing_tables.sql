-- ====================================================================
--  Fehlende Supabase-Tabellen nachziehen (2026-06-23, v1.1-Audit).
--
--  Diese Tabellen werden vom Backend gelesen/geschrieben, hatten aber bisher
--  KEINE create-table-Definition im Repo (sie wurden — wenn überhaupt — manuell
--  im Supabase-SQL-Editor angelegt). Ohne sie degradiert der jeweilige Write
--  still (best-effort try/except) → die Daten leben nur ephemer / In-Memory und
--  verschwinden beim Cloud-Run-Restart. Alle Statements sind idempotent
--  (`create table if not exists`) und ASCII-only.
--
--  Das Backend schreibt mit dem Service-Role-Key (umgeht RLS) — RLS bleibt
--  aktiviert, es werden keine zusätzlichen Policies benötigt.
-- ====================================================================

-- ── ax_crewbus ──────────────────────────────────────────────────────────────
-- Crowdsourced Crewbus-Transferzeiten (Flughafen -> Crew-Hotel), pro IATA.
-- KNOWN GAP (MEMORY: "Supabase-Tabelle ax_crewbus fuer Durabilitaet anlegen").
-- _crewbus_get/_crewbus_put in blueprints/aerox_data_blueprint.py erwartet
-- key=iata + payload jsonb {minutes:[...]}. Ohne diese Tabelle lebt der
-- Crewbus-Schnitt nur In-Memory (_CREWBUS_MEM) und ist nach jedem Restart weg.
create table if not exists public.ax_crewbus (
    iata        text         primary key,
    payload     jsonb        not null default '{}'::jsonb,
    updated_at  timestamptz  not null default now()
);
alter table public.ax_crewbus enable row level security;

-- ── aircraft_age ────────────────────────────────────────────────────────────
-- AeroX-eigene, wachsende Flieger-Alters-DB (#127). Ein AeroDataBox-Lookup pro
-- Hex, danach geteilter Cache. Schema 1:1 wie der Upsert in app.py:aircraft_age.
create table if not exists public.aircraft_age (
    hex         text         primary key,
    year        integer,
    built_date  text,         -- tagesgenaues Baujahr YYYY-MM-DD (AeroDataBox), v1.1
    reg         text,
    type        text,
    updated     timestamptz  not null default now()
);
alter table public.aircraft_age enable row level security;
-- Falls die Tabelle bereits existierte (ohne die Spalte): idempotent nachruesten.
alter table public.aircraft_age add column if not exists built_date text;

-- ── community_stats ─────────────────────────────────────────────────────────
-- AeroX-weiter Benchmark (#111). Token gehasht (keine Klartext-Identitaet).
-- Schema 1:1 wie der Upsert in app.py (_community_stats_upsert).
create table if not exists public.community_stats (
    token_hash   text         primary key,
    hours_flown  float8,
    flights      integer,
    countries    integer,
    tour_days    integer,
    distance_km  float8,
    updated      timestamptz  not null default now()
);
alter table public.community_stats enable row level security;

-- ── support_requests ────────────────────────────────────────────────────────
-- Support-/Kontaktanfragen aus dem Frontend. Insert in app.py (Kontakt-Endpoint),
-- Read im Admin-Endpoint (order by created_at desc).
create table if not exists public.support_requests (
    id          bigint       generated always as identity primary key,
    reason      text,
    email       text,
    phone       text,
    message     text,
    ip_hash     text,
    created_at  timestamptz  not null default now()
);
alter table public.support_requests enable row level security;
create index if not exists idx_support_requests_created
    on public.support_requests (created_at desc);

-- Done.
