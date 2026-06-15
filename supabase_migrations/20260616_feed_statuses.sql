-- Feed-Status: "verschwindende" 24h-Updates (feed_status_blueprint.py)
-- Ein aktiver Status pro User (user_token PK, Upsert ersetzt den vorigen).
-- Verfällt serverseitig über expires_at; abgelaufene Records werden beim
-- nächsten Read best-effort gelöscht. Disk-Fallback existiert, aber auf Cloud
-- Run (multi-instance, ephemer) ist diese Tabelle die verlässliche Quelle.

create table if not exists public.feed_statuses (
    user_token  text primary key,
    text        text not null,
    emoji       text,
    created_at  timestamptz not null default now(),
    expires_at  timestamptz not null
);

create index if not exists feed_statuses_expires_idx
    on public.feed_statuses (expires_at);
