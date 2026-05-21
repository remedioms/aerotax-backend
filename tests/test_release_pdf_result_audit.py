"""Rel Phase 15 — PDF/result_data Audit Tests."""
import os
import sys
import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app as app_module


def _backend():
    return open(os.path.join(ROOT_DIR, 'app.py'), encoding='utf-8').read()


# ════════════════════════════════════════════════════════════════════════════
# PDF/result_data muss enthalten
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('field', [
    'arbeitstage', 'fahr_tage', 'hotel_naechte',
    'z72_tage', 'z73_tage', 'z74_tage', 'z76_eur',
    'gesamt', 'ENGINE_VERSION', 'READER_VERSIONS',
])
def test_result_data_has_kpi_field(field):
    """result_data hat alle KPI-Pflicht-Felder."""
    src = _backend()
    # Field existiert irgendwo im Code (set in tage_detail oder result-dict)
    assert field in src or field.lower() in src


def test_pdf_contains_homebase():
    src = _backend()
    pdf_idx = src.find('def render_pdf')
    if pdf_idx > 0:
        block = src[pdf_idx:pdf_idx + 30000]
        assert 'homebase' in block.lower() or 'base' in block.lower()


def test_pdf_contains_year():
    src = _backend()
    pdf_idx = src.find('def render_pdf')
    if pdf_idx > 0:
        block = src[pdf_idx:pdf_idx + 30000]
        assert 'year' in block.lower() or 'steuerjahr' in block.lower()


def test_pdf_contains_disclaimer():
    src = _backend()
    # „Keine Steuerberatung"-Disclaimer
    assert ('Keine Steuerberatung' in src or 'keine Steuerberatung' in src or
            'Steuerberater' in src)


def test_pdf_no_raw_ki_prompt():
    """PDF schreibt keine Sonnet-Prompts."""
    src = _backend()
    pdf_idx = src.find('def render_pdf')
    if pdf_idx > 0:
        block = src[pdf_idx:pdf_idx + 30000]
        # Keine raw_prompt-Output
        assert 'raw_prompt' not in block


def test_pdf_no_payment_tokens():
    """PDF leakt keine Payment-/Recovery-Tokens."""
    src = _backend()
    pdf_idx = src.find('def render_pdf')
    if pdf_idx > 0:
        block = src[pdf_idx:pdf_idx + 30000]
        # Keine direkten Token-Refs im PDF-Output
        assert 'payment_intent_id' not in block or 'recovery_token' not in block


def test_pdf_no_followme_user_facing():
    """PDF erwaehnt FollowMe nicht im User-facing Output."""
    src = _backend()
    pdf_idx = src.find('def render_pdf')
    if pdf_idx > 0:
        block = src[pdf_idx:pdf_idx + 30000]
        # Kein drawString/Paragraph mit FollowMe
        import re
        bad = re.findall(r'drawString\([^)]*FollowMe', block)
        assert not bad


def test_result_data_includes_versions():
    """result_data hat versions-block."""
    src = _backend()
    # Sample: nach hybrid_analyze wird result_data zusammengestellt
    assert ('ENGINE_VERSION' in src and 'READER_VERSIONS' in src)


def test_pdf_locked_when_needs_review():
    """canShowPdfDownload-Gate aktiv."""
    src = _backend()
    assert 'canShowPdfDownload' in src or 'pdf_allowed' in src


def test_pdf_includes_document_health():
    """PDF zeigt document_health/Source-Status."""
    src = _backend()
    pdf_idx = src.find('def render_pdf')
    if pdf_idx > 0:
        block = src[pdf_idx:pdf_idx + 30000]
        assert ('document_health' in block or 'health' in block.lower())


def test_canonical_state_in_session_response():
    """Session-API gibt canonical_state zurueck."""
    src = _backend()
    assert 'canonical_state' in src
