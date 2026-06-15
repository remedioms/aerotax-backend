-- Feed-Status: Family → Crew „verschwindende" 24h-Nachricht (feed_status_blueprint.py).
-- Eine aktive Nachricht pro Family-Person (family_token PK); crew_token = Empfänger.
-- Verfällt serverseitig über expires_at. Auf Cloud Run (multi-instance, ephemer)
-- ist diese Tabelle die verlässliche Quelle (Disk-Fallback nur Single-Instance).

create table if not exists public.feed_statuses (
    family_token  text primary key,
    crew_token    text not null,
    from_name     text,
    from_avatar   text,
    relation      text,
    text          text not null,
    emoji         text,
    created_at    timestamptz not null default now(),
    expires_at    timestamptz not null
);

create index if not exists feed_statuses_crew_idx
    on public.feed_statuses (crew_token);
create index if not exists feed_statuses_expires_idx
    on public.feed_statuses (expires_at);
