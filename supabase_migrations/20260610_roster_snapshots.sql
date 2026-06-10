-- Roster-Snapshots: persistenter tage_detail-Stand pro User, damit Friends die
-- Roster auch über Cloud-Run-Instanz-Grenzen hinweg sehen (vorher: nur Disk der
-- jeweiligen Instanz → bei >1 Instanz/Container-Wipe sahen Friends leere Roster).
--
-- Multi-Agent-Review 2026-06-10, Welle A.
-- Anwenden: dieses SQL im Supabase SQL-Editor ausführen. Der Code degradiert
-- sauber auf Disk-only solange die Tabelle fehlt (kein Hard-Fail).

create table if not exists public.roster_snapshots (
    token       text primary key,
    payload     jsonb not null,                 -- {taken_at, tage:[...], auto_saved?}
    updated_at  timestamptz not null default now()
);

-- Nur der Service-Role-Key (Backend) greift zu — RLS an, keine anon-Policy.
alter table public.roster_snapshots enable row level security;

comment on table public.roster_snapshots is
  'Per-User tage_detail snapshot for cross-instance friend-roster visibility. Written by /api/user/roster-snapshot and the job pipeline; read by crew-at-destination / friend-roster.';
