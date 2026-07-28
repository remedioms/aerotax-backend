-- Hangout-Zielgruppe (Owner-Wunsch 28.07.2026: „ich kann nicht wählen, FÜR WEN
-- der Hangout ist"). Ein Hangout kann optional auf Airline / Homebase /
-- Cockpit|Kabine eingeschränkt werden; dazu kommt eine reine Anzeige-Zeile
-- („Wer passt dazu": „sportlich, Lust auf Kanu"), die NICHT filtert.
--
-- Form (v1, bewusst offen für weitere Keys):
--   {"v":1,
--    "airline":"LUFTHANSA","airline_label":"Lufthansa",  -- kanonischer Key
--    "base":"FRA",
--    "roles":["cockpit"],                                -- oder ["cabin"]
--    "note":"sportlich, Lust auf Kanu"}
-- NULL / {} = offen für alle (so bleiben ALLE bestehenden Hangouts sichtbar).
--
-- Gefiltert wird SERVERSEITIG beim Ausliefern (app.py:
-- _hangout_audience_matches) — ein nicht passender Viewer bekommt die Zeile
-- gar nicht erst. Alter/Geschlecht/Sprache stehen bewusst NICHT drin: die
-- Felder gibt es in user_profiles nicht, und v1 erfindet dafür keine neue
-- sensible Datenerhebung.
--
-- Solange diese Migration NICHT appliedet ist, lehnt das Backend das Anlegen
-- eines EINGESCHRÄNKTEN Hangouts mit `audience_unsupported` ab (statt ihn
-- still öffentlich zu speichern). Offene Hangouts funktionieren unverändert.
ALTER TABLE public.manual_pins
  ADD COLUMN IF NOT EXISTS audience jsonb;

NOTIFY pgrst, 'reload schema';
