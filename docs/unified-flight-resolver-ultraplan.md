# AeroX Unified Flight Resolver — Ultraplan

> EIN Endpoint, EINE Wahrheit (Free-First-Kaskade iOS ⟷ Backend)
> Erstellt 2026-07-09 aus Multi-Agenten-Audit + Ultraplan-Workflow (code-verankert).
> Kritik-Verdikt: **GRÜN mit Auflagen** (6 Vor-Fixes, siehe unten).

## North Star

Jede Flugfrage (Route, Position, Zeiten, Board, Identität) wird an EINER autoritativen
Quelle beantwortet: Backend `POST /api/ax/flight` fährt eine Free-First-Kaskade
(aircraft_live echter Funkname → airport_delay_obs board-confirmed → warehouse/
operating-leg-lock → fr24_grpc gratis → paid NUR bei Unsicherheit/Widerspruch mit EINEM
Budget+Circuit-Breaker). iOS konsumiert das über EINE dünne `FlightResolver`-Actor-
Fassade mit EINEM `UnifiedFlight`-Codable, geteiltem TTL+Disk-Cache und In-Flight-De-Dup.
Die ~18 iOS-Screens mit Direkt-adsb werden Consumer-für-Consumer hinter Feature-Flag
migriert (kein Big-Bang). Jede Antwort trägt `source+confidence+obs_age` pro Feldgruppe
→ „Radar-Tap == Detail-Screen" garantiert.

## ⚠️ Wichtige Korrektur (Deploy)

Der Plan referenziert an mehreren Stellen Cloud-Run-Deploy (`./deploy.sh`, curl gegen
`aerotax-backend…run.app`). **Cloud Run ist GELÖSCHT.** Ersetzen durch:
`gcloud builds submit --tag …:<tag> .` → `bash ~/aerox-oracle-prep/deploy-hetzner.sh <ref>`;
Verifikation per SSH gegen Hetzner localhost:8080 bzw. `api.aerosteuer.de`.

## Phasen

### Phase 0 — Schema-Fundament (Index CONCURRENTLY + callsign_real), 0 Downtime
Additive Datengrundlage: `aircraft_live.callsign_real` (echter Funkname) + Frische-/
Routing-Indizes, ohne Harvester-Stall.
- Migration `supabase_migrations/20260710_aircraft_live_cache_indices.sql`: ADD COLUMN
  callsign_real; 3× CREATE INDEX CONCURRENTLY (callsign_real, seen_ts DESC, (dest,seen_ts));
  ax_route_cache + ax_aircraft_cache (jsonb, PK).
- Backfill `UPDATE aircraft_live SET callsign_real=callsign WHERE callsign_real IS NULL`.
- Harvester: `nas_harvester/schema.sql` ALTER + `ingest.py _flight_to_snapshot` trägt callsign_real.
- **Wartungsfenster:** Harvester (NAS-Cron) pausieren für CONCURRENTLY, danach neu starten.
- Exit: callsign_real befüllt, Index Scan per EXPLAIN, Harvester stabil.

### Phase 1 — Quick-Win #1: callsign_real als Identitäts-Wahrheit (Backend additiv, 0-Risiko)
LH1412-Falschroute an der Wurzel: echter Funkname (DLH8UA) steuert die Route.
- `_aircraft_live_flight` (aerox_data_blueprint.py:424): + callsign_real, ORDER BY seen_ts
  DESC, Match (1) flight (2) callsign_real≠NULL (3) callsign-Fallback.
- `_aircraft_live_pos` (:342): Frische-Gate gegen **seen_ts** statt updated_at.
- `warehouse_reader.route_for_flight` (:390): Quellen-Prio aircraft_live(callsign_real)→
  board→warehouse-leg→gRPC→paid.
- Exit: LH1412/DLH8UA korrekte Route source=aircraft_live+confirmed, make verify grün,
  `/api/ax/callsign`-Signatur unverändert (iOS-Compat).

### Phase 2 — Quick-Win #2 + #3: iOS geteilter Cache + Radar-Race umdrehen
- **NEU** `Models/FlightResolverCache.swift`: `UnifiedFlight` (alle Felder optional +
  source/confidence/obs_age pro Gruppe) + `actor FlightResolver{shared}` (60s TTL + In-
  Flight-De-Dup). Erst gegen bestehendes `/api/ax/callsign` (fast-tier).
- MyPlaneCard + TailHistorySection + TourTimeline ziehen aus DEMSELBEN Cache (3×→1×).
- **Offline:** bestehenden `Tracking/InboundAircraftStore.swift` ERWEITERN (Disk save/
  load/markStale) → Kaltstart nicht leer.
- Radar `fetchAreaRaced` (:1609): Backend-Task ZUERST; `focusReg` (:1723): via Resolver
  statt 5×2 sequenziell.
- Alles hinter `@AppStorage("feature.migration.unified_flight_v1")` default false.
- Exit: 1× Netz statt 3×, Radar-Pins <2s, focusReg <2s, Offline-Kaltstart nicht leer.

### Phase 3 — Backend Unified Orchestrator `POST /api/ax/flight`
Der EINE Endpoint (additiv neben ax_callsign). Request {identity, live_hint, date, own,
tier fast|full, want_track}.
- `_unified_flight_cascade`: aircraft_live → _route_from_obs (board) → _route_from_
  warehouse (leg-lock) → _route_from_fr24 → route_for_flight(free) → fr24_grpc.
- **gRPC-in-fast** NUR wenn aircraft_live UND board aktiv-leer (Blindzone Russland/Ozean).
- **Uncertainty→Paid**: confidence<0.5 ODER harte(board_dest)≠weiche(route.dst).
- **EIN Budget** `flight:YYYYMMDD` + Circuit-Breaker (fr24+paid verschmolzen).
- Fremd-Radar-Geometrie `_geometry_allows_route` (Großkreis-Kurs ±45°, kurze Hops skip).
- on_ground-Geometrie-Gate; `_callsign_to_iata_flightno` VOR _flight_obs_merged (Board).
- Response: UnifiedFlight + source/confidence/obs_age pro Gruppe + stale + upgradeable + _diagnostics.
- Exit: fast-Tier <200ms, gRPC-in-fast, Uncertainty→Paid + Budget greifen, alle pytest grün.

### Phase 4 — Consumer-für-Consumer-Migration (Feature-Flag, jederzeit grün)
~18 Screens auf `FlightResolver→/api/ax/flight`. STRIKT sequenziell, je 1-2 Tage:
MyPlaneCard → FlightSearchResult-Detail (kritisch, bestimmt heute Ladezeit) → FlightDetail/
AircraftDetail → CrewWhereCards → MyFlightsView → LiveFlightMapCard → SkyMap/Radar → Profile/
NowView/… **Position-Poll bleibt schnell/direkt (Position≠Identität/Route).** Alte APIClient-
Fn als @deprecated-Delegation (nicht löschen). Pro Consumer: Flag + Screenshot + commit.
- Exit: alle Consumer über Resolver, jeder Screen == Detail-Route, 0 Doppel-Calls.

### Phase 5 — Cleanup, Deprecation & Release
NUR nach voller Verifikation: Direkt-adsb für Identität/Route löschen (ADSBLolClient-
Telemetrie behalten, ADSBClient-Proxy behalten), @deprecated-Fn entfernen (0-Aufrufer-
grep), Altendpoints mit Deprecation-Datum, Flags weg, Release (iCloud-Build-Number-Falle
beachten). Ziel: FR24-Credits/Tag −50%.

## KPIs
- Radar-Tap == Detail-Screen (identische Route/Position aus EINER Quelle)
- focusReg <1s (war 9-35s) · MyPlaneCard <1s (war 3-5s) · Detail <1s (war 10-14s)
- Radar-Area erste Pins <2s · fast-Tier p99 <200ms
- 0 Doppel-Calls/Session · FR24+Paid-Credits/Tag −50%
- 0 Blindzonen-Regression · Kaltstart nicht leer · aircraft_live Index Scan

## ⚠️ 6 Vor-Fixes vor Umsetzung (aus der Kritik)
1. **CONCURRENTLY splitten**: CREATE INDEX CONCURRENTLY darf NICHT in einer TX/Multi-
   Statement-Migration — einzeln, TX-frei ausführen.
2. **focusReg-Reihenfolge**: reg-Lookup braucht `/api/ax/flight` (Phase 3), nicht das
   `<callsign>`-GET `/api/ax/callsign` → focusReg-Umbau auf Phase 4 schieben ODER Phase-2-
   reg-Fast-Pfad definieren.
3. **route_for_flight allow_paid=True (Default!) auditieren**: alle bestehenden Aufrufer,
   die es nicht setzen, geben HEUTE schon Paid frei → sonst Kostenregression statt −50%.
4. **NAS-Pfad nachziehen**: `_nas_live_pos` (NAS-RAM-Store, wird ZUERST gefragt) braucht
   callsign_real + seen_ts-Gate auch, sonst untergräbt der NAS-first-Pfad Phase 1.
5. **UnifiedFlight schema_version + Disk-Cache-Migrationsregel**: alter Disk-Payload beim
   App-Update darf nicht crashen.
6. **Bulk-Endpoint für Radar-Area-Identity-Batch + Baseline-Messung**: N Einzel-POST pro
   Frame würde Budget/Breaker sofort triggern; und KPIs (−50%) brauchen eine VOR-Messung.

## Offene Owner-Entscheidungen
- Deprecation-Datum Altendpoints (Empf.: nach 48h Prod + <5% Alt-Traffic)
- Unified-Budget-Tages-Cap (konkrete Zahl fr24+paid)
- Uncertainty→Paid-Schwelle (<0.5 hart, oder feiner Family vs fremd?)
- date-Param-Semantik verbindlich (UTC YYYY-MM-DD empfohlen)
- targeted=True-Freigabe (welche Consumer außer Inbound/Family/Crew?)
- Wartungsfenster CONCURRENTLY (Low-Traffic-Slot)
- Feature-Flag-Granularität (pro Consumer vs Gruppe)
- ADSBLolClient-Telemetrie (dauerhaft direkt oder proxen?)
- Family Cloud-Run-Cold-Start… (jetzt Hetzner — hinfällig, aber fast-Tier muss NAS/Cache treffen)
