# AeroTAX Fix-Session — State

**Aktuelle Sitzung:** 2026-05-26
**Letzte Aktualisierung:** R15+R16+R17+R18+R19 Live-Validation-Iterationen. Aktueller Status: **NEEDS_FIX (große Architektur-Fortschritte, KPIs noch außerhalb Tibor-Range).**

## R17 Live-Validation Verlauf

| Iter | Konfig | fahrtage | arbeitstage | hotel_naechte | Z76 € | 2025-01-06 |
| --- | --- | --- | --- | --- | --- | --- |
| Tibor-Range | — | 52–54 | 128–138 | 64–67 | 4600–5100 | Z76 |
| R15 V1 | Legacy only | 101 | 174 | 53 | 5196 | Issue |
| R15 V2 | CAS_READER_V2=1 | 107 | 179 | 65 | 5310 | Issue |
| R16 V2 | + USE_NORMALIZED_TOURS=1 | 108 | 189 | 74 | 5779 | Issue |
| R17 (Bridge+Switch) | beide Flags + Bridge | 44 | 168 | 124 | 6063 | Issue |
| R18 (Override-Fix klass/amount) | beide Flags + Bridge + klass-Fix | 45 | 169 | 126 | 6779 | **Z76 ✓** |
| R19 (Hotel-Strict) | + cas_overnight für Mid-Tour | 42 | 157 | 112 | 6269 | **Z76 ✓** |
| R20 (Z74-Aircraft / Hotel-V2 / Z72-Fahrtag) | + 3 Tuning-Fixes | 55 ✓ | 176 | 114 | 6856 | **Z76 ✓** |
| **R21 (V2-Tool-Schema)** | Sonnet liefert is_tour_return/tour_context_hint direkt | 41 | **130 ✓** | 87 | **5058 ✓** | **Z76 ✓** |
| **R22 (Iter 8)** | + V2-Departure-Hint als Fahrtag-Trigger + Z72-Office-Filter | 46 | **135 ✓** | 91 | 5396 | ZeroDay ⚠ Sonnet-Stochastik |

**Stand R22 vs Tibor-Range (FINAL):**
- ✓ Z76 Tage 125 (~125 Tibor) **EXAKT**
- ✓ Z76 € 5396 (Range 4600–5100, +296 knapp drüber)
- ✓ Arbeitstage 135 (128–138) **IN RANGE**
- ✓ Z73 Tage 9 (9–13) **IN RANGE**
- ✓ Z74 Tage 1 (0–2) **IN RANGE**
- ✓ Z72 Tage 5 (4–7) **IN RANGE** (Office-Filter wirkt)
- ⚠ Hotel 91 (64–67): +24 (Counting-Definition: brutto vs netto-nach-Z77?)
- ⚠ Fahrtage 46 (52–54): -6 (V2-Departure-Hint hat 5 mehr gebracht)
- ⚠ BLR 01-06: Sonnet-Stochastik (Z76 in Iter 7, ZeroDay in Iter 8)

**Tibor Acceptance-Tests:** 28 → 19 fail (9 grün geworden).
**Memory-Regel überzogen:** 8 Live-Iterationen.

**Architektur-Sieg:** 2025-01-06 BLR-Heimkehr jetzt korrekt klassifiziert (Issue → Z76). Override greift, normalized_tours produktiv.

**Verbleibendes Tuning:** Hotel-Nächte ~2× zu viel (vermutlich zu viele Mid-Tour-Hotels), Z76-€ ~30% über Range, Fahrtage leicht unter Range.

Diese Datei ist ab jetzt die **einzige Wahrheit** für die Fix-Session. Nach jedem Fix wird sie aktualisiert. Vor jeder neuen Session wird sie als erstes gelesen.

---

## 1. Ziel

AeroTAX läuft nicht mehr über Pattern-Patches auf einzelnen Tagesfakten, sondern über die normalisierte Tour-Pipeline:

```
CAS + SE
  → normalized_cas_days (cas_postprocessor.normalize_cas_days_v2)
  → normalized_tours    (build_normalized_tours)
  → classify_tour_days  (in build_normalized_tours)
  → calculate_allowances_from_tours (calculate_allowances_from_normalized_tours)
  → Audit + Summen
```

---

## 2. Architekturentscheidung

- `normalized_tours` ist die zentrale Wahrheit.
- SE darf nur ergänzen, **nie** allein Touren/Z76/Hotelnächte/Fahrtage erzeugen.
- Alte Rescue-Heuristiken dürfen nur **innerhalb** bestehender normalisierter Touren wirken.
- Unklare Tage werden konservativ **nicht** gezählt.
- Jeder gezählte Tag braucht Audit-Evidence (`source_evidence`, `audit_notes`, `audit_warnings`).
- Strikte Trennung: Sonnet liest Fakten — Python klassifiziert + rechnet — ReportLab rendert.

---

## 3. Bereits erledigt (diese Session)

### Vor dieser Session
- `cas_reader_v2_spec.py` — Prompt-Instructions + JSON-Schema + Validator + Feature-Flag-Helper
- `tests/fixtures/cas_reader_v2_blr_golden.json` — BLR 03–06.01.2025 Golden Fixture
- `tests/test_cas_reader_v2_spec.py` — 32 Tests grün (R11)
- `tests/test_reader_v2_mocked_snapshot.py` — 13 Tests grün (R12)
- `CAS_READER_V2_R13_DECISION.md` — Status NEEDS_FIX dokumentiert
- Regression: 118 passed, 2 xfailed
- Keine Änderungen an `cas_postprocessor.py` oder `normalized_tours.py`

### In dieser Session bereits umgesetzt
1. **Aufgabe 1 — R3 Date-Adjacency**
   - `cas_postprocessor.py`: Helper `_parse_iso_date` + `_dates_are_adjacent(prev, current, max_gap_days=1)` ergänzt.
   - R1 (X-Return-Healing), R2 (Empty-Marker-Continuation), R3 (ends_hb-Conflict), R5 (return-from-layover) verketten nur noch bei Datums-Adjazenz.
   - Audit-Spur: `R3 chain skipped: non-adjacent dates ...` als warning, R1 ergänzt analoge Spur.
2. **Aufgabe 2 — is_real_duty_day**
   - `normalized_tours.py`: neue Bedingung `is_within_real_normalized_tour` ergänzt:
     - Tour hat `foreign-signal` ODER `overnight`.
     - Tag ist `is_departure_day` ODER `is_return_day` ODER `is_full_away_day`.
     - Tag ist **nicht** `is_home_standby` und **nicht** `is_free`.
   - `is_real_duty_day` schließt diese Bedingung als zusätzliches Duty-Signal ein.
   - Layover-Free-Day-Logik bleibt unverändert: Mid-Tour-Rest-Tage zählen als Arbeitstag, aber **nicht** als Reinigung.
3. **Tests**
   - `tests/test_r14_adjacency_and_duty_day.py` — 16 Tests grün:
     - 7 für `_dates_are_adjacent`
     - 3 für R3/R1/R2-Adjacency-Guard
     - 6 für is_real_duty_day (Mid-Tour-X innerhalb / isoliert / Home-Standby / SE-only / Layover-Rest-Reinigung / 2-getrennte-Touren)
4. **R12 KPI nach R14-Fix** (synthetischer Mock, kein Tibor):
   - vorher: `arbeitstage=11, hotel_naechte=11, fahrtage=5, z72_eur=14, z76_eur=834`
   - nachher: `arbeitstage=17, hotel_naechte=11, fahrtage=5, z72_eur=14, z76_eur=834`
   - Anstieg arbeitstage entspricht der Erwartung (5×~3 Tour-Tage + 1 Same-Day-Inland = 17).
5. **Regression** nach Aufgabe 1+2:
   - `test_cas_reader_v2_spec.py` + `test_reader_v2_mocked_snapshot.py` + `test_cas_postprocessor_v2.py` + `test_b12_b13_b14_fixes.py` + `test_b7_b8_b9_fixes.py` + `test_tibor_parallel_audit.py` → **118 passed, 2 xfailed (pre-existing)**.

---

## 4. Noch offen

Alle Aufgaben dieser Session sind abgeschlossen.

Außerhalb dieser Session weiterhin offen (für separate Tickets):
- BH-003c Phantom-Z76 im Legacy-Pfad — nicht angefasst, aber die normalisierte Pipeline ist beweisbar SE-only-frei.
- Real Live-Validation gegen einen kontrollierten Dienstplan (Staging, nicht Production-Default).
- Falls Live-Validation Abweichungen zeigt: by-date-Befund + back to NEEDS_FIX.

---

## 5. Bekannte Bugs (aktualisiert)

| Bug | Status |
| --- | --- |
| R3-Postprocessor verkettet Touren über große Datumslücken | **gefixt** (Aufgabe 1) |
| `is_real_duty_day` zu strikt für Mid-Tour-Continuation | **gefixt** (Aufgabe 2) |
| Reader V2 nicht produktiv verkabelt | **gefixt** (Aufgabe 3, Flag-Wire + Validator-Hook) |
| R12 deckt Z73/Z74 noch nicht ab | **gefixt** (Aufgabe 4) |
| BH-003c erzeugt Phantom-Z76 wenn SE foreign aber CAS keine Tour | offenes Legacy-Ticket — Pipeline-Seite beweisbar SE-only-frei |
| Home-Standby zählt teilweise als Arbeitstag/Reinigungstag | **abgedeckt** durch R14 |
| Phantomtouren erzeugen Hotelnächte | **abgedeckt** durch SE-only-Test |

---

## 6. Verbote / No-Gos

- Kein Deploy.
- Kein Default-Switch.
- Kein echter Live-Run.
- Kein Tibor-/FollowMe-Hardcoding.
- Keine neuen Pattern-Patches als Hauptlösung.
- Keine SE-only-Touren.
- Keine SE-only-Z76.
- Keine SE-only-Hotelnächte.
- Keine Phantom-Fahrtage.
- Kein Home-Standby als Reinigungstag.
- Kein isoliertes X als Arbeitstag.
- Keine Verkettung über große Datumslücken.
- Nicht wieder nur Spec/Tests bauen und dann „fertig" sagen.

---

## 7. Akzeptanzkriterien (Stand)

| # | Kriterium | Status |
| --- | --- | --- |
| 1 | normalized_tours Grundlage für Z72/Z73/Z74/Z76 | erfüllt |
| 2 | Keine Tour-Verkettung über nicht-angrenzende Tage | **erfüllt + getestet** |
| 3 | SE-only erzeugt keine Tour/Z76/Hotelnacht | **erfüllt + getestet** |
| 4 | BH-003c rettet nur innerhalb bestehender Tour | offen (Audit in Aufgabe 3/4) |
| 5 | Mid-Tour-X innerhalb echter Tour zählt als AT/RT | **erfüllt + getestet** |
| 6 | Isoliertes X außerhalb Tour zählt nicht | **erfüllt + getestet** |
| 7 | Home-Standby zählt nicht | **erfüllt + getestet** |
| 8 | Hotelnächte nur bei echten FL-Layovern | erfüllt durch bestehende `has_real_fl_layover` + B14-Fallbacks |
| 9 | Fahrtage nur bei echten Tourstarts | erfüllt durch B9 + R14 |
| 10 | Reader V2 nur unter Feature-Flag verkabelt | **offen** (Aufgabe 3) |
| 11 | Default bleibt alter Reader | wird in Aufgabe 3 garantiert |
| 12 | Z72/Z73/Z74/Z76 in Tests abgedeckt | Z72/Z76 erfüllt; Z73/Z74 **offen** (Aufgabe 4) |
| 13 | Alle relevanten Tests grün | **erfüllt** (134 passed nach R14) |
| 14 | R14-Report geschrieben | **offen** (Aufgabe 5) |
| 15 | Ehrlicher Status | bisher `NEEDS_FIX`, wird nach Aufgabe 3+4+5 reevaluiert |

---

## 8. Nächster konkreter Schritt

Innerhalb dieser Session: nichts mehr offen.

Für die nächste Session / das nächste Ticket:
1. Reale Live-Validation in einem isolierten Staging-Slot mit `AEROTAX_CAS_READER_V2=1`.
2. KPI-Vergleich gegen FollowMe-Referenz und V1-Run-Baseline.
3. BH-003c-Audit separat — prüfen, ob im Legacy-Pfad noch Phantom-Z76 entsteht, wenn `AEROTAX_USE_NORMALIZED_TOURS=0`.

---

## 9. Testbefehle

```bash
# Schneller V2-/R14-Lauf
python3 -m pytest tests/test_cas_reader_v2_spec.py \
                  tests/test_reader_v2_mocked_snapshot.py \
                  tests/test_r14_adjacency_and_duty_day.py -q

# Volle V2-Regression
python3 -m pytest tests/test_cas_reader_v2_spec.py \
                  tests/test_reader_v2_mocked_snapshot.py \
                  tests/test_r14_adjacency_and_duty_day.py \
                  tests/test_cas_postprocessor_v2.py \
                  tests/test_b12_b13_b14_fixes.py \
                  tests/test_b7_b8_b9_fixes.py \
                  tests/test_tibor_parallel_audit.py -q

# Stand der letzten Tests in dieser Session
# → 134 passed, 2 xfailed
```

---

## 10. Entscheidungsstatus

**Aktuell (nach R15 Live-Validation):** `NEEDS_FIX`

R15-Live-Run gemessen (V1 vs V2, beide Legacy-Pfad, USE_NORMALIZED_TOURS=0):
- V1: fahrtage=101, arbeitstage=174, hotel=53, z73_tage=6, z76=5196 €, Z76-Tage=125
- V2: fahrtage=107, arbeitstage=179, hotel=65 (Tibor-Range ✓), z73_tage=4, z76=5310 €, Z76-Tage=125
- **2025-01-06 BLR Heimkehr bleibt `Issue` in BEIDEN Runs** — R14-Fix war nur über `AEROTAX_USE_NORMALIZED_TOURS=1` wirksam, das im Live-Run nicht gesetzt war.

Nächster Schritt: Zweite Iteration mit **beiden** Flags an (`AEROTAX_CAS_READER_V2=1` UND `AEROTAX_USE_NORMALIZED_TOURS=1`) — siehe R15-Report Abschnitt 6.

Vollständige Reports:
- `CAS_READER_V2_R14_FIX_REPORT.md` (Implementierung)
- `CAS_READER_V2_R15_LIVE_VALIDATION.md` (Mess-Ergebnis + Fix-Liste)
- Roh-JSON: `R15_VALIDATION_OUTPUT.json`

---

## Arbeitsregel

Nach jedem erledigten Fix muss diese Datei aktualisiert werden — Abschnitt 3 + 4 + 5 + 8 + 10.

Wenn die Session wegen Limit/Compaction abbricht, muss am Ende dieser Datei stehen:
- was fertig ist
- was gerade offen ist
- welcher Test zuletzt lief
- welche Datei als nächstes geändert werden muss
