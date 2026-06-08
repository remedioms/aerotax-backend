-- "Crew at my Destination" — Layover-Sichtbarkeit (Opt-in) + manuelle Pins.
-- Feature 2026-06-08: zwei Modi —
--   A) Layover-basiert (automatisch): wer von meinen Friends hat ±24h einen
--      Layover an DERSELBEN Station wie ich → Avatare auf der Crew Map.
--   B) Manueller Pin: „ich will hier am <Datum> was machen" → nur für
--      gegenseitige Friends sichtbar.
--
-- Pattern wie layover_recs/wall_posts (20260607_layover_recs.sql,
-- 20260601_social.sql): SB-primary, Service-Role umgeht RLS, idempotente
-- Upserts via PK/on_conflict. Häufig gefilterte Felder als columns.

-- ─────────── LAYOVER-VISIBILITY (Opt-in, default an) ───────────
-- Ein Row pro User. enabled=true heißt: meine Layover dürfen Friends auf der
-- Crew Map als Match sehen. Fehlt der Row → Default „an" (Opt-in default-on),
-- wird im Backend (_layover_visibility_get) so behandelt; ein Row entsteht erst
-- wenn der User den Toggle aktiv umlegt.
create table if not exists public.layover_visibility (
    user_token text        primary key,
    enabled    boolean     not null default true,
    updated_at timestamptz not null default now()
);
alter table public.layover_visibility enable row level security;

-- ─────────── MANUAL-PINS (Intent-Pins „hier will ich was machen") ───────────
-- Sichtbar NUR für gegenseitige Friends (Filter im Backend über die
-- user_friends-Kanten). lat/lng werden beim Anlegen aus iata_code aufgelöst
-- (airports_compact.json) oder direkt vom Client (Map-Tap) mitgegeben.
create table if not exists public.manual_pins (
    id         text        primary key,
    user_token text        not null,
    iata_code  text        not null default '',
    lat        double precision,
    lng        double precision,
    pin_date   date,
    note       text,
    created_at timestamptz not null default now()
);
create index if not exists idx_manual_pins_user
    on public.manual_pins(user_token);
create index if not exists idx_manual_pins_iata
    on public.manual_pins(iata_code);
alter table public.manual_pins enable row level security;
