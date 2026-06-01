-- Rate-limit sliding-window buckets per (token, scope, window_sec).
-- Used by rate_limits/config.py for hard per-endpoint limits + 5/60s burst.
--
-- One row per (token, scope, window_sec). When the current epoch crosses
-- window_start_epoch + window_sec we treat the row as expired and start a
-- new window in-place (upsert on conflict).
--
-- We deliberately do not delete expired rows in the hot path -- a nightly
-- cleanup keeps the table small.

create table if not exists public.rate_limit_buckets (
    token              text   not null,
    scope              text   not null,
    window_sec         int    not null,
    window_start_epoch bigint not null,
    count              int    not null default 0,
    updated_at         timestamptz not null default now(),
    primary key (token, scope, window_sec)
);

create index if not exists idx_rl_buckets_updated
    on public.rate_limit_buckets(updated_at);

-- service-role key bypasses RLS; anon clients must never read/write this table
alter table public.rate_limit_buckets enable row level security;

-- Cleanup helper: deletes rows whose window has been expired for >2x window.
-- Schedule via pg_cron (Supabase extension) or run manually:
--   select public.rate_limit_buckets_cleanup();
create or replace function public.rate_limit_buckets_cleanup()
returns int
language plpgsql
as $$
declare
    deleted_count int;
begin
    delete from public.rate_limit_buckets
    where window_start_epoch + (window_sec * 2) < extract(epoch from now())::bigint;
    get diagnostics deleted_count = row_count;
    return deleted_count;
end;
$$;

-- pg_cron schedule (uncomment if pg_cron is enabled on the project):
--   select cron.schedule('rate_limit_buckets_cleanup', '17 3 * * *',
--                        'select public.rate_limit_buckets_cleanup();');
