"""v11 Clean-Release Phase 1 — Document Type Detection.

Verifiziert classify_uploaded_pdf_doc_type():
- 5 finale Kategorien sind als Modul-Konstanten exportiert.
- PUB_/NTF_-Dateinamen → dienstplan_cas (ohne PDF-Text noetig).
- Flugstundenuebersicht-Dateinamen → legacy_ignored_flight_hours_summary.
- LSB-Text-Marker → lohnsteuerbescheinigung.
- Streckeneinsatz-Text-Marker → streckeneinsatz.
- Briefingzeit / Roster-Text → dienstplan_cas.
- Leere / unbekannte Inhalte → unknown.
- Flight-Hours-Summary-Inhalt → legacy_ignored_flight_hours_summary.

Spec: docs/FLUGSTUNDEN_LEGACY_PURGE.md + Master-Auftrag Phase 1.
"""
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app as app_module
from app import (
    classify_uploaded_pdf_doc_type,
    DOC_TYPE_LSB, DOC_TYPE_SE, DOC_TYPE_CAS,
    DOC_TYPE_LEGACY_FLUG, DOC_TYPE_UNKNOWN, DOC_TYPES_ALL,
)


def test_doc_type_constants_are_final_five():
    expected = (
        'lohnsteuerbescheinigung',
        'streckeneinsatz',
        'dienstplan_cas',
        'legacy_ignored_flight_hours_summary',
        'unknown',
    )
    assert DOC_TYPES_ALL == expected, \
        f'DOC_TYPES_ALL muss exakt die 5 finalen Kategorien sein: {expected}'
    assert DOC_TYPE_LSB == 'lohnsteuerbescheinigung'
    assert DOC_TYPE_SE == 'streckeneinsatz'
    assert DOC_TYPE_CAS == 'dienstplan_cas'
    assert DOC_TYPE_LEGACY_FLUG == 'legacy_ignored_flight_hours_summary'
    assert DOC_TYPE_UNKNOWN == 'unknown'


# ════════════════════════════════════════════════════════════════════════════
# Filename-based detection
# ════════════════════════════════════════════════════════════════════════════

def test_pub_filename_is_dienstplan_cas():
    """PUB_11_1_0_2025-10-24.pdf → dienstplan_cas (Pub-Plan)."""
    r = classify_uploaded_pdf_doc_type(b'', filename='PUB_11_1_0_2025-10-24.pdf')
    assert r == DOC_TYPE_CAS


def test_ntf_filename_is_dienstplan_cas():
    """NTF_8_1_1_2025-07-30.pdf → dienstplan_cas (Update-Notification)."""
    r = classify_uploaded_pdf_doc_type(b'', filename='NTF_8_1_1_2025-07-30.pdf')
    assert r == DOC_TYPE_CAS


def test_flugstunden_filename_is_legacy_ignored():
    """Flugstundenübersicht.pdf → legacy_ignored_flight_hours_summary."""
    r = classify_uploaded_pdf_doc_type(b'', filename='2025 Flugstundenübersichten.pdf')
    assert r == DOC_TYPE_LEGACY_FLUG


def test_stundenuebersicht_filename_is_legacy_ignored():
    r = classify_uploaded_pdf_doc_type(b'', filename='Stundenuebersicht_2025.pdf')
    assert r == DOC_TYPE_LEGACY_FLUG


def test_unknown_filename_no_content_is_unknown():
    r = classify_uploaded_pdf_doc_type(b'', filename='random_pdf_2025.pdf')
    assert r == DOC_TYPE_UNKNOWN


# ════════════════════════════════════════════════════════════════════════════
# Content-based detection (via mock pdfplumber by patching extraction)
# ════════════════════════════════════════════════════════════════════════════

def _patch_text(monkeypatch, text):
    """Monkeypatch pdfplumber.open to return a fake PDF with given text."""
    class _FakePage:
        def __init__(self, t):
            self._t = t
        def extract_text(self):
            return self._t
    class _FakePDF:
        def __init__(self, t):
            self.pages = [_FakePage(t)]
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    import pdfplumber as _pp
    monkeypatch.setattr(_pp, 'open', lambda _b: _FakePDF(text))


def test_lsb_text_detected(monkeypatch):
    _patch_text(monkeypatch, 'Lohnsteuerbescheinigung 2025 Brutto 60000 EUR')
    r = classify_uploaded_pdf_doc_type(b'fakepdf', filename='lsb.pdf')
    assert r == DOC_TYPE_LSB


def test_lsb_via_steuer_id(monkeypatch):
    _patch_text(monkeypatch, 'Steueridentifikationsnummer 12345678901 Jahr 2025')
    r = classify_uploaded_pdf_doc_type(b'fakepdf', filename='unbekannt.pdf')
    assert r == DOC_TYPE_LSB


def test_streckeneinsatz_text_detected(monkeypatch):
    _patch_text(monkeypatch, 'Streckeneinsatz-Abrechnung Januar 2025 stfrei 25.00 EUR')
    r = classify_uploaded_pdf_doc_type(b'fakepdf', filename='se_jan.pdf')
    assert r == DOC_TYPE_SE


def test_streckeneinsatz_via_einsatzabrechnung(monkeypatch):
    _patch_text(monkeypatch, 'Einsatzabrechnung Februar 2025 Pauschale 12.00')
    r = classify_uploaded_pdf_doc_type(b'fakepdf', filename='abrechnung.pdf')
    assert r == DOC_TYPE_SE


def test_flugstundenuebersicht_content_detected(monkeypatch):
    _patch_text(monkeypatch,
                'FLUGSTUNDEN-ÜBERSICHT 2025\n'
                '15.10. LH123 A FRA-DUB 06:30')
    r = classify_uploaded_pdf_doc_type(b'fakepdf', filename='random.pdf')
    assert r == DOC_TYPE_LEGACY_FLUG


def test_dienstplan_briefingzeit_detected(monkeypatch):
    _patch_text(monkeypatch,
                'Sa 21 128322 PU\n'
                'Briefingzeit(LT FRA): 21/06/25 11:20\n'
                'LH828-1 A320 FRA 10:45-12:10 CPH')
    r = classify_uploaded_pdf_doc_type(b'fakepdf', filename='unbekannt.pdf')
    assert r == DOC_TYPE_CAS


def test_empty_pdf_no_filename_is_unknown(monkeypatch):
    _patch_text(monkeypatch, '')
    r = classify_uploaded_pdf_doc_type(b'fakepdf', filename='')
    assert r == DOC_TYPE_UNKNOWN


def test_filename_pub_overrides_text_content(monkeypatch):
    """PUB_-Dateiname gewinnt gegen Flugstundenuebersicht-Inhalt (defensive)."""
    _patch_text(monkeypatch, 'FLUGSTUNDEN-ÜBERSICHT')
    r = classify_uploaded_pdf_doc_type(b'fakepdf', filename='PUB_11_2025.pdf')
    assert r == DOC_TYPE_CAS


def test_filename_flugstunden_overrides_text(monkeypatch):
    """Flugstunden-Dateiname gewinnt gegen Lohnsteuer-Inhalt (defensive)."""
    _patch_text(monkeypatch, 'Lohnsteuerbescheinigung 2025')
    r = classify_uploaded_pdf_doc_type(b'fakepdf', filename='Flugstundenuebersicht.pdf')
    assert r == DOC_TYPE_LEGACY_FLUG


# ════════════════════════════════════════════════════════════════════════════
# Anti-Hallucination / Robustness
# ════════════════════════════════════════════════════════════════════════════

def test_returns_one_of_five_categories_always(monkeypatch):
    """Niemals NIEMALS andere Werte als die 5 finalen Kategorien zurueckgeben."""
    test_inputs = [
        (b'', ''),
        (b'', 'random.pdf'),
        (b'\x00' * 100, 'broken.pdf'),
    ]
    for pdf, name in test_inputs:
        r = classify_uploaded_pdf_doc_type(pdf, filename=name)
        assert r in DOC_TYPES_ALL, f'Unbekannter Doc-Typ zurueckgegeben: {r}'


def test_pdf_extract_failure_falls_back_gracefully(monkeypatch):
    """pdfplumber-Crash darf NICHT die Detection krachen lassen."""
    def _boom(_b):
        raise RuntimeError('pdfplumber crash')
    import pdfplumber as _pp
    monkeypatch.setattr(_pp, 'open', _boom)
    r = classify_uploaded_pdf_doc_type(b'broken', filename='random.pdf')
    assert r == DOC_TYPE_UNKNOWN  # Fallback bei Extract-Fail
    # Aber: wenn Filename-Hint existiert, kann der noch greifen
    r2 = classify_uploaded_pdf_doc_type(b'broken', filename='PUB_11.pdf')
    assert r2 == DOC_TYPE_CAS
