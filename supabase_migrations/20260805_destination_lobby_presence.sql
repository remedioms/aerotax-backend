-- Anonymer Live-Zaehler und Push-Adressierung fuer die Destination-Lobby.
-- Es werden bewusst KEINE Namen, Profilbilder oder rohen User-Tokens
-- gespeichert. `user_ref` ist die verschluesselte, nur serverseitig wieder
-- aufloesbare AXU-Referenz; RLS gibt die Tabelle niemals an Clients frei.

create table if not exists public.destination_lobby_presence (
    member_hash text primary key
        check (member_hash ~ '^[0-9a-f]{64}$'),
    user_ref text not null
        check (user_ref ~ '^AXU-[A-Za-z0-9_-]{20,200}$'),
    session_id text not null
        check (session_id ~ '^destination_[A-Z]{3}_[0-9]+$'),
    channel_id text not null
        check (channel_id ~ '^group__destination_[A-Z]{3}$'),
    available_since timestamptz not null,
    expires_at timestamptz not null,
    updated_at timestamptz not null default now(),
    check (expires_at > available_since)
);

create index if not exists idx_destination_lobby_presence_active
    on public.destination_lobby_presence (channel_id, expires_at);

alter table public.destination_lobby_presence enable row level security;

-- Ein atomarer Backend-Call: anonym einchecken und danach die aktuelle Zahl
-- desselben Stations-Channels ermitteln. Abgelaufene Rows zaehlen nie mit.
create or replace function public.touch_destination_lobby_presence(
    p_member_hash text,
    p_user_ref text,
    p_session_id text,
    p_channel_id text,
    p_available_since timestamptz,
    p_expires_at timestamptz
)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
    active_count integer;
begin
    if p_member_hash !~ '^[0-9a-f]{64}$'
       or p_user_ref !~ '^AXU-[A-Za-z0-9_-]{20,200}$'
       or p_session_id !~ '^destination_[A-Z]{3}_[0-9]+$'
       or p_channel_id !~ '^group__destination_[A-Z]{3}$'
       or p_expires_at <= p_available_since
       or p_available_since > now()
       or p_expires_at <= now() then
        raise exception 'invalid destination lobby presence';
    end if;

    insert into public.destination_lobby_presence (
        member_hash, user_ref, session_id, channel_id,
        available_since, expires_at, updated_at
    ) values (
        p_member_hash, p_user_ref, p_session_id, p_channel_id,
        p_available_since, p_expires_at, now()
    )
    on conflict (member_hash) do update set
        user_ref = excluded.user_ref,
        session_id = excluded.session_id,
        channel_id = excluded.channel_id,
        available_since = excluded.available_since,
        expires_at = excluded.expires_at,
        updated_at = now();

    select count(*)::integer into active_count
      from public.destination_lobby_presence
     where channel_id = p_channel_id
       and available_since <= now()
       and expires_at > now();

    return active_count;
end;
$$;

-- Ausschliesslich das Backend mit Service Role darf schreiben/zaehlen.
revoke all on table public.destination_lobby_presence
    from public, anon, authenticated;
revoke all on function public.touch_destination_lobby_presence(
    text, text, text, text, timestamptz, timestamptz
) from public, anon, authenticated;
grant execute on function public.touch_destination_lobby_presence(
    text, text, text, text, timestamptz, timestamptz
) to service_role;
