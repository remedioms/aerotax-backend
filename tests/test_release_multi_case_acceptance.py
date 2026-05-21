"""Rel Phase 16 — Multi-Case Acceptance Matrix.

A. Tibor FRA cabin 2025 (real fixture)
B. Synthetic MUC cabin foreign tour
C. Synthetic FRA cockpit
D. Synthetic DUS unknown airline
E. Synthetic VIE base
F. Synthetic ZRH base (Swiss)
G. Missing SE month
H. Missing CAS month
I. Only LSB+SE no CAS
J. Accidental flight-hours upload
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
from tests.helpers.synthetic_cas_factory import (
    scenario_muc_cabin_tlv_tour,
    scenario_dus_cabin_jfk_tour,
    scenario_ber_cockpit_cdg_tour,
    scenario_vie_cockpit_jfk_tour,
    scenario_zrh_cabin_dxb_tour,
    scenario_other_base_unknown_airline,
)


def _run(matched, base):
    tours = app_module._normalize_tours_from_raw_facts(matched, homebase=base, year=2025)
    return app_module._classify_days_from_normalized_tours(tours, year=2025, homebase=base)


# ════════════════════════════════════════════════════════════════════════════
# Case A: Tibor FRA Cabin 2025 (real fixture)
# ════════════════════════════════════════════════════════════════════════════

def test_case_A_tibor_fra_2025_real_fixture():
    """Existing fixture läuft ohne Crash mit aktueller Pipeline."""
    v2 = json.load(open(os.path.join(FIXTURE_DIR, 'tibor_2025_cas_v2_from_dienstplan.json')))
    matched = app_module._build_matched_from_raw(v2['tage_detail'])
    tours = app_module._normalize_tours_from_raw_facts(matched, homebase='FRA', year=2025)
    result = app_module._classify_days_from_normalized_tours(tours, year=2025, homebase='FRA')
    # Sanity-Checks
    assert result['arbeitstage'] > 100
    assert result['arbeitstage'] < 230
    assert result['z76_eur'] > 0
    assert result['fahr_tage'] > 0


# ════════════════════════════════════════════════════════════════════════════
# Cases B-F: Synthetic Multi-Base/Role
# ════════════════════════════════════════════════════════════════════════════

def test_case_B_synthetic_muc_cabin_tlv():
    matched = scenario_muc_cabin_tlv_tour()
    result = _run(matched, 'MUC')
    z76_days = sum(1 for t in result['tage_detail'] if t.get('klass') == 'Z76')
    assert z76_days >= 1


def test_case_C_synthetic_ber_cockpit_cdg():
    matched = scenario_ber_cockpit_cdg_tour()
    result = _run(matched, 'BER')
    z76_days = sum(1 for t in result['tage_detail'] if t.get('klass') == 'Z76')
    assert z76_days >= 1


def test_case_D_synthetic_dus_jfk():
    matched = scenario_dus_cabin_jfk_tour()
    result = _run(matched, 'DUS')
    z76_days = sum(1 for t in result['tage_detail'] if t.get('klass') == 'Z76')
    assert z76_days >= 1


def test_case_E_synthetic_vie_base():
    matched = scenario_vie_cockpit_jfk_tour()
    result = _run(matched, 'VIE')
    # VIE → foreign-Tour erkannt
    z76_days = sum(1 for t in result['tage_detail'] if t.get('klass') == 'Z76')
    assert z76_days >= 1


def test_case_F_synthetic_zrh_base():
    matched = scenario_zrh_cabin_dxb_tour()
    result = _run(matched, 'ZRH')
    # ZRH → foreign-Tour erkannt
    z76_days = sum(1 for t in result['tage_detail'] if t.get('klass') == 'Z76')
    assert z76_days >= 1


# ════════════════════════════════════════════════════════════════════════════
# Cases G/H/I: Missing-Data-Variants
# ════════════════════════════════════════════════════════════════════════════

def test_case_G_missing_se_month_warning():
    se_3 = {'se_lines': [
        {'datum': f'2025-{m:02d}-15', 'storno': False, 'stfrei_betrag': 50.0}
        for m in (1, 6, 12)
    ]}
    cas_full = {'_tage_detail': [{'datum': f'2025-{m:02d}-15'} for m in range(1, 13)]}
    h = app_module._build_v11_upload_health(
        lsb_data={'brutto': 50000}, se_structured=se_3,
        cas_classification=cas_full, year=2025)
    assert h['status'] == 'yellow'
    assert h['se_months_count'] == 3


def test_case_H_missing_cas_month_warning():
    cas_3 = {'_tage_detail': [{'datum': f'2025-{m:02d}-15'} for m in (1, 6, 12)]}
    se_full = {'se_lines': [
        {'datum': f'2025-{m:02d}-15', 'storno': False, 'stfrei_betrag': 50.0}
        for m in range(1, 13)
    ]}
    h = app_module._build_v11_upload_health(
        lsb_data={'brutto': 50000}, se_structured=se_full,
        cas_classification=cas_3, year=2025)
    assert h['status'] == 'yellow'
    assert h['cas_months_count'] == 3


def test_case_I_only_lsb_se_no_cas():
    h = app_module._build_v11_upload_health(
        lsb_data={'brutto': 50000},
        se_structured={'se_lines': [{'datum': '2025-01-15', 'storno': False}]},
        cas_classification=None, year=2025)
    assert h['status'] == 'red'
    assert any(i['source'] == 'CAS' for i in h['issues'])


# ════════════════════════════════════════════════════════════════════════════
# Case J: Accidental flight-hours upload ignored
# ════════════════════════════════════════════════════════════════════════════

def test_case_J_accidental_flight_hours_ignored():
    r = app_module.classify_uploaded_pdf_doc_type(b'', filename='Flugstundenuebersicht.pdf')
    assert r == 'legacy_ignored_flight_hours_summary'


def test_case_J_cas_reader_refuses_flight_hours():
    result = app_module._sonnet_read_cas_structured(
        cas_bytes=[b'fake'],
        source_filenames=['Flugstundenuebersicht.pdf'],
    )
    refused = result.get('_refused_files') or []
    assert len(refused) >= 1
    assert refused[0]['doc_type'] == 'legacy_ignored_flight_hours_summary'


def test_other_base_unknown_airline_no_crash():
    """Custom Base + Unknown Airline läuft ohne Crash."""
    matched = scenario_other_base_unknown_airline()
    result = _run(matched, 'ABC')
    assert len(result['tage_detail']) >= 1
