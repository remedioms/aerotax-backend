-- AeroX ADS-B poller: beschleunigt den bestehenden Keyframe-Lookup nach ICAO24.
--
-- Rein additive Migration: keine Spalte, Policy, Response oder bestehende Route
-- ändert sich. Alte App-Builds profitieren automatisch von schnelleren Poll-Ticks.
-- Partial Index hält die Struktur klein, weil Rows ohne Hex nie gesucht werden.

create index if not exists idx_aircraft_positions_hex24
    on public.aircraft_positions (hex24)
    where hex24 is not null;
