# FINAL Fahrtage Diff — Tag-genaue Audit-Tabelle

Stand: 2026-05-20. Rohdaten: `/tmp/fahrtage_diff.json`.

## §0 KPI

| Quelle | Fahrtage |
|---|---:|
| AeroTAX Pipeline | 42 |
| Golden `is_anreise=True` | 53 |
| Golden `soll_summary.fahrten.total` (53 normal + 5 zusatz) | **58** |
| Net-Δ | **−16** |

22 Tage in Golden = Fahrtag, aber AeroTAX zählt nicht.
11 Tage in AeroTAX = Fahrtag, aber Golden zählt nicht.
22 − 11 = +11 → net Differenz +11 zugunsten Golden, plus 5 Golden-„zusatz"-Anreisen, plus die 2026-Januar-extras: rechnet auf 16 net.

## §1 FEHLENDE Fahrtage in AeroTAX (22 Tage)

| Datum | Golden Klass | Golden Land | g_pos | AeroTAX Klass | AeroTAX Role | CAS marker | CAS at | layover | SE-Ort | Fix-Kategorie |
|---|---|---|---|---|---|---|---|---|---|---|
| 2025-01-10 | NO_VMA | (-) | 1/1 | Office | non_tour | `EM` | training | – | – | EM-Training mit Anfahrt — Office-Tag zählt nicht counted_fahrtag |
| 2025-02-10 | Z72 | Deutschland | 1/1 | Office | non_tour | `68617 PU` | same_day | – | DUS | Inland-Same-Day-Z72 — Office statt Z72, kein fahrtag |
| 2025-03-17 | Z76 | Schweiz-Genf | 1/1 | Z76 | tour_mid | `83003 PU` | same_day | – | GVA | Z76-Same-Day foreign — fahrtag fehlt da role=tour_mid statt same_day |
| 2025-03-18 | Z72 | Deutschland | 1/1 | Z72 | non_tour | `EH 4 SECCRM 4` | training | – | – | Z72-Inland-Training — non_tour Z72 hat kein counted_fahrtag |
| 2025-03-19 | Z72 | Deutschland | 1/1 | Z72 | non_tour | `EMCRM 4` | training | – | – | gleich wie 03-18 |
| 2025-03-23 | Z76 | USA-Boston | 1/3 | Z76 | tour_start | `73724 P1` | tour | IAD | BOS | Pipeline counted Z76 tour_start aber tour_fahrtag_counted false |
| 2025-03-28 | NO_VMA | (-) | 1/1 | Office | non_tour | `EM /1` | training | – | – | EM-Training |
| 2025-05-13 | Z76 | Island | 1/1 | Office | non_tour | `112232 PU` | same_day | – | REK | Foreign-Same-Day — pipeline Office (kein Z76) |
| 2025-06-04 | NO_VMA | (-) | 1/1 | Office | non_tour | `EM` | training | – | – | EM-Training |
| 2025-06-17 | Z76 | Kroatien | 1/2 | Frei | non_tour | `OFF` | frei | – | – | **documented disagreement** (CLOSEOUT1) |
| 2025-06-30 | Z76 | Italien | 1/3 | Office | non_tour | `129023 PU` | same_day | – | NAP | Foreign-Same-Day — Office statt Z76 |
| 2025-07-23 | Z76 | Schweden | 1/1 | Z76 | tour_start | `==` | frei | – | – | Pipeline=tour_start aber tour_fahrtag_counted bug |
| 2025-08-05 | Z76 | Island | 1/1 | Office | non_tour | `158212 PU` | same_day | – | REK | Foreign-Same-Day |
| 2025-08-06 | Z72 | Deutschland | 1/1 | Z72 | non_tour | `TK` | training | – | – | Z72-Training-Inland |
| 2025-08-29 | Z76 | Ägypten | 1/1 | Z76 | same_day | `232 PU` | same_day | – | CAI | Z76-Same-Day — fahrtag-Bug (counted_fahrtag false) |
| 2025-09-20 | Z72 | Deutschland | 1/1 | Frei | non_tour | `==` | frei | – | – | **documented disagreement** (CLOSEOUT1) |
| 2025-09-26 | Z76 | Bulgarien | 1/3 | Z74 | tour_mid | `15688 PU (Day 2)` | tour | IST | MUC | Day-2-Continuation: pipeline tour_mid SE-Inland-Override → Z74, kein fahrtag |
| 2025-10-15 | Z76 | Frankreich | 1/2 | Frei | non_tour | (leer) | unknown | – | MRS | SE-Foreign aber Marker leer — Standby-Fix triggert nicht |
| 2025-10-31 | Z76 | LON | 1/1 | Office | non_tour | `33491 PU` | same_day | – | LON | Foreign-Same-Day → Office statt Z76 |
| 2025-11-17 | Z76 | Norwegen | 1/2 | Z76 | tour_mid | `SB_M` | standby | – | SVG | Standby-Activation = tour_mid, kein fahrtag (sollte erstes Tag = tour_start sein) |
| 2025-11-20 | Z76 | USA | 1/3 | Z76 | tour_start | `38652 P1` | tour | MIA | MIA | tour_start aber tour_fahrtag_counted bereits true (phantom-tour 11-18) |
| 2025-12-27 | Z76 | Israel | 1/3 | Z76 | tour_start | `70531 PU` | tour | TLV | TLV | gleich wie 11-20 |

### §1.1 Cluster-Verteilung

| Cluster | Tage | Konkret |
|---|---:|---|
| **Foreign-Same-Day-Tour als Office klassifiziert** | 5 | 05-13 REK, 06-30 NAP, 08-05 REK, 08-29 CAI, 10-31 LON |
| **Z72/Z76 Same-Day Z76 ohne counted_fahrtag** | 3 | 03-17 GVA (tour_mid), 08-29 CAI (same_day), 02-10 DUS |
| **Z72-Training/EM-Office: counted_fahrtag fehlt** | 6 | 01-10 EM, 03-18 EH, 03-19 EMCRM, 03-28 EM/1, 06-04 EM, 08-06 TK |
| **tour_fahrtag_counted bug (Phantom-tour absorbiert echten Start)** | 3 | 03-23 BOS, 07-23 Schweden, 11-20 USA, 12-27 TLV |
| **SE-Foreign + leerer Marker (Standby-Detect-Lücke)** | 1 | 10-15 MRS |
| **Day-Suffix mit SE-Inland-Override frisst fahrtag** | 1 | 09-26 Bulgarien (Day 2) |
| **Standby-Activation tour_mid statt tour_start (Day 1)** | 1 | 11-17 SVG |
| **documented disagreement (CLOSEOUT1 §1)** | 2 | 06-17, 09-20 |

## §2 EXTRA Fahrtage in AeroTAX (11 Tage)

| Datum | AeroTAX klass | AeroTAX role | bmf_land | Golden klass | CAS marker | Begründung |
|---|---|---|---|---|---|---|
| 2025-03-22 | Z76 | same_day | Norwegen | NICHT in Golden | `83343 PU` | Phantom-Tour: CAS-Marker für Same-Day-Tour, Golden zählt nicht |
| 2025-05-20 | Z73 | tour_start | – | NICHT in Golden | `103703 P1` | Phantom-Tour-Start |
| 2025-06-01 | Z76 | tour_start | Schweden | NICHT in Golden | `126533 PU` | Skandi-Tour 06-01 — Phase-E-Override-Fix vom Closeout aktiviert |
| 2025-07-01 | Z76 | tour_start | Rumänien-Bukarest | Z76 (gleiche) | `129023 PU / Tag 1` | tour_start ist gleich, aber Golden zählt anders |
| 2025-09-25 | Z76 | tour_start | Polen | NICHT in Golden | `15688 PU` | Day-Suffix-Fix retroaktiv aktiviert (Bulgarien-Tour Day 1) |
| 2025-10-26 | Z76 | tour_start | Israel | NICHT in Golden | `32935 PU` | TLV-Tour-Start nach Tokyo-RES |
| 2025-11-18 | Z73 | tour_start | – | Z76 (Norwegen) | `==` | **Phantom-Anreise** — Pipeline halluziniert Tour-Start aus `==` Marker |
| 2026-01-08 | Z76 | tour_start | China-Shanghai | NICHT in Golden | `74409 P1/ZH` | 2026 außerhalb Golden-Scope (Golden = 2025 nur) |
| 2026-01-11 | Z76 | same_day | China-Shanghai | NICHT in Golden | `74409 P1/ZH` | gleich, 2026 |
| 2026-01-22 | Z76 | tour_start | Israel | NICHT in Golden | `86417 PU` | gleich, 2026 |
| 2026-01-28 | Z76 | tour_start | Irland | NICHT in Golden | `87023 PU` | gleich, 2026 |

### §2.1 Extra-Cluster

| Cluster | Tage | Konkret |
|---|---:|---|
| **2026-Januar (außerhalb Golden-Scope)** | 4 | 01-08, 01-11, 01-22, 01-28 |
| **Phase-E-Closeout retroaktive Aktivierung (Skandi 06-01, Bulgarien 09-25)** | 2 | 06-01, 09-25 |
| **Phantom-Tour-Start aus leerem `==` Marker** | 1 | 11-18 |
| **Same-Day-Tour erkannt (CAS-Marker) aber Golden nicht** | 1 | 03-22 |
| **Tour-Start AeroTAX vs Golden alternative-Start** | 3 | 05-20, 07-01, 10-26 |

## §3 Cluster-Summary

| Bucket | Tage | Effekt auf Δ fahrtage |
|---|---:|---:|
| Echte Counter-Bugs (Z72-Training kein fahrtag, Foreign-Same-Day als Office) | 14 | −14 (zu wenig) |
| Standby/Day-Suffix-Sekundäreffekte | 2 | −2 |
| tour_fahrtag_counted-Phantom-Bug | 3 | −3 |
| documented FollowMe-vs-CAS-disagreement | 2 | −2 (CAS-conform, kein Bug) |
| Sub-Total MISSING | 21 | −21 |
| **EXTRA AeroTAX (2026-outside-Golden)** | 4 | +4 (kein Bug, scope-Differenz) |
| **EXTRA AeroTAX (Phase-E retro)** | 2 | +2 (kann CAS-conform sein) |
| **EXTRA AeroTAX (phantom)** | 1 | +1 (echter Bug: 11-18 `==`) |
| **EXTRA AeroTAX (other)** | 4 | +4 |
| Net AeroTAX vs Golden | – | **−21 + 11 = −10**, plus 5 Golden-zusatz-anreisen = **−15** Real-Gap |

## §4 Diagnose

**Hauptursachen** (sortiert nach Anzahl):

1. **Counter-Bug: `counted_as_fahrtag` fehlt bei non_tour mit echter Dienstreise (6 Tage)**. EM/EH/TK-Training mit `training`-activity_type und SE-stempel oder routing=[HB]+briefing → sollte fahrtag werden.

2. **Counter-Bug: Foreign-Same-Day als Office statt Z76 (5 Tage)**. CAS-Marker `PU<id>` mit cas_at=same_day + SE-foreign-Ort, aber routing=[HB] only (Reader-Lücke) → Pipeline klassifiziert als Office non_tour, nicht Z76. SE-Ort sollte als Tour-Destination greifen.

3. **Counter-Bug: `tour_fahrtag_counted` Phantom-Tour absorbiert echten Tour-Start (3 Tage)**. 11-18 Phantom Z73 nutzt tour_fahrtag_counted → 11-20 echter Miami-Start verliert seinen Fahrtag. Gleich 07-23, 12-27.

4. **Standby-Activation Day 1 als tour_mid (1 Tag)**. 11-17 SB_M = erstes foreign-Activation-Tag, sollte tour_start sein.

5. **Day-Suffix mit SE-Inland-Override frisst fahrtag (1 Tag)**. 09-26 Day 2 Bulgarien wird Z74 inland (mein Override) statt Z76 foreign mit fahrtag.

6. **SE-Foreign + leerer Marker erkennt Standby-Activation nicht (1 Tag)**. 10-15 marker=leer, SE=MRS. Standby-Activation-Trigger required `marker in {RES, SB, ...}` — leerer Marker fällt durch.

7. **documented disagreement (2 Tage)**. 06-17 OFF, 09-20 ==.

## §5 Empfohlene Fix-Strategie (minimal)

| Fix | Erwarteter +Fahrtag-Effekt | Risiko |
|---|---:|:---:|
| (a) Foreign-Same-Day Office → Z76 wenn SE-Foreign-Stempel da | +5 | mittel |
| (b) Z72-Training/EM mit start_time → counted_fahrtag=True | +6 | gering |
| (c) Standby-Activation Day 1 → tour_start statt tour_mid | +1 | gering |
| (d) Day-Suffix-Override: prev_layover_foreign überstimmt SE-inland fuer Z76 | +1 | mittel |
| (e) Leerer Marker + SE-Foreign-Ort → behandle wie Standby-Activation | +1 | mittel |
| (f) Phantom-Tour-Detection 11-18/19 verhindern (anti-phantom) | +3 (counter-bug freigegeben) | mittel |
| **Gesamt (a..f)** | **+17 fahrtage** | – |

Nach allen Fixes: 42 + 17 = **59 fahrtage** (Golden 58 ±2 → **✓ in Toleranz**).

Risiko-Bewertung: Fixes (b), (c) sind klein. (a), (d), (e), (f) erfordern sorgfältige Tests gegen Regressionen.

## §6 Master-Rule-Compliance

- **Fahrtage NICHT aus Z76-Betrag geraten** → Pipeline berechnet fahrtag aus CAS-Tour-Start + Same-Day-counter, nicht aus EUR ✓
- **Quelle CAS/Tourstart/Tourende/Homebase-Commute** → ja, alle Fixes basieren auf CAS-Roles + SE-Stempel
- **Generalisierbarkeit** → ja, keine Tibor-Hardcoding (alle Fixes über SE-Stempel + Marker-Regel)
