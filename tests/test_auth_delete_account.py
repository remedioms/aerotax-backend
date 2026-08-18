"""Account deletion keeps body tokens as identifiers, not capabilities."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend


def _mk_user(email, token, password=None):
    user = {'token': token, 'created_at': '2026-08-18T00:00:00'}
    if password is not None:
        user['password_hash'] = backend._password_hash(password)
    backend._auth_upsert_user(email, user)


def test_token_only_delete_requires_a_matching_bearer():
    email, token = 'delete-binding@test.local', 'AT-DELETE-BINDING-1'
    _mk_user(email, token)
    client = backend.app.test_client()

    missing = client.post('/api/auth/delete-account', json={'token': token})
    mismatched = client.post(
        '/api/auth/delete-account',
        json={'token': token},
        headers={'Authorization': 'Bearer AT-OTHER-ACCOUNT-1'},
    )

    assert missing.status_code == 401
    assert missing.get_json()['error'] == 'token_binding_required'
    assert mismatched.status_code == 401
    assert mismatched.get_json()['error'] == 'token_binding_required'
    assert backend._auth_get_user(email) is not None

    matching = client.post(
        '/api/auth/delete-account',
        json={'token': token},
        headers={'Authorization': f'Bearer {token}'},
    )
    assert matching.status_code == 200
    assert matching.get_json()['ok'] is True
    assert backend._auth_get_user(email) is None


def test_token_only_delete_accepts_a_normalized_modern_session_bearer(monkeypatch):
    email, token = 'delete-modern@test.local', 'AT-DELETE-MODERN-1'
    _mk_user(email, token)
    monkeypatch.setattr(
        backend,
        '_auth_session_access_principal',
        lambda raw_token: ('valid', token) if raw_token == 'AXA-current' else ('invalid', None),
    )

    response = backend.app.test_client().post(
        '/api/auth/delete-account',
        json={'token': token},
        headers={'Authorization': 'Bearer AXA-current'},
    )

    assert response.status_code == 200
    assert response.get_json()['ok'] is True
    assert backend._auth_get_user(email) is None


def test_email_password_reauthentication_remains_compatible_without_bearer():
    email, token, password = (
        'delete-reauth@test.local',
        'AT-DELETE-REAUTH-1',
        'correct-horse-99',
    )
    _mk_user(email, token, password)

    response = backend.app.test_client().post(
        '/api/auth/delete-account',
        json={'email': email, 'password': password, 'token': token},
    )

    assert response.status_code == 200
    assert response.get_json()['ok'] is True
    assert backend._auth_get_user(email) is None
