-- Hangout: Zeitfenster + Zusagen (Owner 2026-07-29: „denk drüber nach, wie wir
-- Hangouts BESSER machen können — auch der Gruppenchat hat keine Informationen").
--
-- Zwei Lücken, die keine neue Datenerhebung über MENSCHEN brauchen:
--
-- 1) `meta` — Anzeige-Meta des Treffs, v1 nur das Zeitfenster:
--        {"v":1,"starts_at":"2026-08-01T14:00:00Z","ends_at":"2026-08-01T18:00:00Z"}
--    Gespeichert war bisher nur `pin_date` (= Ablauf-DATUM). Der Chat-Kontext
--    („Samstag 14:00–18:00") konnte die Uhrzeit deshalb gar nicht zeigen,
--    obwohl der Ersteller sie im Sheet eingegeben hat. Reine Anzeige-Strings,
--    serverseitig NICHT interpretiert (die Zeitzonen-Wahrheit hat der Client).
--
-- 2) `attendees` — Token-Liste der Zusagen („Bin dabei"):
--        ["AT-…","AT-…"]
--    Bis heute gab es KEINEN Beitritts-Mechanismus. „Beitreten" hieß den Chat
--    öffnen, und das wurde nur LOKAL auf dem Gerät vermerkt — niemand konnte
--    sehen, wer kommt. Der Ersteller wird beim Anlegen automatisch eingetragen.
--    Ausgeliefert werden NIE die rohen Tokens (= Bearer-Credential), sondern
--    Name/Avatar/opake match_id (app.py: _hangout_people).
--
-- Die VIBE-TAGS („Sportlich", „Nightlife", …) brauchen bewusst KEINE Migration:
-- sie filtern nicht und leben im bereits vorhandenen, offenen `audience`-jsonb
-- neben dem Freitext `note` (app.py: _hangout_vibes_normalize).
--
-- Alter/Geschlecht/Sprache stehen weiterhin NIRGENDS drin — der Owner hat
-- demografisches Targeting ausdrücklich ausgeschlossen; das Profil bleibt
-- unverändert.
--
-- Solange diese Migration NICHT appliedet ist, legt das Backend den Hangout
-- trotzdem an und lässt nur das Anzeige-Beiwerk fallen (Response-Feld
-- `degraded`). „Bin dabei" antwortet dann ehrlich mit
-- `attendees_unsupported` (503), statt eine Zusage still zu verlieren.
ALTER TABLE public.manual_pins
  ADD COLUMN IF NOT EXISTS meta jsonb;

ALTER TABLE public.manual_pins
  ADD COLUMN IF NOT EXISTS attendees jsonb;

NOTIFY pgrst, 'reload schema';
