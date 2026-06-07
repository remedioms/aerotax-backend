-- Crews-on-Flight — "Wer ist heute/morgen noch auf meinem Flug?"
--
-- Cross-User-Index über (flight_number, date): findet andere opt-in-Crew die am
-- gleichen Datum auf der gleichen Flugnummer eingeteilt ist. Die Roster-Daten
-- liegen sonst nur per-Token (roster_snapshot_<token>.json bzw. per-Token SB-
-- Rows) — es gibt KEINEN Cross-User-Index auf (flight_number, date). Ein
-- Cross-User-Query müsste jeden User-Snapshot scannen. Darum eine dedizierte
-- Tabelle, gespeist aus dem bestehenden Roster-Ingest-Pfad (take_roster_snapshot
-- + Auto-Save nach Job-Done). Spiegelt das crew_edges-Muster (eigene SB-Tabelle
-- + Disk-Fallback + Opt-in-Gating).
--
-- Privacy:
--   · opt_in (default true) spiegelt das bestehende share_roster-Reziprozitäts-
--     Modell. Steht share_roster im Profil explizit auf false, wird opt_in beim
--     Upsert auf false gesetzt → der User taucht in KEINER Crew-Liste auf.
--   · Die Liste exponiert NUR display_name (= profile.name), base (= homebase)
--     und position — NIE Token oder exakte Location.
--   · self_token bleibt server-seitig (Service-Role), wird nie ausgespielt.
--
-- Composite-PK (self_token, flight_number, date): pro User + Flug + Tag genau
-- eine Zeile — verhindert Duplikate bei wiederholtem Roster-Snapshot. Re-Upsert
-- aktualisiert display_name/base/position/opt_in (Profil kann sich ändern).

create table if not exists public.crew_flight_assignments (
    self_token      text         not null,
    flight_number   text         not null,
    flight_date     date         not null,
    display_name    text,
    base            text,
    position        text,
    opt_in          boolean      not null default true,
    created_at      timestamptz  not null default now(),
    updated_at      timestamptz  not null default now(),
    primary key (self_token, flight_number, flight_date)
);

-- Hot-path: "wer ist noch auf <flight_number> am <flight_date>?" — nur opt-in.
create index if not exists idx_crew_flight_lookup
    on public.crew_flight_assignments(flight_number, flight_date)
    where opt_in = true;

-- Aufräum-Index: alte Assignments per Datum löschen (Retention-Job optional).
create index if not exists idx_crew_flight_date
    on public.crew_flight_assignments(flight_date);

-- Service-Role-Key umgeht RLS. Anon-Client bleibt geblockt (kein Read/Write) —
-- die opt-in-Liste wird ausschließlich server-seitig im Blueprint gefiltert und
-- token-frei ausgespielt.
alter table public.crew_flight_assignments enable row level security;
