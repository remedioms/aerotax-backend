"""Rel Phase 6 — Document Type / Upload Fuzz Tests.

Pflicht-Cases per Master:
- LSB Dateinamen-Varianten
- SE 12-Monats-Set
- CAS PUB/NTF + monthly + duplicate
- Invalid/legacy files (Flugstundenuebersicht, bank statements, images, etc.)
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
from app import (
    classify_uploaded_pdf_doc_type,
    DOC_TYPE_LSB, DOC_TYPE_SE, DOC_TYPE_CAS,
    DOC_TYPE_LEGACY_FLUG, DOC_TYPE_UNKNOWN,
)


# ════════════════════════════════════════════════════════════════════════════
# LSB-Filename-Varianten
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('filename', [
    'Lohnsteuerbescheinigung.pdf',
    'Lohnsteuerbescheinigung_2025.pdf',
    'Lohnsteuerbescheinigung 2025.pdf',
    'LSB_2025_Tibor.pdf',  # NOT directly detected by filename
])
def test_lsb_filename_variants_with_content(monkeypatch, filename):
    """LSB-Erkennung mit Inhalt funktioniert auch bei verschiedenen Filenames."""
    class _FakePage:
        def extract_text(self):
            return 'Lohnsteuerbescheinigung 2025 Brutto 60000'
    class _FakePDF:
        pages = [_FakePage()]
        def __enter__(self): return self
        def __exit__(self, *a): return False
    import pdfplumber as _pp
    monkeypatch.setattr(_pp, 'open', lambda _b: _FakePDF())
    r = classify_uploaded_pdf_doc_type(b'fake', filename=filename)
    assert r == DOC_TYPE_LSB


# ════════════════════════════════════════════════════════════════════════════
# SE-Filename-Varianten + Monats-Logic
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('filename', [
    'Streckeneinsatz_01.pdf',
    'Streckeneinsatz-Abrechnung.pdf',
    'SE_Januar_2025.pdf',  # Inhalt entscheidet
    '2025 Streckeneinsatzabrechnungen.pdf',
])
def test_se_filename_with_content(monkeypatch, filename):
    class _FakePage:
        def extract_text(self):
            return 'Streckeneinsatz-Abrechnung Januar 2025 stfrei 50 EUR'
    class _FakePDF:
        pages = [_FakePage()]
        def __enter__(self): return self
        def __exit__(self, *a): return False
    import pdfplumber as _pp
    monkeypatch.setattr(_pp, 'open', lambda _b: _FakePDF())
    r = classify_uploaded_pdf_doc_type(b'fake', filename=filename)
    assert r == DOC_TYPE_SE


def test_se_missing_month_warning_in_health():
    """SE mit nur 3 Monaten → document_health yellow + missing_months_se Liste."""
    se = {'se_lines': [
        {'datum': f'2025-{m:02d}-15', 'storno': False, 'stfrei_betrag': 50.0}
        for m in (1, 6, 12)
    ]}
    cas_full = {'_tage_detail': [
        {'datum': f'2025-{m:02d}-15'} for m in range(1, 13)
    ]}
    h = app_module._build_v11_upload_health(
        lsb_data={'brutto': 50000}, se_structured=se,
        cas_classification=cas_full, year=2025)
    assert h['se_months_count'] == 3
    assert h['status'] == 'yellow'
    assert len(h['missing_months_se']) == 9


# ════════════════════════════════════════════════════════════════════════════
# CAS-Filename-Varianten (PUB/NTF)
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('filename', [
    'PUB_3_1_0_2025-02-25.pdf',
    'PUB_11_1_0_2025-10-24.pdf',
    'NTF_8_1_1_2025-07-30.pdf',
    'NTF_1_1_1_1225191539_2025-12-25.pdf',
])
def test_cas_pub_ntf_filename(filename):
    """PUB_/NTF_-Filenames → dienstplan_cas direkt aus Name."""
    r = classify_uploaded_pdf_doc_type(b'', filename=filename)
    assert r == DOC_TYPE_CAS


def test_cas_missing_month_warning():
    cas_3 = {'_tage_detail': [{'datum': f'2025-{m:02d}-15'} for m in (1, 6, 12)]}
    se_full = {'se_lines': [
        {'datum': f'2025-{m:02d}-15', 'storno': False, 'stfrei_betrag': 50.0}
        for m in range(1, 13)
    ]}
    h = app_module._build_v11_upload_health(
        lsb_data={'brutto': 50000}, se_structured=se_full,
        cas_classification=cas_3, year=2025)
    assert h['cas_months_count'] == 3
    assert h['status'] == 'yellow'
    assert len(h['missing_months_cas']) == 9


# ════════════════════════════════════════════════════════════════════════════
# Invalid/Legacy
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('filename', [
    'Flugstundenübersicht.pdf',
    'Flugstundenuebersicht.pdf',
    '2025 Flugstundenübersichten.pdf',
    'Stundenuebersicht_2025.pdf',
])
def test_flight_hours_filename_recognized_as_legacy(filename):
    r = classify_uploaded_pdf_doc_type(b'', filename=filename)
    assert r == DOC_TYPE_LEGACY_FLUG


def test_random_filename_unknown(monkeypatch):
    class _FakePage:
        def extract_text(self): return 'Random text no markers'
    class _FakePDF:
        pages = [_FakePage()]
        def __enter__(self): return self
        def __exit__(self, *a): return False
    import pdfplumber as _pp
    monkeypatch.setattr(_pp, 'open', lambda _b: _FakePDF())
    r = classify_uploaded_pdf_doc_type(b'fake', filename='random_2025.pdf')
    assert r == DOC_TYPE_UNKNOWN


def test_image_or_unreadable_pdf_returns_unknown(monkeypatch):
    def _boom(_b):
        raise RuntimeError('pdfplumber failed - image PDF')
    import pdfplumber as _pp
    monkeypatch.setattr(_pp, 'open', _boom)
    r = classify_uploaded_pdf_doc_type(b'broken', filename='image_scan.pdf')
    assert r == DOC_TYPE_UNKNOWN


def test_empty_bytes_unknown():
    r = classify_uploaded_pdf_doc_type(b'', filename='empty.pdf')
    assert r == DOC_TYPE_UNKNOWN


# ════════════════════════════════════════════════════════════════════════════
# Slot-Pollution: User laed Flugstunden in CAS-Slot hoch
# ════════════════════════════════════════════════════════════════════════════

def test_cas_reader_refuses_flight_hours_in_cas_slot():
    """Flugstunden im CAS-slot wird refused (Phase 3 Fix)."""
    result = app_module._sonnet_read_cas_structured(
        cas_bytes=[b'fake_pdf'],
        source_filenames=['Flugstundenuebersicht_2025.pdf'],
    )
    assert result is not None
    refused = result.get('_refused_files') or []
    assert len(refused) >= 1
    assert refused[0]['doc_type'] == DOC_TYPE_LEGACY_FLUG


def test_cas_reader_refuses_lsb_in_cas_slot(monkeypatch):
    def _fake_classify(_b, filename=''):
        if 'lohnsteuer' in (filename or '').lower():
            return DOC_TYPE_LSB
        return DOC_TYPE_UNKNOWN
    monkeypatch.setattr(app_module, 'classify_uploaded_pdf_doc_type', _fake_classify)
    result = app_module._sonnet_read_cas_structured(
        cas_bytes=[b'fake_pdf'],
        source_filenames=['Lohnsteuerbescheinigung_2025.pdf'],
    )
    refused = result.get('_refused_files') or []
    assert any(r['doc_type'] == DOC_TYPE_LSB for r in refused)


def test_document_health_lists_ignored_legacy_files():
    """Wenn Flugstunden-PDFs hochgeladen wurden, werden sie als ignored aufgelistet."""
    h = app_module._build_v11_upload_health(
        lsb_data={'brutto': 50000},
        se_structured={'se_lines': [{'datum': '2025-01-15', 'storno': False}]},
        cas_classification={'_tage_detail': [{'datum': '2025-01-15'}]},
        ignored_legacy_filenames=['Flugstundenuebersicht.pdf'],
        year=2025)
    assert 'Flugstundenuebersicht.pdf' in h['ignored_legacy_files']
    assert any('Flugstundenuebersicht' in w for w in h['warnings'])


# ════════════════════════════════════════════════════════════════════════════
# Pflicht: 5 finale Kategorien
# ════════════════════════════════════════════════════════════════════════════

def test_doc_type_returns_only_5_categories():
    """Doc-Type-Detection liefert immer einen der 5 finalen Werte."""
    from app import DOC_TYPES_ALL
    tests = [
        (b'', ''),
        (b'', 'random.pdf'),
        (b'\x00' * 100, 'broken.pdf'),
        (b'fake', 'PUB_11.pdf'),
        (b'fake', 'Flugstunden.pdf'),
    ]
    for pdf, name in tests:
        r = classify_uploaded_pdf_doc_type(pdf, filename=name)
        assert r in DOC_TYPES_ALL
