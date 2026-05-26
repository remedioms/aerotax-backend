# CAS Reader V2 — R13 Decision Report

**Stand:** 2026-05-25
**Scope:** R7–R13 (Sonnet-Prompt + Schema + Validator + Golden Fixture + Mocked Snapshot)
**Hard constraints respected:** Kein Deploy, kein Live-Run, kein Default-Switch, kein Tibor-/FollowMe-Hardcoding.

---

## Was wurde gebaut

| ID  | Artefakt | Pfad |
| --- | --- | --- |
| R7+R8 | V2 Prompt-Instructions (10 Regeln, Tour-Cluster-aware) | `cas_reader_v2_spec.py` → `V2_PROMPT_INSTRUCTIONS` |
| R9  | JSON-Schema + Per-Day-Validator | `cas_reader_v2_spec.py` → `get_v2_json_schema()`, `validate_cas_reader_v2_day()` |
| R10 | BLR 03–06.01.2025 Golden Reader-Fixture | `tests/fixtures/cas_reader_v2_blr_golden.json` |
| R11 | Static prompt-/schema-/validator-Tests (No-Hardcoding-Audit) | `tests/test_cas_reader_v2_spec.py` — 32 Tests |
| R12 | Mocked Reader-V2 Snapshot durch normalized_tours | `tests/test_reader_v2_mocked_snapshot.py` — 13 Tests |
| Flag | Feature-Flag-Helper | `cas_reader_v2_spec.is_v2_enabled()` (`AEROTAX_CAS_READER_V2`) |

**Geändert in bestehenden Modulen:** keine. Postprocessor und `normalized_tours` blieben unangetastet (User-Auflage: nicht weiter „verbiegen").

---

## R11 — Prompt-Spec-Tests (32 / 32 passed)

| Cluster | Tests | Status |
| --- | --- | --- |
| Prompt-Mention je Regel (R1–R10) | 10 | passed |
| No-Hardcoding (Tibor, FollowMe, Date-Literals, €-Beträge) | 3 | passed |
| Schema-Struktur (required, enums, serializable) | 5 | passed |
| Validator-Verhalten (per day + response) | 9 | passed |
| Feature-Flag | 2 | passed |
| BLR Golden Fixture (Schema, X-Return, Mid-Tour) | 3 | passed |

Audit bestätigt: Prompt nennt explizit `X`/leerer Marker → `tour_return`, `ends_at_homebase`-Conflict, Briefing-/Departure-Time-Extraction, Destination-Propagation, `routing_iatas` vs `flight_numbers`, `tour_context_hint`, `unknown_tour_context` statt `free`-Default, `reader_should_not_classify_as_free_reason` + `neighbor_evidence`. Keine Date-Literals, kein Tibor, kein FollowMe, keine €-Beträge im Prompt.

---

## R12 — Mocked Reader-V2 Snapshot durch die Pipeline (13 / 13 passed)

Synthetischer 12-Monats-Mock (kein Tibor): 5 foreign Touren (BLR/HKG/NRT/LCA/BKK) inkl. BLR-X-Return-Pattern, 1 Same-Day-Inland, 3-Tage Home-Standby, 2-Tage Frei. SE-Rows leer (kein Z76-Inflate).

KPI-Output aus dem Snapshot-Test (`READER_V2_MOCKED_SNAPSHOT_KPIS`):

| KPI | Mock-V2 | Was es belegt |
| --- | --- | --- |
| `tour_count`    | 6 | 5 foreign + 1 Inland-Same-Day; Standby + Frei produzieren KEINE Tour |
| `fahrtage`      | 5 | 1 pro foreign Tour-Start (legitimate_tour_start) |
| `arbeitstage`   | 11 | Strikter `is_real_duty_day`: nur dep/ret + Inland-Same-Day |
| `hotel_naechte` | 11 | BLR 3 + HKG 2 + NRT 3 + LCA 1 + BKK 2 ≈ erwartete 11 |
| `reinigungstage` | 11 | ≤ arbeitstage |
| `z72_tage` / €  | 1 / 14 € | Same-Day-Inland > 8h |
| `z73_tage` / €  | 0 / 0 €  | Keine Inland-Übernachtungs-Tage im Mock |
| `z74_tage` / €  | 0 / 0 €  | Keine Voll-24h-Inland-Tage im Mock |
| `z76_tage` / €  | 16 / 834 € | Foreign-Layover-Tage mit synthetischen BMF-Rates |
| `total_vma_eur` | 848 € | konsistent |

**Direkter Read-Across (Behavior, nicht Beträge):**
- Mid-Tour-X-Tage werden als `tour_continuation` gebaut (BLR-Tour ist 4 Tage lang, nicht 2).
- Heimkehr-X mit foreign prev-layover wird `tour_return` (06.01 in der Tour).
- Home-Standby-Block erzeugt keine Phantom-Tour (Test `no_phantom_tours_from_standby`).
- Inland-Only-Pipeline → Z76 = 0 (Sanity-Test).
- Tour-Returns trennen sich von der Folgetour, sobald ein Free-Day-Buffer dazwischen liegt (anders verkettet R3 sie). Siehe **Funde** unten.

---

## KPI-Vergleich gegen Tibor-Acceptance (R13-Frage)

Die Tibor-2025 Acceptance-Ranges (`fahr_tage 52–54`, `hotel_naechte 64–67`, `Z76 4.600–5.100 €`, `total_vma 4.900–5.200 €`) sind **nicht** über den synthetischen Mock prüfbar — dafür müsste der Mock Tibor-spezifische Daten enthalten, was die No-Hardcoding-Regel verletzt.

Was der Mock **wohl** belegt:

| Aspekt | Mock-V2 Resultat | Aussage über Tibor-Acceptance |
| --- | --- | --- |
| foreign Z76 pro Tour-Tag | Funktioniert (834 € auf 5 Touren) | Pipeline kann Z76 produzieren, wenn Reader korrekt liefert |
| `fahrtage` Skaliert mit Tour-Anzahl | 5 Touren → 5 fahrtage | Tibors 52 fahrtage erfordern 52 Tour-Cluster im Reader-Output |
| Hotelnächte aus foreign overnights | 11 nights aus 11 overnight-flags | Skaliert linear mit korrekt gelesenen overnight-Tagen |
| Z72 Same-Day-Inland | korrekt klassifiziert | OK |
| Z73 Inland-Übernachtung | im Mock nicht getestet (kein RES+Inland-Tag) | Test-Lücke; deckt R12 nicht ab |
| Z74 Voll-24h-Inland | im Mock nicht getestet | Test-Lücke; deckt R12 nicht ab |

---

## Funde / offene Wahrheiten

1. **R3 (`ends_hb`-Conflict) ignoriert Datum-Abstand.** Wenn zwei Touren > 1 Tag auseinander liegen, klassifiziert R3 den Return-Tag der ersten Tour fälschlich zu Mid-Tour und kettet beide Touren zusammen. Der Mock musste mit Free-Day-Buffers zwischen Touren arbeiten. Das ist ein **Postprocessor-Bug**, kein Reader-Problem — in echten Tibor-Daten sind die Tage aber dicht, also tritt der Bug dort vermutlich nicht so dramatisch auf. Trotzdem dokumentations- und fix-würdig (eigenes Ticket, nicht im R7-R13-Scope).

2. **`arbeitstage` zählt strikter als Tour-Tage.** Mid-Tour-X mit `is_tour_continuation=true` aber `duty=0` / `has_fl=false` wird in `is_real_duty_day` nicht als arbeitstag gewertet. Wenn die Tibor-Acceptance `arbeitstage 128–138` fordert und der V2-Reader Mid-Tour-X korrekt aber ohne Duty-Minuten liefert, würde die Pipeline aktuell unterzählen. Lösung: Reader-V2 muss auch für Mid-Tour-Continuation-Tage einen sinnvollen `duty_duration_minutes`-Wert liefern (mindestens 480 für Standby-Tag im Hotel) ODER `is_real_duty_day` muss tour_continuation-Hint allein als Arbeitstag akzeptieren.

3. **`reinigungstage == arbeitstage`** im Mock. Die aktuelle Logik gibt jedem arbeitstag einen Reinigungstag — kein Filter „Reinigung nur an Heimkehrtag". Ob Tibors 153 → 133 dazu passen, ist nicht aus dem Mock prüfbar.

4. **Z73/Z74 sind im Mock nicht abgedeckt.** Eine R12-Erweiterung mit RES+Inland-Übernachtung und Voll-24h-Inland-Tag wäre nötig, um die Tibor-Z73-Range (9–13 Tage) und Z74-Range (0–2 Tage) belastbar zu mocken.

---

## Entscheidung

### Status: **NEEDS_FIX**

Begründung (knapp, ehrlich):

| Voraussetzung | erfüllt? |
| --- | --- |
| V2-Prompt-Spec vollständig, getestet, no-hardcoding | **Ja** (R11) |
| V2 Reader → normalized_tours → Z76 fließt | **Ja, prinzipiell** (R12: 834 € auf 5 Mock-Touren) |
| Mock-Output reproduziert Tibor-KPI-Range | **Nicht beweisbar** (kein Tibor-Mock nach R10/R11/R12-Constraints) |
| Pipeline-Bugs außerhalb Reader bekannt? | **Ja, zwei** (R3-Date-Adjacency, arbeitstage-Strenge) |

Damit ein Live-Validation-Run sinnvoll ist, müssen zuerst gefixt werden:

1. **Postprocessor R3:** date-adjacency requirement — Return-Tag darf nur dann „in die nächste Tour gekippt" werden, wenn der Folgetag ≤ 1 Kalendertag entfernt ist. Eigener Fix-Ticket.
2. **`is_real_duty_day` vs `is_tour_continuation`:** Mid-Tour-Continuation-Tage müssen als arbeitstage zählen (entweder Reader liefert duty_min ≥ 240 für jeden Mid-Tour-Tag, oder `is_real_duty_day` akzeptiert `cas_raw.is_tour_continuation=True` als duty-Signal). Entscheidung Reader-side vs Calculator-side: **Reader-side** (V2-Prompt-Regel ergänzen: „Wenn `tour_continuation`, setze `duty_minutes` auf das Standby-Maß ≥ 480, weil Crew im Hotel verfügbar ist").
3. **R12 erweitern** um Z73/Z74-Pattern, sobald die obigen zwei Fixes drin sind, um die Tibor-Range belastbar zu mocken.

**Empfehlung:** Erst die zwei oben genannten Pipeline-Fixes, dann R12 erweitern, dann erst Live-Run. Sonst riskieren wir, einen teuren Live-Run zu zünden, dessen Output wir bereits jetzt als „arbeitstage zu niedrig, Z76 plausibel" vorhersagen können.

---

## Testlauf-Beleg

```
$ pytest tests/test_cas_reader_v2_spec.py tests/test_reader_v2_mocked_snapshot.py \
        tests/test_cas_postprocessor_v2.py tests/test_b12_b13_b14_fixes.py \
        tests/test_b7_b8_b9_fixes.py tests/test_tibor_parallel_audit.py -q
118 passed, 2 xfailed in 0.83s
```

KPI-Linie aus R12:
```
READER_V2_MOCKED_SNAPSHOT_KPIS={"tour_count": 6, "fahrtage": 5, "arbeitstage": 11,
 "hotel_naechte": 11, "reinigungstage": 11, "z72_tage": 1, "z72_eur": 14.0,
 "z73_tage": 0, "z73_eur": 0.0, "z74_tage": 0, "z74_eur": 0.0,
 "z76_tage": 16, "z76_eur": 834.0, "total_vma_eur": 848.0}
```

**STOP nach Bericht** (kein Deploy, kein Default-Switch, kein Live-Run).
