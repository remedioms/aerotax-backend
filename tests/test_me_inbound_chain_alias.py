"""Contracts for the credential-free Android inbound-aircraft alias."""

import app as A
from blueprints import aerox_data_blueprint as data


OWNER = 'AT-1234567890abcdef'


def _valid(_token):
    return A._TokenValidationResult(
        A._TokenValidationState.VALID, 'owner@example.test')


def _request(client, query=''):
    return client.get(
        f'/api/me/ax/flight-inbound-chain{query}',
        headers={'Authorization': f'Bearer {OWNER}'},
    )


def test_me_inbound_chain_requires_a_valid_header_bearer(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    response = A.app.test_client().get(
        '/api/me/ax/flight-inbound-chain?flight_no=LH400&dep_iata=FRA',
    )
    assert response.status_code == 401
    assert response.get_json() == {'ok': False, 'error': 'unauthorized'}


def test_me_inbound_chain_validates_only_its_bounded_operational_query(monkeypatch):
    monkeypatch.setattr(A, '_validate_token', _valid)
    client = A.app.test_client()

    cases = (
        ('?flight_no=LH400&dep_iata=FRA&token=AT-other', 'query_not_allowed'),
        ('?flight_no=LH&dep_iata=FRA', 'need_flight_no_and_dep_iata'),
        ('?flight_no=LH400&dep_iata=FRANKFURT', 'need_flight_no_and_dep_iata'),
        ('?flight_no=LH400&dep_iata=FRA&date=17-08-2026', 'invalid_date'),
        ('?flight_no=LH400&dep_iata=FRA&arr_iata=123', 'invalid_arr_iata'),
        ('?flight_no=LH400&dep_iata=FRA&reg=not/a-tail', 'invalid_registration'),
        ('?flight_no=LH400&dep_iata=FRA&dep_iso=2026-08-17T10:00:00', 'invalid_dep_iso'),
    )
    for query, error in cases:
        response = _request(client, query)
        assert response.status_code == 400
        assert response.get_json() == {'ok': False, 'error': error}


def test_me_inbound_chain_reuses_truthful_builder_payload_without_url_credential(
    monkeypatch,
):
    monkeypatch.setattr(A, '_validate_token', _valid)
    seen = {}

    def build(**kwargs):
        seen.update(kwargs)
        return {
            'ok': True,
            'flight': kwargs['flight_no'],
            'date': kwargs['date'],
            'dep_iata': kwargs['dep_iata'],
            'reg': 'D-AIXY',
            'aircraft_type': 'A320',
            'inbound_flight_no': 'LH123',
            'inbound_origin': {'iata': 'MAD', 'city': 'Madrid'},
            'inbound_sched_arr': '2026-08-17T09:40:00+00:00',
            'inbound_est_arr': None,
            'inbound_delay_min': None,
            'inbound_live': None,
            'dep_delay_forecast': None,
        }

    monkeypatch.setattr(data, 'inbound_chain_payload', build)
    response = _request(
        A.app.test_client(),
        '?flight_no=lh%20400&date=2026-08-17&dep_iata=fra'
        '&reg=d-aixy&arr_iata=jfk&dep_iso=2026-08-17T10:00:00Z',
    )

    assert response.status_code == 200
    assert response.get_json()['inbound_origin'] == {'iata': 'MAD', 'city': 'Madrid'}
    assert response.get_json()['inbound_est_arr'] is None
    assert response.get_json()['dep_delay_forecast'] is None
    assert seen['flight_no'] == 'LH400'
    assert seen['dep_iata'] == 'FRA'
    assert seen['reg_hint'] == 'D-AIXY'
    assert seen['arr_iata'] == 'JFK'
    assert seen['dep_iso'].isoformat() == '2026-08-17T10:00:00+00:00'
    assert OWNER not in str(response.get_json())


def test_legacy_inbound_route_stays_registered_for_ios():
    source = open(data.__file__).read()
    assert "@aerox_data_bp.route('/api/ax/flight-inbound-chain/<token>', methods=['GET'])" in source
    assert "@app.route('/api/me/ax/flight-inbound-chain', methods=['GET'])" in open(A.__file__).read()
