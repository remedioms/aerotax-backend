-- Additive auth migration for the staged bearer-token rollout.
--
-- Compatibility contract:
--   * auth_users.token stays untouched. Existing app versions continue to use it.
--   * new app versions receive short-lived AXA access tokens and rotating AXR
--     refresh tokens. Only hashes are persisted.
--   * user_id is a stable public identifier for the gradual removal of the
--     legacy AT token from URLs and social payloads.

alter table public.auth_users
    add column if not exists user_id text;

create unique index if not exists idx_auth_users_user_id
    on public.auth_users(user_id) where user_id is not null;

alter table public.auth_users
    alter column user_id set default
        ('USR-' || upper(replace(gen_random_uuid()::text, '-', '')));

-- Backfill before enforcing NOT NULL so every existing account has exactly
-- one stable public identifier as soon as the migration commits.
update public.auth_users
   set user_id = 'USR-' || upper(replace(gen_random_uuid()::text, '-', ''))
 where user_id is null;

alter table public.auth_users
    alter column user_id set not null;

create table if not exists public.auth_sessions (
    access_hash        text        primary key,
    refresh_hash       text        unique not null,
    user_token         text        not null,
    user_id            text,
    access_expires_at  timestamptz not null,
    refresh_expires_at timestamptz not null,
    created_at         timestamptz not null default now(),
    last_used_at       timestamptz,
    revoked_at         timestamptz,
    rotated_at         timestamptz,
    client             text
);

create index if not exists idx_auth_sessions_user_token
    on public.auth_sessions(user_token);
create index if not exists idx_auth_sessions_refresh_hash
    on public.auth_sessions(refresh_hash);
create index if not exists idx_auth_sessions_access_expiry
    on public.auth_sessions(access_expires_at);

alter table public.auth_sessions enable row level security;

-- Service-role access only. No anon/authenticated policies are intentional.

-- Atomically consume a refresh token. A conditional UPDATE avoids the replay
-- race where two requests read the same still-valid row before either rotates
-- it. Exactly one caller receives the principal needed to issue a successor.
create or replace function public.consume_auth_refresh_token(
    p_refresh_hash text,
    p_now timestamptz
)
returns table(user_token text, user_id text, access_hash text)
language sql
security definer
set search_path = public
as $$
    update public.auth_sessions as session
       set rotated_at = p_now,
           revoked_at = p_now
     where session.refresh_hash = p_refresh_hash
       and session.rotated_at is null
       and session.revoked_at is null
       and session.refresh_expires_at > p_now
    returning session.user_token, session.user_id, session.access_hash;
$$;

revoke all on function public.consume_auth_refresh_token(text, timestamptz)
    from public, anon, authenticated;
grant execute on function public.consume_auth_refresh_token(text, timestamptz)
    to service_role;
