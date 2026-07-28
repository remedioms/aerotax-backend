-- GETEILTER Crew-Cache über User-Grenzen hinweg (Owner 2026-07-28:
-- „Crew mit dem gleichen Flug hat die Liste im Backend").
--
-- COMMON_CREWLIST ist FLUG-bezogen: die Liste für LH454/2026-07-28/FRA-SFO ist
-- für jedes Mitglied dieselbe. Seit 2026-07-28 liest /api/lh/flightops/crewlist
-- deshalb NICHT mehr nur die eigene Zeile, sondern sucht die JÜNGSTE Zeile
-- dieses Legs — egal unter welchem Token sie steht — und liefert sie ohne
-- LH-Call aus (Berechtigung wird vorher separat nachgewiesen, siehe
-- _crew_shared_serve in blueprints/lh_flightops.py).
--
-- Der Primary Key ist (token, flight, flight_date); ein Select, der NUR auf
-- flight + flight_date filtert, kann sein führendes Token-Präfix nicht nutzen
-- und liefe auf einen Seq-Scan. Dieser Index ist genau dafür da.
--
-- ADDITIV und idempotent: der Code funktioniert auch OHNE diese Migration
-- (gleiches Ergebnis, nur langsamer). Anwenden im Supabase SQL-Editor.

create index if not exists flightops_crew_cache_shared_idx
    on public.flightops_crew_cache (flight, flight_date);

comment on index public.flightops_crew_cache_shared_idx is
  'Lookup-Pfad des GETEILTEN Crew-Cache: select ... where flight = ? and flight_date in (?,?,?) — ohne Token-Filter (die Crew-Liste eines Legs ist fuer alle Mitglieder dieselbe). Ergaenzt den PK (token,flight,flight_date), der fuer diesen Zugriff nicht greift.';
