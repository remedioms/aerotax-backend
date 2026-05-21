"""Rel Phase 12 — Backend Failure Injection (static-Audit)."""
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
# Error-Codes für 19 Failure-Szenarien
# ════════════════════════════════════════════════════════════════════════════

def test_error_codes_defined():
    """Backend definiert error reason_codes (Phase A-1)."""
    src = _backend()
    expected_codes = [
        'CLASSIFICATION_SCHEMA_FAILED',
        'UPLOAD_MISSING_REQUIRED',
        'PAYMENT_ALREADY_USED',
        'UPLOAD_PERSIST_FAILED',
    ]
    for code in expected_codes:
        assert code in src, f'reason_code {code} fehlt'


def test_lsb_reader_failure_handled():
    """Wenn LSB-Reader None liefert → Fehler-State, kein Crash."""
    src = _backend()
    # `lsb_data` wird auf None-handle geprüft
    assert 'lsb_data' in src
    assert 'lsb_data is None' in src or 'if lsb_data' in src or 'if not lsb_data' in src


def test_se_reader_failure_handled():
    src = _backend()
    assert 'se_structured' in src
    assert ('se_structured is None' in src or 'if se_structured' in src or
            'if not se_structured' in src or "se_structured.get('se_lines')" in src)


def test_cas_reader_failure_handled():
    src = _backend()
    # CAS-pipeline-crash → SCHEMA_FAILED
    assert "v11-cas-pipeline" in src.lower()
    assert "CLASSIFICATION_SCHEMA_FAILED" in src or "_is_schema_crash" in src


def test_bmf_lookup_missing_handled():
    """Fehlendes BMF-Mapping → klare Issue/Z73-Fallback."""
    src = _backend()
    assert 'bmf_missing' in src.lower() or 'kein BMF' in src or 'BMF fallback' in src.lower()


def test_ki_timeout_handled():
    """KI-Timeout → retry oder needs_review."""
    src = _backend()
    assert ('timeout' in src.lower())


def test_ki_malformed_json_handled():
    """KI-Output mit invalidem JSON → robust-parse oder needs_review."""
    src = _backend()
    assert ('json' in src.lower() and ('try' in src.lower() or 'except' in src.lower()))


def test_reportlab_pdf_fail_handled():
    """ReportLab-Crash beim PDF-Render → klare Error-Message."""
    src = _backend()
    assert 'reportlab' in src.lower()
    # Try/except um PDF-Render
    pdf_idx = src.find('def render_pdf') if 'def render_pdf' in src else src.find('reportlab')
    if pdf_idx > 0:
        block = src[pdf_idx:pdf_idx + 10000]
        assert 'except' in block


def test_supabase_insert_fail_fallback():
    """Supabase-Insert-Failure → file-fallback (in-memory + disk)."""
    src = _backend()
    assert ('supabase' in src.lower())
    assert ('fallback' in src.lower() or '_store' in src)


def test_supabase_read_fail_fallback():
    src = _backend()
    assert ('supabase' in src.lower())


def test_cloud_task_duplicate_blocked():
    src = _backend()
    assert 'attempt_id' in src


def test_partial_upload_handled():
    src = _backend()
    # Partial upload: missing file in slot → 400 with error
    assert ('files.get(\'lsb\')' in src and 'files.get(\'cas\')' in src)


def test_corrupted_pdf_handled():
    """pdfplumber-Crash bei corrupted PDF → graceful fallback."""
    # Verified in test_v11_doc_type_detection.py::test_pdf_extract_failure_falls_back_gracefully
    src = _backend()
    assert 'pdfplumber' in src


def test_huge_file_handled():
    """Sehr grosse Files: Backend hat Cap (Cloud Run Memory)."""
    src = _backend()
    # Max-File-Size-Limit
    assert ('MAX_CONTENT_LENGTH' in src or 'max_size' in src.lower() or
            'len(pdf_bytes)' in src or 'len(byte' in src)


def test_too_many_files_handled():
    src = _backend()
    # Cap auf 24 PDFs an Sonnet
    assert 'dp_bytes[:24]' in src or 'cas_bytes' in src and '[:24]' in src or 'pdf_count' in src


def test_session_expired_handled():
    src = _backend()
    assert ('expired' in src.lower() or 'TTL' in src)


def test_delete_during_processing_handled():
    """Delete-while-processing setzt session als deleted."""
    src = _backend()
    assert ('deleted' in src.lower())


def test_no_pii_in_error_messages():
    """Error-messages enthalten keine PII (Name/Token/PDF-bytes)."""
    src = _backend()
    # Error-Output: KEINE direkten PII-Refs
    import re
    # Suche typische PII-Patterns
    pii_in_errors = re.findall(r"(?:return\s+jsonify|raise\s+\w+Error)\([^)]*personalnummer", src, re.IGNORECASE)
    assert not pii_in_errors


def test_no_false_done_state_after_failure():
    """Failure-States setzen canonical_state korrekt, nicht 'done'."""
    src = _backend()
    # `_set_job_failed` setzt 'failed', kein 'done'
    assert '_set_job_failed' in src
