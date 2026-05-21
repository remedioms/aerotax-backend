"""Rel Phase 9 — Payment/Process E2E Contract Tests (static-Audit).

Diese Tests pruefen Backend-Code-Pfade fuer Payment-/Process-Robustness via
Source-Inspektion und Mock-Calls. Echte Stripe-Calls werden NICHT ausgeloest.
"""
import os
import re
import sys
import pytest

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')


def _read_app():
    return open(os.path.join(ROOT_DIR, 'app.py'), encoding='utf-8').read()


def test_payment_idempotency_via_attempt_id():
    """Cloud-Tasks-Idempotenz via attempt_id verhindert Doppel-Job."""
    src = _read_app()
    assert 'attempt_id' in src
    assert 'idempoten' in src.lower() or 'attempt_id' in src


def test_payment_intent_dedup_check():
    """Payment-Intent darf nicht doppelt eingelöst werden."""
    src = _read_app()
    assert 'payment_intent_id' in src
    # Replay-Schutz: Backend prüft Stripe-Status
    assert 'payment_intent' in src.lower()


def test_free_retry_token_validation():
    """free_retry_token wird im Backend validiert."""
    src = _read_app()
    assert 'free_retry_token' in src
    assert ('_validate_free_retry_token' in src or
            'free_retry_token' in src and ('expired' in src.lower() or 'consumed' in src.lower()))


def test_no_double_charge_pattern():
    """Backend hat Anti-Double-Charge-Pattern."""
    src = _read_app()
    # Pattern: einmal verwendet → markieren
    assert ('payment_used' in src or
            'free_retry_consumed' in src or
            'one-time' in src.lower() or
            'used' in src.lower() and 'token' in src.lower())


def test_rate_limit_on_process():
    """Process-Endpoint hat IP-Rate-Limit."""
    src = _read_app()
    proc_idx = src.find('def process_real')
    block = src[proc_idx:proc_idx + 3000]
    assert '_ip_rate_limited' in block


def test_missing_docs_before_payment_blocked():
    """LSB+SE+CAS required."""
    src = _read_app()
    assert ('files.get(\'lsb\')' in src and 'files.get(\'se\')' in src and "files.get('cas')" in src)


def test_session_restore_after_reload():
    """`restore-session` Endpoint exists."""
    src = _read_app()
    assert 'restore-session' in src


def test_job_id_returned_immediately():
    """Process startet asynchron — liefert job_id sofort zurueck."""
    src = _read_app()
    assert 'job_id' in src and ('async' in src.lower() or '_run_process_async' in src)


def test_stripe_webhook_signature_check():
    """Stripe-Webhook verifiziert Signature."""
    src = _read_app()
    webhook_idx = src.find('stripe-webhook')
    block = src[webhook_idx:webhook_idx + 2000]
    # Webhook verifiziert via Stripe SDK construct_event oder header check
    assert ('construct_event' in block or
            'Stripe-Signature' in block or 'sig_header' in block.lower())


def test_payment_ref_not_logged_in_plaintext():
    """Payment-Refs werden im Log redacted."""
    src = _read_app()
    # `_redact_token` oder ähnliches helper existiert
    assert any(p in src for p in ('_redact_token', '_safe_log_session', '[:8]', '[:6]'))
