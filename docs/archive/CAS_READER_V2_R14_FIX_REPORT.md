# CAS Reader V2 — R14 Fix Report

**Stand:** 2026-05-26
**Vorhergehender Report:** `CAS_READER_V2_R13_DECISION.md` (Status NEEDS_FIX)
**Status nach R14:** siehe Abschnitt „Entscheidung" unten.
**Hard constraints respected:** Kein Deploy, kein Default-Switch, kein Live-Run, kein Tibor-/FollowMe-Hardcoding, keine SE-only-Touren, keine SE-only-Z76, keine neuen Rescue-Heuristiken.

---

## 1. Was wurde geändert

### Aufgabe 1 — R3 Date-Adjacency
**Datei:** `cas_postprocessor.py`

- Neuer Helper `_dates_are_adjacent(prev_date, current_date, max_gap_days=1)` plus `_parse_iso_date`.
- R1 (X-Return-Healing), R2 (Empty-Marker-Continuation), R3 (ends_hb-Conflict), R5 (Return-from-Layover) verketten Tage **nur noch**, wenn `prev_date + 1 day == current_date`.
- Audit-Spur:
  - R1: `R1 chain skipped: non-adjacent dates {prev_ds} -> {cur_ds}` als warning.
  - R3: `R3 chain skipped: non-adjacent dates {cur_ds} -> {nxt_ds}` als warning.
- Großzügige Lücken über sortierte Listen werden nicht mehr automatisch verkettet.

### Aufgabe 2 — `is_real_duty_day` für echte Tour-Continuation
**Datei:** `normalized_tours.py`

- Neue Bedingung `is_within_real_normalized_tour`:
  - Tour hat foreign-signal ODER overnight (kein Office-only-Tour-Cluster).
  - Tag ist `is_departure_day | is_return_day | is_full_away_day`.
  - Tag ist **kein** `is_home_standby`, **kein** `is_free`.
- `is_real_duty_day` schließt diese Bedingung als zusätzliches Duty-Signal ein.
- Layover-Free-Day bekommt arbeitstag, aber **kein** reinigungstag (im Hotel — kein Hausputz).
- Phantom-Touren werden durch den bestehenden `_flush_tour`-Evidence-Check schon im Builder weggefiltert; Aufgabe 2 baut darauf auf, ohne ihn aufzuweichen.

### Aufgabe 3 — Reader V2 unter Feature-Flag verkabeln
**Datei:** `app.py:12087` (`_sonnet_read_cas_single_pdf`)

- Import `cas_reader_v2_spec.V2_PROMPT_INSTRUCTIONS` + `is_v2_enabled` + `validate_cas_reader_v2_response`.
- Wenn `AEROTAX_CAS_READER_V2=1`:
  - V2-Prompt-Instructions werden an den existing slim-Prompt **angehängt** (additiv, ersetzt nichts).
  - `result['_v2_active']=True`, `result['_v2_prompt_appended']=True` als Audit-Felder.
  - Bei Import-Fehler des Spec-Moduls: stillschweigend mit V1 weiter + Log-Eintrag (Production-Pipeline darf nicht crashen).
- Wenn Flag aus: bestehender Pfad **unverändert**.
- Neue Helper-Funktion `_validate_cas_v2_postprocessed_response(post_days)`:
  - Wird vom Caller nach `normalize_cas_days_v2` aufgerufen.
  - Validiert die postprocessor-output-days gegen V2-Schema.
  - Errors → laut loggen, **kein** silent accept; Audit-Dict zurückgegeben.
- Default bleibt alter Reader.

### Aufgabe 4 — R12 erweitert um Z73 / Z74
**Datei:** `tests/test_reader_v2_mocked_snapshot.py`

- Neue synthetische Inland-Helpers `_inland_dep_to_muc`, `_inland_mid_muc`, `_inland_ret_from_muc`.
- 5 neue Tests:
  - `test_inland_overnight_short_tour_triggers_z73_not_z76` — 2-Tage-Inland → 2× Z73, kein Z76.
  - `test_inland_3day_tour_triggers_z74_for_mid_day` — 3-Tage-Inland → 2× Z73 + 1× Z74.
  - `test_same_day_inland_only_triggers_z72_not_z73_or_z74` — Same-Day → nur Z72.
  - `test_inland_hotel_does_not_count_as_foreign_z76` — Verbot: Inland-Hotel zählt nicht als Ausland.
  - `test_z73_z74_audit_print` — KPI-Audit-Print für 4-Tage-Inland.

---

## 2. Neue / geänderte Dateien

| Datei | Art |
| --- | --- |
| `cas_postprocessor.py` | geändert (Helper + Adjacency-Guards in R1/R2/R3/R5) |
| `normalized_tours.py` | geändert (is_within_real_normalized_tour-Bedingung in `is_real_duty_day`) |
| `app.py` | geändert (V2-Prompt-Verkabelung + `_validate_cas_v2_postprocessed_response`) |
| `tests/test_r14_adjacency_and_duty_day.py` | neu — 16 Tests |
| `tests/test_r14_reader_v2_wire.py` | neu — 9 Tests |
| `tests/test_reader_v2_mocked_snapshot.py` | erweitert — 18 Tests (vorher 13) |
| `AEROTAX_FIX_STATE.md` | neu — Session-State (Verbindlichkeit für Folge-Sessions) |
| `CAS_READER_V2_R14_FIX_REPORT.md` | dieser Report |

---

## 3. Tests

### Neue Tests
- `tests/test_r14_adjacency_and_duty_day.py` — **16 / 16 grün**
  - 7 Tests für `_dates_are_adjacent`-Helper
  - 3 Tests für R3-/R1-/R2-Adjacency-Guard
  - 6 Tests für `is_real_duty_day` (Mid-Tour-X innerhalb, isoliertes X, Home-Standby, SE-only, Layover-Rest-Reinigung, zwei getrennte Touren ohne Buffer)
- `tests/test_r14_reader_v2_wire.py` — **9 / 9 grün**
  - 3 Tests: Flag off → kein V2-Prompt, Flag on → V2-Prompt im Sonnet-Call, Legacy-Prompt bleibt intakt
  - 5 Tests: Validator-Hook off/on/valid/invalid/empty
  - 1 Test: Default ist off
- `tests/test_reader_v2_mocked_snapshot.py` — **18 / 18 grün** (vorher 13)
  - 5 neue Inland-Tests (Z73 + Z74 + No-Z76-Spillover)

### Volle V2/R14-Regression
```
$ pytest tests/test_cas_reader_v2_spec.py tests/test_reader_v2_mocked_snapshot.py \
        tests/test_r14_adjacency_and_duty_day.py tests/test_r14_reader_v2_wire.py \
        tests/test_cas_postprocessor_v2.py tests/test_b12_b13_b14_fixes.py \
        tests/test_b7_b8_b9_fixes.py tests/test_tibor_parallel_audit.py -q
148 passed, 2 xfailed in 0.93s
```

(2 xfailed sind pre-existing dokumentierte Disagreements, kein neuer Regress.)

---

## 4. Mock-KPI Vorher / Nachher

R12-Mock (synthetischer 12-Monats-Snapshot, kein Tibor):

| KPI | Vor R14 | Nach R14 | Bemerkung |
| --- | --- | --- | --- |
| tour_count       |  6 |  6 | unverändert |
| fahrtage         |  5 |  5 | unverändert |
| arbeitstage      | 11 | **17** | Mid-Tour-Continuation jetzt korrekt gezählt |
| hotel_naechte    | 11 | 11 | unverändert |
| reinigungstage   | 11 | 11 | unverändert (Layover-Rest weiterhin nicht in Reinigung) |
| z72_tage / €     | 1 / 14 | 1 / 14 | unverändert |
| z73_tage / €     | 0 / 0  | 0 / 0  | im Auslands-Mock erwartet 0 |
| z74_tage / €     | 0 / 0  | 0 / 0  | im Auslands-Mock erwartet 0 |
| z76_tage / €     | 16 / 834 | 16 / 834 | unverändert |
| total_vma_eur    | 848 | 848 | unverändert |

Zusätzliche Inland-KPIs (neue Z73/Z74-Tests, 4-Tage-Inland MUC):
```
R14_INLAND_KPIS = {
  inland_z73_tage: 2, inland_z73_eur: 28.00,
  inland_z74_tage: 2, inland_z74_eur: 56.00,
  foreign_z76_tage: 0, foreign_z76_eur: 0.0,
}
```

---

## 5. Free-Day-Buffer im Mock — noch nötig?

Vor R14: ja, sonst chained R3 die Tours über Wochen.
Nach R14: nein — `test_two_separate_tours_no_chain_across_gap` beweist, dass zwei Touren mit 5-Wochen-Lücke ohne Buffer korrekt getrennt bleiben. Der Buffer im R12-Mock ist defensiv, nicht notwendig.

---

## 6. arbeitstage realistischer?

Ja. Im 12-Monats-Mock:
- 5 foreign-Touren mit 4+3+4+2+3 Tagen = 16 Tour-Tage + 1 Same-Day-Inland = 17 arbeitstage.
- Vor R14: 11 arbeitstage (Mid-Tour-X fiel raus).
- Nach R14: 17 arbeitstage — entspricht der Erwartung.

Für Tibors Acceptance-Range (128–138) wird der reale Reader-Output noch entscheidend bleiben: V2-Prompt soll Sonnet dazu bringen, Mid-Tour-X korrekt zu lesen und `is_tour_continuation`-Hint zu setzen. Im Mock funktioniert das jetzt; ein echter Live-Run muss das in Production-Daten bestätigen.

---

## 7. Feature-Flag funktioniert?

Ja, verifiziert durch Mocks (kein Sonnet-Call):

- **Flag off (Default):** Prompt enthält weder `CAS READER V2` noch `REGEL 1`. `result['_v2_active'] = False`. Legacy-Pfad unverändert.
- **Flag on (`AEROTAX_CAS_READER_V2=1`):** V2-Instructions im Sonnet-Prompt. `result['_v2_active'] = True`. Validator-Hook ist aktiv.
- **Validator:** Ungültige V2-Days produzieren Errors + Log-Ausgabe `[CAS-Reader-V2-Validator] errors=...`. Kein silent accept.
- **Fallback bei Spec-Import-Fehler:** Production-Pipeline läuft weiter mit V1, einzelner Log-Eintrag, kein Crash.

---

## 8. Was weiterhin NICHT gemacht wurde

- Kein Deploy zu Cloud Run / Render.
- Kein Default-Switch des Flags.
- Kein echter Live-Run gegen Tibor-Daten.
- Kein V2-Tool-Schema im Sonnet-Aufruf — der Tool-Output bleibt V1-shaped, V2-Felder kommen via Postprocessor. (Bewusste Entscheidung: minimaler Wire-Eingriff, der V2-Prompt-Influenz erlaubt ohne Schema-Migration.)
- Keine selektive Re-Read-Pipeline für `pending_reread`.
- BH-003c wurde **nicht** weiter angefasst — sein Phantom-Z76-Risiko ist abhängig vom CAS-Klassifikator-Pfad in `app.py`, nicht von der normalisierten Pipeline. Tests beweisen: SE-only erzeugt **keine** Tour, **keinen** Z76, **keine** Hotelnacht (`test_se_only_does_not_create_tour_or_hotel`). Falls BH-003c trotzdem Phantome erzeugt, ist das ein separater Bug im Legacy-Pfad und braucht eigenes Ticket.

---

## 9. Akzeptanzkriterien (Check)

| # | Kriterium | Status |
| --- | --- | --- |
| 1 | normalized_tours Grundlage für Z72/Z73/Z74/Z76 | erfüllt |
| 2 | Keine Tour-Verkettung über nicht-angrenzende Tage | **erfüllt + getestet** |
| 3 | SE-only erzeugt keine Tour/Z76/Hotelnacht | **erfüllt + getestet** |
| 4 | BH-003c rettet nur innerhalb bestehender Tour | nicht angefasst, separates Ticket; Tests beweisen Pipeline-seitig No-SE-Phantom |
| 5 | Mid-Tour-X innerhalb echter Tour zählt als AT/RT | **erfüllt + getestet** |
| 6 | Isoliertes X außerhalb Tour zählt nicht | **erfüllt + getestet** |
| 7 | Home-Standby zählt nicht | **erfüllt + getestet** |
| 8 | Hotelnächte nur bei echten FL-Layovern | erfüllt (bestehende B14-Fallbacks unverändert) |
| 9 | Fahrtage nur bei echten Tourstarts | erfüllt (B9-Filter unverändert) |
| 10 | Reader V2 nur unter Feature-Flag verkabelt | **erfüllt + getestet** |
| 11 | Default bleibt alter Reader | **erfüllt + getestet** |
| 12 | Z72/Z73/Z74/Z76 in Tests abgedeckt | **erfüllt** (R12 erweitert) |
| 13 | Alle relevanten Tests grün | **erfüllt** (148 passed, 2 xfailed pre-existing) |
| 14 | R14-Report geschrieben | dieser Report |
| 15 | Ehrlicher Status | siehe Entscheidung |

---

## 10. Verbleibende Risiken

1. **Phantom-Z76 außerhalb der normalisierten Pipeline (`app.py` Legacy-Pfad, BH-003c):** Wenn die Hauptklassifikation noch über den alten Pfad läuft, kann SE-only-Z76 dort entstehen. Die normalisierte Pipeline ist sauber, aber sie ist nicht zwingend der einzige Pfad in `_berechne_via_hybrid`. Vor Live-Validation: separat auditieren, ob `AEROTAX_USE_NORMALIZED_TOURS` durchgehend wirkt.
2. **V1-Tool-Schema bleibt aktiv:** Wenn Sonnet trotz V2-Prompt-Instructions die Mid-Tour-X-Tage falsch liefert, fängt es der Postprocessor — aber nur deterministisch. Forensik-Audit bei einem ersten Live-Run nötig.
3. **Mock kann Tibors KPI-Range nicht direkt prüfen.** No-Hardcoding-Regel verbietet das — daher bleiben die exakten Tibor-Zahlen ein offener Test, der erst beim Live-Run beantwortet wird.

---

## 11. Empfehlung / Entscheidungsstatus

**Status: READY_FOR_LIVE_VALIDATION**

Begründung:

| Akzeptanzpunkt | erfüllt |
| --- | --- |
| Pipeline-Bugs R3-Adjacency + is_real_duty_day | **ja**, mit Tests |
| Reader V2 verkabelt unter Flag (default off) | **ja**, mit Mock-Tests |
| Validator-Hook ohne silent accept | **ja**, mit Test |
| Z72/Z73/Z74/Z76 in Tests | **ja** |
| Volle Regression grün | **ja** (148 passed) |

Was der Live-Validation-Run leisten muss:

1. `AEROTAX_CAS_READER_V2=1` in einem **isolierten Staging-Slot** (nicht Production-Default!) gegen einen kontrollierten realen Dienstplan setzen.
2. KPI-Output gegen FollowMe-Referenz vergleichen.
3. `result['_v2_active']` muss True sein.
4. `_validate_cas_v2_postprocessed_response`-Output prüfen (errors-Liste leer oder verständlich).
5. Vergleich zu V1-Run im selben Job auf denselben Bytes → Diff dokumentieren.

Falls Live-Run gravierende Abweichungen zeigt: zurück zu NEEDS_FIX mit by-date-Befund. Sonst: Flag im Staging belassen, später Default-Switch erwägen.

**STOP nach Bericht** — kein Deploy, kein Default-Switch, kein automatischer Live-Run.
