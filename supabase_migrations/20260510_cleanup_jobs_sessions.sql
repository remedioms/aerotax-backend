-- ════════════════════════════════════════════════════════════════════════════
-- v10.3 Migration — Indexes + Cleanup-Function für jobs/sessions
--
-- Ziel:
--   - Performante Lookups + TTL-Cleanup ohne pg_cron-Zwang.
--   - Backend führt Cleanup im _cleanup_loop alle 30 Min aus.
--   - Diese Function ist optional zusätzlich via pg_cron schedulebar.
-- ════════════════════════════════════════════════════════════════════════════

-- Indexes auf den TTL-Spalten — Cleanup-DELETE-Queries werden Index-Scan statt Full-Scan
-- HINWEIS: jobs hat nur updated_at, kein expires_at (Cutoff = 7 Tage seit Update).
create index if not exists idx_jobs_updated_at
  on public.jobs (updated_at);

create index if not exists idx_sessions_expires_at
  on public.sessions (expires_at);

create index if not exists idx_pdfs_expires_at
  on public.pdfs (expires_at);

create index if not exists idx_uploaded_files_expires_at
  on public.uploaded_files (expires_at);


-- Cleanup-Function: löscht abgelaufene Sessions + alte Jobs.
-- Wird vom Backend im _cleanup_loop und optional via pg_cron aufgerufen.
create or replace function public.aerotax_cleanup_old_state()
returns json
language plpgsql
as $$
declare
  v_sessions_deleted int;
  v_jobs_deleted int;
  v_pdfs_deleted int;
  v_uploads_deleted int;
begin
  -- Sessions: expires_at < now (24h TTL)
  with deleted as (
    delete from public.sessions
    where expires_at is not null and expires_at < now()
    returning 1
  )
  select count(*) into v_sessions_deleted from deleted;

  -- Jobs: 7-Tage Cutoff (Debug-Window für Pilot-User; access-code ist 24h)
  -- jobs hat kein expires_at, nur updated_at — das reicht für die Cleanup-Regel.
  with deleted as (
    delete from public.jobs
    where updated_at is not null and updated_at < now() - interval '7 days'
    returning 1
  )
  select count(*) into v_jobs_deleted from deleted;

  -- PDFs: 24h TTL
  with deleted as (
    delete from public.pdfs
    where expires_at is not null and expires_at < now()
    returning 1
  )
  select count(*) into v_pdfs_deleted from deleted;

  -- Uploaded-Files: 4h TTL
  with deleted as (
    delete from public.uploaded_files
    where expires_at is not null and expires_at < now()
    returning 1
  )
  select count(*) into v_uploads_deleted from deleted;

  return json_build_object(
    'sessions_deleted', v_sessions_deleted,
    'jobs_deleted',     v_jobs_deleted,
    'pdfs_deleted',     v_pdfs_deleted,
    'uploads_deleted',  v_uploads_deleted
  );
end;
$$;


-- ────────────────────────────────────────────────────────────────────────────
-- Optional — falls pg_cron aktiviert ist, kann das Backend-Cleanup zusätzlich
-- nightly server-side laufen (redundant zur Backend-Schleife, aber als Safety-Net):
-- ────────────────────────────────────────────────────────────────────────────
-- select cron.schedule(
--   'aerotax-cleanup-nightly',
--   '0 3 * * *',
--   'select public.aerotax_cleanup_old_state();'
-- );
