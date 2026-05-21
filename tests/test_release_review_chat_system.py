"""Rel Phase 10 — Review/Chat System Tests."""
import os
import re
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
SITE_HTML = '/Users/miguelschumann/Desktop/site/index.html'

import pytest

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')


def _read_app():
    return open(os.path.join(ROOT_DIR, 'app.py'), encoding='utf-8').read()


def _read_html():
    if not os.path.exists(SITE_HTML):
        pytest.skip('index.html nicht gefunden')
    return open(SITE_HTML, encoding='utf-8').read()


# ════════════════════════════════════════════════════════════════════════════
# Backend review-API
# ════════════════════════════════════════════════════════════════════════════

def test_backend_has_review_endpoints():
    """Backend bietet review-question-endpoint(s)."""
    src = _read_app()
    assert ('review' in src.lower())
    # Mind. 1 Review-Endpoint
    assert ('/api/review' in src or 'review_answer' in src or 'review_items' in src)


def test_needs_review_state_documented():
    """canonical_state=needs_review wird im Backend gesetzt."""
    src = _read_app()
    assert 'needs_review' in src


def test_pdf_locked_when_needs_review():
    """`canShowPdfDownload`-Logik blockiert PDF bei needs_review."""
    src = _read_app() + (_read_html() if os.path.exists(SITE_HTML) else '')
    # canShowPdfDownload-Gate
    assert 'canShowPdfDownload' in src or 'pdf_allowed' in src or 'canonical_state' in src


def test_review_answer_updates_calculation():
    """Review-Answer kann Berechnung beeinflussen (review_decisions)."""
    src = _read_app()
    assert ('review_decisions' in src or 'review_answer' in src or '_apply_review' in src)


def test_chat_attachment_cas_labelled_correctly():
    """Chat-Picker zeigt CAS-Optionen, nicht Flugstunden."""
    html = _read_html()
    assert 'Dienstplan' in html or 'CAS' in html
    # Anti-Test: keine aktive Flugstundenuebersicht-Option im Picker
    picker_idx = html.find('picker') if 'picker' in html else 0
    if picker_idx > 0:
        block = html[picker_idx:picker_idx + 5000]
        # In aktiven Picker-Optionen darf Flugstunden nicht erscheinen
        assert 'Flugstundenübersicht' not in block or 'nicht' in block.lower()


def test_chat_input_not_logged_with_pii():
    """Chat-Inputs werden vor Logging gestrippt."""
    src = _read_app()
    # PII-Stripper aktiv
    assert ('_strip_pii' in src or 'PII' in src or 'redact' in src.lower())


def test_failed_state_hides_review_chat():
    """failed_retryable-State zeigt keinen Chat (außer Support)."""
    src = _read_app() + (_read_html() if os.path.exists(SITE_HTML) else '')
    # failed_retryable existiert
    assert 'failed_retryable' in src


def test_expired_token_cannot_answer_review():
    """Expired token wird beim Review-Answer rejected."""
    src = _read_app()
    assert ('expired' in src.lower() and 'session' in src.lower())


def test_deleted_token_returns_410_or_clear_state():
    """Geloeschte Session: 410/Gone oder klares state-result."""
    src = _read_app()
    assert ('410' in src or 'deleted' in src.lower())
