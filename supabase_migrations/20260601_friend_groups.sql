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
