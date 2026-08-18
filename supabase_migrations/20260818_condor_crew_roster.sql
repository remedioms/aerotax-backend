-- Condor-Crewliste serverseitig — EINE Zeile pro (token, Flugdatum, Flug).
--
-- OWNER-ENTSCHEIDUNG 18.08.2026 (woertlich): „warum lokal.. dann ist es nicht
-- wie bei LH sollte der ausfallen haben wir es im backend und koennen so ihr
-- den immer laden.. auch bei app loeschung und neue installation und so..
-- angeben wer bei aero x als crew ist etc etc hotel auch mit bewertungen und
-- alles alles sicher gespeichert wie bei LH im backend".
--
-- Damit ist die bis 17.08. geltende Regel „Condor-Crew bleibt strikt lokal"
-- ersetzt. Vorher lag die aus dem iCal-DESCRIPTION gelesene Besatzung nur in
-- UserDefaults (`aerox_condor_local_roster_v1.<hash>`): nach App-Loeschung,
-- Geraetewechsel oder einem Ausfall des cube.aero-Feeds war sie weg — genau
-- das, was es bei Lufthansa (FlightOps + flightops_crew_cache) nicht gibt.
--
-- INHALT
--   crew  jsonb  [{role, name, staff_no?, base?}, …] (max 30, vom Code gekappt)
--   source text  'server_ics' (URL-Import/Auto-Refresh, Rohtext lag serverseitig
--                vor) | 'device_ics' (alter App-Build sendete Rohtext) |
--                'device_structured' (neue App laedt die LOKAL geparste Crew
--                ueber /api/ax/condor/crew/upload hoch).
--                PFLICHTFELD — es gibt keinen Crew-Eintrag ohne Quelle
--                (Owner-Regel „keine Fake-Werte").
--
-- PERSONALNUMMERN (staff_no) SIND HIER ERLAUBT und beabsichtigt. Sie sind bei
-- Condor exakt das, was `lh_pk_number` bei Lufthansa ist: das einzige
-- belastbare Match-Kriterium fuer „wer aus dieser Crew ist selbst auf AeroX".
-- Der Namens-Match ist bewusst NICHT im Einsatz (Marco-C.-Vorfall 30.07.: aus
-- einem abgekuerzten Namen laesst sich keine Identitaet ableiten). Die
-- Nummer wird NIE ausgeliefert — sie bleibt intern; die API gibt pro
-- Mitglied nur {role, name, aerox?} zurueck (gleiche Form wie die
-- LH-Crewliste).
--
-- WAS HIER NICHT LANDET (unveraendert seit 08.08.):
--   * der ROHE DESCRIPTION-Text (`_condor_ics_privacy_sanitize` laeuft
--     weiterhin vor JEDEM Parser-/Persistenzpfad des Kalenders),
--   * Hotel-ADRESSE und Telefonnummer,
--   * alles, was nicht Rolle/Name/Personalnummer/Basis ist.
--
-- AUSLIEFERUNG ist fail-closed: `/api/ax/condor/crew` gibt eine Zeile nur an
-- einen Token heraus, der GENAU DIESEN Flug an DIESEM Tag im EIGENEN, echt
-- importierten Roster stehen hat (`_condor_roster_has_leg`, Gate-Muster wie
-- `_condor_roster_hotel_iatas`). Fremd-/Family-Roster kommen nie durch.
--
-- Der Inhalt sind Daten ueber Kolleginnen und Kollegen (Name + Personalnummer)
-- → RLS an, KEINE anon-Policy, Grants weg. Nur der Service-Role-Client des
-- Backends liest/schreibt hier.
--
-- Anwenden: dieses SQL im Supabase SQL-Editor ausfuehren (macht Owner/Deploy —
-- NICHT aus dem Code heraus). NICHT ANGEWENDET.

create table if not exists public.condor_crew_roster (
    token       text        not null,
    flight_date date        not null,
    flight      text        not null,   -- normalisiert, z.B. 'DE2360'
    crew        jsonb       not null,   -- [{role,name,staff_no?,base?}, …]
    dep         text,
    arr         text,
    source      text        not null,
    updated_at  timestamptz not null default now(),
    primary key (token, flight_date, flight)
);

-- Der Serve-Pfad liest immer (token, flight_date, flight) — das ist der PK.
-- Zusaetzlich ein Datums-Index fuer Aufraeum-/Retention-Laeufe.
create index if not exists condor_crew_roster_date_idx
    on public.condor_crew_roster (flight_date);

alter table public.condor_crew_roster enable row level security;

-- `anon` und `authenticated` erben PUBLIC-Rechte — die auch entziehen, damit
-- der Schutz nicht allein am RLS-Flag haengt (Muster 20260817_user_data_
-- tables_rls_guard.sql).
revoke all on table public.condor_crew_roster from public, anon, authenticated;

comment on table public.condor_crew_roster is
  'Server-side Condor crew lists, one row per (token, flight_date, flight). Structured only: [{role,name,staff_no?,base?}] parsed from the roster iCal DESCRIPTION before sanitising (server fetch) or uploaded pre-parsed by the device (POST /api/ax/condor/crew/upload). staff_no is the Condor equivalent of lh_pk_number and is the ONLY match criterion for the aerox flag; it is never returned by the API. Raw DESCRIPTION text, hotel address and phone number are never stored. Delivery is fail-closed: the requesting token must have that exact flight on that date in its own imported roster. Owner decision 2026-08-18.';

notify pgrst, 'reload schema';
