-- Distributed paid-upstream singleflight + atomic budget reservations.
--
-- Apply BEFORE deploying code that imports blueprints/paid_cost_control.py.
-- All RPCs are service-role-only; app clients cannot reserve/refund budget or
-- read cached provider payloads directly.

-- Same schema as 20260705_budget_increment.sql. Repeating it idempotently makes
-- this migration safe on a freshly provisioned database as well as production.
create table if not exists public.ax_api_budget (
    month       text primary key,
    n           integer not null default 0,
    updated_at  timestamptz default now()
);

alter table public.ax_api_budget enable row level security;

create table if not exists public.ax_paid_call_cache (
    call_key         text primary key,
    provider         text not null,
    lease_owner      text,
    lease_until      timestamptz,
    result           jsonb,
    result_until     timestamptz,
    negative_reason  text,
    negative_until   timestamptz,
    updated_at       timestamptz not null default now()
);

create index if not exists ax_paid_call_cache_updated_idx
    on public.ax_paid_call_cache (updated_at);

alter table public.ax_paid_call_cache enable row level security;

create table if not exists public.ax_paid_budget_reservations (
    idempotency_key  text primary key,
    provider         text not null,
    day_key          text not null,
    month_key        text not null,
    reserved_units   integer not null check (reserved_units > 0),
    actual_units     integer check (actual_units >= 0),
    state            text not null default 'reserved',
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now()
);

create index if not exists ax_paid_budget_reservations_created_idx
    on public.ax_paid_budget_reservations (created_at);

alter table public.ax_paid_budget_reservations enable row level security;

-- Reuse the existing counter table/RPC family, but reserve BOTH caps in one
-- transaction. Advisory locks make the idempotency check race-free even before
-- the reservation row exists. Counter rows are locked in lexical order.
create or replace function public.ax_paid_reserve_budget(
    p_idempotency_key text,
    p_provider text,
    p_day_key text,
    p_month_key text,
    p_units integer,
    p_day_cap integer,
    p_month_cap integer
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_existing public.ax_paid_budget_reservations%rowtype;
    v_day integer;
    v_month integer;
    v_units integer := greatest(1, p_units);
begin
    if coalesce(p_idempotency_key, '') = ''
       or coalesce(p_day_key, '') = '' or coalesce(p_month_key, '') = ''
       or p_day_key = p_month_key then
        return jsonb_build_object('status', 'invalid');
    end if;

    perform pg_advisory_xact_lock(hashtextextended(p_idempotency_key, 0));

    -- Opportunistic bounded cleanup (~1 % of calls) keeps both additive tables
    -- small without a separate scheduler. Active/current rows are never touched.
    if random() < 0.01 then
        delete from public.ax_paid_budget_reservations
         where ctid in (
             select ctid from public.ax_paid_budget_reservations
              where actual_units is not null
                and updated_at < now() - interval '7 days'
              order by updated_at limit 500);
        delete from public.ax_paid_call_cache
         where ctid in (
             select ctid from public.ax_paid_call_cache
              where coalesce(lease_until, '-infinity'::timestamptz) < now()
                and coalesce(result_until, '-infinity'::timestamptz) < now()
                and coalesce(negative_until, '-infinity'::timestamptz) < now()
                and updated_at < now() - interval '7 days'
              order by updated_at limit 500);
    end if;

    select * into v_existing
      from public.ax_paid_budget_reservations
     where idempotency_key = p_idempotency_key;
    if found then
        return jsonb_build_object(
            'status', 'granted', 'idempotent', true,
            'reserved_units', v_existing.reserved_units,
            'actual_units', v_existing.actual_units);
    end if;

    insert into public.ax_api_budget(month, n, updated_at)
    values (p_day_key, 0, now()), (p_month_key, 0, now())
    on conflict (month) do nothing;

    -- Deterministic order avoids deadlocks when different providers share caps.
    perform 1 from public.ax_api_budget
     where month in (p_day_key, p_month_key)
     order by month for update;

    select n into v_day from public.ax_api_budget where month = p_day_key;
    select n into v_month from public.ax_api_budget where month = p_month_key;

    if v_day + v_units > greatest(0, p_day_cap)
       or v_month + v_units > greatest(0, p_month_cap) then
        return jsonb_build_object(
            'status', 'denied', 'day_used', v_day, 'month_used', v_month);
    end if;

    update public.ax_api_budget
       set n = n + v_units, updated_at = now()
     where month in (p_day_key, p_month_key);

    insert into public.ax_paid_budget_reservations(
        idempotency_key, provider, day_key, month_key, reserved_units)
    values (p_idempotency_key, coalesce(nullif(p_provider, ''), 'unknown'),
            p_day_key, p_month_key, v_units);

    return jsonb_build_object(
        'status', 'granted', 'reserved_units', v_units,
        'day_used', v_day + v_units, 'month_used', v_month + v_units);
end;
$$;


create or replace function public.ax_paid_reconcile_budget(
    p_idempotency_key text,
    p_actual_units integer,
    p_state text default 'completed'
) returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    v_row public.ax_paid_budget_reservations%rowtype;
    v_actual integer := greatest(0, p_actual_units);
    v_delta integer;
begin
    perform pg_advisory_xact_lock(hashtextextended(p_idempotency_key, 0));
    select * into v_row from public.ax_paid_budget_reservations
     where idempotency_key = p_idempotency_key for update;
    if not found then
        return false;
    end if;
    if v_row.actual_units is not null then
        return true; -- idempotent retry; never refund twice
    end if;

    v_delta := v_actual - v_row.reserved_units;
    if v_delta <> 0 then
        update public.ax_api_budget
           set n = greatest(0, n + v_delta), updated_at = now()
         where month in (v_row.day_key, v_row.month_key);
    end if;
    update public.ax_paid_budget_reservations
       set actual_units = v_actual,
           state = coalesce(nullif(p_state, ''), 'completed'),
           updated_at = now()
     where idempotency_key = p_idempotency_key;
    return true;
end;
$$;


create or replace function public.ax_paid_call_acquire(
    p_call_key text,
    p_owner text,
    p_lease_seconds integer default 20
) returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_row public.ax_paid_call_cache%rowtype;
begin
    if coalesce(p_call_key, '') = '' or coalesce(p_owner, '') = '' then
        return jsonb_build_object('status', 'invalid');
    end if;
    perform pg_advisory_xact_lock(hashtextextended(p_call_key, 1));
    select * into v_row from public.ax_paid_call_cache
     where call_key = p_call_key for update;

    if found and v_row.result is not null and v_row.result_until > now() then
        return jsonb_build_object('status', 'hit', 'result', v_row.result);
    end if;
    if found and v_row.negative_reason is not null
       and v_row.negative_until > now() then
        return jsonb_build_object('status', 'negative',
                                  'negative_reason', v_row.negative_reason);
    end if;
    if found and v_row.lease_owner is distinct from p_owner
       and v_row.lease_until > now() then
        return jsonb_build_object('status', 'busy');
    end if;

    insert into public.ax_paid_call_cache(
        call_key, provider, lease_owner, lease_until, result, result_until,
        negative_reason, negative_until, updated_at)
    values (p_call_key, split_part(p_call_key, ':', 1), p_owner,
            now() + make_interval(secs => greatest(1, p_lease_seconds)),
            null, null, null, null, now())
    on conflict (call_key) do update set
        lease_owner = excluded.lease_owner,
        lease_until = excluded.lease_until,
        result = null, result_until = null,
        negative_reason = null, negative_until = null,
        updated_at = now();
    return jsonb_build_object('status', 'acquired');
end;
$$;


create or replace function public.ax_paid_call_complete(
    p_call_key text,
    p_owner text,
    p_result jsonb,
    p_result_ttl_seconds integer,
    p_negative_reason text,
    p_negative_ttl_seconds integer
) returns boolean
language plpgsql
security definer
set search_path = public
as $$
begin
    update public.ax_paid_call_cache set
        lease_owner = null,
        lease_until = null,
        result = case when p_negative_reason is null then p_result else null end,
        result_until = case when p_negative_reason is null
                            then now() + make_interval(
                                secs => greatest(1, p_result_ttl_seconds))
                            else null end,
        negative_reason = p_negative_reason,
        negative_until = case when p_negative_reason is not null
                              then now() + make_interval(
                                  secs => greatest(1, p_negative_ttl_seconds))
                              else null end,
        updated_at = now()
     where call_key = p_call_key and lease_owner = p_owner;
    return found;
end;
$$;


revoke all on table public.ax_paid_call_cache from public, anon, authenticated;
revoke all on table public.ax_paid_budget_reservations from public, anon, authenticated;
grant all on table public.ax_paid_call_cache to service_role;
grant all on table public.ax_paid_budget_reservations to service_role;

revoke execute on function public.ax_paid_reserve_budget(text,text,text,text,integer,integer,integer)
    from public, anon, authenticated;
revoke execute on function public.ax_paid_reconcile_budget(text,integer,text)
    from public, anon, authenticated;
revoke execute on function public.ax_paid_call_acquire(text,text,integer)
    from public, anon, authenticated;
revoke execute on function public.ax_paid_call_complete(text,text,jsonb,integer,text,integer)
    from public, anon, authenticated;

grant execute on function public.ax_paid_reserve_budget(text,text,text,text,integer,integer,integer)
    to service_role;
grant execute on function public.ax_paid_reconcile_budget(text,integer,text)
    to service_role;
grant execute on function public.ax_paid_call_acquire(text,text,integer)
    to service_role;
grant execute on function public.ax_paid_call_complete(text,text,jsonb,integer,text,integer)
    to service_role;
