import time
from unittest.mock import patch

import app as A
import pytest


TOKEN = "AT-VALID-TRISTATE-TEST"
UNKNOWN = "AT-UNKNOWN-TRISTATE-TEST"


def _set_cache(tokens, expires):
    A._TOKEN_VALIDATE_CACHE['tokens'] = tokens
    A._TOKEN_VALIDATE_CACHE['expires'] = expires


def test_cold_start_store_outage_is_unavailable_not_valid():
    _set_cache(None, 0.0)
    with patch.object(A, '_refresh_token_cache', return_value=None):
        result = A._validate_token(UNKNOWN)
        with pytest.raises(A._AuthStoreUnavailable):
            A._validate_token_exists(UNKNOWN)
    assert result.state is A._TokenValidationState.UNAVAILABLE
    assert result.email is None


def test_store_outage_keeps_stale_known_user_valid_but_unknown_unavailable():
    _set_cache({TOKEN: 'known@example.test'}, 0.0)
    with patch.object(A, '_refresh_token_cache', return_value=None):
        known = A._validate_token(TOKEN)
        unknown = A._validate_token(UNKNOWN)
    assert known.state is A._TokenValidationState.VALID
    assert known.email == 'known@example.test'
    assert unknown.state is A._TokenValidationState.UNAVAILABLE


def test_reachable_store_can_prove_token_invalid():
    _set_cache({}, time.time() + 60)
    with patch.object(A, '_refresh_token_cache', return_value={}):
        result = A._validate_token(UNKNOWN)
    assert result.state is A._TokenValidationState.INVALID


def test_auth_gate_returns_retryable_503_when_validation_unavailable():
    client = A.app.test_client()
    unavailable = A._TokenValidationResult(A._TokenValidationState.UNAVAILABLE)
    with patch.object(A, '_validate_token', return_value=unavailable):
        response = client.post(f'/api/wall/{UNKNOWN}/post', json={'text': 'x'})
    assert response.status_code == 503
    assert response.get_json()['error'] == 'auth_store_unavailable'
    assert response.headers['Retry-After'] == '5'


def test_legacy_boolean_auth_route_also_maps_unavailable_to_503():
    client = A.app.test_client()
    unavailable = A._TokenValidationResult(A._TokenValidationState.UNAVAILABLE)
    with patch.object(A, '_validate_token', return_value=unavailable):
        response = client.post('/api/user/contacts-match', json={
            'token': UNKNOWN, 'email_hashes': [], 'names': []
        })
    assert response.status_code == 503
    assert response.get_json()['error'] == 'auth_store_unavailable'
    assert response.headers['Retry-After'] == '5'


def test_cross_user_exemptions_are_exact_method_and_route_rules():
    profile = f'/api/user/profile/{TOKEN}'
    friends = f'/api/user/friends/{TOKEN}'
    assert A._bug004_is_cross_user_route('GET', profile) is True
    assert A._bug004_is_cross_user_route('HEAD', friends) is True
    assert A._bug004_is_cross_user_route('PUT', profile) is False
    assert A._bug004_is_cross_user_route('POST', friends + '/add') is False
    assert A._bug004_is_cross_user_route('POST', friends + '/remove') is False
    assert A._bug004_is_cross_user_route('GET', friends + '/overlap') is False
    assert A._bug004_is_cross_user_route('GET', '/api/layover-recs/' + TOKEN) is False


def test_token_binding_is_deny_by_default_with_explicit_emergency_opt_out():
    assert A._token_binding_enforced_from_env(None) is False  # test env pins opt-out
    assert A._token_binding_enforced_from_env('') is True
    assert A._token_binding_enforced_from_env('1') is True
    assert A._token_binding_enforced_from_env('true') is True
    assert A._token_binding_enforced_from_env('0') is False


def test_friends_write_with_mismatched_bearer_is_denied_in_legacy_mode():
    client = A.app.test_client()
    valid = A._TokenValidationResult(A._TokenValidationState.VALID,
                                     'known@example.test')
    with patch.object(A, '_validate_token', return_value=valid), \
            patch.object(A, '_BUG004_REQUIRE_TOKEN_BINDING', False):
        response = client.post(
            f'/api/user/friends/{TOKEN}/add',
            headers={'Authorization': 'Bearer AT-DIFFERENT-OWNER-TOKEN'},
            json={'friend_token': 'AT-ANOTHER-VALID-USER'},
        )
    assert response.status_code == 401
    assert response.get_json()['error'] == 'token_binding_mismatch'
