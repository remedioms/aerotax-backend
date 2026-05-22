"""QA-Seed Endpoint — synthetische Browser-QA-Sessions.

Verifiziert:
- Endpoint ist DORMANT wenn env nicht gesetzt (403)
- Auth via X-QA-Seed-Token-Header (HMAC-compare)
- Scenario 'needs_review' → canonical_state=needs_review, pdf_allowed=false,
  _review_items mit 2 pending items, kein download_url
- Scenario 'done' → canonical_state=done, pdf_allowed=true, download_url gesetzt
- Token-Form: AT- prefix, 19 chars
- Synthetische Daten klar markiert (_qa_seed=true, name='QA Test User')
- /api/session/<token> liefert genau die erwarteten Felder zurück (Integration)
"""
import os
import json
import pytest


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('AEROTAX_EXECUTION_MODE', 'thread')
    import importlib
    import app as _app
    importlib.reload(_app)
    _app.app.config['TESTING'] = True
    return _app.app.test_client()


# ─── Auth / Dormant-Mode ─────────────────────────────────────────────────────

def test_qa_seed_dormant_when_env_not_set(client, monkeypatch):
    monkeypatch.delenv('AEROTAX_QA_SEED_TOKEN', raising=False)
    r = client.post('/api/admin/qa-seed', json={'scenario': 'done'})
    assert r.status_code == 403
    body = r.get_json()
    assert 'dormant' in body.get('error', '').lower()


def test_qa_seed_requires_correct_token(client, monkeypatch):
    monkeypatch.setenv('AEROTAX_QA_SEED_TOKEN', 'correct-secret-xyz')
    r = client.post('/api/admin/qa-seed',
                    json={'scenario': 'done'},
                    headers={'X-QA-Seed-Token': 'wrong-secret'})
    assert r.status_code == 401


def test_qa_seed_requires_some_token_header(client, monkeypatch):
    monkeypatch.setenv('AEROTAX_QA_SEED_TOKEN', 'correct-secret-xyz')
    r = client.post('/api/admin/qa-seed', json={'scenario': 'done'})
    assert r.status_code == 401


# ─── Scenario Validation ─────────────────────────────────────────────────────

def test_qa_seed_rejects_missing_scenario(client, monkeypatch):
    monkeypatch.setenv('AEROTAX_QA_SEED_TOKEN', 'secret')
    r = client.post('/api/admin/qa-seed',
                    json={},
                    headers={'X-QA-Seed-Token': 'secret'})
    assert r.status_code == 400


def test_qa_seed_rejects_unknown_scenario(client, monkeypatch):
    monkeypatch.setenv('AEROTAX_QA_SEED_TOKEN', 'secret')
    r = client.post('/api/admin/qa-seed',
                    json={'scenario': 'failed_support'},
                    headers={'X-QA-Seed-Token': 'secret'})
    assert r.status_code == 400


# ─── needs_review Scenario ─────────────────────────────────────────────────

def test_qa_seed_needs_review_returns_correct_state(client, monkeypatch):
    monkeypatch.setenv('AEROTAX_QA_SEED_TOKEN', 'secret')
    r = client.post('/api/admin/qa-seed',
                    json={'scenario': 'needs_review'},
                    headers={'X-QA-Seed-Token': 'secret'})
    assert r.status_code == 201
    body = r.get_json()
    assert body['canonical_state'] == 'needs_review'
    assert body['pdf_allowed'] is False
    assert body['download_url'] is None
    assert body['scenario'] == 'needs_review'


def test_qa_seed_needs_review_token_format(client, monkeypatch):
    monkeypatch.setenv('AEROTAX_QA_SEED_TOKEN', 'secret')
    r = client.post('/api/admin/qa-seed',
                    json={'scenario': 'needs_review'},
                    headers={'X-QA-Seed-Token': 'secret'})
    body = r.get_json()
    token = body['token']
    assert token.startswith('AT-')
    assert len(token) == 19  # 'AT-' + 16 hex chars


def test_qa_seed_needs_review_short_code(client, monkeypatch):
    monkeypatch.setenv('AEROTAX_QA_SEED_TOKEN', 'secret')
    r = client.post('/api/admin/qa-seed',
                    json={'scenario': 'needs_review'},
                    headers={'X-QA-Seed-Token': 'secret'})
    body = r.get_json()
    assert body['short_code'].startswith('ATX-')


def test_qa_seed_needs_review_session_lookup_via_api(client, monkeypatch):
    """Integration: /api/session/<token> liefert canonical_state=needs_review
    + alle BUG-009-Felder die _autoResume an render() durchreicht."""
    monkeypatch.setenv('AEROTAX_QA_SEED_TOKEN', 'secret')
    r = client.post('/api/admin/qa-seed',
                    json={'scenario': 'needs_review'},
                    headers={'X-QA-Seed-Token': 'secret'})
    token = r.get_json()['token']

    r2 = client.get(f'/api/session/{token}')
    assert r2.status_code == 200
    body = r2.get_json()
    assert body['canonical_state']        == 'needs_review'
    assert body['pdf_allowed']            is False
    assert body['reason_code']            == 'OPEN_REVIEW'
    assert body['can_show_final_amount']  is False
    assert body['can_chat_explain_calculation'] is True
    assert body['retry_allowed']          is False
    assert isinstance(body['next_actions'], list)
    assert len(body['next_actions']) >= 1


def test_qa_seed_needs_review_has_pending_items_in_result_data(client, monkeypatch):
    monkeypatch.setenv('AEROTAX_QA_SEED_TOKEN', 'secret')
    r = client.post('/api/admin/qa-seed',
                    json={'scenario': 'needs_review'},
                    headers={'X-QA-Seed-Token': 'secret'})
    token = r.get_json()['token']

    r2 = client.get(f'/api/session/{token}')
    body = r2.get_json()
    rd = body.get('result_data') or {}
    items = rd.get('_review_items') or []
    pending = [i for i in items if i.get('status') == 'pending']
    assert len(pending) >= 2, f'expected ≥2 pending items, got {len(pending)}'


def test_qa_seed_needs_review_netto_positive(client, monkeypatch):
    """needs_review hat trotzdem netto > 0 — User-Spec: 'result_data mit netto > 0'."""
    monkeypatch.setenv('AEROTAX_QA_SEED_TOKEN', 'secret')
    r = client.post('/api/admin/qa-seed',
                    json={'scenario': 'needs_review'},
                    headers={'X-QA-Seed-Token': 'secret'})
    token = r.get_json()['token']

    r2 = client.get(f'/api/session/{token}')
    rd = (r2.get_json() or {}).get('result_data') or {}
    assert float(rd.get('netto', 0)) > 0


# ─── done Scenario ──────────────────────────────────────────────────────────

def test_qa_seed_done_returns_correct_state(client, monkeypatch):
    monkeypatch.setenv('AEROTAX_QA_SEED_TOKEN', 'secret')
    r = client.post('/api/admin/qa-seed',
                    json={'scenario': 'done'},
                    headers={'X-QA-Seed-Token': 'secret'})
    assert r.status_code == 201
    body = r.get_json()
    assert body['canonical_state'] in ('done', 'done_clean')
    assert body['pdf_allowed'] is True
    assert body['download_url'] is not None
    assert body['download_url'].startswith('/api/download/')


def test_qa_seed_done_session_lookup_via_api(client, monkeypatch):
    monkeypatch.setenv('AEROTAX_QA_SEED_TOKEN', 'secret')
    r = client.post('/api/admin/qa-seed',
                    json={'scenario': 'done'},
                    headers={'X-QA-Seed-Token': 'secret'})
    token = r.get_json()['token']

    r2 = client.get(f'/api/session/{token}')
    assert r2.status_code == 200
    body = r2.get_json()
    assert body['canonical_state'] in ('done', 'done_clean')
    assert body['pdf_allowed']           is True
    assert body['can_show_final_amount'] is True
    assert body['can_chat_explain_calculation'] is True
    assert body.get('download_url')


def test_qa_seed_done_pdf_download_works(client, monkeypatch):
    """Integration: download_url muss tatsächlich ein PDF zurückgeben (200 + bytes)."""
    monkeypatch.setenv('AEROTAX_QA_SEED_TOKEN', 'secret')
    r = client.post('/api/admin/qa-seed',
                    json={'scenario': 'done'},
                    headers={'X-QA-Seed-Token': 'secret'})
    download_url = r.get_json()['download_url']

    r2 = client.get(download_url)
    assert r2.status_code == 200
    assert len(r2.data) > 1000, 'PDF should be > 1KB'
    assert r2.data[:4] == b'%PDF', 'must be a real PDF file'


# ─── Synthetic-Data-Marker ──────────────────────────────────────────────────

def test_qa_seed_marks_data_as_qa(client, monkeypatch):
    """Result-Data klar als QA markiert — kein Versehen-mit-echter-Auswertung."""
    monkeypatch.setenv('AEROTAX_QA_SEED_TOKEN', 'secret')
    r = client.post('/api/admin/qa-seed',
                    json={'scenario': 'done'},
                    headers={'X-QA-Seed-Token': 'secret'})
    token = r.get_json()['token']

    r2 = client.get(f'/api/session/{token}')
    rd = (r2.get_json() or {}).get('result_data') or {}
    assert rd.get('_qa_seed') is True
    assert rd.get('name') == 'QA Test User'
    assert 'QA' in rd.get('arbeitgeber', '')


def test_qa_seed_no_real_data_leakage(client, monkeypatch):
    """Synthetische Daten enthalten keine offensichtlichen Echt-Daten-Marker."""
    monkeypatch.setenv('AEROTAX_QA_SEED_TOKEN', 'secret')
    r = client.post('/api/admin/qa-seed',
                    json={'scenario': 'done'},
                    headers={'X-QA-Seed-Token': 'secret'})
    body_str = json.dumps(r.get_json())
    # Verbotene Strings — keine realen Customer-Daten leaken
    forbidden = ['Tibor', 'Miguel', 'Lufthansa AG', 'schumann']
    for f in forbidden:
        assert f.lower() not in body_str.lower(), f'forbidden string in QA seed: {f}'


# ─── Multiple seeds isoliert ─────────────────────────────────────────────────

def test_qa_seed_multiple_calls_return_different_tokens(client, monkeypatch):
    monkeypatch.setenv('AEROTAX_QA_SEED_TOKEN', 'secret')
    r1 = client.post('/api/admin/qa-seed',
                     json={'scenario': 'needs_review'},
                     headers={'X-QA-Seed-Token': 'secret'})
    r2 = client.post('/api/admin/qa-seed',
                     json={'scenario': 'needs_review'},
                     headers={'X-QA-Seed-Token': 'secret'})
    assert r1.get_json()['token'] != r2.get_json()['token']


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
