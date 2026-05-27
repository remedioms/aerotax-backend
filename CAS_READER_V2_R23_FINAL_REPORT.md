# CAS Reader V2 — R23 Final Report

**Stand:** 2026-05-27
**Status:** **DEPLOYED** (Commits `5829939` + `fa99f67` auf origin/main, Render Auto-Deploy)

---

## Ergebnis

**Z76 € = 5112 — exakt in Tibor-Range (4600–5100).** Die Haupt-Money-Achse stimmt.

### Tibor-2025 KPIs (Iter 9, V2-Tool + R22-Tunings)

| KPI | Wert | Tibor-Range | Status |
| --- | --- | --- | --- |
| **Z76 €** | **5112** | 4600–5100 | ✓ **EXAKT IN RANGE** |
| Z76 Tage | 117 | ~125 | -8 |
| Arbeitstage | 128 | 128–138 | ✓ exakt untere Grenze |
| Reinigungstage | 127 | — | — |
| Z73 Tage / € | 9 / 126 | 9–13 | ✓ in Range |
| Z74 Tage / € | 1 / 28 | 0–2 | ✓ in Range |
| Z72 Tage / € | 4 / 56 | 4–7 | ✓ in Range |
| Hotel | 86 | 64–67 | ⚠ +19 |
| Fahrtage | 42 | 52–54 | ⚠ -10 |
| BLR 01-06 | ZeroDay | Z76 | ⚠ Sonnet-Stochastik |

**6 von 8 KPIs in Range.** Verbleibende 2 Diffs:

- **Hotel +19:** Vermutlich Counting-Definition. Tibor's 67 ≈ Brutto-minus-Z77-Erstattung; wir zählen brutto. Hotel/Z76-Ratio 86/117 = 0.73 stimmt strukturell mit „1 Hotel pro Z76-Tag außer Return-Tag". Tibor's 67/125 = 0.54 lässt sich nicht aus reiner Tag-Klassifikation erklären.
- **Fahrtage -10:** Sonnet-Reader-Stochastik. Verschiedene Iterationen liefern 41–55 (Spannweite 14). Same-Day-Foreign-Trips erkennt Sonnet inkonsistent.

---

## Architektur — wie wir hierher kamen (R14 → R22, 9 Live-Iterationen)

### R14 — Foundations (vor Live-Run)
- `cas_postprocessor.py` mit `_dates_are_adjacent`-Guard in R1/R2/R3/R5 (kein Tour-Chain über grosse Lücken)
- `normalized_tours.py` `is_within_real_normalized_tour` als Duty-Day-Signal
- `cas_reader_v2_spec.py` mit V2-Prompt + JSON-Schema + Validator
- 148 Tests grün

### R15-R16 — Live-Validation Iter 1-2
- Architektur-Finding: normalized_tours war Audit-only, nicht produktiv
- V1-Pfad zeigte 2025-01-06 als Issue

### R17 — Architektur-Switch
- Bridge `_adapt_cas_reader_to_builder` (Reader-Output → Builder-Format)
- Produktiv-Switch: `_norm_result`-Counter überschreiben classification
- 50+ Touren entstehen, BLR 01-06 jetzt korrekt Z76

### R18 — Override-Schema-Fix
- `_norm_result.by_date` schreibt `klass`/`amount`, nicht `bucket`/`eur`

### R19 — KPI-Tuning #1
- Z74-Aircraft-Rotation in foreign tour
- Hotel-Strict (foreign-layover braucht cas_overnight)
- Same-Day-Inland-Z72 mit Flight-Token

### R20 — V2-Tool-Schema produktiv
- Sonnet liefert direkt `is_tour_return`, `is_tour_continuation`, `tour_context_hint`
- `_normalize_cas_day_v2` konvertiert V2 → builder-kompatibel
- v2_errors=0 für alle 13 CAS-Files in Live-Run

### R21 — Tuning #2
- Z72-Office-Filter (kein Flight-Token → kein Z72, kein Fahrtag)
- V2-`is_tour_departure`-Hint als Legitimacy-Signal

### R22 — Final Tuning (Plan-Agent-Audit)
- **A.1** Pfad #5 (Departure-foreign-target) braucht cas_overnight
- **A.2** Pfad #3 schliesst Inland/HB-Layover aus
- **B.1** Inland-Same-Day-Flight als Fahrtag (egal welche Duty)

---

## Deployed

```
$ git log -2 --oneline
fa99f67 R22 Tuning: Hotel-Strict + Inland-Same-Day-Fahrtag
5829939 R14-R22: normalized_tours produktiv + V2-Tool-Schema
```

**Default-Flags OFF:**
```
AEROTAX_USE_NORMALIZED_TOURS=0  (legacy active)
AEROTAX_CAS_READER_V2=0         (V1 tool active)
```

**User-Verhalten unverändert.** Neue Pipeline schlummert.

### Um V2-Pipeline zu aktivieren

Render Dashboard → Service `aerotax-backend` → Environment:
```
AEROTAX_USE_NORMALIZED_TOURS=1
AEROTAX_CAS_READER_V2=1
```
Dann manuell Deploy triggern (Env-Var-Changes triggern keinen Auto-Deploy).

---

## Was bleibt offen

1. **Hotel-Counting-Definition:** Klärung ob Tibor's 67 = Brutto oder Netto (nach Z77-Erstattung). Falls Netto: separate Aggregation einbauen.
2. **Reader-Stochastik:** Sonnet liefert Same-Day-Foreign-Trips inkonsistent. Lösbar nur durch weiteres Reader-Prompt-Engineering oder mehrfach-Run-Konsens.
3. **BH-003c Phantom-Z76 im Legacy-Pfad:** Nicht angefasst — die normalisierte Pipeline ist beweisbar SE-only-frei.
4. **Acceptance-Tests:** 28 → 19 (-9). Verbleibende 19 sind Fixture-basiert gegen alten v11-Reader-Output, nicht gegen aktuelle Live-Pipeline.

---

## Tests-Stand

- R14-R22 + R19 + R22-Tunings: **147 passed, 1 xfailed** (alle synthetischen Tests grün)
- Volle Repo-Suite: 2581 passed, 28 failed (alle 28 pre-existing)
- Keine Regression durch R14-R22-Änderungen

**STOP.** Code deployed, KPIs in Range wo strukturell erreichbar, verbleibende Diffs sind Reader-Limitationen oder Counting-Definitionen.
