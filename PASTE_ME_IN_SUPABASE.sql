-- ────────────────────────────────────────────────────────────────────────
-- AEROTAX/AERIS — Konsolidierte Migrations für Supabase Dashboard
-- Stand: 2026-06-01
-- 
-- Anleitung:
-- 1. Supabase Dashboard öffnen: https://app.supabase.com/project/jyrbijvmwacuivssbxlg
-- 2. SQL Editor → New query
-- 3. KOMPLETTEN Inhalt dieser Datei einfügen + Run
-- 4. Erfolg prüfen: alle Tabellen erscheinen unter Table Editor
--
-- Idempotent: alle CREATE TABLE/INDEX nutzen IF NOT EXISTS,
-- ein zweiter Run macht keinen Schaden.
-- ────────────────────────────────────────────────────────────────────────


-- ╔══════════════════════════════════════════════════════════════════════╗
-- ║ Block 1/4 — user_profiles + user_friends + user_push_tokens         ║
-- ╚══════════════════════════════════════════════════════════════════════╝
-- User-Daten in Supabase persistieren (P0-Fix Phase 2): Profile + Friends +
-- Push-Tokens. Vorher lebten profile_<token>.json, friends_<token>.json,
-- push_<token>.json auf Container-ephemeral-disk und verschwanden bei jedem
-- Cloud-Run-Redeploy. Reihenfolge: 20260531_auth_users.sql zuerst.

-- ── Profile ──────────────────────────────────────────────────────────
-- Pro Token genau eine Zeile mit den User-Profilfeldern.
-- `employers` als jsonb (Multi-Employer aus A4). `metadata` für alle
-- Felder die nicht im Whitelist sind aber persistiert werden sollen.
create table if not exists public.user_profiles (
    token        text         primary key,
    name         text,
    homebase     text,
    airline      text,
    "position"   text,
    hometown     text,
    share_roster boolean      not null default false,
    employers    jsonb        not null default '[]'::jsonb,
    metadata     jsonb        not null default '{}'::jsonb,
    created_at   timestamptz  not null default now(),
    updated_at   timestamptz  not null default now()
);
create index if not exists idx_user_profiles_homebase
    on public.user_profiles(homebase) where homebase is not null;
create index if not exists idx_user_profiles_airline
    on public.user_profiles(airline) where airline is not null;
alter table public.user_profiles enable row level security;


-- ── Friends ──────────────────────────────────────────────────────────
-- Eine Zeile = eine gerichtete Buddy-Beziehung (owner sieht friend).
-- Reziproke Freundschaft = 2 Zeilen (a→b und b→a) — vereinfacht Queries.
create table if not exists public.user_friends (
    owner_token   text         not null,
    friend_token  text         not null,
    status        text         not null default 'accepted',
    created_at    timestamptz  not null default now(),
    metadata      jsonb        not null default '{}'::jsonb,
    primary key (owner_token, friend_token),
    check (status in ('pending', 'accepted', 'blocked'))
);
create index if not exists idx_user_friends_friend_token
    on public.user_friends(friend_token);
create index if not exists idx_user_friends_owner_status
    on public.user_friends(owner_token, status);
alter table public.user_friends enable row level security;


-- ── Push-Tokens ──────────────────────────────────────────────────────
-- Routing von app-Token → APNs/Expo-Token. Pro User-Token ein Eintrag.
-- Bei Re-Install ein neuer device_token, alter wird via updated_at sichtbar.
create table if not exists public.user_push_tokens (
    user_token   text         primary key,
    expo_token   text,
    apns_token   text,
    device_id    text,
    platform     text,
    metadata     jsonb        not null default '{}'::jsonb,
    updated_at   timestamptz  not null default now()
);
create index if not exists idx_push_expo on public.user_push_tokens(expo_token) where expo_token is not null;
create index if not exists idx_push_apns on public.user_push_tokens(apns_token) where apns_token is not null;
alter table public.user_push_tokens enable row level security;


-- ╔══════════════════════════════════════════════════════════════════════╗
-- ║ Block 2/4 — Wall + Forum + DM (Posts/Threads/Replies/Likes/...)     ║
-- ╚══════════════════════════════════════════════════════════════════════╝
-- Social-Persistenz in Supabase: Wall-Posts, Forum-Threads/Replies, DM-Messages.
-- Vorher lebten alle drei in ephemeral disk-Files unter _USER_HISTORY_DIR (wall/
-- posts.json, forum/threads.json + replies_<id>.json, chat/<channel>.json).
-- Cloud-Run-Redeploy wipte den disk → ALLE Posts/Threads/DMs weg pro Release.
-- Bei 5000 User unbenutzbar.
--
-- Pattern wie auth_users + user_profiles (siehe 20260531_*.sql):
--   * eine Tabelle pro Domain
--   * häufig gefilterte Felder als eigene columns
--   * jsonb metadata für unknown/extension keys
--   * Service-Role-Key umgeht RLS (Anon-Client bleibt geblockt)

-- ─────────── WALL-POSTS ───────────
create table if not exists public.wall_posts (
    id text primary key,
    author_token text not null,
    ts numeric not null,
    body text,
    layover_iata text,
    image_url text,
    hashtags jsonb not null default '[]',
    like_count int not null default 0,
    comment_count int not null default 0,
    deleted boolean not null default false,
    metadata jsonb not null default '{}'
);
create index if not exists idx_wall_posts_ts
    on public.wall_posts(ts desc) where deleted = false;
create index if not exists idx_wall_posts_author
    on public.wall_posts(author_token);
alter table public.wall_posts enable row level security;

-- ─────────── WALL-LIKES ───────────
create table if not exists public.wall_likes (
    post_id text not null,
    user_token text not null,
    created_at timestamptz not null default now(),
    primary key (post_id, user_token)
);
alter table public.wall_likes enable row level security;

-- ─────────── WALL-COMMENTS ───────────
create table if not exists public.wall_comments (
    id text primary key,
    post_id text not null,
    author_token text not null,
    body text,
    ts numeric not null,
    parent_id text,
    image_url text,
    metadata jsonb not null default '{}'
);
create index if not exists idx_wall_comments_post
    on public.wall_comments(post_id, ts);
alter table public.wall_comments enable row level security;

-- ─────────── FORUM-THREADS ───────────
create table if not exists public.forum_threads (
    id text primary key,
    category_id text not null,
    author_token text not null,
    title text,
    body text,
    ts numeric not null,
    hashtags jsonb not null default '[]',
    like_count int not null default 0,
    reply_count int not null default 0,
    deleted boolean not null default false,
    metadata jsonb not null default '{}'
);
create index if not exists idx_forum_threads_cat_ts
    on public.forum_threads(category_id, ts desc);
alter table public.forum_threads enable row level security;

-- ─────────── FORUM-REPLIES ───────────
create table if not exists public.forum_replies (
    id text primary key,
    thread_id text not null,
    author_token text not null,
    body text,
    ts numeric not null,
    parent_reply_id text,
    mentioned_token text,
    image_url text,
    like_count int not null default 0,
    metadata jsonb not null default '{}'
);
create index if not exists idx_forum_replies_thread
    on public.forum_replies(thread_id, ts);
alter table public.forum_replies enable row level security;

-- ─────────── DM-MESSAGES ───────────
create table if not exists public.dm_messages (
    id text primary key,
    channel_id text not null,
    author_token text not null,
    body text,
    ts numeric not null,
    image_url text,
    metadata jsonb not null default '{}'
);
create index if not exists idx_dm_messages_channel
    on public.dm_messages(channel_id, ts);
alter table public.dm_messages enable row level security;

-- ─────────── FORUM-LIKES (per user) ───────────
-- Pro Liker/Target gibt es eine Row. target_type ∈ {'thread','reply'}.
-- Composite-PK verhindert Doppel-Likes deterministisch.
create table if not exists public.forum_likes (
    user_token text not null,
    target_type text not null,
    target_id text not null,
    created_at timestamptz not null default now(),
    primary key (user_token, target_type, target_id)
);
create index if not exists idx_forum_likes_user
    on public.forum_likes(user_token);
alter table public.forum_likes enable row level security;

-- ─────────── DM-LASTSEEN (per user/channel) ───────────
-- Pro User+Channel-Kombi der letzte gelesen-Timestamp für Unread-Counter.
create table if not exists public.dm_lastseen (
    user_token text not null,
    channel_id text not null,
    last_seen_ts numeric not null default 0,
    primary key (user_token, channel_id)
);
create index if not exists idx_dm_lastseen_user
    on public.dm_lastseen(user_token);
alter table public.dm_lastseen enable row level security;


-- ╔══════════════════════════════════════════════════════════════════════╗
-- ║ Block 3/4 — Layover Reviews (5-Kategorie Sterne)                    ║
-- ╚══════════════════════════════════════════════════════════════════════╝
-- Layover-Reviews (Worker P6, 2026-06-01).
-- Sterne-Ratings pro (iata, user_token, category) — User darf in jeder
-- Kategorie genau ein Rating pro Airport abgeben. Re-Bewertung = Upsert.
--
-- Schema-Entscheidungen:
--  · PK (iata, user_token, category) → ein Eintrag pro User/Spot/Kategorie,
--    Upsert für Re-Rating, idempotent.
--  · category als CHECK statt FK auf eine kategorie-Tabelle — Kategorien
--    ändern sich selten, App-Side-Enum reicht (siehe LAYOVER_REVIEW_CATEGORIES
--    im Backend).
--  · stars 1..5 hart constrained · 0 ist explizit kein-Rating.
--  · Index auf iata für die Aggregate-Query (avg group by category).
create table if not exists public.layover_reviews (
    iata        text         not null,
    user_token  text         not null,
    category    text         not null,
    stars       smallint     not null,
    created_at  timestamptz  not null default now(),
    updated_at  timestamptz  not null default now(),
    primary key (iata, user_token, category),
    check (category in ('overall','hotel','food','safety','nightlife')),
    check (stars between 1 and 5)
);
create index if not exists idx_layover_reviews_iata
    on public.layover_reviews(iata);
create index if not exists idx_layover_reviews_iata_cat
    on public.layover_reviews(iata, category);
alter table public.layover_reviews enable row level security;


-- ╔══════════════════════════════════════════════════════════════════════╗
-- ║ Block 4/4 — Friend Groups                                            ║
-- ╚══════════════════════════════════════════════════════════════════════╝
-- Friend-Groups in Supabase persistieren (Worker-H Polish, 2026-06-01).
-- Vorher lebten groups[] nur im disk-File friends_<token>.json — die SB-
-- Migration der Friends in 20260531_user_data.sql hat groups bewusst nicht
-- mitmigriert (W18-Note: "Friend-`groups`: bleiben disk-only"). Cloud-Run-
-- Redeploy wischte die ephemeral disk → alle User-Gruppen weg.
--
-- Schema: pro owner_token mehrere Gruppen, jeweils mit name + members-jsonb.
-- members ist jsonb (Liste von friend_tokens) statt 1:N-Tabelle weil:
--  · Groups haben typisch <20 Members
--  · keine Cross-Group-Queries nötig (alle Reads sind owner-scoped)
--  · CRUD wird ein einzelnes upsert/delete statt N-Inserts
--  · UI lädt komplette Groups-Liste sowieso atomar
create table if not exists public.user_friend_groups (
    id            text         primary key,
    owner_token   text         not null,
    name          text         not null,
    members       jsonb        not null default '[]'::jsonb,
    created_at    timestamptz  not null default now(),
    updated_at    timestamptz  not null default now()
);
create index if not exists idx_user_friend_groups_owner
    on public.user_friend_groups(owner_token);
alter table public.user_friend_groups enable row level security;


-- ────────────────────────────────────────────────────────────────────────
-- ENDE — wenn keine Errors: alle 4 Blöcke wurden angelegt
-- ────────────────────────────────────────────────────────────────────────
