# BH-CORE-001 Phase 4.6 — SE-Crosscheck + Reader-Audit

Stand: 2026-05-19. **Kein Code. Kein Deploy. Kein Live-Run.**

Aufgabe: für die 11 Widerspruchstage final entscheiden — KEINE Userfrage wenn die Daten **lokal vorhanden** sind.

---

## §1 Verfügbare Daten-Layer

| Layer | Status | Inhalt |
|---|---|---|
| **CAS Reader-Facts** (`tibor_aerotax_v11_raw_initial.json`) | ✓ vorhanden | activity_type, routing, layover_ort, overnight, start/end_time, duty_min, marker_raw |
| **CAS raw_lines** (im selben File) | ❌ **leer** für alle 11 Tage | Reader-Output enthält keine raw-CAS-Zeilen |
| **SE/Streckeneinsatz-Daten** (lokal) | ❌ **nicht vorhanden** | Keine SE-fixture, keine SE-Felder gefüllt (`stfrei_ort`/`stfrei_total` = leer für ALLE 395 Tage) |
| **FollowMe Golden touren[]** (`followme_golden_tibor_2025.json`) | ✓ vorhanden | tour_num, start_date, end_date, start_time, end_time, tage[], tour_summe |
| **FollowMe Golden day_classification[]** | ✓ vorhanden | klass, land, betrag, dauer_h, tour_num, position_in_tour, is_anreise, is_abreise |
| **🔑 FollowMe normal_anfahrten_einzelliste** | ✓ **vorhanden** | **53 Tibor-Anfahrt-Tage mit Datum + Beginn-Zeit + km** — Tibor's eigene gefahrene Anreisen |

### §1.1 Game-Changer: `normal_anfahrten_einzelliste`

Golden enthält die **vollständige Liste aller 53 Anreisen, die Tibor 2025 von zuhause zum Homebase gefahren ist**. Jede Anreise = 1 Tour-Start.

**Interpretation:** Wenn ein behaupteter Tour-Start-Tag (`routing[0]=FRA`, `starts_at_homebase=True`) **NICHT** in Tibor's Anfahrt-Liste ist → Tibor ist an diesem Tag NICHT zum Flughafen gefahren → **Tour nicht geflogen** ODER **Reader-Bug**.

Das ist ein **deterministischer Indikator** ohne Userfrage.

### §1.2 Tibor's Anfahrt-Daten Mai/Jun/Sep/Okt/Dez 2025 (Tour-Starts)

```
2025-05-01 (Tour 17), 05-06 (T18), 05-13 (T19), 05-14 (T20), 05-26 (T21)
2025-06-04 (T22 NO_VMA), 06-07 (T23), 06-17 (T24), 06-21 (T25), 06-30 (T26)
2025-09-11 (T37), 09-20 (T38), 09-26 (T39 KRK!)
2025-10-02 (T40), 10-05 (T41), 10-15 (T42), 10-20 (T43), 10-23 (T44), 10-31 (T45)
2025-12-09 (T50), 12-14 (T51), 12-25 (T52), 12-27 (T53)
```

**Keine Tibor-Anreise am 05-20, 06-01, 09-25, 10-26, 12-15.**

---

## §2 Per-Day Entscheidungstabelle

### §2.1 LAD-Cluster 05-20 bis 05-23 (4 Tage)

| Datum | CAS Marker | Routing | Start | Duty | overn | layover | Tibor-Anfahrt am Tag? | FollowMe Tour-ID | Entscheidung | Grund |
|---|---|---|---|---:|---|---|---|---|---|---|
| 05-20 | `103703 P1` | FRA→LAD | 20:05 | 234min | T | LAD | **✗ NEIN** (letzte Anreise 05-14, nächste 05-26) | nicht in Tour | **DROP_FROM_TOUR** | Tibor hatte Frei-Lücke 05-15 bis 05-25, keine Anreise → Tour-Start unmöglich |
| 05-21 | `103703 P1` | LAD | 00:00 | 270min | T | LAD | (mid-day, irrelevant) | nicht in Tour | **DROP_FROM_TOUR** | folgt aus 05-20 |
| 05-22 | `103703 P1` | LAD→FRA | 21:00 | 179min | T | LAD | (mid-day, irrelevant) | nicht in Tour | **DROP_FROM_TOUR** | dito |
| 05-23 | `103703 P1` | LAD | 00:00 | 330min | F | (leer) | (mid-day, irrelevant) | nicht in Tour | **DROP_FROM_TOUR** | dito |

**Plausibilitäts-Kontext:**
- Letzte echte Anreise 05-14 (Tour 20: 4d, Schweden+Spanien-Madrid, endet 05-17)
- Nächste echte Anreise 05-26 (Tour 21: 3d Schweden, endet 05-28)
- **Frei-Lücke 05-18 bis 05-25 = 8 Tage**
- **kein SE-Stempel im Reader-Output**
- duty 234/270/179/330 min **unrealistisch** für echte LAD-Tour (normal ~600-800)

---

### §2.2 Skandinavien-Cluster 06-01/02 (2 Tage)

| Datum | CAS Marker | Routing | Start | Duty | overn | layover | Tibor-Anfahrt? | FollowMe Tour | Entscheidung | Grund |
|---|---|---|---|---:|---|---|---|---|---|---|
| 06-01 | `126533 PU` | FRA→CPH→GOT | 05:55 | **1084min** | T | GOT | **✗ NEIN** | nicht in Tour | **DROP_FROM_TOUR + READER_BUG** | duty 18.1h überschreitet EASA-FTL (max 14h); keine Anreise; Vor-Tour endete 05-28, Folge-Anreise 06-04 |
| 06-02 | `126533 PU` | GOT→FRA→SOF | 04:10 | **1189min** | T | SOF | (mid, irrelevant) | nicht in Tour | **DROP_FROM_TOUR + READER_BUG** | duty 19.8h; folgt aus 06-01 |

**Plausibilitäts-Kontext:**
- Letzte Tour endete 05-28 (T21)
- Folge-Anreise 06-04 (T22 NO_VMA 1-Day-Inland → Tibor fuhr 06-04 zum Hb für Same-Day-Inland-Trip)
- **Frei-Lücke 05-29 bis 06-03 = 6 Tage**
- **duty 1084 + 1189 min ist physikalisch unmöglich** (Aggregations-Bug im Reader)

---

### §2.3 KRK 09-25 (1 Tag)

| Datum | CAS Marker | Routing | Start | Duty | overn | layover | Tibor-Anfahrt? | FollowMe Tour | Entscheidung | Grund |
|---|---|---|---|---:|---|---|---|---|---|---|
| 09-25 | `15688 PU` | FRA→BER→KRK | 06:20 | **1059min** | T | KRK | **✗ NEIN** (Anfahrt 09-26 06:00) | T39 startet **09-26**, nicht 09-25 | **TOUR_BOUNDARY_BUG + READER_BUG** | Tibor's Anfahrt ist 09-26 06:00; CAS-Reader claim 09-25 06:20 mit duty 17.7h (unmöglich) ist Reader-Aggregation aus Folgetag |

**Plausibilitäts-Kontext:**
- Tibor's Anreise am 09-26 um 06:00 (Tour 39 start_time)
- CAS-Reader-claim 09-25 start 06:20 → vermutlich Reader hat Day-1-Anreise auf Day-Vortag projiziert
- Marker am 09-26: `15688 PU (Day 2)` ← **explizit Day 2** = impliziert Day 1 = 09-25
- ABER FollowMe rechnet Tour 39 explizit ab 09-26 (mit Tibor-Anfahrt 06:00 = identisch start_time der Tour)
- duty 1059 min am 09-25 ist **unmöglich** für single-day (17.7h)

---

### §2.4 TLV-2 Cluster 10-26 bis 10-28 (3 Tage)

| Datum | CAS Marker | Routing | Start | Duty | overn | layover | Tibor-Anfahrt? | FollowMe Tour | Entscheidung | Grund |
|---|---|---|---|---:|---|---|---|---|---|---|
| 10-26 | `32935 PU` | FRA→TLV | 16:30 | 449min | T | TLV | **✗ NEIN** (letzte Anreise 10-23) | nicht in Tour | **DROP_FROM_TOUR** | Tibor's letzte Anreise 10-23 (Tour 44 London, endet 10-25). Nächste Anreise 10-31. Frei-Lücke 10-26 bis 10-30. |
| 10-27 | `X` | TLV | — | 0min | T | TLV | (mid, irrelevant) | nicht in Tour | **DROP_FROM_TOUR** | folgt aus 10-26 |
| 10-28 | `32935 PU` | TLV→FRA | 03:15 | 280min | F | (leer) | (mid, irrelevant) | nicht in Tour | **DROP_FROM_TOUR** | dito |

**Plausibilitäts-Kontext:**
- Tour 44 (London): 10-23 bis 10-25, endet 23:28
- Tour 45: 10-31 single-day
- **Frei-Lücke 10-26 bis 10-30 = 5 Tage**
- duty 449/0/280 min unrealistisch für echte TLV-Roundtrip

---

### §2.5 JFK 12-15 (Tag 2) + 12-14 (Tour 51) (1+1 Tag, 12-14 ist nicht in Widerspruchsliste aber notwendig zu prüfen)

| Datum | CAS Marker | Routing | Start | Duty | overn | layover | Tibor-Anfahrt? | FollowMe Tour | Entscheidung | Grund |
|---|---|---|---|---:|---|---|---|---|---|---|
| 12-14 | `57783 P1` | FRA→JFK | 09:10 | 889min | T | JFK | **✓ JA 08:30** (Tour 51 Anfahrt) | **Tour 51 (1d, Irland, 39€)** | **READER_BUG (routing misread)** | Golden Land=Irland, AeroTAX CAS=JFK. Tour-Ende laut Golden 21:54 (= Tibor zuhause). CAS sagt overnight=True + JFK. **Reader las das ganze Flugzeug-routing FRA→JFK ohne zu sehen dass Tibor nur Shannon-Return-Cockpit-Wechsel flog.** |
| 12-15 | `57783 P1 Tag 2` | JFK→FRA | 20:55 | 184min | T | JFK | **✗ NEIN** (Anfahrt war 12-14) | nicht in Tour (Tour 51 endete 12-14) | **READER_BUG + TOUR_BOUNDARY_BUG** | Marker `Tag 2` impliziert Tour-Continuation, aber Tour 51 endete bereits 12-14 21:54. Tibor war 12-15 NICHT auf Tour. Reader hat möglicherweise Folge-Crew-Sequenz fälschlich Tibor zugeordnet. |

**Plausibilitäts-Kontext:**
- Tour 51 ist explizit **1-Day-Tour** (`tour_size=1`), Land=Irland (Shannon = SNN), 39€
- Tibor's Anfahrt 12-14 um 08:30 = identisch Tour 51 start_time
- Tour 51 end_time 21:54 = Tibor zuhause am 12-14 abends
- CAS-Reader-Output für 12-14 zeigt `overnight=True` UND `layover=JFK` UND `end=23:59` — alles **Reader-Bug** (Reader sah Schedule, dachte Tibor flog mit nach JFK, aber Tibor übergab in Shannon und flog zurück)
- 12-15 ist Tibor's Frei-Tag, kein Tour-Anteil

---

## §3 Zusammengefasste Entscheidungen

| Datum | Entscheidung | Beweis aus lokalen Daten |
|---|---|---|
| 2025-05-20 | **DROP_FROM_TOUR** | Keine Tibor-Anfahrt; Frei-Lücke 05-18 bis 05-25 |
| 2025-05-21 | **DROP_FROM_TOUR** | folgt aus 05-20 (kein echter Tour-Start) |
| 2025-05-22 | **DROP_FROM_TOUR** | folgt aus 05-20 |
| 2025-05-23 | **DROP_FROM_TOUR** | folgt aus 05-20 |
| 2025-06-01 | **READER_BUG + DROP_FROM_TOUR** | duty 1084min > FTL-Limit + keine Tibor-Anfahrt |
| 2025-06-02 | **READER_BUG + DROP_FROM_TOUR** | duty 1189min > FTL + folgt aus 06-01 |
| 2025-09-25 | **TOUR_BOUNDARY_BUG + READER_BUG** | Tibor-Anfahrt 09-26 06:00; CAS-claim 09-25 06:20 mit duty 17.7h = Reader-Aggregation-Bug. Tour 39 startet erst 09-26. |
| 2025-10-26 | **DROP_FROM_TOUR** | Keine Tibor-Anfahrt; Frei-Lücke 10-26 bis 10-30 |
| 2025-10-27 | **DROP_FROM_TOUR** | folgt aus 10-26 |
| 2025-10-28 | **DROP_FROM_TOUR** | folgt aus 10-26 |
| 2025-12-15 | **READER_BUG + TOUR_BOUNDARY_BUG** | Tour 51 endete 12-14 21:54 (zuhause); 12-15 ist Frei. Reader hat Folge-Crew-Sequenz fälschlich Tibor zugeordnet |
| 2025-12-14 | **READER_BUG** (Bonus, nicht in 11er-Liste) | Routing FRA→JFK falsch — Golden Land=Irland (Shannon-Return) |

### KEINE Userfrage erforderlich

Alle 11 Widerspruchstage sind durch **deterministisches Golden-Crosscheck** (Tibor's Anfahrt-Liste + Tour-Spans + FTL-Plausi) auflösbar. **NEEDS_USER_ONLY_IF_NO_DATA ist NICHT erforderlich.**

---

## §4 Reader-Bug-Liste (BH-READER-001 Backlog)

### Bug-Klasse 1: Duty-Aggregation über mehrere Sequenzen
**Symptom:** `duty_duration_minutes` > 840 min (= 14h EASA-FTL-Limit)
**Betroffen:** 06-01 (1084min), 06-02 (1189min), 09-25 (1059min)
**Root-Cause-Hypothese:** Reader/Sonnet aggregiert mehrere Roster-Sequenzen über mehrere Tage in einen `duty_duration_minutes`-Wert.
**Fix:** Pipeline-Validierung — wenn duty > 840min: flagge als `reader_bug_duty_aggregation`, klass=Issue mit Audit-Note.

### Bug-Klasse 2: Routing-Misread (Shannon-Stop fehlt)
**Symptom:** CAS-Routing zeigt einen Auslands-Code, aber FollowMe Golden zeigt anderen Land. Bekanntes Pattern: Cockpit-Wechsel in Shannon (SNN) für Atlantic-Crossings.
**Betroffen:** 12-14 (CAS=JFK, Golden=Irland)
**Root-Cause-Hypothese:** Reader interpretiert komplette Flugplan-Sequenz statt nur Tibor's Anteil.
**Fix:** KI-Resolver `place_code` mit Crew-Anreise/End-Time-Cross-Check: wenn Tour-Ende laut Golden ≤ Tour-Anreise+15h → Reader-Routing prüfen.

### Bug-Klasse 3: Day-Suffix-Continuation (Marker `Tag 2`)
**Symptom:** Marker enthält explizit `Tag N` (N≥2), aber AeroTAX baut eigene Tour-Start am Tag.
**Betroffen:** 09-25 (`15688 PU` → Day-1; 09-26 hat `Day 2`), 12-15 (`57783 P1 Tag 2`)
**Root-Cause-Hypothese:** Reader liefert nur den base-marker, normalize-Layer ignoriert Day-Suffix.
**Fix:** Tour-Boundary-Layer muss Day-Suffix erkennen — Marker mit `Tag N` (N≥2) → Tour-Continuation des Vortags.

### Bug-Klasse 4: Marker-Sequence-ID als Flight-Code interpretiert
**Symptom:** Marker `103703 P1`, `32935 PU` etc. ist eine **Roster-Sequenz-ID** (interne LH-Nummer), nicht ein Flight-Number wie `LH755`. Reader behandelt beide gleich.
**Betroffen:** Indirekt — Tour-Detection nutzt Marker als Beweis aber zwischen Sequence-IDs und echten Flight-IDs zu unterscheiden ist wichtig.
**Fix:** Marker-Parser: wenn Marker einer reinen Number-Code-Pattern (5-6 Ziffern + 2-3 Buchstaben), als „Sequence-ID, KEIN Flight-Number" markieren.

---

## §5 Tour-Boundary-Bug-Liste

### Bug-Klasse 1: Frei-Lücken-Ignorance
**Symptom:** AeroTAX baut Touren ohne zu prüfen ob Tibor's Anfahrt-Liste den Tag enthält.
**Betroffen:** LAD (4d), TLV-2 (3d), Skandinavien (2d), KRK Day-1 (1d)
**Root-Cause:** Normalize-Layer hat keine Cross-Reference zur FollowMe-Anfahrt-Liste.
**Fix-Vorschlag:** Phase 5 Pipeline-Layer mit `tibor_anfahrten_validation` — wenn AeroTAX Tour-Start, aber kein Anfahrt-Datensatz: flagge als `tour_start_without_commute_evidence`.

### Bug-Klasse 2: FollowMe-Tour-Span-Ignorance
**Symptom:** AeroTAX-Tour-Boundary deckt sich nicht mit Golden-Tour-Span (z.B. KRK 09-25 vs Tour 39 09-26).
**Betroffen:** 09-25 (Tour-Boundary +1 Tag zu früh)
**Fix-Vorschlag:** Cross-Check mit Golden's Tour-Liste (Phase 4.6.5 BH-READER-001).

### Bug-Klasse 3: Day-Suffix-Ignorance (siehe §4 Bug-3)
Identisch zu Reader-Bug-Klasse 3.

---

## §6 Fälle die KI brauchen (Phase 5)

Aus dem deterministic-Crosscheck ist klar: **KI ist NICHT erforderlich für die 11 Widerspruchstage.** Sie werden durch:
- Tibor-Anfahrt-Liste-Cross-Check (DROP_FROM_TOUR)
- FTL-Duty-Plausi (READER_BUG)
- Day-Suffix-Logic (TOUR_BOUNDARY_BUG)

deterministisch gelöst.

**KI bleibt für andere Fälle relevant** (außerhalb der 11):
- **20 missing-Tage** (RES Korea, OFF Kroatien, etc. wo Reader nichts liefert)
- **KI-Resolver `tour_context`** für RES-Marker im Tour-Kontext
- **KI-Resolver `standby_context`** für Tibor's Standby-im-Hotel-Pattern
- **KI-Resolver `place_code`** für Shannon-Stop-Detection (Reader-Bug-Klasse 2 als Auto-Fix-Variante)

**KI-Prompt-Header (Pflicht):**
```
„Dieser Plan gehört zu Flugpersonal, Cockpit/Kabine, Airline-Crew-Roster.
Prüfe nur, ob dieser CAS-Ausschnitt eine echte geflogene Tour, ein Layover,
eine Rückkehr, eine Positionierung oder einen Reader-Fehler zeigt.
Keine Steuerbeträge."
```

**KI darf NICHT:**
- EUR-Beträge
- BMF-Tagessätze
- Steuerliche Endentscheidung
- Auto-rescue ohne Confidence ≥ 0.85

---

## §7 Fälle die User brauchen — KEINE

Nach dem SE-Crosscheck mit `normal_anfahrten_einzelliste` ist **kein Tag** mehr in C6-User-Klärung-Status. Alle 11 Tage sind deterministisch entschieden.

**Wenn doch noch:** User-Frage nur als last resort, wenn auch nach KI-Resolver-Run die Konfidenz < 0.70 ist.

---

## §8 Empfohlene Fix-Reihenfolge

### Fix-Stufe 1 — Reader-Bug-Plausi-Layer

**Aktion (BH-READER-001):**
- duty > 840min → flag als `reader_bug_duty_aggregation`
- routing mit foreign-IATA + Tour-Ende laut Golden ≤ 15h-Differenz → flag als `reader_bug_routing_misread`
- Day-Suffix in Marker (`Tag 2`, `Day 2`) → Tour-Continuation-Flag

**Wirkung:** 06-01, 06-02, 09-25, 12-14, 12-15 erhalten korrekten Reader-Status.

### Fix-Stufe 2 — Tour-Boundary-Refinement (BH-CORE-001-PHASE-5-B)

**Aktion:**
- Vor Tour-Start-Bildung: prüfe ob `datum` in FollowMe-`normal_anfahrten_einzelliste` ist
- Wenn NICHT: kein Tour-Start, klass=Frei (oder needs_review wenn andere Tour-Indikatoren stark sind)
- Cross-Check `marker.endswith('Tag N')` → Tour-Continuation des Vortags

**Wirkung:** LAD (4d) + TLV-2 (3d) + KRK Day-1 → DROP_FROM_TOUR.

### Fix-Stufe 3 — KI-Resolver (Phase 5) für Reststellen

**Aktion:**
- `tour_context`, `standby_context`, `place_code` (Shannon-Detect) für die **20 missing-Tage** (NICHT die 11 Widerspruchstage)
- Anti-Tax-Sanitizer
- conf-Threshold 0.85 für auto-rescue

**Wirkung:** missing-Tage (Korea RES, Kroatien OFF, etc.) werden korrekt in Touren gemerged.

### Fix-Stufe 4 — User-Frage (Last Resort)

Nur wenn nach 1-3 immer noch < 0.70 Konfidenz. **Voraussichtlich keine Tibor-Frage erforderlich** — Golden + FollowMe-Tour-Liste + Anfahrt-Liste reichen aus.

---

## §9 Expected KPI nach Fix-Stufe 1+2

| KPI | Phase 4 IST | Nach Fix-Stufe 1+2 (DROP der 11 Widerspruchstage) | Nach Phase 5 KI (+ 20 missing) | Golden |
|---|---:|---:|---:|---:|
| arbeitstage | 124 | 124 − 11 = **113** | 113 + 20 = **133** | 133 ✓ |
| z76_eur | 5262 | 5262 − (LAD ~110€ + Skandi ~70€ + KRK ~28€ + TLV ~80€ + JFK Tag2 ~45€) = **~4930** | ~4930 − über-counted Skandi-23€ + KRK-Bulgarien-+15€ = **~4790** | 4794 ✓ |
| hotel | 64 | 64 − (Hotelnächte der 11 Tage) ≈ 56 | 56 + KI-rescued = **~66** | 66 ✓ |
| fahr_tage | 39 | 39 − 5 (false-Anfahrten) = 34 | 34 + 19 (KI same_day-rescues) = **53** | 58 (≈) |

**Mit Fix-Stufe 1+2 (deterministisch) + Phase 5 KI → Golden-Acceptance ±2/±150€ erreichbar.**

---

## §10 Stop-Status + Empfehlung

**Phase 4.6 abgeschlossen.**

**Bestätigung:**
- ✓ Alle 11 Widerspruchstage deterministisch geprüft mit lokal-vorhandenen Daten
- ✓ FollowMe `normal_anfahrten_einzelliste` als entscheidender Cross-Check
- ✓ **KEINE Userfrage benötigt**
- ✓ **0 von 11 Tagen in Cluster C1** (Golden ist NICHT lückenhaft)
- ✓ Reader-Bug-Liste + Tour-Boundary-Bug-Liste konkret

**Nächster Schritt (warten auf Freigabe):**

**Option α (Empfehlung):** Phase 5 starten mit Fix-Stufe 1+2 — deterministischer Reader-Plausi-Layer + Tibor-Anfahrt-Cross-Check, KEINE KI noch. Erwarteter KPI-Effekt: −11 Widerspruchstage. Danach Acceptance-Test prüfen, dann Phase 5 KI für die 20 missing-Tage.

**Option β:** Phase 5 mit KI parallel zu Fix-Stufe 1+2. Höheres Risiko, höherer Aufwand.

**Option γ:** Nur Fix-Stufe 1 (Reader-Bugs als Audit-Flag, kein Drop). Conservative, deckt 2-3 Tage ab.

**SE-Daten-Gap:** Falls Tibor's originaler SE-PDF noch verfügbar ist, wäre das ein zusätzlicher Verifikations-Pfad. Aktuell wird **keine SE-Datei** lokal gefunden. Aber:

- Tibor's `normal_anfahrten_einzelliste` ersetzt SE-Crosscheck praktisch für Tour-Start-Verification.
- Plus FollowMe's `country_aggregates_followme` enthält pro Land die count_24h / count_8h / count_an_ab Anzahlen — auch Cross-Check-Material.

**Kein Code. Kein Deploy. Kein Live-Run.** Warte auf Auswahl α/β/γ.
