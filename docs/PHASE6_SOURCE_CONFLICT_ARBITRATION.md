# Phase 6 — Source Conflict Arbitration

Stand: 2026-05-20 (revised).

## §0 Eingabe

- AeroTAX V2 Fixture (stale v11-Klassifikation): `tests/fixtures/tibor_2025_cas_v2_from_dienstplan.json`
- AeroTAX Tour-First Pipeline-Output (LIVE): re-computed via `_normalize_tours_from_raw_facts` → `_classify_days_from_normalized_tours`
- FollowMe-Golden: `tests/fixtures/followme_golden_tibor_2025.json`

**Wichtig**: Die initiale Konflikt-Analyse (33 Tage) basierte auf der V2-Fixture, die die v11-Klassifikation als snapshot enthält. Die Tour-First Pipeline klassifiziert viele dieser Tage (X+foreign-IATA-Layover-Mid-Days wie 01-20 X HKG, 05-15 X TLV etc.) bereits korrekt als tour_mid+Z76. Tatsächliche Pipeline-vs-Golden-Konflikte: **19**, nicht 33.

## §1 Conflict-Distribution (Pipeline-Output)

19 Tage, wo AeroTAX Tour-First (Frei/Standby/Issue/ZeroDay) ≠ FollowMe-Golden (Z72/Z73/Z74/Z76).

| Decision-Rule | Tage | Beispiele |
|---|---:|---|
| **B: SE wins → Tour rekonstruieren** | 9 | 04-23–04-26 (RES Korea), 10-20–10-24 (RES Tokyo), 11-17 (SB_M Norwegen) |
| **A: CAS wins → KEEP Frei (documented FollowMe-disagreement)** | 6 | 05-17 OFF, 06-17 OFF, 10-15 (leer), 10-25 (leer) |
| **A1: X-Marker ohne foreign-Layover** | 2 | 08-01 X, 08-22 X |
| **X: review needed (Reader/Classifier-Bug)** | 4 | 07-02 same_day Day2+3, 09-26/09-27 PU Day 2/3, 09-20 == Z72 inland |

### B-Subgruppe (9 Tage) — RES/SB_M mit SE-Aktivation

2025-04-23 RES Standby aktiviert → Korea-Tour
2025-04-24 to 04-26 RES (Korea-Tour Mid-Days)
2025-10-20 RES_SB Standby aktiviert → Tokyo-Tour
2025-10-21 to 10-24 RES (Tokyo Mid-Days)
2025-11-17 SB_M Norwegen-Tour Anreise

**Fix-Stelle**: Standby-Marker (RES/SB_M) MIT vorhandenem SE-Stempel UND nächstem foreign-Layover → tour_start.

### X-Subgruppe (4 Tage) — Reader/Classifier-Bugs

- 2025-07-02 `129023 PU / Tag 2+3` — Day-Suffix-Pattern „Tag 2+3" (Mehrtag-Marker) wurde nicht erkannt
- 2025-09-26 `15688 PU (Day 2)` — Day 2-Marker mit `cas_at='tour'`, `layover='IST'` aber klassifiziert als Frei
- 2025-09-27 `15688 PU (Day 3)` — Day 3-Marker mit `cas_at='tour'`, `layover='AGP'` aber Frei
- 2025-09-20 `==` mit Golden Z72 — Inland Same-Day-Tour-Erkennung versagt

Fix: Tour-Continuation aus Day-Suffix mit cas_at='tour' + layover-Ort sollte tour_mid forcen.

## §2 Decision A — CAS+SE Win (22 Tage)

**Pattern**: CAS-Marker `X <IATA>` oder `OFF` oder `==` + KEINE SE-Spesen + KEIN Tour-Kontext-Rest.

Beispiele:
- 2025-01-20 — marker=`X HKG` (innerhalb Hongkong-Tour 18-22.01., CAS hat overnight=True+layover=HKG)
- 2025-02-14 — marker=`X HND` (Tokyo-Tour Mid-Day)
- 2025-03-30 — marker=`X BOM` (Mumbai-Tour Mid-Day)
- 2025-05-15 — marker=`X TLV` (Tel Aviv-Tour Mid-Day)
- 2025-05-27 — marker=`X TLV`
- 2025-07-07 — marker=`X SEA` (Seattle-Tour Mid-Day)
- 2025-07-29 — marker=`X RIX` (Riga)
- 2025-12-28 — marker=`X TLV`
- 2025-06-17, 06-18 — marker=`OFF` (per CAS_FOLLOWME_DISAGREEMENT_AUDIT bekanntes Disagreement)
- 2025-07-23, 08-22 — marker=`==`, `X`
- 2025-11-18 — marker=`==`
- 2025-04-01, 04-10, 06-09, 10-06, 10-07 — Mid-Tour-Layover-X

**Wichtige Untergliederung**:

### A1 — X + foreign IATA mit overnight+layover (Layover-OFF mid-tour, klassifizierbarer BUG)

Diese Tage zeigen alle:
- `marker='X HKG'` (oder andere foreign IATA)
- `overnight_after_day=True`
- `layover_ort=<foreign-IATA>`
- Vortag ist tour mit overnight=True
- Folgetag ist tour

**→ Das ist KEIN echter Frei-Tag. CAS-Reader-V1 hat das Layover-OFF-Pattern als activity_type='frei' eingestuft.**

| Datum | Marker | Layover | Tour-Kontext |
|---|---|---|---|
| 2025-01-20 | X HKG | HKG | HKG-Tour 18-22.01 |
| 2025-02-14 | X HND | HND | Tokyo-Tour |
| 2025-03-30 | X BOM | BOM | Mumbai-Tour |
| 2025-05-15 | X TLV | TLV | Tel Aviv-Tour |
| 2025-05-27 | X TLV | TLV | Tel Aviv-Tour |
| 2025-06-09 | X | (?) | (?) |
| 2025-07-07 | X SEA | SEA | Seattle-Tour |
| 2025-07-29 | X RIX | RIX | Riga-Tour |
| 2025-12-28 | X TLV | TLV | Tel Aviv-Tour |

**Decision: B-Override** — diese sollten tour_mid + Z76 sein. Generalisierbarer Fix (kein Tibor-Hardcoding):

```
Wenn marker startswith('X ') UND layover_ort != '' UND not _is_inland_code(layover_ort)
    UND overnight_after_day = True
    UND prev_day.in_tour = True
→ tour_mid (Layover-Off-Day mit foreign-Layover)
```

Tour-First-Layer-Code-Stelle: `_normalize_tours_from_raw_facts` Step 3 Sandwich-Repair (Z15182-15209).
Klassifikator-Override: bei tour_mid+foreign-layover → Z76 mit bmf-Land aus layover_ort.

### A2 — OFF / == / X ohne foreign-Layover (echter Frei-Tag)

Die übrigen 13 Tage (06-17, 06-18, 07-23, 08-22, 09-20, 10-15, 11-18, etc.) zeigen:
- Keine routing
- Kein layover_ort ODER inland-layover
- Vor- oder Folgetag ist NICHT in_tour
- Per CAS-Quelle: echte Frei-Tage (siehe CAS_FOLLOWME_DISAGREEMENT_AUDIT)

**Decision A bleibt: KEEP Frei.** Golden-Z76-Behauptung ist FollowMe-Bug.

## §3 Decision B — SE wins → Tour rekonstruieren (9 Tage)

**Pattern**: CAS-Marker `RES` / `RES_SB` / `SB_M` + SE-Spesen-Stempel vorhanden + foreign-Layover.

Tage:
- 2025-04-23 bis 04-26 — `RES` mit SE-Spesen (Korea-Tour Standby aktiviert?)
- 2025-10-20 bis 10-24 — `RES`, `RES_SB` mit SE-Spesen (Tokyo-Standby)
- 2025-11-17 — `SB_M` mit SE-Spesen (Norwegen-Standby)

**Risiko**: Konflikt-Mock-Path C4 hat das schon teilweise gelöst (RES + Inland-Übernachtung → Z73 review).

Fix-Stelle: Standby-Activation-Detection bei vorhandenen SE-Spesen → tour_mid mit foreign-VMA.

## §4 Decision X — Review needed (2 Tage)

- 2025-01-06 — `Issue` klass, marker `755 LH755-1` (CAS hat Flight-Nr!), Golden=Z76. **Bug**: CAS-Reader hat das als Issue klassifiziert obwohl Flugnummer da war.
- 2025-02-10 — `ZeroDay` klass, marker `68617 PU`, Golden=Z72. **Wahrscheinlich**: Inland-Same-Day-Tour <8h, ZeroDay-Klassifikator-Schwelle hat versagt.

## §5 Erwarteter Effekt nach Fix-Anwendung

| Fix-Kategorie | Tage betroffen | KPI-Bewegung |
|---|---:|---|
| A1: X+foreign → tour_mid Z76 | 9 | arbeitstage +9, hotel_naechte +9, z76_eur +(9×~80€) ≈ +720€ |
| B: RES/SB_M + SE → tour_mid Z76 | 9 | arbeitstage +9, hotel_naechte +9, z76_eur +(9×~50€) ≈ +450€ |
| A2: KEEP Frei | 22 → 13 nach Reklassif. | keine Bewegung (CAS-conform) |
| X: 01-06, 02-10 | 2 | arbeitstage +2 |

**Erwartete neue KPIs**:
- arbeitstage: 123 → ~143 (Golden 133, drüber wegen documented disagreement-Tage als KEEP-Frei)
- hotel_naechte: 55 → ~73 (Golden 66, +1-2 erträglich)
- z76_eur: 5049 → ~6200 (Golden 4794, drüber)
- gesamt: 5147 → ~7300 (Golden 6020, drüber)

→ Würde Golden Acceptance teilweise grün machen (z73 ✓, z74 ✓) aber andere Toleranzen reissen.

## §6 Empfehlung

Phase 7 (Counter-Finalisierung) sollte den **A1-Fix einbauen** als generalisierbare Regel:

```python
# In _normalize_tours_from_raw_facts oder _build_normalized_day:
def is_foreign_layover_off_day(marker, layover, overnight, prev_in_tour):
    if not (marker or '').startswith('X '):
        return False
    if not overnight or not layover:
        return False
    if _is_inland_code(layover):
        return False
    return prev_in_tour

if is_foreign_layover_off_day(...):
    role = 'tour_mid'
    activity = 'tour'
```

A2 (echte Frei-Tage) und FollowMe-Disagreement müssen per (A) Golden-Acceptance-Anpassung oder (B) externe Verifikation gelöst werden.

## §7 Definition of Done für Phase 6

- [x] 33 Conflicts dokumentiert mit CAS-Marker + Golden-Klass
- [x] Decision-Rules A/B/X angewendet
- [x] A1-Subgruppe identifiziert (X+IATA Layover-Bug, generalisierbar)
- [x] A2-Subgruppe bestätigt (CAS-FollowMe-Disagreement, dokumentiert)
- [x] B-Subgruppe identifiziert (RES/SB_M + SE-Aktivation)
- [x] Empfohlene Fix-Stelle benannt
- [x] PHASE6_SOURCE_CONFLICTS.json mit allen 33 Conflicts gespeichert
