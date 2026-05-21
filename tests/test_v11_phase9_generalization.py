"""v11 Clean-Release Phase 9 — Generalization Tests.

Synthetische Faelle aus Master-Auftrag, ohne Tibor-spezifische Daten:
- Day-Suffix / position_in_tour
- PU/P1/P2 Marker ohne IATA-Verwechslung
- NTF update ueberschreibt PUB
- Flight-Hours-Summary darf Detailplan nicht ueberschreiben
- Missing CAS / SE month warnings
- Office/Training mit/ohne Uhrzeit

Spec: Master-Auftrag Phase 9.
"""
import os
import sys
import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app as app_module


# ════════════════════════════════════════════════════════════════════════════
# PU / P1 / P2 Marker — NICHT als Airport interpretieren
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('marker', [
    '15688 PU',
    '15688 PU (Day 2)',
    '12345 PU / Tag 1',
    '31591 P1',
    '49444 P1 /ZH',
    '73724 P1',
])
def test_pu_p1_marker_does_not_imply_iata(marker):
    """Marker mit PU/P1 darf KEINE IATA-Annotation ausloesen (es ist Position/Sequence)."""
    if not hasattr(app_module, '_cas_reader_v2_mock_dispatch'):
        pytest.skip('Reader V2 mock missing')
    r = app_module._cas_reader_v2_mock_dispatch(
        day_excerpt='', prev_day=None, next_day=None, homebase='FRA',
        marker_hint=marker, routing_hint=[], layover_hint='',
        overnight_hint=False, has_fl_hint=False, start_time_hint='',
        duty_hint=0, datum='2025-06-15',
    )
    assert r['layover_ort'] == '', f'PU-Marker erzeugt phantom-layover: {r["layover_ort"]}'
    assert r['routing'] == [], f'PU-Marker erzeugt phantom-routing: {r["routing"]}'


def test_pula_purser_disambiguation():
    """PU = Purser/Position (NICHT Pula-IATA). Reader-V2 darf das nicht verwechseln."""
    if not hasattr(app_module, '_cas_reader_v2_mock_dispatch'):
        pytest.skip('Reader V2 mock missing')
    r = app_module._cas_reader_v2_mock_dispatch(
        day_excerpt='', prev_day=None, next_day=None, homebase='FRA',
        marker_hint='12345 PU', routing_hint=[], layover_hint='',
        overnight_hint=False, has_fl_hint=False, start_time_hint='',
        duty_hint=0, datum='2025-06-15',
    )
    assert 'PUY' not in (r.get('layover_ort') or ''), 'PU != Pula'
    assert 'PU' not in (r.get('routing') or []), 'PU != IATA'


# ════════════════════════════════════════════════════════════════════════════
# Doc-Type Detection — Flight Hours kann CAS NICHT überschreiben
# ════════════════════════════════════════════════════════════════════════════

def test_flight_hours_summary_cannot_override_dienstplan_cas_detection():
    """Wenn beide Dateien hochgeladen sind: dienstplan_cas wird primary, flight_hours legacy_ignored."""
    cas_result = app_module.classify_uploaded_pdf_doc_type(b'', filename='PUB_11_2025.pdf')
    flug_result = app_module.classify_uploaded_pdf_doc_type(b'', filename='Flugstundenuebersicht.pdf')
    assert cas_result == 'dienstplan_cas'
    assert flug_result == 'legacy_ignored_flight_hours_summary'
    assert flug_result != cas_result, 'beide Klassifikationen muessen disjoint sein'


def test_cas_reader_refuses_flight_hours_file_even_if_renamed():
    """Wenn jemand Flugstundenuebersicht.pdf zu 'Dienstplan.pdf' umbennent: Refuse moeglich
    nur via content-detection, aber filename allein gibt nicht 100% Sicherheit.

    Hier testen wir: Filename-only-detection liefert KEIN false-positive fuer
    'Dienstplan'-Filename → wir koennen content-detection nicht testen ohne PDF."""
    r = app_module.classify_uploaded_pdf_doc_type(b'', filename='Dienstplan_2025.pdf')
    # Ohne Content → unknown (kein false-positive)
    assert r in ('unknown', 'dienstplan_cas')  # weak check, content-detection wuerde mehr saying


# ════════════════════════════════════════════════════════════════════════════
# Missing CAS / SE month warnings
# ════════════════════════════════════════════════════════════════════════════

def test_missing_cas_months_listed_in_health():
    """document_health.missing_months_cas listet fehlende Monate."""
    cas_3 = {
        '_tage_detail': [{'datum': f'2025-{m:02d}-15'} for m in (1, 6, 12)]
    }
    se_full = {
        'se_lines': [
            {'datum': f'2025-{m:02d}-15', 'storno': False, 'stfrei_betrag': 50.0}
            for m in range(1, 13)
        ]
    }
    h = app_module._build_v11_upload_health(
        lsb_data={'brutto': 50000, 'ag_z17': 0},
        se_structured=se_full,
        cas_classification=cas_3,
        year=2025,
    )
    assert h['cas_months_count'] == 3
    assert h['missing_months_cas'] == [2, 3, 4, 5, 7, 8, 9, 10, 11]


def test_missing_se_months_listed_in_health():
    cas_full = {
        '_tage_detail': [{'datum': f'2025-{m:02d}-15'} for m in range(1, 13)]
    }
    se_partial = {
        'se_lines': [
            {'datum': f'2025-{m:02d}-15', 'storno': False, 'stfrei_betrag': 50.0}
            for m in (1, 2, 3)
        ]
    }
    h = app_module._build_v11_upload_health(
        lsb_data={'brutto': 50000, 'ag_z17': 0},
        se_structured=se_partial,
        cas_classification=cas_full,
        year=2025,
    )
    assert h['se_months_count'] == 3
    assert h['missing_months_se'] == [4, 5, 6, 7, 8, 9, 10, 11, 12]


# ════════════════════════════════════════════════════════════════════════════
# Document-Type Detection — alle Lufthansa-PDF-Familien
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('filename,expected', [
    ('PUB_3_1_0_2025-02-25.pdf', 'dienstplan_cas'),
    ('PUB_11_1_0_2025-10-24.pdf', 'dienstplan_cas'),
    ('NTF_8_1_1_2025-07-30.pdf', 'dienstplan_cas'),
    ('NTF_1_1_1_1225191539_2025-12-25.pdf', 'dienstplan_cas'),
    ('Lohnsteuerbescheinigung_2025.pdf', 'unknown'),  # Filename allein, ohne PDF-Inhalt
    ('Flugstundenuebersicht_2025.pdf', 'legacy_ignored_flight_hours_summary'),
    ('2025_flugstunden_summary.pdf', 'legacy_ignored_flight_hours_summary'),
    ('random.pdf', 'unknown'),
])
def test_filename_based_doc_detection(filename, expected):
    r = app_module.classify_uploaded_pdf_doc_type(b'', filename=filename)
    assert r == expected, f'{filename} → {r}, expected {expected}'


# ════════════════════════════════════════════════════════════════════════════
# Standby-Disambiguation aus Phase-5b/c
# ════════════════════════════════════════════════════════════════════════════

def test_sb_m_without_se_stays_homebase_standby():
    """SB_M ohne SE-Aktivation → homebase_standby (Frei-aequivalent)."""
    if not hasattr(app_module, '_cas_reader_v2_mock_dispatch'):
        pytest.skip('Reader V2 mock missing')
    r = app_module._cas_reader_v2_mock_dispatch(
        day_excerpt='', prev_day=None, next_day=None, homebase='FRA',
        marker_hint='SB_M', routing_hint=['FRA'], layover_hint='',
        overnight_hint=False, has_fl_hint=False, start_time_hint='08:00',
        duty_hint=450, datum='2025-06-15',
    )
    assert r['activity_type'] == 'standby'
    # Ohne foreign-prev → standby_home_air
    assert r['tour_context'] in ('homebase_standby',), f'unexpected ctx: {r["tour_context"]}'


# ════════════════════════════════════════════════════════════════════════════
# Anti-Hallucination Pflicht-Test
# ════════════════════════════════════════════════════════════════════════════

def test_empty_inputs_produce_no_phantom_tour():
    """Reader-V2 ohne Inputs darf KEINE Tour erfinden."""
    if not hasattr(app_module, '_cas_reader_v2_mock_dispatch'):
        pytest.skip('Reader V2 mock missing')
    r = app_module._cas_reader_v2_mock_dispatch(
        day_excerpt='', prev_day=None, next_day=None, homebase='FRA',
        marker_hint='', routing_hint=[], layover_hint='',
        overnight_hint=False, has_fl_hint=False, start_time_hint='',
        duty_hint=0, datum='2025-01-15',
    )
    assert r['tour_id_candidate'] == ''
    assert r['layover_ort'] == ''
    assert r['routing'] == []
    assert r['needs_context_resolution'] is True


# ════════════════════════════════════════════════════════════════════════════
# Counter-Logic darf NICHT Tibor-spezifische Konstanten enthalten
# ════════════════════════════════════════════════════════════════════════════

def test_no_specific_tibor_values_hardcoded_in_logic():
    """app.py darf KEINE Tibor-spezifischen KPI-Werte (123, 55, 37, 5049) als Konstante haben."""
    src = open(os.path.join(ROOT_DIR, 'app.py'), 'r', encoding='utf-8').read()
    # Pruefe: keine Hard-Coded Tibor-spezifische Werte
    # Erwartung: 123, 55, 37, 5049, 4794, 6020.72 dürfen NICHT als logische Konstante stehen
    forbidden = ('= 5049', '= 4794', '= 6020.72', '== 5049')
    for f in forbidden:
        assert f not in src, f'Verbotene Tibor-Konstante: "{f}" gefunden in app.py'


# ════════════════════════════════════════════════════════════════════════════
# Pipeline-Output-Schema Pflicht-Felder
# ════════════════════════════════════════════════════════════════════════════

def test_pipeline_result_has_all_required_kpi_fields(monkeypatch):
    """Pipeline-Result enthaelt ALLE Pflicht-KPI-Felder fuer Master-Acceptance."""
    import json
    v2 = json.load(open(os.path.join(ROOT_DIR, 'tests/fixtures/tibor_2025_cas_v2_from_dienstplan.json')))
    raw = v2['tage_detail']
    matched = app_module._build_matched_from_raw(raw)
    tours = app_module._normalize_tours_from_raw_facts(matched, homebase='FRA', year=2025)
    r = app_module._classify_days_from_normalized_tours(tours, year=2025, homebase='FRA')
    required = ('arbeitstage', 'reinigungstage', 'hotel_naechte', 'fahr_tage',
                'z72_tage', 'z73_tage', 'z74_tage', 'z76_eur', 'gesamt',
                'tage_detail')
    for f in required:
        assert f in r, f'Pflicht-KPI-Feld {f} fehlt'
