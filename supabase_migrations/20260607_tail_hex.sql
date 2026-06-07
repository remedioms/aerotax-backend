-- Tail-Registration → ICAO24-Hex Mapping (Aircraft-Tracker-Stammdaten, 2026-06-07).
--
-- Ersetzt den hartkodierten _BACKEND_REG_HEX-Dict im adsb_blueprint als
-- PRIMÄRE Reg→Hex-Quelle für den "Flieger nach Registration"-Tracker. Befüllt
-- aus der frei verfügbaren OpenSky-Aircraft-Database (siehe
-- scripts/import_aircraft_db.py) — KEINE bezahlte Flug-API mehr.
--
-- Lookup-Fluss (resolve_reg_to_hex):
--   1) tail_hex (diese Tabelle, via Service-Role)   ← primär, ~hunderttausende Tails
--   2) _BACKEND_REG_HEX (hartkodiert im Blueprint)  ← Fallback, wenn SB down/Miss
--
-- Schema-Entscheidungen:
--  · PK registration (text) — eine Reg = ein Flugzeug. Upsert on_conflict=
--    'registration' überschreibt die Stammdaten bei jedem Monats-Refresh.
--  · icao24 ist der ADS-B-24-Bit-Hex (lowercase), den OpenSky/adsb.lol erwarten.
--  · type_code/operator sind informativ (Anzeige/Debug), nicht lookup-kritisch.
--  · updated_at protokolliert den letzten Import-Lauf (Monats-Refresh sichtbar).
--
-- Befüllung: scripts/import_aircraft_db.py manuell jetzt laufen lassen, danach
-- monatlich refreshen (manuell oder als Cron-Job). Idempotent, safe zu re-runnen.

create table if not exists public.tail_hex (
    registration   text          primary key,
    icao24         text          not null,
    type_code      text,
    operator       text,
    updated_at     timestamptz   not null default now()
);

-- Inverse-/Bulk-Lookups per Hex (Hex→Reg, Operator-Flotten-Queries).
create index if not exists idx_tail_hex_icao on public.tail_hex(icao24);

-- Service-Role-Key umgeht RLS. Anon-Client bleibt geblockt — die Tabelle wird
-- nur vom Import-Script (Service-Key) befüllt und vom Blueprint (Service-Key)
-- gelesen, nie direkt vom Client.
alter table public.tail_hex enable row level security;
