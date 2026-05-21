"""v11 Clean-Release Phase 3 — CAS Reader refuses non-CAS files.

Verifiziert dass _sonnet_read_cas_structured() Dateien refuset, die nicht als
dienstplan_cas erkannt wurden:
- Flugstundenuebersicht → refused
- LSB im CAS-Slot → refused
- SE im CAS-Slot → refused
- Echter CAS (PUB_/NTF_) → akzeptiert

Spec: Master-Auftrag Phase 3.
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


@pytest.fixture(autouse=True)
def _no_anthropic_key():
    """Verhindere echten Sonnet-Call NUR fuer diese Tests, ohne Session-State zu beruehren."""
    prev = os.environ.pop('ANTHROPIC_API_KEY', None)
    yield
    if prev is not None:
        os.environ['ANTHROPIC_API_KEY'] = prev


def test_cas_reader_refuses_flugstundenuebersicht_by_filename():
    """Datei mit 'Flugstunden' im Filename wird refused."""
    result = app_module._sonnet_read_cas_structured(
        cas_bytes=[b'fake_pdf_bytes'],
        source_filenames=['2025 Flugstundenuebersichten.pdf'],
    )
    assert result is not None
    assert result.get('_files_processed') == 0
    assert result.get('_refused_files'), 'refused_files muss befuellt sein'
    refused = result['_refused_files']
    assert len(refused) == 1
    assert refused[0]['doc_type'] == 'legacy_ignored_flight_hours_summary'
    assert 'Flugstundenuebersicht' in refused[0]['reason']


def test_cas_reader_refuses_lsb_by_filename(monkeypatch):
    """LSB-PDF im CAS-Slot wird refused."""
    # Mock doc-detection so LSB-Text wird erkannt
    def _fake_classify(_b, filename=''):
        if 'lohnsteuer' in (filename or '').lower():
            return 'lohnsteuerbescheinigung'
        return 'unknown'
    monkeypatch.setattr(app_module, 'classify_uploaded_pdf_doc_type', _fake_classify)
    result = app_module._sonnet_read_cas_structured(
        cas_bytes=[b'fake_pdf'],
        source_filenames=['Lohnsteuerbescheinigung_2025.pdf'],
    )
    assert result is not None
    assert result.get('_files_processed') == 0
    assert any('lohnsteuerbescheinigung' in r['doc_type'] for r in result.get('_refused_files') or [])


def test_cas_reader_refuses_se_by_filename(monkeypatch):
    """SE-PDF im CAS-Slot wird refused."""
    def _fake_classify(_b, filename=''):
        if 'streckeneinsatz' in (filename or '').lower():
            return 'streckeneinsatz'
        return 'unknown'
    monkeypatch.setattr(app_module, 'classify_uploaded_pdf_doc_type', _fake_classify)
    result = app_module._sonnet_read_cas_structured(
        cas_bytes=[b'fake_pdf'],
        source_filenames=['Streckeneinsatz_2025.pdf'],
    )
    assert result is not None
    assert result.get('_files_processed') == 0
    assert any(r['doc_type'] == 'streckeneinsatz' for r in result.get('_refused_files') or [])


def test_cas_reader_accepts_pub_file(monkeypatch):
    """PUB_-Datei wird als dienstplan_cas erkannt und nicht refused.
    Mocke den Sonnet-Call so dass kein echter API-Call passiert."""
    def _fake_classify(_b, filename=''):
        if filename.startswith('PUB_'):
            return 'dienstplan_cas'
        return 'unknown'
    monkeypatch.setattr(app_module, 'classify_uploaded_pdf_doc_type', _fake_classify)
    # ANTHROPIC_KEY ist None → CAS-Reader fällt durch ohne echten Call.
    # Wichtig: kein _refused_files mit dem PUB-File.
    result = app_module._sonnet_read_cas_structured(
        cas_bytes=[b'fake_pdf'],
        source_filenames=['PUB_11_2025-10-24.pdf'],
    )
    # Wenn refused_files leer: PUB wurde akzeptiert (auch wenn Sonnet-Call dann scheitert)
    if result is not None:
        refused = result.get('_refused_files') or []
        # PUB-Datei DARF NICHT in refused-Liste sein
        assert not any('PUB_' in r.get('filename', '') for r in refused), \
            f'PUB_-Datei darf nicht refused werden: {refused}'


def test_cas_reader_mixed_refuses_legacy_keeps_cas(monkeypatch):
    """Wenn 2 Dateien hochgeladen: 1 Flugstunden + 1 PUB → Flugstunden refused, PUB behalten."""
    def _fake_classify(_b, filename=''):
        if 'flugstunden' in (filename or '').lower():
            return 'legacy_ignored_flight_hours_summary'
        if filename.startswith('PUB_') or filename.startswith('NTF_'):
            return 'dienstplan_cas'
        return 'unknown'
    monkeypatch.setattr(app_module, 'classify_uploaded_pdf_doc_type', _fake_classify)
    result = app_module._sonnet_read_cas_structured(
        cas_bytes=[b'fake_pdf_1', b'fake_pdf_2'],
        source_filenames=['Flugstundenuebersicht.pdf', 'PUB_11_2025.pdf'],
    )
    if result is not None:
        refused = result.get('_refused_files') or []
        refused_names = [r['filename'] for r in refused]
        assert 'Flugstundenuebersicht.pdf' in refused_names
        assert 'PUB_11_2025.pdf' not in refused_names
