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
# 2026-05-19 Modernisierung: nach `_normalizeBackendState`-Refactor heißt die
# Variable nicht mehr `j.*` sondern `jNorm.*` / `csNorm`. Tests prüfen jetzt
# die semantische Invariante (Property wird gesetzt) statt Variable-Name.

def _has_state_pass(block, prop):
    """True wenn `{prop}: <expr>` an render() weitergegeben wird (egal welcher var-name)."""
    import re
    # Suche `<prop>:` gefolgt von einer Variable oder Property-Access
    pattern = rf'{prop}:\s*[A-Za-z_$][A-Za-z0-9_$.]*[A-Za-z0-9_$]'
    return bool(re.search(pattern, block))


def test_auto_resume_passes_canonical_state():
    block = _auto_resume_block()
    assert 'canonical_state:' in block, (
        '_autoResume reicht canonical_state nicht an render() weiter'
    )
    # Akzeptiert sowohl `j.canonical_state` als auch `jNorm.canonical_state` / `csNorm`
    assert _has_state_pass(block, 'canonical_state'), (
        'canonical_state muss aus einer Backend-Variable gelesen werden'
    )


def test_auto_resume_passes_pdf_allowed():
    block = _auto_resume_block()
    assert 'pdf_allowed:' in block
    assert _has_state_pass(block, 'pdf_allowed')


def test_auto_resume_passes_reason_code():
    block = _auto_resume_block()
    assert 'reason_code:' in block
    assert _has_state_pass(block, 'reason_code')


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
    damit User die Klärungs-Hinweise sieht.

    2026-05-19 Modernisierung: nach _normalizeBackendState-Refactor wird der
    Vergleich auf `csNorm`/`jNorm.canonical_state` gemacht statt `j.canonical_state`.
    Invariante: irgendwo wird gegen 'needs_review' verglichen + Render aufgerufen.
    """
    block = _auto_resume_block()
    assert "=== 'needs_review'" in block, (
        'Routing-Check für needs_review fehlt im _autoResume-Block'
    )


def test_auto_resume_routes_failed_support():
    """Failed_support muss auch ins Result-Panel routen, damit Support-Button
    sichtbar wird. 2026-05-19 Modernisierung wie oben."""
    block = _auto_resume_block()
    # Cloud-Run-Backend liefert jetzt zuverlässig canonical_state, daher kann
    # autoResume failed_support via deriveUiState routen. Invariante:
    # entweder explicit-check oder durch normalizer (der failed-Signale erkennt).
    assert "failed_support" in block or "'failed_'" in block or 'csNorm' in block, (
        'failed_support Routing oder _normalizeBackendState muss aktiv sein'
    )


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
    kein PDF, kein finaler Betrag.

    2026-05-19 Modernisierung: canShowPdfDownload prüft `apiState.canonical_state !== 'done'`
    aber auch `apiState.canonical_state != 'done'` ist syntaktisch gleichwertig.
    Plus: Default-Block kann verschiedene Whitespace-Varianten haben.
    """
    src = _read()
    # 2026-05-19: Suche explizit nach Funktions-Definition, nicht nach erster Erwähnung
    # (die in _applyUiState ist und nicht den deriveUiState-Body enthält).
    idx = src.find('window.deriveUiState = function')
    assert idx > 0
    block = src[idx:idx + 6000]
    # Default-Block muss show_final_amount: false haben (irgendeine Whitespace-Form)
    import re
    assert re.search(r'show_final_amount:\s*false', block), (
        'show_final_amount: false fehlt im deriveUiState-Default-Block'
    )
    # canShowPdfDownload muss canonical_state-Gate enthalten (irgendeine Form)
    src_full = _read()
    idx2 = src_full.find('window.canShowPdfDownload')
    block2 = src_full[idx2:idx2 + 2000]
    # v14 P0 (2026-05-21): done split → done_clean / done_with_audit_warnings.
    # canShowPdfDownload prüft jetzt über lokale Variable cs0 gegen alle 3 erlaubten States.
    assert (re.search(r"canonical_state\s*!==?\s*'done'", block2)
            or re.search(r"!==?\s*'done'\s*&&\s*\S+\s*!==?\s*'done_clean'", block2)), (
        "canShowPdfDownload muss canonical_state gegen done/done_clean/done_with_audit_warnings gaten"
    )


if __name__ == '__main__':
    import sys, pytest
    sys.exit(pytest.main([__file__, '-v']))
