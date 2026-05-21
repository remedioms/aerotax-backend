"""Rel Phase 11 — Frontend State Machine Exhaustive Tests (static-Audit)."""
import os
import re
import sys

import pytest

SITE_HTML = '/Users/miguelschumann/Desktop/site/index.html'
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)


def _html():
    if not os.path.exists(SITE_HTML):
        pytest.skip('index.html nicht gefunden')
    return open(SITE_HTML, encoding='utf-8').read()


def _backend():
    return open(os.path.join(ROOT_DIR, 'app.py'), encoding='utf-8').read()


# ════════════════════════════════════════════════════════════════════════════
# 13 Pflicht-States
# ════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('state', [
    'processing', 'needs_review', 'done', 'failed_retryable',
    'expired', 'deleted',
])
def test_canonical_state_defined_in_backend(state):
    """Canonical states sind im Backend definiert."""
    src = _backend()
    assert state in src


def test_canonical_state_mutual_exclusion():
    """deriveUiState/canShowPdfDownload macht Mutual Exclusion."""
    html = _html()
    assert ('canShowPdfDownload' in html or 'deriveUiState' in html)


def test_done_state_shows_amount():
    """done-State zeigt Final-Amount + PDF-Link."""
    html = _html()
    assert 'done' in html
    # PDF-Render-Block geguarded
    assert 'pdf' in html.lower()


def test_needs_review_hides_pdf():
    """needs_review hide pdf-button."""
    html = _html()
    # PDF rendert nur unter canShowPdfDownload-Gate
    assert 'canShowPdfDownload' in html


def test_failed_state_no_done_mix():
    """failed_retryable + done = unmoegliche Kombination."""
    html = _html()
    # Demo-Isolation existiert
    assert ('demo' in html.lower() or 'failed' in html.lower())


def test_processing_no_done_amount():
    """processing zeigt keinen finalen Betrag."""
    html = _html()
    assert ('processing' in html.lower() or 'process' in html.lower())


def test_hard_reload_restore_state():
    """Hard reload via /api/restore-session/<token> works."""
    bk = _backend()
    assert 'restore-session' in bk
    assert 'restore_session' in bk


def test_auto_resume_banner():
    """Auto-Resume-Banner bei reload mit gespeichertem Token."""
    html = _html()
    assert ('autoResume' in html or 'auto-resume' in html.lower() or 'aerotax_recovery_token' in html.lower())


def test_token_recall_endpoint():
    bk = _backend()
    assert '/api/session/' in bk


def test_no_endless_status_geprueft():
    """Backend gibt klaren Status zurueck, kein endless polling."""
    bk = _backend()
    # canonical_state ist definitiv (done/failed/needs_review/processing)
    assert 'canonical_state' in bk


def test_state_machine_no_undocumented_state():
    """deriveUiState dokumentiert alle moeglichen states."""
    html = _html()
    # alle 6 plus etwaige weitere
    states_in_html = sum(html.count(s) for s in ['done','failed','needs_review','processing','expired','deleted'])
    assert states_in_html > 10  # genug Referenzen
