-- Echte App-Open-Messung (2026-07-25): bislang gab es KEINEN sauberen
-- Foreground-Stempel — user_profiles.updated_at wird auch server-seitig
-- gebumpt (Freunde-View → _maybe_refresh_calendar_feed → _profile_save),
-- roster_snapshots kommt auch aus dem BGAppRefresh. Diese Tabelle zählt
-- ausschließlich bewusste Foreground-Opens (iOS pingt beim Aktivwerden).
-- Eine Row pro (token, tag) → DAU/WAU/Retention sind einfache Counts.
create table if not exists public.ax_app_opens (
    token     text not null,
    day       date not null,
    opens     int  not null default 1,
    first_at  timestamptz not null default now(),
    last_at   timestamptz not null default now(),
    build     text,
    primary key (token, day)
);

create index if not exists ax_app_opens_day_idx on public.ax_app_opens (day);

-- Atomarer Upsert-Increment (PostgREST-Upsert kann kein opens+1).
create or replace function public.ax_app_open(
    p_token text,
    p_build text default null
) returns void
language plpgsql
as $$
begin
    insert into public.ax_app_opens as a (token, day, opens, first_at, last_at, build)
    values (p_token, current_date, 1, now(), now(), p_build)
    on conflict (token, day) do update
        set opens   = a.opens + 1,
            last_at = now(),
            build   = coalesce(excluded.build, a.build);
end;
$$;

-- Service-Role reicht (Backend ruft mit Service-Key); kein anon-Zugriff nötig.
revoke all on function public.ax_app_open(text, text) from public, anon;
grant execute on function public.ax_app_open(text, text) to service_role;
alter table public.ax_app_opens enable row level security;
