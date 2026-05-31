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
