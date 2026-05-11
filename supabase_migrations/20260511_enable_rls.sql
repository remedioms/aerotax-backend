-- ════════════════════════════════════════════════════════════════════════════
-- v11 B-003 Security Migration — RLS für alle aerotax-Tables aktivieren
--
-- Hintergrund:
--   Bisher war RLS auf jobs/sessions/pdfs/uploaded_files/job_chunks deaktiviert.
--   Bei Kompromittierung des service_role-Keys gäbe das voller Read/Write-Zugriff
--   via anon/authenticated-Client — defense-in-depth-Lücke.
--
-- Architektur-Kontext:
--   - Backend (Flask) nutzt service_role-Key → bypasst RLS automatisch.
--   - Frontend (Cloudflare Pages, static index.html) nutzt KEIN Supabase-SDK
--     direkt — alle DB-Zugriffe gehen via Flask-Backend.
--   - anon/authenticated-Rollen sollen daher KEINEN Zugriff haben.
--
-- Strategie:
--   - RLS auf allen 5 Tables aktivieren
--   - KEINE policies für anon/authenticated → impliziter DENY für alle Operationen
--   - service_role bypasst RLS standardmäßig → Backend funktioniert unverändert
--
-- Validierung nach Anwendung:
--   - Backend-Health-Check sollte weiter sb.table('sessions').select(...) können
--   - anon-Client (z.B. via REST-API mit anon-Key) sollte 0 Rows zurückbekommen
-- ════════════════════════════════════════════════════════════════════════════

alter table public.jobs            enable row level security;
alter table public.sessions        enable row level security;
alter table public.pdfs            enable row level security;
alter table public.uploaded_files  enable row level security;
alter table public.job_chunks      enable row level security;

-- Explizit FORCE RLS, damit auch table-owner (BYPASSRLS-Rollen ausgenommen)
-- die Policies durchlaufen. service_role hat BYPASSRLS und ist nicht betroffen.
alter table public.jobs            force row level security;
alter table public.sessions        force row level security;
alter table public.pdfs            force row level security;
alter table public.uploaded_files  force row level security;
alter table public.job_chunks      force row level security;

-- Keine Policies — impliziter DENY für anon und authenticated.
-- Falls in Zukunft direkter Client-Zugriff nötig wird: gezielt policies hinzufügen.
