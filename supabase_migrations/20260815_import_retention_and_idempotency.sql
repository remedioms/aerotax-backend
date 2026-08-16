-- Close three production lifecycle gaps observed on 2026-08-15:
--   1. imported private files were retained forever,
--   2. a terminal `review` state had no actionable reason,
--   3. retried MetricKit/hotel writes could create byte-identical duplicates.

alter table public.ax_logbook_upload
    alter column data_b64 drop not null,
    add column if not exists error_code text,
    add column if not exists error_message text,
    add column if not exists payload_purged_at timestamptz,
    add column if not exists purge_after timestamptz;

create index if not exists idx_ax_logbook_upload_review_retention
    on public.ax_logbook_upload (purge_after)
    where status = 'review' and data_b64 is not null;

comment on column public.ax_logbook_upload.purge_after is
    'Review payload deletion deadline; completed/failed payloads are deleted immediately.';

alter table public.ax_crash_reports
    add column if not exists event_key text;

create unique index if not exists ax_crash_reports_event_key_uidx
    on public.ax_crash_reports (event_key)
    where event_key is not null;

alter table public.hotel_room_reports
    add column if not exists client_report_id text;

create unique index if not exists hotel_room_reports_client_id_uidx
    on public.hotel_room_reports (reported_by_token, client_report_id)
    where client_report_id is not null;
