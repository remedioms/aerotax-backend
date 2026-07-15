-- AeroX native push: multi-device installations + durable delivery outbox.
-- Apply BEFORE the matching backend image. All RPCs are additive; the legacy
-- user_push_tokens table and routes remain available during client migration.

create extension if not exists pgcrypto;

create table if not exists public.push_installations (
    id                 uuid primary key default gen_random_uuid(),
    user_token         text not null,
    apns_token         text not null,
    bundle_id          text not null,
    environment        text not null default 'unknown'
                       check (environment in ('prod', 'sandbox', 'unknown')),
    device_id          text,
    platform           text not null default 'ios',
    active             boolean not null default true,
    registered_at      timestamptz not null default now(),
    account_bound_at   timestamptz not null default now(),
    updated_at         timestamptz not null default now(),
    tombstoned_at      timestamptz,
    tombstone_reason   text,
    last_success_at    timestamptz,
    last_failure_at    timestamptz,
    failure_count      integer not null default 0,
    unregister_secret_hash text,
    metadata           jsonb not null default '{}'::jsonb,
    unique (apns_token, bundle_id, environment)
);
alter table public.push_installations
    add column if not exists unregister_secret_hash text;

create index if not exists idx_push_installations_user_active
    on public.push_installations(user_token, active);
create index if not exists idx_push_installations_device
    on public.push_installations(device_id) where device_id is not null;
alter table public.push_installations enable row level security;

-- Existing single-device registrations become active installations. Re-running
-- the migration is idempotent. Environment and topic are recovered from metadata.
with legacy_normalized as (
    select
        p.user_token,
        p.apns_token,
        coalesce(nullif(p.metadata->>'bundle_id', ''), 'aerotax.AeroTax')
            as bundle_id,
        case lower(coalesce(p.metadata->>'apns_env', 'unknown'))
            when 'prod' then 'prod'
            when 'sandbox' then 'sandbox'
            else 'unknown'
        end as environment,
        nullif(p.device_id, '') as device_id,
        coalesce(nullif(p.platform, ''), 'ios') as platform,
        coalesce(p.updated_at, now()) as updated_at
    from public.user_push_tokens p
    where nullif(p.apns_token, '') is not null
), legacy_deduped as (
    -- Production can contain the same physical APNs installation on several
    -- historical account rows. Pick the most recently updated binding before
    -- the upsert; otherwise PostgreSQL rejects one INSERT that would update the
    -- same unique target twice.
    select distinct on (apns_token, bundle_id, environment)
        user_token, apns_token, bundle_id, environment, device_id, platform,
        updated_at
    from legacy_normalized
    order by apns_token, bundle_id, environment,
             updated_at desc nulls last, user_token desc
)
insert into public.push_installations (
    user_token, apns_token, bundle_id, environment, device_id, platform,
    registered_at, updated_at, metadata
)
select
    p.user_token,
    p.apns_token,
    p.bundle_id,
    p.environment,
    p.device_id,
    p.platform,
    -- Legacy metadata is user-adjacent JSON and may contain malformed dates;
    -- never cast it during a release-blocking migration.
    p.updated_at,
    p.updated_at,
    jsonb_build_object('migrated_from', 'user_push_tokens')
from legacy_deduped p
on conflict (apns_token, bundle_id, environment) do update set
    user_token = excluded.user_token,
    device_id = coalesce(excluded.device_id, push_installations.device_id),
    platform = excluded.platform,
    active = true,
    account_bound_at = now(),
    updated_at = now(),
    tombstoned_at = null,
    tombstone_reason = null;

create or replace function public.register_push_installation(
    p_user_token text,
    p_apns_token text,
    p_bundle_id text,
    p_environment text,
    p_device_id text default null,
    p_platform text default 'ios',
    p_metadata jsonb default '{}'::jsonb,
    p_unregister_secret_hash text default null
) returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
    v_id uuid;
    v_environment text;
begin
    if nullif(trim(p_user_token), '') is null
       or nullif(trim(p_apns_token), '') is null then
        raise exception 'missing push installation identity';
    end if;
    v_environment := case lower(coalesce(p_environment, 'unknown'))
        when 'prod' then 'prod'
        when 'sandbox' then 'sandbox'
        else 'unknown'
    end;

    -- Prevent the legacy fallback row from resurrecting the previous account
    -- after an account switch on the same physical APNs installation.
    update public.user_push_tokens
       set apns_token = null,
           expo_token = null,
           updated_at = now(),
           metadata = coalesce(metadata, '{}'::jsonb)
                      || jsonb_build_object('installation_rebound_at', now())
     where user_token <> p_user_token
       and apns_token = p_apns_token;

    -- A stable, app-generated device_id lets token rotation/build changes
    -- retire older endpoints for the same physical installation. Never do
    -- this when the client omitted device_id: guessing would disable another
    -- phone in the same account.
    if nullif(trim(coalesce(p_device_id, '')), '') is not null then
        update public.push_installations
           set active = false,
               tombstoned_at = now(),
               tombstone_reason = 'device_endpoint_replaced',
               updated_at = now()
         where device_id = trim(p_device_id)
           and bundle_id = coalesce(nullif(trim(p_bundle_id), ''), 'aerotax.AeroTax')
           and not (apns_token = p_apns_token and environment = v_environment);
    end if;

    insert into public.push_installations (
        user_token, apns_token, bundle_id, environment, device_id, platform,
        active, registered_at, account_bound_at, updated_at, tombstoned_at,
        tombstone_reason, failure_count, metadata, unregister_secret_hash
    ) values (
        p_user_token, p_apns_token,
        coalesce(nullif(trim(p_bundle_id), ''), 'aerotax.AeroTax'),
        v_environment, nullif(trim(coalesce(p_device_id, '')), ''),
        coalesce(nullif(trim(p_platform), ''), 'ios'), true, now(), now(), now(),
        null, null, 0, coalesce(p_metadata, '{}'::jsonb),
        nullif(p_unregister_secret_hash, '')
    )
    on conflict (apns_token, bundle_id, environment) do update set
        -- Account switch is authoritative: the same installation is detached
        -- from the previous account and atomically rebound to the new one.
        user_token = excluded.user_token,
        device_id = coalesce(excluded.device_id, push_installations.device_id),
        platform = excluded.platform,
        active = true,
        account_bound_at = case
            when push_installations.user_token is distinct from excluded.user_token
                then now()
            else push_installations.account_bound_at
        end,
        updated_at = now(),
        tombstoned_at = null,
        tombstone_reason = null,
        failure_count = 0,
        unregister_secret_hash = coalesce(
            excluded.unregister_secret_hash,
            push_installations.unregister_secret_hash),
        metadata = push_installations.metadata || excluded.metadata
    returning id into v_id;
    return v_id;
end;
$$;

create or replace function public.tombstone_push_installation_by_secret(
    p_installation_id uuid,
    p_unregister_secret_hash text,
    p_reason text default 'logout_capability'
) returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    v_user_token text;
    v_apns_token text;
begin
    -- Match and tombstone in one row-locking statement. Do not require
    -- `active=true`: retry after a lost HTTP 200 is idempotently successful.
    -- Registration atomically rotates the hash, so this predicate also makes
    -- an account-rebind race safe: whichever row update locks/commits last has
    -- an explicit, current capability.
    update public.push_installations
       set active = false,
           tombstoned_at = now(),
           tombstone_reason = left(coalesce(p_reason, 'logout_capability'), 80),
           updated_at = now()
     where id = p_installation_id
       and unregister_secret_hash = p_unregister_secret_hash
    returning user_token, apns_token into v_user_token, v_apns_token;

    if v_user_token is null then
        return false;
    end if;
    update public.user_push_tokens
       set apns_token = null,
           expo_token = null,
           updated_at = now(),
           metadata = coalesce(metadata, '{}'::jsonb)
                      || jsonb_build_object('installation_tombstoned_at', now())
     where user_token = v_user_token and apns_token = v_apns_token;
    return true;
end;
$$;

create or replace function public.tombstone_push_installations(
    p_user_token text,
    p_installation_id uuid default null,
    p_apns_token text default null,
    p_bundle_id text default null,
    p_environment text default null,
    p_reason text default 'logout'
) returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
    v_count integer;
begin
    update public.push_installations
       set active = false,
           tombstoned_at = now(),
           tombstone_reason = left(coalesce(p_reason, 'logout'), 80),
           updated_at = now()
     where user_token = p_user_token
       and active = true
       and (p_installation_id is null or id = p_installation_id)
       and (nullif(p_apns_token, '') is null or apns_token = p_apns_token)
       and (nullif(p_bundle_id, '') is null or bundle_id = p_bundle_id)
       and (nullif(p_environment, '') is null or environment = p_environment);
    get diagnostics v_count = row_count;
    return v_count;
end;
$$;


create table if not exists public.push_outbox (
    id                 uuid primary key default gen_random_uuid(),
    idempotency_key    text not null unique,
    user_token         text,
    payload            jsonb not null,
    status             text not null default 'pending'
                       check (status in ('pending', 'processing', 'retry',
                                         'delivered', 'dead')),
    attempts           integer not null default 0,
    available_at       timestamptz not null default now(),
    locked_at          timestamptz,
    locked_by          text,
    last_error         text,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),
    delivered_at       timestamptz,
    dead_at            timestamptz
);

create index if not exists idx_push_outbox_claim
    on public.push_outbox(status, available_at, created_at)
    where status in ('pending', 'retry', 'processing');
alter table public.push_outbox enable row level security;

create or replace function public.enqueue_push_outbox(
    p_idempotency_key text,
    p_user_token text,
    p_payload jsonb
) returns table(outbox_id uuid, inserted boolean)
language plpgsql
security definer
set search_path = public
as $$
declare
    v_id uuid;
begin
    insert into public.push_outbox (idempotency_key, user_token, payload)
    values (p_idempotency_key, p_user_token, p_payload)
    on conflict (idempotency_key) do nothing
    returning id into v_id;
    if v_id is not null then
        return query select v_id, true;
        return;
    end if;
    return query
        select id, false from public.push_outbox
         where idempotency_key = p_idempotency_key limit 1;
end;
$$;

create or replace function public.claim_push_outbox(
    p_worker_id text,
    p_limit integer default 20,
    p_lock_timeout_seconds integer default 120
) returns setof public.push_outbox
language sql
security definer
set search_path = public
as $$
    with picked as (
        select id
          from public.push_outbox
         where (
             status in ('pending', 'retry') and available_at <= now()
         ) or (
             status = 'processing'
             and locked_at < now() - make_interval(secs => greatest(30, p_lock_timeout_seconds))
         )
         order by created_at
         for update skip locked
         limit least(greatest(p_limit, 1), 100)
    )
    update public.push_outbox o
       set status = 'processing',
           attempts = o.attempts + 1,
           locked_at = now(),
           locked_by = p_worker_id,
           updated_at = now()
      from picked
     where o.id = picked.id
    returning o.*;
$$;

create or replace view public.push_outbox_metrics as
select status, count(*)::bigint as count,
       min(created_at) as oldest_created_at,
       max(updated_at) as newest_updated_at
  from public.push_outbox
 group by status;

-- Delivered payloads are erased immediately by the worker. This cleanup keeps
-- only short operational history and bounds dead-letter PII retention.
create or replace function public.cleanup_push_outbox()
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare v_count integer;
begin
    delete from public.push_outbox
     where (status = 'delivered' and delivered_at < now() - interval '24 hours')
        or (status = 'dead' and dead_at < now() - interval '7 days');
    get diagnostics v_count = row_count;
    return v_count;
end;
$$;

-- PostgreSQL grants EXECUTE to PUBLIC on new functions by default. Revoking
-- only anon/authenticated is insufficient because both inherit via PUBLIC.
revoke execute on function public.register_push_installation(text,text,text,text,text,text,jsonb,text)
    from public, anon, authenticated;
revoke execute on function public.tombstone_push_installations(text,uuid,text,text,text,text)
    from public, anon, authenticated;
revoke execute on function public.enqueue_push_outbox(text,text,jsonb)
    from public, anon, authenticated;
revoke execute on function public.claim_push_outbox(text,integer,integer)
    from public, anon, authenticated;
revoke execute on function public.cleanup_push_outbox()
    from public, anon, authenticated;
revoke execute on function public.tombstone_push_installation_by_secret(uuid,text,text)
    from public, anon, authenticated;
grant execute on function public.register_push_installation(text,text,text,text,text,text,jsonb,text)
    to service_role;
grant execute on function public.tombstone_push_installations(text,uuid,text,text,text,text)
    to service_role;
grant execute on function public.enqueue_push_outbox(text,text,jsonb)
    to service_role;
grant execute on function public.claim_push_outbox(text,integer,integer)
    to service_role;
grant execute on function public.cleanup_push_outbox()
    to service_role;
grant execute on function public.tombstone_push_installation_by_secret(uuid,text,text)
    to service_role;

-- Supabase projects commonly have permissive default privileges for objects in
-- `public`. RLS protects the two tables, but the metrics view could otherwise
-- execute with its owner's privileges and expose operational timing/counts.
-- Make the intended service-role-only boundary explicit for every new object.
revoke all on table public.push_installations
    from public, anon, authenticated;
revoke all on table public.push_outbox
    from public, anon, authenticated;
revoke all on table public.push_outbox_metrics
    from public, anon, authenticated;
grant all on table public.push_installations to service_role;
grant all on table public.push_outbox to service_role;
grant select on table public.push_outbox_metrics to service_role;
