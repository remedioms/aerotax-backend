-- ════════════════════════════════════════════════════════════════════════════
-- v10.4 Migration — job_chunks für chunked DP-Reader-Pipeline
--
-- Ziel:
--   - Pro Chunk wird das Sonnet-Ergebnis sofort persistiert
--   - Memory-Pressure auf Render Free-Tier reduziert
--   - Resume nach Restart möglich (completed chunks bleiben)
--   - Result_json enthält NUR strukturierte Tagesdaten, KEINE PDF-Bytes/base64
-- ════════════════════════════════════════════════════════════════════════════

create extension if not exists "pgcrypto";

create table if not exists public.job_chunks (
  id              uuid primary key default gen_random_uuid(),
  job_id          text not null,
  document_type   text not null,
  chunk_index     integer not null,
  page_from       integer,
  page_to         integer,
  status          text not null default 'pending',
  result_json     jsonb,
  error_code      text,
  error_message   text,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create index if not exists idx_job_chunks_job_id
  on public.job_chunks(job_id);

create index if not exists idx_job_chunks_status
  on public.job_chunks(status);

create index if not exists idx_job_chunks_updated_at
  on public.job_chunks(updated_at);

create unique index if not exists idx_job_chunks_unique
  on public.job_chunks(job_id, document_type, chunk_index);


-- RLS aus — Backend nutzt Service-Role
alter table public.job_chunks disable row level security;


-- aerotax_cleanup_old_state erweitern um job_chunks (7-Tage Cutoff wie jobs)
create or replace function public.aerotax_cleanup_old_state()
returns json
language plpgsql
as $$
declare
  v_sessions_deleted int := 0;
  v_jobs_deleted int := 0;
  v_pdfs_deleted int := 0;
  v_uploads_deleted int := 0;
  v_chunks_deleted int := 0;
begin
  -- Sessions: expires_at < now (24h TTL)
  delete from public.sessions
  where expires_at is not null and expires_at < now();
  get diagnostics v_sessions_deleted = row_count;

  -- Jobs: 7-Tage Cutoff via updated_at
  delete from public.jobs
  where updated_at is not null and updated_at < now() - interval '7 days';
  get diagnostics v_jobs_deleted = row_count;

  -- PDFs: 24h TTL
  delete from public.pdfs
  where expires_at is not null and expires_at < now();
  get diagnostics v_pdfs_deleted = row_count;

  -- Uploaded-Files: 4h TTL
  delete from public.uploaded_files
  where expires_at is not null and expires_at < now();
  get diagnostics v_uploads_deleted = row_count;

  -- v10.4: job_chunks 7-Tage Cutoff (synchron zur jobs-TTL)
  delete from public.job_chunks
  where updated_at is not null and updated_at < now() - interval '7 days';
  get diagnostics v_chunks_deleted = row_count;

  return json_build_object(
    'sessions_deleted', v_sessions_deleted,
    'jobs_deleted',     v_jobs_deleted,
    'pdfs_deleted',     v_pdfs_deleted,
    'uploads_deleted',  v_uploads_deleted,
    'chunks_deleted',   v_chunks_deleted
  );
end;
$$;
