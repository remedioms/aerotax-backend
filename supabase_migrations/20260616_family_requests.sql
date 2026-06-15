-- Family-Anfragen durable in Supabase (family_watch.py).
-- Vorher NUR auf Disk → auf Cloud Run (ephemer, multi-instance) sah die
-- Crew-Person die Anfrage der Familie nie („poppt nicht auf"). SB-primary fixt das.

create table if not exists public.family_requests (
    crew_token        text not null,
    family_token      text not null,
    relation          text,
    requester_name    text,
    requester_avatar  text,
    created_at        timestamptz not null default now(),
    primary key (crew_token, family_token)
);

create index if not exists family_requests_crew_idx
    on public.family_requests (crew_token);
