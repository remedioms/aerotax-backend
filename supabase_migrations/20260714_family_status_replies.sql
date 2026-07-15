-- Family ↔ Crew 24h messaging.
-- Apply BEFORE deploying the matching backend/iOS build.
-- The original Family message keeps its own expires_at; a Crew reply gets an
-- independent 24h reply_expires_at and never extends the Crew feed item.

alter table public.feed_statuses
    add column if not exists body text,
    -- Production may predate the small 20260616 reaction migration. Bundle
    -- those additive columns here so the new status upsert cannot fall back to
    -- disk merely because it sends reaction=null alongside the reply fields.
    add column if not exists reaction text,
    add column if not exists reacted_at timestamptz,
    add column if not exists message_id text,
    add column if not exists reply_body text,
    add column if not exists reply_created_at timestamptz,
    add column if not exists reply_expires_at timestamptz,
    add column if not exists reply_idempotency_key text;

-- Older installs called the payload column `text`; newer backend code uses
-- `body`. Keep old rows readable without assuming every install has `text`.
do $$
begin
    if exists (
        select 1 from information_schema.columns
         where table_schema = 'public' and table_name = 'feed_statuses'
           and column_name = 'text'
    ) then
        execute 'update public.feed_statuses set body = text '
                'where body is null and text is not null';
        -- Backend writes the canonical `body` column. Some early installs kept
        -- `text not null`; relax it so a body-only upsert cannot fail.
        execute 'alter table public.feed_statuses alter column text drop not null';
    end if;
end $$;

create index if not exists feed_statuses_reply_expires_idx
    on public.feed_statuses (reply_expires_at)
    where reply_expires_at is not null;

create or replace function public.set_family_status_reply(
    p_crew_token text,
    p_created_at timestamptz,
    p_reply_body text,
    p_idempotency_key text
) returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
    v_row public.feed_statuses%rowtype;
    v_now timestamptz := clock_timestamp();
begin
    -- Row lock makes idempotency + rate limiting atomic across workers.
    select fs.* into v_row
      from public.feed_statuses fs
     where fs.crew_token = p_crew_token
       and fs.created_at = p_created_at
     for update;

    if not found then
        return jsonb_build_object('outcome', 'not_found');
    end if;
    if v_row.expires_at <= v_now then
        return jsonb_build_object('outcome', 'expired');
    end if;
    if v_row.reply_idempotency_key = p_idempotency_key then
        return to_jsonb(v_row) || jsonb_build_object('outcome', 'idempotent');
    end if;
    if v_row.reply_created_at is not null
       and v_row.reply_created_at > v_now - interval '3 seconds' then
        return jsonb_build_object('outcome', 'rate_limited');
    end if;

    update public.feed_statuses fs
       set reply_body = p_reply_body,
           reply_created_at = v_now,
           reply_expires_at = v_now + interval '24 hours',
           reply_idempotency_key = p_idempotency_key
     where fs.family_token = v_row.family_token
     returning fs.* into v_row;

    return to_jsonb(v_row) || jsonb_build_object('outcome', 'saved');
end;
$$;

revoke execute on function public.set_family_status_reply(text,timestamptz,text,text)
    from public, anon, authenticated;
grant execute on function public.set_family_status_reply(text,timestamptz,text,text)
    to service_role;
