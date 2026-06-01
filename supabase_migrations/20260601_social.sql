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
