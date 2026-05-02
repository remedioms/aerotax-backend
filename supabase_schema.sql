-- ═══════════════════════════════════════════════════════════════
-- AEROSTEUER SUPABASE SCHEMA
-- Im Supabase Dashboard → SQL Editor → Paste + Run
-- ═══════════════════════════════════════════════════════════════

-- ─── Q&A FORUM ───
CREATE TABLE IF NOT EXISTS questions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  codename TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  tags TEXT[] DEFAULT '{}',
  aerotax_answer TEXT,
  aerotax_answered_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_questions_created ON questions (created_at DESC);

CREATE TABLE IF NOT EXISTS answers (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  question_id UUID NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
  codename TEXT NOT NULL,
  body TEXT NOT NULL,
  reply_to UUID,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_answers_question ON answers (question_id, created_at DESC);

-- Upvotes mit IP-Hash-Dedupe + 30-Tage-Decay-Tracking
CREATE TABLE IF NOT EXISTS upvotes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  target_type TEXT NOT NULL CHECK (target_type IN ('question','answer')),
  target_id UUID NOT NULL,
  ip_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (target_type, target_id, ip_hash)
);
CREATE INDEX IF NOT EXISTS idx_upvotes_target ON upvotes (target_type, target_id, created_at DESC);

-- ─── SESSIONS (Chat + Result-Recall) ───
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  job_id TEXT,
  result_data JSONB,
  notes JSONB DEFAULT '[]',
  download_url TEXT,
  chat_history JSONB DEFAULT '[]',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions (expires_at);

-- ─── PDF PERSISTENZ (überlebt Render-Restarts) ───
CREATE TABLE IF NOT EXISTS pdfs (
  token TEXT PRIMARY KEY,
  filename TEXT NOT NULL,
  pdf_b64 TEXT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pdfs_expires ON pdfs (expires_at);
ALTER TABLE pdfs DISABLE ROW LEVEL SECURITY;

-- ─── AUDIT LOG (optional Compliance) ───
CREATE TABLE IF NOT EXISTS audit_logs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  job_id TEXT NOT NULL,
  event TEXT NOT NULL,
  data JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_audit_job ON audit_logs (job_id, created_at);

-- ─── RLS DISABLEN (wir nutzen service_role key, kein Public-Auth) ───
ALTER TABLE questions DISABLE ROW LEVEL SECURITY;
ALTER TABLE answers DISABLE ROW LEVEL SECURITY;
ALTER TABLE upvotes DISABLE ROW LEVEL SECURITY;
ALTER TABLE sessions DISABLE ROW LEVEL SECURITY;
ALTER TABLE audit_logs DISABLE ROW LEVEL SECURITY;
ALTER TABLE pdfs DISABLE ROW LEVEL SECURITY;

-- ─── Cleanup-Funktion: alte Sessions löschen ───
CREATE OR REPLACE FUNCTION cleanup_expired_sessions()
RETURNS INTEGER AS $$
DECLARE deleted_count INTEGER;
BEGIN
  DELETE FROM sessions WHERE expires_at < NOW();
  GET DIAGNOSTICS deleted_count = ROW_COUNT;
  RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;

-- Fertig. Tabellen + Indices + RLS-Disable sind drin.
