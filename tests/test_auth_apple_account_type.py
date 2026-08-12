"""Apple-Registrierung fragt den Kontotyp genau einmal und bewahrt Bestand."""

import app as backend


def _client(monkeypatch, *, existing=None, profile=None):
    monkeypatch.setattr(
        backend, '_verify_apple_identity_token',
        lambda _token, expected_sub=None: (True, expected_sub, 'family@example.test'))
    monkeypatch.setattr(
        backend, '_auth_find_user_by',
        lambda _key, _value: existing if existing is not None else (None, None))
    monkeypatch.setattr(backend, '_auth_get_user', lambda _email: None)
    monkeypatch.setattr(backend, '_auth_upsert_user', lambda _email, _user: True)
    monkeypatch.setattr(backend, '_invalidate_token_cache', lambda: None)
    monkeypatch.setattr(
        backend, '_auth_success_payload',
        lambda email, user: {'ok': True, 'token': user['token'], 'email': email})
    monkeypatch.setattr(
        backend, '_profile_load',
        lambda _token: {'profile': dict(profile or {})})
    return backend.app.test_client()


def _apple_body(**extra):
    return {
        'apple_sub': 'apple-family-1',
        'identity_token': 'verified-jwt',
        **extra,
    }


def test_new_apple_account_reports_that_account_type_is_still_required(monkeypatch):
    saved = []
    client = _client(monkeypatch)
    monkeypatch.setattr(
        backend, '_profile_save',
        lambda token, profile: saved.append((token, dict(profile))) or True)

    response = client.post('/api/auth/apple', json=_apple_body(name='Karsten Fischer'))

    assert response.status_code == 200
    body = response.get_json()
    assert body['created'] is True
    assert body['needs_account_type'] is True
    assert saved == [(body['token'], {'name': 'Karsten Fischer'})]


def test_second_verified_apple_request_persists_family_choice(monkeypatch):
    user = {'token': 'AT-APPLEFAMILY123', 'apple_sub': 'apple-family-1'}
    saved = []
    client = _client(
        monkeypatch,
        existing=('family@example.test', user),
        profile={'name': 'Karsten Fischer'})
    monkeypatch.setattr(
        backend, '_profile_save',
        lambda token, profile: saved.append((token, dict(profile))) or True)

    response = client.post(
        '/api/auth/apple', json=_apple_body(account_type='family'))

    assert response.status_code == 200
    assert response.get_json()['created'] is False
    assert response.get_json()['needs_account_type'] is False
    assert saved == [('AT-APPLEFAMILY123', {
        'name': 'Karsten Fischer', 'account_type': 'family'})]


def test_existing_crew_account_is_never_switched_by_apple_login(monkeypatch):
    user = {'token': 'AT-APPLECREW12345', 'apple_sub': 'apple-family-1'}
    client = _client(
        monkeypatch,
        existing=('crew@example.test', user),
        profile={'account_type': 'crew', 'airline': 'Lufthansa'})
    monkeypatch.setattr(
        backend, '_profile_save',
        lambda *_args: (_ for _ in ()).throw(
            AssertionError('existing account type must not be overwritten')))

    response = client.post(
        '/api/auth/apple', json=_apple_body(account_type='family'))

    assert response.status_code == 200
    assert response.get_json()['created'] is False
    assert response.get_json()['needs_account_type'] is False


def test_legacy_crew_profile_without_account_type_is_not_prompted(monkeypatch):
    user = {'token': 'AT-APPLELEGACY123', 'apple_sub': 'apple-family-1'}
    client = _client(
        monkeypatch,
        existing=('legacy-crew@example.test', user),
        profile={'name': 'Bestandscrew', 'homebase': 'FRA'})
    monkeypatch.setattr(
        backend, '_profile_save',
        lambda *_args: (_ for _ in ()).throw(
            AssertionError('legacy crew profile must stay untouched')))

    response = client.post('/api/auth/apple', json=_apple_body())

    assert response.status_code == 200
    assert response.get_json()['created'] is False
    assert response.get_json()['needs_account_type'] is False


def test_invalid_apple_account_type_is_rejected(monkeypatch):
    client = _client(monkeypatch)

    response = client.post(
        '/api/auth/apple', json=_apple_body(account_type='passenger'))

    assert response.status_code == 400
    assert response.get_json()['error'] == 'invalid_account_type'
