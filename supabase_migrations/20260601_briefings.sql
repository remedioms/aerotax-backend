-- Briefings-Persistenz in Supabase (P0, 2026-06-01).
-- Vorher lebten beide Briefing-Quellen in ephemeral disk-Files unter
-- _USER_HISTORY_DIR:
--   · _USER_HISTORY_DIR/briefings/<token>.json  → iCal-Importe (ICS-Feed + EKEventStore)
--   · _USER_HISTORY_DIR/briefing_<token>.json   → User-PUT Briefings (manuelle Notizen)
-- Cloud-Run-Redeploy wipte den disk → User sah "keine Briefings im Kalender".
-- Pattern wie wall_posts/forum_threads (siehe 20260601_social.sql):
--   * SB-primary, Disk-Fallback, einmal-Lazy-Migrate beim ersten SB-leeren Read.
--   * Bekannte häufig-gefilterte Felder als eigene columns, Rest in jsonb.
--   * PK (token, datum) → idempotente Upserts.
--   * Service-Role-Key umgeht RLS (Anon-Client bleibt geblockt).

-- ─────────── ICAL-BRIEFINGS (iCal-Importe pro Datum) ───────────
create table if not exists public.user_ical_briefings (
    token         text         not null,
    datum         date         not null,
    ical_summary  text,
    ical_location text,
    ical_start    timestamptz,
    ical_end      timestamptz,
    raw_event     jsonb,
    updated_at    timestamptz  not null default now(),
    primary key (token, datum)
);
create index if not exists idx_ical_briefings_token
    on public.user_ical_briefings(token);
alter table public.user_ical_briefings enable row level security;

-- ─────────── MANUAL-BRIEFINGS (User-PUT pro Datum) ───────────
create table if not exists public.user_manual_briefings (
    token            text         not null,
    datum            date         not null,
    weather_summary  text,
    alternate_icao   text,
    mel_items        jsonb,
    remarks          text,
    extra            jsonb        not null default '{}'::jsonb,
    updated_at       timestamptz  not null default now(),
    primary key (token, datum)
);
create index if not exists idx_manual_briefings_token
    on public.user_manual_briefings(token);
alter table public.user_manual_briefings enable row level security;
