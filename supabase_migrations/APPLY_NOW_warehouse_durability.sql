-- ============================================================================
--  AeroX — Flight Warehouse: full durable schema for ALL German hubs
--  APPLY ONCE in Supabase -> SQL Editor.  Idempotent, safe to re-run.
--  2026-06-24
--
--  Makes the board pollers persist EVERYTHING they read from each airport's free
--  JSON feed (FRA/MUC/DUS/BER/HAM/HAJ/FMO/LEJ/DRS/DTM/STR/NUE/CGN) for every
--  airline: route, TAIL registration, aircraft type, scheduled/estimated times,
--  delay, gate, terminal, status. The backend writes with the service-role key
--  (bypasses RLS); RLS stays on, no extra policies needed. No code deploy needed
--  -- the already-running poller starts saving tails the moment these exist.
-- ============================================================================

-- ── 1) airport_delay_obs : the board warehouse (one row per flight+day) ─────
create table if not exists public.airport_delay_obs (
    date          text        not null,   -- operating day 'YYYY-MM-DD'
    airport       text        not null,   -- 'FRA' / 'MUC' / 'BER' ... ('<AP>#ARR' = arrivals)
    flight        text        not null,   -- flight number, e.g. 'LH401'
    sched         text        not null,   -- scheduled time 'HH:MM'
    max_delay_min integer     not null default 0,
    cancelled     boolean     not null default false,
    status        text,
    updated_at    timestamptz not null default now(),
    primary key (date, airport, flight, sched)
);
-- Full per-flight info (nullable, additive -- safe on an existing table):
alter table public.airport_delay_obs add column if not exists dest_iata  text;
alter table public.airport_delay_obs add column if not exists dest_name  text;
alter table public.airport_delay_obs add column if not exists gate       text;
alter table public.airport_delay_obs add column if not exists terminal   text;
alter table public.airport_delay_obs add column if not exists airline    text;
alter table public.airport_delay_obs add column if not exists esti       text;
alter table public.airport_delay_obs add column if not exists reg        text;   -- TAIL, e.g. D-AILH
alter table public.airport_delay_obs add column if not exists type_code  text;   -- type code, e.g. A21N
-- Indexes for the day-load + the warehouse query endpoints
-- (/api/ax/flight-info/<flightno> -> WHERE flight, /api/ax/aircraft-history/<reg> -> WHERE reg):
create index if not exists idx_airport_delay_obs_date_airport on public.airport_delay_obs(date, airport);
create index if not exists idx_airport_delay_obs_date         on public.airport_delay_obs(date);
create index if not exists idx_airport_delay_obs_flight       on public.airport_delay_obs(flight);
create index if not exists idx_airport_delay_obs_reg          on public.airport_delay_obs(reg);

-- ── 2) flight_observations : self-building per-flight DB (ADS-B + delay trend) ──
create table if not exists public.flight_observations (
    callsign    text not null,
    obs_date    text not null,
    reg         text,
    type_code   text,
    dep         text,
    arr         text,
    first_seen  timestamptz not null default now(),
    last_seen   timestamptz not null default now(),
    primary key (callsign, obs_date)
);
create index if not exists flight_obs_callsign_idx on public.flight_observations (callsign);
alter table public.flight_observations add column if not exists sched     text;
alter table public.flight_observations add column if not exists delay_min integer;
alter table public.flight_observations add column if not exists status    text;
alter table public.flight_observations add column if not exists cancelled boolean;

-- ── 3) Tables that were never created in the repo (other durability gaps) ───
create table if not exists public.ax_crewbus (
    iata        text         primary key,
    payload     jsonb        not null default '{}'::jsonb,
    updated_at  timestamptz  not null default now()
);
alter table public.ax_crewbus enable row level security;

create table if not exists public.aircraft_age (
    hex         text         primary key,
    year        integer,
    built_date  text,
    reg         text,
    type        text,
    updated     timestamptz  not null default now()
);
alter table public.aircraft_age enable row level security;
alter table public.aircraft_age add column if not exists built_date text;

create table if not exists public.community_stats (
    token_hash   text         primary key,
    hours_flown  float8,
    flights      integer,
    countries    integer,
    tour_days    integer,
    distance_km  float8,
    updated      timestamptz  not null default now()
);
alter table public.community_stats enable row level security;

create table if not exists public.support_requests (
    id          bigint       generated always as identity primary key,
    reason      text,
    email       text,
    phone       text,
    message     text,
    ip_hash     text,
    created_at  timestamptz  not null default now()
);
alter table public.support_requests enable row level security;
create index if not exists idx_support_requests_created on public.support_requests (created_at desc);

-- ============================================================================
--  Done. Verify after ~10-15 min of polling:
--    select airport, flight, reg, type_code, dest_iata, gate
--    from airport_delay_obs where reg is not null
--    order by updated_at desc limit 10;
-- ============================================================================
