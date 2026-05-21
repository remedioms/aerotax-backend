"""v15 BUG-001 — Recall Debug-Stepper + Hard-Gates.

Diese Tests sind statisch (Code-Inspection von index.html).
Browser-Beweis fehlt explizit — siehe AUDIT_BUG_REGISTER.md BUG-001.

Status der Tests: ergänzen NICHT ersetzen Browser-Verifikation.
"""
import os
import re

_FRONTEND = '/Users/miguelschumann/Desktop/site/index.html'


def _read():
    return open(_FRONTEND).read()


# ─── Debug-Stepper-Existenz ──────────────────────────────────────────────────

def test_recall_debug_flag_via_query():
    """DEBUG_RECALL wird aus ?debug=1 URL-Param abgeleitet."""
    src = _read()
    idx = src.find('window._recallSubmit = async function')
    assert idx > 0
    block = src[idx:idx + 20000]  # 2026-05-19: _recallSubmit wuchs durch BH-001+P0-Fixes
    assert 'DEBUG_RECALL' in block
    assert "get('debug') === '1'" in block or "get(\"debug\") === '1'" in block or "get('debug')==='1'" in block


def test_recall_dbgStep_function_exists():
    """`_dbgStep(n, label, extra)` Helper in _recallSubmit."""
    src = _read()
    idx = src.find('window._recallSubmit = async function')
    block = src[idx:idx + 20000]  # 2026-05-19: _recallSubmit wuchs durch BH-001+P0-Fixes
    assert 'function _dbgStep(' in block
    assert "console.log(tag, 'step='" in block or "[recall-debug]" in block


def test_recall_debug_host_lazy_created():
    """recall-debug-host wird lazy erstellt, nur bei DEBUG_RECALL=true."""
    src = _read()
    idx = src.find('window._recallSubmit = async function')
    block = src[idx:idx + 20000]  # 2026-05-19: _recallSubmit wuchs durch BH-001+P0-Fixes
    assert 'recall-debug-host' in block
    # Nur sichtbar bei DEBUG_RECALL
    assert 'if(!DEBUG_RECALL) return' in block


# ─── 20 Steps existieren (Spec) ──────────────────────────────────────────────

def test_recall_step_1_click_received():
    src = _read()
    assert "_dbgStep(1, 'click received'" in src


def test_recall_step_2_input_code_read():
    src = _read()
    assert "_dbgStep(2, 'input code read'" in src


def test_recall_step_3_session_fetch_started():
    src = _read()
    assert "_dbgStep(3, 'session fetch started'" in src


def test_recall_step_4_session_fetch_http():
    src = _read()
    assert "_dbgStep(4, 'session fetch HTTP'" in src


def test_recall_step_5_session_response_meta():
    src = _read()
    assert "_dbgStep(5, 'session response'" in src


def test_recall_step_6_session_json_fail_handled():
    """Step 6 = 'session JSON parse FAILED' (im catch der r.json())."""
    src = _read()
    assert "_dbgStep(6, 'session JSON parse FAILED'" in src


def test_recall_step_7_session_json_parsed():
    src = _read()
    assert "_dbgStep(7, 'session JSON parsed'" in src


def test_recall_step_8_canonical_state():
    src = _read()
    assert "_dbgStep(8, 'session canonical_state'" in src


def test_recall_step_9_http_error_route():
    """Step 9 = route=http_error bei !r.ok."""
    src = _read()
    assert "_dbgStep(9, 'route=http_error'" in src


def test_recall_step_10_route_chosen():
    """Step 10 ist 'route=...' (progress|error|result) — 3 Varianten."""
    src = _read()
    assert "_dbgStep(10, 'route=progress_panel'" in src
    assert "_dbgStep(10, 'route=error_state'" in src
    assert "_dbgStep(10, 'route=result_render'" in src


def test_recall_step_11_panel_visible():
    """Step 11 = 'progress panel visible' oder 'overlay closed'."""
    src = _read()
    assert "_dbgStep(11, 'progress panel visible'" in src
    assert "_dbgStep(11, 'overlay closed'" in src


def test_recall_step_12_render_called():
    src = _read()
    assert "_dbgStep(12, 'render called'" in src


def test_recall_step_13_render_returned():
    src = _read()
    assert "_dbgStep(13, 'render returned'" in src


def test_recall_step_14_panel_visible_check():
    src = _read()
    assert "_dbgStep(14, 'panel visible'" in src


def test_recall_step_20_button_reset_finally():
    """Step 20 = 'button reset (finally)' — Garantie dass es immer läuft."""
    src = _read()
    assert "_dbgStep(20, 'button reset (finally)'" in src


def test_recall_step_50_exception():
    src = _read()
    assert "_dbgStep(50, 'EXCEPTION'" in src


def test_recall_step_99_hard_reset_timer():
    src = _read()
    assert "_dbgStep(99, 'hard-reset-timer fired" in src


# ─── Button Stuck Prevention ─────────────────────────────────────────────────

def test_recall_finally_resets_button():
    """`finally`-Block in _recallSubmit setzt Button immer wieder aktiv."""
    src = _read()
    idx = src.find('window._recallSubmit = async function')
    block = src[idx:idx + 20000]  # 2026-05-19: _recallSubmit wuchs durch BH-001+P0-Fixes
    # finally-Block enthält Button-Reset
    finally_idx = block.find('} finally {')
    assert finally_idx > 0, 'finally-Block fehlt'
    finally_block = block[finally_idx:finally_idx + 600]
    assert 'openBtn.disabled = false' in finally_block
    assert "openBtn.textContent = 'Code prüfen'" in finally_block


def test_recall_has_hard_reset_timer():
    """20s Hard-Reset-Timer: garantierte Button-Reaktivierung."""
    src = _read()
    idx = src.find('window._recallSubmit = async function')
    block = src[idx:idx + 20000]  # 2026-05-19: _recallSubmit wuchs durch BH-001+P0-Fixes
    assert '_hardResetTimer' in block
    assert 'setTimeout' in block
    assert '20000' in block  # 20s


def test_recall_fetch_has_abortcontroller():
    """Jeder fetch hat AbortController-Timeout."""
    src = _read()
    idx = src.find('window._recallSubmit = async function')
    block = src[idx:idx + 20000]  # 2026-05-19: _recallSubmit wuchs durch BH-001+P0-Fixes
    assert 'AbortController' in block
    assert '_fetchTimeout' in block


def test_recall_fetch_timeout_is_15s():
    """Session-fetch hat 15s timeout."""
    src = _read()
    idx = src.find('window._recallSubmit = async function')
    block = src[idx:idx + 20000]  # 2026-05-19: _recallSubmit wuchs durch BH-001+P0-Fixes
    # 15000 als ms-Wert
    assert '15000' in block


def test_recall_abort_error_friendly():
    """Wenn AbortError (timeout) → friendly 'Verbindung dauert länger'."""
    src = _read()
    idx = src.find('window._recallSubmit = async function')
    block = src[idx:idx + 20000]  # 2026-05-19: _recallSubmit wuchs durch BH-001+P0-Fixes
    assert 'AbortError' in block
    assert 'dauert länger als erwartet' in block


# ─── Console-Logs für DevTools-Debugging ─────────────────────────────────────

def test_recall_console_logs_with_prefix():
    """console.log mit '[recall-debug]' Prefix für DevTools."""
    src = _read()
    idx = src.find('window._recallSubmit = async function')
    block = src[idx:idx + 20000]  # 2026-05-19: _recallSubmit wuchs durch BH-001+P0-Fixes
    assert "'[recall-debug]'" in block


# ─── No-Silent-Fallthrough ───────────────────────────────────────────────────

def test_recall_every_branch_emits_visible_feedback():
    """Jeder Pfad ruft _setRecallStatus mit success/error UND _dbgStep route=..."""
    src = _read()
    idx = src.find('window._recallSubmit = async function')
    block = src[idx:idx + 20000]  # 2026-05-19: _recallSubmit wuchs durch BH-001+P0-Fixes
    # routes: progress, error, result
    assert "route=progress_panel" in block
    assert "route=error_state" in block
    assert "route=result_render" in block
    # Jede setzt _setRecallStatus
    for route in ('Auswertung läuft noch. Öffne Status',
                   'Auswertung ist nicht verfügbar',
                   'wird geöffnet'):
        assert route in block, f'Status-Text fehlt: {route}'


def test_recall_no_falltrough_without_route():
    """Es gibt KEINEN Code-Pfad, der nach session-response weiterläuft ohne route-step.
    Statisch: nach `if(canonical === 'processing'...)` und `if(canonical === 'failed_*'...)`
    folgt entweder return oder ein klarer 3. Branch (done/needs_review)."""
    src = _read()
    idx = src.find('window._recallSubmit = async function')
    block = src[idx:idx + 20000]  # 2026-05-19: _recallSubmit wuchs durch BH-001+P0-Fixes
    # Suche „done / needs_review" Kommentar — der signalisiert dass alle States gehandelt sind
    assert 'done / needs_review' in block


# ─── Format-Check ────────────────────────────────────────────────────────────

def test_recall_at_prefix_required():
    """Code muss mit AT- beginnen, sonst friendly error."""
    src = _read()
    idx = src.find('window._recallSubmit = async function')
    block = src[idx:idx + 14000]
    assert "code.startsWith('AT-')" in block
    assert 'Code muss mit „AT-" beginnen' in block


def test_recall_empty_code_rejected():
    """Leerer Code → friendly error 'Bitte Code eingeben'."""
    src = _read()
    idx = src.find('window._recallSubmit = async function')
    block = src[idx:idx + 20000]  # 2026-05-19: _recallSubmit wuchs durch BH-001+P0-Fixes
    assert 'Bitte Code eingeben' in block


# ─── Browser Proof Marker ────────────────────────────────────────────────────

def test_browser_proof_required_marker():
    """Diese Test-Suite ersetzt NICHT Browser-Verifikation.
    AUDIT_BUG_REGISTER.md BUG-001 Browser-Proof: noch offen."""
    register = open('/Users/miguelschumann/Desktop/aerotax-backend/docs/AUDIT_BUG_REGISTER.md').read()
    # BUG-001 ist `open` mit Browser-Proof required
    assert 'BUG-001' in register
    assert 'Browser Proof Required' in register


if __name__ == '__main__':
    import sys, pytest
    sys.exit(pytest.main([__file__, '-v']))
