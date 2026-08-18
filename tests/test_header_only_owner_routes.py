"""Regression contracts for the additive, credential-free Android URLs."""

import sys

import app as A
from blueprints import layover_group_blueprint as groups


TOKEN = 'AT-1234567890abcdef'


def _valid(_token):
    return A._TokenValidationResult(
        A._TokenValidationState.VALID, 'owner@example.test')


def test_me_route_uses_only_the_validated_bearer_as_owner(monkeypatch):
    seen = []
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(
        A, 'get_logbook', lambda token: seen.append(token) or A.jsonify({'ok': True}),
    )
    with A.app.test_request_context(
            '/api/me/logbook', headers={'Authorization': f'Bearer {TOKEN}'}):
        response = A.me_logbook()
    assert response.status_code == 200
    assert seen == [TOKEN]


def test_calendar_pdf_me_route_uses_only_the_validated_bearer(monkeypatch):
    seen = []
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(
        A,
        'get_user_calendar_pdf',
        lambda token: seen.append(token) or A.app.response_class(
            b'%PDF-test', mimetype='application/pdf'),
    )
    with A.app.test_request_context(
            '/api/me/calendar-pdf?month=2026-08',
            headers={'Authorization': f'Bearer {TOKEN}'}):
        response = A.me_calendar_pdf()
    assert response.status_code == 200
    assert response.mimetype == 'application/pdf'
    assert seen == [TOKEN]


def test_calendar_events_me_route_uses_only_the_validated_bearer(monkeypatch):
    seen = []
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(
        A,
        'upload_calendar_events',
        lambda token: seen.append(token) or A.jsonify({'ok': True}),
    )
    with A.app.test_request_context(
            '/api/me/calendar-events/upload', method='POST',
            json={'events': []},
            headers={'Authorization': f'Bearer {TOKEN}'}):
        response = A.me_calendar_events_upload()
    assert response.status_code == 200
    assert response.get_json() == {'ok': True}
    assert seen == [TOKEN]


def test_destination_leaderboard_me_route_binds_the_bearer_not_a_url_token(monkeypatch):
    seen = []
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(
        A,
        'get_destination_leaderboard',
        lambda iata, token: seen.append((iata, token)) or A.jsonify({
            'ok': True, 'iata': iata, 'ranking': [], 'total_crew': 0,
            'my_rank': None, 'my_count': 0,
        }),
    )
    with A.app.test_request_context(
            '/api/me/destination-leaderboard/FRA',
            headers={'Authorization': f'Bearer {TOKEN}'}):
        response = A.me_destination_leaderboard('FRA')
    assert response.status_code == 200
    assert response.get_json()['iata'] == 'FRA'
    assert seen == [('FRA', TOKEN)]


def test_me_route_rejects_missing_or_non_account_bearer(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    with A.app.test_request_context('/api/me/logbook'):
        response, status = A.me_logbook()
    assert status == 401
    assert response.get_json()['error'] == 'unauthorized'


def test_profile_me_route_is_registered_and_preserves_owner_shape(monkeypatch):
    """A fresh Android login can classify the existing account via the router."""
    seen = []
    profile = {
        'name': 'Existing Crew',
        'homebase': 'FRA',
        'account_type': 'crew',
    }
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(
        A,
        'get_user_profile',
        lambda token: seen.append(('GET', token)) or A.jsonify({'profile': profile}),
    )
    monkeypatch.setattr(
        A,
        'put_user_profile',
        lambda token: seen.append(('PUT', token)) or A.jsonify({
            'ok': True,
            'profile': profile,
        }),
    )
    client = A.app.test_client()
    headers = {'Authorization': f'Bearer {TOKEN}'}

    get_response = client.get('/api/me/profile', headers=headers)
    put_response = client.put('/api/me/profile', headers=headers, json={'name': 'Existing Crew'})
    anonymous_response = client.get('/api/me/profile')

    assert get_response.status_code == 200
    assert get_response.get_json() == {'profile': profile}
    assert put_response.status_code == 200
    assert put_response.get_json() == {'ok': True, 'profile': profile}
    assert anonymous_response.status_code == 401
    assert anonymous_response.get_json() == {'ok': False, 'error': 'unauthorized'}
    assert seen == [('GET', TOKEN), ('PUT', TOKEN)]


def test_entitlement_me_route_is_registered_and_preserves_policy_shape(monkeypatch):
    entitlement = {
        'ok': True,
        'pro_required': True,
        'family': False,
        'subscription_active': True,
        'free_until': None,
        'subscription_platform': 'google_play',
        'subscription_valid_until': '2026-09-01T00:00:00Z',
    }
    seen = []
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(
        A,
        'user_entitlement',
        lambda token: seen.append(token) or A.jsonify(entitlement),
    )
    client = A.app.test_client()

    response = client.get(
        '/api/me/entitlement',
        headers={'Authorization': f'Bearer {TOKEN}'},
    )

    assert response.status_code == 200
    assert response.get_json() == entitlement
    assert seen == [TOKEN]


def test_crew_map_me_route_uses_bearer_and_redacts_foreign_credentials(monkeypatch):
    """The Android map gets only the legacy handler's gated result and AXU refs."""
    seen = []
    monkeypatch.setattr(A, '_validate_token', _valid)
    monkeypatch.setattr(
        A,
        'get_crew_at_destination',
        lambda token: seen.append(token) or A.jsonify({
            'ok': True,
            'layover_matches': [{
                'iata': 'FRA', 'lat': 50.0379, 'lng': 8.5622,
                'friends': [{'token': 'AT-abcdef0123456789', 'name': 'Ada'}],
            }],
            'manual_pins': [{
                'id': 'meetup', 'lat': 50.0, 'lng': 8.0,
                'owner_token': 'AT-abcdef0123456789',
            }],
        }),
    )
    with A.app.test_request_context(
            '/api/me/crew-at-destination',
            headers={'Authorization': f'Bearer {TOKEN}'}):
        response = A.me_crew_at_destination()
    assert response.status_code == 200
    assert seen == [TOKEN]
    body = response.get_json()
    assert body['layover_matches'][0]['friends'][0]['token'].startswith('AXU-')
    assert body['manual_pins'][0]['owner_token'].startswith('AXU-')
    assert 'AT-abcdef0123456789' not in str(body)


def test_flightops_crewlist_me_is_header_only_bounded_and_public(monkeypatch):
    """The Android crew sheet cannot select an owner or receive AT secrets."""
    monkeypatch.setattr(A, '_validate_token', _valid)
    from blueprints import lh_flightops as fo

    seen = []
    def legacy_handler(token):
        seen.append((token, A.request.get_json()))
        return A.jsonify({
            'ok': True,
            'crew': [{
                'name': 'A. Crew', 'position': 'FO', 'duty': 'OD',
                'aerox': {'token': TOKEN, 'name': 'A. Crew'},
            }],
            'flight_date': '2026-08-15',
        })
    monkeypatch.setattr(fo, 'flightops_crewlist', legacy_handler)
    with A.app.test_request_context(
            '/api/me/flightops/crewlist', method='POST',
            headers={'Authorization': f'Bearer {TOKEN}'},
            json={'flight': 'LH402', 'date': '2026-08-15',
                  'dep': 'fra', 'arr': 'ewr'}):
        response = A.me_flightops_crewlist()
    assert response.status_code == 200
    assert seen == [(TOKEN, {'flight': 'LH402', 'date': '2026-08-15',
                             'dep': 'FRA', 'arr': 'EWR'})]
    body = response.get_json()
    assert body['crew'][0]['aerox']['token'].startswith('AXU-')
    assert TOKEN not in str(body)


def test_flightops_crewlist_me_rejects_legacy_access_and_bad_leg(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    headers = {'Authorization': f'Bearer {TOKEN}'}
    with A.app.test_request_context(
            '/api/me/flightops/crewlist', method='POST', headers=headers,
            json={'flight': 'LH402', 'date': '2026-08-15', 'dep': 'FRA',
                  'arr': 'EWR', 'force': True}):
        response, status = A.me_flightops_crewlist()
    assert status == 400
    assert response.get_json()['error'] == 'invalid_body'
    with A.app.test_request_context(
            '/api/me/flightops/crewlist', method='POST', headers=headers,
            json={'flight': 'LH402', 'date': '2026-02-30', 'dep': 'FRA',
                  'arr': 'EWR'}):
        response, status = A.me_flightops_crewlist()
    assert status == 400
    assert response.get_json()['error'] == 'invalid_leg'

    with A.app.test_request_context(
            '/api/me/logbook', headers={'Authorization': 'Bearer AXU-public-ref'}):
        response, status = A.me_logbook()
    assert status == 401
    assert response.get_json()['error'] == 'unauthorized'


def test_me_lh_flightops_status_and_start_bind_only_the_header_owner(monkeypatch):
    """Android cannot select an LH grant owner via query or body."""
    from blueprints import lh_flightops as fo

    monkeypatch.setitem(sys.modules, 'app', A)
    monkeypatch.setattr(A, '_header_only_owner', lambda: (TOKEN, None))
    seen = []
    monkeypatch.setattr(
        fo, 'flightops_status',
        lambda token: seen.append(('status', token)) or A.jsonify({'ok': True}),
    )
    monkeypatch.setattr(
        fo, '_flightops_oauth_start_for',
        lambda token: seen.append(('start', token)) or A.jsonify({'ok': True}),
    )

    with A.app.test_request_context('/api/me/lh/flightops/status'):
        assert fo.me_flightops_status().status_code == 200
    with A.app.test_request_context('/api/me/lh/flightops/oauth/start'):
        assert fo.me_flightops_oauth_start().status_code == 200
    with A.app.test_request_context('/api/me/lh/flightops/status?token=ATTACKER'):
        response, status = fo.me_flightops_status()
    assert status == 400
    assert response.get_json()['error'] == 'query_not_allowed'
    assert seen == [('status', TOKEN), ('start', TOKEN)]


def test_me_lh_flightops_exchange_requires_exact_body_and_state_owner(monkeypatch):
    from blueprints import lh_flightops as fo

    monkeypatch.setitem(sys.modules, 'app', A)
    monkeypatch.setattr(A, '_header_only_owner', lambda: (TOKEN, None))
    seen = []
    monkeypatch.setattr(
        fo, '_flightops_oauth_exchange_for',
        lambda expected_owner=None: seen.append(expected_owner) or A.jsonify({'ok': True}),
    )

    with A.app.test_request_context(
            '/api/me/lh/flightops/oauth/exchange', method='POST',
            json={'code': 'one-time-code', 'state': 'state'}):
        assert fo.me_flightops_oauth_exchange().status_code == 200
    with A.app.test_request_context(
            '/api/me/lh/flightops/oauth/exchange', method='POST',
            json={'code': 'one-time-code', 'state': 'state', 'owner': 'ATTACKER'}):
        response, status = fo.me_flightops_oauth_exchange()
    assert status == 400
    assert response.get_json()['error'] == 'invalid_body'
    assert seen == [TOKEN]


def test_lh_flightops_owner_mismatch_does_not_consume_the_pkce_state():
    from blueprints import lh_flightops as fo

    state = 'owner-bound-state-for-test'
    with fo._flow_lock:
        fo._flow_store[state] = (
            fo.time.time() + 60,
            {'verifier': 'test-verifier', 'user_token': TOKEN},
        )
    assert fo._flow_take(state, expected_user_token='AT-ATTACKER') is None
    assert fo._flow_take(state, expected_user_token=TOKEN) == {
        'verifier': 'test-verifier', 'user_token': TOKEN,
    }


def test_me_lh_flightops_import_validates_only_the_bounded_window(monkeypatch):
    from blueprints import lh_flightops as fo

    monkeypatch.setitem(sys.modules, 'app', A)
    monkeypatch.setattr(A, '_header_only_owner', lambda: (TOKEN, None))
    seen = []
    monkeypatch.setattr(
        fo, 'flightops_import',
        lambda token: seen.append((token, A.request.get_json())) or A.jsonify({'ok': True}),
    )

    with A.app.test_request_context(
            '/api/me/lh/flightops/import', method='POST',
            json={'from_date': '2026-08-01', 'to_date': '2026-08-31'}):
        assert fo.me_flightops_import().status_code == 200
    with A.app.test_request_context(
            '/api/me/lh/flightops/import', method='POST',
            json={'history': True}):
        response, status = fo.me_flightops_import()
    assert status == 400
    assert response.get_json()['error'] == 'invalid_body'
    assert seen == [(TOKEN, {'from_date': '2026-08-01', 'to_date': '2026-08-31'})]


def test_layover_group_legacy_path_needs_a_real_matching_account(monkeypatch):
    # test_calculation.py deliberately re-imports app during the full suite;
    # this blueprint resolves app lazily, so pin it to the collected instance.
    monkeypatch.setitem(sys.modules, 'app', A)
    monkeypatch.setattr(
        A, '_validate_token',
        lambda token: _valid(token) if token == TOKEN else A._TokenValidationResult(
            A._TokenValidationState.INVALID
        ),
    )
    with A.app.test_request_context(
            '/api/layover-group/AT-invented/meta/fra',
            headers={'Authorization': 'Bearer AT-invented'}):
        response = groups.get_layover_group_meta('AT-invented', 'fra')
    status = response[1] if isinstance(response, tuple) else response.status_code
    assert status == 401

    with A.app.test_request_context(
            f'/api/layover-group/{TOKEN}/meta/fra',
            headers={'Authorization': f'Bearer {TOKEN}'}):
        response = groups.get_layover_group_meta(TOKEN, 'fra')
    assert response.status_code == 200


def test_layover_group_me_url_does_not_accept_a_path_credential(monkeypatch):
    monkeypatch.setitem(sys.modules, 'app', A)
    monkeypatch.setattr(A, '_validate_token', _valid)
    with A.app.test_request_context(
            '/api/me/layover-group/meta/fra',
            headers={'Authorization': f'Bearer {TOKEN}'}):
        response = groups.get_layover_group_meta_me('fra')
    assert response.status_code == 200
