"""Bearer-only regression contracts for dynamic airline onboarding."""

import app as A


TOKEN = 'AT-1234567890abcdef'


def _valid(_token):
    return A._TokenValidationResult(
        A._TokenValidationState.VALID, 'owner@example.test')


def test_catalog_alias_forwards_only_validated_bearer(monkeypatch):
    seen = []
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(
        A, 'get_supported_airlines',
        lambda token: seen.append(token) or A.jsonify({'ok': True, 'airlines': []}),
    )
    with A.app.test_request_context(
            '/api/me/airlines', headers={'Authorization': f'Bearer {TOKEN}'}):
        response = A.me_supported_airlines()
    assert response.status_code == 200
    assert response.get_json() == {'ok': True, 'airlines': []}
    assert seen == [TOKEN]


def test_request_alias_rejects_owner_in_body_before_dispatch(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(
        A, 'submit_airline_support_request',
        lambda _token: (_ for _ in ()).throw(AssertionError('must not dispatch')),
    )
    with A.app.test_request_context(
            '/api/me/airline-request', method='POST',
            headers={'Authorization': f'Bearer {TOKEN}'},
            json={'token': 'AT-foreign', 'airline_name': 'Example Air'}):
        response, status = A.me_airline_support_request()
    assert status == 400
    assert response.get_json()['error'] == 'owner_in_body_not_allowed'
