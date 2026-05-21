create table if not exists public.recovery_tokens (
    token         text         primary key,
    job_id        uuid,
    created_at    timestamptz  not null default now(),
    retries_used  integer      not null default 0,
    expires_at    timestamptz  not null,
    metadata      jsonb        not null default '{}'::jsonb
);

create index if not exists idx_recovery_tokens_expires_at
    on public.recovery_tokens(expires_at);

create index if not exists idx_recovery_tokens_job_id
    on public.recovery_tokens(job_id);

alter table public.recovery_tokens enable row level security;
