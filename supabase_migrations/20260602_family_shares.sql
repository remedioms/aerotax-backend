-- ════════════════════════════════════════════════════════════════
--  Family-Watcher Shares  (Wave-1 BUG-002, 2026-06-02)
--
--  Crew gewährt einer Family-Person Lese-Zugriff auf bestimmte
--  Status-Felder (FamilyWatchClient.swift). Server filtert die
--  Feed-Response strikt nach `fields` — Family bekommt NIE Felder
--  die nicht hier in der Whitelist eingetragen wurden.
-- ════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS family_shares (
    crew_token   TEXT NOT NULL,
    family_token TEXT NOT NULL,
    relation     TEXT,
    fields       JSONB NOT NULL DEFAULT '[]',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ,
    deleted      BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (crew_token, family_token)
);

-- Lookup by family-token für GET /api/family-watch/<token>/feed
CREATE INDEX IF NOT EXISTS idx_family_shares_family
    ON family_shares (family_token) WHERE deleted = FALSE;

-- Lookup by crew-token für GET /api/family-share/<token>/list
CREATE INDEX IF NOT EXISTS idx_family_shares_crew
    ON family_shares (crew_token) WHERE deleted = FALSE;

-- RLS: gleiche Pattern wie wall_posts / friend_groups → service_role darf alles,
-- anon/auth wird vom Backend-Server-Code geblockt (token-based auth, not RLS).
ALTER TABLE family_shares ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS family_shares_service_all ON family_shares;
CREATE POLICY family_shares_service_all ON family_shares
    FOR ALL TO service_role
    USING (true)
    WITH CHECK (true);
