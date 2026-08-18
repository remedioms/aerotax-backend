"""Sensitive auth endpoints require the active account principal."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend


def _mk_user(email, token):
    backend._auth_upsert_user(email, {
        'token': token,
        'created_at': '2026-08-18T00:00:00',
    })


def test_export_rejects_query_and_body_tokens_without_a_bearer(monkeypatch):
    email, token = 'export-binding@test.local', 'AT-EXPORT-BINDING-1'
    _mk_user(email, token)
    monkeypatch.setattr(
        backend,
        '_validate_token_exists',
        lambda value: email if value == token else None,
    )
    client = backend.app.test_client()

    query = client.get(f'/api/auth/export-data?token={token}')
    body = client.post('/api/auth/export-data', json={'token': token})

    assert query.status_code == 401
    assert query.get_json()['error'] == 'bearer_required'
    assert body.status_code == 401
    assert body.get_json()['error'] == 'bearer_required'

    allowed = client.get(
        '/api/auth/export-data',
        headers={'Authorization': f'Bearer {token}'},
    )
    assert allowed.status_code == 200
    assert allowed.get_json()['meta']['email'] == email
    assert allowed.get_json()['meta']['token'] == '[redacted]'


def test_export_accepts_a_normalized_modern_bearer(monkeypatch):
    email, token = 'export-modern@test.local', 'AT-EXPORT-MODERN-1'
    _mk_user(email, token)
    monkeypatch.setattr(
        backend,
        '_auth_session_access_principal',
        lambda raw_token: ('valid', token) if raw_token == 'AXA-current' else ('invalid', None),
    )
    monkeypatch.setattr(
        backend,
        '_validate_token_exists',
        lambda value: email if value == token else None,
    )

    response = backend.app.test_client().get(
        '/api/auth/export-data',
        headers={'Authorization': 'Bearer AXA-current'},
    )

    assert response.status_code == 200
    assert response.get_json()['meta']['email'] == email


def test_revoke_session_requires_the_current_normalized_access_principal(monkeypatch):
    calls = []
    monkeypatch.setattr(
        backend,
        '_auth_session_revoke',
        lambda **kwargs: calls.append(kwargs) or True,
    )
    client = backend.app.test_client()

    body_only = client.post(
        '/api/auth/revoke-session',
        json={'refresh_token': 'AXR-foreign-refresh'},
    )
    assert body_only.status_code == 401
    assert body_only.get_json()['error'] == 'session_binding_required'
    assert calls == []

    monkeypatch.setattr(
        backend,
        '_auth_session_access_principal',
        lambda raw_token: ('valid', 'AT-REVOKE-OWNER')
        if raw_token == 'AXA-current' else ('invalid', None),
    )
    bound = client.post(
        '/api/auth/revoke-session',
        json={'refresh_token': 'AXR-foreign-refresh'},
        headers={'Authorization': 'Bearer AXA-current'},
    )

    assert bound.status_code == 200
    assert bound.get_json()['ok'] is True
    assert len(calls) == 1
    assert calls[0].get('access_hash') == backend._auth_session_hash('AXA-current')
    assert calls[0].get('refresh_token') is None
