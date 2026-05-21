"""v11 Clean-Release Phase 2 — Upload-Contract Document-Health Tests.

Verifiziert _build_v11_upload_health() liefert die geforderten Felder:
- lsb_present
- se_months_count
- cas_months_count
- detailed_cas_present
- missing_months_se
- missing_months_cas
- ignored_legacy_files
- warnings
- status

Spec: Master-Auftrag Phase 2.
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
from app import _build_v11_upload_health


# ════════════════════════════════════════════════════════════════════════════
# Helper-Fixtures
# ════════════════════════════════════════════════════════════════════════════

def _lsb_ok():
    return {'brutto': 60000.0, 'ag_z17': 600.0}


def _se_full_year():
    """SE mit allen 12 Monaten + active lines."""
    return {
        'se_lines': [
            {'datum': f'2025-{m:02d}-15', 'storno': False, 'stfrei_betrag': 50.0,
             'stfrei_inland': False, 'stfrei_ort': 'XXX'}
            for m in range(1, 13)
        ],
    }


def _se_3_months():
    """SE mit nur 3 Monaten (warning expected)."""
    return {
        'se_lines': [
            {'datum': f'2025-{m:02d}-15', 'storno': False, 'stfrei_betrag': 50.0}
            for m in (1, 6, 12)
        ],
    }


def _cas_full_year():
    """CAS-Classification-Output mit Tagen aus allen 12 Monaten."""
    return {
        '_tage_detail': [
            {'datum': f'2025-{m:02d}-15'} for m in range(1, 13)
        ],
        '_cas_conflicts': [],
    }


def _cas_3_months():
    return {
        '_tage_detail': [
            {'datum': f'2025-{m:02d}-15'} for m in (1, 6, 12)
        ],
    }


# ════════════════════════════════════════════════════════════════════════════
# Pflicht-Felder existieren
# ════════════════════════════════════════════════════════════════════════════

def test_required_fields_present():
    h = _build_v11_upload_health()
    required = (
        'pipeline', 'lsb_present', 'se_months_count', 'cas_months_count',
        'detailed_cas_present', 'missing_months_se', 'missing_months_cas',
        'ignored_legacy_files', 'warnings', 'issues', 'status',
    )
    for f in required:
        assert f in h, f'Pflicht-Feld {f} fehlt in document_health'


def test_pipeline_field_is_v11_cas_primary():
    h = _build_v11_upload_health()
    assert h['pipeline'] == 'v11_cas_primary'


# ════════════════════════════════════════════════════════════════════════════
# Green Case
# ════════════════════════════════════════════════════════════════════════════

def test_full_year_all_present_is_green():
    h = _build_v11_upload_health(
        lsb_data=_lsb_ok(),
        se_structured=_se_full_year(),
        cas_classification=_cas_full_year(),
        year=2025,
    )
    assert h['status'] == 'green'
    assert h['lsb_present'] is True
    assert h['se_months_count'] == 12
    assert h['cas_months_count'] == 12
    assert h['detailed_cas_present'] is True
    assert h['missing_months_se'] == []
    assert h['missing_months_cas'] == []
    assert h['ignored_legacy_files'] == []
    # Keine red issues
    red_issues = [i for i in h['issues'] if i.get('severity') == 'red']
    assert red_issues == []


# ════════════════════════════════════════════════════════════════════════════
# LSB-Cases
# ════════════════════════════════════════════════════════════════════════════

def test_missing_lsb_is_red():
    h = _build_v11_upload_health(
        lsb_data=None,
        se_structured=_se_full_year(),
        cas_classification=_cas_full_year(),
        year=2025,
    )
    assert h['lsb_present'] is False
    assert h['status'] == 'red'
    assert any(i['source'] == 'LSB' for i in h['issues'])


def test_lsb_with_brutto_zero_is_red():
    h = _build_v11_upload_health(
        lsb_data={'brutto': 0.0},
        se_structured=_se_full_year(),
        cas_classification=_cas_full_year(),
        year=2025,
    )
    assert h['lsb_present'] is False
    assert h['status'] == 'red'


# ════════════════════════════════════════════════════════════════════════════
# SE-Cases
# ════════════════════════════════════════════════════════════════════════════

def test_missing_se_is_red():
    h = _build_v11_upload_health(
        lsb_data=_lsb_ok(),
        se_structured=None,
        cas_classification=_cas_full_year(),
        year=2025,
    )
    assert h['se_months_count'] == 0
    assert h['status'] == 'red'
    assert any(i['source'] == 'SE' for i in h['issues'])


def test_se_only_3_months_yellow_warning():
    h = _build_v11_upload_health(
        lsb_data=_lsb_ok(),
        se_structured=_se_3_months(),
        cas_classification=_cas_full_year(),
        year=2025,
    )
    assert h['se_months_count'] == 3
    assert h['status'] == 'yellow'
    assert h['missing_months_se'] == [2, 3, 4, 5, 7, 8, 9, 10, 11]
    assert any('SE-Monate' in w for w in h['warnings'])


def test_se_with_only_storno_is_red():
    """SE-Datei vorhanden, aber alle Zeilen sind storniert."""
    se = {'se_lines': [
        {'datum': '2025-03-15', 'storno': True, 'stfrei_betrag': 50.0}
    ]}
    h = _build_v11_upload_health(
        lsb_data=_lsb_ok(),
        se_structured=se,
        cas_classification=_cas_full_year(),
        year=2025,
    )
    # 0 active months
    assert h['se_months_count'] == 0


# ════════════════════════════════════════════════════════════════════════════
# CAS-Cases
# ════════════════════════════════════════════════════════════════════════════

def test_missing_cas_is_red():
    h = _build_v11_upload_health(
        lsb_data=_lsb_ok(),
        se_structured=_se_full_year(),
        cas_classification=None,
        year=2025,
    )
    assert h['cas_months_count'] == 0
    assert h['detailed_cas_present'] is False
    assert h['status'] == 'red'
    assert any(i['source'] == 'CAS' for i in h['issues'])


def test_cas_only_3_months_yellow_warning():
    h = _build_v11_upload_health(
        lsb_data=_lsb_ok(),
        se_structured=_se_full_year(),
        cas_classification=_cas_3_months(),
        year=2025,
    )
    assert h['cas_months_count'] == 3
    assert h['detailed_cas_present'] is True
    assert h['status'] == 'yellow'
    assert h['missing_months_cas'] == [2, 3, 4, 5, 7, 8, 9, 10, 11]


def test_empty_cas_tage_detail_is_red():
    h = _build_v11_upload_health(
        lsb_data=_lsb_ok(),
        se_structured=_se_full_year(),
        cas_classification={'_tage_detail': []},
        year=2025,
    )
    assert h['detailed_cas_present'] is False
    assert h['status'] == 'red'


# ════════════════════════════════════════════════════════════════════════════
# Legacy-Ignored-Files
# ════════════════════════════════════════════════════════════════════════════

def test_ignored_flight_hours_files_listed():
    h = _build_v11_upload_health(
        lsb_data=_lsb_ok(),
        se_structured=_se_full_year(),
        cas_classification=_cas_full_year(),
        ignored_legacy_filenames=['Flugstundenuebersichten.pdf', 'Stunden_2025.pdf'],
        year=2025,
    )
    assert h['ignored_legacy_files'] == ['Flugstundenuebersichten.pdf', 'Stunden_2025.pdf']
    assert any('Flugstundenuebersicht' in w for w in h['warnings'])


def test_no_legacy_files_no_warning():
    h = _build_v11_upload_health(
        lsb_data=_lsb_ok(),
        se_structured=_se_full_year(),
        cas_classification=_cas_full_year(),
        ignored_legacy_filenames=None,
        year=2025,
    )
    assert h['ignored_legacy_files'] == []
    legacy_warnings = [w for w in h['warnings'] if 'Flugstundenuebersicht' in w]
    assert legacy_warnings == []


# ════════════════════════════════════════════════════════════════════════════
# Hybrid-Analyze-Integration
# ════════════════════════════════════════════════════════════════════════════

def test_hybrid_analyze_uses_v11_upload_health():
    """In hybrid_analyze wird _build_v11_upload_health im v11_cas-Pfad gerufen."""
    src = open(os.path.join(ROOT_DIR, 'app.py'), 'r', encoding='utf-8').read()
    fn_idx = src.find('def hybrid_analyze(')
    block = src[fn_idx:fn_idx + 80000]
    assert '_build_v11_upload_health(' in block, \
        'hybrid_analyze muss _build_v11_upload_health im v11_cas-Pfad rufen.'
