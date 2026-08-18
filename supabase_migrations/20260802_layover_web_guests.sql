-- Zeitlich begrenzte Web-Gaeste fuer native Layover-Gruppenchats.
-- Nur SHA-256-Hashes der Invite-/Session-Secrets werden gespeichert.

create table if not exists public.layover_guest_invites (
    id uuid primary key,
    token_hash text not null unique check (length(token_hash) = 64),
    group_id text not null,
    group_name text not null check (char_length(group_name) between 1 and 60),
    owner_token text not null,
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    max_guests integer not null default 50 check (max_guests between 1 and 100),
    revoked boolean not null default false
);

create index if not exists idx_layover_guest_invites_group
    on public.layover_guest_invites (group_id, expires_at desc);
create index if not exists idx_layover_guest_invites_owner
    on public.layover_guest_invites (owner_token, created_at desc);

create table if not exists public.layover_guest_sessions (
    id uuid primary key,
    invite_id uuid not null references public.layover_guest_invites(id)
        on delete cascade,
    token_hash text not null unique check (length(token_hash) = 64),
    display_name text not null check (char_length(display_name) between 1 and 32),
    avatar_emoji text not null check (char_length(avatar_emoji) between 1 and 16),
    created_at timestamptz not null default now(),
    expires_at timestamptz not null,
    revoked boolean not null default false
);

create index if not exists idx_layover_guest_sessions_invite
    on public.layover_guest_sessions (invite_id, created_at desc);

-- Der Handler prueft das Limit fuer eine freundliche 409-Antwort vorab. Dieser
-- Trigger ist die atomare letzte Schranke fuer parallele Join-Requests.
create or replace function public.enforce_layover_guest_limit()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
    allowed_count integer;
    active_count integer;
begin
    select max_guests into allowed_count
      from public.layover_guest_invites
     where id = new.invite_id and revoked = false and expires_at > now()
     for update;

    if allowed_count is null then
        raise exception 'layover invite unavailable';
    end if;

    select count(*) into active_count
      from public.layover_guest_sessions
     where invite_id = new.invite_id and revoked = false and expires_at > now();

    if active_count >= allowed_count then
        raise exception 'layover invite full';
    end if;
    return new;
end;
$$;

drop trigger if exists trg_enforce_layover_guest_limit
    on public.layover_guest_sessions;
create trigger trg_enforce_layover_guest_limit
before insert on public.layover_guest_sessions
for each row execute function public.enforce_layover_guest_limit();

alter table public.layover_guest_invites enable row level security;
alter table public.layover_guest_sessions enable row level security;

-- Absichtlich keine Client-RLS-Policies: Zugriff ausschliesslich ueber das
-- Backend mit Service Role. Browser sehen weder Hashes noch interne group_id.
-- Revoke direct Data API access as well: RLS is the row boundary, while these
-- grants keep legacy/default privileges from becoming an exposure after a
-- future policy change. The trigger runs during service-role inserts and does
-- not need a callable public function.
revoke all on table public.layover_guest_invites
    from public, anon, authenticated;
revoke all on table public.layover_guest_sessions
    from public, anon, authenticated;
revoke all on function public.enforce_layover_guest_limit()
    from public, anon, authenticated;
