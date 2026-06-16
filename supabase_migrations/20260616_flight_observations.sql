-- Selbst-bauende Flug-DB (flight_profile_blueprint.py, Stage 2).
-- Eine Beobachtung pro Flugnummer+Tag (Client meldet Live-ADS-B reg/type + Route).
-- Über die Zeit: typische Maschine, zuletzt gesehene Tails/Tage — kostenlos, kein
-- bezahltes Flugplan-API.

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

create index if not exists flight_obs_callsign_idx
    on public.flight_observations (callsign);
