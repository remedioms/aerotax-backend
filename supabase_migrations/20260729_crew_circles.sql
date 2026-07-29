-- KREISE (Owner 2026-07-29: „Hangouts werden kaum genutzt … Demografie-Filter
-- sind vom Tisch, keine Profildaten").
--
-- Der Owner will, dass ein Hangout an „nur Deutschsprachige" oder „nur Frauen"
-- adressiert werden kann — OHNE dass AeroX Sprache oder Geschlecht über
-- Menschen speichert. Ein KREIS löst das über SELBST-ZUORDNUNG: ein frei
-- benannter, öffentlich sichtbarer Topf, dem man selbst beitritt. Ein Hangout
-- mit `audience.circle_id` ist nur für aktive Mitglieder sichtbar
-- (serverseitig gefiltert, app.py: _hangout_row_matches).
--
-- Der Unterschied zur Demografie ist der springende Punkt: AeroX
-- KATEGORISIERT NIEMANDEN. Gespeichert wird nur, was jemand über sich selbst
-- erklärt hat, indem er auf „Beitreten" getippt hat. Es gibt kein Zuweisen
-- von aussen — weder durch den Ersteller noch durch den Server.
--
-- v1-GRENZEN (bewusst): kein Kreis-Chat, keine Rollen ausser „Ersteller",
-- keine Moderation (kein Kick/Report/Umbenennen) — Follow-up. `join_policy`
-- kennt nur 'open' und 'request'; bei 'request' landet der Beitritt als
-- `status='pending'` und der Ersteller bestätigt/lehnt ab (ohne das wäre
-- „auf Anfrage" eine Sackgasse). Mitglieder-LISTEN werden nie ausgeliefert —
-- nur die ANZAHL.
--
-- Solange diese Migration NICHT appliedet ist, degradiert alles sauber:
-- /api/user/circles antwortet `storage: 'unavailable'` (der Client blendet die
-- Fläche aus, statt „keine Kreise" zu behaupten), Schreibpfade antworten mit
-- `circles_unsupported` (503) statt still nichts zu speichern, und
-- `_circles_of_user` liefert eine leere Menge → ein kreis-adressierter Hangout
-- ist fail-closed für NIEMANDEN sichtbar ausser seinem Ersteller.
--
-- Die operativen Hangout-Filter (same_hotel / free_tomorrow / min_nights /
-- arriving_today / departing_tomorrow) brauchen bewusst KEINE Migration: sie
-- leben im bereits vorhandenen, offenen `manual_pins.audience`-jsonb
-- (20260728_hangout_audience.sql).

CREATE TABLE IF NOT EXISTS public.crew_circles (
  id           text PRIMARY KEY,
  name         text NOT NULL,
  emoji        text,
  color        text,
  join_policy  text NOT NULL DEFAULT 'open',   -- 'open' | 'request'
  created_by   text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.crew_circle_members (
  circle_id    text NOT NULL,
  user_token   text NOT NULL,
  role         text NOT NULL DEFAULT 'member', -- 'member' | 'owner'
  status       text NOT NULL DEFAULT 'active', -- 'active' | 'pending'
  joined_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (circle_id, user_token)
);

-- „In welchen Kreisen bin ich?" läuft bei JEDEM Hangout-Feed mit einem
-- kreis-adressierten Eintrag → eigener Index auf user_token.
CREATE INDEX IF NOT EXISTS idx_crew_circle_members_user
  ON public.crew_circle_members (user_token);

-- „Wie viele Mitglieder hat dieser Kreis?" (Listen-Endpoint).
CREATE INDEX IF NOT EXISTS idx_crew_circle_members_circle
  ON public.crew_circle_members (circle_id);

CREATE INDEX IF NOT EXISTS idx_crew_circles_created_by
  ON public.crew_circles (created_by);

NOTIFY pgrst, 'reload schema';
