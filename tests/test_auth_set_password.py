"""POST /api/auth/set-password (Christoph, Support 2026-07-21): Passwort für
Sign-in-with-Apple-Konten festlegen — danach geht zusätzlich E-Mail+Passwort-
Login. Bestehendes Passwort ⇒ old_password Pflicht."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend


def _mk_user(email, token, pw_hash=None):
    user = {'token': token, 'created_at': '2026-07-21T00:00:00'}
    if pw_hash:
        user['password_hash'] = pw_hash
    backend._auth_upsert_user(email, user)
    return user


def test_apple_account_can_set_password_then_login_style_verify():
    email, token = 'setpw-apple@test.local', 'AT-SETPW-APPLE-1'
    _mk_user(email, token)   # kein password_hash = SIWA-Konto
    c = backend.app.test_client()
    r = c.post('/api/auth/set-password', json={'token': token, 'password': 'geheim123'},
               headers={'Authorization': f'Bearer {token}'})
    assert r.status_code == 200 and r.get_json()['ok'] is True
    assert r.get_json()['email'] == email
    u = backend._auth_get_user(email)
    ok, _ = backend._password_verify('geheim123', u.get('password_hash', ''))
    assert ok


def test_existing_password_requires_old_password():
    email, token = 'setpw-mail@test.local', 'AT-SETPW-MAIL-1'
    _mk_user(email, token, pw_hash=backend._password_hash('altesPW99'))
    c = backend.app.test_client()
    headers = {'Authorization': f'Bearer {token}'}
    r = c.post('/api/auth/set-password', json={'token': token, 'password': 'neuesPW99'},
               headers=headers)
    assert r.status_code == 401 and r.get_json()['error'] == 'invalid_credentials'
    r2 = c.post('/api/auth/set-password', json={'token': token, 'password': 'neuesPW99',
                                                'old_password': 'altesPW99'}, headers=headers)
    assert r2.status_code == 200 and r2.get_json()['ok'] is True


def test_short_password_and_bad_token_rejected():
    c = backend.app.test_client()
    r = c.post('/api/auth/set-password', json={'token': 'AT-SETPW-APPLE-1', 'password': 'kurz'})
    assert r.status_code == 400 and r.get_json()['error'] == 'password_too_short'
    r2 = c.post('/api/auth/set-password', json={'token': 'AT-GIBTSNICHT-0', 'password': 'geheim123'})
    assert r2.status_code == 401


def test_set_password_requires_matching_bearer_for_body_account_token():
    email, token = 'setpw-binding@test.local', 'AT-SETPW-BINDING-1'
    _mk_user(email, token)
    c = backend.app.test_client()

    missing = c.post('/api/auth/set-password', json={
        'token': token, 'password': 'geheim123',
    })
    mismatched = c.post('/api/auth/set-password', json={
        'token': token, 'password': 'geheim123',
    }, headers={'Authorization': 'Bearer AT-OTHER-ACCOUNT-1'})

    assert missing.status_code == 401
    assert missing.get_json()['error'] == 'token_binding_required'
    assert mismatched.status_code == 401
    assert mismatched.get_json()['error'] == 'token_binding_required'


def test_set_password_accepts_a_normalized_modern_session_bearer(monkeypatch):
    email, token = 'setpw-modern@test.local', 'AT-SETPW-MODERN-1'
    _mk_user(email, token)
    monkeypatch.setattr(
        backend, '_auth_session_access_principal',
        lambda raw_token: ('valid', token) if raw_token == 'AXA-current' else ('invalid', None),
    )

    response = backend.app.test_client().post('/api/auth/set-password', json={
        'token': token, 'password': 'geheim123',
    }, headers={'Authorization': 'Bearer AXA-current'})

    assert response.status_code == 200
    assert response.get_json()['ok'] is True
