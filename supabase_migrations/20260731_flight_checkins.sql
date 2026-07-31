-- Für einen Flug einchecken (Forum-Wunsch 2026-07-31) — Abo-Tabelle.
--
-- WAS DAS IST: ein Nutzer meldet sich auf der Crew-Bordkarte für EINEN
-- konkreten Flug an und bekommt dafür Ereignis-Pushes (abgeflogen, landet
-- voraussichtlich in etwa einer Stunde, gelandet). Mehr nicht.
--
-- WAS DAS AUSDRÜCKLICH NICHT IST — und warum die Tabelle so schmal ist:
-- KEINE Anwesenheits-Meldung. Ein Check-in macht NIEMANDEM sichtbar, dass
-- jemand auf diesem Flug sitzt oder ihn verfolgt. Es gibt deshalb bewusst
-- keine Spalte, die den beobachteten Menschen benennt, und keinen Endpoint,
-- der Check-ins fremder Nutzer ausliefert (Owner-Regel: „AeroX ist eine
-- Arbeits-App, ein Standort ist IMMER der Flughafen, nie eine private
-- Live-Ortung" — nichts sichtbar machen, was heute nicht sichtbar ist).
-- Wer dem anderen etwas mitteilen will, schickt eine Nachricht; DAS ist der
-- sichtbare, bewusst ausgelöste Weg.
--
-- flight_date ist das LOKALE Abflugdatum am Startflughafen — exakt der
-- Schlüssel, auf den die LH-MQTT-Topics keyen
-- (`blueprints/lh_mqtt._sector_topic_dates`). Ein UTC-Datum läge bei
-- Abendabflügen aus USA/Asien daneben und der Event-Fanout fände die Zeile
-- nie. Der Server rechnet den Wert selbst aus `dep_iso` + Airport-TZ, statt
-- dem Client zu vertrauen.
--
-- `sent` ist die Ereignis-Buchführung PRO ABO: welche der drei Meldungen ist
-- schon raus. Sie sitzt an der Zeile (nicht im Prozess), weil mehrere
-- Gunicorn-Worker und der Sweep unabhängig laufen — ein In-Memory-Set hätte
-- pro Worker eine eigene Wahrheit und würde dieselbe Meldung mehrfach
-- schicken. Der Outbox-idempotency_key ist die zweite Sicherung.
create table if not exists public.flight_checkins (
    id           bigserial primary key,
    user_token   text not null,
    flight_no    text not null,          -- normalisiert, z.B. 'LH455'
    flight_date  date not null,          -- LOKALES Abflugdatum am Startflughafen
    dep_iata     text,
    arr_iata     text,
    dep_iso      timestamptz,            -- geplanter/effektiver Abflug-Instant
    sent         jsonb not null default '{}'::jsonb,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),
    unique (user_token, flight_no, flight_date)
);

-- Der Event-Fanout fragt „wer hat für DIESEN Flug an DIESEM Datum
-- eingecheckt" — das ist der einzige heiße Lesepfad.
create index if not exists flight_checkins_flight_idx
    on public.flight_checkins (flight_no, flight_date);

-- Sweep (landet-in-1h, Aufräumen) liest tageweise.
create index if not exists flight_checkins_date_idx
    on public.flight_checkins (flight_date);

alter table public.flight_checkins enable row level security;
-- Kein policy-Grant: der Zugriff läuft ausschließlich über den
-- service_role-Client des Backends, das vorher den Bearer gegen den
-- user_token prüft (gleiche Bauart wie live_activities/push_outbox).
