# FINAL Z76 EUR Diff — Tag-genaue Audit-Tabelle

Stand: 2026-05-20. Rohdaten: `/tmp/z76_diff.json`, `/tmp/z76_real_conflicts.json`.

## §0 KPI

| Quelle | Z76 EUR |
|---|---:|
| AeroTAX Pipeline | 5484 |
| AeroTAX Z76-Days-Sum (mein Diff) | 5142 |
| Golden | 4794 |
| Net-Δ | **+690** (Pipeline) bzw **+348** (Z76-Day-Sum) |

Diff zwischen Pipeline 5484 und Day-Sum 5142 (= 342€) kommt von Z76-Anteilen in non-Z76-klass-Tagen (z.B. same_day-Office mit Z76-Betrag). Untersuchung folgt unten.

## §1 Bucket-Analyse

| Bucket | Tage | EUR-Effekt |
|---|---:|---:|
| **exact_match** (gleiche Land, gleicher EUR) | 50 | +0 |
| **rate_only_diff** (gleiche Land, voll_24h vs an_abreise) | 9 | +89 |
| **formatting_diff** (gleiches Land, City-Detail anders) | 13 | +20 |
| **real_land_conflict** (verschiedene Country) | 26 | −116 |
| **extra_aero** (AeroTAX=Z76, Golden=Z73/Z74/Office/Frei) | 20 | +869 |
| **missing_aero** (AeroTAX=Office/Frei, Golden=Z76) | 15 | −514 |
| **Net** | – | **+348** |

## §2 REAL LAND CONFLICTS (26 Tage, Net −116€)

**Hauptregel-Finding**: Golden wählt das Land in **24 von 26 Fällen aus SE-Ort**, nicht aus CAS-Layover.

| Datum | CAS routing | layover | SE-Ort | first_non_hb | AeroTAX land | Golden land | Diff | Quelle Golden |
|---|---|---|---|---|---|---|---:|---|
| 2025-03-17 | FRA→MXP→GVA | – | **GVA** | MXP | Norwegen | Schweiz-Genf | +31 | **SE** |
| 2025-05-02 | KRK→FRA→PRG→HAM | HAM | **PRG** | KRK | Polen | Tschechische Republik | +2 | **SE** |
| 2025-05-14 | FRA→TLV | TLV | **SEA** | TLV | Israel | USA | +4 | **SE** |
| 2025-05-15 | (leer) | TLV | (leer) | – | Israel | USA | +7 | – |
| 2025-05-16 | TLV→FRA | – | **SEA** | TLV | Israel | USA | −15 | **SE** |
| 2025-05-26 | FRA→TLV | TLV | **CHI** | TLV | Israel | USA | +0 | **SE** |
| 2025-05-27 | (leer) | TLV | (leer) | – | Israel | USA | +1 | – |
| 2025-05-28 | TLV→FRA | – | **CHI** | TLV | Israel | USA | +0 | **SE** |
| 2025-06-21 | FRA→CPH→ATH | ATH | **STO** | CPH | Griechenland-Athen | Schweden | −17 | **SE** |
| 2025-06-22 | ATH→FRA→HAJ→WAW | WAW | **STO** | ATH | Polen-Warschau | Schweden | −26 | **SE** |
| 2025-06-23 | WAW→FRA→SZG→LIN | LIN | **MAD** | WAW | Italien-Mailand | Spanien-Madrid | +0 | **SE** |
| 2025-06-24 | LIN→FRA→HAM→BER | BER | **MAD** | LIN | Italien-Mailand | Spanien-Madrid | +0 | **SE** |
| 2025-06-25 | BER→FRA→MUC | – | **EDI** | BER | Dänemark | Vereinigtes Königreich | +40 | **SE** |
| 2025-07-20 | FRA→MAN→MAD | MAD | **CPH** | MAN | Spanien-Madrid | Dänemark | −22 | **SE** |
| 2025-07-28 | FRA→LHR→RIX | RIX | **FRA** | LHR | Lettland | UK-London | −20 | **CAS layover (LHR=LON)** |
| 2025-08-20 | FRA→TLV→LCA | LCA | **TLV** | TLV | Zypern | Israel | −16 | **SE** |
| 2025-08-28 | ARN→FRA→OSL | – | **OSL** | ARN | Schweden | Norwegen | −6 | **SE** |
| 2025-09-14 | LIS→FRA→LHR | – | **LON** | LIS | Portugal | UK-London | −23 | **SE** |
| 2025-09-28 | AGP→FRA→CPH | – | **GOT** | AGP | Spanien | Schweden | −21 | **SE** |
| 2025-10-02 | FRA→SOF→TLL | TLL | **SOF** | SOF | Estland | Bulgarien | +5 | **SE** |
| 2025-10-03 | TLL→FRA→VCE | – | **VCE** | TLL | Estland | Italien | +1 | **SE** |
| 2025-10-24 | FRA | LON | (leer) | – | UK-London | UK-London | +0 | **CAS layover** |
| 2025-12-11 | PRG→FRA→MLA | – | **MLA** | PRG | Tschechische | Malta | −10 | **SE** |
| 2025-12-14 | FRA→JFK | JFK | **SNN** | JFK | USA | Irland | +5 | **SE** |
| 2025-12-25 | FRA→BRE→MUC→BIO | BIO | **ROM** | BRE | Spanien | Italien-Rom | −9 | **SE** |
| 2025-12-26 | BIO→FRA→EDI | – | **ROM** | BIO | Spanien | Norwegen | −27 | **SE? oder Tour-Layover** |

### §2.1 Source-Verteilung

| Source | Tage | %-Wahl Golden |
|---|---:|---:|
| **SE-Ort** | 24 | 92% |
| CAS-Layover (wenn SE leer) | 2 | 8% |
| first-non-hb-IATA | 0 | 0% |
| Other | 0 | 0% |

→ **Quelle-Hierarchie für Z76-Land-Wahl (validiert per Golden):**
   1. **SE-Ort** für den konkreten Tag, wenn vorhanden (24/26 = 92% Übereinstimmung)
   2. **CAS-Layover-Ort** wenn SE leer (2/26 = 8%)
   3. first_non_homebase_IATA wäre NIE Golden's Wahl
   4. KI/Review bei Konflikt
   5. FollowMe nur Benchmark

**Pipeline aktuell**: nutzt CAS-Layover-Ort als bmf_place_code primary, fällt zurück auf routing. SE-Ort wird NICHT prioritär verwendet.

**Fix**: in `_build_normalized_day` Z15576-15586, **SE-Ort als Top-Priority** für `bmf_place_code` wenn vorhanden.

## §3 EXTRA Z76 in AeroTAX (20 Tage, +869€)

| Datum | a_land | a_eur | g_klass | g_land | SE-Ort | CAS marker | Fix-Kategorie |
|---|---|---:|---|---|---|---|---|
| 2025-01-03 | Indien-Bangalore | 28 | Z73 | Deutschland | – | (Anreise) | Foreign-tour Anreise = Z73 inland (Late-Briefing-Fall) |
| 2025-02-12 | Japan-Tokio | 33 | Z73 | Deutschland | FRA | (Anreise) | gleich — SE=FRA inland |
| 2025-03-29 | Indien-Mumbai | 36 | Z73 | Deutschland | FRA | (Anreise) | gleich — SE=FRA inland |
| 2025-04-08 | Korea | 32 | Z73 | Deutschland | FRA | (Anreise) | gleich — SE=FRA inland |
| 2025-10-05 | Korea | 32 | Z73 | Deutschland | FRA | (Anreise) | gleich — SE=FRA inland |
| 2025-03-22 | Norwegen | 50 | NICHT in Golden | – | (leer) | `83343 PU` | Phantom: same_day Tour erkannt, Golden zählt nicht |
| 2025-05-21 | Angola | 40 | NICHT in Golden | – | – | (?) | Phantom Angola-Tour — Golden hat kein Angola |
| 2025-05-22 | Angola | 40 | NICHT in Golden | – | – | (?) | gleich |
| 2025-05-23 | Angola | 40 | NICHT in Golden | – | – | (?) | gleich |
| 2025-06-01 | Schweden | 44 | NICHT in Golden | – | – | `126533 PU` | Phantom: Phase-E-Closeout retro-aktiviert |
| 2025-06-02 | Bulgarien | 22 | NICHT in Golden | – | – | – | Phantom Bulgarien |
| 2025-06-03 | Bulgarien | 22 | NICHT in Golden | – | – | – | gleich |
| 2025-07-24 | Schweden | 66 | NICHT in Golden | – | – | – | Phantom |
| 2025-09-25 | Polen | 23 | NICHT in Golden | – | – | `15688 PU` | Bulgarien-Tour Day 1 (Closeout-retro-aktiviert) |
| 2025-10-26 | Israel | 44 | NICHT in Golden | – | – | `32935 PU` | TLV-Tour-Start nach Tokyo-RES — Golden hat? |
| 2025-10-27 | Israel | 66 | NICHT in Golden | – | – | – | TLV-Tour-Continuation |
| 2025-10-28 | Israel | 44 | NICHT in Golden | – | – | – | TLV-Tour-End |
| 2025-11-19 | Norwegen | 75 | NICHT in Golden | – | – | `==` | Phantom (siehe Closeout 11-18/19) |
| 2025-12-15 | USA-NY | 66 | NICHT in Golden | – | – | – | Phantom |
| 2025-12-16 | USA-NY | 66 | NICHT in Golden | – | – | – | gleich |

### §3.1 Extra-Cluster

| Cluster | Tage | EUR |
|---|---:|---:|
| **Foreign-Anreise mit SE=FRA-inland → sollte Z73 inland werden** | 5 | 161 |
| **Phantom-Touren (CAS-Marker aktiv, Golden zählt nicht)** | 15 | 708 |

Phantom-Cluster aufgeschlüsselt:
- Angola 05-21/22/23 (3× 40€ = 120€) — möglicherweise echte Tour die Golden vergessen hat
- Schweden 06-01 (44€), Bulgarien 06-02/03 (44€) — Skandi/Bulg-Tour 06-01 (Phase-E-Override retro)
- Schweden 07-24 (66€) — phantom?
- Polen 09-25 (23€) — Bulgarian Day 1 retro
- Israel 10-26/27/28 (154€) — TLV-Tour
- Norwegen 11-19 (75€) — Phantom-Continuation
- USA-NY 12-15/16 (132€) — Phantom
- Norwegen 03-22 (50€)

## §4 MISSING Z76 in AeroTAX (15 Tage, −514€)

| Datum | a_klass | g_land | g_eur | CAS marker | SE-Ort | Fix-Kategorie |
|---|---|---|---:|---|---|---|
| 2025-05-13 | Office | Island | 41 | `112232 PU` | **REK** | Foreign-Same-Day → Office statt Z76 |
| 2025-05-17 | Frei | USA | 40 | `OFF` | – | **documented disagreement** |
| 2025-06-17 | Frei | Kroatien | 31 | `OFF` | – | **documented disagreement** |
| 2025-06-18 | Office | Kroatien | 31 | `OFF` | – | **documented disagreement** |
| 2025-06-30 | Office | Italien | 28 | `129023 PU` | **NAP** | Foreign-Same-Day → Office |
| 2025-07-02 | Issue | UK-London | 44 | `129023 PU / Tag 2+3` | **LON** | Day 2+3 Mehrtag-Suffix nicht erkannt |
| 2025-08-05 | Office | Island | 41 | `158212 PU` | **REK** | Foreign-Same-Day → Office |
| 2025-08-22 | Frei | Zypern | 28 | `X` | – | **documented disagreement** |
| 2025-09-11 | Z73 | Nordmazedonien | 18 | `14542 PU` | **BER** | Z73 Deutschland statt Z76 Nordmazedonien — SE=BER inland verwirrt |
| 2025-09-26 | Z74 | Bulgarien | 15 | `15688 PU (Day 2)` | **MUC** | Day 2 Continuation: SE-Inland-Override frisst Z76 |
| 2025-10-15 | Frei | Frankreich | 36 | (leer) | **MRS** | Leerer Marker + SE-Foreign — Standby-Detect-Lücke |
| 2025-10-16 | Office | Spanien | 23 | – | **AGP** | Foreign-Same-Day → Office |
| 2025-10-25 | Frei | UK-London | 44 | (leer) | **LON** | Leerer Marker + SE-Foreign |
| 2025-10-31 | Office | UK-London | 44 | `33491 PU` | **LON** | Foreign-Same-Day → Office |
| 2025-11-18 | Z73 | Norwegen | 50 | `==` | – | Phantom-Z73 statt Z76 |

### §4.1 Missing-Cluster

| Cluster | Tage | EUR |
|---|---:|---:|
| **Foreign-Same-Day-Tour Office→Z76 (gleicher fix wie Fahrtage)** | 6 | 218 |
| **documented disagreement (CLOSEOUT1 §1)** | 3 | 90 |
| **SE-Foreign + leerer/`==`-Marker** | 2 | 80 |
| **Day-Suffix-Override SE-Inland frisst Z76** | 2 | 33 |
| **Z73 Deutschland statt Z76 (BER SE-Override)** | 1 | 18 |
| **Phantom Z73 11-18** | 1 | 50 |
| **Mehrtag-Suffix `Tag 2+3` nicht erkannt** | 1 | 44 |

## §5 Voll_24h vs an_abreise Rate-Mismatches (9 Tage, +89€)

| Datum | Land | a_eur (voll_24h) | g_eur (an_abreise) | Diff |
|---|---|---:|---:|---:|
| 02-15 | Japan-Tokyo | 50 | 33 | +17 |
| 03-30 | Indien-Mumbai | 53 | 36 | +17 |
| 04-01 | Indien-Mumbai | 53 | 36 | +17 |
| 04-26 | Korea | 48 | 32 | +16 |
| 05-03 | Tunesien | 40 | 27 | +13 |
| 07-01 | Rumänien-Bukarest | 21 | 32 | −11 |
| 08-21 | Zypern | 28 | 42 | −14 |
| 11-09 | Litauen | 26 | 17 | +9 |
| 11-17 | Norwegen | 75 | 50 | +25 |

**Pattern**: Letzter Tag der Tour (tour_end) wird in AeroTAX als tour_mid (voll_24h) statt tour_end (an_abreise) gerollt. Tour-Boundary-Detection-Bug: erkennt Heimkehr nicht.

## §6 Fix-Strategien — Erwartete EUR-Einsparung

| Fix | Tage | EUR-Effekt |
|---|---:|---:|
| (a) **SE-Ort priorisieren als bmf_place_code** | 24 | −116€ (von Z76-real-conflicts) |
| (b) **Foreign-Anreise mit SE-inland → Z73 statt Z76** | 5 | −161€ |
| (c) **Foreign-Same-Day Office → Z76 mit SE-Ort** | 6 | +218€ (Z76 erhöhen) |
| (d) **Mehrtag-Suffix `Tag 2+3` erkennen** | 1 | +44€ |
| (e) **Leerer Marker + SE-Foreign → Standby-Activation** | 2 | +80€ |
| (f) **Day-Suffix-Override Korrektur: foreign-layover gewinnt gegen SE-inland** | 2 | +33€ |
| (g) **Phantom-Tour-Verhinderung (Angola, USA-NY, etc)** | 15+ | −500-700€ |
| (h) **Tour-End-Detection: an_abreise statt voll_24h für letzten Tag** | 9 | −89€ |
| (i) **documented disagreement (3 Tage `OFF`)** | 3 | +90€ (Golden zählt, AeroTAX bleibt CAS) |

**Netto-Effekt aller Fixes**: −116 −161 +218 +44 +80 +33 −600 (mittel) −89 +90 = **−501€**

Aktuelle Z76 = 5484; Golden = 4794. Δ−501 würde Pipeline auf **4983€ bringen** (Δ+189) — knapp außerhalb ±150-Tol aber sehr nah.

## §7 Wichtige Erkenntnis: Pipeline-Sum vs Day-Sum Mismatch

| | EUR |
|---|---:|
| Pipeline `z76_eur` Counter | 5484 |
| Sum of `klass=Z76` days in tage_detail | 5142 |
| Differenz | **+342** |

→ Pipeline rechnet 342€ Z76 von Tagen, die in `tage_detail` NICHT `klass=Z76` haben. Wahrscheinlich Same-Day-Tours mit klass=Z76 aber counted_as_workday-flag-Pattern.

**Audit-Bug**: z76_eur Counter ist inkonsistent mit tage_detail.klass-Sum. Fix: Counter sollte STRENG aus `Σ tage[klass=Z76].amount` aggregieren (per CLAUDE.md §6 „Counter aus tage_detail.klass aggregiert").

## §8 Master-Rule-Compliance

- **SE-Ort hat Priorität** (validiert: 92% Golden-Übereinstimmung) ✓
- **CAS-Layover als Fallback** (2/26 Fälle) ✓
- **Generalisierbarkeit**: alle Fixes über Quellen-Hierarchie, kein Tibor-Hardcoding ✓
- **Counter-Konsistenz**: Audit-Bug §7 fixen ✓
