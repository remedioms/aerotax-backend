-- Auth-Users in Supabase persistieren (P0-Fix: ephemeral disk Cloud Run wipte
-- alle Accounts bei jedem Redeploy + multi-instance hatte keine Shared State).
-- Drop-in für _auth_load/_auth_save mit dict-Interface. Optional-Felder leben
-- als Spalten (häufig gelesen) + metadata jsonb für unbekannte Zusatzdaten.

create table if not exists public.auth_users (
    email             text         primary key,
    password_hash     text,
    token             text         unique,
    apple_sub         text         unique,
    reset_token       text,
    reset_expires     timestamptz,
    reset_used_at     timestamptz,
    hash_migrated_at  timestamptz,
    created_at        timestamptz  not null default now(),
    last_login_at     timestamptz,
    metadata          jsonb        not null default '{}'::jsonb
);

create index if not exists idx_auth_users_token
    on public.auth_users(token) where token is not null;
create index if not exists idx_auth_users_apple_sub
    on public.auth_users(apple_sub) where apple_sub is not null;
create index if not exists idx_auth_users_reset_token
    on public.auth_users(reset_token) where reset_token is not null;

alter table public.auth_users enable row level security;

-- Default deny — Service-Role-Key (SUPABASE_SERVICE_KEY) umgeht RLS sowieso.
-- Anon/authenticated User dürfen die Tabelle nie direkt anfassen.
