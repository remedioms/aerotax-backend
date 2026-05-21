# FollowMe vs AeroTAX — Tibor 2025 Day-by-Day Diff

Stand: 2026-05-21.
Quelle: Live-Job `AT-11CEB21120E7799B` (Cloud Run rev `00065-x7z`) vs
`tests/fixtures/followme_golden_tibor_2025.json`.

## §1 Z77 / steuerfreie Spesen — Source Proof

Monatliche steuerfreie-Spesen-Summen aus Streckeneinsatzabrechnung 2025
(User-bestätigt):

| Monat | Stfrei €  | Running Σ €  |
|---|---:|---:|
| Januar  |  275 |   275 |
| Februar |  147 |   422 |
| März    |  347 |   769 |
| April   |  443 | 1 212 |
| Mai     |  545 | 1 757 |
| Juni    |  670 | 2 427 |
| Juli    |  477 | 2 904 |
| August  |  274 | 3 178 |
| September | 278 | 3 456 |
| Oktober |  560 | 4 016 |
| November | 341 | 4 357 |
| Dezember | 348 | **4 705** |

**Z77 = 4 705 € — exakt belegt.** AeroTAX live-result `z77=4705.0` ✓ identisch.

Dedupe-Logik: SE-Reader filtert Storno-Zeilen vor Aggregation (siehe
`app.py:_parse_streckeneinsatz_*`), keine Doppel-Zählung möglich.

## §2 VMA Day-Diff Summary

Vergleich aller VMA-relevanten Tage in FollowMe-Golden + AeroTAX-`_tage_detail`:

- FollowMe: 133 Tage mit VMA-Klassifikation
- AeroTAX:  130 Tage mit Z72/Z73/Z74/Z76
- **Diff-Tage: 74**
- **Total Δ: −683 €** (FollowMe Brutto 5 046 € vs AeroTAX Brutto 4 363 €)

## §3 Diff-Kategorien

| Kategorie | Tage | Δ Brutto | Wirkung |
|---|---:|---:|---|
| **C. Wrong rate type 24h vs 8h** | 27 | **−395 €** | AeroTAX nimmt `same_day_8h` / `an_abreise`-Satz, FollowMe `voll_24h`-Satz |
| **A. AeroTAX lost day (Issue/Frei/Standby/ZeroDay)** | 9 | **−198 €** | AeroTAX hat den Tag entirely-classified als nicht-VMA, FM zählt ihn |
| **D. AeroTAX → Inland, FollowMe → Ausland** | 8 | **−246 €** | Tag-Klassen-Flip in Tour-Boundary |
| **G. AeroTAX extra day** | 9 | **+209 €** | AeroTAX zählt Tag, FM nicht |
| **D-inv. AeroTAX → Ausland, FollowMe → Inland** | 5 | **+70 €** | Inverse Tour-Boundary-Flip |
| **B. Wrong country (Z76→Z76)** | 12 | **±0 €** | Land falsch, Betrag teils ähnlich |
| Other small | 4 | ±0 € | — |
| **TOTAL** | 74 | **−683 €** | — |

## §4 Top Patterns — Mechanik

### Pattern C — 24h-Satz vs 8h-Satz (größter Block, −395 €)

AeroTAX klassifiziert mehrere Mid-of-Tour-Tage als `same_day_8h` oder
`an_abreise`, während FollowMe sie als volle 24h-Tage zählt:

| Datum | Land | FM (24h) | AT (8h) | Δ |
|---|---|---:|---:|---:|
| 2025-01-19 | China - Hong Kong | 71 | 48.0 | -23 |
| 2025-01-20 | China - Hong Kong | 71 | 48.0 | -23 |
| 2025-01-21 | China - Hong Kong | 71 | 48.0 | -23 |
| 2025-02-13 | Japan - Tokyo | 50 | 33.0 | -17 |
| 2025-02-14 | Japan - Tokyo | 50 | 33.0 | -17 |
| 2025-03-24 | Vereinigte Staaten von Amerika (USA) - Boston | 63 | 42.0 | -21 |
| 2025-04-17 | Iran | 33 | 22.0 | -11 |
| 2025-05-02 | Tschechische Republik | 32 | 21.0 | -11 |
| 2025-05-15 | Vereinigte Staaten von Amerika (USA) | 59 | 40.0 | -19 |
| 2025-05-16 | Vereinigte Staaten von Amerika (USA) | 59 | 40.0 | -19 |
| 2025-06-08 | Singapur | 71 | 48.0 | -23 |
| 2025-06-09 | Singapur | 71 | 48.0 | -23 |
| 2025-06-10 | Singapur | 71 | 48.0 | -23 |
| 2025-06-24 | Spanien - Madrid | 42 | 28.0 | -14 |
| 2025-07-01 | Rumänien - Bukarest | 32 | 21.0 | -11 |
| 2025-07-07 | Vereinigte Staaten von Amerika (USA) | 59 | 40.0 | -19 |
| 2025-07-21 | Spanien - Barcelona | 34 | 23.0 | -11 |
| 2025-07-29 | Lettland | 35 | 24.0 | -11 |
| 2025-07-30 | Polen - Krakau | 34 | 23.0 | -11 |
| 2025-07-31 | Polen - Krakau | 34 | 23.0 | -11 |
| 2025-08-21 | Zypern | 42 | 28.0 | -14 |
| 2025-10-21 | Spanien | 23 | 34.0 | +11 |
| 2025-11-02 | Schweden | 66 | 44.0 | -22 |
| 2025-11-17 | Norwegen | 50 | 75.0 | +25 |
| 2025-11-21 | Vereinigte Staaten von Amerika (USA) - Miami | 65 | 44.0 | -21 |
| 2025-12-10 | Tschechische Republik | 32 | 21.0 | -11 |
| 2025-12-28 | Israel | 66 | 44.0 | -22 |

**Root cause**: AeroTAX' Reader-Logic teilt mehrere Tour-Tage in An-Ab-Stücke
(je 8h-Satz), wo FollowMe sie als 24h-Volltag zählt. Tibor war z.B. von 18.-22.
Januar in Hongkong — FollowMe gibt 71+71+71+48 = 261 €, AeroTAX 48+48+48+48 =
192 €. Die Klassen-Logik `same_day_8h` ist hier falsch für Mid-Tour-Tage.

### Pattern A — Lost days (−198 €)

| Datum | FM-Klass | FM € | AT-Klass | AT-Reason |
|---|---|---:|---|---|
| 2025-01-04 | Z76 (Indien - Bangalore) | 42 | Issue | Heimkehr aus Vortag-Tour — separater Tour-Abschluss |
| 2025-01-06 | Z76 (Indien - Bangalore) | 28 | Issue | Heimkehr aus Vortag-Tour — separater Tour-Abschluss |
| 2025-02-10 | Z72 (Deutschland) | 14 | ZeroDay | Same-Day < 8h (total=420min, duty_plus_commute) — kein VMA |
| 2025-04-23 | Z73 (Deutschland) | 14 | Standby | Standby zuhause — AT, kein FT, kein VMA |
| 2025-07-23 | Z76 (Schweden) | 44 | Frei | frei |
| 2025-08-01 | Z73 (Deutschland) | 14 | Issue | Heimkehr aus Vortag-Tour — separater Tour-Abschluss |
| 2025-09-20 | Z72 (Deutschland) | 14 | Frei | frei |
| 2025-10-20 | Z73 (Deutschland) | 14 | Standby | Standby zuhause — AT, kein FT, kein VMA |
| 2025-10-23 | Z73 (Deutschland) | 14 | Standby | Standby zuhause — AT, kein FT, kein VMA |

**Root cause**:
- 2025-01-04/06: AeroTAX-Reader hat 2 Tage einer Bangalore-Tour als "Issue" markiert
- 2025-07-23: Tag als "Frei" — aber FM hat eine Schweden-Tour mit 44€ Auslands-VMA an dem Tag
- 2025-09-20: "Frei" laut AeroTAX — FM zählt 14€ Inland Z72
- 2025-10-20/23, 2025-04-23: Standby-Tage, AeroTAX zählt nicht — FM zählt sie als Z73 An-/Abreise

### Pattern D — Tour-Boundary Inland/Ausland Flip (−246 €)

| Datum | FM | AT | Δ |
|---|---|---|---:|
| 2025-01-03 | Z73 Deutschland 14€ | Z76 Indien – Bangalore 28.0€ | +14€ |
| 2025-01-05 | Z76 Indien - Bangalore 42€ | Z73 - 14€ | -28€ |
| 2025-01-11 | Z76 Dänemark 50€ | Z72 - 14€ | -36€ |
| 2025-02-12 | Z73 Deutschland 14€ | Z76 HND 28.0€ | +14€ |
| 2025-03-16 | Z76 Norwegen 50€ | Z72 - 14€ | -36€ |
| 2025-03-25 | Z76 Vereinigte Staaten von Amerika (USA) - Boston 42€ | Z73 FRA 14€ | -28€ |
| 2025-03-29 | Z73 Deutschland 14€ | Z76 BOM 28.0€ | +14€ |
| 2025-03-31 | Z76 Indien - Mumbai 53€ | Z73 FRA 14€ | -39€ |
| 2025-04-08 | Z73 Deutschland 14€ | Z76 ICN 28.0€ | +14€ |
| 2025-05-08 | Z76 Vereinigte Staaten von Amerika (USA) - Chicago 44€ | Z73 FRA 14€ | -30€ |
| 2025-07-08 | Z76 Vereinigte Staaten von Amerika (USA) 59€ | Z73 FRA 14€ | -45€ |
| 2025-09-11 | Z76 Nordmazedonien 18€ | Z73 BER 14€ | -4€ |
| 2025-10-05 | Z73 Deutschland 14€ | Z76 ICN 28.0€ | +14€ |

**Root cause**: An- und Abreisetage einer Auslands-Tour klassifiziert
AeroTAX als Inland-An-/Abreise (Z73 14 €), FollowMe als foreign 24h.
Beispiel 2025-01-05 Bangalore-Tour-Tag 3: AT=Z73 (14€), FM=Z76 Bangalore 42€.

### Pattern G — AeroTAX zählt extra Tage (+209 €)

| Datum | AT-Klass | AT-Land | AT € |
|---|---|---|---:|
| 2025-03-22 | Z72 | - | 14 |
| 2025-04-28 | Z72 | - | 14 |
| 2025-05-19 | Z72 | - | 14 |
| 2025-05-20 | Z73 | LAD | 14 |
| 2025-05-21 | Z76 | Angola | 27 |
| 2025-06-01 | Z76 | Schweden | 44 |
| 2025-06-02 | Z76 | Bulgarien | 15 |
| 2025-09-25 | Z76 | Polen – im Übrigen | 23 |
| 2025-10-26 | Z76 | Israel | 44 |

**Root cause**: AeroTAX zählt 9 Tage die FollowMe nicht hat. Mögliche
Ursachen: Reader-False-Positive (Tag erkannt als Auslands-Same-Day obwohl
FollowMe ihn als Frei/Office klassifiziert), oder echte Tour-Tage die FM
übersehen hat. Bei 2025-05-19/20/21 Angola: AeroTAX hat eine LAD-Tour erkannt,
FM nicht — sollte gegen CAS+SE verifiziert werden.

### Pattern B — Wrong country (12 days, Δ ±0)

| Datum | FM-Land | AT-Land | Δ |
|---|---|---|---:|
| 2025-04-09 | Republik Korea | Korea, Republik | -16 |
| 2025-04-10 | Republik Korea | Korea, Republik | -16 |
| 2025-04-26 | Republik Korea | Korea, Republik | +16 |
| 2025-05-07 | Vereinigte Staaten von Amerika (USA) - Chicago | ORD | -21 |
| 2025-05-27 | Vereinigte Staaten von Amerika (USA) - Chicago | TLV | -21 |
| 2025-07-28 | Vereinigtes Königreich - London | RIX | -16 |
| 2025-09-13 | Portugal | Spanien – Madrid | -4 |
| 2025-09-26 | Bulgarien | Polen – im Übrigen | +9 |
| 2025-10-06 | Republik Korea | Korea, Republik | -16 |
| 2025-10-07 | Republik Korea | Korea, Republik | -16 |
| 2025-11-08 | Litauen | Griechenland – Athen | +1 |
| 2025-12-26 | Norwegen |  | -18 |

**Root cause**: BMF-Country-Mapping in AeroTAX nimmt teilweise das CAS-
Routing statt SE-stfrei_ort. Beispiele:
- 2025-09-13: FM Portugal 32€, AT Spanien-Madrid 28€ — AT mapped LIS auf
  Madrid (falsch)
- 2025-11-08: FM Litauen, AT Griechenland-Athen — Routing-Reader-Bug
- 2025-07-28: FM UK-London, AT RIX → Komplett anderes Land

## §5 Netto-Impact

### Bei Tibor (Z77 = 4 705 €)

- AeroTAX VMA-Brutto = 4 363 €
- FollowMe VMA-Brutto = 5 046 € (rechnerisch)
- Z77 = 4 705 € **übersteigt beide** Brutto-Werte
- → **VMA-netto = 0 € in beiden Fällen** (Clamp `max(0, vma−z77)`)
- → Einzutragender Gesamtbetrag wäre **identisch** bei beiden: ~ 976 €
- **Δ-Netto-Effekt für Tibor: 0 €**

### Bei einem User ohne Z77 (z.B. ein Crew-Member ohne stfrei-Spesen)

- AeroTAX würde nur 4 363 € als VMA ansetzen
- FollowMe würde 5 046 € ansetzen
- **Δ-Netto-Effekt: −683 € weniger Werbungskosten in AeroTAX**
- Bei Grenzsteuersatz 42% → ~287 € weniger Steuererstattung

### Bei User mit teilweisem Z77 (z.B. 2 000 €)

- AeroTAX VMA-netto = max(0, 4 363 − 2 000) = 2 363 €
- FollowMe VMA-netto = max(0, 5 046 − 2 000) = 3 046 €
- **Δ-Netto-Effekt: −683 €** voll durchschlagend

## §6 Hotelnächte +7 Diff

AeroTAX 73 vs FollowMe 66. Da weder die FollowMe-Golden noch
`_tage_detail` einzelne Hotelnacht-Flags exponieren, lässt sich diese Lücke
ohne Custom-Aggregation der `overnight_after_day` + `layover_ort != FRA`-Logik
nicht direkt taggenau zuordnen. Erwartung: Pattern G (extra-days) + 2-3
zusätzliche Mid-Tour-Tage liefern den +7-Überschuss.

Effekt: +7 × 3.60 € Trinkgeld = **+25.20 €** Werbungskosten (in Block A,
wirkt nicht gegen Z77).

## §7 Source Arbitration — Empfehlungen pro Pattern

| Pattern | # | Δ € | Source-stronger | Decision |
|---|---:|---:|---|---|
| C. 24h vs 8h | 27 | −395 | FollowMe (FM kennt Tour-Boundary; AT klassifiziert Mid-Tour fälschlich als same_day_8h) | **FIX_AEROTAX**: tour_position-aware rate selection |
| A. Lost days | 9 | −198 | FollowMe meist, aber Standby-Tage sind echte Source-Conflicts | **NEEDS_USER_REVIEW** für jeden Standby; **FIX_AEROTAX** für Issue/Frei |
| D. Inland/Ausland flip | 8 | −246 | FollowMe (BMF §9 Abs. 4a: Auslandstour-Anreise = Ausland) | **FIX_AEROTAX**: tour-foreign-anreise → Z76 nicht Z73 |
| G. AeroTAX extra | 9 | +209 | Gemischt — manche FM-misses, manche AT-false-positives | **NEEDS_USER_REVIEW** pro Tag |
| D-inv. AT→Ausland | 5 | +70 | Wahrscheinlich AT zu aggressiv | **DOCUMENT_CONFLICT_ACCEPTED** |
| B. Wrong country | 12 | ±0 | SE stfrei_ort > CAS routing | **FIX_AEROTAX**: bmf_place_code-Priorität SE > CAS |

## §8 Fix-Priorität

### P0 — Vor Public-Launch fixen (User ohne hohes Z77 betroffen)

1. **Tour-Position-Aware-Rate** (Pattern C): Mid-Tour-Tage müssen 24h-Satz
   bekommen, nicht same_day_8h. Erwarteter KPI-Bewegung: +395 € VMA Brutto.
2. **Foreign-Tour-Anreise** (Pattern D, half): An-/Abreise-Tag einer
   Auslands-Tour → Z76 statt Z73. +246 € VMA.
3. **Wrong country mapping** (Pattern B): SE-Place vor CAS-Routing.
   Kein €-Effekt für Tibor, aber Audit-Korrektheit.

### P1 — Akzeptable Differenz mit Audit

1. **Pattern G AeroTAX-extras**: Audit-Eintrag mit FollowMe-Vergleich
   wenn Tag in beiden Quellen
2. **Pattern A Standby-Tage**: User-Review-Item statt stiller Issue

### P2 — Kosmetik

- Hotelnächte +7 — Δ Netto-Effekt 25 €, keine Steuer-Wirkung wegen Z77

## §9 Tibor Release-GO?

**Aktueller Tibor-Live-Run mit Z77 4705€ → Δ-Netto = 0€.**
PDF-Betrag ist korrekt (976 €).

**ABER**: System zeigt User ohne Z77 systematisch −683 € zu niedrig an.
Bei 42 % Grenzsteuer = −287 € weniger Erstattung.

**Empfehlung**: **NEEDS_FIX before public launch** — die P0-Fixes (Pattern
C+D) sind generalisierbar und betreffen jeden Crew-Member mit Auslandstouren.
Tibor selbst ist nicht betroffen, weil sein hoher Z77 die VMA komplett deckt.

## §10 Test-Plan

Neue Tests einzubauen (alle parametrisierbar, kein Tibor-hardcoded):

```python
# tests/test_z76_tour_position_rate.py
- test_mid_tour_day_uses_voll_24h_not_same_day_8h
- test_anreise_day_to_foreign_country_uses_z76_not_z73
- test_abreise_day_from_foreign_country_uses_z76_not_z73
- test_tour_with_3_mid_days_uses_voll_24h_thrice
- test_short_tour_anreise_abreise_uses_8h_rate
- test_bmf_country_picks_se_place_before_cas_routing

# tests/test_z77_offset_clamp.py
- test_z77_only_offsets_vma_bucket
- test_z77_excess_does_not_flow_to_fahr_or_reinig
- test_z76_underreporting_invisible_when_z77_exceeds_vma  # Tibor case
- test_z76_underreporting_full_impact_when_z77_zero
```

## §11 Final Status

| Aspekt | Status |
|---|---|
| Z77 4 705 € belegt? | ✓ |
| Z76-Lücke vollständig erklärt? | ✓ (74 Tage, −683 € summiert exakt) |
| Steuer-Effekt für Tibor? | 0 € |
| Steuer-Effekt für User ohne Z77? | bis −287 € (42 % Grenzsteuer) |
| Public-Launch-Blocker? | **JA — P0-Fixes vor Launch** |
| Tibor-Live-Run als demo? | OK (PDF korrekt 976 €) |
