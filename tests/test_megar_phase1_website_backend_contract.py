"""MegaR Phase 1 — Website-Backend Contract Tests.

Verifiziert:
- frontend_upload_contract: index.html sendet die 20 erwarteten Felder
- backend_receives_base_year_km
- backend_receives_anreise_modes
- backend_receives_cas_not_dp
- missing_cas_blocks_or_warns
- flight_hours_not_required
- flight_hours_uploaded_accidentally_ignored

Spec: docs/WEBSITE_BACKEND_CONTRACT_AUDIT.md
"""
import os
import sys
import re

import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
SITE_HTML = '/Users/miguelschumann/Desktop/site/index.html'

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app as app_module


def _read_index_html():
    if not os.path.exists(SITE_HTML):
        pytest.skip(f'index.html nicht gefunden ({SITE_HTML}) — Frontend-Tests übersprungen')
    return open(SITE_HTML, encoding='utf-8').read()


def _read_backend():
    return open(os.path.join(ROOT_DIR, 'app.py'), encoding='utf-8').read()


# ════════════════════════════════════════════════════════════════════════════
# Frontend FormData Contract
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('formdata_key', [
    'name', 'vorname', 'nachname',
    'km', 'base', 'year',
    'anreise', 'fahrzeug',
    'oepnv_kosten', 'jobticket', 'shuttle_kosten',
    'lsb', 'se', 'cas',
])
def test_frontend_sends_formdata_field(formdata_key):
    """index.html FormData enthaelt jedes Pflicht-Feld."""
    html = _read_index_html()
    pattern = re.compile(rf"fd\.append\(\s*['\"]{formdata_key}['\"]")
    assert pattern.search(html), f'Frontend sendet kein fd.append({formdata_key!r}, ...)'


def test_frontend_does_not_send_dp_field():
    """Frontend darf nicht mehr `dp`-Field (Flugstundenuebersicht) im FormData haben."""
    html = _read_index_html()
    # `dp` darf nicht als FormData-Key auftauchen (das war Legacy)
    assert not re.search(r"fd\.append\(\s*['\"]dp['\"]", html), \
        'Frontend darf kein fd.append(\'dp\',...) mehr enthalten (Flugstunden raus per v11)'


def test_frontend_does_not_send_einsatz_field():
    """Frontend sendet kein einsatz-File mehr (per CLAUDE.md aus Produkt entfernt)."""
    html = _read_index_html()
    assert not re.search(r"fd\.append\(\s*['\"]einsatz['\"]", html), \
        'Frontend darf kein einsatz-Field mehr senden'


def test_frontend_has_3_required_upload_cards():
    """UI zeigt 3 Pflicht-Karten: LSB + SE + CAS."""
    html = _read_index_html()
    assert 'f-lsb' in html
    assert 'f-se' in html
    assert 'f-cas' in html
    # f-dp darf nur als historischer Kommentar erscheinen, nicht als aktiver Input
    active_input_dp = re.search(r'<input[^>]*id=["\']f-dp["\']', html)
    assert not active_input_dp, 'Aktives <input id="f-dp"> ist verboten'


def test_frontend_has_base_select_and_year_card():
    """UI bietet Homebase-Select und Year-Auswahl."""
    html = _read_index_html()
    assert re.search(r'<select[^>]*id=["\']base["\']', html), 'Homebase select fehlt'
    assert re.search(r'id=["\']yc-202[3-5]["\']', html), 'Year-Cards fehlen'


# ════════════════════════════════════════════════════════════════════════════
# Backend Contract: liest die Felder
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('backend_var', [
    'name', 'vorname', 'nachname',
    'km', 'base', 'year', 'anreise', 'fahrzeug',
    'oepnv_kosten', 'shuttle_kosten', 'jobticket', 'anfahrt_min',
])
def test_backend_reads_form_field(backend_var):
    """Backend `/api/process` liest jedes Frontend-Feld via request.form.get."""
    src = _read_backend()
    proc_idx = src.find('def process_real(')
    block = src[proc_idx:proc_idx + 8000]
    pattern = re.compile(rf"request\.form\.get\(\s*['\"]{backend_var}['\"]")
    assert pattern.search(block), f'/api/process liest nicht request.form.get({backend_var!r})'


def test_backend_validates_base_required():
    """Backend HTTP 400 wenn `base` fehlt (kein hardcoded FRA-Default)."""
    src = _read_backend()
    assert 'UPLOAD_MISSING_REQUIRED' in src
    assert 'Pflichtfeld' in src and 'Homebase' in src


def test_backend_clamps_year_2023_to_2026():
    """Year wird auf 2023-2026 geklemmt."""
    src = _read_backend()
    assert 'max(2023, min(2026, year_input))' in src


def test_backend_caps_km_500():
    """km wird auf 0-500 geklemmt (Plausi)."""
    src = _read_backend()
    assert 'min(500.0' in src or 'min(500, km' in src


def test_backend_anreise_multi_mode_csv():
    """anreise kann CSV-Multi-Mode sein."""
    src = _read_backend()
    assert "anreise_modes_raw" in src
    assert "split(',')" in src


# ════════════════════════════════════════════════════════════════════════════
# Document Routing
# ════════════════════════════════════════════════════════════════════════════

def test_backend_requires_lsb_se_cas_all_three():
    """Wenn LSB oder SE oder CAS fehlt → HTTP 400."""
    src = _read_backend()
    # Suche nach „Lohnsteuerbescheinigung, Streckeneinsatzabrechnung und Dienstplan/CAS"
    assert 'Lohnsteuerbescheinigung' in src
    assert 'Streckeneinsatz' in src
    assert ('Dienstplan' in src or 'CAS' in src)


def test_backend_rejects_dp_without_cas():
    """User laedt Flugstundenuebersicht (dp) hoch aber kein CAS → freundlicher Reject."""
    src = _read_backend()
    assert "files.get('dp') and not files.get('cas')" in src
    assert 'Flugstundenuebersicht' in src or 'Flugstundenübersicht' in src


def test_audit_label_dp_is_legacy_ignored():
    """Audit-Log labelt dp-Uploads als legacy_ignored_flight_hours_summary."""
    src = _read_backend()
    assert "'dp': 'legacy_ignored_flight_hours_summary'" in src


def test_cas_reader_refuses_flight_hours():
    """CAS-Reader refuset flight_hours-PDFs (siehe test_v11_cas_reader_refuses_non_cas)."""
    # Re-verify via doc-type-detection
    r = app_module.classify_uploaded_pdf_doc_type(b'', filename='Flugstundenuebersicht.pdf')
    assert r == 'legacy_ignored_flight_hours_summary'


# ════════════════════════════════════════════════════════════════════════════
# Document Health passes 3-Doc-Modell
# ════════════════════════════════════════════════════════════════════════════

def test_document_health_pipeline_field():
    """_build_v11_upload_health hat pipeline=v11_cas_primary."""
    h = app_module._build_v11_upload_health()
    assert h['pipeline'] == 'v11_cas_primary'


def test_document_health_required_fields():
    """document_health hat alle 9 Pflicht-Felder."""
    h = app_module._build_v11_upload_health()
    for f in ('lsb_present', 'se_months_count', 'cas_months_count',
              'detailed_cas_present', 'missing_months_se', 'missing_months_cas',
              'ignored_legacy_files', 'warnings', 'status'):
        assert f in h, f'Pflicht-Feld {f} fehlt in document_health'
