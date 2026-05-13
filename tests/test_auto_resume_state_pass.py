"""BUG-009 — Auto-Resume reicht canonical_state an render() weiter.

Vorher rief `_autoResume` window.render({...rd, download_url, notes}) ohne
canonical_state. deriveUiState sah 'unknown', kein Banner, kein State-Kontext.

Fix: render-Aufruf mit allen state-Feldern (canonical_state, reason_code,
pdf_allowed, _review_items, user_title, user_message, next_actions).

Statische Tests — Browser-Beweis siehe AUDIT_BROWSER_QA.md R.5 / R.6.
"""
import os
import re


_FRONTEND = '/Users/miguelschumann/Desktop/site/index.html'


def _read():
    return open(_FRONTEND).read()


def _auto_resume_block():
    """Liefert den _autoResume-Block. Window auf 6000 chars erweitert wegen F-08
    Race-Guard-Insertion + F-10 Timeout-Wrapper + F-14 pollIv-canonical_state-Pass.
    """
    src = _read()
    idx = src.find('async function _autoResume')
    if idx < 0:
        idx = src.find('_autoResume')
    assert idx > 0
    return src[idx:idx + 6000]


# ─── Fix verifiziert: canonical_state wird durchgereicht ─────────────────────

def test_auto_resume_passes_canonical_state():
    block = _auto_resume_block()
    # Render-Aufruf mit canonical_state-Pass
    assert 'canonical_state:' in block, (
        '_autoResume reicht canonical_state nicht an render() weiter'
    )
    assert 'j.canonical_state' in block


def test_auto_resume_passes_pdf_allowed():
    block = _auto_resume_block()
    assert 'pdf_allowed:' in block
    assert 'j.pdf_allowed' in block


def test_auto_resume_passes_reason_code():
    block = _auto_resume_block()
    assert 'reason_code:' in block
    assert 'j.reason_code' in block


def test_auto_resume_passes_review_items():
    block = _auto_resume_block()
    assert '_review_items:' in block, (
        'review_items müssen für deriveUiState verfügbar sein'
    )


def test_auto_resume_passes_user_message():
    block = _auto_resume_block()
    # user_message ist Backend-text der bei needs_review/failed_* genutzt wird
    assert 'user_message:' in block


def test_auto_resume_passes_next_actions():
    block = _auto_resume_block()
    assert 'next_actions:' in block


def test_auto_resume_passes_document_health():
    block = _auto_resume_block()
    assert 'document_health:' in block, (
        'document_health=red blockiert PDF — muss an render() weitergegeben werden'
    )


# ─── Routing bei needs_review trotz hasResult=false ──────────────────────────

def test_auto_resume_routes_needs_review_even_without_netto():
    """Wenn Backend liefert canonical_state='needs_review' aber result_data
    leer (z.B. weil pre-classification) → trotzdem in Result-Panel routen,
    damit User die Klärungs-Hinweise sieht."""
    block = _auto_resume_block()
    assert "j.canonical_state === 'needs_review'" in block


def test_auto_resume_routes_failed_support():
    """Failed_support muss auch ins Result-Panel routen, damit Support-Button
    sichtbar wird."""
    block = _auto_resume_block()
    assert "j.canonical_state === 'failed_support'" in block


# ─── Regression Guard: alte unvollständige Pass-Logik ist weg ────────────────

def test_auto_resume_no_naked_render_call():
    """Der alte Aufruf `window.render({...rd, download_url: j.download_url, notes: ...})`
    OHNE canonical_state darf nicht mehr existieren."""
    block = _auto_resume_block()
    # Suche Render-Aufruf ohne canonical_state
    # Statisch: kein render( das nur 2-3 keys hat
    lines = block.split('\n')
    for i, line in enumerate(lines):
        if 'window.render(' in line and 'function' not in line:
            # Nimm die nächsten 20 Zeilen
            ctx = '\n'.join(lines[i:i+25])
            assert 'canonical_state' in ctx, (
                f'render-Aufruf ohne canonical_state in _autoResume:\n{ctx[:600]}'
            )


# ─── deriveUiState defensiv-Check ────────────────────────────────────────────

def test_derive_ui_state_handles_unknown_safely():
    """deriveUiState mit fehlendem canonical_state setzt sicheren default:
    kein PDF, kein finaler Betrag."""
    src = _read()
    idx = src.find('window.deriveUiState')
    assert idx > 0
    block = src[idx:idx + 4000]
    # Default-Block muss show_pdf_download:false und show_final_amount:false haben
    assert 'show_final_amount:  false' in block or 'show_final_amount: false' in block
    # PDF wird via canShowPdfDownload abgeleitet, das prüft canonical_state !== 'done'
    src_full = _read()
    idx2 = src_full.find('window.canShowPdfDownload')
    block2 = src_full[idx2:idx2 + 2000]
    assert "apiState.canonical_state !== 'done'" in block2


if __name__ == '__main__':
    import sys, pytest
    sys.exit(pytest.main([__file__, '-v']))
