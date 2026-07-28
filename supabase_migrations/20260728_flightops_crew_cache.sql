-- Last-Good-Cache der LH-FlightOps-Crewlisten, EINE Zeile pro Leg.
--
-- Vorher lag der Cache im Profil-Mirror (metadata.flightops_crew_cache) und
-- war dort auf 8 Legs gedeckelt — höher ging nicht, weil der Profil-Blob auf
-- nahezu jedem Request gelesen wird (~1,4 KB je Leg). Folge (Owner-Befund
-- 2026-07-28, live am Owner-Token nachgestellt): ein frisch angesehenes Leg
-- verdrängte ein KÜNFTIGES (LH454/28.07. warf LH455/30.07. raus), und beim
-- nächsten Tap stand die Crew-Fläche leer bzw. antwortete 401/404/502.
-- Die Durabilität war NIE das Problem: die Profil-Einträge vom 24.07. haben
-- alle Deploys überlebt.
--
-- Eigene Tabelle: wird ausschliesslich im Crew-Endpoint gelesen, deckelt nach
-- FLUGDATUM (120 Tage) statt nach Anzahl. Fehlt sie, degradiert der Code
-- sauber auf den alten Profil-Cache (kein Hard-Fail); beim ersten Read nach
-- dem Anlegen hebt eine Lazy-Migration den Alt-Bestand hoch und leert den
-- Profil-Key.
--
-- Anwenden: dieses SQL im Supabase SQL-Editor ausführen.

create table if not exists public.flightops_crew_cache (
    token       text not null,
    flight      text not null,          -- normalisiert, z. B. 'LH454'
    flight_date text not null,          -- YYYY-MM-DD (Abflugdatum lt. Roster)
    crew        jsonb not null,         -- [{position,name,duty,pk,aerox?}, …]
    cached_at   double precision not null default extract(epoch from now()),
    primary key (token, flight, flight_date)
);

-- Prune-Pfad: delete where token = ? and flight_date < ?
create index if not exists flightops_crew_cache_prune_idx
    on public.flightops_crew_cache (token, flight_date);

-- Nur der Service-Role-Key (Backend) greift zu — RLS an, keine anon-Policy.
-- Inhalt ist PII (Klarnamen fremder Crew-Mitglieder).
alter table public.flightops_crew_cache enable row level security;

comment on table public.flightops_crew_cache is
  'Last-good LH FlightOps crew lists, one row per (user, flight, date). Written by /api/lh/flightops/crewlist on every successful fetch; read when the grant is dead/pending, the accessCode is unresolvable, LH is down or LH returns an empty list. Pruned by flight_date (120d). Replaces metadata.flightops_crew_cache (8-entry cap) since 2026-07-28.';
