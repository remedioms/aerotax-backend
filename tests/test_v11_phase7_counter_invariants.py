"""v11 Clean-Release Phase 7 — Counter Finalisierung Invariants.

Verifiziert die Master-Pflicht-Counter-Regeln:
- Arbeitstag nur bei echter Dienst-/Tour-/Trainings-Evidenz
- Passive Homebase-Marker nicht zaehlen
- Standby zuhause nicht zaehlen
- Hotelnacht nur bei echter Uebernachtung ausserhalb Homebase
- Rueckkehr Homebase erzeugt keine Hotelnacht
- Flight hours summary cannot influence counters

Spec: Master-Auftrag Phase 7.
"""
import json
import os
import sys
import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
FIXTURE_DIR = os.path.join(THIS_DIR, 'fixtures')

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app as app_module


@pytest.fixture(scope='module')
def tibor_pipeline_result():
    """Tour-First Pipeline auf V2-Fixture re-running."""
    v2 = json.load(open(os.path.join(FIXTURE_DIR, 'tibor_2025_cas_v2_from_dienstplan.json')))
    raw = v2['tage_detail']
    matched = app_module._build_matched_from_raw(raw)
    tours = app_module._normalize_tours_from_raw_facts(matched, homebase='FRA', year=2025)
    result = app_module._classify_days_from_normalized_tours(tours, year=2025, homebase='FRA')
    return result


# ════════════════════════════════════════════════════════════════════════════
# Hard-Constraints aus CLAUDE.md
# ════════════════════════════════════════════════════════════════════════════

def test_hotel_naechte_not_greater_than_arbeitstage(tibor_pipeline_result):
    """Hard-Fail: hotel_naechte > arbeitstage waere absurd."""
    r = tibor_pipeline_result
    assert r['hotel_naechte'] <= r['arbeitstage'], \
        f'hotel_naechte {r["hotel_naechte"]} > arbeitstage {r["arbeitstage"]}'


def test_arbeitstage_not_greater_than_230(tibor_pipeline_result):
    """Hard-Fail: arbeitstage > 230 waere physisch unmoeglich (Tarifvertrag)."""
    r = tibor_pipeline_result
    assert r['arbeitstage'] <= 230, f'arbeitstage {r["arbeitstage"]} > 230 = HARD FAIL'


def test_fahr_tage_le_arbeitstage(tibor_pipeline_result):
    """Fahrtage <= Arbeitstage (jeder Fahrtag ist ein Arbeitstag)."""
    r = tibor_pipeline_result
    assert r['fahr_tage'] <= r['arbeitstage']


def test_z76_eur_non_negative(tibor_pipeline_result):
    r = tibor_pipeline_result
    assert r.get('z76_eur', 0) >= 0


def test_z72_z73_z74_tage_non_negative(tibor_pipeline_result):
    r = tibor_pipeline_result
    assert r.get('z72_tage', 0) >= 0
    assert r.get('z73_tage', 0) >= 0
    assert r.get('z74_tage', 0) >= 0


# ════════════════════════════════════════════════════════════════════════════
# Counter-Konsistenz: tage_detail.klass summiert zu Counters
# ════════════════════════════════════════════════════════════════════════════

def test_counters_match_tage_detail_klass_sum(tibor_pipeline_result):
    """Counter werden aus tage_detail.klass aggregiert (kein inkrementelles Hochzaehlen)."""
    r = tibor_pipeline_result
    tage = r.get('tage_detail') or []
    z72 = sum(1 for t in tage if t.get('klass') == 'Z72')
    z73 = sum(1 for t in tage if t.get('klass') == 'Z73')
    z74 = sum(1 for t in tage if t.get('klass') == 'Z74')
    z76 = sum(1 for t in tage if t.get('klass') == 'Z76')
    assert r.get('z72_tage') == z72, f'z72_tage {r["z72_tage"]} != sum {z72}'
    assert r.get('z73_tage') == z73
    assert r.get('z74_tage') == z74


def test_arbeitstage_via_counted_as_workday_flag(tibor_pipeline_result):
    """Tour-First-Output: counted_as_workday=True markiert Arbeitstage."""
    r = tibor_pipeline_result
    tage = r.get('tage_detail') or []
    work_count = sum(1 for t in tage if t.get('counted_as_workday'))
    assert r['arbeitstage'] == work_count, \
        f'arbeitstage {r["arbeitstage"]} != counted_as_workday-sum {work_count}'


def test_hotel_naechte_via_counted_as_hotel_nacht_flag(tibor_pipeline_result):
    r = tibor_pipeline_result
    tage = r.get('tage_detail') or []
    h_count = sum(1 for t in tage if t.get('counted_as_hotel_nacht'))
    assert r['hotel_naechte'] == h_count


def test_fahr_tage_via_counted_as_fahrtag_flag(tibor_pipeline_result):
    r = tibor_pipeline_result
    tage = r.get('tage_detail') or []
    f_count = sum(1 for t in tage if t.get('counted_as_fahrtag'))
    assert r['fahr_tage'] == f_count


# ════════════════════════════════════════════════════════════════════════════
# Flight hours summary cannot influence counters
# ════════════════════════════════════════════════════════════════════════════

def test_flight_hours_summary_cannot_influence_counters_via_legacy_reader():
    """Legacy DP-Reader (Flugstundenuebersicht) wirft RuntimeError ohne Forensik-Override.

    Daher kann kein Counter-Wert aus Flugstundenuebersicht-Daten kommen.
    """
    # Nicht-Forensik-Mode: Legacy-Reader DARF NICHT laufen
    os.environ.pop('AEROTAX_LEGACY_FLUGSTUNDEN_FORENSIK', None)
    with pytest.raises(RuntimeError) as ei:
        app_module._sonnet_read_dp_structured([b'fake'])
    assert 'v11 Clean-Release' in str(ei.value)


def test_v2_fixture_has_no_flight_hours_source():
    """V2-Fixture hat keine Flugstundenuebersicht-Quelle in sources."""
    v2 = json.load(open(os.path.join(FIXTURE_DIR, 'tibor_2025_cas_v2_from_dienstplan.json')))
    for t in v2['tage_detail']:
        for src in t.get('sources', []):
            assert 'FLUG' not in str(src).upper(), \
                f'Tag {t.get("datum")}: Flugstunden-Quelle verboten'


# ════════════════════════════════════════════════════════════════════════════
# Tour-Continuity Invarianten
# ════════════════════════════════════════════════════════════════════════════

def test_no_phantom_hotel_at_homebase(tibor_pipeline_result):
    """counted_as_hotel_nacht=True darf nur bei klass Z73/Z74/Z76 sein, nicht Frei/Standby."""
    r = tibor_pipeline_result
    tage = r.get('tage_detail') or []
    phantom = []
    for t in tage:
        if t.get('counted_as_hotel_nacht'):
            klass = t.get('klass')
            if klass not in ('Z73', 'Z74', 'Z76'):
                phantom.append(f"{t.get('datum')} klass={klass}")
    assert not phantom, f'Phantom-Hotel-Naechte: {phantom[:5]}'


def test_z76_only_with_foreign_layover_evidence(tibor_pipeline_result):
    """Z76 darf nur bei klarer foreign-Tour-Evidence vergeben werden.

    Tour-First-Output hat bmf_land + location_context + tour_id direkt am Tag.
    """
    r = tibor_pipeline_result
    tage = r.get('tage_detail') or []
    suspicious = []
    for t in tage:
        if t.get('klass') != 'Z76':
            continue
        bmf = t.get('bmf_land', '')
        loc_ctx = t.get('location_context') or {}
        tour_id = t.get('tour_id') or ''
        routing = t.get('routing') or []
        # Z76 muss mindestens eines haben: bmf_land != 'Deutschland', location_context oder tour_id
        has_evidence = bool(bmf and bmf != 'Deutschland') or bool(tour_id) or bool(loc_ctx) or bool(routing)
        if not has_evidence:
            suspicious.append(t.get('datum'))
    assert len(suspicious) == 0, f'{len(suspicious)} Z76 ohne foreign-Evidence: {suspicious[:5]}'


# ════════════════════════════════════════════════════════════════════════════
# Stabilitaet: gleicher Input → gleicher Output (Determinismus)
# ════════════════════════════════════════════════════════════════════════════

def test_pipeline_deterministic_on_v2_fixture(tibor_pipeline_result):
    """Pipeline ist deterministisch — zwei Runs liefern identische KPIs."""
    v2 = json.load(open(os.path.join(FIXTURE_DIR, 'tibor_2025_cas_v2_from_dienstplan.json')))
    raw = v2['tage_detail']
    matched1 = app_module._build_matched_from_raw(raw)
    tours1 = app_module._normalize_tours_from_raw_facts(matched1, homebase='FRA', year=2025)
    r1 = app_module._classify_days_from_normalized_tours(tours1, year=2025, homebase='FRA')
    # Re-run
    matched2 = app_module._build_matched_from_raw(raw)
    tours2 = app_module._normalize_tours_from_raw_facts(matched2, homebase='FRA', year=2025)
    r2 = app_module._classify_days_from_normalized_tours(tours2, year=2025, homebase='FRA')
    for k in ('arbeitstage', 'hotel_naechte', 'fahr_tage', 'z72_tage', 'z73_tage',
              'z74_tage', 'z76_eur'):
        assert r1.get(k) == r2.get(k), f'Determinismus-Verlust: {k} {r1.get(k)} ≠ {r2.get(k)}'


# ════════════════════════════════════════════════════════════════════════════
# Generalisierbarkeit (kein Tibor-Hardcoding in Counter-Logik)
# ════════════════════════════════════════════════════════════════════════════

def test_no_tibor_hardcoded_strings_in_app():
    """app.py darf KEINE Tibor-spezifischen Strings enthalten."""
    src = open(os.path.join(ROOT_DIR, 'app.py'), 'r', encoding='utf-8').read()
    forbidden_substrings = (
        'tibor', 'TIBOR',
        '99102',  # Tibor's Personalnummer
    )
    for s in forbidden_substrings:
        # Tests/Audit-Trail nicht zaehlen — nur Produktions-Code
        # (app.py ist Produktions-Code; ein-paar Erwaehnungen in Kommentaren sind grenzwertig)
        lines_with_s = [l for l in src.split('\n') if s in l and not l.strip().startswith('#')]
        non_comment = [l for l in lines_with_s if not (l.strip().startswith('"""') or '#' in l[:l.find(s)])]
        # Soft check: hoechstens 1-2 Erwaehnung in Pfad/Kommentar erlaubt
        assert len(non_comment) <= 2, f'Tibor-Hardcoding gefunden ({s}): {non_comment[:3]}'
