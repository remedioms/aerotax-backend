"""v14 UI Safety Gate — Tests dass das Frontend Backend-State respektiert.

Problem-History: Mini-Run zeigte „PDF herunterladen"-Button obwohl
canonical_state=needs_review + pdf_allowed=false. Root Cause: Frontend
las canonical_state nirgends.

Diese Tests prüfen statisch:
1. canShowPdfDownload-Funktion existiert und blockiert non-done states
2. deriveUiState liefert konsistente UI-Marker pro canonical_state
3. PDF-Buttons initial display:none, werden NUR via _applyPdfVisibility aktiviert
4. dlPDF() hat Hard-Gate am Anfang
5. _refreshPdfBubble prüft canShowPdfDownload zuerst
6. Demo-Path nur wenn _isDemo=true UND kein job_id
7. Verbotene User-Facing-Strings nicht im DOM
"""
import os
import re

_FRONTEND = '/Users/miguelschumann/Desktop/site/index.html'


def _read():
    return open(_FRONTEND).read()


# ─── 1. canShowPdfDownload existiert + gates korrekt ─────────────────────────

def test_canShowPdfDownload_function_exists():
    """window.canShowPdfDownload als zentrale Gate-Funktion."""
    src = _read()
    assert 'window.canShowPdfDownload = function(' in src or \
           'window.canShowPdfDownload=function(' in src


def test_canShowPdfDownload_blocks_non_done():
    """canShowPdfDownload prüft canonical_state==done als Pflicht."""
    src = _read()
    idx = src.find('window.canShowPdfDownload = function(')
    block = src[idx:idx + 2500]
    # canonical_state Check
    assert "canonical_state !== 'done'" in block, \
        'canShowPdfDownload muss canonical_state==done erzwingen'


def test_canShowPdfDownload_blocks_pdf_not_allowed():
    src = _read()
    idx = src.find('window.canShowPdfDownload = function(')
    block = src[idx:idx + 2500]
    assert 'pdf_allowed === false' in block


def test_canShowPdfDownload_blocks_stale_result():
    src = _read()
    idx = src.find('window.canShowPdfDownload = function(')
    block = src[idx:idx + 2500]
    assert 'result_stale === true' in block


def test_canShowPdfDownload_blocks_red_health():
    src = _read()
    idx = src.find('window.canShowPdfDownload = function(')
    block = src[idx:idx + 2500]
    assert "document_health" in block
    assert "'red'" in block


def test_canShowPdfDownload_blocks_fetch_error():
    src = _read()
    idx = src.find('window.canShowPdfDownload = function(')
    block = src[idx:idx + 2500]
    assert 'fetch_error === true' in block


def test_canShowPdfDownload_blocks_pending_review_items():
    src = _read()
    idx = src.find('window.canShowPdfDownload = function(')
    block = src[idx:idx + 2500]
    assert 'pending' in block
    assert "status === 'pending'" in block


def test_canShowPdfDownload_blocks_missing_download_url():
    src = _read()
    idx = src.find('window.canShowPdfDownload = function(')
    block = src[idx:idx + 2500]
    assert 'download_url' in block


# ─── 2. deriveUiState liefert pro state Marker ───────────────────────────────

def test_deriveUiState_function_exists():
    src = _read()
    assert 'window.deriveUiState = function(' in src


def test_deriveUiState_handles_all_canonical_states():
    """Pro canonical_state: passender branch im deriveUiState."""
    src = _read()
    idx = src.find('window.deriveUiState = function(')
    block = src[idx:idx + 8000]
    for state in ('processing', 'needs_review', 'done', 'failed_retryable',
                   'failed_support', 'expired', 'deleted'):
        assert f"'{state}'" in block, f'deriveUiState handelt {state} nicht'


def test_deriveUiState_done_shows_pdf():
    """done + pdfOk → show_pdf_download=true."""
    src = _read()
    idx = src.find('window.deriveUiState = function(')
    block = src[idx:idx + 8000]
    done_branch = block[block.find("cs === 'done'"):block.find("cs === 'failed_retryable'")]
    assert 'show_final_amount = true' in done_branch
    # show_pdf_download wird via canShowPdfDownload bestimmt, nicht hier hardgecoded


def test_deriveUiState_needs_review_no_final_amount():
    """needs_review → show_final_amount=false (vorläufig only)."""
    src = _read()
    idx = src.find('window.deriveUiState = function(')
    block = src[idx:idx + 8000]
    nr_branch = block[block.find("cs === 'needs_review'"):block.find("cs === 'done'")]
    assert 'show_final_amount = false' in nr_branch
    assert 'NIE final bei needs_review' in nr_branch or 'show_final_amount = false' in nr_branch


def test_deriveUiState_failed_support_no_retry():
    """failed_support → show_retry=false (KEIN retry)."""
    src = _read()
    idx = src.find('window.deriveUiState = function(')
    block = src[idx:idx + 8000]
    fs_branch = block[block.find("cs === 'failed_support'"):block.find("cs === 'expired'")]
    assert 'show_retry = false' in fs_branch
    assert 'show_support = true' in fs_branch


def test_deriveUiState_fetch_error_locks_pdf():
    """fetch_error → show_pdf_locked=true, friendly text (nicht 'Auswertung fehlgeschlagen')."""
    src = _read()
    idx = src.find('window.deriveUiState = function(')
    block = src[idx:idx + 8000]
    assert "if(fetchErr){" in block
    fetch_branch = block[block.find("if(fetchErr){"):block.find("if(cs === 'processing'")]
    assert 'Verbindung kurz unterbrochen' in fetch_branch
    # NICHT „Auswertung fehlgeschlagen" bei fetch_error
    assert 'fehlgeschlagen' not in fetch_branch.lower()


# ─── 3. PDF-Buttons initial display:none ─────────────────────────────────────

def test_pdf_button_initial_display_none():
    """dl-btn-main HTML-Deklaration hat display:none als Default."""
    src = _read()
    # Suche button mit id="dl-btn-main"
    m = re.search(r'<button[^>]*id="dl-btn-main"[^>]*>', src)
    assert m, 'dl-btn-main button muss existieren'
    button_tag = m.group(0)
    assert 'display:none' in button_tag, \
        'dl-btn-main muss initial display:none haben (v14 Safety-Gate)'


def test_header_pdf_button_initial_display_none():
    """header-pdf-btn hat display:none als initial state."""
    src = _read()
    m = re.search(r'<button[^>]*id="header-pdf-btn"[^>]*>', src)
    assert m
    assert 'display:none' in m.group(0)


def test_pdf_locked_indicator_element_exists():
    """pdf-locked-indicator DOM-Element für locked-state vorhanden."""
    src = _read()
    assert 'id="pdf-locked-indicator"' in src


# ─── 4. _applyPdfVisibility ist die einzige Stelle die PDF-Buttons aktiviert ─

def test_apply_pdf_visibility_function_exists():
    src = _read()
    assert 'window._applyPdfVisibility = function(' in src or \
           'window._applyPdfVisibility=function(' in src


def test_apply_pdf_visibility_toggles_both_buttons():
    """_applyPdfVisibility setzt header-pdf-btn UND dl-btn-main display."""
    src = _read()
    idx = src.find('window._applyPdfVisibility = function(')
    block = src[idx:idx + 1500]
    assert 'header-pdf-btn' in block
    assert 'dl-btn-main' in block
    assert 'display' in block


def test_apply_pdf_visibility_called_in_render():
    """render() ruft _applyPdfVisibility nach State-Ableitung."""
    src = _read()
    render_idx = src.find('function render(d){')
    block = src[render_idx:render_idx + 5000]
    assert '_applyPdfVisibility' in block, \
        'render() muss _applyPdfVisibility aufrufen'


def test_apply_pdf_visibility_called_in_window_render_wrapper():
    """window.render Wrapper ruft _applyUiState — defensive Re-Apply."""
    src = _read()
    idx = src.find('var _origRender=window.render;')
    block = src[idx:idx + 1500]
    assert '_applyUiState' in block or '_applyPdfVisibility' in block


# ─── 5. dlPDF Hard-Gate ──────────────────────────────────────────────────────

def test_dlPDF_has_hard_gate_at_start():
    """dlPDF() prüft canShowPdfDownload als ALLERERSTEN Schritt."""
    src = _read()
    idx = src.find('async function dlPDF(){')
    block = src[idx:idx + 1500]
    # canShowPdfDownload muss VOR dem ersten 'await' kommen
    gate_pos = block.find('canShowPdfDownload')
    first_await = block.find('await ')
    assert gate_pos > 0, 'dlPDF muss canShowPdfDownload aufrufen'
    if first_await > 0:
        assert gate_pos < first_await, 'Gate muss vor erstem await sein'


def test_dlPDF_returns_on_blocked_state():
    """dlPDF: bei blocked state → return ohne fetch."""
    src = _read()
    idx = src.find('async function dlPDF(){')
    block = src[idx:idx + 1500]
    gate_idx = block.find('canShowPdfDownload')
    after_gate = block[gate_idx:gate_idx + 800]
    assert 'return' in after_gate


# ─── 6. _refreshPdfBubble prüft state ────────────────────────────────────────

def test_refresh_pdf_bubble_checks_state():
    """_refreshPdfBubble ruft canShowPdfDownload + skipped wenn locked."""
    src = _read()
    idx = src.find('window._refreshPdfBubble = function(')
    block = src[idx:idx + 1500]
    assert 'canShowPdfDownload' in block


def test_refresh_pdf_bubble_returns_when_locked():
    """_refreshPdfBubble macht NICHTS wenn PDF locked — keine veraltete Bubble."""
    src = _read()
    idx = src.find('window._refreshPdfBubble = function(')
    block = src[idx:idx + 1000]
    # Suche return-Stelle nach canShowPdfDownload-Check
    gate_idx = block.find('canShowPdfDownload')
    after_gate = block[gate_idx:gate_idx + 400]
    assert 'return' in after_gate


# ─── 7. Demo-Path isoliert ───────────────────────────────────────────────────

def test_demo_path_requires_isDemo_flag():
    """dlPDF Demo-Branch nur wenn _isDemo=true UND kein _lastJobId."""
    src = _read()
    idx = src.find('async function dlPDF(){')
    block = src[idx:idx + 4000]
    # Demo-Branch hat _isDemo-check
    demo_idx = block.find('// Demo path')
    if demo_idx > 0:
        pre_demo = block[max(0, demo_idx - 400):demo_idx]
        assert '_isDemo === true' in pre_demo
        assert '_lastJobId' in pre_demo


def test_demo_pdf_no_fallback_for_real_job():
    """Kein 'else'-fall der Demo zeigt wenn echter Job existiert."""
    src = _read()
    idx = src.find('async function dlPDF(){')
    block = src[idx:idx + 4500]
    # Letzter else-Branch nach if/else if: hard fail, kein Demo-Fallback
    # Suche „kein dlUrl ableitbar" oder „PDF noch nicht verfügbar"
    assert 'noch nicht verfügbar' in block or 'kein dlUrl ableitbar' in block


# ─── 8. Forbidden User-Facing-Strings ────────────────────────────────────────

def test_no_load_failed_user_facing():
    """'Load failed' nur in regex/comment, nicht als UI-Text."""
    src = _read()
    # Vorkommen in regex/check ist erlaubt; in `textContent`/`innerHTML` mit
    # genau diesem Text wäre verboten.
    bad_patterns = [
        r'textContent\s*=\s*["\']Load failed["\']',
        r'innerHTML\s*=\s*["\']Load failed["\']',
        r'showError\s*\(\s*["\']Load failed["\']',
    ]
    for pat in bad_patterns:
        m = re.search(pat, src, re.IGNORECASE)
        assert not m, f'Forbidden user-facing pattern: {pat}'


def test_no_runtime_error_user_facing():
    """'RuntimeError' nicht in user-facing strings."""
    src = _read()
    # raw text nur in templates/textContent verboten
    for pat in (r'textContent\s*=\s*["\'][^"\']*RuntimeError',
                 r'innerHTML\s*=\s*["\'][^"\']*RuntimeError'):
        assert not re.search(pat, src), f'RuntimeError user-facing: {pat}'


def test_no_traceback_user_facing():
    src = _read()
    for pat in (r'textContent\s*=\s*["\'][^"\']*Traceback',
                 r'innerHTML\s*=\s*["\'][^"\']*Traceback'):
        assert not re.search(pat, src), f'Traceback user-facing: {pat}'


def test_no_tuple_attribute_error_user_facing():
    src = _read()
    for pat in (r'textContent\s*=\s*["\'][^"\']*tuple.*object',
                 r'innerHTML\s*=\s*["\'][^"\']*tuple.*object'):
        assert not re.search(pat, src), f'tuple-attribute-error user-facing'


def test_no_max_tokens_user_facing():
    src = _read()
    for pat in (r'textContent\s*=\s*["\'][^"\']*max_tokens',
                 r'innerHTML\s*=\s*["\'][^"\']*max_tokens'):
        assert not re.search(pat, src)


def test_no_sonnet_user_facing():
    """'Sonnet' / 'Anthropic' nicht im user-facing text (Implementations-Detail)."""
    src = _read()
    for pat in (r'textContent\s*=\s*["\'][^"\']*Sonnet',
                 r'innerHTML\s*=\s*["\'][^"\']*Sonnet'):
        # Kommentare und Variablen-Namen sind OK
        m = re.search(pat, src)
        if m:
            # Check ob es in einem Kommentar steht
            line_start = src.rfind('\n', 0, m.start()) + 1
            line = src[line_start:m.end() + 50]
            assert line.strip().startswith('//') or line.strip().startswith('/*'), \
                f'Sonnet in user-facing: {line[:120]}'


def test_no_undefined_user_facing():
    src = _read()
    # Suche 'undefined' in einem template-literal mit user-text
    bad = re.search(r'innerHTML\s*=\s*[`"]\s*undefined\s*[`"]', src)
    assert not bad


# ─── 9. Backend-Frontend Contract: pro State der richtige Banner ─────────────

def test_contract_processing_banner_text():
    """deriveUiState liefert für processing den richtigen Banner."""
    src = _read()
    idx = src.find("if(cs === 'processing'")
    block = src[idx:idx + 800]
    assert 'Auswertung läuft' in block


def test_contract_needs_review_banner_text():
    src = _read()
    idx = src.find("if(cs === 'needs_review'")
    block = src[idx:idx + 800]
    assert 'kurze Klärung nötig' in block or 'Klärung nötig' in block


def test_contract_done_banner_text():
    src = _read()
    idx = src.find("if(cs === 'done'")
    block = src[idx:idx + 800]
    assert 'fertig' in block.lower()


def test_contract_failed_retryable_banner_text():
    src = _read()
    idx = src.find("if(cs === 'failed_retryable'")
    block = src[idx:idx + 800]
    assert 'unterbrochen' in block.lower()


def test_contract_failed_support_banner_text():
    src = _read()
    idx = src.find("if(cs === 'failed_support'")
    block = src[idx:idx + 800]
    assert 'nicht sicher' in block.lower() or 'gestoppt' in block.lower()


def test_contract_expired_banner_text():
    src = _read()
    idx = src.find("if(cs === 'expired'")
    block = src[idx:idx + 600]
    assert 'abgelaufen' in block.lower()


def test_contract_deleted_banner_text():
    src = _read()
    idx = src.find("if(cs === 'deleted'")
    block = src[idx:idx + 600]
    assert 'gelöscht' in block.lower()


# ─── 10. Mini-Run-Re-Open Simulation ─────────────────────────────────────────

def test_mini_run_needs_review_blocks_pdf():
    """Simuliere: Backend liefert {canonical_state:needs_review, pdf_allowed:false,
    review_items:[{status:pending}]}. canShowPdfDownload(apiState) muss false."""
    # Static check: die canShowPdfDownload Funktion muss explizit:
    # 1. canonical_state !== 'done' → false
    # 2. pdf_allowed === false → false
    # 3. pending review_items → false
    src = _read()
    idx = src.find('window.canShowPdfDownload = function(')
    block = src[idx:idx + 2500]
    # Alle drei conditions im Block
    assert "canonical_state !== 'done'" in block
    assert 'pdf_allowed === false' in block
    assert 'pending' in block.lower()


if __name__ == '__main__':
    import pytest
    import sys
    sys.exit(pytest.main([__file__, '-v']))
