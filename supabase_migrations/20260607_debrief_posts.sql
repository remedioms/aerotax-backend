-- Community-Debrief-Feed-Persistenz in Supabase (P0 Data-Loss-Fix, 2026-06-07).
-- WURZEL-BUG: Die Tabelle public.debrief_posts existierte in KEINER Migration.
-- _debrief_sb_insert/_debrief_sb_set_upvotes schrieben gegen eine nicht-
-- existente Tabelle → jeder SB-Write schlug still fehl, und nur der ephemere
-- Disk-Spiegel (_USER_HISTORY_DIR/news_history/debrief_posts.json) überlebte.
-- Cloud-Run-Redeploy / Instanz-Recycle wipte den Disk → jeder Debrief-Post,
-- jeder Upvote war weg ("hab was gepostet aber es ist nicht geblieben").
--
-- Pattern wie wall_posts/layover_recs (siehe 20260601_social.sql /
-- 20260607_layover_recs.sql):
--   * eine Tabelle pro Domain, SB-primary + Disk-Read-Cache + lazy-migrate.
--   * häufig gefilterte Felder als eigene columns, Rest in jsonb.
--   * Service-Role-Key umgeht RLS (Anon-Client bleibt geblockt).
--   * idempotente Upserts via PK / on_conflict='id'.
--
-- Schema spiegelt 1:1 die row-dict-Felder aus news_blueprint.post_news_debrief:
--   id, author_token_hash, pseudonym, poster_role, body, hashtags, upvotes,
--   upvoters, comment_count, deleted, created_at, updated_at.
--
-- Schema-Entscheidungen:
--  · PK auf id (uuid4().hex als text) — Backend generiert die ID, on_conflict
--    macht Insert + lazy-migrate kollisionsfrei.
--  · author_token_hash text — NIE das Klartext-Token; nur der Hash für
--    Rate-Limit-/Idempotenz-Zwecke. RLS + Service-Role schützen Cross-User.
--  · poster_role nullable — iOS posterRole: String? (Allowlist-gefiltert).
--  · hashtags jsonb — Allowlist-Tags, max 5; iOS bekommt sie als Array.
--  · upvoters jsonb-Array von Upvoter-Hashes (kein Klartext) — die Upvote-
--    Toggle-Logik liest/schreibt die ganze Liste; daher als jsonb-column statt
--    eigener Votes-Tabelle (anders als layover_recs, weil hier ein anonymer
--    Hash und keine user_token-Composite-PK existiert).
--  · upvotes int als denormalisierter Zähler = len(upvoters), vom Backend
--    konsistent mitgeschrieben (Last-Writer via updated_at).
--  · comment_count int default 0 — Platzhalter für künftige Kommentare.
--  · deleted boolean Soft-Delete — die Read-Query filtert deleted=false.
create table if not exists public.debrief_posts (
    id                text         primary key,
    author_token_hash text         not null default '',
    pseudonym         text         not null default 'Crew',
    poster_role       text,
    body              text         not null default '',
    hashtags          jsonb        not null default '[]'::jsonb,
    upvotes           int          not null default 0,
    upvoters          jsonb        not null default '[]'::jsonb,
    comment_count     int          not null default 0,
    deleted           boolean      not null default false,
    created_at        timestamptz  not null default now(),
    updated_at        timestamptz  not null default now()
);

-- Häufigste Query: Feed = "nicht-gelöschte Posts, neueste zuerst" mit
-- created_at-Cursor für Infinite-Scroll (_debrief_sb_list).
create index if not exists idx_debrief_posts_feed
    on public.debrief_posts(created_at desc) where deleted = false;

alter table public.debrief_posts enable row level security;
