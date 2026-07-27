-- Daily Briefing (P10, Owner-Freigabe 27.07.2026): LHs offizieller Hotel-
-- Klarname aus COMMON_CREW_ROTATION ERGÄNZT den crowdgesourcten Verzeichnis-
-- Eintrag — er ersetzt ihn nie. `hotel`, `transfer_min`, `votes`, `status`
-- bleiben unangetastet (harte Owner-Regeln); die Herkunft des Namens ist über
-- official_name_source/-_at nachvollziehbar und die Anreicherung damit
-- rücknehmbar (Spalten nullen = Zustand davor).
ALTER TABLE crew_hotel_directory
  ADD COLUMN IF NOT EXISTS official_name text,
  ADD COLUMN IF NOT EXISTS official_name_source text,
  ADD COLUMN IF NOT EXISTS official_name_at timestamptz;

NOTIFY pgrst, 'reload schema';
