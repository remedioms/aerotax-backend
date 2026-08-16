"""Router-level contracts for Android's primary authenticated account reads."""

import app as A


OWNER = 'AT-1111111111111111'


def _valid(_token):
    return A._TokenValidationResult(
        A._TokenValidationState.VALID, 'owner@example.test',
    )


def _headers():
    return {'Authorization': f'Bearer {OWNER}'}


def test_me_profile_get_and_put_use_the_real_router_and_header_owner(monkeypatch):
    stored = {'token': OWNER, 'profile': {'name': 'Ada', 'homebase': 'FRA'}}
    saved = []
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(A, '_profile_load', lambda token, **_kwargs: stored)
    monkeypatch.setattr(A, '_profile_load_from_disk', lambda _token: stored)
    monkeypatch.setattr(
        A,
        '_profile_save',
        lambda token, profile, **_kwargs: saved.append((token, profile)) or True,
    )

    client = A.app.test_client()
    got = client.get('/api/me/profile', headers=_headers())
    assert got.status_code == 200
    assert got.get_json()['profile'] == stored['profile']

    updated = client.put(
        '/api/me/profile', headers=_headers(), json={'name': 'Grace'},
    )
    assert updated.status_code == 200
    assert updated.get_json() == {
        'ok': True,
        'profile': {'name': 'Grace', 'homebase': 'FRA'},
    }
    assert saved == [(OWNER, {'name': 'Grace', 'homebase': 'FRA'})]


def test_me_profile_requires_a_valid_bearer_at_the_real_router(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    response = A.app.test_client().get('/api/me/profile')
    assert response.status_code == 401
    assert response.get_json() == {'ok': False, 'error': 'unauthorized'}


def test_me_entitlement_has_owner_shape_and_requires_a_valid_bearer(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(
        A,
        '_profile_load',
        lambda _token: {'profile': {'pro_first_seen': '2026-01-01'}},
    )
    client = A.app.test_client()

    allowed = client.get('/api/me/entitlement', headers=_headers())
    assert allowed.status_code == 200
    payload = allowed.get_json()
    assert isinstance(payload.get('ok'), bool)
    assert isinstance(payload.get('pro_required'), bool)
    assert {'free_until', 'family', 'subscription_active'} <= payload.keys()

    denied = client.get('/api/me/entitlement')
    assert denied.status_code == 401
    assert denied.get_json() == {'ok': False, 'error': 'unauthorized'}
