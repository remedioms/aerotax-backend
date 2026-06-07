-- Layover-Recs-Persistenz in Supabase (P0 Fix, 2026-06-07).
-- WURZEL-BUG: Layover-Tipps (recs), deren Up/Down-Votes und die Kommentare
-- lebten ALLE nur in ephemeral disk-Files unter _USER_HISTORY_DIR/layover_recs/:
--   · <IATA>.json              → die Tipps pro Airport
--   · votes_<token>.json       → die Up/Down-Votes pro User
--   · comments_<rec_id>.json   → die Kommentare pro Tipp
-- Cloud-Run-Redeploy / Instanz-Recycle wipte den disk → Tipps, Votes und
-- Kommentare verschwanden ("hab was gepostet aber es ist nicht geblieben").
--
-- Pattern wie wall_posts/forum_threads (siehe 20260601_social.sql):
--   * eine Tabelle pro Domain, SB-primary + Disk-Read-Cache.
--   * häufig gefilterte Felder als eigene columns, Rest in jsonb metadata.
--   * Service-Role-Key umgeht RLS (Anon-Client bleibt geblockt).
--   * idempotente Upserts via PK / on_conflict.

-- ─────────── LAYOVER-RECS (Tipps pro Airport) ───────────
create table if not exists public.layover_recs (
    id            text         primary key,
    iata          text         not null,
    category      text         not null default 'other',
    title         text,
    description   text,
    rating        smallint     not null default 0,
    price_band    text         not null default '',
    location_hint text,
    author_token  text         not null default '',
    author_short  text,
    ts            numeric      not null default 0,
    vote_score    int          not null default 0,
    vote_count    int          not null default 0,
    deleted       boolean      not null default false,
    metadata      jsonb        not null default '{}'::jsonb
);
create index if not exists idx_layover_recs_iata
    on public.layover_recs(iata) where deleted = false;
alter table public.layover_recs enable row level security;

-- ─────────── LAYOVER-REC-VOTES (Up/Down pro User+Rec) ───────────
-- Composite-PK (user_token, rec_id) → ein Vote pro User/Tipp, idempotent.
-- direction ∈ {-1, 1}. „kein Vote" = Row gelöscht (nicht direction=0).
create table if not exists public.layover_rec_votes (
    user_token text     not null,
    rec_id     text     not null,
    direction  smallint not null,
    created_at timestamptz not null default now(),
    primary key (user_token, rec_id),
    check (direction in (-1, 1))
);
create index if not exists idx_layover_rec_votes_user
    on public.layover_rec_votes(user_token);
create index if not exists idx_layover_rec_votes_rec
    on public.layover_rec_votes(rec_id);
alter table public.layover_rec_votes enable row level security;

-- ─────────── LAYOVER-REC-COMMENTS (Kommentare pro Tipp) ───────────
create table if not exists public.layover_rec_comments (
    id                text        primary key,
    rec_id            text        not null,
    author_token      text        not null default '',
    author_short      text,
    author_name       text,
    body              text,
    image_url         text,
    parent_comment_id text,
    ts                numeric     not null default 0,
    metadata          jsonb       not null default '{}'::jsonb
);
create index if not exists idx_layover_rec_comments_rec
    on public.layover_rec_comments(rec_id, ts);
alter table public.layover_rec_comments enable row level security;
