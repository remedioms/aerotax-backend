-- Die EINGETEILTE Pause, geteilt mit der Crew DESSELBEN Legs (Owner 2026-08-09).
--
-- Owner, wörtlich (Screenshot der Feed-Karte):
--   „leute in der gleichen crew sehen es auch auf deren feed. sobald einer der
--    purser die pause berechnet. sonst sehen sie es nicht"
-- Präzisierung derselben Runde:
--   „und natürlich nur cabin crews und pursers.. da cockpit eine andere
--    aufteilung macht.. die sehen deren.. auch nur der flug an dem tag"
--
-- ── WAS HIER DRIN STEHT — UND WAS NICHT ────────────────────────────────────
-- `plan` enthält AUSSCHLIESSLICH ZEITEN: [{nummer, start_min, end_min}].
-- KEINE Namen, KEINE Personalnummern, KEINE Positionen einzelner Personen.
-- Die Einteilung sagt „Ruhezeit 1: 15:05–16:23" — nicht, wer darin liegt.
-- Der einzige Personenbezug ist `author_token` (der Schreibende selbst); daraus
-- löst der Endpoint für berechtigte Leser den ANZEIGENAMEN aus dem Profil des
-- Autors auf („von X eingeteilt"), damit eine Änderung nachvollziehbar ist.
-- Der Server RÄT dabei nie: es gibt keinen Namens-Abgleich, keine Ähnlichkeit,
-- keine Ableitung aus Crew-Listen.
--
-- ── DER SCHLÜSSEL BINDET FLUG **UND** DATUM ────────────────────────────────
-- PK (flight, flight_date, bereich). Kein ±1-Tag-Fenster, nirgends — weder
-- beim Lesen noch beim Roster-Beweis. Im Projekt ist am 06.08. eine ganze
-- Fehlerfamilie daran hochgegangen, dass ein Schlüssel den NACHBARTAG mitfing
-- (Cross-Date-Guard, Condor-Briefing-Anker), und am 02.08. hat die Flugnummer
-- OHNE Datum bei LH455 SFO→FRA die Vortags-Instanz erwischt. `dep`/`arr` sind
-- zusätzlich gespeichert, damit ein Leser die Instanz gegenprüfen kann
-- (gleiche Flugnummer zweimal am selben Tag) — sie sind KEIN Teil des PK,
-- weil nicht jede Roster-Quelle beide Stationen führt.
--
-- ── BEREICH TRENNT STRIKT ──────────────────────────────────────────────────
-- 'kabine' und 'cockpit' sind zwei getrennte Zeilen mit zwei getrennten
-- Sichtbarkeiten. Der Bereich ist Teil des Schlüssels; niemand sieht die
-- Einteilung der anderen Gruppe. Ist die Rolle des Anfragenden nicht lesbar,
-- gibt es NICHTS (fail-closed) — die Entscheidung fällt serverseitig in
-- blueprints/rest_assignments.py, nicht im Client.
--
-- ── ZUGRIFF ────────────────────────────────────────────────────────────────
-- Nur der Service-Role-Key (Backend) greift zu — RLS an, keine anon-Policy.
-- Gelesen wird ausschliesslich über GET /api/rest-assignment/…, und der
-- Endpoint verlangt vorher den Roster-Beweis (roster_snapshots: der
-- Anfragende hat GENAU DIESES Leg an GENAU DIESEM Tag im EIGENEN Dienstplan).
--
-- ADDITIV: alte App-Builds kennen den Endpoint nicht und verlieren nichts.
-- Fehlt diese Tabelle, degradiert der Blueprint lautlos auf „keine Einteilung"
-- — die lokale Einteilung auf dem Gerät des Einteilenden bleibt unberührt.
--
-- Anwenden: dieses SQL im Supabase SQL-Editor ausführen (VOR dem Deploy des
-- Backends ist unnötig, aber unschädlich — der Code lebt ohne die Tabelle).

create table if not exists public.rest_assignments (
    flight       text not null,          -- normalisiert, z. B. 'LH462'
    flight_date  text not null,          -- YYYY-MM-DD, LOKALES ABFLUGDATUM
    bereich      text not null,          -- 'kabine' | 'cockpit'
    dep          text,                   -- IATA, Gegenprobe der Flug-Instanz
    arr          text,                   -- IATA, Gegenprobe der Flug-Instanz
    author_token text not null,          -- wer eingeteilt hat (Anzeige „von X")
    plan         jsonb not null,         -- {v, clock, ruhen:[{nummer,start_min,end_min}]}
    updated_at   double precision not null default extract(epoch from now()),
    primary key (flight, flight_date, bereich),
    constraint rest_assignments_bereich_chk
        check (bereich in ('kabine', 'cockpit'))
);

-- Lese-Pfad ist der PK (flight, flight_date, bereich) — der braucht keinen
-- eigenen Index. Dieser hier ist der AUFRÄUM-Pfad: der Account-Delete löscht
-- token-scoped (DSGVO Art. 17, app.py `_cascade`), und der Prune löscht nach
-- Flugdatum.
create index if not exists rest_assignments_author_idx
    on public.rest_assignments (author_token);
create index if not exists rest_assignments_prune_idx
    on public.rest_assignments (flight_date);

alter table public.rest_assignments enable row level security;

comment on table public.rest_assignments is
  'Eingeteilte Ruhezeiten eines Legs, geteilt mit der Crew DESSELBEN Legs (Pausenrechner, Owner 2026-08-09). Eine Zeile je (Flug, Flugdatum, Bereich). plan enthaelt NUR Zeiten - keine Namen, keine Personalnummern, keine Positionen. Geschrieben von PUT /api/rest-assignment/<token> (Kabine: Purser; Cockpit: Cockpit), gelesen von GET /api/rest-assignment/<token>/<flight>/<date>/<bereich> - und nur von jemandem, der dieses Leg an diesem Tag im eigenen Roster hat.';
