# BH-003 Tibor-Diff Forensics

Phase B. Stand 2026-05-19.

Datenquellen:
- **IST**: Phase-A-Forensik (Tibor Live-Run e132976f vom 2026-05-15), kommagetrennte `_klass_summary` + `_tage_detail` Aggregate
- **SOLL**: `tests/fixtures/followme_golden_tibor_2025.json` (FollowMe.aero Manual-Klassifikation, 133 Tage)
- **Code**: app.py post-Phase-1-7-Sync (Commit `c0078aa`)

> ⚠️ Diagnose-Doc. **Keine Codeänderung** bis User Fixplan freigibt.

---

## §1 Aggregat (vorher gesichert)

| Wert | IST | SOLL | Δ |
|---|---:|---:|---:|
| arbeitstage | **140** | 133 | **+7** |
| reinigungstage | 140 | 133 | +7 (mirrors arbeitstage) |
| fahrtage | 55 | 58 | −3 |
| hotelnächte | **78** | 66 | **+12** |
| z72 | 5 | 5 | 0 ✓ |
| z73 | 8 | 11 | −3 |
| z74 | 0 | 1 | −1 |
| z76_eur | 4437 | 4794 | −357 |
| z77_total | 4705 | 4705 | 0 ✓ |
| **gesamt** | **5621** | **6020.72** | **−399.72** |

---

## §2 BUCKET A — Arbeitstage +7 (Issue-Tage Cross-Reference)

Erwartung war: 8 Issue-Tage = Tour-Heimkehr-Bug. **Realität:**

| Datum | Marker | Routing | IST klass | Golden klass | Golden Land | Tour-Pos | Diagnose |
|---|---|---|---|---|---|---|---|
| **2025-01-04** | X | BLR | Issue | **Z76** | Indien - Bangalore (42€) | Mitte 2/4 | Echter Tour-Mitte-Tag — Issue-Fehl |
| **2025-01-06** | X | FRA | Issue | **Z76** | Indien - Bangalore (28€) | **Ab** 4/4 | Echte Heimkehr — Issue-Fehl |
| 2025-03-26 | == | (leer) | Issue | **NICHT in Golden** | — Frei | — | Frei-Marker `==` falsch als Issue |
| 2025-04-02 | == | FRA | Issue | **NICHT in Golden** | — Frei | — | Frei-Marker `==` falsch als Issue |
| 2025-05-23 | 103703 | LAD | Issue | **NICHT in Golden** | — Frei | — | Heimkehr-Tag KEIN Arbeitstag laut Golden |
| 2025-06-03 | 126533 | SOF | Issue | **NICHT in Golden** | — Frei | — | dito |
| 2025-10-28 | 32935 | TLV | Issue | **NICHT in Golden** | — Frei | — | dito |
| 2025-12-16 | X | JFK | Issue | **NICHT in Golden** | — Frei | — | dito |

### Befund

**8 Issue-Tage zerfallen in 2 Klassen:**
- **2 Tage** (01-04, 01-06): Echte Tour-Tage (Bangalore-Tour). Sollten Z76 sein, sind Issue. → BH-003a
- **6 Tage**: In Golden NICHT als Arbeitstag — diese Tage sind Frei/Urlaub. Reader/Klassifikator stuft sie aber als Issue ein. → BH-003b

### Hypothese BH-003a (2 Tage)

Bangalore-Tour 01-03 bis 01-06 (4 Tage):
- Golden: Z73 Deutschland An (01-03), Z76 Indien 24h (01-04, 01-05), Z76 Indien Ab (01-06)
- IST: 01-03 vermutlich Z76/Z73, 01-04 `Issue` (statt Z76 Mitte), 01-05 vermutlich Z76, 01-06 `Issue` (statt Z76 Ab)

Root-Cause: Klassifikator-Tour-Cluster-Logik bricht den Cluster fälschlich auf, behandelt 01-04 als isolierten Tag mit reader_facts.routing=`['BLR']` (kein FRA). 01-06 hat routing FRA aber wird trotzdem als Issue klassifiziert weil „Heimkehr aus Vortag-Tour, separater Cluster".

### Hypothese BH-003b (6 Tage)

Marker `==` und `X` werden vom Klassifikator als „Tour-Continuation" interpretiert wenn Vortag overnight=True war. Aber laut Golden sind diese Tage **Frei** (Crew hatte Urlaub nach Tour). → Reader/Klassifikator sollte erkennen:
- `==` = Frei-Marker
- `X` = OFF/Frei-Marker
- Trotz Vortag-overnight → Tag bleibt Frei

Daraus folgt: 6 Tage werden fälschlich als Arbeitstag/Issue gezählt. Korrektur: **−6 arbeitstage** (= +6 Frei).

Erwarteter Gewinn: arbeitstage 140 − 6 = 134 (Golden=133). Δ=+1. Plus BH-003a (2 Tage in Z76 statt Issue, kein Δ in arbeitstage) → 134. Restdiff +1 könnte 2025-08-01 (HAM Same-Day) sein, der als ZeroDay-counted_as_workday gezählt wird.

---

## §3 BUCKET B — Reinigung +7

`reinigungstage = arbeitstage` (App-Logik). Wenn arbeitstage 140→133 korrigiert wird, reinigung folgt automatisch.

| Wert | IST | SOLL | Δ | Spec-Check |
|---|---:|---:|---:|---|
| reinigung | 140 | 133 | +7 | Spec: `reinigung == arbeitstage` ✓ |
| satz | 1.6€ | 1.6€ | 0 | OK |
| gesamt | ~224€ | 212.80€ | +~11€ | folgt aus arbeitstage |

**Kein eigener Bug.** Wird durch BH-003a/b automatisch behoben.

---

## §4 BUCKET C — Hotel +12

| Wert | IST | SOLL | Δ |
|---|---:|---:|---:|
| hotelnächte | 78 | 66 | **+12** |

### Bekannte Hotel-Mehrzahl-Quellen (Phase-A-Daten)

1. **8 Issue-Tage zählen ggf. als Hotel** wenn overnight=True. Wenn diese zu Frei/Z76 An/Ab korrigiert werden:
   - 2 echte Heimkehr (01-04, 01-06): An/Ab-Tage zählen NICHT als Hotel → −2
   - 6 Frei-Tage: zählen ohnehin nicht als Hotel
   
2. **Inland-Layover-Tage fälschlich als Z76 mit Hotel** (siehe §5):
   - 2025-06-23/24, 09-26/27, 11-01 — alle mit Hotel-Zählung obwohl Layover-Ort fragwürdig

3. **Möglich: Phase-4 layover_place_inferred** für Tage mit routing[-1]=Inland-Ziel könnte Hotel-Counter unnötig erhöhen

### Hypothese

| Source | erwartete Reduktion |
|---|---:|
| BH-003a 2 Heimkehr-Tage (An/Ab) zählen nicht | −2 |
| BH-003b 6 Frei-Tage waren nie Hotel | (kein Effekt, sind nicht Hotel) |
| BH-004 Inland-Z76-Override (4–5 Tage mit overnight=True) | −4 bis −5 |
| Phase-4 layover_place zu aggressiv (Diagnose nötig) | −3 bis −5 |

Sum: −9 bis −12 → Hotel 78 → 66–69 ✓ Golden 66.

**Cluster mit BH-003 + BH-004 + BH-NEW-PHASE4-AUDIT.**

---

## §5 BUCKET D — z73 −3 / z74 −1 / klass_diffs

### §5.1 _missing_z73_candidates (bekannt, 2 Tage)

| Datum | IST klass | IST Layover | Golden klass | Golden Land | Golden Ort | Δ |
|---|---|---|---|---|---|---|
| 2025-09-26 | Z76 | IST (Türkei) | **Z76** | Bulgarien | An | Land falsch (Türkei statt Bulgarien) |
| 2025-09-27 | Z76 | AGP (Spanien) | **Z74** | Deutschland | Volltag | **Klasse falsch + Land Inland** |

**Erkenntnis 09-27:** Golden klassifiziert das als **Z74 Deutschland 28€** (Inland-Volltag). Tibor IST klassifiziert es als Z76 AGP (Auslands-An/Ab). Cluster-C2 hat fälschlich Auslands-Override gemacht.

**Erkenntnis 09-26:** Golden = Z76 **Bulgarien** (15€). Tibor IST = Z76 **Türkei** (24€). Verschiedenes Tour-Ziel. CAS-Layover IST = Istanbul (Türkei) — aber das ist nicht das Tour-Ziel sondern ein Transfer-Stop.

### §5.2 Inland-Tour-Tage in Z76 fälschlich mit Auslands-Land

| Datum | IST | Golden | Effekt |
|---|---|---|---|
| 2025-06-23 | Z76 Layover LIN (?Italien) | Z76 Spanien-Madrid 42€ pos 3/5 | Tour-Land falsch erkannt |
| 2025-06-24 | Z76 Layover BER (Berlin Inland!) | Z76 Spanien-Madrid 42€ pos 4/5 | Layover-Ort Inland statt Tour-Land |
| 2025-11-01 | Z76 Layover LEJ (Leipzig Inland!) | Z76 Schweden 44€ pos 1/3 | wie oben |

**Root-Cause-Hypothese:** Klassifikator nutzt `layover_ort` (= aktueller Übernachtungs-Ort) statt **Tour-Land** (= Zielland der Tour). Bei Multi-Stop-Touren (z.B. WAW→LIN→BER→FRA) zählt der Klassifikator jeden Layover als eigene Land-Tagessatz. Golden aggregiert pro Tour zum Hauptziel (Madrid bei dieser Tour).

→ Das ist **#222 normalized_tours-Schicht**: CAS+SE → Touren mit Ziel-Land-Erkennung statt Per-Day-Layover.

### §5.3 z73 −3 Bilanz

Erwarteter Effekt nach Fixes:
- BH-003a 01-06 (Indien Z76 Ab statt Issue): kein z73-Effekt
- BH-004 09-27 (Z74 Inland statt Z76 AGP): +1 Z74, −1 Z76
- Tour-Land-Fix (06-23/24, 11-01): keine z73-Effekte direkt, aber z76-€-Diff (siehe §6)

Restliche 2 fehlende Z73 → unklar ohne aktuellen Live-Snapshot. Werden mit BH-004-Fix sichtbar.

---

## §6 BUCKET E — z76 −357€ Tagessatz-Diff

Golden-Aggregate: 4794€ (Soll-Summary). Die Land-Aggregate (count_24h × satz + count_8h × satz + count_an_ab × satz) summieren zu 5046€ — **Diskrepanz 252€** zwischen `country_aggregates_followme` und `z76.gesamt` deutet auf Storno-/Korrektur-Logik in der Golden-Aggregation (Z76 mit Storno-Tagen abgezogen?).

### §6.1 Bekannte Tour-Land-Mismatches (Phase A + §5)

| Datum | IST Land/€ | Golden Land/€ | Diff |
|---|---|---|---|
| 2025-06-23 | LIN-Italien (?) | Spanien-Madrid 42€ | unklar |
| 2025-06-24 | BER-Inland → Z73? | Spanien-Madrid 42€ | falsch klassifiziert |
| 2025-09-26 | Türkei 24€ | Bulgarien 15€ | +9€ zu viel |
| 2025-09-27 | Spanien 23€ | Deutschland Z74 28€ | falsche Klasse |
| 2025-11-01 | Schweden? oder LEJ? | Schweden 44€ | unklar (vermutlich korrekt) |

### §6.2 Bilanz-Hypothese

| Quelle | Δ z76 € |
|---|---:|
| BH-003a Issue→Z76 (01-04 +42, 01-06 +28) | +70 |
| BH-004 09-26 (Türkei→Bulgarien) | −9 |
| BH-004 09-27 (Z76 AGP →Z74 Deutschland, NICHT z76) | −23 |
| Tour-Land 06-23/24, 11-01 + andere | ±N (TBD) |
| BMF-Tagesätze (F5/#228) | Restdiff |

Σ erwartete Korrektur: ungefähr +38€ bis −300€, je nach Tour-Land-Fix-Coverage. Restdiff < 100€ wäre Erfolg für BH-003/BH-004 alleine, der Rest geht in **BH-007** F5.

---

## §7 Bug-Cluster-Plan

### Cluster A (BH-003a + BH-003b) — Tour-Heimkehr/Frei-Differenzierung
**Gemeinsam fixbar:** Klassifikator-Logik für Heimkehr/Frei-Marker.

- **BH-003a Issue → Z76 An/Ab im Tour-Cluster** (2 Tage echte Heimkehr)
  - Files: `_build_tour_clusters`, `_deterministic_classify_v7`
  - Risiko: hoch (Tour-Cluster ist zentral)
  - KPI: +2 Z76, +56-70€ z76, −2 Issue
  - Tests: `test_tibor_bangalore_tour_01_04_z76_mitte`, `test_tibor_bangalore_01_06_z76_abreise`

- **BH-003b Frei-Marker (`==`, `X`, Number-Codes) → Frei nicht Issue** (6 Tage)
  - Files: Marker-Lexikon, `_build_tour_clusters`, Issue-Branch
  - Risiko: mittel (könnte echte Issue-Tage falsch als Frei deklarieren)
  - KPI: −6 arbeitstage, −6 reinigung
  - Tests: `test_frei_marker_double_equals_not_issue`, `test_frei_marker_after_overnight_not_continuation`

### Cluster B (BH-004) — Inland-Layover-Override & Tour-Land-Erkennung
**Komplex, einzeln deployen:**

- **BH-004a Cluster-C2 strikter** (SE-Inland überstimmt nur bei klarer Foreign-Evidence)
  - Files: `_deterministic_classify_v7` Cluster-C2-Branch
  - Risiko: mittel (könnte echte CAS-Foreign-Overrides verhindern)
  - KPI: 09-27 Z74 Deutschland statt Z76 AGP → +1 Z74, −1 Z76, −23€
  - Tests: `test_se_inland_dus_not_overridden_by_cas_agp`

- **BH-004b Layover_ort Inland-Detection** (BER/LEJ/Inland-Codes erkennen)
  - Files: `_get_bmf_for_iata` oder Tag-Klass-Branch
  - Risiko: niedrig (additive)
  - KPI: hotel −3 bis −5
  - Tests: `test_inland_layover_ber_lej_not_hotel_z76`

### Cluster C (#222) — normalized_tours-Schicht (Langfristig)
Tour-Ziel-Land aus CAS+SE statt per-Day-Layover. Multi-Stop-Touren aggregieren zum Hauptziel.

**Defer:** Zu groß für BH-003. Bleibt als #222 Backlog. Wenn nach Cluster A+B die Restdiff <50€, Cluster C optional.

### Cluster D (F5 #228) — BMF-Tagessätze
**Nach Cluster A+B**, wenn Restdiff z76 noch >50€ bleibt.

---

## §8 Empfohlene Reihenfolge

| # | Bug | Cluster | KPI-Effekt | Risiko |
|---|---|---|---|---|
| 1 | **BH-003a** Issue→Z76 für 01-04/01-06 | A | +2 Z76, +70€, −2 Issue | hoch |
| 2 | **BH-003b** Frei-Marker nicht Issue | A | −6 arbeitstage, −6 reinigung | mittel |
| 3 | **BH-004a** Cluster-C2 strikter | B | +1 Z74, −1 Z76, −23€ | mittel |
| 4 | **BH-004b** Inland-Layover-Hotel-Skip | B | −3 bis −5 hotel | niedrig |
| 5 | Restdiff-Check + ggf. #228 F5 | D | <100€ z76 | niedrig |

Nach #1–4 Erwartung:
- arbeitstage: 140 → 134 → 133 ✓
- hotel: 78 → 72 → 67 → 66 ✓
- z73/z74: +korrekt
- z76_eur: 4437 → ~4500–4700, Restdiff für #228

---

## §9 Daten-Limitierung

- **Token AT-C33E… ist expired** (>24h alt) → Cloud Run liefert keine `_tage_detail` mehr
- **`/tmp/tibor.json` weg** durch tmpfs-Cleanup
- **Diagnose verwendet:** §1 aus Phase-A-Forensik vom 2026-05-15 (memorisiert) + §2 Issue-Tage und §4-§5 Inland-Z76-Tage (memorisiert) + §6 Golden-Aggregate (live geladen)
- **Für jeden Fix ist neuer synthetischer Test mit Mini-Repro erforderlich** (kein neuer Tibor-Live-Run nötig)
- Volle Verifikation nach allen Fixes mit **einem** Live-Run am Ende (User-Memory: max 1 Live-Run/Iteration)
