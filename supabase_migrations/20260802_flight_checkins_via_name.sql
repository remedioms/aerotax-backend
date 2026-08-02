-- flight_checkins.via_name — ÜBER WESSEN BORDKARTE wurde eingecheckt?
--
-- ANLASS (Tibor, 02.08.2026): Tibor checkte sich über die Crew-Bordkarte bei
-- JULIENS Umlauf ein und bekam Stunden später „Landet bald · LH455". Er wusste
-- nicht, warum ihn dieser Flug etwas angeht — der Push nannte weder den Anlass
-- noch den Menschen, über den das Abo entstanden ist. Mit dieser Spalte heißt
-- der Titel „Juliens Flug · LH455"; ohne sie (alte App-Builds, Selbst-
-- Check-in) bleibt es beim neutralen „Dein Check-in · LH455".
--
-- WARUM EINE SPALTE UND KEINE ABLEITUNG: Der Server könnte theoretisch raten,
-- wer aus dem Bekanntenkreis auf diesem Flug sitzt. Genau das passiert hier
-- NICHT (Owner-Regel „keine Fake-Werte"): sitzen zwei Bekannte auf dem Flug,
-- stünde im Push ein FALSCHER Name — schlimmer als gar keiner. Den Namen kennt
-- nur der Client, der die Bordkarte anzeigt; er schickt ihn beim Check-in mit.
--
-- WAS HIER DRINSTEHT: der RUFNAME, wie ihn der Nutzer ohnehin schon vor sich
-- hatte — kein Nachname, keine ID, kein Token. Das Backend nimmt nur das erste
-- Wort und weist alles mit Ziffern/Steuerzeichen ab
-- (`flight_checkins.clean_via_name`).
--
-- KEIN WIDERSPRUCH ZUM DATENSCHUTZ-ABSATZ der Basis-Migration
-- (20260731_flight_checkins.sql, „keine Spalte, die den beobachteten Menschen
-- benennt"): Diese Spalte macht NIEMANDEM etwas sichtbar, was er nicht schon
-- gesehen hat. Der Wert fließt ausschließlich in die Pushes und die Abo-Liste
-- GENAU DES NUTZERS, der eingecheckt hat. Es gibt weiterhin keinen Endpoint,
-- der fremde Check-ins ausliefert, und der Beobachtete erfährt weiterhin
-- nichts. Mit dem Abo verschwindet auch der Name (Prune nach Flugtag +2).
--
-- Rein additiv: nullable, kein Default, kein Schreiber muss etwas ändern.
alter table public.flight_checkins
  add column if not exists via_name text;

-- PostgREST cached das Schema. Ohne dieses NOTIFY liefert die REST-API die
-- neue Spalte nicht aus und ein `select(...via_name)` scheitert mit „column
-- does not exist" (teuer gelernt: fcm_token-Spalte, 01.08.2026). Das Backend
-- fängt diesen Fall zwar ab (Fallback auf die alte Projektion), aber dann
-- bleiben die Titel dauerhaft namenlos.
notify pgrst, 'reload schema';
