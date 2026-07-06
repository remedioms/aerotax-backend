# AeroX — Session-Report 2026-07-06 (Senior-Review)

**Thema:** Flugdaten-Kosten senken & Coverage-Löcher schließen durch freie Quellen;
verteilter FR24-Harvester; großer Konsolidierungs-Audit.
**Autor:** Claude (Pair-Session mit Owner). **Status:** Backend deployed, iOS unarchiviert (→ 1.6(8)).

---

## 1. Executive Summary

Ziel des Owners: weg von teuren Paid-APIs (AeroDataBox/AviationStack), hin zu **einem
kostenlosen, vollständigen, ban-sicheren Datenstack**, und Ende der Inkonsistenz, dass
verschiedene Screens für denselben Flug **widersprüchliche** Position/Route/Status zeigen.

Diese Session hat den **freien Datenstack gebaut und live geschaltet**, und einen
**Max-Effort-Audit (16 Agenten, Backend+iOS)** gefahren, der einen konkreten
Konsolidierungsplan liefert. Der Plan ist der eigentliche strategische Output und
sollte vor der Umsetzung vom Senior geprüft werden.

**Kernzahl:** Laut Audit lässt sich der synchrone Paid-Spend (AeroDataBox/AviationStack)
im User-Pfad um **>90 %** senken, und das **Single-IP-Ban-Risiko bei 5000 Usern**
eliminieren — aber nur, wenn die im Plan als BLOCKER markierten Punkte zuerst kommen
(v.a. FR24-Selbst-Harvest aus dem User-Pfad entfernen).

---

## 2. Was diese Session gebaut & deployed wurde (verifiziert)

### Backend (Cloud Run, alle gepusht → auto-deploy)
| Commit | Inhalt | Verifikation |
|---|---|---|
| `9ba759d` | Status-Engine Root-Fixes; Family=gratis (allow_paid=False), Freunde=watch-Ping, own=Tier3 | 179 aerox-Tests grün |
| `a03818f` | Gratis-Positions-Mirrors adsb.fi + airplanes.live hinter adsb.lol (ADSBX-v2-kompatibel) | Live-Curl + 6 Tests |
| `c4531a9` | **10 native EU-Board-Adapter, 23 Airports** (Finavia/Fraport-GR/CPH/BRU/LHR/PRG/BUD/ANA-PT/FCO/LGW) | Smoke: alle beide Richtungen ≥1 Row; Prod-DB zeigt LIS/OPO/FAO/LHR-Obs |
| `f18b14e` | **FR24-Grauzonen-Positions-Tier** (Coverage-Loch China/Russland/Ozean) | Live: DLH743/B-LRR über Asien; 8 Tests |
| `2ae1d0c` | FR24 **verteilt**: Harvester → Supabase `fr24_live` → Backend liest warm | E2E: Write→Readback verifiziert; 4 Tests |
| `1c7559e` | FR24-Store als **Gratis-Routenquelle** (spart AeroDataBox-Routen-Spend) | Prod: `DLH433 ORD→FRA`, `BAW42DB DUB→LHR` in fr24_live |

**Datenbank-Fixes (direkt in Prod-DB via Pooler):**
- `airport_delay_obs` PK-Swap `(date,flight,sched)` → `(date,airport,flight,sched)`:
  Cross-Airport-Kollisionen (DY8492/BEL6NQ-Log-Spam, verlorene Delay-Updates) → **0 write_FAILs** seither.
- `fr24_live` angelegt + `origin/dest/flight`-Spalten (Route-Enrichment).

### Verteilter FR24-Harvester (Synology-NAS, deployed & live)
- Läuft als Docker-Container auf der DS225+ (Tailscale `100.121.106.8`), **deutsche
  Residential-IP** = am wenigsten blockbar, 24/7, Auto-Restart.
- **Platten-still** (`logging:none` + `read_only` + `tmpfs` + QUIET) → HDDs hibernieren, NAS leise.
- Ban-Härtung: UA-Rotation, ±30 % Jitter, Block-Backoff, Proxy-Support.
- **15 Kacheln**: 8 Coverage-Löcher + 7 Europa (Route/Tail-Enrichment).
- Warum NAS statt Oracle: Oracle-Signup scheiterte (Tokyo-IP vs DE-Karte = Fraud-Flag).
  NAS ist ohnehin besser (Residential, immer an, kein Signup).

### iOS (`faa6a55`, unarchiviert → 1.6(8))
Device-Batch-2: Glyph-immer bei bestätigt-airborne, Post-Landing-Layover-Flip,
nightstop-only Pack, Strecken-Suche „FRA-HND", Sheet-Dismiss, Dienstplan-Diff DE.

---

## 3. Der Konsolidierungs-Plan (strategischer Kern — Review nötig)

Voller Plan: `docs/reports/2026-07-06_datenquellen_konsolidierungsplan.md`.
Audit: 10 Domänen-Auditoren (Position own/family/friends, Route, Tail, Suche, Status,
Delay, Tabellen-, Externe-Inventar) → Synthese → 4-Linsen-Review → finaler Plan.

**Kernproblem (Audit-Befund):** Dieselbe Frage wird je Screen aus 4–5 konkurrierenden
Quellen beantwortet, teils bezahlt, teils client-direkt. `fr24_live` (die neue
Gold-Tabelle) ist nirgends Primärquelle. **~24 iOS-Dateien** fassen externe APIs
**synchron pro Request** an → bei 5000 Geräten Ban- und Kosten-GAU.

**Lösung (eine Zeile):** EIN geteilter, **tabellen-basierter Resolver pro Datentyp**
(neues `warehouse_reader.py`), der nach **echtem Beobachtungs-Zeitstempel** entscheidet
(nicht nach Quellen-Rang), konsumiert von allen Screens über Backend-Endpunkte.

**BLOCKER vor Ausrollen (Plan-Schritt 0):**
1. FR24-Selbst-Harvest aus dem User-Pfad KILLEN (`_fetch_fr24` Selbst-Refresh) +
   Kill-Switch — sonst kippt Harvester-Ausfall auf Selbst-Harvest der Cloud-Run-IP.
2. `fr24_live` Geo-Index (vor `/api/adsb/area`-Umstellung, sonst Seq-Scan pro Pan).
3. EU-Positions-Dichte NICHT über FR24-Tiles (clippen bei 1500) → dedizierter
   Area-Poller in spatiale Tabelle; FR24-EU bleibt Route/Tail-Enrichment.
4. FR24-Frische ehrlich: `pos_ts` + `estimated`-Flag als eigene Spalten spiegeln.

**Konkrete Widersprüche, die der Owner in den Screenshots meldete, sind bestätigt:**
- „Basti in Oslo statt Live-Flieger" = Layover-Location-Fallback tarnt sich als
  aktuelle Position (Task #21). Der Resolver-Plan adressiert genau das.
- „Radar live, MyPlaneCard kein Signal" = zwei Backends für denselben eigenen Flug.

**Roadmap:** 14 Schritte (0 Blocker → Position → Paid/Route/Status → iOS-Massen-Umbau
→ CI-Guard gegen synchrone Extern-Calls). Aufwand S/M/L je Schritt im Plan.

---

## 4. Offene Punkte

### Owner-Aktionen (extern, kann ich nicht)
- **#17** Gratis-Keys: Swedavia (ARN, 10 SE-Airports), Schiphol (AMS). LH Open API:
  Registrierung eingefroren (nur IATA-Portal-Weg).
- **#18** Raspberry-Pi-Feeder (optional, Residential-Backbone).

### Bugs aus Owner-Screenshots (neu erfasst)
- **#21** Crew-Karte zeigt Layover-Ort statt Live-Flieger (Kern-Konsolidierung).
- **#22** Family: eigenes Profilbild fehlt, Tour nicht klickbar, Position unzuverlässig.
- **#23** Briefing-Hero „Freier Morgen" bei ganzem freien Tag.

### Strategisch
- **#15** Status-Engine-Review (deckt sich mit Resolver-Schritt 7 des Plans).
- **#19** Bot-walled Boards (ADP/Mailand/STN) via eu_scraper.
- Umsetzung Konsolidierungsplan (14 Schritte).

---

## 5. Risiken & ehrliche Vorbehalte

- **FR24 ist ToS-Grauzone** (non-commercial). Mitigation: nur Harvester-Kontakt,
  Residential-IP, niedrige Rate. Kein Login → kein Account-Ban, nur IP-Block.
- **Coverage-Loch bleibt teilweise**: kein freier Satelliten-ADS-B verifiziert;
  Langstrecke stützt sich auf FR24-Store + (letzter Tier) AeroDataBox.
- **iOS-Umbau ist groß** (~24 Dateien) — der größte Ban-Hebel, aber auch das größte
  Regressions-Risiko. Plan empfiehlt CI-Guard, der synchrone Extern-Calls verbietet.
- **Der Plan ist noch nicht umgesetzt** — diese Session hat den Stack + die Analyse
  geliefert, nicht die Konsolidierung selbst. Das ist bewusst: erst Senior-Review.
- Harvester-Betrieb hat eine Falle gezeigt (leere Kachel = Fehlalarm-Block, gefixt in
  `933ebaf`; `docker compose up --build` ersetzt laufenden Container nicht → `--force-recreate` nötig). Beides dokumentiert.

---

## 6. Verifikations-Nachweise (Auswahl)
- `fr24_live` füllt sich live über alle Kacheln inkl. Europa, mit Route (`origin/dest`).
- EU-Boards schreiben in Prod: LIS 340, OPO 175, FAO 136, LHR-Obs.
- Backend-Suite: 125 passed (route/fr24/family/position); adsb/position 56 passed.
- FR24-Positionen live über Coverage-Loch: DLH743 (Kasachstan), B-LRR/CPA539 (Taiwan).
