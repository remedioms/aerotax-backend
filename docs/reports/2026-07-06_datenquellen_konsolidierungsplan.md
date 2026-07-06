# FINALER KONSOLIDIERUNGSPLAN — Eine Wahrheit pro Flug, ban-sicher bei 5000 Usern

## (A) KERNPROBLEM (3 Sätze)

Dieselbe Frage ("wo ist der Flieger / welche Route / fliegt er noch / welche Verspätung / welcher Hex") wird heute je Screen aus 4-5 konkurrierenden Quellen mit unterschiedlicher Priorität, Frische-Semantik und teils bezahlt (AeroDataBox/AviationStack) beantwortet, weshalb identische Flüge auf MyPlaneCard, Radar, Family-Karte, Freund-Profil und Flugsuche widersprüchliche Positionen/Routen/Status zeigen. Der neue gratis, ban-sichere `fr24_live`-Stream ist nirgends Primärquelle (nur Kaskaden-Step 2c über einen Insert-Zeit-maskierten In-Memory-Index), während ~24 iOS-Dateien UND mehrere Backend-User-Endpunkte externe Community-/Bezahl-APIs SYNCHRON pro Request anfassen — das verletzt bei 5000 Geräten/einer Cloud-Run-IP das Kern-Prinzip "nur Hintergrund-Harvester schreiben von extern, User lesen nur Tabellen". Der einzige tragfähige Fix ist EIN geteilter, tabellen-basierter Resolver pro Datentyp, der nach **echtem Beobachtungs-Zeitstempel** (nicht Quellen-Rang) entscheidet und den alle Screens über Backend-Endpunkte konsumieren.

---

## (B) DIE EINE RESOLVER-LOGIK JE DATENTYP

Neues Modul `blueprints/warehouse_reader.py`. **Grundregel für alle vier: Auswahl nach max echtem `obs_ts`, nicht nach Tabellen-Rang.** Jede Kandidaten-Quelle liefert `(value, obs_ts, confidence, source)`; der Resolver nimmt den jüngsten Kandidaten oberhalb der Frische-Schwelle. Rang bricht nur Gleichstände.

### 1. `position_for_flight(hex|reg|callsign, datum)`
Kandidaten einsammeln, jüngsten frischen `obs_ts` gewinnen lassen:
1. **fr24_live** — direkter Read per `hex` (sekundär `callsign`). **NUR wenn `pos_ts` (echte FR24-Beobachtungszeit, NICHT `updated_at`/Erntezeit) frisch UND `estimated=false`.** fr24-Rows mit `estimated=1` oder `pos_ts` > ~90 s zählen als `source='estimated'`, niedrige confidence.
2. **aircraft_positions** — Warm-Persist; `registration→hex` via `tail_hex` normalisiert; nur wenn `last_seen_unix` frisch (age<90 s als „confirmed", sonst ehrlicher `rec_ts` zurückgeben — das bestehende Muster `_backfill_cache_from_sb:1936-1960` NICHT durch fr24-primär-by-rank rückgängig machen).
3. **In-Mem `_CACHE`** (60 s) — nur Poller-warm.
4. **AeroDataBox** — nur `targeted=own/watch`, hinter zentralem atomarem Budget-Guard, letzter Tier.
5. **Interpolations-Anker** (Great-Circle aus `airport_delay_obs`/`flights` dep/arr) — IMMER `source='estimated'`, eigene niedrige confidence, überschreibt NIE einen echten Fix, und wird von `status_for_flight` gegatet (kein airborne-Track wenn Phase=landed/grounded).

Konsumenten: `get_adsb_state`, `_machine_live`/`ax_flight_live`, `resolve_position_for_watch` (Family **und** Freunde), `aircraft_by_reg`, `get_adsb_area` (aus spatial-indizierter Positions-Tabelle, s. C), `/api/aviation/aircraft` (umbiegen oder droppen).

### 2. `route_for_flight(callsign|reg, datum)`
1. **ax_route_cache** — NUR `CS@YYYYMMDD` und `REG:<reg>@YYYYMMDD`. **Nackter-CS-Key wird gelöscht, nicht „freshness-gated"** (an Mehr-Leg-Tagen nicht disambiguierbar).
2. **airport_delay_obs** (`_route_from_obs`).
3. **flights** (`_route_from_warehouse`).
4. **fr24_live** (`_route_from_fr24`) — Toleranz nur MIT expliziter ICAO→IATA-Normtabelle; Einzelseiten-/Halb-Routen werden REJECTED (nie über eine confirmed Voll-Route ranken).
5. OpenSky → 6. AeroDataBox → 7. AviationStack (alle budget-gated, nur wenn kein freier Treffer).

**EIN Geometrie-Reject-Gate zentral. Für den Suchpfad (keine Live-lat/lon) ersetzt ein LEG-Zeitfenster-Gate das Positions-Gate** (nur Cache-Row akzeptieren, deren dep/arr-Fenster `now` einschließt) — sonst zeigt Suche weiter das vorige Leg (EZY29CT-Klasse). **EIN `confidence`-Feld in ALLEN Endpunkten**; `confirmed` NUR wenn eine beobachtete Quelle es trägt — ein cache_date-Read ohne bestandenes Gate ist NICHT automatisch `confirmed`.

### 3. `tail_hex_resolve(reg↔hex)`
1. **tail_hex** (einzige Wahrheit) → 2. baked-SQLite offline-Fallback → 3. hardcoded NUR als Seed (nach tail_hex importieren, kein Laufzeit-Fallback). Cron spiegelt `fr24_live(hex + row[2]-reg)` laufend nach `tail_hex`. iOS `AircraftRegistryLookup` client reg→hex **ersatzlos gestrichen**. Stammdaten: `/api/ax/aircraft` + `/api/aircraft-info/<reg>` auf EINEN Cache (`ax_aircraft_cache`).

### 4. `status_for_flight(callsign|reg, datum)` → `airborne|landed|grounded|cancelled|unknown`
Serverseitig, EIN kuratiertes Vokabular. Regeln:
- ADS-B/`fr24_live.on_ground=airborne` überstimmt stale Board-`grounded`.
- **`landed` NUR wenn (a) Board explizit `arrived/landed/gelandet/angekommen` ODER (b) `on_ground` am ZIEL-Airport nach beobachtetem airborne-Fix.** `at gate`/`on blocks`/`on ground` werden per **tokenisiertem** Match + Origin/Dest-Kontext klassifiziert, NICHT per Substring (`at gate` am Origin vor Pushback ≠ landed).
- `aircraft_by_reg` on_ground: Phase über recent-airborne + Origin-vs-Dest ableiten — **kein blindes Rename `at_gate`→`landed`** (on_ground gilt auch am Origin/Taxi-Out).
- Board-`airborne` zeigt Glyph auch OHNE ADS-B-Position (Owner-Leitprinzip).
- iOS klassifiziert NICHT mehr selbst: `DelayLogic` (iOS) und `_flight_window_state` (family_watch) entfallen als eigene Vokabulare; Backend liefert fertige Phase.

### 5. Delay/Zeit (Teil von status_for_flight-Payload)
`_flight_obs_merged` ist EINZIGE delay_min-Wahrheit (arr>dep D15-OTP, `delay_known`-Flag = „unbekannt≠pünktlich" überall). `route-history` konsumiert dessen Ergebnis statt zweitem Dual-Side-Merge. **Alle `est_*_iso` in UTC** (`_board_local_to_utc_iso`); Stations-Ortszeit-Strings aus `flight_status` entfernen (Tibor-Bug-Klasse). iOS `DelayPropagationEngine` bleibt `estimated` und weicht immer der gemessenen Zahl.

### Genau EINE Estimation-Engine
Backend liefert `estimated`-Position, **iOS interpoliert dann NICHT** (`simulatedCoordinate`/`FlyingRadarWidget`-Great-Circle nur aktiv wenn Backend gar keinen Punkt liefert). Nie beide gleichzeitig.

---

## (C) 5000-USER-FAN-OUT-REGEL + WAS FEHLT

**Regel:** Kein `@app.route`/`@*_bp.route`-Handler fasst je OpenSky/adsb.lol/adsb.fi/adsbdb/hexdb/planespotters/FR24/AeroDataBox/AviationStack/aviationweather/open-meteo/rainviewer/aero.de synchron an. User-Pfad = **Tabellen-Reader + throttled `_touch_watch`**; Miss = ehrlich „kein Signal". Externe Schreiber ausschließlich Hintergrund-Harvester/Poller.

**Harvester→Tabelle-Matrix (die einzigen externen Schreiber):**
- `fr24_harvester` (NAS/Residential, 15 Kacheln) → `fr24_live` (Position+Route+hex+reg) + NEU Cron → `tail_hex`.
- `/api/adsb/poll` (adsb_watch-Set) → `aircraft_positions` + `_CACHE`.
- NEU **dedizierter Area-Poller** (adsb.lol point-sweeps Hot-Metros FRA/MUC/…) → spatial-indizierte Positions-Tabelle für `/api/adsb/area`.
- `poll-boards`/`scrape-boards`/`eu-fill`/`eu_scraper` → `airport_delay_obs`.
- Warehouse-Poller (separat) → `flights`.
- `harvest-routes`-Cron (adsbdb) → `ax_route_cache`.
- NEU Wetter-Poller → `airport_wx_obs` (METAR/TAF).

**Was noch fehlt (harte Blocker vor Ausrollen):**
1. **FR24-Selbst-Harvest im User-Pfad KILLEN** (BLOCKING): `_fetch_fr24:1155-1157` `if not store_live: _fr24_refresh_one_tile()` ersatzlos streichen. Cross-Instance Kill-Switch `FR24_BACKEND_SELFHARVEST=0` (Default). Harvester-Ausfall darf NIE auf Selbst-Harvest der Cloud-Run-IP kippen.
2. **fr24_live Geo-Index** vor `/api/adsb/area`-Umstellung: PostGIS `geography(Point)`+GiST ODER btree `(lat,lon)` Partial `WHERE updated_at > now()-interval '15 min'`. Ohne das = Seq-Scan pro Pan.
3. **EU-Positions-Dichte NICHT über fr24-Tiles** (harvester.py:48 clippt bei 1500/Call) → dedizierter Area-Poller in spatiale Tabelle; fr24-EU-Tiles bleiben Route/Tail-Enrichment. Sonst Coverage-Regression im Hauptmarkt DE/EU.
4. **fr24 Frische ehrlich machen**: Harvester spiegelt `pos_ts` + `estimated`-Flag als eigene Spalten; `maxage` für Positions-Zweck von 14400 (4 h!) drastisch senken; `estimated=1` nur fürs Route-Enrichment.
5. **fr24_live-Prune-Cron aktiv schedulen** (delete >15 min) + hartes `pos_ts`-Frische-Gate in JEDEM Read.
6. **Atomarer Budget-Guard**: `20260705_budget_increment.sql` APPLIED verifizieren, `_BUDGET_RPC_DISABLED`/Kommentar mit Realität abgleichen, `_MEM_BUDGET` nicht mehr autoritativ. `_opensky_fill_budget_ok:31266` + `_delay_finalize_opensky_budget_ok:30087` unter denselben Guard.
7. **adsb_watch 2000-Cap** (`_load_active_watch:534`) → nach priority/Box paginieren/sharden ODER per Roster/Freundes-Set deduplizieren. Sonst kein Cold-Start-Backfill für Überhang-User.
8. **`_touch_watch` throttlen**: max 1 Upsert/≥5 min pro Hex; bei frischem Tabellen-/Cache-Treffer überspringen. Sonst wird 5000-User-Read-Last 1:1 zu adsb_watch-Write-Last.
9. **`_LIVE_FIX_MEMO` (In-Process)** → Supabase-basierte Ping-Reservierung, sonst „1 Ping/10 min" nur pro Container.
10. **RLS-Policies** für Read-Only-Pfad auf `fr24_live/aircraft_positions/airport_delay_obs/flights/ax_route_cache/tail_hex` (user/anon SELECT, kein world-open).
11. **CDN/HTTP-Cache-Header** (area/route/board/metar) als Pflicht gegen In-Process-Cache-Amplifikation bei N Containern.

---

## (D) AUFRÄUM-LISTE (raus)

**Backend paid im User-Pfad (ersetzen durch free-first + Hintergrund-Enrichment):**
- `/api/flight-times/<flightno>` (app.py:20916) AeroDataBox ungedeckelt → `_flight_obs_merged`.
- `/api/flight/<token>/status` (app.py:29588) ADB-first umdrehen; `_merged_status_fallback` `free_only=True` **erzwingen** (Default-False Paid-Leak, app.py:29554).
- `/api/airport/<token>/board` (app.py:29161) `allow_paid=False` + Poller-Vorfüllung.
- MUC aus `_BOARD_PREFER_AERODATABOX` (app.py:26189) → ein Cache-Slot statt zwei vergiftender.
- `/api/ax/route-history` `_aerodatabox_punctuality` (app.py:30911) → Hintergrund-Aggregation.
- `/api/aircraft-age/<hex>` (app.py:21004) unter Budget-Guard.
- `/api/ax/callsign` fast-path `_aerodatabox_route`/AviationStack (aerox_data:1534/1596/1607) → `fast=False` im Nicht-eigenen-Pfad.

**Toter/widersprüchlicher Code:**
- Nackter-CS-Key in `ax_route_cache` (aerox_data:2171).
- adsbdb-Generik auf allen Screens angleichen: bereits im Radar abgeschaltet (aerox_data:1581), lebt in `/api/ax/flight:2176`, `/api/ax/flight-route` (app.py:28086), `harvest-routes:2522`, iOS `FlightRouteProfileCard:55` → nur noch Hintergrund-Harvester.
- `_BACKEND_REG_HEX` (adsb:326) + iOS `AircraftRegistryLookup.hardcoded` + `_STATIC_FLEET` → als Seed in tail_hex, dann streichen.
- `/api/aviation/aircraft` (app.py:12455, OpenSky-only Duplikat) → auf `position_for_flight` oder droppen.
- Tote AeroDataBox-`own`-Stufe: `ADSBClient.fetchViaBackend:334` setzt nie `own=1` → verdrahten ODER Tier entfernen.

**iOS Direkt-Calls (alle auf Backend-Proxy — vollständige Liste inkl. Review-Nachträge):**
- ADS-B: `ADSBClient.swift:182/200`, `ADSBLolClient.swift`, `RadarAreaMapView.swift:1565/1631/1665/1936/1957/1991` (adsb.lol **+ adsb.fi:1633/1646/1665** + adsbdb + hexdb + planespotters), `RadarView.swift:1489`, `FlightSearchResult.swift:1105/1485`, `FlightRouteProfileCard.swift:55`, `MyFlightsView.swift:1082`, `AircraftDetailView.swift:752`, `LiveFlightMapCard.swift:750`, `CrewWhereCards.swift:1149`, `ProfileDetailView.swift`, `EventExtras.swift:152`, `AircraftRegistryLookup.swift:128`.
- **Wetter (Review-Lücke): `AviationWeatherClient.swift`/`WeatherService.swift` (aviationweather.gov + open-meteo), `RadarMapView/RadarView` RainViewer-Kacheln (tilecache/api.rainviewer.com).**
- **News: `AeroNewsService.swift` (aero.de-RSS) → Backend-News-Feed.**

**Tabellen/Migration:**
- `ax_route_cache`-DDL aus `docs/archive/PASTE_ME_AX_CACHE.sql` in aktive `supabase_migrations/` heben.
- `ax_crewbus`-Rollup-Reads final auf `ax_crewbus_obs`; Alt-Tabelle droppen.
- `airport_delay_obs.airport` `#ARR`-Suffix: PostgREST-`#`-Quoting-Guard verifizieren.
- **`flight_observations`**: einziger Feeder (iOS-adsb-post) stirbt durch iOS-Kappung → entweder Backend-Poller schreibt aus fr24_live/aircraft_positions weiter, ODER Tabelle+`flight_profile_blueprint.py:150-200` bewusst deprecaten. Nicht still sterben lassen.

---

## (E) PRIORISIERTE ROADMAP

| # | Schritt | Aufw. | Dateien |
|---|---------|-------|---------|
| 0 | **BLOCKER-Bündel**: FR24-Selbst-Harvest killen + Kill-Switch; fr24_live Geo-Index + `pos_ts`/`estimated`-Spalten + Prune-Cron; Budget-Migration 20260705 apply+atomar verifizieren; RLS-Policies | **M** | `adsb_blueprint.py:1035-1174`, `fr24_harvester/harvester.py:118/147`, `supabase_migrations/20260706_fr24_live.sql`, `20260705_budget_increment.sql`, `20260511_enable_rls.sql` |
| 1 | `warehouse_reader.position_for_flight()` — **by max obs_ts**, fr24 mit `pos_ts`-Gate, aircraft_positions age<90s-Muster, estimated nie über Fix | **L** | neu `warehouse_reader.py`; `adsb_blueprint.py:1177/1325/1936` |
| 2 | `_machine_live`/`ax_flight_live` + `aircraft_by_reg` auf Resolver; on_ground→Phase via recent-airborne+Origin/Dest | **M** | `aerox_data_blueprint.py:3165/3470/3512`, `app.py:29619/29703` |
| 3 | `resolve_position_for_watch` Family **und** Freunde verdrahten; `get_friends_today` liefert live_lat/lon; allow_paid-Hardcode klären | **M** | `family_watch.py:646/1041`, `app.py:11904` |
| 4 | Dedizierter EU-Area-Poller → spatiale Tabelle; `/api/adsb/area` + `/api/aviation/aircraft` daraus lesen | **M** | `adsb_blueprint.py:3208/3359`, `app.py:12455` |
| 5 | flight_status/board/flight-times umdrehen (free-first, `free_only=True` erzwingen); MUC entfernen; app.py-Paid unter zentralen Guard | **M** | `app.py:29507/29554/29588/20894/29051/20967/30870/26189` |
| 6 | `route_for_flight` — flight-route/flight auf Resolver; nackter-CS droppen; Leg-Fenster-Gate für Suche; fr24 ICAO→IATA-Normtabelle + Halb-Route-Reject; ein confidence-Feld | **L** | `aerox_data_blueprint.py:1450/1581/2152/2171/299`, `app.py:28075` |
| 7 | `status_for_flight` kanonisch, tokenisiert + Origin/Dest-Kontext; iOS klassifiziert nicht mehr | **M** | `app.py:27313`, `family_watch.py:529`, iOS `DelayLogic.swift:28`, `BoardFlightPosition.swift`, `CrewWhereCards.swift:269` |
| 8 | iOS EIN Proxy-Client; ALLE Direkt-Calls umbiegen (ADS-B **+ adsb.fi + Wetter + RainViewer + aero.de**); `AircraftRegistryLookup` reg→hex streichen; iOS-Interpolation nur bei Backend-Null | **L** | ~24 Swift-Dateien (s. D) |
| 9 | `tail_hex` einzige Reg↔Hex-Wahrheit; fr24→tail_hex-Cron; Stammdaten auf einen Cache | **M** | `adsb_blueprint.py:326/415/264`, `aircraft_info_blueprint.py`, `fr24_harvester/harvester.py:147` |
| 10 | Wetter als 4. konsolidierte Domäne: `weather_reader` + `airport_wx_obs` + Poller; 3 METAR + TAF zusammenlegen | **M** | `aerox_data_blueprint.py:2567`, `app.py:12341/12385/12650` |
| 11 | `_touch_watch` throttle; adsb_watch-Cap sharden; `_LIVE_FIX_MEMO`→Supabase | **M** | `adsb_blueprint.py:1401/534/2726`, `family_watch.py:575` |
| 12 | DDL-Hygiene (ax_route_cache/crewbus/#ARR); flight_observations-Zukunft; Roster→Flug-Key-Integritäts-Guard + crew_flight_assignments im Contract | **S** | `docs/archive/PASTE_ME_AX_CACHE.sql`, `20260702_crewbus.sql`, `flight_profile_blueprint.py:150` |
| 13 | **CI-Guard**: kein `@*.route` ruft synchron `_aerodatabox_*`/`aviationstack`/`_fetch_opensky`/`_fetch_adsb_lol`/`_fr24_refresh_one_tile`/aviationweather/open-meteo/rainviewer/adsb.fi/aero.de | **S** | neu `tests/test_no_sync_external.py` |

Reihenfolge-Logik: 0 zuerst (ohne die Blocker ist der Resolver unsicher/langsam), dann Position (1-4), dann Paid/Route/Status (5-7), dann iOS-Massen-Umbau (8, größter Ban-Hebel), Rest parallelisierbar. CI-Guard (13) sofort nach jedem Umbau greifen lassen.

---

## (F) ERWARTETE KOSTEN-/RISIKO-EINSPARUNG

**Direkt bezahlt (AeroDataBox/AviationStack) eliminiert aus dem User-Pfad:**
- `/api/flight-times`: praktisch alle synchronen ADB-Flight-Time-Units (heute ungedeckelt, skaliert 1:1 mit Suchen×5000) — **größter Einzelposten**.
- `/api/flight/status`: 1 ADB-Call pro Flugsuche → 0.
- `/api/airport/board` (breit gerufen: NowView/FeedSynergy/StandbyContext/FeedInboundAircraft/MyFlights): ADB pro Tafel-Tap für Nicht-native-Airports → 0 synchron.
- MUC: 2 ADB-Units/Tap → 0.
- route-history/aircraft-age/callsign-fast-path: ADB/AviationStack-Units pro Tap → nur noch budget-gated Hintergrund. Schützt insb. das knappe AviationStack-Free-Kontingent (90/Monat).
- Netto Paid: von „skaliert mit Nutzern×Taps, teils ungedeckelt" auf „nur Hintergrund-Enrichment hinter atomarem Cap" — bei 5000 Usern Größenordnung **>90 % Reduktion der synchronen Paid-Units** und Ende des N-Container-Overspend-Risikos.

**Ban-/Rate-Limit-Risiko (der eigentliche Existenz-Hebel):**
- ~24 iOS-Dateien × 5000 Geräte hämmern adsb.lol/adsb.fi/adsbdb/hexdb/planespotters/opensky/aviationweather/rainviewer direkt → **0 Geräte-Direkt-Calls**.
- FR24-Selbst-Harvest + OpenSky/adsb.lol-Kaskade von der einen Cloud-Run-IP → nur noch Residential-Harvester-Flotte + gedeckelte Poller. Single-IP-Block-Risiko eliminiert.
- Konsistenz: eine Position/Route/Status/Hex pro Flug über alle Screens → die vom Owner gemeldeten Widersprüche („Radar live, MyPlaneCard kein Signal", „Tafel zeigt Gate, Detail nicht", „falsche Rotation in der Suche") verschwinden strukturell, nicht per Einzelfix.

**Kritischer Vorbehalt aus den Reviews:** Die Einsparung ist nur real, wenn Blocker #0 (fr24 Frische/`pos_ts`, Geo-Index, atomarer Budget-Guard, EU-Area-Poller) VOR dem Umschalten steht — sonst tauscht man teuer+inkonsistent gegen billig+einheitlich-falsch (4h-alte estimated fr24-Fixe als „frisch", EU-Coverage-Regression, N-fach-Overspend).