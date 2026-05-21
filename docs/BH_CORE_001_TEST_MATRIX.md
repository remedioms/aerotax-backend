# BH-CORE-001 — Test Matrix

Stand: 2026-05-19. **Phase 0 — RED-Tests vorbereitet, Produktiv-Code folgt erst nach Spec-Freigabe.**

---

## §1 Test-Files (RED-Tests in Phase 0)

| Datei | Tests | Status (Phase 0) | Zweck |
|---|---:|---|---|
| `tests/test_tibor_2025_golden_acceptance.py` | 8 | **RED** (fehlende Funktion `_normalize_tours_from_raw_facts`) | Master-Gate für BH-CORE-001 |
| `tests/test_normalized_tours_bangalore.py` | 6 | RED | Case 1: Tour-Boundary mit X-Marker |
| `tests/test_normalized_tours_res_standby.py` | 5 | RED | Case 2: RES in foreign tour vs RES zuhause |
| `tests/test_normalized_tours_x_off_markers.py` | 7 | RED | Case 3/4/5: X/==/OFF-Marker |
| `tests/test_phase1_se_override_guard.py` | 6 | RED | Case 9: SE-Override Guard tightening |

**Total neue Tests in Phase 0:** 32, alle RED — sie schlagen mit AttributeError/ImportError fehl, weil `_normalize_tours_from_raw_facts` und `_classify_days_from_normalized_tours` nicht existieren.

---

## §2 Bestehende Tests (müssen grün bleiben)

| Test-Suite | Tests | aktueller Status |
|---|---:|---|
| Backend Pytest (1358 tests) | 1358 | **GRÜN** |
| Frontend test_normalize_state.mjs | 12 | grün |
| Frontend test_state_machine.mjs | 19 | grün |
| BH-001 Review-Question Semantik | 9 | grün |
| BH-003a Issue-Heimkehr | 12 | grün |
| Phase 4 Layover-Place | 11 | grün |
| Phase 5 Review-Schema | 7 | grün |
| Phase 6 Marker-Gruppierung | 9 | grün |
| Phase 7 SE-Inland-Audit | 4 | grün |

**Regression-Garantie:** Phase 1 muss `AEROTAX_TOUR_FIRST_CLASSIFIER=0` (default off) so implementieren, dass keiner der 1358 grünen Tests rot wird.

---

## §3 Acceptance-Test im Detail: `test_tibor_2025_golden_acceptance.py`

### Test-Struktur

```python
import json
import pytest
import app as app_module

FIXTURE = 'tests/fixtures/tibor_aerotax_v11_raw_initial.json'
GOLDEN  = 'tests/fixtures/followme_golden_tibor_2025.json'


@pytest.fixture(scope='module')
def normalized_tibor():
    """Builds normalized_tours from Tibor raw fixture.
    REQUIRES: _normalize_tours_from_raw_facts (BH-CORE-001 Layer 1).
    """
    raw_days = json.load(open(FIXTURE))
    matched = _build_matched_from_raw(raw_days)  # helper to be built
    tours = app_module._normalize_tours_from_raw_facts(
        matched, homebase='FRA', year=2025
    )
    return tours


@pytest.fixture(scope='module')
def aerotax_result(normalized_tibor):
    """Full Tour-First-Pipeline output."""
    matched = ...
    result = app_module._classify_days_from_normalized_tours(
        normalized_tibor, year=2025, homebase='FRA'
    )
    return result


@pytest.fixture(scope='module')
def golden():
    return json.load(open(GOLDEN))


# ─── Test 1: Totals Tolerance ─────────────────────────────────

def test_tibor_golden_totals_within_tolerance(aerotax_result, golden):
    soll = golden['soll_summary']
    assert abs(aerotax_result['arbeitstage']      - 133)    <= 2
    assert abs(aerotax_result['reinigungstage']   - 133)    <= 2
    assert abs(aerotax_result['hotel_naechte']    - 66)     <= 2
    assert abs(aerotax_result['fahr_tage']        - 58)     <= 2
    assert abs(aerotax_result['z72_tage']         - 5)      <= 1
    assert abs(aerotax_result['z73_tage']         - 11)     <= 1
    assert abs(aerotax_result['z74_tage']         - 1)      <= 1
    assert abs(aerotax_result['z76_eur']          - 4794.0) <= 150
    # Gesamt-Toleranz inkludiert Z77/AG-Z17-Anteile
    assert abs(aerotax_result['gesamt']           - 6020.72) <= 150


# ─── Test 2: Bangalore-Tour 01-03 bis 01-06 ───────────────────

def test_tibor_bangalore_tour_0103_0106(aerotax_result, golden):
    days = {t['datum']: t for t in aerotax_result['tage_detail']}
    # 4 Tage → 1 Tour (nicht gesplittet)
    assert days['2025-01-03']['tour_id'] == days['2025-01-06']['tour_id']
    assert days['2025-01-04']['tour_id'] == days['2025-01-06']['tour_id']
    # 01-04 nicht Frei
    assert days['2025-01-04']['klass'] in ('Z76', 'Z73')
    # 01-06 Z76 Abreise
    assert days['2025-01-06']['klass'] == 'Z76'


# ─── Test 3: RES Korea-Tour 04-23 bis 04-26 ───────────────────

def test_tibor_res_korea_tour(aerotax_result):
    days = {t['datum']: t for t in aerotax_result['tage_detail']}
    # Keine 4 Standby-zuhause-Tage
    for d in ['2025-04-23', '2025-04-24', '2025-04-25', '2025-04-26']:
        assert days[d]['klass'] != 'Standby', f'{d} darf nicht Standby sein'
        assert days[d]['tour_id'] is not None
    # Mid-Tour-Tage Z76
    for d in ['2025-04-24', '2025-04-25']:
        assert days[d]['klass'] == 'Z76'


# ─── Test 4: X-Marker innerhalb foreign tour ──────────────────

@pytest.mark.parametrize('datum,expected_klass', [
    ('2025-01-04', 'Z76'),
    ('2025-01-20', 'Z76'),
    ('2025-02-14', 'Z76'),
    ('2025-03-30', 'Z76'),
    ('2025-04-10', 'Z76'),
    ('2025-05-15', 'Z76'),
    ('2025-05-27', 'Z76'),
    ('2025-06-09', 'Z76'),
])
def test_tibor_x_marker_inside_foreign_tour(aerotax_result, datum, expected_klass):
    days = {t['datum']: t for t in aerotax_result['tage_detail']}
    assert days[datum]['klass'] == expected_klass, (
        f'{datum} X-Marker im foreign-tour-Kontext muss {expected_klass} sein, '
        f'war {days[datum]["klass"]}'
    )


# ─── Test 5: SE-Override nicht zu breit ───────────────────────

def test_tibor_phase1_se_override_not_too_broad(aerotax_result):
    # Synthetisch: SE-Inland-Stamp ohne Tour-Evidence → KEIN Z76-Rescue
    # (geprüft via Mock in test_phase1_se_override_guard.py;
    #  hier nur Live-Daten-Cross-Check)
    days = {t['datum']: t for t in aerotax_result['tage_detail']}
    # 2025-09-27 Beispiel: SE-Inland DUS + CAS-Foreign AGP, Golden = Z74 DE
    assert days['2025-09-27']['klass'] == 'Z74', (
        f'09-27 DUS-Inland nicht durch AGP-CAS überschrieben sein, war: '
        f'{days["2025-09-27"]["klass"]}'
    )


# ─── Test 6: keine known-bad-extra-workdays ──────────────────

@pytest.mark.parametrize('datum,not_klass', [
    ('2025-03-22', 'Z72'),  # FRA→TOS endet nicht in FRA, nicht Inland-Z72
    ('2025-04-07', 'Z72'),  # ORTSTAG FRS — passive, nicht Z72
    ('2025-04-28', 'Z72'),  # LMN_AS — passive, nicht Z72
    ('2025-05-19', 'Z72'),  # LMN_AS — passive, nicht Z72
    ('2025-07-03', 'Z72'),  # OTP→FRA→LHR endet LHR — nicht Inland
])
def test_tibor_no_known_bad_extra_workdays(aerotax_result, datum, not_klass):
    days = {t['datum']: t for t in aerotax_result['tage_detail']}
    assert days[datum]['klass'] != not_klass, (
        f'{datum} darf NICHT {not_klass} sein'
    )


# ─── Test 7: keine known-missing-z76-layover-days ────────────

@pytest.mark.parametrize('datum,expected_klass', [
    ('2025-01-06', 'Z76'),  # BH-003a Bangalore Heimkehr
    ('2025-04-01', 'Z76'),  # == Mumbai
    ('2025-05-17', 'Z76'),  # OFF USA
    ('2025-06-17', 'Z76'),  # OFF Kroatien
    ('2025-06-18', 'Z76'),  # OFF Kroatien
    ('2025-07-23', 'Z76'),  # == Schweden
])
def test_tibor_no_known_missing_z76_layover_days(aerotax_result, datum, expected_klass):
    days = {t['datum']: t for t in aerotax_result['tage_detail']}
    assert days[datum]['klass'] == expected_klass, (
        f'{datum} muss {expected_klass} sein, war {days[datum]["klass"]}'
    )


# ─── Test 8: Hotelnächte 66 ±2 ────────────────────────────────

def test_tibor_hotel_nights_within_tolerance(aerotax_result):
    assert abs(aerotax_result['hotel_naechte'] - 66) <= 2, (
        f'Hotel {aerotax_result["hotel_naechte"]} != 66 ±2'
    )
```

### Erwarteter Status (Phase 0)

- **8/8 tests RED** (alle scheitern mit AttributeError oder Funktion-fehlt)
- Test-Datei selbst **kompiliert** (kein SyntaxError) — Tests sind ausführbar
- pytest meldet sauberen Stack-Trace: „module has no attribute `_normalize_tours_from_raw_facts`"

---

## §4 Tour-Boundary-Tests (Phase 0 RED)

### `tests/test_normalized_tours_bangalore.py` (6 tests)

| Test | Erwartung (nach BH-CORE-001) |
|---|---|
| `test_bangalore_4_days_single_tour` | normalize → 1 tour, days[0..3] |
| `test_bangalore_01_04_x_marker_is_tour_mid` | day['2025-01-04'].role='tour_mid' (NICHT non_tour) |
| `test_bangalore_destination_is_blr_india` | tour.primary_destination='BLR', destination_country contains 'Indien' |
| `test_bangalore_tour_size_4` | tour.tour_size=4 |
| `test_bangalore_01_03_is_tour_start` | day['2025-01-03'].role='tour_start' |
| `test_bangalore_01_06_is_tour_end` | day['2025-01-06'].role='tour_end' |

### `tests/test_normalized_tours_res_standby.py` (5 tests)

| Test | Erwartung |
|---|---|
| `test_res_in_foreign_tour_is_standby_hotel` | RES + prev.overnight=True + foreign layover → is_standby_hotel=True |
| `test_res_at_homebase_is_standby_homebase` | RES + kein overnight + homebase → is_standby_homebase=True, role=non_tour |
| `test_res_korea_4_days_single_tour` | 04-23 bis 04-26 = 1 Tour, tour_size=4 |
| `test_res_korea_destination_resolved_by_ai_or_routing` | tour.primary_destination resolved (ICN/Republik Korea) |
| `test_res_alone_at_homebase_not_tour` | RES ohne foreign context → role=non_tour |

### `tests/test_normalized_tours_x_off_markers.py` (7 tests)

| Test | Erwartung |
|---|---|
| `test_x_marker_with_iata_hint_is_tour_mid` | `X HKG` mit foreign layover → role=tour_mid, location=foreign_layover |
| `test_double_equals_in_active_tour_is_tour_mid` | `==` mit Sandwich-Pattern → tour_mid |
| `test_double_equals_at_home_without_tour_is_non_tour` | `==` ohne overnight-prev/next → non_tour |
| `test_off_marker_in_foreign_tour_is_tour_mid` | `OFF` mit prev.overnight=True → tour_mid |
| `test_off_marker_at_home_is_non_tour` | `OFF` ohne Tour → non_tour |
| `test_x_marker_at_home_without_routing_is_non_tour` | `X` ohne Routing + ohne overnight → non_tour, Frei |
| `test_ki_resolver_called_for_ambiguous_marker` | `X` ohne klare evidence → KI resolver called with kind='tour_context' |

### `tests/test_phase1_se_override_guard.py` (6 tests)

| Test | Erwartung |
|---|---|
| `test_se_override_requires_prev_overnight_foreign` | SE-Ausland-Stempel allein reicht NICHT für Frei→Z76 |
| `test_se_override_with_prev_overnight_foreign_works` | SE-Ausland-Stempel + prev.overnight foreign → Frei→Z76 OK |
| `test_se_inland_stamp_not_overridden_by_cas_foreign_without_continuation` | 09-27 Pattern: SE-DUS + CAS-AGP ohne strong continuation → Z74 (NICHT Z76 AGP) |
| `test_se_override_respects_routing_continuity` | Wenn routing zeigt nicht-foreign → kein rescue |
| `test_se_override_respects_ki_tour_context_low_conf` | KI conf < 0.85 → kein auto rescue, needs_review |
| `test_se_override_logs_evidence_in_audit_trail` | Rescue audit-note enthält evidence list |

---

## §5 Frontend-Tests (unverändert)

| Datei | Tests | Status |
|---|---:|---|
| `test_normalize_state.mjs` | 12 | grün (BH-CORE-001 ändert nichts am Frontend) |
| `test_state_machine.mjs` | 19 | grün |

---

## §6 Risiken-Matrix

| Risiko | Mitigation in Phase 0 |
|---|---|
| Tests rufen nicht-existente Funktion → ImportError statt RED-assertion | `pytest.importorskip` oder `try/except ModuleNotFoundError` mit `pytest.skip("BH-CORE-001 not implemented")` |
| 1358 bestehende Tests könnten regressionieren | Phase 0 ändert **keine** Produktiv-Files in app.py. Nur tests/ und docs/ |
| Acceptance-Toleranzen sind zu eng/locker | `±2 Tage / ±150 €` ist ChatGPT-Empfehlung. Justierbar nach Phase 1-3 |
| `_build_matched_from_raw` helper fehlt | Phase 1-Aufgabe: aus raw fixture JSON `matched_days`-format bauen |
| Fixture-Daten für 04-23 bis 04-26 unklar (RES) | Tests skippen mit Marker `pytest.mark.xfail(reason="awaiting RES-fixture-detail")` bis Phase 1 |

---

## §7 Regression-Target nach Phase 0

```
backend pytest: 1358 grün, +32 RED (neue BH-CORE-001 tests)
frontend mjs: 31 grün
total: 1390 + 32 RED = 1422 expected
```

Phase 1 Ziel: 1358 grün bleiben + 32 zu grün bringen.

---

## §8 Open Issues für Phase 1

1. **`_build_matched_from_raw`** helper schreiben (raw fixture → matched_days schema)
2. **`_normalize_tours_from_raw_facts`** Stub mit korrekter Signatur einbauen (wirft `NotImplementedError`)
3. **Tests RED**: `python -m pytest tests/test_tibor_2025_golden_acceptance.py` muss laufen + fail mit clearen Reasons
4. **Fixture-Daten erweitern**: falls Tibor-Daten für 04-23 bis 04-26 unvollständig (RES) → KI-Resolver-Use-Case oder synthetic fixture
5. **CI-Pipeline**: BH-CORE-001-Tests **nicht** als blocker, bis Phase 1 fertig (xfail oder skip)
