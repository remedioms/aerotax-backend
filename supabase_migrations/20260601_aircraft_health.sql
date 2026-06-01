-- Aircraft-Health Crowd-Reports (Worker W-USP, 2026-06-01).
--
-- Crews reichen tail-spezifische Reports ein (IFE row 24-30 broken, Galley
-- freezer warm, Toilet vac intermittent). Die naechste Crew die auf derselben
-- Tail-Reg fliegt sieht beim Boarding "3 Berichte letzter 7 Tage · Tap fuer
-- Details".
--
-- Schema-Entscheidungen:
--  · PK report_id (uuid) statt composite — Listing-Queries gehen
--    immer ueber `tail_reg + created_at`, lookup by PK ist selten.
--  · `reported_by_token` ist gespeichert (Spam-/Abuse-Tracking serverseitig)
--    aber NIE im Listing-Output gerendert (siehe blueprint).
--  · `system` + `severity` als CHECK statt FK auf Enum-Tabellen — Werte sind
--    stabil im App-Code (siehe AircraftHealthClient.SystemCategory/Severity).
--  · `status` default 'open' — spaeter koennte ein Maintenance-Mod einen
--    Report als 'resolved' markieren (Listing kappt das dann optional).
--  · Description-Cap 280 chars wie iOS/Server-Validation.

create table if not exists public.aircraft_health_reports (
    report_id           uuid          primary key default gen_random_uuid(),
    tail_reg            text          not null,
    system              text          not null,
    severity            text          not null,
    description         text          not null,
    reported_by_token   text          not null,
    status              text          not null default 'open',
    created_at          timestamptz   not null default now(),
    updated_at          timestamptz   not null default now(),
    check (system in ('ife', 'galley', 'cabin', 'lavatory', 'avionics', 'other')),
    check (severity in ('info', 'minor', 'major')),
    check (status   in ('open', 'resolved')),
    check (char_length(description) between 6 and 280),
    check (char_length(tail_reg) between 3 and 12)
);

-- Hot-path: Listing pro Tail in einem Zeitfenster.
create index if not exists idx_aircraft_health_tail_created
    on public.aircraft_health_reports(tail_reg, created_at desc);

-- Defensive: Wenn ein Token mehrere Reports am gleichen Tag fuer den gleichen
-- Tail einreicht (Abuse-Pattern), koennen wir das spaeter im Blueprint
-- detecten via diesem Index.
create index if not exists idx_aircraft_health_token_created
    on public.aircraft_health_reports(reported_by_token, created_at desc);

-- Service-Role-Key umgeht RLS. Anon-Client bleibt geblockt (Reports kommen
-- nur via Blueprint-Endpoint mit Token, nie direkt vom Client).
alter table public.aircraft_health_reports enable row level security;
