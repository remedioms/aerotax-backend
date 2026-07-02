-- Durability-Lücken schliessen (P1, 2026-07-02).
--
-- Zwei User-Datenquellen lebten NUR auf ephemeral Cloud-Run-Disk
-- (_USER_HISTORY_DIR) und wurden bei JEDEM Redeploy/Container-Restart gewipt:
--   · flightops_<token>.json        → Per-Flight-Operational-Details (getippt)
--   · voice_notes/<token>/<datum>.m4a → Voice-Note-Aufnahmen pro Diensttag
--
-- Pattern wie user_manual_briefings (siehe 20260601_briefings.sql) + Avatare:
--   * flight-ops:  SB-primary (jsonb), Disk-Fallback, Lazy-Migrate.
--   * voice-notes: Bytes → R2 (wie Avatare, Zero-Egress), Metadata hier in SB.
--   * PKs erlauben idempotente Upserts. Service-Role-Key umgeht RLS.
--
-- WICHTIG: Der App-Code degradiert auf Disk (flight-ops) bzw. R2+Disk (voice)
-- solange diese Tabellen NICHT existieren — PostgREST PGRST205 ("Could not find
-- the table ... in the schema cache") wird abgefangen und einmal geloggt. Diese
-- Migration im Supabase-SQL-Editor anwenden, dann ist die Persistenz durabel.

-- ─────────── FLIGHT-OPS (Per-Flight-Operational-Details pro Datum) ───────────
create table if not exists public.user_flight_ops (
    token       text         not null,
    datum       date         not null,
    ops         jsonb        not null default '{}'::jsonb,
    updated_at  timestamptz  not null default now(),
    primary key (token, datum)
);
create index if not exists idx_user_flight_ops_token
    on public.user_flight_ops(token);
alter table public.user_flight_ops enable row level security;

-- ─────────── VOICE-NOTES (Metadata; Audio-Bytes liegen in R2) ───────────
create table if not exists public.user_voice_notes (
    token       text         not null,
    day_key     date         not null,
    r2_key      text         not null,
    mime        text         not null default 'audio/mp4',
    size_bytes  integer      not null default 0,
    created_at  timestamptz  not null default now(),
    primary key (token, day_key)
);
create index if not exists idx_user_voice_notes_token
    on public.user_voice_notes(token);
alter table public.user_voice_notes enable row level security;
