-- AeroX Android push: add native FCM endpoints to the existing multi-device
-- installation registry. Apply before the backend image that selects
-- push_installations.fcm_token.

alter table public.push_installations
    alter column apns_token drop not null,
    add column if not exists fcm_token text;

create unique index if not exists uq_push_installations_fcm_bundle
    on public.push_installations(fcm_token, bundle_id)
    where fcm_token is not null;

create or replace function public.register_fcm_installation(
    p_user_token text,
    p_fcm_token text,
    p_bundle_id text,
    p_device_id text default null,
    p_metadata jsonb default '{}'::jsonb,
    p_unregister_secret_hash text default null
) returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
    v_id uuid;
    v_bundle_id text;
begin
    if nullif(trim(p_user_token), '') is null
       or nullif(trim(p_fcm_token), '') is null then
        raise exception 'missing fcm installation identity';
    end if;
    v_bundle_id := coalesce(
        nullif(trim(p_bundle_id), ''), 'de.aerosteuer.aerox');

    -- Rebinding the globally unique endpoint must atomically detach the old
    -- account's compatibility row as well.
    update public.user_push_tokens
       set expo_token = null,
           updated_at = now(),
           metadata = coalesce(metadata, '{}'::jsonb)
                      || jsonb_build_object('fcm_installation_rebound_at', now())
     where user_token <> p_user_token
       and expo_token = p_fcm_token
       and lower(coalesce(platform, '')) = 'android';

    -- A stable client device id retires the previous token after an FCM token
    -- rotation without touching a second phone on the same account.
    if nullif(trim(coalesce(p_device_id, '')), '') is not null then
        update public.push_installations
           set active = false,
               tombstoned_at = now(),
               tombstone_reason = 'device_endpoint_replaced',
               updated_at = now()
         where device_id = trim(p_device_id)
           and bundle_id = v_bundle_id
           and platform = 'android'
           and fcm_token is distinct from p_fcm_token;
    end if;

    insert into public.push_installations (
        user_token, apns_token, fcm_token, bundle_id, environment, device_id,
        platform, active, registered_at, account_bound_at, updated_at,
        tombstoned_at, tombstone_reason, failure_count, metadata,
        unregister_secret_hash
    ) values (
        p_user_token, null, p_fcm_token, v_bundle_id, 'unknown',
        nullif(trim(coalesce(p_device_id, '')), ''), 'android', true,
        now(), now(), now(), null, null, 0, coalesce(p_metadata, '{}'::jsonb),
        nullif(p_unregister_secret_hash, '')
    )
    on conflict (fcm_token, bundle_id) where fcm_token is not null do update set
        user_token = excluded.user_token,
        device_id = coalesce(excluded.device_id, push_installations.device_id),
        platform = 'android',
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

create or replace function public.tombstone_fcm_installations(
    p_user_token text,
    p_fcm_token text default null,
    p_device_id text default null,
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
       and platform = 'android'
       and active = true
       and (nullif(p_fcm_token, '') is null or fcm_token = p_fcm_token)
       and (nullif(p_device_id, '') is null or device_id = p_device_id);
    get diagnostics v_count = row_count;
    return v_count;
end;
$$;

revoke execute on function public.register_fcm_installation(
    text,text,text,text,jsonb,text) from public, anon, authenticated;
revoke execute on function public.tombstone_fcm_installations(
    text,text,text,text) from public, anon, authenticated;
grant execute on function public.register_fcm_installation(
    text,text,text,text,jsonb,text) to service_role;
grant execute on function public.tombstone_fcm_installations(
    text,text,text,text) to service_role;

