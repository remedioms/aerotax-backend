-- ════════════════════════════════════════════════════════════════════════════
-- v10.4.1 Migration — file_hash + parser_version für Cache-Lookups
--
-- Ziel:
--   - Wenn dieselbe Datei (SHA-256) + selbe parser_version schon ausgewertet
--     wurde: Cache nutzen, kein Sonnet-Call.
--   - parser_version bumpen wenn DP-Reader-Logik sich ändert → automatische
--     Cache-Invalidierung.
-- ════════════════════════════════════════════════════════════════════════════

alter table public.job_chunks
  add column if not exists file_hash      text,
  add column if not exists parser_version text;

-- Cache-Lookup-Index: schnelle Suche nach completed chunks für (hash, doc, idx, version)
create index if not exists idx_job_chunks_cache_lookup
  on public.job_chunks (file_hash, document_type, chunk_index, parser_version)
  where status = 'completed';
