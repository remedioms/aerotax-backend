# Unified Flight-Info Layer — Ultraplan v2 (2026-07-09)

> Owner-Vision: **„Alles an EINEM Ort, smart, zum direkt Lesen — Soll und Ist,
> Flugzeug, wann/wo/wie. Und wenn keine Info: von FR24 holen und DA cachen.
> Zero double-spend."**

Dieser Plan baut auf dem auf, was in dieser Session schon steht, und schließt die
letzte Lücke: die Board-Zeiten/Status/Ankunft sind heute über mehrere Tabellen +
Lese-Pfade verstreut und werden nur ad-hoc pro Screen gemerged (diese Session:
nur der Radar-Enrich). Ziel: **EINE Wahrheit pro Flug, EIN Merge, EIN Read.**

---

## 1. Was schon existiert (nicht neu bauen)

- **Route-Kaskade** `warehouse_reader.route_for_flight` mit `aircraft_live` Tier-0
  (FR24-gRPC-Harvest, gratis) → Radar-Tap == Detail == Routen-Karte (verifiziert).
- **Aggregat** `/api/ax/flight-detail/<q>` bündelt resolve+route+info+history+photo
  (~2s, memo 45s).
- **Enrich** `ax_radar_enrich` merged jetzt die Ankunft aus `airport_delay_obs`
  (`<Ziel>#ARR`) — aber NUR für den Radar-Pfad (der zu zentralisierende One-Off).

## 2. Die Datenquellen (alle gratis außer FR24-official)

| Quelle | Trägt | Kosten |
|---|---|---|
| `aircraft_live` (FR24-gRPC-Harvest) | Route, Position, Reg, Callsign | gratis |
| `airport_delay_obs` DEP-Row (`<AP>`) | Soll/Ist-Abflug, Gate, Terminal, Delay, Status | gratis |
| `airport_delay_obs` ARR-Row (`<AP>#ARR`) | **Soll/Ist-Ankunft**, Herkunft | gratis |
| `flights` Warehouse | board-verifizierte Route/Tail/Zeiten | gratis |
| Baked-SQLite Reference | Flugzeug-Typ/Baujahr/Config/Jumpseats, Airport-Koords | gratis (gebündelt) |
| `fr24_grpc` | Route/Detail/Position (Blindzonen) | gratis |
| **`fr24_official`** | alles, wenn nichts anderes reicht | **PAID → muss gecached werden** |

## 3. Zielarchitektur

### 3a. Das eine Modell — `UnifiedFlight`
Ein Dict/JSON pro (flight, date) mit ALLEM + Herkunft pro Feldgruppe:

```
identity:  { callsign, flight_no, reg, hex }
route:     { origin{iata,icao,city,lat,lon}, dest{…} }
times:     { sched_dep, est_dep, act_dep, sched_arr, est_arr, act_arr }   # Soll+Ist
status:    { phase: scheduled|boarding|departed|enroute|landed|cancelled,
             gate, terminal, arr_gate, arr_terminal, dep_delay_min, arr_delay_min }
aircraft:  { type, reg, name, age, seats_total, config, jumpseats }
position:  { lat, lon, alt_ft, gs_kt, track, on_ground, seen_ts }         # nur airborne
meta:      { source_per_group{route:…, times:…, position:…},
             confidence_per_group, obs_age_sec, fetched_at }
```

### 3b. Der eine Merge — `_flight_facts_from_obs(flight, date)`  (Phase 0)
Verallgemeinert den Ankunft-Merge dieser Session: liest DEP- **und** ARR-Obs und
liefert Soll/Ist-Zeiten beider Seiten + Gate/Terminal/Delay/Status/Phase. **Genau
eine Funktion**, die Radar, Detail, Dienstplan, MyPlane nutzen — statt drei Patches.
Normalisiert das Zeit-Format (Obs `sched`=„17:35" HH:MM lokal vs `esti`=naive ISO →
einheitlich ISO+station-TZ, killt die schedArrDelayMin-Formatmix-Macke).

### 3c. Der eine Resolver — `resolve_unified_flight(id, date, tier)`  (Phase 1)
Free-First-Kaskade, **per Feldgruppe** den besten Treffer nehmen (nie eine
bestätigte Route/Zeit mit schwächerer überschreiben):
1. `aircraft_live` → route + position + reg
2. `_flight_facts_from_obs` → times(soll+ist) + status + gate + delay
3. `flights` Warehouse → Lücken in route/tail/times
4. Reference-DB → aircraft-Details
5. `fr24_grpc` (gratis) → Blindzonen route/position
6. **`fr24_official` (paid)** NUR wenn Konfidenz < Schwelle ODER Kernfeld fehlt
   → Budget-Gate + Circuit-Breaker → Ergebnis **HART in Cache** (3d).

### 3d. Der eine Ort — `ax_flight_cache` (read-through)  (Phase 2)
Tabelle `(flight, date)` → volles `UnifiedFlight`-JSON + `fetched_at` + `tier`.
- **Read-Through:** `resolve_unified_flight` schreibt sein Ergebnis rein → der
  nächste Read ist ein reiner SELECT („smart zum direkt Lesen").
- **TTL gestaffelt:** live/airborne kurz (~60–90s), Board-Zeiten mittel (~10min),
  reine Plandaten lang (Stunden). Paid-FR24-Felder bekommen die LÄNGSTE TTL →
  **jeder Credit wird genau einmal ausgegeben** (zero double-spend).
- Optional Phase 2b: ein Background-Warmer hält die „heißen" Flüge (Dienstplan des
  Users, FRA/MUC-Board) warm → Reads fast immer Cache-Hit.

## 4. Phasen (jede für sich lieferbar, Build/Tests grün, deploybar)

- **P0 — `_flight_facts_from_obs` (geteilt).** Extrahieren + Zeit-Normalisierung.
  Den Radar-Enrich-One-Off dieser Session darauf umstellen (Verhalten identisch,
  nur zentral). *Risiko niedrig, additiv.*
- **P1 — `UnifiedFlight` + `resolve_unified_flight`.** Free-Kaskade + Per-Feld-Merge
  + Provenance. Hinter Flag `UNIFIED_FLIGHT_SHADOW=1` gegen die Ist-Endpoints
  schatten-vergleichen (48h), bevor irgendwer live drauf liest.
- **P2 — `ax_flight_cache`** (Migration: Tabelle + Index CONCURRENTLY separat).
  Read-Through-Wiring + TTL-Staffel.
- **P3 — FR24-official-Fallback** (Budget `flight:YYYYMMDD`, Circuit-Breaker,
  hart cachen). Owner-Entscheid: Tages-Credit-Cap + Uncertainty-Schwelle.
- **P4 — Consumer auf den Resolver umstellen** (einzeln, je Flag, nie zwei parallel):
  Detail-Aggregat → Radar-Enrich → Dienstplan(`axFlightInfo`) → MyPlane → Suche.
  Nach jedem: verifizieren, dass der Screen dieselbe Wahrheit zeigt.
- **P5 — iOS `UnifiedFlight`-Modell + EIN Read** + Cleanup der Alt-Pfade
  (Position-Poller bleiben direkt, siehe Audit).

## 5. Risiken / offene Owner-Entscheide

- **FR24-Budget:** Tages-Cap? Uncertainty-Schwelle (wann paid)? (heute
  `FR24_DAILY_CREDIT_CAP=8000`).
- **Cache-TTLs:** obige Staffel ok, oder aggressiver/konservativer?
- **Statement-Timeouts:** `flights`/`airport_delay_obs` timen bei nicht-indizierten
  Queries aus → alle neuen Queries auf indizierte Spalten (date/flight/hex) + LIMIT.
- **Zeit-TZ:** Obs-`sched` ist HH:MM lokal ohne Datum → beim Normalisieren Station-TZ
  + Servicedatum ansetzen (sonst Delay-Rechnung falsch).
- **Materialized vs read-through:** Start mit Read-Through (einfacher), Warmer (P2b)
  nur wenn Cache-Hit-Rate zu niedrig.

## 6. Was der Owner am Ende sieht

Jeder Screen (Radar, Detail, Dienstplan, MyPlane, Suche) zeigt für denselben Flug
**dieselben** Soll+Ist-Zeiten, Flugzeug, Gate, Status, Position — aus **einer**
Logik, **einem** Cache. Neue Flüge: sofort aus dem Gratis-Scrape; Lücken: einmal
FR24, dann dauerhaft gecached.
