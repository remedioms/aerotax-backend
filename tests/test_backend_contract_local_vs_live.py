"""Foundation A — Backend Contract Local vs Live (static + smoke).

Vergleicht den lokalen Code-Pfad (was app.py setzen SOLLTE) mit dem
Live-Render-Deploy (was tatsächlich rauskommt) für /api/session/<token>
und /api/job/<id>.

Ergebnis dieses Audits zum Zeitpunkt 2026-05-20:
  - Local code: _classify_job_state(...) wird per safe.update gemerged → liefert
    canonical_state, reason_code, pdf_allowed, user_title, user_message,
    next_actions, retry_allowed, support_recommended.
  - Live deploy: zeigt KEINE dieser Felder. Deploy-Lag bestätigt.
  - Frontend kompensiert per _normalizeBackendState — kein Release-Blocker,
    aber Backend-Redeploy ist Bedingung für saubere Release-GO.

Diese Datei läuft NICHT gegen Live-Backend bei Pytest-Default — schaltet sich
nur bei AEROTAX_LIVE_CONTRACT=1 ein. Im Default-Modus testet sie nur den lokalen
Code-Pfad als Source-of-Truth.
"""

import json
import os
import re

import pytest

import app


# ════════════════════════════════════════════════════════════════════
# STATIC AUDIT: Local code MUST set these contract fields
# ════════════════════════════════════════════════════════════════════

REQUIRED_SESSION_FIELDS = [
    'canonical_state', 'reason_code', 'user_title', 'user_message',
    'next_actions', 'pdf_allowed', 'retry_allowed',
]
REQUIRED_JOB_FIELDS = REQUIRED_SESSION_FIELDS  # same shape


def test_classify_job_state_done_contract():
    """done-State liefert alle Pflichtfelder."""
    job = {'status': 'done', 'data': {'netto': 100}}
    state = app._classify_job_state(job)
    for k in REQUIRED_JOB_FIELDS:
        assert k in state, f'done-state missing {k}'
    assert state['canonical_state'] == 'done'
    assert state['pdf_allowed'] is True
    assert state['user_title'] == 'Auswertung fertig'
    assert isinstance(state['next_actions'], list) and len(state['next_actions']) > 0


def test_classify_job_state_needs_review_contract():
    """needs_review (done + pending review_items) liefert review-spezifisches Copy."""
    job = {'status': 'done', 'data': {
        'netto': 100,
        '_review_items': [{'status': 'pending', 'type': 'unknown_marker', 'question': 'X?'}],
    }}
    state = app._classify_job_state(job)
    assert state['canonical_state'] == 'needs_review'
    assert state['pdf_allowed'] is False, 'PDF MUST be locked when review pending'
    assert state['reason_code'] == 'OPEN_REVIEW'
    actions = [a.get('type') for a in state['next_actions']]
    assert 'open_review_chat' in actions


def test_classify_job_state_failed_retryable_contract():
    """failed_retryable liefert retry+support+klare Fehler-Copy."""
    job = {'status': 'failed_timeout', 'data': {}}
    state = app._classify_job_state(job)
    assert state['canonical_state'] in ('failed_retryable', 'failed_support')
    assert state['pdf_allowed'] is False
    if state['canonical_state'] == 'failed_retryable':
        assert state['retry_allowed'] is True


def test_classify_job_state_expired_contract():
    """Kein job + keine deleted-session → expired."""
    state = app._classify_job_state(None, None)
    assert state['canonical_state'] == 'expired'
    assert state['pdf_allowed'] is False
    assert state['reason_code'] == 'ACCESS_CODE_EXPIRED'


def test_classify_job_state_deleted_contract():
    """Kein job + deleted-session → deleted."""
    state = app._classify_job_state(None, {'deleted': True})
    assert state['canonical_state'] == 'deleted'
    assert state['pdf_allowed'] is False


def test_classify_job_state_processing_contract():
    """status=running/pending → processing."""
    job = {'status': 'running', 'data': {}}
    state = app._classify_job_state(job)
    assert state['canonical_state'] == 'processing'
    assert state['pdf_allowed'] is False
    actions = [a.get('type') for a in state['next_actions']]
    assert 'refresh' in actions
    assert 'come_back_later' in actions


def test_classify_job_state_queued_contract():
    """status=queued → queued (with refresh action)."""
    job = {'status': 'queued', 'data': {}}
    state = app._classify_job_state(job)
    assert state['canonical_state'] == 'queued'
    assert state['pdf_allowed'] is False


def test_classify_job_state_pending_reread_overrides_done():
    """done + pending_reread=True → needs_review."""
    job = {
        'status': 'done',
        'data': {'netto': 100},
        'pending_reread': True,
    }
    state = app._classify_job_state(job)
    assert state['canonical_state'] == 'needs_review'
    assert state['pdf_allowed'] is False


def test_session_recall_safe_dict_includes_state_fields():
    """Static: source code of session_recall calls _classify_job_state via safe.update."""
    src = open(os.path.join(os.path.dirname(__file__), '..', 'app.py'), encoding='utf-8').read()
    # Pattern: safe.update(_classify_job_state(job, s))
    assert re.search(r'safe\.update\(\s*_classify_job_state\(\s*job\s*,\s*s\s*\)\s*\)', src), \
        'session_recall must call safe.update(_classify_job_state(job, s))'


def test_get_job_status_includes_state_fields():
    """Static: /api/job/<id> calls _classify_job_state(j)."""
    src = open(os.path.join(os.path.dirname(__file__), '..', 'app.py'), encoding='utf-8').read()
    # Find the get_job_status function and check it calls _classify_job_state
    func_match = re.search(
        r'def get_job_status\(.*?(?=\n@app\.route|\ndef \w)',
        src, re.DOTALL,
    )
    assert func_match, 'get_job_status function not found'
    body = func_match.group(0)
    assert '_classify_job_state' in body, \
        '/api/job/<id> must inject _classify_job_state output'


def test_no_response_returns_only_error_for_404_path():
    """404 path should NOT return only {error: ...} — must include state."""
    src = open(os.path.join(os.path.dirname(__file__), '..', 'app.py'), encoding='utf-8').read()
    # In session_recall: if not s: return jsonify({'error': ..., **state}), 404
    assert re.search(
        r'if\s+not\s+s\s*:.*?_classify_job_state\(\s*None\s*,\s*None\s*\)',
        src, re.DOTALL,
    ), '404 session_recall must call _classify_job_state(None, None) and spread the result'


# ════════════════════════════════════════════════════════════════════
# LIVE smoke (opt-in via env var — Live-Run-Pre-Check, no PII, no auth)
# ════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(
    os.environ.get('AEROTAX_LIVE_CONTRACT') != '1',
    reason='Live contract check only when AEROTAX_LIVE_CONTRACT=1 (avoid network in CI)',
)
def test_live_contract_pre_deploy():
    """Smoke against live deploy — fails if state-fields missing.

    Skip by default. When enabled, this asserts the Release-GO precondition:
    after Backend-Redeploy, /api/session/<token> + 404 path must contain
    canonical_state. Currently (2026-05-20) FAILS — deploy-lag confirmed.
    """
    import urllib.request

    def _get(url):
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read())

    # 404 path
    j404 = _get('https://aerotax-backend.onrender.com/api/session/__test_404__')
    assert 'canonical_state' in j404, \
        f'Live 404 path missing canonical_state. Backend re-deploy needed. raw={j404}'


# ════════════════════════════════════════════════════════════════════
# Documented deploy-lag — XFAIL until backend redeployed
# ════════════════════════════════════════════════════════════════════

@pytest.mark.xfail(
    reason='Backend Render deploy lags behind app.py at 2026-05-20. '
           'Frontend compensates via _normalizeBackendState. '
           'Re-deploy of app.py to Render closes this gap. Tracked in '
           'docs/RECALC_PDF_CHAT_STATE_CONTRACT.md §Deploy-Lag.',
)
def test_live_session_endpoint_has_canonical_state():
    """Xfail: Live /api/session/<token> currently returns no canonical_state."""
    import urllib.request
    with urllib.request.urlopen(
        'https://aerotax-backend.onrender.com/api/session/__test_404__',
        timeout=15,
    ) as r:
        j = json.loads(r.read())
    assert 'canonical_state' in j
