-- Durable client-visible lifecycle for manual logbook imports. Reuses the
-- existing private operator inbox; id is the job_id returned to the app.
alter table public.ax_logbook_upload
    add column if not exists status text not null default 'pending',
    add column if not exists completed_at timestamptz,
    add column if not exists push_enqueued_at timestamptz;

do $$
begin
    alter table public.ax_logbook_upload
        add constraint ax_logbook_upload_status_check
        check (status in ('pending', 'processing', 'completed', 'failed'));
exception
    when duplicate_object then null;
end $$;

-- Old processed rows predate the client contract. Make their terminal state
-- truthful, without claiming that they received a completion push.
update public.ax_logbook_upload
set status = 'completed', completed_at = coalesce(completed_at, now())
where processed = true and status = 'pending';

create index if not exists idx_ax_logbook_upload_owner_status
    on public.ax_logbook_upload (token, id, status);
