# BH-CORE-001 — Tibor Cases Catalog

Konkrete Fälle aus `tests/fixtures/tibor_aerotax_v11_raw_initial.json` + Golden, die durch die Tour-First-Pipeline gelöst werden sollen. Jeder Fall: aktueller Zustand → erwartete neue Logik → Erfolgsmessung.

---

## Case 1: Bangalore-Tour 2025-01-03 bis 2025-01-06

### Raw Facts

| Datum | Marker | Routing | overnight | layover_ort | starts_HB | ends_HB | duty_min |
|---|---|---|---|---|---|---|---:|
| 01-03 | `31591 P1` | FRA→BLR | True | BLR | True | False | 785 |
| 01-04 | `X` | (leer) | True | BLR | False | False | 0 |
| 01-05 | `755 LH755-1` | BLR→FRA | True | BLR | False | False | 31 |
| 01-06 | `755 LH755-1` | BLR→FRA | False | (leer) | True | True | 561 |

### Golden Soll

| Datum | Golden klass | Land | Betrag | pos_in_tour | is_anreise | is_abreise |
|---|---|---|---:|---|---|---|
| 01-03 | Z73 | Deutschland | 14 | 1/4 | True | False |
| 01-04 | Z76 | Indien - Bangalore | 42 | 2/4 | False | False |
| 01-05 | Z76 | Indien - Bangalore | 42 | 3/4 | False | False |
| 01-06 | Z76 | Indien - Bangalore | 28 | 4/4 | False | True |

### Aktuelle AeroTAX-Klassifikation (fixture)

| Datum | IST klass | Reason |
|---|---|---|
| 01-03 | Z76 | „Auslands-Layover BLR (Z76 An/Ab)" — sollte Z73 sein (Abend-Briefing 10:55 ist OK aber Golden zählt es als Inland-An wegen Briefing-Lokation) |
| 01-04 | **Frei** | „frei" — **falsch**, sollte Z76 Mitte sein |
| 01-05 | Z73 | „Auslandstour-Anreise mit Abend-Briefing 23:28 → Inland-Anreise 14€" — **falsch**, sollte Z76 Mitte sein |
| 01-06 | Issue → Z76 (post BH-003a) | nach BH-003a fix |

### Tour-First-Logik (Soll)

```
_normalize_tours_from_raw_facts liest:
  - 01-03 starts_at_homebase=True, routing=FRA→BLR, overnight=True
    → tour_start, foreign destination BLR (Indien-Bangalore), location=in_flight
  - 01-04 marker=X, prev.overnight=True, prev.layover_ort=BLR, next.overnight=True
    → Sandwich-Pattern erkannt: tour_mid, location=foreign_layover
    → KI-Resolver tour_context bestätigt mit conf≥0.85
  - 01-05 routing=BLR→FRA, prev.overnight=True
    → tour_mid (noch nicht zuhause), location=in_flight
  - 01-06 ends_at_homebase=True, routing=BLR→FRA, prev.overnight=True
    → tour_end, BMF-Land Indien
```

Output normalized_tours: 1 Tour T01 mit 4 days, primary_destination=BLR, country=Indien-Bangalore.

Classifier:
- 01-03 (tour_start, foreign, in_flight, briefing 10:55 morgens) → Z76 An/Ab oder Z73 Inland-Anreise nach Briefing-Lokations-Regel
- 01-04 (tour_mid, foreign_layover, no duty) → Z76 voll_24h
- 01-05 (tour_mid, in_flight back to homebase) → Z76 voll_24h
- 01-06 (tour_end, foreign Heimkehr) → Z76 An/Ab

### Erfolgsmessung

- Tour bleibt 1 Stück (nicht gesplittet)
- 01-04 wird NICHT Frei
- 01-05 wird Z76, NICHT Z73
- 01-06 bleibt Z76 (BH-003a-kompatibel)
- Sum für Tour: 42+42+42+28 = 154 € (Golden) ±30 €

---

## Case 2: Korea-Tour 2025-04-23 bis 2025-04-26 (RES-Pattern)

### Raw Facts

| Datum | Marker | Routing | overnight | layover_ort |
|---|---|---|---|---|
| 04-23 | `RES` | (TBD aus fixture) | True (vermutlich) | (TBD) |
| 04-24 | `RES` | | True | |
| 04-25 | `RES` | | True | |
| 04-26 | `RES` | | False | |

### Golden Soll

| Datum | Golden klass | Land | tour_pos | is_anreise | is_abreise |
|---|---|---|---|---|---|
| 04-23 | Z73 | Deutschland | 1/4 | True | False |
| 04-24 | Z76 | Republik Korea | 2/4 | False | False |
| 04-25 | Z76 | Republik Korea | 3/4 | False | False |
| 04-26 | Z76 | Republik Korea | 4/4 | False | True |

### Aktuelle AeroTAX-Klassifikation

Alle 4 Tage: `Standby` (klass=Standby, marker=RES, kein Tour-Kontext erkannt).

### Tour-First-Logik (Soll)

```
_normalize_tours_from_raw_facts:
  - 04-23 RES + (Reader-Daten zu verifizieren: routing/overnight)
  - 04-24/25 RES + prev.overnight=True
    → standby_context KI-Resolver:
      "RES + prev.overnight=True + foreign-layover-context"
      → is_standby_hotel=True, role=tour_mid
  - 04-26 RES + ends_at_homebase, prev.overnight=True
    → tour_end
```

KI darf `standby_context` resolven mit value `{is_standby_hotel: True, location: 'foreign_hotel', destination: 'ICN'}`.

Classifier:
- 04-23 (tour_start) → Z73 (Briefing-in-DE) oder Z76 An/Ab (je BMF-Regel)
- 04-24/25 (tour_mid, standby_hotel foreign) → Z76 voll_24h
- 04-26 (tour_end foreign) → Z76 An/Ab

### Erfolgsmessung

- RES wird NICHT Standby-zuhause
- 4 Tage = 1 Tour
- arbeitstage zählt diese 4 Tage als Tour-Tage
- Z76-Betrag: 48+48+48+32 ≈ 176 € (Golden) ±30 €

**Schwierigkeit:** Aktuelle fixture-Daten für 04-23 bis 04-26 müssen verifiziert werden: routing, overnight, layover_ort. Wenn Reader nichts geliefert hat (RES marker ohne weitere CAS-Detail), braucht KI-Resolver mehr Evidence aus SE-Stamp.

---

## Case 3: X-Marker innerhalb foreign tour (15 Tage in Tibor)

### Pattern

Marker `X` (manchmal `X HKG`, `X BLR`, etc.) zwischen 2 overnight=True Tagen mit foreign layover_ort.

### Liste (aus REVIEW_DIFF.csv)

| Datum | Marker | layover_ort | Golden klass | Golden Land |
|---|---|---|---|---|
| 2025-01-04 | X | BLR | Z76 | Indien - Bangalore |
| 2025-01-20 | X HKG | HKG | Z76 | China - Hong Kong |
| 2025-02-14 | X HND | HND | Z76 | Japan - Tokyo |
| 2025-03-30 | X BOM | BOM | Z76 | Indien - Mumbai |
| 2025-04-10 | X | ICN | Z76 | Republik Korea |
| 2025-05-15 | X TLV | LAX | Z76 | USA |
| 2025-05-27 | X TLV | ORD | Z76 | USA - Chicago |
| 2025-06-09 | X | SIN | Z76 | Singapur |
| ... | | | | (7 more) |

Aktuelle Klassifikation: alle `Frei`.

### Tour-First-Logik

X-Marker + prev.overnight=True + foreign layover → `role=tour_mid`, `is_layover_free_day=True`.

KI-Resolver `marker_semantics` mit conf≥0.85:
```
{
  'semantics': 'tour_mid_at_foreign_hotel',
  'meaning': 'OFF-day during active foreign tour (Hotel-Rest-Day)',
  'evidence': ['prev.overnight=True', 'layover_ort=BLR (foreign)',
               'marker text contains airport code BLR']
}
```

Hinweis: Wenn Marker explizit Airport-Code enthält (z.B. `X HKG`), ist Confidence hart (≥0.95).

### Erfolgsmessung

Alle 15 Tage werden Z76 Volltag. **Größter KPI-Gewinn** der gesamten BH-CORE-001.

---

## Case 4: `==` Marker innerhalb foreign tour (4 Tage)

### Liste

| Datum | Marker | Golden klass | Golden Land |
|---|---|---|---|
| 2025-04-01 | == | Z76 | Indien - Mumbai |
| 2025-07-23 | == | Z76 | Schweden |
| 2025-09-20 | == | Z72 | Deutschland |
| 2025-11-18 | == | Z76 | Norwegen |

Aktuelle Klassifikation: alle `Frei`.

### Tour-First-Logik

Identisch X-Pattern: Sandwich-Pattern + KI-Resolver.

`==` ist mehrdeutig: Frei zuhause vs Layover. Im Tour-Kontext → tour_mid. Außerhalb → Frei.

09-20 ist interessant: Golden = Z72 Deutschland → Same-Day Inland >8h. Reader hat aktuell `activity_type='frei'` — vermutlich Reader-Bug.

---

## Case 5: `OFF` Marker während Auslandstour (3 Tage)

### Liste

| Datum | Marker | layover_ort (Reader) | Golden klass | Golden Land |
|---|---|---|---|---|
| 2025-05-17 | OFF | (TBD) | Z76 | USA |
| 2025-06-17 | OFF | (TBD) | Z76 | Kroatien |
| 2025-06-18 | OFF | (TBD) | Z76 | Kroatien |

Aktuelle Klassifikation: alle `Frei`.

### Tour-First-Logik

Identisch zu X/==-Pattern. OFF in foreign tour-context → tour_mid.

---

## Case 6: Z76-Tour-Anreise/Heimkehr-Double-Count (7 Tage)

### Liste (Extras aus REVIEW_DIFF, AeroTAX zählt zu viel)

| Datum | Marker | Routing | AeroTAX klass | Golden status |
|---|---|---|---|---|
| 2025-05-20 | 103703 P1 | FRA→LAD | Z73 | NICHT in Golden |
| 2025-05-21 | 103703 P1 | LAD | Z76 | NICHT in Golden |
| 2025-05-22 | 103703 P1 | LAD→FRA | Z76 | NICHT in Golden |
| 2025-06-01 | 126533 PU | FRA→CPH→GOT | Z76 | NICHT in Golden |
| 2025-06-02 | 126533 PU | GOT→FRA→SOF | Z76 | NICHT in Golden |
| 2025-09-25 | 15688 PU | FRA→BER→KRK | Z76 | NICHT in Golden |
| 2025-12-15 | 57783 P1 Tag 2 | JFK→FRA | Z76 | NICHT in Golden |

**Hypothese 1 (Golden-Bug):** FollowMe.aero (Golden) erfasst diese Touren nicht (manueller-Eintrag-Verlust). Dann ist AeroTAX **richtig**.

**Hypothese 2 (AeroTAX-Bug):** Diese sind Anreise/Heimkehr-Hälften die im Golden zur Vor/Folge-Tour gehören. AeroTAX zählt sie doppelt.

### Tour-First-Logik

normalized_tours sollte erkennen ob 05-20 bis 05-22 EINE Tour ist oder ob 05-20 zu einer früheren Tour gehört und 05-22 zu einer späteren.

Cross-Check mit Golden `tours` Liste klären welche Hypothese stimmt.

### Erfolgsmessung

Wenn Hypothese 1: arbeitstage bleibt korrekt, Golden-Doku-Fehler dokumentieren.
Wenn Hypothese 2: Tour-Boundary-Logik korrigiert sich, arbeitstage Δ=−7.

---

## Case 7: Z72-Inland-Office >8h (5 Tage, AeroTAX zählt zu viel)

### Liste

| Datum | Marker | Duty | AeroTAX | Golden |
|---|---|---:|---|---|
| 2025-03-22 | 83343 PU FRA→TOS | 510m | Z72 (Same-Day-Roundtrip Norge) | NICHT in Golden |
| 2025-04-07 | ORTSTAG FRS | 1439m | Z72 (Office Inland >8h) | NICHT in Golden |
| 2025-04-28 | LMN_AS LMN_CR1 | 600m | Z72 | NICHT in Golden |
| 2025-05-19 | LMN_AS / LMN_CR1 | 600m | Z72 | NICHT in Golden |
| 2025-07-03 | 129023 PU / Tag 3 OTP→FRA→LHR | 485m | Z72 (Same-Day Z72) | NICHT in Golden |

### Tour-First-Logik

- **03-22** `FRA→TOS`: TOS = Tromsø Norwegen. Routing **endet nicht in homebase**, also kein same_day-roundtrip. Vermutlich Tour-Start für Tour die in Folgetagen weitergeht. NICHT Z72 Inland.
- **04-07** `ORTSTAG FRS` mit duty=1439min (= 24h reader-fehler): Marker-passive. Duty-Wert ist verdächtig (24h ist unmöglich). → non_tour, NICHT Z72, KI-Resolver für duty-cleanup.
- **04-28**, **05-19** `LMN_AS LMN_CR1`: Medical-License-Marker. Tibor's BMF-Soll: nicht zählbar. → non_tour passive, NICHT Z72.
- **07-03** `OTP→FRA→LHR`: routing endet **LHR (London foreign)**, also NICHT Inland-Roundtrip. → tour_start oder tour_mid foreign, NICHT Z72 Inland.

### Erfolgsmessung

Alle 5 Tage **nicht** als Z72 gezählt nach BH-CORE-001.

---

## Case 8: 09-27 AGP/DUS-Override (kritisch)

### Raw Facts (zu verifizieren)

| Datum | Marker | Routing | layover_ort | SE-Ort | SE-Inland |
|---|---|---|---|---|---|
| 09-26 | (TBD) | (TBD) | IST | MUC | True |
| 09-27 | (TBD) | (TBD) | AGP | DUS | True |

### Golden Soll

| Datum | Golden klass | Land | Betrag |
|---|---|---|---:|
| 09-26 | Z76 | Bulgarien | 15 |
| 09-27 | **Z74** | **Deutschland** | **28** |

### Aktuelle AeroTAX-Klassifikation

Cluster-C2 überstimmt SE-Inland mit CAS-Foreign:
- 09-26: Z76 Türkei 24€
- 09-27: Z76 Spanien-Málaga 23€

Beide Tage falsch klassifiziert.

### Tour-First-Logik

```
_normalize_tours_from_raw_facts erkennt:
  - 09-25 prev: tour_start FRA→KRK overnight=True (Krakow)
  - 09-26 layover_ort=IST aber SE-Stamp=MUC → ambig
    → KI-Resolver tour_context mit beiden evidences
  - 09-27 layover_ort=AGP aber SE-Stamp=DUS → ambig
    → KI-Resolver: prev=IST/Türkei, next.routing=?
    → wenn next=homebase → tour_end inland (Z74 Volltag)
    → wenn next=foreign → tour_mid foreign

Cross-Check Golden: 09-27 ist Z74 → Tour endet zuhause am 28., 27. ist Inland-Volltag.
```

### Erfolgsmessung

- 09-27 wird Z74 Deutschland 28€ (NICHT Z76 AGP)
- Cluster-C2 Override wird strikter (oder ersetzt durch tour_context KI)

---

## Case 9: SE-Override Overshoot

Aktuell springt Phase-1-SE-Override sehr breit auf jeden `Frei`-Tag mit foreign-SE-Stamp. Lebt im Verdacht zu viele Tage rescue zu greifen.

### Bekannte SE-Overshoot-Kandidaten

| Datum | Marker | SE-Ort | SE-Inland | Aktuell | Golden | Hypothese |
|---|---|---|---|---|---|---|
| 2025-05-17 | OFF | (foreign?) | False | Frei (oder Z76 nach rescue?) | Z76 USA | korrekt rescued |
| 2025-06-17 | OFF | LCA (Larnaca) | False | Frei→Z76 | Z76 Kroatien | rescued aber **falsches Land** (LCA Zypern vs Golden Kroatien) |
| 2025-06-18 | OFF | LCA | False | Frei→Z76 | Z76 Kroatien | wie 06-17 |

### Tour-First-Logik (Soll)

SE-Override nur greifen wenn ZUSÄTZLICH Tour-Evidence:
- 06-17/18: prev.overnight=True UND prev.layover_ort=Foreign (Tour-Kontext)
- SE-Stamp dient als Bestätigung, NICHT als Trigger

09-26/27 Anti-Pattern: SE-Inland nicht überschrieben werden ohne starke CAS-Foreign-Continuation-Evidenz.

---

## Case 10: Hotelnächte 66 vs 78 (Tibor)

Aktuelle Formel: `Σ Z76-tage per Tour − 1` produziert vermutlich zu viel (live=78, fixture=46, golden=66).

Diskrepanz zur Golden-Definition siehe `HOTEL_SEMANTICS_AUDIT.md`.

### Tour-First-Logik (Soll)

Hotelnacht direkt aus normalized_day:
```
if day.overnight_after_day and day.role in {tour_start, tour_mid}:
    hotel_count += 1
# tour_end zählt NIE (Crew schläft zuhause nach Heimkehr)
# non_tour zählt NIE
```

Inland-Hotel (Z74/Z73-Volltag mit overnight) zählt mit.

---

## Case 11: Metro-Codes CHI/ROM/STO

Drei IATA-Metro-Codes müssen vor BMF-Lookup aufgelöst werden:
- `CHI` → Chicago Metro (BMF: USA - Chicago)
- `ROM` → Rome Metro (BMF: Italien - Rom)
- `STO` → Stockholm Metro (BMF: Schweden)

Phase 3-Fix existiert bereits in `bmf_data.IATA_METRO_TO_BMF`. Tour-First muss diesen Helper weiter nutzen für `bmf_place_code`-Resolution.

---

## Summary Table: Erwarteter KPI-Effekt pro Case

| Case | Tage | KPI-Effekt | Risiko |
|---|---:|---|---|
| 1 Bangalore | 4 | Z76 ×3 sauber, Z73 ×1 → +120 € z76 | niedrig |
| 2 RES Korea | 4 | Z76/Z73 statt Standby → +176 € z76 | mittel |
| 3 X-Marker | 15 | Z76 Volltag → +600 € z76, +15 arbeitstage | niedrig |
| 4 ==-Marker | 4 | Z76 + 1 Z72 → +150 € z76 | mittel |
| 5 OFF-Marker | 3 | Z76 Volltag → +120 € z76 | niedrig |
| 6 Z76-Double-Count | 7 | je nach Hypothese: −7 arbeitstage oder Golden-Bug | hoch |
| 7 Z72-Office >8h | 5 | −5 Z72, −70 € z72 | mittel |
| 8 09-27 AGP/DUS | 1 | Z74 +1, Z76 −1 (mit Land-Wechsel) | mittel |
| 9 SE-Override | (?) | tightening guard, weniger false-positives | hoch |
| 10 Hotelnächte | n/a | hotel 78→66 (−12) | mittel |
| 11 Metro-Codes | bereits gefixt | n/a | niedrig |

**Erwarteter Netto-Effekt (grob):**
- arbeitstage: 140 → ~133 (−7)
- hotel: 78 → ~66 (−12)
- z76_eur: 4437 → ~4794 (+357)
- gesamt: 5621 → ~6020 (+400)

Zielwerte matchen Golden ±150 €.
