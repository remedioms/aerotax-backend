-- Durabler LH-FlightOps-Link-Cache (accessCode-Referenzen), EINE Zeile pro Token.
--
-- Vorher lag der Cache NUR als `folinks_<token>.json` auf der ungemounteten
-- Container-Disk (`Mounts: []`) und war nach JEDEM Deploy leer. Folge:
-- 404-`no_access_code`-Wellen (Log 28.07. 06:00:35), bis jeder User sein
-- Tages-Fenster einmal live nachgeladen hatte — und genau dieser Nachlade-
-- Call fiel im Hintergrund zusätzlich unterm Key-Budget-Deckel aus.
--
-- Muster wie flightops_crew_cache (20260728): die Tabelle ist der durable
-- Spiegel, die Disk bleibt der heiße Lesepfad (ein SB-Treffer rehydriert sie
-- nach dem Deploy einmalig). Fehlt die Tabelle oder ist SB weg, degradiert
-- der Code aufs alte Disk-Verhalten — fail-open, kein Hard-Fail.
--
-- Inhalt: [{service, params}, …] (max 800, vom Code gekappt) — die params
-- tragen accessCodes für Crewlist/Check-in/Landing-Report. accessCodes sind
-- Credentials-Äquivalente → RLS an, kein anon-Zugriff, nur Service-Role.
--
-- Anwenden: dieses SQL im Supabase SQL-Editor ausführen (macht Owner/Deploy —
-- NICHT aus dem Code heraus).

create table if not exists public.flightops_links_cache (
    token text primary key,
    links jsonb not null,               -- [{service, params:{accessCode,…}}, …]
    ts    double precision not null default extract(epoch from now())
);

-- Nur der Service-Role-Key (Backend) greift zu — RLS an, keine anon-Policy.
alter table public.flightops_links_cache enable row level security;

comment on table public.flightops_links_cache is
  'Durable mirror of the per-user LH FlightOps duty-links cache (folinks_<token>.json). One jsonb row per token with the [{service, params}] list (accessCode refs for crewlist/checkin/landingreport). Written on every _links_save; read once per container/token when the local disk cache is empty (fresh deploy). Replaces the deploy-volatile disk-only cache since 2026-08-18.';
