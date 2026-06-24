-- Airport-Delay-Beobachtungen: AIRCRAFT-Identität persistieren (2026-06-23).
--
-- PROBLEM: Die Board-Polls (Tafel) sehen pro Flug bereits die Maschine (Tail-
-- Registrierung `aircraft_reg` und Typ-Code, z.B. D-AIXG / A21N), aber der
-- Write-Through nach airport_delay_obs verwarf reg/type still — es gab keine
-- Spalten dafür. Folge: man konnte aus der self-growing DB zwar Flugnummer +
-- Verspätung + von→nach lesen, aber NICHT „welche Maschine flog Flug X" oder
-- „wo war Tail Y unterwegs". Aircraft-Suche/Flight-Aircraft fiel auf den teuren
-- Drittanbieter zurück oder lieferte nichts.
--
-- Diese Migration rüstet zwei NULLABLE Spalten idempotent nach (analog
-- 20260614_fullfields). Abwärtskompatibel: der manuelle UPDATE→INSERT-Upsert im
-- Backend hängt NICHT von ihnen ab; bis die Migration läuft, schreibt das Backend
-- über einen Schema-Safe-Fallback weiterhin ohne reg/type (Delay-Daten gehen NIE
-- verloren). Sobald die Spalten existieren, füllt jeder Board-Poll sie mit.

alter table public.airport_delay_obs
    add column if not exists reg          text;   -- Tail-Registrierung, z.B. D-AIXG
alter table public.airport_delay_obs
    add column if not exists type_code    text;   -- ICAO/IATA-Typ-Code, z.B. A21N

-- Query-Pfade der neuen Endpunkte:
--   /api/ax/flight-info/<flightno>      → WHERE flight = ?
--   /api/ax/aircraft-history/<reg>      → WHERE reg = ?
-- Indizes machen diese Lookups über die wachsende Multi-Airport-Historie schnell.
create index if not exists idx_airport_delay_obs_flight
    on public.airport_delay_obs(flight);
create index if not exists idx_airport_delay_obs_reg
    on public.airport_delay_obs(reg);
