-- ====================================================================
--  AeroX Aviation Data Engine - self-growing cache tables (idempotent)
--  Paste into Supabase SQL Editor and run. ASCII-only (no smart quotes)
--  so the SQL editor never chokes on comment characters.
--
--  These tables are the HOT/growing layer of the Data Engine: every
--  external hit (adsbdb/hexdb/planespotters/AviationStack) is written
--  back here so the same fact is fetched at most once and all users then
--  pull it from our backend. The engine runs fine WITHOUT them (caching
--  just no-ops) - but then each lookup re-hits the source.
-- ====================================================================

-- Aircraft cache (hex -> details from adsbdb/hexdb)
create table if not exists public.ax_aircraft_cache (
    hex         text         primary key,
    payload     jsonb        not null default '{}'::jsonb,
    updated_at  timestamptz  not null default now()
);
alter table public.ax_aircraft_cache enable row level security;

-- Route cache (flight number / callsign -> route from adsbdb)
create table if not exists public.ax_route_cache (
    flight      text         primary key,
    payload     jsonb        not null default '{}'::jsonb,
    updated_at  timestamptz  not null default now()
);
alter table public.ax_route_cache enable row level security;

-- Photo link cache (hex -> planespotters photo URL, link only, no image bytes)
create table if not exists public.ax_photo_cache (
    hex         text         primary key,
    payload     jsonb        not null default '{}'::jsonb,
    updated_at  timestamptz  not null default now()
);
alter table public.ax_photo_cache enable row level security;

-- Schedule cache (city pair like FRA-LIS -> real flight numbers/times, AviationStack)
create table if not exists public.ax_schedule_cache (
    route       text         primary key,
    payload     jsonb        not null default '{}'::jsonb,
    updated_at  timestamptz  not null default now()
);
alter table public.ax_schedule_cache enable row level security;

-- API budget counter (protects AviationStack free limit; one row per month, YYYY-MM)
create table if not exists public.ax_api_budget (
    month       text         primary key,
    n           integer      not null default 0,
    updated_at  timestamptz  not null default now()
);
alter table public.ax_api_budget enable row level security;

-- Done. The backend writes with the service-role key (bypasses RLS),
-- so no extra policies are needed.
