-- Roster-Verlauf (pending/history der Dienstplan-Änderungen): persistenter
-- Stand pro User. Vorher lag der Verlauf NUR im ungemounteten Container-Layer
-- (_user_history_state/roster_changes_<token>.json) → JEDER Deploy wischte
-- pending + history. Sichtbare Folgen: „Push bekommen, aber in der App keine
-- Änderung gefunden" (Julia 2026-07-27) und leerer Verlauf nach jedem Update.
--
-- Owner-Auftrag 2026-07-27 („beides ultra wichtig … 3k Nutzer").
-- Anwenden: dieses SQL im Supabase SQL-Editor ausführen. Der Code degradiert
-- sauber auf Disk-only solange die Tabelle fehlt (kein Hard-Fail); beim
-- ersten Read nach Anlegen hebt eine Lazy-Migration den Disk-Bestand hoch.

create table if not exists public.roster_changes (
    token       text primary key,
    payload     jsonb not null,                 -- {pending:[...], history:[...]}
    updated_at  timestamptz not null default now()
);

-- Nur der Service-Role-Key (Backend) greift zu — RLS an, keine anon-Policy.
alter table public.roster_changes enable row level security;

comment on table public.roster_changes is
  'Per-user roster change log (pending + decided history). Written by /api/user/roster-snapshot diffing and /roster-changes/decide; read by /api/user/roster-changes. Survives deploys (was container-layer JSON before 2026-07-27).';
